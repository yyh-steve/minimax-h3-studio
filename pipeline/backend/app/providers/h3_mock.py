"""本地假 H3（MockProvider），不花钱，用来打通重试/续跑/对账。

和 OfficialH3 实现同一组方法：create / get_task / list_tasks / cancel / download。
任务存在内存里（重启后丢失，正好模拟"平台侧状态以 task_id 为准"），
成片用 ffmpeg 现生成一个真 mp4 并缓存到 data/mock_media，下载读字节。

场景 mock_scenario：
  success        排队 -> 运行 -> 成功
  rate_limit     第 1 次 create 返回 429，之后成功（验证退避自动重试）
  sensitive      create 返回 422 内容敏感（验证 needs_edit）
  balance        create 返回 402 余额不足（验证整队暂停）
  create_timeout create 请求"超时"，但平台其实建成了任务（验证 unknown -> 对账绑定）
  fail_running   任务跑到 failed（验证平台失败分类）
  cancel_ok      任务停在 queued，等待取消（验证 queued 可取消、running 不可取消）
  black_once     第 1 个任务的成片前 2 秒是黑场（验证技术检查不通过 -> 自动重试），之后正常
"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import TAKES_DIR
from .base import CreateResult, ProviderError, TaskInfo, classify_platform_failure

QUEUE_SECONDS = 1.5      # 排队停留
RUN_SECONDS = 2.5        # 运行停留
MOCK_DIR = TAKES_DIR.parent / "mock_media"


@dataclass
class MockTask:
    id: str
    status: str = "queued"          # queued | running | succeeded | failed | cancelled
    created_at: float = field(default_factory=time.time)
    request_json: dict = field(default_factory=dict)
    fail: bool = False
    black_head: bool = False        # 成片开头 2 秒黑场（技术检查应判不通过）
    queue_seconds: float = QUEUE_SECONDS
    run_seconds: float = RUN_SECONDS

    def current_status(self) -> str:
        if self.status in ("succeeded", "failed", "cancelled"):
            return self.status
        elapsed = time.time() - self.created_at
        if elapsed < self.queue_seconds:
            return "queued"
        if self.fail:
            self.status = "failed"
            return "failed"
        if elapsed < self.queue_seconds + self.run_seconds:
            return "running"
        self.status = "succeeded"
        return "succeeded"


class MockH3:
    def __init__(self, queue_seconds: float = QUEUE_SECONDS,
                 run_seconds: float = RUN_SECONDS) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, MockTask] = {}
        self._scenario = "success"
        self._rate_limited_once = False
        self._black_done = False
        self._queue_seconds = queue_seconds
        self._run_seconds = run_seconds

    def set_timing(self, queue_seconds: float, run_seconds: float) -> None:
        self._queue_seconds = queue_seconds
        self._run_seconds = run_seconds

    def set_scenario(self, scenario: str) -> None:
        # 只在场景真正切换时重置一次性状态（如 429 的"仅首次"标记）；
        # runner 每次取 provider 都会调本方法，不能把已经发生过的 429 重置掉。
        with self._lock:
            if scenario == self._scenario:
                return
            self._scenario = scenario
            self._rate_limited_once = False
            self._black_done = False

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._rate_limited_once = False
            self._black_done = False

    # ---------- create ----------
    def create(self, request_json: dict) -> CreateResult:
        scenario = self._scenario
        if scenario == "rate_limit" and not self._rate_limited_once:
            self._rate_limited_once = True
            raise ProviderError(kind="rate_limit", message="rate limit, please retry later (1002)",
                                http_status=429, code=1002, retryable=True)
        if scenario == "sensitive":
            raise ProviderError(kind="sensitive",
                                message="video description contains sensitive content (1026)",
                                http_status=422, code=1026, retryable=False)
        if scenario == "balance":
            raise ProviderError(kind="balance", message="insufficient balance (1008)",
                                http_status=402, code=1008, retryable=False)

        task = MockTask(id=uuid.uuid4().hex[:18], request_json=request_json,
                        fail=(scenario == "fail_running"),
                        queue_seconds=self._queue_seconds,
                        run_seconds=self._run_seconds)
        with self._lock:
            if scenario == "black_once" and not self._black_done:
                self._black_done = True
                task.black_head = True
            self._tasks[task.id] = task
            # create_timeout：平台其实建成了任务，但客户端这边"超时"
            if scenario == "create_timeout":
                raise ProviderError(kind="network",
                                    message="create timeout: simulated (task actually created)",
                                    retryable=False)
        return CreateResult(task_id=task.id)

    # ---------- query ----------
    def get_task(self, task_id: str) -> TaskInfo:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return TaskInfo(task_id=task_id, status="unknown", found=False)
            return self._to_info(task)

    def list_tasks(self, task_ids: list[str], page_size: int = 20) -> dict[str, TaskInfo]:
        with self._lock:
            out: dict[str, TaskInfo] = {}
            for tid in task_ids[:page_size]:
                task = self._tasks.get(tid)
                if task:
                    out[tid] = self._to_info(task)
            return out

    # ---------- cancel ----------
    def cancel(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ProviderError(kind="bad_request", message="task not found",
                                    http_status=400, retryable=False)
            status = task.current_status()
            if status == "queued":
                task.status = "cancelled"
                return {"task_id": task_id, "action": "cancelled", "status": "cancelled"}
            # running / 已终态都不可取消
            raise ProviderError(kind="bad_request",
                                message=f"task in status {status} cannot be cancelled",
                                http_status=400, retryable=False)

    # ---------- download ----------
    def download(self, url: str) -> bytes:
        # mock 下载地址形如 mock://<task_id>
        task_id = url.replace("mock://", "")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.current_status() != "succeeded":
                raise ProviderError(kind="network", message="mock media not ready",
                                    retryable=True)
            duration = int(task.request_json.get("duration", 5))
            black_head = task.black_head
        path = _ensure_mp4(task_id, duration, black_head=black_head)
        return path.read_bytes()

    # ---------- helpers ----------
    def _to_info(self, task: MockTask) -> TaskInfo:
        status = task.current_status()
        if status == "succeeded":
            duration = int(task.request_json.get("duration", 5))
            return TaskInfo(
                task_id=task.id, status="succeeded",
                video_url=f"mock://{task.id}",
                resolution=task.request_json.get("resolution", "768P"),
                duration=duration, ratio="adaptive",
                usage={"output_seconds": duration, "total_seconds": duration,
                       "input_image_count": _count_images(task.request_json)},
                raw={"id": task.id, "status": "succeeded"},
            )
        if status == "failed":
            pe = classify_platform_failure(1000, "simulated generation failure")
            return TaskInfo(task_id=task.id, status="failed",
                            error_code=pe.code, error_message=pe.message,
                            resolution=task.request_json.get("resolution"),
                            duration=int(task.request_json.get("duration", 5)),
                            ratio="adaptive",
                            raw={"id": task.id, "status": "failed"})
        # queued / running：平台也知道提交时的参数
        return TaskInfo(task_id=task.id, status=status,
                        resolution=task.request_json.get("resolution", "768P"),
                        duration=int(task.request_json.get("duration", 5)),
                        ratio=task.request_json.get("ratio", "adaptive"),
                        raw={"id": task.id, "status": status})


def _count_images(request_json: dict) -> int:
    return sum(1 for c in request_json.get("content", []) if c.get("type") == "image_url")


def _ensure_mp4(task_id: str, duration: int, black_head: bool = False) -> Path:
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = MOCK_DIR / f"{task_id}.mp4"
    if path.exists() and path.stat().st_size > 0:
        return path
    # 用 testsrc2（画面一直在动，不会被冻帧检测误判）造一个 768x1344 / 24fps / 带声音的 mp4。
    # ultrafast + 高 crf：只是给下载/ffprobe/质检一个合法容器，不追求画质。
    video_src = f"testsrc2=size=768x1344:rate=24:duration={duration}"
    if black_head:
        # 前 2 秒压成全黑，其余照常
        video_src += ",drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,2)'"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", video_src,
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=32000:duration={duration}",
        "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path
