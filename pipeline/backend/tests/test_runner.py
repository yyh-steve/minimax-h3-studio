"""Runner 行为测试：成功、429、422、402、创建超时对账、取消、平台失败重试、重启续跑。"""
from __future__ import annotations

import asyncio
import json

import pytest


def make_take(env, duration: int = 4, shot_key: str = "s1") -> int:
    req = {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "镜头提示词：水果落入杯中，镜头推近，画面无文字"}],
        "duration": duration,
        "resolution": "768P",
        "ratio": "9:16",
    }
    return env.db.create_take(model="MiniMax-H3", request_json=req,
                              title="测试镜头", shot_key=shot_key)


def set_scenario(env, name: str) -> None:
    """同时设置运行配置和 mock：runner 每次取 provider 都会按 settings 重置场景。"""
    env.settings.mock_scenario = name
    env.mock.set_scenario(name)


async def pump(env, rounds: int = 40, delay: float = 0.2) -> None:
    """推进执行器若干轮，给后台 to_thread / ffmpeg 留执行时间。"""
    for _ in range(rounds):
        await env.runner._tick()
        await asyncio.sleep(delay)


def status(env, take_id: int) -> str:
    return env.db.get_take(take_id)["status"]


async def wait_status(env, take_id: int, targets: set[str], rounds: int = 60) -> str:
    for _ in range(rounds):
        await env.runner._tick()
        await asyncio.sleep(0.2)
        if status(env, take_id) in targets:
            return status(env, take_id)
    return status(env, take_id)


# 1) 成功链路：提交 -> 平台成功 -> 下载 -> ffprobe 校验 -> accepted
async def test_success_end_to_end(env):
    set_scenario(env, "success")
    tid = make_take(env)
    await wait_status(env, tid, {"accepted", "review", "needs_edit", "paused"}, rounds=50)

    row = env.db.get_take(tid)
    assert row["status"] == "accepted"
    assert row["task_id"]
    assert row["local_path"]
    from app.media import probe_video
    probe = probe_video(row["local_path"])
    assert (probe["width"], probe["height"]) == (768, 1344)
    assert probe["has_audio"]
    assert abs(probe["duration"] - 4.0) < 0.6
    usage = json.loads(row["usage_json"])
    assert usage["output_seconds"] == 4
    assert len(env.mock._tasks) == 1          # 没有重复提交


# 2) 首次 429：退避后自动重试成功，429 那次没有在平台建任务
async def test_rate_limit_then_success(env):
    set_scenario(env, "rate_limit")
    tid = make_take(env)
    await wait_status(env, tid, {"accepted", "review"}, rounds=60)

    assert status(env, tid) == "accepted"
    assert len(env.mock._tasks) == 1          # 仅成功的那次建成了任务
    kinds = [e["message"] for e in env.db.list_events(tid, limit=50) if e["type"] == "retry"]
    assert any("rate_limit" in m for m in kinds)


# 3) 422 内容敏感：标记待修改，不自动重试，不新建 take
async def test_sensitive_needs_edit(env):
    set_scenario(env, "sensitive")
    tid = make_take(env)
    await wait_status(env, tid, {"needs_edit", "accepted", "review"})

    assert status(env, tid) == "needs_edit"
    assert env.db.get_take(tid)["attempt_no"] == 1
    assert len(env.db.list_takes()) == 1


# 4) 402 余额不足：该 take 暂停，整个队列暂停，已提交任务不丢
async def test_balance_pauses_queue(env):
    set_scenario(env, "balance")
    tid = make_take(env)
    await wait_status(env, tid, {"paused", "accepted", "needs_edit"})

    assert status(env, tid) == "paused"
    assert env.runner.is_paused is True
    err = json.loads(env.db.get_take(tid)["error_json"])
    assert err["kind"] == "balance" and err["code"] == 1008


# 5) 创建请求超时：平台其实建成了任务 -> unknown -> 对账绑定 -> 成功
async def test_create_timeout_reconcile_bind(env):
    set_scenario(env, "create_timeout")
    tid = make_take(env)
    await wait_status(env, tid, {"accepted", "review"}, rounds=60)

    row = env.db.get_take(tid)
    assert row["status"] == "accepted"
    assert row["task_id"]                      # 对账绑定上了平台任务
    assert len(env.mock._tasks) == 1           # 没有重复提交
    msgs = [e["message"] for e in env.db.list_events(tid, limit=50)]
    assert any("对账唯一命中" in m for m in msgs)


