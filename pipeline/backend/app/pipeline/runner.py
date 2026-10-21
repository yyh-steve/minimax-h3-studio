"""任务执行器：提交、轮询、下载、质检、错误分类、退避重试、启动对账、后台合成。

设计要点（对应方案 D6）：
- 每次状态变化先写 SQLite 再调外部接口。
- 提交前用 claim_submitting 原子占位，避免重复提交。
- 创建请求超时不判失败：转 unknown，由对账决定绑定还是人工处理。
- 重启后只看数据库状态即可续跑：有 task_id 继续轮询，已成功未下载重新下载，
  submitting 无 task_id 走对账，backoff 到点重提，checking 回退到 downloaded 重新质检。
- M4：下载后先做技术检查（黑场/冻帧/静音/时长），再交给 M3 看片；
  不通过就在预算和重试上限内新建一条 take，否则转人工。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .. import constants as C
from ..config import TAKES_DIR, Settings
from .. import db
from ..media import probe_video
from ..providers import ProviderError, get_m3, get_provider
from ..providers.base import classify_platform_failure
from ..providers.capabilities import validate_local_request


class Runner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._inflight: set[int] = set()
        self._checking: set[int] = set()
        self._rendering: set[int] = set()
        self._paused = False

    def _provider_for(self, row):
        if not row['idempotency_key'] or not row['provider_name'] or not row['provider_origin']:
            raise ProviderError('source_mismatch', '缺少原始提交身份或服务来源，停止自动恢复并转人工核对', retryable=False)
        if (row['provider_name'], row['provider_origin']) != (self.settings.provider, self.settings.h3_origin):
            raise ProviderError('source_mismatch', '任务属于另一 H3 服务，已停止向当前后端发送请求', retryable=False)
        return get_provider(self.settings)

    # ---------- lifecycle ----------
    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            db.add_event(None, "runner", "执行器已启动")

    async def stop(self) -> None:
        if self._task:
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                pass
            self._task = None

    def pause(self) -> None:
        self._paused = True
        db.add_event(None, "runner", "队列已暂停（不提交新任务；已提交的继续轮询）", level="warn")

    def resume(self) -> None:
        self._paused = False
        db.add_event(None, "runner", "队列已恢复")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def reconcile_on_startup(self) -> dict:
        """启动对账。返回各状态 take 的数量，供测试断言。"""
        counts: dict[str, int] = {}
        for row in db.list_active_takes():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        # 提交中但没有 task_id：平台状态未知，转 unknown 等待对账
        for row in db.list_active_takes():
            if row["status"] == C.SUBMITTING and not row["task_id"]:
                db.update_take(row["id"], status=C.UNKNOWN)
                db.add_event(row["id"], "reconcile",
                             "重启时发现提交中但无 task_id，转入结果未知对账", level="warn")
            elif row['status'] == C.CANCELLING:
                db.update_take(row['id'], status=C.REVIEW)
                db.add_event(row['id'], 'cancel', '取消请求中断，结果未知；请人工核对，不自动重新发送删除请求', level='warn')
        # 下载了一半（downloading）：回退到 succeeded，下一轮重新下载
        for row in db.list_active_takes():
            if row["status"] == C.DOWNLOADING:
                db.update_take(row["id"], status=C.SUCCEEDED)
        # 质检到一半（checking）：回退到 downloaded 重新质检（本地文件还在，不花 H3 的钱）
        for row in db.list_active_takes():
            if row["status"] == C.CHECKING and row["id"] not in self._checking:
                db.update_take(row["id"], status=C.DOWNLOADED, next_run_at=None)
                db.add_event(row["id"], "reconcile", "重启时发现质检中断，重新质检")
        # 合成到一半（rendering）：放回队列重新合成
        for r in db.list_renders(status="rendering", limit=200):
            if r["id"] not in self._rendering:
                db.update_render(r["id"], status="queued")
                db.add_event(None, "render", f"重启时发现合成 #{r['id']} 中断，重新排队",
                             project_id=r["project_id"])
        return counts

    # ---------- main loop ----------
    async def _loop(self) -> None:
        # 启动时先做一次对账
        try:
            self.reconcile_on_startup()
        except Exception as e:  # 对账失败不应杀死循环
            db.add_event(None, "reconcile", f"启动对账异常: {e}", level="error")

        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:  # 单轮异常不杀循环
                db.add_event(None, "runner", f"执行轮次异常: {e}", level="error")
            await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        # 1) 轮询所有有 task_id、平台未结束的 take（不占提交并发，不花新钱）
        await self._poll_all()

        # 2) 处理 succeeded 但本地还没下载的
        await self._download_all()

        # 3) 已落盘的做质检（技术检查 + M3 看片）
        await self._check_all()

        # 4) 提交 pending / 到期 backoff（受并发和暂停、预算约束）
        if not self._paused:
            if self.settings.provider == 'local':
                for row in db.list_active_takes():
                    if row['status'] == C.UNKNOWN and not row['task_id'] and row['id'] not in self._inflight:
                        await self._reconcile_unknown(row['id'])
            await self._submit_due()

        # 5) 合成队列（后台线程跑 ffmpeg，不阻塞轮询）
        await self._render_due()

    # ---------- submit ----------
    async def _submit_due(self) -> None:
        rows = db.list_pending_or_due()
        slots = 1 if self.settings.provider == 'local' else max(self.settings.max_concurrency, 1)
        if self.settings.provider == 'local' and any(
                row['status'] in (C.SUBMITTING, C.UNKNOWN, C.CANCELLING, C.QUEUED, C.RUNNING)
                for row in db.list_active_takes()):
            return
        if self.settings.provider == 'local':
            recovering = [row for row in db.list_active_takes() if row['status'] == C.BACKOFF]
            if recovering:
                # An ambiguous create may already occupy the local worker. Recover
                # that exact key before letting the next pending Take submit.
                recovery_id = recovering[0]['id']
                rows = [row for row in rows if row['id'] == recovery_id]
        inflight_submitting = len([i for i in self._inflight])
        for row in rows:
            if inflight_submitting >= slots:
                return
            if row["id"] in self._inflight:
                continue
            # 预算护栏只拦自动重试：首次确认和人工重试是人明确要花的钱
            req = _safe_json(row["request_json"]) or {}
            duration = int(req.get("duration", 0) or 0)
            if row["source"] == "auto_retry":
                limit = self._budget_for(row)
                used = (db.used_output_seconds(row["project_id"])
                        if row["project_id"] is not None else db.used_output_seconds())
                if used + duration > limit:
                    db.update_take(row["id"], status=C.REVIEW,
                                   error_json={"kind": "budget",
                                               "message": f"已用 {used}s，再生成 {duration}s 会超过预算 {limit}s，"
                                                          "停止自动重试，请人工决定"})
                    db.add_event(row["id"], "budget", "达到输出秒数预算，自动重试转人工",
                                 level="warn", project_id=row["project_id"])
                    continue
            self._inflight.add(row["id"])
            inflight_submitting += 1
            asyncio.create_task(self._submit_one(row["id"]))

    async def _submit_one(self, take_id: int) -> None:
        try:
            with db.transaction():
                db.bind_take_source(take_id, self.settings.provider, self.settings.h3_origin)
                if not db.claim_submitting(take_id):
                    return
                row = db.get_take(take_id)
            req = _safe_json(row["request_json"]) or {}
            db.add_event(take_id, "submit", "提交 H3 任务")
            try:
                provider = self._provider_for(row)
                if self.settings.provider == 'local':
                    validate_local_request(req)
                    result = await asyncio.to_thread(provider.create, req, idempotency_key=row['idempotency_key'])
                else:
                    result = await asyncio.to_thread(provider.create, req)
            except ValueError as e:
                await self._handle_create_error(take_id, ProviderError('bad_request', str(e), retryable=False))
                return
            except ProviderError as e:
                await self._handle_create_error(take_id, e)
                return
            db.update_take(take_id, task_id=result.task_id, status=C.QUEUED,
                           finished_at=None)
            db.add_event(take_id, "submit", f"任务已创建 task_id={result.task_id}")
        finally:
            self._inflight.discard(take_id)

    async def _handle_create_error(self, take_id: int, e: ProviderError) -> None:
        db.update_take(take_id, http_status=e.http_status, error_json=e.to_dict())
        if e.kind == 'source_mismatch' or (self.settings.provider == 'local' and e.http_status == 409):
            db.update_take(take_id, status=C.REVIEW)
            db.add_event(take_id, 'reconcile', '来源或幂等键冲突，转人工；不创建新任务', level='warn')
            return
        if self.settings.provider == 'official':
            if e.http_status == 429:
                await self._schedule_backoff(take_id, e)
                return
            if (e.http_status is not None and e.http_status >= 500
                    or e.kind in ('network', 'server', 'unknown')):
                db.update_take(take_id, status=C.UNKNOWN)
                db.add_event(take_id, 'reconcile', '官网创建结果未知，停止自动重提和猜测绑定；请人工确认任务 ID', level='warn')
                return
            if e.kind == 'rate_limit':
                db.update_take(take_id, status=C.NEEDS_EDIT)
                db.add_event(take_id, 'error', '官网未返回明确 HTTP 429，停止自动重提：' + e.message, level='warn')
                return
        if self.settings.provider in ('local', 'official') and e.kind in ('network', 'server', 'unknown'):
            db.update_take(take_id, status=C.UNKNOWN)
            if self.settings.provider == 'local':
                db.add_event(take_id, 'reconcile', '创建结果未知，使用本 Take 原幂等键恢复，不猜测任务', level='warn')
                await self._reconcile_unknown(take_id)
            else:
                db.add_event(take_id, 'reconcile', '官网创建结果未知，停止自动重提和猜测绑定；请人工确认任务 ID', level='warn')
            return
        if e.kind in ("rate_limit", "server") or (e.kind == "network" and e.retryable):
            await self._schedule_backoff(take_id, e)
            return
        if e.kind == "network" and not e.retryable:
            # 创建请求超时：平台状态未知，进入对账，绝不自动重提
            db.update_take(take_id, status=C.UNKNOWN)
            db.add_event(take_id, "reconcile",
                         "创建请求超时，转入结果未知对账（不自动重提，避免重复扣费）",
                         level="warn")
            await self._reconcile_unknown(take_id)
            return
        if e.kind in ("auth", "balance"):
            db.update_take(take_id, status=C.PAUSED)
            self.pause()
            db.add_event(take_id, "queue", f"{e.kind}：整队暂停 - {e.message}", level="error")
            return
        # bad_request / sensitive / 其它 4xx：需要人改
        db.update_take(take_id, status=C.NEEDS_EDIT)
        db.add_event(take_id, "error", f"待修改：{e.message}", level="error")

    # ---------- backoff ----------
    async def _schedule_backoff(self, take_id: int, e: ProviderError) -> None:
        """瞬时错误（429/5xx/网络）：在同一条 take 上退避后重提，不新建 take。"""
        row = db.get_take(take_id)
        attempt = _backoff_attempts(row)
        if attempt >= len(C.BACKOFF_SCHEDULE_SECONDS):
            db.update_take(take_id, status=C.REVIEW)
            db.add_event(take_id, "retry",
                         f"瞬时错误退避次数已用完，转人工：{e.message}", level="warn")
            return
        wait = C.BACKOFF_SCHEDULE_SECONDS[attempt]
        db.update_take(take_id, status=C.BACKOFF, next_run_at=time.time() + wait)
        db.add_event(take_id, "retry",
                     f"{e.kind}，{wait}s 后自动重试（第 {attempt + 1}/{len(C.BACKOFF_SCHEDULE_SECONDS)} 次）",
                     level="warn")

    async def _spawn_retry_take(self, parent_id: int, reason: str, *,
                                expected_status: str | None = None) -> int | None:
        with db.transaction():
            return self._spawn_retry_take_locked(parent_id, reason, expected_status=expected_status)

    def _spawn_retry_take_locked(self, parent_id: int, reason: str, *,
                                 expected_status: str | None = None) -> int | None:
        """平台失败 / 质检不通过后的分镜级重试：旧 take 置 rejected，新建一条 take 退避后提交。

        受 settings.auto_retry_limit 限制（默认 1）：只数连续的自动重试，
        人工点过重试后重新计数。超限则旧 take 转 review。
        返回新 take_id；无法再重试时返回 None。
        """
        parent = db.get_take(parent_id)
        if (parent is None or expected_status is not None and parent['status'] != expected_status
                or parent['status'] in C.TERMINAL_STATES | {C.CANCELLING}
                or (_safe_json(parent['qc_json']) or {}).get('decision', {}).get('by') == 'human'):
            return None
        if self.settings.provider == 'local' or parent['provider_name'] == 'local':
            db.update_take(parent_id, status=C.REVIEW)
            db.add_event(parent_id, 'retry', '本地 H3 不自动重新生成；保留结果并转人工：' + reason,
                         level='warn', project_id=parent['project_id'])
            return None
        auto_used = _auto_retry_streak(parent)
        if auto_used >= self.settings.auto_retry_limit:
            db.update_take(parent_id, status=C.REVIEW)
            db.add_event(parent_id, "retry",
                         f"自动重试次数已用完（上限 {self.settings.auto_retry_limit}），转人工：{reason}",
                         level="warn", project_id=parent["project_id"])
            return None
        req = _safe_json(parent["request_json"]) or {}
        new_id = db.create_take(
            project_id=parent["project_id"], storyboard_id=parent["storyboard_id"],
            shot_key=parent["shot_key"], attempt_no=int(parent["attempt_no"]) + 1,
            parent_take_id=parent_id, title=parent["title"], model=parent["model"],
            request_json=req, fingerprint=parent["fingerprint"] or "", source="auto_retry",
            provider_name=parent['provider_name'], provider_origin=parent['provider_origin'])
        wait = C.BACKOFF_SCHEDULE_SECONDS[0]
        db.update_take(new_id, status=C.BACKOFF, next_run_at=time.time() + wait)
        db.update_take(parent_id, status=C.REJECTED)
        db.add_event(parent_id, "retry",
                     f"旧结果不通过（{reason}），新建第 {parent['attempt_no'] + 1} 条 Take，{wait}s 后提交",
                     level="warn", project_id=parent["project_id"])
        db.add_event(new_id, "retry", f"作为重试自动创建，{wait}s 后提交",
                     project_id=parent["project_id"])
        return new_id

    # ---------- poll ----------
    async def _poll_all(self) -> None:
        rows = [r for r in db.list_active_takes()
                if r["task_id"] and r["status"] in (C.QUEUED, C.RUNNING)]
        if not rows:
            return
        # 批量查询；mock/官网都支持 list_tasks。失败则这一轮跳过。
        compatible = []
        provider = None
        for row in rows:
            try:
                provider = self._provider_for(row)
                compatible.append(row)
            except ProviderError as error:
                db.update_take_if(row['id'], expected_status=row['status'],
                                  expected_task_id=row['task_id'], status=C.REVIEW,
                                  error_json=error.to_dict())
        rows = compatible
        if not rows:
            return
        ids = [r["task_id"] for r in rows]
        try:
            infos = await asyncio.to_thread(provider.list_tasks, ids, 20)
        except ProviderError as e:
            db.add_event(None, "poll", f"批量查询失败，下轮再查：{e.message}", level="warn")
            return
        for row in rows:
            info = infos.get(row["task_id"])
            if info is None:
                # 批量没返回：退化为单查，顺便覆盖"超过7天查不到"
                try:
                    info = await asyncio.to_thread(provider.get_task, row["task_id"])
                except ProviderError as e:
                    db.add_event(row["id"], "poll", f"单查失败：{e.message}", level="warn")
                    continue
            await self._apply_task_info(row["id"], info, expected_status=row['status'])

    async def _apply_task_info(self, take_id: int, info, *, expected_status: str | None = None) -> None:
        with db.transaction():
            self._apply_task_info_locked(take_id, info, expected_status=expected_status)

    def _apply_task_info_locked(self, take_id: int, info, *, expected_status: str | None = None) -> None:
        row = db.get_take(take_id)
        if (row is None or row['status'] not in (C.QUEUED, C.RUNNING)
                or row['task_id'] != info.task_id
                or expected_status is not None and row['status'] != expected_status):
            return
        if not info.found:
            # 平台查不到。提交超过 7 天判过期；否则可能只是短暂不一致，保持现状
            age = time.time() - (row["submit_started_at"] or time.time())
            if age > C.PLATFORM_TTL_SECONDS:
                db.update_take(take_id, status=C.EXPIRED)
                db.add_event(take_id, "expire", "平台已查不到（超过 7 天），标记过期", level="warn")
            return

        if info.status in ("queued", "running"):
            if row["status"] != info.status:
                db.update_take(take_id, status=info.status, platform_status=info.status)
                db.add_event(take_id, "poll", f"平台状态：{info.status}")
            return

        if info.status == "succeeded":
            db.update_take(take_id, status=C.SUCCEEDED, platform_status="succeeded",
                           platform_ratio=info.ratio, platform_resolution=info.resolution,
                           platform_duration=info.duration, usage_json=info.usage,
                           raw_response_json=json.dumps(info.raw, ensure_ascii=False) if info.raw else None)
            db.add_event(take_id, "poll", "平台生成成功，等待下载")
            return

        if info.status == "failed":
            pe = classify_platform_failure(info.error_code, info.error_message)
            if row['provider_name'] == 'local' and info.error_code == 'invalid_media':
                pe.kind = 'bad_request'
            self._handle_platform_failure_locked(take_id, pe, expected_status=row['status'])
            return

        if info.status == "cancelled":
            db.update_take(take_id, status=C.CANCELLED, platform_status="cancelled")
            db.add_event(take_id, "cancel", "任务已取消")
            return

    async def _handle_platform_failure(self, take_id: int, e: ProviderError) -> None:
        with db.transaction():
            self._handle_platform_failure_locked(take_id, e)

    def _handle_platform_failure_locked(self, take_id: int, e: ProviderError, *,
                                       expected_status: str | None = None) -> None:
        row = db.get_take(take_id)
        if (row is None or row['status'] not in (C.QUEUED, C.RUNNING)
                or expected_status is not None and row['status'] != expected_status):
            return
        db.update_take(take_id, http_status=e.http_status, error_json=e.to_dict(),
                       platform_status="failed")
        row = db.get_take(take_id)
        if row['provider_name'] == 'local' or self.settings.provider == 'local':
            status = C.NEEDS_EDIT if e.kind in ('bad_request', 'sensitive') else C.REVIEW
            db.update_take(take_id, status=status)
            db.add_event(take_id, 'error', '本地 H3 运行失败，转人工处理且不自动重算：' + e.message, level='warn')
            return
        if e.kind in ("auth", "balance"):
            db.update_take(take_id, status=C.PAUSED)
            self.pause()
            db.add_event(take_id, "queue", f"{e.kind}：整队暂停 - {e.message}", level="error")
            return
        if e.kind in ("sensitive", "bad_request"):
            db.update_take(take_id, status=C.NEEDS_EDIT)
            db.add_event(take_id, "error", f"平台失败，待修改：{e.message}", level="error")
            return
        # 平台侧运行失败（rate_limit / server / 未知码）：新建一条 Take 重试
        self._spawn_retry_take_locked(take_id, e.message, expected_status=row['status'])

    # ---------- download ----------
    async def _download_all(self) -> None:
        rows = [r for r in db.list_active_takes() if r["status"] == C.SUCCEEDED]
        for row in rows:
            if row["id"] in self._inflight:
                continue
            self._inflight.add(row["id"])
            asyncio.create_task(self._download_one(row["id"]))

    async def _download_one(self, take_id: int) -> None:
        try:
            row = db.get_take(take_id)
            if row is None or row['status'] != C.SUCCEEDED or not row['task_id']:
                return
            expected_task_id = row['task_id']
            def update_if_downloading(**fields):
                return db.update_take_if(take_id, expected_status=C.DOWNLOADING,
                                         expected_task_id=expected_task_id, **fields)
            try:
                provider = self._provider_for(row)
                # Always query a fresh URL: cached signatures can expire during a long pause.
                info = await asyncio.to_thread(provider.get_task, row['task_id'])
            except ProviderError as e:
                if e.kind == 'source_mismatch':
                    db.update_take_if(take_id, expected_status=C.SUCCEEDED,
                                      expected_task_id=expected_task_id,
                                      status=C.REVIEW, error_json=e.to_dict())
                db.add_event(take_id, 'download', '重新查询失败：' + e.message, level='warn')
                return
            if not info.found:
                db.update_take_if(take_id, expected_status=C.SUCCEEDED, expected_task_id=expected_task_id,
                                  status=C.EXPIRED if time.time() - (row['submit_started_at'] or time.time()) > C.PLATFORM_TTL_SECONDS else C.REVIEW)
                db.add_event(take_id, 'download', '任务已过期、删除或不可查询，停止自动下载', level='warn')
                return
            if info.status != 'succeeded' or not info.video_url:
                db.update_take_if(take_id, expected_status=C.SUCCEEDED,
                                  expected_task_id=expected_task_id, status=C.REVIEW)
                db.add_event(take_id, 'download', '任务不再成功或缺少下载链接，转人工处理', level='warn')
                return
            url = info.video_url
            if not db.update_take_if(take_id, expected_status=C.SUCCEEDED,
                                     expected_task_id=expected_task_id, status=C.DOWNLOADING):
                return
            try:
                content = await asyncio.to_thread(provider.download, url)
            except ProviderError as e:
                update_if_downloading(status=C.SUCCEEDED)  # 回到可下载状态
                db.add_event(take_id, "download", f"下载失败，下轮重试：{e.message}", level="warn")
                return

            TAKES_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = TAKES_DIR / f"take_{take_id}.part"
            final_path = TAKES_DIR / f"take_{take_id}.mp4"
            tmp_path.write_bytes(content)
            try:
                probe = probe_video(tmp_path)
            except Exception as e:
                tmp_path.unlink(missing_ok=True)
                update_if_downloading(status=C.SUCCEEDED)
                db.add_event(take_id, "download", f"成片校验失败，丢弃重下：{e}", level="error")
                return
            with db.transaction():
                current = db.get_take(take_id)
                if current['status'] != C.DOWNLOADING or current['task_id'] != expected_task_id:
                    tmp_path.unlink(missing_ok=True)
                    return
                tmp_path.replace(final_path)
                update_if_downloading(status=C.DOWNLOADED, local_path=str(final_path),
                                      probe_json=json.dumps(probe, ensure_ascii=False),
                                      finished_at=time.time(), next_run_at=None, qc_json=None)
            db.add_event(take_id, "download",
                         f"成片已下载并校验通过 {probe['width']}x{probe['height']} "
                         f"{probe['duration']:.2f}s，等待质检", project_id=row["project_id"])
        finally:
            self._inflight.discard(take_id)

    # ---------- M4：质检（技术检查 + M3 看片）----------
    async def _check_all(self) -> None:
        free = C.MAX_PARALLEL_CHECKS - len(self._checking)
        if free <= 0:
            return
        for row in db.list_takes_for_check(limit=free):
            if row["id"] in self._checking:
                continue
            if not db.claim_checking(row["id"]):
                continue
            self._checking.add(row["id"])
            asyncio.create_task(self._check_one(row["id"]))

    async def _check_one(self, take_id: int) -> None:
        try:
            await self._check_one_inner(take_id)
        except Exception as e:     # 质检自身出错不能卡住流水线：转人工，保留成片
            db.update_take(take_id, status=C.REVIEW,
                           error_json={"kind": "qc_error", "message": f"质检异常：{e}"})
            db.add_event(take_id, "qc", f"质检异常，转人工：{e}", level="error")
        finally:
            self._checking.discard(take_id)

    async def _check_one_inner(self, take_id: int) -> None:
        from ..qc import run_tech_check
        from ..review_inputs import build_review_context

        row = db.get_take(take_id)
        pid = row["project_id"]
        qc = _safe_json(row["qc_json"]) or {}
        probe = _safe_json(row["probe_json"]) or {}
        path = row["local_path"]
        if not path or not Path(path).exists():
            # 本地文件丢了：回到 succeeded 让下载流程重新拉（限时链接失效会自动重新查询）
            db.update_take(take_id, status=C.SUCCEEDED, local_path=None)
            db.add_event(take_id, "qc", "本地成片缺失，重新下载", level="warn", project_id=pid)
            return
        ctx = await asyncio.to_thread(build_review_context, row)
        project_ratio = None
        if pid is not None:
            project = db.get_project(int(pid))
            project_ratio = project["ratio"] if project else None

        # 1) 技术检查（已做过就不重复，例如 M3 看片失败后的重试）
        if "tech" not in qc:
            tech = await asyncio.to_thread(
                run_tech_check, path, probe,
                expected_duration=float(ctx["expected_duration"] or 0) or None,
                expected_ratio=project_ratio or ctx.get("request_ratio"),
                expect_voice=ctx["expect_voice"])
            qc["tech"] = tech.to_dict()
            db.update_take(take_id, qc_json=qc)
            if not tech.passed:
                reason = "；".join(i.message for i in tech.issues if i.severity == "blocker")
                qc["decision"] = {"result": "rejected", "by": "tech", "reason": reason, "ts": time.time()}
                db.update_take(take_id, qc_json=qc, error_json={"kind": "qc_tech", "message": reason})
                db.add_event(take_id, "qc", f"技术检查不通过：{reason}", level="warn", project_id=pid)
                await self._spawn_retry_take(take_id, f"技术检查不通过：{reason}", expected_status=C.CHECKING)
                return
            note = "；".join(i.message for i in tech.issues) or "无问题"
            db.add_event(take_id, "qc", f"技术检查通过（{note}）", project_id=pid)

        # 2) M3 看片
        if not self.settings.m3_take_review:
            qc["m3"] = {"skipped": True, "reason": "设置里关闭了 M3 看片"}
            qc["decision"] = {"result": "accepted", "by": "tech", "ts": time.time()}
            db.update_take(take_id, status=C.ACCEPTED, qc_json=qc, error_json=None)
            db.add_event(take_id, "qc", "技术检查通过，已采纳（未开启 M3 看片）", project_id=pid)
            self._follow_shot_selection(take_id)
            return

        from ..review_inputs import make_review_proxy, video_data_uri
        from ..providers import M3Error
        proxy = Path(path).with_name(f"take_{take_id}_review.mp4")
        try:
            await asyncio.to_thread(make_review_proxy, path, proxy)
            uri = video_data_uri(proxy)
        finally:
            proxy.unlink(missing_ok=True)
        m3 = get_m3(self.settings)
        try:
            review = await asyncio.to_thread(
                m3.review_take, video_data_uri=uri, ref_image_uris=ctx["ref_image_uris"],
                shot_text=ctx["shot_text"], fps=float(self.settings.m3_review_fps),
                detail=self.settings.m3_review_detail,
                context_key=f"{pid}:{row['shot_key']}:{row['fingerprint'] or take_id}")
        except M3Error as e:
            attempts = int(qc.get("m3_attempts", 0)) + 1
            qc["m3_attempts"] = attempts
            qc["m3_last_error"] = str(e)[:300]
            if e.fatal or attempts > len(C.M3_REVIEW_BACKOFF_SECONDS):
                qc["decision"] = {"result": "review", "by": "m3_error", "reason": str(e)[:200],
                                  "ts": time.time()}
                db.update_take(take_id, status=C.REVIEW, qc_json=qc,
                               error_json={"kind": "m3_review", "message": f"M3 看片失败：{e}"})
                db.add_event(take_id, "qc", f"M3 看片失败，转人工（成片可直接采纳）：{e}",
                             level="warn", project_id=pid)
                return
            wait = C.M3_REVIEW_BACKOFF_SECONDS[attempts - 1]
            db.update_take(take_id, status=C.DOWNLOADED, qc_json=qc,
                           next_run_at=time.time() + wait)
            db.add_event(take_id, "qc", f"M3 看片调用失败，{wait}s 后重试：{e}",
                         level="warn", project_id=pid)
            return

        verdict = review.final_verdict()
        qc["m3"] = {**review.model_dump(), "final_verdict": verdict,
                    "fps": float(self.settings.m3_review_fps),
                    "detail": self.settings.m3_review_detail,
                    "ref_images": len(ctx["ref_image_uris"])}
        if verdict == "pass":
            qc["decision"] = {"result": "accepted", "by": "m3", "ts": time.time()}
            db.update_take(take_id, status=C.ACCEPTED, qc_json=qc, error_json=None)
            db.add_event(take_id, "qc", f"M3 看片通过：{review.summary}", project_id=pid)
            self._follow_shot_selection(take_id)
        elif verdict == "fail":
            reason = review.summary or "；".join(i.message for i in review.issues)
            qc["decision"] = {"result": "rejected", "by": "m3", "reason": reason, "ts": time.time()}
            db.update_take(take_id, qc_json=qc, error_json={"kind": "qc_m3", "message": reason})
            db.add_event(take_id, "qc", f"M3 看片不通过：{reason}", level="warn", project_id=pid)
            await self._spawn_retry_take(take_id, f"M3 看片不通过：{reason}", expected_status=C.CHECKING)
        else:
            qc["decision"] = {"result": "review", "by": "m3", "reason": review.summary,
                              "ts": time.time()}
            db.update_take(take_id, status=C.REVIEW, qc_json=qc,
                           error_json={"kind": "qc_uncertain",
                                       "message": f"M3 拿不准：{review.summary}"})
            db.add_event(take_id, "qc", f"M3 看片拿不准，转人工：{review.summary}",
                         level="warn", project_id=pid)

    # ---------- reconcile unknown ----------
    def _budget_for(self, row) -> int:
        """项目任务：目标时长×3；无项目的临时 take：全局预算。"""
        if row["project_id"] is None:
            return self.settings.budget_output_seconds
        project = db.get_project(row["project_id"])
        if project is None:
            return self.settings.budget_output_seconds
        return max(int(project["target_seconds"]) * 3,
                   1)

    def _follow_shot_selection(self, take_id: int) -> None:
        """take 通过后，把指向它重试家族里其它 take 的镜头选用改到它身上。

        覆盖：首次确认的 take 被自动重试替换、改稿复用期间旧 take 重试成功等情况。
        已采纳的选用不动（不抢人工选定或已经通过的结果），跨分镜版本同样生效。
        """
        row = db.get_take(take_id)
        if row is None or row["project_id"] is None:
            return
        family = db.take_lineage_ids(take_id)
        changed = db.retarget_selection(family, take_id, keep_accepted=True)
        for sb_id, shot_key in changed:
            db.add_event(take_id, "select",
                         f"分镜 #{sb_id} 的镜头 {shot_key} 选用跟随到新 Take #{take_id}",
                         project_id=row["project_id"])

    # ---------- M4：后台合成 ----------
    async def _render_due(self) -> None:
        if len(self._rendering) >= C.RENDER_MAX_PARALLEL:
            return
        queued = sorted(db.list_renders(status="queued", limit=20), key=lambda r: r["id"])
        for r in queued:
            if len(self._rendering) >= C.RENDER_MAX_PARALLEL:
                return
            if r["id"] in self._rendering or not db.claim_render(r["id"]):
                continue
            self._rendering.add(r["id"])
            asyncio.create_task(self._render_one(r["id"]))

    async def _render_one(self, render_id: int) -> None:
        from .render_service import execute_render
        try:
            await asyncio.to_thread(execute_render, render_id)
        except Exception as e:      # execute_render 自己会落库失败原因，这里兜底
            db.update_render(render_id, status="failed", error=str(e)[:500],
                             finished_at=time.time())
        finally:
            self._rendering.discard(render_id)

    async def _reconcile_unknown(self, take_id: int) -> None:
        """创建超时后的对账。

        本地 H3 沿用持久化幂等键恢复；官网保持 unknown 等人工绑定明确的任务 ID。
        只有测试用 mock 保留内存查找路径。
        """
        await asyncio.sleep(0.1)  # 让平台状态先落定
        row = db.get_take(take_id)
        if row is None or row['status'] != C.UNKNOWN or row['task_id']:
            return
        try:
            provider = self._provider_for(row)
        except ProviderError as error:
            db.update_take(take_id, status=C.REVIEW, error_json=error.to_dict())
            return
        if self.settings.provider == 'local':
            request = _safe_json(row['request_json']) or {}
            try:
                validate_local_request(request)
                result = await asyncio.to_thread(provider.create, request, idempotency_key=row['idempotency_key'])
            except ValueError as error:
                db.update_take(take_id, status=C.NEEDS_EDIT, error_json={'kind': 'bad_request', 'message': str(error)})
            except ProviderError as error:
                if error.kind in ('network', 'server', 'rate_limit', 'unknown'):
                    await self._schedule_backoff(take_id, error)
                else:
                    await self._handle_create_error(take_id, error)
            else:
                db.update_take(take_id, task_id=result.task_id, status=C.QUEUED, next_run_at=None)
                db.add_event(take_id, 'reconcile', '通过原幂等键恢复任务 ID；未生成新幂等身份')
            return

        # mock：直接列出最近任务匹配
        from ..providers import MockH3
        if isinstance(provider, MockH3):
            matched = None
            for tid in list(provider._tasks.keys()):
                if tid in db.bound_task_ids():
                    continue
                info = await asyncio.to_thread(provider.get_task, tid)
                if not info.found:
                    continue
                # 简单核对：时长/分辨率一致
                req = _safe_json(row["request_json"]) or {}
                if str(info.duration) == str(req.get("duration")) and \
                   (info.resolution or "") == (req.get("resolution") or info.resolution):
                    matched = info
                    break
            if matched is not None:
                db.update_take(take_id, task_id=matched.task_id, status=C.QUEUED)
                db.add_event(take_id, "reconcile",
                             f"对账唯一命中，绑定 task_id={matched.task_id}")
            else:
                db.add_event(take_id, "reconcile",
                             "对账未命中候选，保持结果未知，等待人工决定", level="warn")
            return

        # Official requests have no advertised idempotency guarantee here.
        # A similar timestamp/duration does not identify a request.
        db.add_event(take_id, 'reconcile', '官网未知任务不按时间或时长猜测绑定，请人工确认任务 ID', level='warn')


def _safe_json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _backoff_attempts(row) -> int:
    """从事件里数这个 take 已经退避过几次。"""
    return len([e for e in db.list_events(row["id"], limit=50) if e["type"] == "retry"])


def _auto_retry_streak(row) -> int:
    """从当前 take 往上数连续的自动重试次数；遇到首次确认或人工重试就停。"""
    count = 0
    cur = row
    seen: set[int] = set()
    while cur is not None and cur["source"] == "auto_retry" and cur["id"] not in seen:
        seen.add(cur["id"])
        count += 1
        if cur["parent_take_id"] is None:
            break
        cur = db.get_take(int(cur["parent_take_id"]))
    return count
