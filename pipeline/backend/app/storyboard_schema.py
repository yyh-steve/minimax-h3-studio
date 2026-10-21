"""分镜领域模型与校验（Pydantic）。

对齐方案 6.1：M3 通过 submit_storyboard 工具产出 product_card + storyboard；
本模块负责结构校验、规则引擎所需的类型，以及"镜头 -> H3 请求"的拼装与内容指纹。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---- 取值范围（与官网 V2 一致）----
PURPOSES = {"hook", "pain", "feature", "scene", "trust", "cta"}
MODES = {"first_frame", "first_last", "reference", "t2v"}
RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
RESOLUTIONS = {"480P", "768P", "2K"}
H3_MIN_DURATION, H3_MAX_DURATION = 4, 15
PROMPT_MAX_CHARS = 7000
MAX_REF_IMAGES = 9

# 常见广告极限词（只提示，不替代法务判断）
DEFAULT_BANNED = ["最好", "最佳", "最优", "最先进", "最低价", "第一", "唯一",
                  "顶级", "国家级", "绝无仅有", "100%", "百分百", "永久"]

VoiceType = Literal["none", "on_screen", "voiceover"]


class SellingPoint(BaseModel):
    id: str
    text: str
    source: str = "用户提供"          # 用户提供 | 图 N 可见 | 推断


class ImageNote(BaseModel):
    image_id: str
    view: str = ""
    best_for: str = ""
    issues: str = ""


class ProductCard(BaseModel):
    name: str = ""
    category: str = ""
    appearance: str = ""
    visible_features: list[str] = Field(default_factory=list)
    selling_points: list[SellingPoint] = Field(default_factory=list)
    image_notes: list[ImageNote] = Field(default_factory=list)
    risky_claims: list[str] = Field(default_factory=list)


class Voice(BaseModel):
    type: VoiceType = "none"
    text: str = ""


class ShotImages(BaseModel):
    first_frame: str | None = None      # project asset_id
    last_frame: str | None = None
    references: list[str] = Field(default_factory=list)


class Shot(BaseModel):
    shot_key: str = ""
    purpose: str = "feature"
    duration: int = 5
    mode: str = "first_frame"
    images: ShotImages = Field(default_factory=ShotImages)
    visual: str = ""
    action: str = ""
    camera: str = ""
    audio: Voice = Field(default_factory=Voice)
    subtitle: str = ""
    selling_point_ids: list[str] = Field(default_factory=list)
    prompt_override: str = ""
    title: str = ""

    @field_validator("duration")
    @classmethod
    def _duration_ok(cls, v: int) -> int:
        if not (H3_MIN_DURATION <= v <= H3_MAX_DURATION):
            raise ValueError(f"duration 必须在 {H3_MIN_DURATION}-{H3_MAX_DURATION} 秒之间")
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose_ok(cls, v: str) -> str:
        if v not in PURPOSES:
            raise ValueError(f"未知 purpose: {v}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError(f"未知 mode: {v}")
        return v


class StoryboardBody(BaseModel):
    ratio: str = "9:16"
    total_duration: int = 0
    shots: list[Shot]

    @field_validator("ratio")
    @classmethod
    def _ratio_ok(cls, v: str) -> str:
        if v not in RATIOS or v == "adaptive":
            raise ValueError("项目画幅必须是具体比例，不能是 adaptive")
        return v


class StoryboardResult(BaseModel):
    """M3 submit_storyboard 工具的参数结构。"""
    product_card: ProductCard
    storyboard: StoryboardBody


class ReviewIssue(BaseModel):
    severity: Literal["blocker", "suggestion"] = "suggestion"
    shot_key: str | None = None
    code: str = ""
    message: str = ""
    fix: str = ""


class ReviewReport(BaseModel):
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""


# ---------- M4：M3 看片质检 ----------

class TakeReviewIssue(BaseModel):
    severity: Literal["blocker", "suggestion"] = "suggestion"
    code: str = ""
    message: str = ""


class TakeReview(BaseModel):
    """M3 submit_take_review 工具的参数结构：对一条生成结果的看片结论。"""
    verdict: Literal["pass", "fail", "uncertain"]
    product_consistent: bool | None = None     # 商品外观与参考图一致
    action_matches: bool | None = None         # 动作/画面与分镜一致
    text_or_watermark: bool | None = None      # 画面里出现文字/字幕/水印/乱码
    artifacts: bool | None = None              # 明显畸形/闪烁/穿帮等生成瑕疵
    issues: list[TakeReviewIssue] = Field(default_factory=list)
    summary: str = ""
    retry_hint: str = ""                        # 给人看的改进建议（不会自动改提示词）

    def final_verdict(self) -> str:
        """模型说通过但又列了阻断问题时，交给人判断。"""
        has_blocker = any(i.severity == "blocker" for i in self.issues)
        if self.verdict == "fail":
            return "fail"
        if self.verdict == "pass" and has_blocker:
            return "uncertain"
        return self.verdict


# ---------- 规则引擎 ----------

@dataclass
class RuleIssue:
    severity: str          # blocker | warning
    code: str
    shot_key: str | None
    message: str


def voice_chars_per_second() -> float:
    return 4.0


def validate_rules(storyboard: StoryboardResult, *, target_seconds: int,
                   tolerance_seconds: int = 1,
                   user_banned: list[str] | None = None,
                   available_asset_ids: set[str] | None = None) -> list[RuleIssue]:
    """确定性规则校验。blocker 必须清零才能确认。"""
    issues: list[RuleIssue] = []
    body = storyboard.storyboard
    shots = body.shots
    banned = sorted(set(DEFAULT_BANNED) | set(user_banned or []))

    if not shots:
        issues.append(RuleIssue("blocker", "empty_storyboard", None, "分镜为空"))
        return issues

    total = 0
    point_ids = {sp.id for sp in storyboard.product_card.selling_points}
    covered: set[str] = set()

    for i, shot in enumerate(shots, start=1):
        key = shot.shot_key or f"s{i}"
        total += shot.duration

        # 时长已由 pydantic 校验，这里再兜一层
        if not (H3_MIN_DURATION <= shot.duration <= H3_MAX_DURATION):
            issues.append(RuleIssue("blocker", "duration_range", key,
                                    f"时长 {shot.duration}s 超出 {H3_MIN_DURATION}-{H3_MAX_DURATION}"))

        # 模式与图片
        imgs = shot.images
        refs = imgs.references
        if shot.mode == "first_frame":
            if not imgs.first_frame:
                issues.append(RuleIssue("blocker", "missing_first_frame", key, "首帧方式缺少首帧图"))
            if imgs.last_frame:
                issues.append(RuleIssue("blocker", "mode_image_mismatch", key,
                                        "first_frame 模式不能带尾帧，应改为 first_last"))
        elif shot.mode == "first_last":
            if not imgs.first_frame or not imgs.last_frame:
                issues.append(RuleIssue("blocker", "missing_keyframe", key,
                                        "首尾帧模式需要同时提供首帧和尾帧"))
        elif shot.mode == "reference":
            if not refs:
                issues.append(RuleIssue("blocker", "missing_reference", key,
                                        "参考图方式至少需要 1 张参考图"))
            if len(refs) > MAX_REF_IMAGES:
                issues.append(RuleIssue("blocker", "too_many_refs", key,
                                        f"参考图最多 {MAX_REF_IMAGES} 张，当前 {len(refs)}"))
            if imgs.first_frame or imgs.last_frame:
                issues.append(RuleIssue("blocker", "mode_mix", key,
                                        "参考图方式不能与首尾帧混用"))
        elif shot.mode == "t2v":
            if imgs.first_frame or imgs.last_frame or refs:
                issues.append(RuleIssue("warning", "t2v_with_images", key,
                                        "文生模式带了图片，图片会被忽略"))

        if available_asset_ids is not None:
            for aid in [imgs.first_frame, imgs.last_frame, *refs]:
                if aid and aid not in available_asset_ids:
                    issues.append(RuleIssue("blocker", "asset_missing", key,
                                            f"引用的素材不存在：{aid}"))

        # 口播字数
        if shot.audio.type in ("on_screen", "voiceover") and shot.audio.text:
            limit = int(shot.duration * voice_chars_per_second())
            if len(shot.audio.text) > limit:
                issues.append(RuleIssue("blocker", "voice_too_long", key,
                                        f"口播 {len(shot.audio.text)} 字，超过 {shot.duration}s 上限 {limit} 字"))

        # 卖点覆盖
        for pid in shot.selling_point_ids:
            if pid not in point_ids:
                issues.append(RuleIssue("blocker", "unknown_point", key,
                                        f"引用了不存在的卖点：{pid}"))
            covered.add(pid)

        # 提示词长度（override 或拼装后估算，这里只校验 override）
        if shot.prompt_override and len(shot.prompt_override) > PROMPT_MAX_CHARS:
            issues.append(RuleIssue("blocker", "prompt_too_long", key,
                                    f"自定义提示词 {len(shot.prompt_override)} 字，超过 {PROMPT_MAX_CHARS}"))

        # 违禁词（提示级）
        haystack = " ".join([shot.visual, shot.action, shot.subtitle,
                             shot.audio.text, shot.prompt_override])
        for word in banned:
            if word and word in haystack:
                issues.append(RuleIssue("warning", "banned_word", key,
                                        f"文案包含需确认的词：{word}"))

    missing_points = point_ids - covered
    for pid in missing_points:
        issues.append(RuleIssue("blocker", "point_uncovered", None,
                                f"卖点未被任何镜头覆盖：{pid}"))

    if abs(total - target_seconds) > tolerance_seconds:
        issues.append(RuleIssue("blocker", "total_duration_mismatch", None,
                                f"镜头总时长 {total}s，目标 {target_seconds}s，"
                                f"允许偏差 ±{tolerance_seconds}s"))

    return issues


def blockers(issues: list[RuleIssue]) -> list[RuleIssue]:
    return [i for i in issues if i.severity == "blocker"]


# ---------- 镜头 -> H3 请求 ----------

def build_prompt(shot: Shot, product_card: ProductCard, *, ratio: str,
                 style: str = "") -> str:
    """自定义提示词优先；否则按方案 6.2 的模板拼装。"""
    if shot.prompt_override.strip():
        return shot.prompt_override.strip()

    voice_line = "无人声"
    if shot.audio.type == "on_screen" and shot.audio.text:
        voice_line = f"人物说：「{shot.audio.text}」"
    elif shot.audio.type == "voiceover":
        voice_line = "画外音在后期合成，画面人物不说话"

    parts = [
        f"[风格] {ratio} 商品短视频，{style or '画面干净、真实、电影感'}",
        f"[画面] {shot.visual}".rstrip(),
        f"[动作] {shot.action}".rstrip(),
        f"[运镜] {shot.camera or '固定机位'}",
        f"[产品] {product_card.appearance}，与参考图保持一致，不改变颜色和结构",
        f"[声音] 无背景音乐，只保留环境音和动作音效；{voice_line}",
        "[约束] 画面中不出现任何文字、字幕、水印",
    ]
    return "\n".join(p for p in parts if not p.endswith("[画面] ") and not p.endswith("[动作] "))


def build_h3_request(shot: Shot, product_card: ProductCard, *,
                     ratio: str, style: str, model: str, resolution: str,
                     asset_data_uri: dict[str, str]) -> dict[str, Any]:
    """把镜头转成官网 V2 content 结构。asset_data_uri: asset_id -> data URI。"""
    content: list[dict[str, Any]] = [{"type": "text",
                                      "text": build_prompt(shot, product_card,
                                                           ratio=ratio, style=style)}]
    if shot.mode == "first_frame" and shot.images.first_frame:
        content.append({"type": "image_url",
                        "image_url": {"url": asset_data_uri[shot.images.first_frame]},
                        "role": "first_frame"})
    elif shot.mode == "first_last":
        content.append({"type": "image_url",
                        "image_url": {"url": asset_data_uri[shot.images.first_frame]},
                        "role": "first_frame"})
        content.append({"type": "image_url",
                        "image_url": {"url": asset_data_uri[shot.images.last_frame]},
                        "role": "last_frame"})
    elif shot.mode == "reference":
        for aid in shot.images.references:
            content.append({"type": "image_url",
                            "image_url": {"url": asset_data_uri[aid]},
                            "role": "reference_image"})

    request: dict[str, Any] = {"model": model, "content": content,
                               "duration": shot.duration, "resolution": resolution}
    if shot.mode == "t2v":
        request["ratio"] = ratio                      # 文生必须具体比例
    elif shot.mode == "reference":
        request["ratio"] = ratio                      # 参考图可显式指定
    else:
        request["ratio"] = "adaptive"                 # 首尾帧由图决定
    return request


def content_fingerprint(*, provider: str, model: str, resolution: str, duration: int,
                        ratio: str, mode: str, prompt: str, image_hashes: list[str],
                        backend_origin: str = "") -> str:
    payload = json.dumps({
        "provider": provider, "model": model, "resolution": resolution,
        "backend_origin": backend_origin,
        "duration": duration, "ratio": ratio, "mode": mode,
        "prompt": prompt, "images": sorted(image_hashes),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def shot_fingerprint(shot: Shot, product_card: ProductCard, *, provider: str,
                     model: str, resolution: str, ratio: str, style: str,
                     asset_hashes: dict[str, str], backend_origin: str = "") -> str:
    prompt = build_prompt(shot, product_card, ratio=ratio, style=style)
    ids = [shot.images.first_frame, shot.images.last_frame, *shot.images.references]
    hashes = [asset_hashes[i] for i in ids if i and i in asset_hashes]
    return content_fingerprint(provider=provider, model=model, resolution=resolution,
                               duration=shot.duration, ratio=ratio, mode=shot.mode,
                               prompt=prompt, image_hashes=hashes, backend_origin=backend_origin)


def storyboard_to_json(sb: StoryboardResult) -> dict:
    return sb.model_dump()


def storyboard_from_json(data: dict) -> StoryboardResult:
    return StoryboardResult.model_validate(data)
