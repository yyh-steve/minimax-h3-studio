"""本地假 M3：不花钱，用于打通分镜生成/评审/修复。

行为：
- generate_storyboard：按图片数量和目标时长产出一份覆盖全部卖点的分镜。
  若 self.bad_json_once=True，第一次返回坏 tool 参数，第二次才给合法 JSON，
  用来验证"校验失败回传修复"。
- review_storyboard：给出确定性的问题列表（前 3 秒钩子、口播超长等）。
- 支持脚本开关，让特定镜头被标问题，便于前端/规则联调。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from ..storyboard_schema import (StoryboardResult, ReviewReport, ReviewIssue,
                                 TakeReview, TakeReviewIssue)


@dataclass
class MockM3:
    """假 M3，永远返回结构合法的分镜/评审，用于 service 与 API 级测试。

    M3Client 自身"坏 JSON -> 回传修复"的逻辑用 httpx MockTransport 单测，
    不走这个假实现。
    """
    # 下一次生成时，让某镜头口播超长（触发规则 blocker）
    force_voice_overflow_shot: str | None = None
    reference_only: bool = False
    _calls: int = 0
    # 看片结论：pass / fail_once（每个镜头第一次不通过）/ fail / uncertain / error_once
    review_mode: str = "pass"
    review_calls: int = 0
    _review_seen: set = field(default_factory=set)
    last_review_request: dict | None = None

    def reset(self) -> None:
        self.force_voice_overflow_shot = None
        self.reference_only = False
        self._calls = 0
        self.review_mode = "pass"
        self.review_calls = 0
        self._review_seen = set()
        self.last_review_request = None

    def set_review_mode(self, mode: str) -> None:
        # 只在真正切换时清空"已经失败过一次"的记录（runner 每次取实例都会调用）
        if mode != self.review_mode:
            self.review_mode = mode
            self._review_seen = set()

    # ---------- 看片（M4）----------
    def review_take(self, *, video_data_uri: str, ref_image_uris: list[str],
                    shot_text: str, fps: float = 2.0, detail: str = "low",
                    context_key: str | None = None) -> TakeReview:
        # 用官网约束校验入参，提前暴露拼装错误
        from .m3_official import M3Error
        if not video_data_uri.startswith("data:video/mp4;base64,"):
            raise M3Error("假 M3：video_url 需要 data:video/mp4;base64 格式", http_status=400)
        if not (0.2 <= float(fps) <= 5):
            raise M3Error("假 M3：fps 超出 0.2-5", http_status=400)
        if detail not in ("low", "default", "high"):
            raise M3Error("假 M3：detail 只能是 low/default/high", http_status=400)
        for uri in ref_image_uris:
            if not uri.startswith(("data:image/jpeg", "data:image/png",
                                   "data:image/webp", "data:image/gif")):
                raise M3Error("假 M3：参考图格式不被 M3 支持", http_status=400)
        self.review_calls += 1
        self.last_review_request = {"fps": fps, "detail": detail, "refs": len(ref_image_uris),
                                    "video_bytes": len(video_data_uri), "context_key": context_key,
                                    "shot_text": shot_text}
        key = context_key or "default"
        mode = self.review_mode
        if mode == "error_once" and key not in self._review_seen:
            self._review_seen.add(key)
            raise M3Error("假 M3：模拟服务端错误", http_status=500)
        if mode == "fail" or (mode == "fail_once" and key not in self._review_seen):
            self._review_seen.add(key)
            return TakeReview(
                verdict="fail", product_consistent=False, action_matches=True,
                text_or_watermark=False, artifacts=True,
                issues=[TakeReviewIssue(severity="blocker", code="product_shape",
                                        message="约 2s 处商品瓶身变形，与参考图不一致")],
                summary="商品外观与参考图不一致，需要重新生成",
                retry_hint="减少镜头运动幅度，保持商品在画面中央")
        if mode == "uncertain":
            return TakeReview(verdict="uncertain", issues=[], summary="画面偏暗，无法确认商品细节")
        return TakeReview(verdict="pass", product_consistent=True, action_matches=True,
                          text_or_watermark=False, artifacts=False, issues=[],
                          summary="商品外观一致，动作符合分镜，未见文字和明显瑕疵")

    # ---------- 分镜 ----------
    def generate_storyboard(self, *, user_text: str, image_data_uris: list[str],
                            fix_message: str | None = None,
                            previous: dict | None = None) -> StoryboardResult:
        self._calls += 1

        # 修复轮：返回上一版（模拟模型按意见修正；规则修复由 service 再校验）
        if previous is not None:
            return StoryboardResult.model_validate(copy.deepcopy(previous))

        brief = _parse_brief(user_text)
        return _build_deterministic_storyboard(
            target=brief["target_seconds"], ratio=brief["ratio"],
            style=brief["style"], selling_points=brief["selling_points"],
            n_images=len(image_data_uris),
            force_voice_overflow=self.force_voice_overflow_shot,
            reference_only=self.reference_only)

    # ---------- 评审 ----------
    def review_storyboard(self, *, product_card: dict, storyboard: dict) -> ReviewReport:
        issues: list[ReviewIssue] = []
        shots = storyboard.get("shots", [])
        if shots and shots[0].get("purpose") != "hook":
            issues.append(ReviewIssue(
                severity="suggestion", shot_key=shots[0].get("shot_key"),
                code="weak_hook", message="第一个镜头没有明显的钩子，前 3 秒可能留不住人",
                fix="把第一镜 purpose 改为 hook，用反差或结果前置开场"))
        for shot in shots:
            audio = shot.get("audio") or {}
            text = audio.get("text") or ""
            dur = int(shot.get("duration", 0) or 0)
            if text and len(text) > dur * 4:
                issues.append(ReviewIssue(
                    severity="blocker", shot_key=shot.get("shot_key"),
                    code="voice_too_long",
                    message=f"口播 {len(text)} 字，超过 {dur} 秒上限 {dur * 4} 字",
                    fix="删减口播或增加该镜时长"))
        summary = "评审完成" if not issues else f"发现 {len(issues)} 个问题"
        return ReviewReport(summary=summary, issues=issues)

    # ---------- 单镜重写 ----------
    def rewrite_shot(self, *, shot, instruction: str, product_card) :
        """确定性地只改一个镜头：应用意见中的常见意图，其它字段不动。"""
        new = shot.model_copy(deep=True)
        text = instruction or ""
        if "运镜" in text or "镜头" in text:
            new.camera = "缓慢推近" if "推近" in text or "推" in text else "固定机位"
        if "动作" in text:
            new.action = (new.action + "；动作更聚焦").strip("；")
        # 若意见要求"简化/一个动作"，保留 visual，动作收敛
        if "简化" in text or "一个动作" in text:
            new.action = "只保留一个主要动作，节奏放慢"
        # 去掉可能的口播超长（配合评审建议）
        if new.audio.text and len(new.audio.text) > new.duration * 4:
            new.audio = type(new.audio)(type=new.audio.type,
                                        text=new.audio.text[: max(1, new.duration * 4 - 2)])
        new.prompt_override = ""
        return new


def _parse_brief(user_text: str) -> dict:
    """从编排层拼的 brief 文本里取几个关键字段（带默认值）。"""
    brief: dict = {"target_seconds": 30, "ratio": "9:16", "style": "",
                   "selling_points": []}
    try:
        start = user_text.find("{")
        if start >= 0:
            data, _ = json.JSONDecoder().raw_decode(user_text[start:])
            brief["target_seconds"] = int(data.get("target_seconds", 30))
            brief["ratio"] = data.get("ratio", "9:16")
            brief["style"] = data.get("style", "")
            brief["selling_points"] = list(data.get("selling_points", []))
    except Exception:
        pass
    return brief


def _build_deterministic_storyboard(*, target: int, ratio: str, style: str,
                                    selling_points: list[str], n_images: int,
                                    force_voice_overflow: str | None,
                                    reference_only: bool = False) -> StoryboardResult:
    """按目标时长均匀切成若干 4-15 秒的镜头，并覆盖全部卖点。"""
    points = [{"id": f"sp{i+1}", "text": text, "source": "用户提供"}
              for i, text in enumerate(selling_points)] or [
        {"id": "sp1", "text": "核心卖点", "source": "用户提供"}]

    durations = _split_duration(target)
    purposes = ["hook", "feature", "scene", "trust", "cta"]
    shots = []
    has_img = n_images > 0
    for i, dur in enumerate(durations):
        key = f"s{i+1}"
        purpose = purposes[min(i, len(purposes) - 1)]
        mode = "t2v"
        images = {"first_frame": None, "last_frame": None, "references": []}
        if has_img and reference_only:
            mode = "reference"
            images["references"] = [f"img{(i % n_images) + 1}"]
        elif has_img and i < min(2, n_images):
            mode = "first_frame"
            images["first_frame"] = f"img{i+1}"
        elif has_img:
            mode = "reference"
            images["references"] = [f"img{(i % n_images) + 1}"]

        point_id = points[i % len(points)]["id"]
        voice = {"type": "none", "text": ""}
        if force_voice_overflow == key:
            voice = {"type": "on_screen", "text": "字" * (dur * 4 + 10)}

        shots.append({
            "shot_key": key,
            "purpose": purpose,
            "duration": dur,
            "mode": mode,
            "images": images,
            "visual": f"{purpose} 画面：展示商品与使用场景",
            "action": "一个主要动作，节奏清晰",
            "camera": ["缓慢推近", "固定机位", "环绕", "跟拍"][i % 4],
            "audio": voice,
            "subtitle": points[i % len(points)]["text"],
            "selling_point_ids": [point_id],
            "prompt_override": "",
            "title": "",
        })

    # 确保每条卖点都被覆盖
    covered = {pid for s in shots for pid in s["selling_point_ids"]}
    for j, sp in enumerate(points):
        if sp["id"] not in covered and shots:
            shots[j % len(shots)]["selling_point_ids"].append(sp["id"])

    return StoryboardResult.model_validate({
        "product_card": {
            "name": "示例商品", "category": "",
            "appearance": "按参考图，颜色与结构与图片一致",
            "visible_features": [], "selling_points": points,
            "image_notes": [{"image_id": f"img{i+1}", "view": "",
                             "best_for": "首帧" if i < 2 and not reference_only else "参考图", "issues": ""}
                            for i in range(n_images)],
            "risky_claims": [],
        },
        "storyboard": {"ratio": ratio, "total_duration": sum(durations), "shots": shots},
    })


def _split_duration(target: int, lo: int = 4, hi: int = 15) -> list[int]:
    """把目标秒数切成每段 4-15 的整数，尽量均匀。target 本身允许 15-60。"""
    target = max(lo, min(target, 60))
    n = max(1, (target + hi - 1) // hi)
    base, rem = divmod(target, n)
    durations = [base + (1 if i < rem else 0) for i in range(n)]
    # 边界修正
    fixed: list[int] = []
    for d in durations:
        if d < lo:
            fixed.append(lo)
        else:
            fixed.append(d)
    total = sum(fixed)
    # 让总和回到 target（在容差内由规则校验处理；这里尽量对齐）
    while total - target > 1 and fixed:
        i = max(range(len(fixed)), key=lambda k: fixed[k])
        fixed[i] -= 1
        total -= 1
    return fixed
