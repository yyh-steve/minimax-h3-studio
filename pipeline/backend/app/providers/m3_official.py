"""MiniMax-M3 客户端（OpenAI 兼容 Chat Completions，httpx 直连）。

关键点（见官网 OpenAI SDK / Chat Completions 文档）：
- 多模态：image_url / video_url 内容块；图片 JPEG/PNG/GIF/WEBP，单张 ≤10MB；
  视频 MP4/AVI/MOV/MKV，URL 或 base64 ≤50MB；video_url 可带 fps（0.2-5，默认 1）和 detail
- 不支持音频输入：看片只能看画面，声音由本地技术检查负责
- tools：用 function calling 承载结构化输出（M3 无 response_format 保证）
- reasoning_split：把思考内容与正文分开
- 业务错误可能以 HTTP 200 + base_resp.status_code≠0 返回，需要单独判断
- 校验失败时把错误回传，让模型修复（最多 N 次）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from ..storyboard_schema import StoryboardResult, ReviewReport, Shot, TakeReview


class M3Error(Exception):
    def __init__(self, message: str, http_status: int | None = None,
                 body: Any = None, code: int | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body
        self.code = code

    @property
    def fatal(self) -> bool:
        """鉴权/余额/参数问题：重试也不会好，直接交给人。"""
        return (self.http_status in (400, 401, 402, 403, 422)
                or self.code in (1004, 1008, 2013))


@dataclass
class M3Result:
    data: Any
    reasoning: str
    raw: dict


TOOL_STORYBOARD = {
    "type": "function",
    "function": {
        "name": "submit_storyboard",
        "description": "提交商品理解卡和分镜脚本。必须严格覆盖用户卖点，时长总和等于目标时长。",
        "parameters": {
            "type": "object",
            "properties": {
                "product_card": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "appearance": {"type": "string",
                                       "description": "只描述图片里确实可见的外观（颜色/材质/形状）"},
                        "visible_features": {"type": "array", "items": {"type": "string"}},
                        "selling_points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "source": {"type": "string",
                                               "description": "用户提供 或 图N可见"},
                                },
                                "required": ["id", "text"],
                            },
                        },
                        "image_notes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "image_id": {"type": "string"},
                                    "view": {"type": "string"},
                                    "best_for": {"type": "string"},
                                    "issues": {"type": "string"},
                                },
                                "required": ["image_id"],
                            },
                        },
                        "risky_claims": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "storyboard": {
                    "type": "object",
                    "properties": {
                        "ratio": {"type": "string"},
                        "total_duration": {"type": "integer"},
                        "shots": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "shot_key": {"type": "string"},
                                    "purpose": {"type": "string",
                                                "enum": ["hook", "pain", "feature",
                                                         "scene", "trust", "cta"]},
                                    "duration": {"type": "integer",
                                                 "minimum": 4, "maximum": 15},
                                    "mode": {"type": "string",
                                             "enum": ["first_frame", "first_last",
                                                      "reference", "t2v"]},
                                    "images": {
                                        "type": "object",
                                        "properties": {
                                            "first_frame": {"type": ["string", "null"]},
                                            "last_frame": {"type": ["string", "null"]},
                                            "references": {"type": "array",
                                                           "items": {"type": "string"}},
                                        },
                                    },
                                    "visual": {"type": "string"},
                                    "action": {"type": "string"},
                                    "camera": {"type": "string"},
                                    "audio": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string",
                                                     "enum": ["none", "on_screen", "voiceover"]},
                                            "text": {"type": "string"},
                                        },
                                    },
                                    "subtitle": {"type": "string"},
                                    "selling_point_ids": {"type": "array",
                                                          "items": {"type": "string"}},
                                    "prompt_override": {"type": "string"},
                                    "title": {"type": "string"},
                                    "required": ["duration", "mode", "visual", "action"],
                                },
                            },
                        },
                    },
                    "required": ["ratio", "shots"],
                },
            },
            "required": ["product_card", "storyboard"],
        },
    },
}

TOOL_REVIEW = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "对分镜脚本做独立评审，返回阻断问题和建议。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string", "enum": ["blocker", "suggestion"]},
                            "shot_key": {"type": ["string", "null"]},
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                            "fix": {"type": "string",
                                    "description": "可直接采用的修改建议"},
                        },
                        "required": ["severity", "message"],
                    },
                },
            },
            "required": ["issues"],
        },
    },
}

SYSTEM_STORYBOARD = (
    "你是电商短视频分镜导演。你会拿到商品图片和用户卖点，"
    "先产出商品理解卡（只描述图片里确实可见的外观，卖点只能来自用户输入或图片可见信息，"
    "不得编造功能、参数或极限词），再产出可直接用于视频生成的分镜。"
    "每个镜头只有一个主要动作；时长为 4-15 的整数，总和必须等于目标时长；"
    "每条卖点至少被一个镜头覆盖。结果必须通过 submit_storyboard 工具提交。"
)

SYSTEM_REVIEW = (
    "你是严格的分镜评审，只评审不改写。检查：前 3 秒钩子、是否有理解卡之外的功能宣称、"
    "单镜动作是否过多、跨镜头产品外观是否一致、口播字数是否超时。"
    "结果必须通过 submit_review 工具提交。"
)

SYSTEM_SHOT_REWRITE = (
    "你只重写被指定的那一个镜头，其它镜头不变。严格保留该镜头的时长、生成方式、"
    "所用图片和卖点归属；只能修改画面、动作、运镜、声音和字幕描述，"
    "且不得新增理解卡之外的功能。结果必须通过 submit_shot 工具提交。"
)

TOOL_SHOT = {
    "type": "function",
    "function": {
        "name": "submit_shot",
        "description": "提交重写后的单个镜头",
        "parameters": TOOL_STORYBOARD["function"]["parameters"]["properties"]
        ["storyboard"]["properties"]["shots"]["items"],
    },
}

TOOL_TAKE_REVIEW = {
    "type": "function",
    "function": {
        "name": "submit_take_review",
        "description": "提交对一段 AI 生成视频镜头的看片质检结论。",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"],
                            "description": "pass=可用；fail=有阻断问题需要重新生成；uncertain=看不清或拿不准"},
                "product_consistent": {"type": ["boolean", "null"],
                                       "description": "商品颜色/形状/结构/标识是否与参考图一致"},
                "action_matches": {"type": ["boolean", "null"],
                                   "description": "画面和动作是否符合分镜要求"},
                "text_or_watermark": {"type": ["boolean", "null"],
                                      "description": "画面中是否出现文字、字幕、水印或乱码"},
                "artifacts": {"type": ["boolean", "null"],
                              "description": "是否有明显畸形、闪烁、融化、穿帮等生成瑕疵"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string", "enum": ["blocker", "suggestion"]},
                            "code": {"type": "string"},
                            "message": {"type": "string",
                                        "description": "具体问题，尽量带上出现的大致时间"},
                        },
                        "required": ["severity", "message"],
                    },
                },
                "summary": {"type": "string", "description": "一句话结论"},
                "retry_hint": {"type": "string",
                               "description": "如果需要重新生成，给出可操作的改进建议"},
            },
            "required": ["verdict", "issues", "summary"],
        },
    },
}

SYSTEM_TAKE_REVIEW = (
    "你是电商短视频的看片质检员。你会拿到商品参考图、分镜要求和一段 AI 生成的视频镜头"
    "（只有画面，没有声音）。逐项检查：1) 商品外观（颜色、形状、结构、标识）是否与参考图一致，"
    "有没有变形；2) 画面和动作是否符合分镜要求；3) 画面里是否出现文字、字幕、水印或乱码；"
    "4) 是否有明显的肢体或手指畸形、物体闪烁或融化、穿帮等生成瑕疵；5) 画面是否清晰可用。"
    "任何一项会让这个镜头不能直接用于成片，就记为 blocker 并给 fail；只是可以更好，记为 suggestion。"
    "只根据看到的内容判断，不要猜测；看不清或拿不准时给 uncertain。"
    "结果必须通过 submit_take_review 工具提交。"
)


class M3Client:
    def __init__(self, base_url: str, api_key: str, model: str = "MiniMax-M3",
                 timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport          # 测试注入 httpx.MockTransport

    def _chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "reasoning_split": True,
        }
        if tools:
            body["tools"] = tools
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as cli:
                resp = cli.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.HTTPError as e:
            raise M3Error(f"M3 网络错误：{e}") from e
        if resp.status_code // 100 != 2:
            raise M3Error(f"M3 HTTP {resp.status_code}: {resp.text[:300]}",
                          http_status=resp.status_code, body=_safe(resp))
        data = resp.json()
        base = data.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0):
            raise M3Error(f"M3 错误 {code}: {base.get('status_msg', '')}",
                          http_status=resp.status_code, body=data, code=int(code))
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise M3Error(f"M3 返回缺少 choices.message: {str(data)[:300]}") from e

    # ---------- 分镜 ----------

    def generate_storyboard(self, *, user_text: str, image_data_uris: list[str],
                            fix_message: str | None = None,
                            previous: dict | None = None) -> StoryboardResult:
        """生成或修复分镜。fix_message + previous 用于让模型按校验错误修正。"""
        content: list[dict] = [{"type": "text", "text": user_text}]
        for uri in image_data_uris:
            content.append({"type": "image_url",
                            "image_url": {"url": uri, "detail": "default"}})
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_STORYBOARD},
            {"role": "user", "content": content},
        ]
        if fix_message and previous is not None:
            # 把上一版作为 assistant 工具调用放回历史，再要求修复
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [{"id": "call_prev", "type": "function",
                                             "function": {"name": "submit_storyboard",
                                                          "arguments": json.dumps(previous,
                                                                                  ensure_ascii=False)}}]})
            messages.append({"role": "tool", "tool_call_id": "call_prev",
                             "content": "已收到上一版，请按下面的问题修正后重新调用 submit_storyboard。"})
            messages.append({"role": "user", "content": fix_message})

        result = self._chat_with_structured_tool(
            messages, TOOL_STORYBOARD, "submit_storyboard", StoryboardResult,
            max_fix_rounds=2)
        return result.data

    def review_storyboard(self, *, product_card: dict, storyboard: dict) -> ReviewReport:
        text = ("请评审下面的商品分镜。\n商品理解卡：\n"
                f"{json.dumps(product_card, ensure_ascii=False)}\n"
                f"分镜：\n{json.dumps(storyboard, ensure_ascii=False)}")
        messages = [{"role": "system", "content": SYSTEM_REVIEW},
                    {"role": "user", "content": text}]
        result = self._chat_with_structured_tool(
            messages, TOOL_REVIEW, "submit_review", ReviewReport, max_fix_rounds=1)
        return result.data

    def rewrite_shot(self, *, shot, instruction: str, product_card) -> Shot:
        from ..storyboard_schema import Shot
        text = ("商品理解卡：\n"
                f"{json.dumps(product_card.model_dump(), ensure_ascii=False)}\n"
                "要重写的镜头：\n"
                f"{json.dumps(shot.model_dump(), ensure_ascii=False)}\n"
                f"修改意见：{instruction}")
        messages = [{"role": "system", "content": SYSTEM_SHOT_REWRITE},
                    {"role": "user", "content": text}]
        result = self._chat_with_structured_tool(
            messages, TOOL_SHOT, "submit_shot", Shot, max_fix_rounds=1)
        return result.data

    # ---------- M4：看片质检 ----------

    def review_take(self, *, video_data_uri: str, ref_image_uris: list[str],
                    shot_text: str, fps: float = 2.0, detail: str = "low",
                    context_key: str | None = None) -> TakeReview:
        """把代理视频（base64 data URL）和参考图交给 M3 看片，按 submit_take_review 返回结论。

        video_url 字段按官网：{"url": ..., "fps": 0.2-5, "detail": low|default|high}。
        context_key 只给假 M3 用，这里忽略。
        """
        n = len(ref_image_uris)
        intro = (f"前 {n} 张图是商品参考图，最后是待检查的视频镜头。\n" if n
                 else "没有商品参考图，只检查画面质量和是否符合分镜。\n")
        content: list[dict] = [{"type": "text", "text": intro + "分镜要求：\n" + shot_text}]
        for uri in ref_image_uris:
            content.append({"type": "image_url", "image_url": {"url": uri, "detail": "low"}})
        content.append({"type": "video_url",
                        "video_url": {"url": video_data_uri, "fps": fps, "detail": detail}})
        messages = [{"role": "system", "content": SYSTEM_TAKE_REVIEW},
                    {"role": "user", "content": content}]
        result = self._chat_with_structured_tool(
            messages, TOOL_TAKE_REVIEW, "submit_take_review", TakeReview, max_fix_rounds=1)
        return result.data

    # ---------- 通用：工具输出解析 + 校验 + 修复 ----------

    def _chat_with_structured_tool(self, messages: list[dict], tool: dict,
                                   tool_name: str, model_cls, *,
                                   max_fix_rounds: int) -> M3Result:
        last_raw: dict = {}
        last_text = ""
        for round_idx in range(max_fix_rounds + 1):
            message = self._chat(messages, tools=[tool])
            last_raw = message
            reasoning = _extract_reasoning(message)
            tool_calls = message.get("tool_calls") or []
            call = next((c for c in tool_calls
                         if c.get("function", {}).get("name") == tool_name), None)

            if call is None:
                # 模型直接回了文字：要求它用工具提交
                last_text = message.get("content") or ""
                messages.append({"role": "assistant", "content": last_text})
                messages.append({"role": "user",
                                 "content": f"请调用 {tool_name} 工具提交结果，不要直接用文字回答。"})
                continue

            raw_args = call["function"].get("arguments", "")
            try:
                parsed = json.loads(raw_args)
                obj = model_cls.model_validate(parsed)
                return M3Result(data=obj, reasoning=reasoning, raw=last_raw)
            except (json.JSONDecodeError, ValueError) as e:
                if round_idx >= max_fix_rounds:
                    raise M3Error(f"{tool_name} 输出无法解析/校验，且修复次数已用完：{e}\n{raw_args[:500]}")
                # 回传错误，让模型修复
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [{"id": call.get("id", "call_x"),
                                                 "type": "function",
                                                 "function": {"name": tool_name,
                                                              "arguments": raw_args}}]})
                messages.append({"role": "tool", "tool_call_id": call.get("id", "call_x"),
                                 "content": f"结构校验失败：{e}"})
                messages.append({"role": "user",
                                 "content": f"请修正这些问题后重新调用 {tool_name}。"})
        raise M3Error(f"{tool_name} 未能得到结构化输出")


def _extract_reasoning(message: dict) -> str:
    details = message.get("reasoning_details") or []
    parts = [d.get("text", "") for d in details if isinstance(d, dict)]
    if parts:
        return "\n".join(p for p in parts if p)
    return message.get("reasoning_content") or ""


def _safe(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:
        return resp.text[:500]