# 6) queued 可以取消，取消后不下载、不扣费（本地无成片）
async def test_cancel_queued(env):
    set_scenario(env, "cancel_ok")
    tid = make_take(env)
    # 等到 queued（窗口只有 0.3s）
    for _ in range(20):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        if status(env, tid) == "queued":
            break
    assert status(env, tid) == "queued"

    row = env.db.get_take(tid)
    result = env.mock.cancel(row["task_id"])
    assert result["action"] == "cancelled"
    await wait_status(env, tid, {"cancelled", "running", "succeeded"}, rounds=10)

    assert status(env, tid) == "cancelled"
    assert env.db.get_take(tid)["local_path"] is None


# 7) 平台运行失败：旧 take 置 rejected，自动新建一条 take 重试成功
async def test_platform_failure_auto_retry_succeeds(env):
    set_scenario(env, "success")
    tid = make_take(env)
    # 等第一个任务进队列后，人为把平台任务改成失败
    for _ in range(20):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        if env.db.get_take(tid)["task_id"]:
            break
    platform_id = env.db.get_take(tid)["task_id"]
    env.mock._tasks[platform_id].fail = True

    await wait_status(env, tid, {"rejected", "review", "accepted"}, rounds=60)
    # 旧 take 被拒绝
    assert status(env, tid) == "rejected"
    takes = env.db.list_takes()
    assert len(takes) == 2
    new_tid = [t["id"] for t in takes if t["id"] != tid][0]
    await wait_status(env, new_tid, {"accepted", "review"}, rounds=60)
    assert status(env, new_tid) == "accepted"
    assert env.db.get_take(new_tid)["parent_take_id"] == tid


# 8) 自动重试次数用完：第二次仍失败则转人工，不无限重试
async def test_retry_limit_exhausted_then_review(env):
    set_scenario(env, "fail_running")
    tid = make_take(env)
    # 等两条 take 都落定
    final_id = tid
    for _ in range(80):
        await env.runner._tick()
        await asyncio.sleep(0.15)
        takes = env.db.list_takes()
        if len(takes) >= 2 and all(t["status"] in ("review", "rejected", "accepted",
                                                   "needs_edit", "paused", "cancelled",
                                                   "expired") for t in takes):
            final_id = max(t["id"] for t in takes)
            if status(env, final_id) == "review":
                break
    takes = env.db.list_takes()
    assert len(takes) == 2                     # 根 + 1 次自动重试，没有第 3 条
    assert status(env, tid) == "rejected"
    assert status(env, max(t["id"] for t in takes)) == "review"


# 9) 重启续跑：任务已在平台排队，换新 Runner 后继续轮询，不重复提交
async def test_restart_resume_without_resubmit(env):
    set_scenario(env, "success")
    tid = make_take(env)
    for _ in range(20):
        await env.runner._tick()
        await asyncio.sleep(0.05)
        if status(env, tid) in ("queued", "running"):
            break
    assert status(env, tid) in ("queued", "running")
    tasks_before = len(env.mock._tasks)

    # 模拟重启：新建 Runner（同一 db、同一平台侧 mock），启动对账
    from app.pipeline.runner import Runner
    runner2 = Runner(env.settings)
    counts = runner2.reconcile_on_startup()
    assert set(counts).intersection({"queued", "running"})
    for _ in range(50):
        await runner2._tick()
        await asyncio.sleep(0.2)
        if status(env, tid) == "accepted":
            break
    assert status(env, tid) == "accepted"
    assert len(env.mock._tasks) == tasks_before     # 没有重新创建任务


# 10) 提交占位是原子的：并发 claim 只有一个成功
async def test_claim_submitting_is_atomic(env):
    tid = make_take(env)
    assert env.db.claim_submitting(tid) is True
    assert env.db.claim_submitting(tid) is False


# 11) 下载到一半重启：downloading 回退 succeeded，重新下载成功
async def test_downloading_restart_redownloads(env):
    set_scenario(env, "success")
    tid = make_take(env)
    await wait_status(env, tid, {"accepted"}, rounds=40)
    # 人为把状态打回 downloading、清掉本地文件，模拟下载中断后重启
    env.db.update_take(tid, status="downloading", local_path=None)
    from app.pipeline.runner import Runner
    runner2 = Runner(env.settings)
    runner2.reconcile_on_startup()
    for _ in range(50):
        await runner2._tick()
        await asyncio.sleep(0.2)
        if status(env, tid) == "accepted":
            break
    assert status(env, tid) == "accepted"
    assert env.db.get_take(tid)["local_path"]
