"""M4：官网 M3 看片客户端的请求拼装、tools 结构、base_resp 错误分类。"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.providers.m3_official import M3Client, M3Error


def _tool_message(args: dict) -> dict:
    return {
        "id": "chatcmpl-x",
        "choices": [{"finish_reason": "tool_calls", "index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "submit_take_review",
                                         "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
        "usage": {"total_tokens": 1},
    }


def test_review_take_request_shape_and_parse():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_tool_message({
            "verdict": "pass", "product_consistent": True, "action_matches": True,
            "text_or_watermark": False, "artifacts": False, "issues": [],
            "summary": "可以直接用", "retry_hint": ""}))

    cli = M3Client("https://api.minimax.cn/v1", "key-123", transport=httpx.MockTransport(handler))
    review = cli.review_take(
        video_data_uri="data:video/mp4;base64," + base64.b64encode(b"fake-mp4").decode(),
        ref_image_uris=["data:image/jpeg;base64,AAAA"],
        shot_text="画面：产品特写", fps=2.0, detail="low")
    assert review.verdict == "pass" and review.final_verdict() == "pass"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer key-123"
    body = captured["body"]
    assert body["model"] == "MiniMax-M3" and body["reasoning_split"] is True
    tools = [t["function"]["name"] for t in body["tools"]]
    assert tools == ["submit_take_review"]
    parts = body["messages"][-1]["content"]
    kinds = {p["type"] for p in parts}
    assert kinds == {"text", "image_url", "video_url"}
    video = next(p["video_url"] for p in parts if p["type"] == "video_url")
    assert video["url"].startswith("data:video/mp4;base64,")
    assert video["fps"] == 2.0 and video["detail"] == "low"
    assert any(p.get("image_url", {}).get("url", "").startswith("data:image/jpeg") for p in parts)


def test_pass_with_blocker_becomes_uncertain():
    cli = M3Client("https://x/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json=_tool_message({
            "verdict": "pass", "issues": [{"severity": "blocker", "message": "瓶身融化"}],
            "summary": "模型自相矛盾"}))))
    review = cli.review_take(video_data_uri="data:video/mp4;base64,AAAA",
                             ref_image_uris=[], shot_text="x")
    assert review.final_verdict() == "uncertain"


def test_fail_verdict():
    cli = M3Client("https://x/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json=_tool_message({
            "verdict": "fail", "artifacts": True,
            "issues": [{"severity": "blocker", "code": "shape", "message": "变形"}],
            "summary": "商品变形", "retry_hint": "减少运动"}))))
    review = cli.review_take(video_data_uri="data:video/mp4;base64,AAAA",
                             ref_image_uris=[], shot_text="x")
    assert review.final_verdict() == "fail" and review.retry_hint


@pytest.mark.parametrize("code,http_status", [(1004, 401), (1008, 200), (2013, 200)])
def test_base_resp_errors_are_fatal(code, http_status):
    def handler(request):
        # 401 是真正的 HTTP 状态码（没有 base_resp 正文）；业务码也可能随 200 + base_resp 返回
        if http_status == 200:
            return httpx.Response(200, json={"base_resp": {"status_code": code,
                                                           "status_msg": "err"}})
        return httpx.Response(http_status, json={"base_resp": {"status_code": code,
                                                               "status_msg": "err"}})
    cli = M3Client("https://x/v1", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(M3Error) as ei:
        cli.review_take(video_data_uri="data:video/mp4;base64,AAAA",
                        ref_image_uris=[], shot_text="x")
    assert ei.value.fatal is True
    assert ei.value.code == code if http_status == 200 else ei.value.http_status == 401


def test_http_500_is_not_fatal():
    cli = M3Client(
        "https://x/v1", "k", transport=httpx.MockTransport(
            lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(M3Error) as ei:
        cli.review_take(video_data_uri="data:video/mp4;base64,AAAA",
                        ref_image_uris=[], shot_text="x")
    assert ei.value.fatal is False
