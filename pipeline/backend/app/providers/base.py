"""Provider 抽象与错误分类。

官方 V2 错误体（OpenAI 风格）：
  {"type":"error","error":{"type":"rate_limit_error","message":"... (1002)","http_code":"429"},"request_id":"..."}
本模块把它归一化成 ProviderError，runner 只看 .kind / .code / .retryable。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import constants as C


@dataclass
class ProviderError(Exception):
    kind: str                 # auth | balance | bad_request | sensitive | rate_limit | server | network | unknown
    message: str
    http_status: int | None = None
    code: int | str | None = None
    request_id: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "message": self.message,
            "http_status": self.http_status, "code": self.code,
            "request_id": self.request_id, "retryable": self.retryable,
        }


@dataclass
class CreateResult:
    task_id: str


@dataclass
class TaskInfo:
    task_id: str
    status: str               # queued | running | succeeded | failed | cancelled | unknown
    video_url: str | None = None
    error_code: int | str | None = None
    error_message: str | None = None
    resolution: str | None = None
    duration: int | None = None
    ratio: str | None = None
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None
    found: bool = True


_CODE_RE = re.compile(r"\((\d{3,5})\)\s*$")


def classify_http_error(http_status: int, body: Any, request_id: str | None = None) -> ProviderError:
    """把官网错误响应归一化。body 为解析后的 JSON（拿不到时传 None）。"""
    err_type = message = None
    code = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        err_type = e.get("type") if isinstance(e.get("type"), str) else None
        message = e.get("message") if isinstance(e.get("message"), str) else None
        code = e.get("code") if type(e.get("code")) in (int, str) else None
        m = _CODE_RE.search(message or "")
        if code is None and m:
            code = int(m.group(1))
        request_id = body.get("request_id", request_id)
    if not message:
        message = f"HTTP {http_status}"

    kind = "unknown"
    retryable = False
    if http_status == 401 or err_type == "authorized_error" or code == C.CODE_AUTH:
        kind = "auth"
    elif http_status == 402 or err_type == "insufficient_balance_error" or code == C.CODE_BALANCE:
        kind = "balance"
    elif http_status == 422 or code == C.CODE_SENSITIVE:
        kind = "sensitive"
    elif http_status == 429 or err_type == "rate_limit_error" or code == C.CODE_RATE_LIMIT:
        kind, retryable = "rate_limit", True
    elif http_status in (500, 502, 503, 529) or err_type in ("server_error", "overloaded_error") or code == C.CODE_SERVER:
        kind, retryable = "server", True
    elif 400 <= http_status < 500:
        kind = "bad_request"
    return ProviderError(kind=kind, message=message, http_status=http_status,
                         code=code, request_id=request_id, retryable=retryable)


def classify_platform_failure(error_code: int | str | None, message: str | None) -> ProviderError:
    """查询接口里 task.status=failed 时的 error.code/message。"""
    code = error_code
    retryable = False
    comparable = str(code)
    if comparable == str(C.CODE_AUTH):
        kind = "auth"
    elif comparable == str(C.CODE_BALANCE):
        kind = "balance"
    elif comparable == str(C.CODE_SENSITIVE):
        kind = "sensitive"
    elif comparable == str(C.CODE_RATE_LIMIT):
        kind, retryable = "rate_limit", True
    elif comparable == str(C.CODE_SERVER):
        kind, retryable = "server", True
    else:
        kind = "server"  # 未知/本地失败不授权重新计算；由 runner 决定如何恢复。
    return ProviderError(kind=kind, message=message or f"platform failed ({code})",
                         code=code, retryable=retryable)
