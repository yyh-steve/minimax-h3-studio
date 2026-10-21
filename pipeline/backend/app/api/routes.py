"""REST 路由。"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import constants as C
from .. import db
from ..config import ASSETS_DIR
from ..image_ops import make_variants, sha256_bytes
from ..media import image_to_data_uri
from ..providers import get_provider
from ..providers.capabilities import validate_local_request
from ..pipeline.storyboard_service import StoryboardService
from .. import services

router = APIRouter(prefix="/api")

_MODELS = {"MiniMax-H3", "MiniMax-H3-Max"}
_RESOLUTIONS = {"480P", "768P", "2K"}
_RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
_ROLES = {"first_frame", "last_frame", "reference_image"}
_ALLOWED_UPLOAD = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_PROJECT_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}


class ImageIn(BaseModel):
    url: str = Field(..., description="公网 URL 或 data:image/...;base64,...")
    role: str = "first_frame"


class CreateTakeIn(BaseModel):
    prompt: str
    duration: int = Field(default=5, strict=True)
    resolution: str = "768P"
    ratio: str = "adaptive"
    model: str = "MiniMax-H3"
    title: str = ""
    images: list[ImageIn] = Field(default_factory=list)


class RetryIn(BaseModel):
    message: str = ""


class BindTaskIn(BaseModel):
    task_id: str


class SettingsIn(BaseModel):
    provider: str | None = None
    m3_provider: str | None = None
    h3_base_url: str | None = None
    m3_base_url: str | None = None
    api_key: str | None = None
    h3_api_key: str | None = None
    m3_api_key: str | None = None
    mock_scenario: str | None = None
    max_concurrency: int | None = None
    auto_retry_limit: int | None = None
    budget_output_seconds: int | None = None
    poll_interval_seconds: int | None = None
    m3_take_review: bool | None = None
    m3_review_fps: float | None = None
    m3_review_detail: str | None = None
    m3_mock_review: str | None = None


class ProjectIn(BaseModel):
    name: str
    ratio: str = "9:16"
    target_seconds: int = 30
    style: str = ""
    selling_points: list[str] = Field(default_factory=list)
    banned_words: list[str] = Field(default_factory=list)
    h3_model: str = "MiniMax-H3"
    draft_resolution: str = "768P"


class ProductCardIn(BaseModel):
    card: dict


class ShotPatchIn(BaseModel):
    patch: dict


class RewriteIn(BaseModel):
    instruction: str = ""


def _validate_and_build(payload: CreateTakeIn) -> dict:
    if not payload.prompt.strip():
        raise HTTPException(400, "prompt 不能为空")
    if not (4 <= payload.duration <= 15):
        raise HTTPException(400, "duration 必须是 4-15 的整数")
    if payload.model not in _MODELS:
        raise HTTPException(400, "model 不支持")
    if payload.resolution not in _RESOLUTIONS:
        raise HTTPException(400, "resolution 不支持")
    if payload.ratio not in _RATIOS:
        raise HTTPException(400, "ratio 不支持")

    content: list[dict] = [{"type": "text", "text": payload.prompt}]
    roles = [i.role for i in payload.images]
    for img in payload.images:
        if img.role not in _ROLES:
            raise HTTPException(400, f"未知 role: {img.role}")
        content.append({"type": "image_url",
                        "image_url": {"url": img.url}, "role": img.role})

    # 官网互斥规则：reference_image 与 first/last_frame 不能混用
    has_ref = any(r == "reference_image" for r in roles)
    has_keyframe = any(r in ("first_frame", "last_frame") for r in roles)
    if has_ref and has_keyframe:
        raise HTTPException(400, "参考图方式和首尾帧方式不能混用")
    if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
        raise HTTPException(400, "首帧、尾帧各最多 1 张")
    if roles.count("reference_image") > 9:
        raise HTTPException(400, "参考图最多 9 张")
    if roles and "last_frame" in roles and "first_frame" not in roles:
        raise HTTPException(400, "尾帧必须和首帧成对出现")

    # 文生必须显式比例（不能 adaptive）；图生比例恒为 adaptive
    if not has_ref and not has_keyframe and payload.ratio == "adaptive":
        raise HTTPException(400, "文生视频 ratio 必填，不能是 adaptive")
    request = {"model": payload.model, "content": content,
               "duration": payload.duration, "resolution": payload.resolution,
               "ratio": "adaptive" if has_keyframe else payload.ratio}
    return request


def _take_dict(row) -> dict:
    d = dict(row)
    for key in ("request_json", "error_json", "probe_json", "usage_json", "raw_response_json",
                "qc_json"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    # 请求体里的 data URI 很大，列表接口不返回原文
    req = d.get("request_json")
    if isinstance(req, dict):
        for c in req.get("content", []):
            url = (c.get("image_url") or {}).get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                c["image_url"]["url"] = url[:40] + f"...（{len(url)} 字符）"
    d["can_cancel"] = d["status"] == C.QUEUED
    d["can_bind_task"] = d["status"] == C.UNKNOWN and not d.get("task_id")
    d["can_retry"] = d["status"] in (C.REJECTED, C.REVIEW, C.NEEDS_EDIT, C.EXPIRED, C.PAUSED)
    d["can_accept"] = bool(d.get("local_path")) and d["status"] in (C.REVIEW, C.REJECTED)
    d["can_requalify"] = bool(d.get("local_path")) and d["status"] in (C.REVIEW, C.REJECTED,
                                                                         C.ACCEPTED)
    return d


@router.get("/health")
def health():
    s = services.get_settings()
    err = s.validate_provider()
    return {"ok": err is None, "provider": s.provider, "error": err,
            "paused": services.get_runner().is_paused}


@router.get("/settings")
def get_settings():
    return services.get_settings().public_dict()


@router.put("/settings")
def put_settings(payload: SettingsIn):
    patch = payload.model_dump(exclude_none=True)
    if "m3_review_fps" in patch and not (0.2 <= patch["m3_review_fps"] <= 5):
        raise HTTPException(400, "M3 看片 fps 必须在 0.2-5 之间（官网 video_url.fps 取值范围）")
    if "m3_review_detail" in patch and patch["m3_review_detail"] not in ("low", "default", "high"):
        raise HTTPException(400, "M3 看片 detail 只能是 low / default / high")
    if "m3_mock_review" in patch and patch["m3_mock_review"] not in (
            "pass", "fail_once", "fail", "uncertain", "error_once"):
        raise HTTPException(400, "未知的假 M3 看片场景")
    try:
        s = services.update_settings(patch)
    except services.BackendChangeBlocked as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return s.public_dict()


@router.post("/takes")
def create_take(payload: CreateTakeIn):
    s = services.get_settings()
    if s.validate_provider():
        raise HTTPException(400, s.validate_provider())
    request = _validate_and_build(payload)
    if s.provider == "local":
        try:
            validate_local_request(request)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    take_id = db.create_take(model=payload.model, request_json=request,
                             title=payload.title or payload.prompt[:20],
                             provider_name=s.provider, provider_origin=s.h3_origin)
    db.add_event(take_id, "api", "已创建分镜任务，等待提交")
    return _take_dict(db.get_take(take_id))


@router.get("/takes")
def list_takes(limit: int = 100):
    return [_take_dict(r) for r in db.list_takes(limit=limit)]


@router.get("/takes/{take_id}")
def get_take(take_id: int):
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    return _take_dict(row)


@router.get("/takes/{take_id}/events")
def take_events(take_id: int, limit: int = 100):
    if db.get_take(take_id) is None:
        raise HTTPException(404, "take 不存在")
    return [dict(e) for e in db.list_events(take_id, limit=limit)]


@router.post("/takes/{take_id}/retry")
def manual_retry(take_id: int, payload: RetryIn):
    """手动重试：不受自动重试上限和预算护栏约束，始终新建一条 take（同项目、同镜头）。"""
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    s = services.get_settings()
    if s.validate_provider():
        raise HTTPException(400, s.validate_provider())
    req = json.loads(row["request_json"])
    if s.provider == "local":
        try:
            validate_local_request(req)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    new_id = db.create_take(
        shot_key=row["shot_key"], attempt_no=int(row["attempt_no"]) + 1,
        parent_take_id=take_id, title=row["title"], model=row["model"],
        request_json=req, fingerprint=(row["fingerprint"] or "") if
        (row["provider_name"], row["provider_origin"]) == (s.provider, s.h3_origin) else "",
        project_id=row["project_id"], storyboard_id=row["storyboard_id"],
        source="manual_retry", provider_name=s.provider, provider_origin=s.h3_origin)
    db.add_event(take_id, "retry", f"手动重试：{payload.message or '无备注'}", level="warn",
                 project_id=row["project_id"])
    db.add_event(new_id, "retry", f"由 #{take_id} 手动重试创建", project_id=row["project_id"])
    return _take_dict(db.get_take(new_id))


@router.post("/takes/{take_id}/cancel")
def cancel_take(take_id: int):
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    if row["status"] != C.QUEUED or not row["task_id"]:
        raise HTTPException(400, "只有排队中（queued）且有 task_id 的任务可以取消")
    try:
        services.validate_take_origin(row)
    except services.BackendChangeBlocked as e:
        raise HTTPException(409, str(e)) from e
    provider = get_provider(services.get_settings())
    if not db.update_take_if(take_id, expected_status=C.QUEUED,
                             expected_task_id=row['task_id'], status=C.CANCELLING):
        raise HTTPException(409, '任务状态已变化，请刷新后核对')
    try:
        result = provider.cancel(row["task_id"])
    except Exception as e:
        db.update_take_if(take_id, expected_status=C.CANCELLING,
                          expected_task_id=row['task_id'], status=C.REVIEW,
                          error_json={'kind': 'cancel_unknown', 'message': str(e)})
        raise HTTPException(400, f"取消失败：{e}")
    action = result.get("action") or result.get("status")
    if action == "cancelled":
        db.update_take_if(take_id, expected_status=C.CANCELLING,
                          expected_task_id=row['task_id'], status=C.CANCELLED, platform_status="cancelled")
        db.add_event(take_id, "cancel", f"已取消：{result}")
    else:
        db.update_take_if(take_id, expected_status=C.CANCELLING,
                          expected_task_id=row['task_id'], status=C.REVIEW, platform_status=action or "unknown")
        message = ("取消时任务已结束，平台已删除结果；请人工核对，不会自动重生成"
                   if action == "deleted" else "平台未确认取消；请人工核对")
        db.add_event(take_id, "cancel", message, level="warn")
    return _take_dict(db.get_take(take_id))


@router.post("/takes/{take_id}/bind-task")
def bind_task(take_id: int, payload: BindTaskIn):
    """人工提供确定的任务 ID，只查询并绑定，不猜测或再次创建。"""
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    if row["status"] != C.UNKNOWN or row["task_id"]:
        raise HTTPException(409, "只有未绑定 task_id 的 unknown 任务可以人工绑定")
    task_id = payload.task_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", task_id):
        raise HTTPException(400, "请输入有效的平台 task_id")
    if task_id in db.bound_task_ids():
        raise HTTPException(409, "该 task_id 已绑定其他 Take")
    try:
        services.validate_take_origin(row)
    except services.BackendChangeBlocked as e:
        raise HTTPException(409, str(e)) from e
    s = services.get_settings()
    if s.validate_provider():
        raise HTTPException(400, s.validate_provider())
    try:
        info = get_provider(s).get_task(task_id)
    except Exception as e:
        raise HTTPException(400, f"查询任务失败：{e}") from e
    if (not info.found or info.task_id != task_id
            or info.status not in (C.QUEUED, C.RUNNING, C.SUCCEEDED, "failed", C.CANCELLED)):
        raise HTTPException(400, "平台未返回可绑定的明确任务状态")
    db.bind_take_source(take_id, s.provider, s.h3_origin)
    db.update_take(take_id, task_id=task_id,
                   status=C.REVIEW if info.status == "failed" else info.status,
                   platform_status=info.status, raw_response_json=info.raw,
                   error_json=({"code": info.error_code, "message": info.error_message}
                               if info.status == "failed" else None))
    db.add_event(take_id, "reconcile", f"人工绑定已核实的平台任务 {task_id}")
    return _take_dict(db.get_take(take_id))


class AcceptIn(BaseModel):
    note: str = ""


@router.post("/takes/{take_id}/accept")
def accept_take(take_id: int, payload: AcceptIn):
    """人工采纳：质检拿不准或误判时，由人决定这条成片可用，并把镜头选用指向它。"""
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    if not row["local_path"] or not Path(row["local_path"]).exists():
        raise HTTPException(400, "这条 take 没有本地成片，不能采纳")
    if row["status"] not in (C.REVIEW, C.REJECTED, C.ACCEPTED):
        raise HTTPException(400, f"当前状态 {row['status']} 不能人工采纳")
    qc = json.loads(row["qc_json"] or "{}")
    qc["decision"] = {"result": "accepted", "by": "human", "note": payload.note,
                      "previous_status": row["status"], "ts": db.now()}
    db.update_take(take_id, status=C.ACCEPTED, qc_json=qc, error_json=None)
    family = db.take_lineage_ids(take_id)
    changed = db.retarget_selection(family, take_id, keep_accepted=False)
    db.add_event(take_id, "qc", f"人工采纳{('：' + payload.note) if payload.note else ''}",
                 project_id=row["project_id"])
    # 采纳了更早的结果后，同一家族里还没开始的自动重试就没必要再花钱了
    for tid in family:
        other = db.get_take(tid)
        if other is not None and other["id"] != take_id and other["status"] in (C.PENDING,
                                                                                  C.BACKOFF):
            db.update_take(tid, status=C.CANCELLED)
            db.add_event(tid, "cancel", f"已人工采纳 #{take_id}，取消尚未提交的重试",
                         project_id=row["project_id"])
    return {**_take_dict(db.get_take(take_id)), "retargeted": len(changed)}


@router.post("/takes/{take_id}/requalify")
def requalify_take(take_id: int):
    """重新质检：成片不变，重新跑技术检查和 M3 看片（不花 H3 的钱）。"""
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    if not row["local_path"] or not Path(row["local_path"]).exists():
        raise HTTPException(400, "这条 take 没有本地成片")
    if row["status"] not in (C.REVIEW, C.REJECTED, C.ACCEPTED):
        raise HTTPException(400, f"当前状态 {row['status']} 不能重新质检")
    db.update_take(take_id, status=C.DOWNLOADED, qc_json=None, error_json=None, next_run_at=None)
    db.add_event(take_id, "qc", "人工发起重新质检", project_id=row["project_id"])
    return _take_dict(db.get_take(take_id))


class SelectIn(BaseModel):
    take_id: int


@router.put("/storyboards/{storyboard_id}/shots/{shot_key}/selection")
def select_take_for_shot(storyboard_id: int, shot_key: str, payload: SelectIn):
    """手动选片：把某镜头的选用改成同项目同镜头的另一条已采纳 take。"""
    sb = db.get_storyboard(storyboard_id)
    if sb is None:
        raise HTTPException(404, "分镜不存在")
    take = db.get_take(payload.take_id)
    if take is None or take["project_id"] != sb["project_id"] or take["shot_key"] != shot_key:
        raise HTTPException(400, "只能选用同项目、同镜头的 take")
    if take["status"] != C.ACCEPTED:
        raise HTTPException(400, "只能选用已采纳的 take，可先人工采纳")
    sel = db.get_shot_take(storyboard_id, shot_key)
    origin = "reused" if take["storyboard_id"] != storyboard_id else "new"
    db.set_shot_take(storyboard_id, shot_key, int(take["id"]), origin=origin,
                     fingerprint=(sel["fingerprint"] if sel else take["fingerprint"] or ""))
    db.add_event(int(take["id"]), "select", f"镜头 {shot_key} 手动选用 #{take['id']}",
                 project_id=sb["project_id"])
    return {"storyboard_id": storyboard_id, "shot_key": shot_key, "take_id": int(take["id"])}


@router.get("/events")
def all_events(limit: int = 200):
    return [dict(e) for e in db.list_events(None, limit=limit)]


@router.post("/queue/pause")
def pause_queue():
    services.get_runner().pause()
    return {"paused": True}


@router.post("/queue/resume")
def resume_queue():
    services.get_runner().resume()
    return {"paused": False}


@router.post("/queue/reconcile")
def reconcile_now():
    counts = services.get_runner().reconcile_on_startup()
    return {"counts": counts}


@router.get("/media/takes/{take_id}")
def take_media(take_id: int):
    """流式返回本地成片，供 <video> 播放。"""
    from fastapi.responses import FileResponse
    row = db.get_take(take_id)
    if row is None:
        raise HTTPException(404, "take 不存在")
    if not row["local_path"]:
        raise HTTPException(404, "该 take 还没有本地成片")
    path = Path(row["local_path"])
    if not path.exists():
        raise HTTPException(404, "成片文件缺失")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


# ================= M2：项目 / 素材 / 分镜 =================

def _project_dict(row) -> dict:
    d = dict(row)
    d["selling_points"] = json.loads(row["selling_points_json"] or "[]")
    d["banned_words"] = json.loads(row["banned_words_json"] or "[]")
    d.pop("selling_points_json", None)
    d.pop("banned_words_json", None)
    return d


def _asset_dict(row) -> dict:
    return dict(row)


def _svc() -> StoryboardService:
    return StoryboardService(services.get_settings())


@router.post("/projects")
def create_project(payload: ProjectIn):
    if not (15 <= payload.target_seconds <= 60):
        raise HTTPException(400, "目标时长建议 15-60 秒")
    if payload.ratio not in _PROJECT_RATIOS:
        raise HTTPException(400, "画幅必须是具体比例")
    if payload.h3_model not in _MODELS:
        raise HTTPException(400, "model 不支持")
    if payload.draft_resolution not in _RESOLUTIONS:
        raise HTTPException(400, "分辨率不支持")
    pid = db.create_project(
        name=payload.name, ratio=payload.ratio, target_seconds=payload.target_seconds,
        style=payload.style, selling_points=payload.selling_points,
        banned_words=payload.banned_words, h3_model=payload.h3_model,
        draft_resolution=payload.draft_resolution)
    db.add_event(None, "project", f"创建项目 {pid}：{payload.name}", project_id=pid)
    return _project_dict(db.get_project(pid))


@router.get("/projects")
def list_projects(limit: int = 100):
    return [_project_dict(r) for r in db.list_projects(limit=limit)]


@router.get("/projects/{project_id}")
def get_project(project_id: int):
    row = db.get_project(project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    return _project_dict(row)


@router.post("/projects/{project_id}/assets")
async def upload_project_asset(project_id: int, file: UploadFile = File(...),
                               make_variants_flag: bool = True):
    """上传商品原图并落库；默认生成目标画幅的裁切/补边两个首帧候选。"""
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD:
        raise HTTPException(400, f"不支持的图片格式 {suffix}")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, "图片超过 30MB")

    project = db.get_project(project_id)
    assets_dir = ASSETS_DIR / f"project_{project_id}"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_id = f"{uuid.uuid4().hex[:12]}{suffix}"
    src_path = assets_dir / asset_id
    src_path.write_bytes(data)

    width = height = None
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            width, height = im.size
    except Exception:
        pass
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heif"}.get(suffix, "application/octet-stream")
    db.add_asset(project_id=project_id, asset_id=asset_id, path=str(src_path),
                 kind="original", variant="", sha256=sha256_bytes(data),
                 width=width, height=height, mime=mime, size_bytes=len(data))

    variants_out = []
    if make_variants_flag and mime in ("image/jpeg", "image/png", "image/webp"):
        out_dir = assets_dir / "variants"
        for v in make_variants(src_path, project["ratio"], out_dir):
            v_id = Path(v["path"]).name
            db.add_asset(project_id=project_id, asset_id=v_id, path=v["path"],
                         kind="variant", variant=v["variant"], parent_asset_id=asset_id,
                         sha256=v["sha256"], width=v["width"], height=v["height"],
                         mime=v["mime"], size_bytes=v["bytes"])
            variants_out.append({"asset_id": v_id, "variant": v["variant"],
                                 "width": v["width"], "height": v["height"]})

    db.add_event(None, "asset", f"上传商品图 {asset_id}", project_id=project_id)
    return {"asset_id": asset_id, "width": width, "height": height,
            "variants": variants_out}


@router.get("/projects/{project_id}/assets")
def list_project_assets(project_id: int):
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    return [_asset_dict(r) for r in db.list_assets(project_id)]


@router.get("/projects/{project_id}/assets/{asset_id}")
def get_project_asset_file(project_id: int, asset_id: str):
    from fastapi.responses import FileResponse
    row = db.get_asset_by_id(project_id, asset_id)
    if row is None:
        raise HTTPException(404, "素材不存在")
    return FileResponse(row["path"], media_type=row["mime"] or "application/octet-stream")


@router.post("/projects/{project_id}/storyboards/generate")
def generate_storyboard(project_id: int):
    try:
        return _svc().generate(project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if e.__class__.__name__ == "M3Error":
            raise HTTPException(502, f"M3 调用失败：{e}")
        raise


@router.get("/projects/{project_id}/storyboards/active")
def active_storyboard(project_id: int):
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if not project["active_storyboard_id"]:
        return {"storyboard_id": None}
    return _svc().get_detail(int(project["active_storyboard_id"]))


@router.get("/storyboards/{storyboard_id}")
def get_storyboard(storyboard_id: int):
    try:
        return _svc().get_detail(storyboard_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/storyboards/{storyboard_id}/validate")
def validate_storyboard(storyboard_id: int):
    try:
        return _svc().validate(storyboard_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/storyboards/{storyboard_id}/review")
def review_storyboard(storyboard_id: int):
    try:
        return _svc().review(storyboard_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        if e.__class__.__name__ == "M3Error":
            raise HTTPException(502, f"M3 调用失败：{e}")
        raise


@router.put("/storyboards/{storyboard_id}/product-card")
def save_product_card(storyboard_id: int, payload: ProductCardIn):
    try:
        return _svc().save_product_card(storyboard_id, payload.card)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/storyboards/{storyboard_id}/shots/{shot_key}")
def save_shot(storyboard_id: int, shot_key: str, payload: ShotPatchIn):
    try:
        return _svc().save_shot(storyboard_id, shot_key, payload.patch)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/storyboards/{storyboard_id}/rewrite-all")
def rewrite_all(storyboard_id: int, payload: RewriteIn):
    try:
        return _svc().rewrite_all(storyboard_id, payload.instruction)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if e.__class__.__name__ == "M3Error":
            raise HTTPException(502, f"M3 调用失败：{e}")
        raise


@router.post("/storyboards/{storyboard_id}/shots/{shot_key}/rewrite")
def rewrite_shot(storyboard_id: int, shot_key: str, payload: RewriteIn):
    try:
        return _svc().rewrite_shot(storyboard_id, shot_key, payload.instruction)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if e.__class__.__name__ == "M3Error":
            raise HTTPException(502, f"M3 调用失败：{e}")
        raise


@router.post("/storyboards/{storyboard_id}/confirm")
def confirm_storyboard(storyboard_id: int):
    try:
        return _svc().confirm(storyboard_id)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/projects/{project_id}/storyboards/revise")
def revise_storyboard(project_id: int):
    """确认后改稿：基于当前 confirmed 开一个草稿新版本。"""
    try:
        return _svc().revise(project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/projects/{project_id}/board")
def project_board(project_id: int):
    """镜头看板：当前 active 分镜每镜选用的 take（含复用标记、状态、视频）。"""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    sb_id = project["active_storyboard_id"]
    if not sb_id:
        return {"project_id": project_id, "storyboard_id": None, "shots": [],
                "total": 0, "accepted": 0, "all_accepted": False}

    detail = _svc().get_detail(sb_id)
    selections = {r["shot_key"]: r for r in db.list_shot_takes(sb_id)}
    out_shots, accepted = [], 0
    for shot in detail["shots"]:
        key = shot["shot_key"]
        item = {"shot_key": key, "purpose": shot["purpose"],
                "duration": shot["duration"], "mode": shot["mode"],
                "subtitle": shot.get("subtitle", ""),
                "take_id": None, "status": "pending_take",
                "origin": None, "reused": False, "attempt_no": None,
                "video_url": None, "error": None,
                "can_cancel": False, "can_retry": False, "can_bind_task": False}
        sel = selections.get(key)
        if sel is not None:
            take = db.get_take(int(sel["take_id"]))
            if take is not None:
                display = take
                if take["status"] != C.ACCEPTED:
                    # 选用指向的结果还没通过时，看板展示这个重试家族里最新的一次尝试，
                    # 避免旧 take 已 rejected、按钮却还挂在旧记录上。
                    family = sorted((db.get_take(t) for t in
                                     db.take_lineage_ids(int(take["id"]))),
                                    key=lambda t: t["id"], reverse=True)
                    display = family[0] if family else take
                qc = json.loads(display["qc_json"]) if display["qc_json"] else None
                item.update({
                    "take_id": display["id"], "status": display["status"],
                    "origin": sel["origin"], "reused": sel["origin"] == "reused",
                    "attempt_no": display["attempt_no"],
                    "video_url": f"/api/media/takes/{display['id']}" if display["local_path"] else None,
                    "error": json.loads(display["error_json"]) if display["error_json"] else None,
                    "platform_duration": display["platform_duration"],
                    "qc": _qc_brief(qc),
                    "can_cancel": display["status"] == C.QUEUED,
                    "can_bind_task": display["status"] == C.UNKNOWN and not display["task_id"],
                    "can_retry": display["status"] in (C.REJECTED, C.REVIEW,
                                                       C.NEEDS_EDIT, C.PAUSED, C.EXPIRED),
                    "can_accept": bool(display["local_path"]) and display["status"] in (
                        C.REVIEW, C.REJECTED),
                    "can_requalify": bool(display["local_path"]) and display["status"] in (
                        C.REVIEW, C.REJECTED, C.ACCEPTED),
                })
                if display["status"] == C.ACCEPTED:
                    accepted += 1
        out_shots.append(item)

    total = len(out_shots)
    renders = [_render_brief(r) for r in db.list_renders(project_id=project_id, limit=5)]
    return {"project_id": project_id, "storyboard_id": sb_id,
            "version": detail["version"],
            "status": detail["status"], "shots": out_shots,
            "total": total, "accepted": accepted,
            "all_accepted": total > 0 and accepted == total,
            "can_render": detail["status"] == "confirmed" and total > 0 and accepted == total,
            "used_output_seconds": db.used_output_seconds(project_id),
            "budget_seconds": int(project["target_seconds"]) * 3,
            "has_bgm": bool(project["bgm_path"]) and Path(project["bgm_path"]).exists(),
            "renders": renders,
            "estimated_output_seconds": sum(s["duration"] for s in detail["shots"])}


def _qc_brief(qc: dict | None) -> dict | None:
    """看板只要结论和要点，不返回整段检测区间。"""
    if not qc:
        return None
    tech = qc.get("tech") or {}
    m3 = qc.get("m3") or {}
    return {
        "tech_passed": tech.get("passed"),
        "tech_issues": [i.get("message") for i in tech.get("issues", [])],
        "m3_verdict": m3.get("final_verdict") if not m3.get("skipped") else "skipped",
        "m3_summary": m3.get("summary", "") if not m3.get("skipped") else m3.get("reason", ""),
        "m3_issues": [i.get("message") for i in m3.get("issues", [])],
        "m3_retry_hint": m3.get("retry_hint", ""),
        "m3_error": qc.get("m3_last_error"),
        "decision": qc.get("decision"),
    }


def _render_brief(row) -> dict:
    from ..pipeline.render_service import render_dict
    d = render_dict(row)
    d.pop("plan_json", None)
    return d


# ================= M4：合成 =================

class RenderIn(BaseModel):
    transition: str | None = None
    subtitles: bool | None = None
    keep_audio: bool | None = None
    original_volume: float | None = None
    use_bgm: bool | None = None
    bgm_volume: float | None = None


@router.post("/projects/{project_id}/renders")
def create_render(project_id: int, payload: RenderIn):
    from ..pipeline.render_service import RenderNotReady, create_render as _create, render_dict
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    try:
        rid = _create(project_id, payload.model_dump(exclude_none=True))
    except RenderNotReady as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return render_dict(db.get_render(rid))


@router.get("/projects/{project_id}/renders")
def list_project_renders(project_id: int, limit: int = 20):
    from ..pipeline.render_service import render_dict
    return [render_dict(r) for r in db.list_renders(project_id=project_id, limit=limit)]


@router.get("/renders/{render_id}")
def get_render(render_id: int):
    from ..pipeline.render_service import render_dict
    row = db.get_render(render_id)
    if row is None:
        raise HTTPException(404, "合成记录不存在")
    return render_dict(row)


@router.get("/media/renders/{render_id}")
def render_media(render_id: int, download: int = 0):
    from fastapi.responses import FileResponse
    row = db.get_render(render_id)
    if row is None or row["status"] != "done" or not row["output_path"]:
        raise HTTPException(404, "成片还没有合成好")
    path = Path(row["output_path"])
    if not path.exists():
        raise HTTPException(404, "成片文件缺失")
    project = db.get_project(int(row["project_id"]))
    name = f"{(project['name'] if project else 'video') or 'video'}_v{render_id}.mp4"
    return FileResponse(str(path), media_type="video/mp4", filename=name,
                        content_disposition_type="attachment" if download else "inline")


@router.post("/projects/{project_id}/bgm")
async def upload_bgm(project_id: int, file: UploadFile = File(...)):
    from ..pipeline.render_service import save_bgm
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    data = await file.read()
    try:
        path = save_bgm(project_id, file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"project_id": project_id, "bgm": Path(path).name, "bytes": len(data)}


@router.delete("/projects/{project_id}/bgm")
def delete_bgm(project_id: int):
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if project["bgm_path"]:
        Path(project["bgm_path"]).unlink(missing_ok=True)
    db.update_project(project_id, bgm_path=None)
    return {"project_id": project_id, "bgm": None}
