"""官网 MiniMax-H3 视频生成 V2 客户端。

严格按官网契约：
- 创建：POST {base}/v2/video_generation            -> {"task_id": ...}
- 单查：GET  {base}/v2/query/video_generation/{id}
- 列表：GET  {base}/v2/query/video_generation?filter.task_ids=..&page_size=..
- 取消：DELETE {base}/v2/video_generation/{id}
本地图片直接用 data:image/..;base64 放进 content（官网 V2 支持 data URI）。
轮询建议 10 秒；任务记录只保留 7 天。
"""
from __future__ import annotations

import httpx

from .base import CreateResult, ProviderError, TaskInfo, classify_http_error

# 官网 V2 的模型取值
MODEL_H3 = "MiniMax-H3"
MODEL_H3_MAX = "MiniMax-H3-Max"

_TERMINAL = {"succeeded", "failed", "cancelled"}


class OfficialH3:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=self._timeout,
            transport=self.transport,
        )

    # ---------- create ----------
    def create(self, request_json: dict) -> CreateResult:
        """官网创建没有幂等承诺；不确定响应须由 runner 对账。"""
        return self._create(request_json)

    def _create(self, request_json: dict, *, headers: dict | None = None) -> CreateResult:
        try:
            with self._client() as cli:
                resp = cli.post("/v2/video_generation", json=request_json, headers=headers)
        except httpx.TimeoutException as e:
            raise ProviderError(kind="network", message=f"create timeout: {e}",
                                retryable=False) from e
        except httpx.HTTPError as e:
            raise ProviderError(kind="network", message=f"create network error: {e}",
                                retryable=False) from e

        if resp.status_code // 100 != 2:
            body = _safe_json(resp)
            error = classify_http_error(resp.status_code, body, _request_id(body))
            error.retryable = resp.status_code == 429
            raise error
        data = _safe_json(resp)
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderError(kind="server",
                                message=f"创建响应没有有效 task_id: {data}",
                                http_status=resp.status_code, retryable=False)
        return CreateResult(task_id=task_id)

    # ---------- query one ----------
    def get_task(self, task_id: str) -> TaskInfo:
        try:
            with self._client() as cli:
                resp = cli.get(f"/v2/query/video_generation/{task_id}")
        except httpx.TimeoutException as e:
            raise ProviderError(kind="network", message=f"query timeout: {e}",
                                retryable=True) from e
        except httpx.HTTPError as e:
            raise ProviderError(kind="network", message=f"query network error: {e}",
                                retryable=True) from e

        if resp.status_code == 404:
            return TaskInfo(task_id=task_id, status="unknown", found=False)
        if resp.status_code // 100 != 2:
            body = _safe_json(resp)
            raise classify_http_error(resp.status_code, body, _request_id(body))
        data = _safe_json(resp) or {}
        task = data.get("task")
        if not task:
            return TaskInfo(task_id=task_id, status="unknown", found=False)
        return _parse_task(task)

    # ---------- list（批量对账 / 批量轮询）----------
    def list_tasks(self, task_ids: list[str], page_size: int = 20) -> dict[str, TaskInfo]:
        params: list[tuple[str, str]] = [("page_size", str(min(max(page_size, 1), 100))),
                                         ("page_num", "1")]
        # 传空列表时不带 filter，拉最近任务（用于创建超时后的对账）
        params.extend(("filter.task_ids", tid) for tid in task_ids)
        try:
            with self._client() as cli:
                resp = cli.get("/v2/query/video_generation", params=params)
        except httpx.HTTPError as e:
            raise ProviderError(kind="network", message=f"list network error: {e}",
                                retryable=True) from e
        if resp.status_code // 100 != 2:
            body = _safe_json(resp)
            raise classify_http_error(resp.status_code, body, _request_id(body))
        data = _safe_json(resp) or {}
        result: dict[str, TaskInfo] = {}
        for item in data.get("items", []) or []:
            info = _parse_task(item)
            result[info.task_id] = info
        return result

    # ---------- cancel（仅 queued）----------
    def cancel(self, task_id: str) -> dict:
        try:
            with self._client() as cli:
                resp = cli.request("DELETE", f"/v2/video_generation/{task_id}")
        except httpx.HTTPError as e:
            raise ProviderError(kind="network", message=f"cancel network error: {e}",
                                retryable=True) from e
        if resp.status_code // 100 != 2:
            body = _safe_json(resp)
            raise classify_http_error(resp.status_code, body, _request_id(body))
        return _safe_json(resp) or {}

    # ---------- download ----------
    def download(self, url: str) -> bytes:
        """下载成片。官网 content.url 是限时 CDN，失败应重新查询换链接。"""
        try:
            with httpx.Client(timeout=120.0, follow_redirects=True, transport=self.transport) as cli:
                resp = cli.get(url)
        except httpx.HTTPError as e:
            raise ProviderError(kind="network", message=f"download network error: {e}",
                                retryable=True) from e
        if resp.status_code // 100 != 2:
            raise ProviderError(kind="network",
                                message=f"download HTTP {resp.status_code}",
                                http_status=resp.status_code, retryable=True)
        return resp.content


def _parse_task(task: dict) -> TaskInfo:
    status = task.get("status", "unknown")
    content = task.get("content") or {}
    error = task.get("error") or {}
    return TaskInfo(
        task_id=str(task.get("id")),
        status=status if status in _TERMINAL or status in ("queued", "running") else "unknown",
        video_url=content.get("url") if status == "succeeded" else None,
        error_code=error.get("code") if type(error.get("code")) in (str, int) else None,
        error_message=error.get("message"),
        resolution=task.get("resolution"),
        duration=task.get("duration"),
        ratio=task.get("ratio"),
        usage=task.get("usage"),
        raw=task,
    )


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:
        return None


def _request_id(body) -> str | None:
    return body.get("request_id") if isinstance(body, dict) else None
