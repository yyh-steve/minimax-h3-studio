"""分镜编排：M3 生成 / 规则校验 / M3 评审 / 整份与单镜重写 / 确认闸门。

确认闸门（方案 0/3）：
- 生成、编辑、评审阶段只调用 M3，绝不调用 H3
- blocker 清零才允许确认
- 确认时为每个镜头 build 一条 H3 take（含内容指纹），交给 M1 的 Runner
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import db
from ..config import Settings
from ..media import image_to_data_uri
from ..providers import get_m3
from ..providers.capabilities import local_planning_hint, validate_local_request
from ..storyboard_schema import (
    RuleIssue, Shot, StoryboardResult, blockers, build_h3_request,
    shot_fingerprint, storyboard_from_json, validate_rules,
)


class StoryboardService:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ---------- 组装与存取 ----------

    def _project_dict(self, project) -> dict:
        return {
            "id": project["id"], "name": project["name"], "ratio": project["ratio"],
            "target_seconds": project["target_seconds"], "style": project["style"],
            "selling_points": json.loads(project["selling_points_json"] or "[]"),
            "banned_words": json.loads(project["banned_words_json"] or "[]"),
            "h3_model": project["h3_model"],
            "draft_resolution": project["draft_resolution"],
        }

    def _original_assets(self, project_id: int) -> list:
        return [a for a in db.list_assets(project_id) if a["kind"] == "original"]

    def _asset_index(self, project_id: int) -> dict[str, Any]:
        rows = db.list_assets(project_id)
        by_id = {a["asset_id"]: a for a in rows}
        originals = [a for a in rows if a["kind"] == "original"]
        # M3 用 img1..imgN 引用原图
        img_ref = {f"img{i+1}": a["asset_id"] for i, a in enumerate(originals)}
        return {"by_id": by_id, "originals": originals, "img_ref": img_ref}

    def _load_result(self, storyboard_id: int) -> StoryboardResult:
        sb = db.get_storyboard(storyboard_id)
        if sb is None:
            raise ValueError("分镜不存在")
        card = json.loads(sb["product_card_json"] or "{}")
        shots = []
        for row in db.list_shots(storyboard_id):
            shots.append(self._shot_from_row(row))
        data = {"product_card": card,
                "storyboard": {"ratio": _project_ratio(sb["project_id"]),
                               "total_duration": sum(s["duration"] for s in shots),
                               "shots": shots}}
        return storyboard_from_json(data)

    def _shot_from_row(self, row) -> dict:
        return {
            "shot_key": row["shot_key"], "purpose": row["purpose"],
            "duration": row["duration"], "mode": row["mode"],
            "images": json.loads(row["images_json"] or "{}"),
            "visual": row["visual"], "action": row["action"],
            "camera": row["camera"], "audio": json.loads(row["audio_json"] or "{}"),
            "subtitle": row["subtitle"],
            "selling_point_ids": json.loads(row["selling_point_ids_json"] or "[]"),
            "prompt_override": row["prompt_override"], "title": row["title"],
        }

    # ---------- 生成 ----------

    def generate(self, project_id: int) -> dict:
        project = db.get_project(project_id)
        if project is None:
            raise ValueError("项目不存在")
        p = self._project_dict(project)
        idx = self._asset_index(project_id)
        if not idx["originals"]:
            raise ValueError("请先上传至少 1 张商品图")

        uris, img_lines = [], []
        for ref, asset_id in idx["img_ref"].items():
            asset = idx["by_id"][asset_id]
            uris.append(image_to_data_uri(asset["path"]))
            img_lines.append(f"{ref} = asset_id {asset['asset_id']}（{asset['width']}x{asset['height']}）")

        brief = {
            "product_name": p["name"], "target_seconds": p["target_seconds"],
            "ratio": p["ratio"], "style": p["style"],
            "selling_points": p["selling_points"], "banned_words": p["banned_words"],
            "image_refs": img_lines,
        }
        user_text = ("请根据商品图和以下需求生成分镜，镜头里引用图片请用 img1/img2 编号：\n"
                     + json.dumps(brief, ensure_ascii=False))
        if self.settings.provider == "local":
            user_text += "\n" + local_planning_hint()

        m3 = get_m3(self.settings)
        result = m3.generate_storyboard(user_text=user_text, image_data_uris=uris)
        return self._persist(project_id, result, created_by="m3")

    def _persist(self, project_id: int, result: StoryboardResult,
                 created_by: str = "m3") -> dict:
        """把 M3 结果落库：新版本 storyboard + shots，并把 imgN 映射成 asset_id。"""
        idx = self._asset_index(project_id)
        existing_versions = db.list_storyboards(project_id)
        version = (existing_versions[0]["version"] + 1) if existing_versions else 1

        # 图片引用归一化：imgN -> asset_id；变体/真实 asset_id 原样保留
        for i, shot in enumerate(result.storyboard.shots, start=1):
            if not shot.shot_key:
                shot.shot_key = f"s{i}"
            imgs = shot.images
            imgs.first_frame = self._resolve_ref(imgs.first_frame, idx)
            imgs.last_frame = self._resolve_ref(imgs.last_frame, idx)
            imgs.references = [self._resolve_ref(r, idx) for r in imgs.references]

        sb_id = db.create_storyboard(
            project_id, result.product_card.model_dump(), version=version,
            status="draft", created_by=created_by)
        db.replace_shots(sb_id, [s.model_dump() for s in result.storyboard.shots])
        db.update_project(project_id, active_storyboard_id=sb_id, status="editing")

        return self.get_detail(sb_id)

    @staticmethod
    def _resolve_ref(ref: str | None, idx: dict) -> str | None:
        if not ref:
            return None
        if ref in idx["by_id"]:
            return ref
        if ref in idx["img_ref"]:
            return idx["img_ref"][ref]
        return ref  # 未知引用留给规则引擎报 asset_missing

    # ---------- 校验 / 评审 ----------

    def _local_issues(self, result: StoryboardResult, project: dict,
                      available: set[str]) -> list[RuleIssue]:
        if self.settings.provider != "local":
            return []
        issues = []
        # Syntax-only stand-in: the actual media is validated again before confirm.
        sources = {aid: "data:image/png;base64,AAAA" for aid in available}
        for shot in result.storyboard.shots:
            try:
                if (shot.mode != "reference" or len(shot.images.references) != 1
                        or shot.images.first_frame or shot.images.last_frame):
                    raise ValueError("本地 H3 每镜必须是单张 reference_image；请手动修改首尾帧或多图分镜")
                request = build_h3_request(
                    shot, result.product_card, ratio=project["ratio"], style=project["style"],
                    model=project["h3_model"], resolution=project["draft_resolution"],
                    asset_data_uri=sources)
                validate_local_request(request)
            except (ValueError, KeyError) as error:
                issues.append(RuleIssue("blocker", "local_h3_capability", shot.shot_key, str(error)))
        return issues

    def validate(self, storyboard_id: int) -> dict:
        sb = db.get_storyboard(storyboard_id)
        project = db.get_project(sb["project_id"])
        result = self._load_result(storyboard_id)
        p = self._project_dict(project)
        available = set(self._asset_index(project["id"])["by_id"].keys())
        issues = validate_rules(
            result, target_seconds=p["target_seconds"], tolerance_seconds=1,
            user_banned=p["banned_words"], available_asset_ids=available)
        issues.extend(self._local_issues(result, p, available))
        report = {"issues": [i.__dict__ for i in issues],
                  "total_seconds": sum(s.duration for s in result.storyboard.shots),
                  "target_seconds": p["target_seconds"]}
        db.update_storyboard(storyboard_id, qc_report_json=report)
        return report

    def review(self, storyboard_id: int) -> dict:
        sb = db.get_storyboard(storyboard_id)
        result = self._load_result(storyboard_id)
        m3 = get_m3(self.settings)
        report = m3.review_storyboard(
            product_card=result.product_card.model_dump(),
            storyboard=result.storyboard.model_dump())
        payload = report.model_dump()
        db.update_storyboard(storyboard_id, review_json=payload)
        db.add_event(None, "m3_review", f"M3 评审完成：{payload.get('summary','')}",
                     project_id=sb["project_id"])
        return payload

    # ---------- 编辑与重写 ----------

    def save_product_card(self, storyboard_id: int, card: dict) -> dict:
        result = self._load_result(storyboard_id)
        # 只允许改理解卡字段，用它重建并保留镜头
        merged = result.model_copy(deep=True)
        merged.product_card = type(result.product_card).model_validate(card)
        return self._replace_from_result(storyboard_id, merged)

    def save_shot(self, storyboard_id: int, shot_key: str, patch: dict) -> dict:
        result = self._load_result(storyboard_id)
        for shot in result.storyboard.shots:
            if shot.shot_key == shot_key:
                # 用合并后的完整字典重新校验，确保 audio/images 等嵌套字段类型正确
                updated = Shot.model_validate({**shot.model_dump(), **patch})
                idx = result.storyboard.shots.index(shot)
                result.storyboard.shots[idx] = updated
                break
        else:
            raise ValueError(f"镜头 {shot_key} 不存在")
        return self._replace_from_result(storyboard_id, result)

    def rewrite_all(self, storyboard_id: int, instruction: str) -> dict:
        """带上当前理解卡和修改意见，让 M3 重写整份分镜。"""
        sb = db.get_storyboard(storyboard_id)
        project = db.get_project(sb["project_id"])
        p = self._project_dict(project)
        idx = self._asset_index(project["id"])
        current = self._load_result(storyboard_id)

        uris = [image_to_data_uri(a["path"]) for a in idx["originals"]]
        brief = {"product_name": p["name"], "target_seconds": p["target_seconds"],
                 "ratio": p["ratio"], "style": p["style"],
                 "selling_points": p["selling_points"],
                 "banned_words": p["banned_words"]}
        user_text = ("请按修改意见重写整份分镜，保留修正后的商品理解卡事实：\n"
                     + json.dumps(brief, ensure_ascii=False)
                     + "\n修改意见：" + instruction)
        m3 = get_m3(self.settings)
        result = m3.generate_storyboard(
            user_text=user_text, image_data_uris=uris,
            fix_message=instruction, previous=current.model_dump())
        # 重写以当前（人工修正过的）理解卡为准
        result.product_card = current.product_card
        return self._persist(project["id"], result)

    def rewrite_shot(self, storyboard_id: int, shot_key: str,
                     instruction: str) -> dict:
        """让 M3 只重写一个镜头（其余镜头不动）。"""
        result = self._load_result(storyboard_id)
        target = next((s for s in result.storyboard.shots if s.shot_key == shot_key), None)
        if target is None:
            raise ValueError(f"镜头 {shot_key} 不存在")
        m3 = get_m3(self.settings)
        new_shot = m3.rewrite_shot(
            shot=target, instruction=instruction,
            product_card=result.product_card)
        new_shot.shot_key = shot_key
        idx = result.storyboard.shots.index(target)
        result.storyboard.shots[idx] = Shot.model_validate(new_shot.model_dump())
        return self._replace_from_result(storyboard_id, result)

    def _replace_from_result(self, storyboard_id: int, result: StoryboardResult) -> dict:
        """用内存中的结果覆盖某草稿分镜的镜头/理解卡，不新增版本。"""
        sb = db.get_storyboard(storyboard_id)
        db.update_storyboard(storyboard_id,
                             product_card_json=result.product_card.model_dump())
        db.replace_shots(storyboard_id, [s.model_dump() for s in result.storyboard.shots])
        db.update_project(sb["project_id"], status="editing")
        return self.get_detail(storyboard_id)

    # ---------- 改稿：基于当前 confirmed 开新版本 ----------

    def revise(self, project_id: int) -> dict:
        """确认后改稿：复制当前 confirmed 分镜为新的 draft（镜头、理解卡一起复制）。"""
        project = db.get_project(project_id)
        if project is None:
            raise ValueError("项目不存在")
        src_id = project["active_storyboard_id"]
        if not src_id:
            raise ValueError("项目还没有分镜，请先生成")
        if db.get_storyboard(src_id)["status"] != "confirmed":
            raise ValueError("只有已确认的分镜才能开新版本改稿；请先确认当前分镜")
        src = self._load_result(src_id)
        versions = db.list_storyboards(project_id)
        version = (versions[0]["version"] + 1) if versions else 1
        new_id = db.create_storyboard(
            project_id, src.product_card.model_dump(), version=version,
            status="draft", created_by="user")
        db.replace_shots(new_id, [s.model_dump() for s in src.storyboard.shots])
        db.update_project(project_id, active_storyboard_id=new_id, status="editing")
        db.add_event(None, "revise",
                     f"基于 v{db.get_storyboard(src_id)['version']} 开草稿 v{version}",
                     project_id=project_id)
        return self.get_detail(new_id)

    # ---------- 确认闸门 ----------

    def confirm(self, storyboard_id: int) -> dict:
        # The status gate and every Take/selection write commit together. A
        # crash rolls the whole batch back; duplicate requests cannot mint IDs.
        with db.transaction():
            sb = db.get_storyboard(storyboard_id)
            if sb is None:
                raise ValueError("分镜不存在")
            if sb['status'] != 'draft':
                raise PermissionError("这份分镜已确认或已被新版本替代，不能重复创建生成任务")
            return self._confirm_draft(storyboard_id)

    def _confirm_draft(self, storyboard_id: int) -> dict:
        sb = db.get_storyboard(storyboard_id)
        project = db.get_project(sb["project_id"])
        p = self._project_dict(project)
        result = self._load_result(storyboard_id)
        available = set(self._asset_index(project["id"])["by_id"].keys())
        issues = validate_rules(result, target_seconds=p["target_seconds"],
                                user_banned=p["banned_words"],
                                available_asset_ids=available)
        issues.extend(self._local_issues(result, p, available))
        blocked = blockers(issues)
        if blocked:
            raise PermissionError("还有阻断项，不能确认：" +
                                  "；".join(i.message for i in blocked))
        error = self.settings.validate_provider()
        if error:
            raise PermissionError(error)

        assets = self._asset_index(project["id"])["by_id"]
        data_uris, hashes = {}, {}
        for aid, a in assets.items():
            data_uris[aid] = image_to_data_uri(a["path"])
            hashes[aid] = a["sha256"] or a["asset_id"]

        # Validate every complete request before creating or reusing any Take.
        requests = {}
        for shot in result.storyboard.shots:
            req = build_h3_request(
                shot, result.product_card, ratio=p["ratio"], style=p["style"],
                model=p["h3_model"], resolution=p["draft_resolution"],
                asset_data_uri=data_uris)
            if self.settings.provider == "local":
                try:
                    validate_local_request(req)
                except ValueError as error:
                    raise PermissionError(f"镜头 {shot.shot_key}：{error}") from error
            requests[shot.shot_key] = req

        # 同一项目的上一个 confirmed 版本（用于指纹复用）
        prev_confirmed = None
        for old in db.list_storyboards(project["id"]):
            if old["id"] != storyboard_id and old["status"] == "confirmed":
                prev_confirmed = old
                break

        new_take_ids, reused = [], []
        for shot in result.storyboard.shots:
            fp = shot_fingerprint(
                shot, result.product_card, provider=self.settings.provider,
                model=p["h3_model"], resolution=p["draft_resolution"],
                ratio=p["ratio"], style=p["style"], asset_hashes=hashes,
                backend_origin=self.settings.h3_origin)

            reused_take = db.find_accepted_take(
                project_id=project["id"], shot_key=shot.shot_key, fingerprint=fp)
            if reused_take is not None:
                db.set_shot_take(storyboard_id, shot.shot_key,
                                 int(reused_take["id"]), origin="reused", fingerprint=fp)
                reused.append(shot.shot_key)
                continue

            tid = db.create_take(
                model=p["h3_model"], request_json=requests[shot.shot_key],
                title=shot.title or f"{shot.shot_key} {shot.purpose}",
                shot_key=shot.shot_key, fingerprint=fp,
                project_id=project["id"], storyboard_id=storyboard_id,
                source="confirm", provider_name=self.settings.provider,
                provider_origin=self.settings.h3_origin)
            db.set_shot_take(storyboard_id, shot.shot_key, tid,
                             origin="new", fingerprint=fp)
            new_take_ids.append(tid)
            db.add_event(tid, "confirm",
                         f"确认 v{sb['version']}，镜头 {shot.shot_key} 新建生成任务（{shot.duration}s）",
                         project_id=project["id"])

        if prev_confirmed is not None:
            db.update_storyboard(prev_confirmed["id"], status="superseded")
        for old in db.list_storyboards(project["id"]):
            if old["id"] != storyboard_id and old["status"] == "draft":
                db.update_storyboard(old["id"], status="superseded")

        db.update_storyboard(storyboard_id, status="confirmed")
        db.update_project(project["id"], status="generating")
        db.add_event(None, "confirm",
                     f"确认 v{sb['version']}：新建 {len(new_take_ids)} 个镜头，复用 {len(reused)} 个："
                     + (",".join(reused) if reused else "无"),
                     project_id=project["id"])

        return {"storyboard_id": storyboard_id,
                "new_take_ids": new_take_ids, "reused_shot_keys": reused,
                "shots": len(result.storyboard.shots),
                "new_shots": len(new_take_ids), "reused_shots": len(reused),
                "estimated_output_seconds": sum(s.duration for s in result.storyboard.shots)}

    # ---------- 详情 ----------

    def get_detail(self, storyboard_id: int) -> dict:
        sb = db.get_storyboard(storyboard_id)
        if sb is None:
            raise ValueError("分镜不存在")
        result = self._load_result(storyboard_id)
        return {
            "storyboard_id": storyboard_id,
            "project_id": sb["project_id"], "version": sb["version"],
            "status": sb["status"], "created_by": sb["created_by"],
            "product_card": result.product_card.model_dump(),
            "shots": [s.model_dump() for s in result.storyboard.shots],
        }


def _project_ratio(project_id: int) -> str:
    project = db.get_project(project_id)
    return project["ratio"] if project else "9:16"
