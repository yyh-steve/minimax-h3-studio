"""M3 看片的输入准备：代理视频、参考图、分镜要求文本。

- 代理视频：短边 384、4fps、去掉音轨（M3 不支持音频输入），只用于看片，不进成片。
  以 data:video/mp4;base64 传给官网 video_url（URL/base64 视频 ≤50MB）。
- 参考图：优先用镜头里引用的图（首帧/尾帧/参考图），否则用项目原图；
  M3 只认 JPEG/PNG/GIF/WEBP（HEIC 只能给 H3），统一转成长边 ≤1024 的 JPEG。
- 分镜要求：画面/动作/运镜 + 理解卡里的外观描述，让 M3 有据可查。
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
from pathlib import Path

from PIL import Image

from . import constants as C
from . import db


def make_review_proxy(src: str | Path, out: str | Path) -> Path:
    """生成看片代理：短边 384、REVIEW_PROXY_FPS、无音轨、H.264。"""
    se = C.REVIEW_PROXY_SHORT_EDGE
    vf = (f"scale='if(lt(iw,ih),{se},-2)':'if(lt(iw,ih),-2,{se})',"
          f"fps={C.REVIEW_PROXY_FPS},format=yuv420p")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"生成看片代理失败：{proc.stderr.strip()[-300:]}")
    if out.stat().st_size > C.M3_VIDEO_MAX_BYTES:
        raise RuntimeError("看片代理超过 M3 的 50MB 限制")
    return out


def video_data_uri(path: str | Path) -> str:
    return "data:video/mp4;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def image_for_m3(path: str | Path, long_edge: int = C.REVIEW_REF_IMAGE_LONG_EDGE) -> str | None:
    """转成 M3 能读的 JPEG data URI；打不开（例如本机 Pillow 不支持 HEIC）返回 None。"""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = min(1.0, long_edge / max(im.size))
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _shot_row(storyboard_id: int | None, shot_key: str):
    if storyboard_id is None:
        return None
    for row in db.list_shots(int(storyboard_id)):
        if row["shot_key"] == shot_key:
            return row
    return None


def build_review_context(take) -> dict:
    """组装看片需要的参考图和分镜要求。无项目的单镜调试任务也能用（只看提示词）。"""
    req = json.loads(take["request_json"] or "{}")
    prompt = next((c.get("text", "") for c in req.get("content", [])
                   if c.get("type") == "text"), "")
    expect_voice = False
    ref_paths: list[str] = []
    lines = [f"镜头 {take['shot_key']}，时长 {req.get('duration', '?')} 秒"]

    shot = _shot_row(take["storyboard_id"], take["shot_key"])
    card = {}
    if take["storyboard_id"] is not None:
        sb = db.get_storyboard(int(take["storyboard_id"]))
        if sb is not None:
            card = json.loads(sb["product_card_json"] or "{}")
    if shot is not None:
        audio = json.loads(shot["audio_json"] or "{}")
        expect_voice = audio.get("type") == "on_screen" and bool(audio.get("text"))
        lines += [f"画面：{shot['visual']}", f"动作：{shot['action']}",
                  f"运镜：{shot['camera'] or '固定机位'}"]
        if expect_voice:
            lines.append(f"画内口播：{audio.get('text')}（本次只看画面，口型不做要求）")
        images = json.loads(shot["images_json"] or "{}")
        ids = [images.get("first_frame"), images.get("last_frame"), *images.get("references", [])]
        for aid in ids:
            if not aid or take["project_id"] is None:
                continue
            asset = db.get_asset_by_id(int(take["project_id"]), aid)
            if asset is not None:
                ref_paths.append(asset["path"])
    else:
        lines.append(f"生成提示词：{prompt}")

    if card.get("appearance"):
        lines.append(f"商品外观（理解卡）：{card['appearance']}")
    lines.append("约束：画面中不应出现任何文字、字幕或水印。")

    if not ref_paths and take["project_id"] is not None:
        ref_paths = [a["path"] for a in db.list_assets(int(take["project_id"]))
                     if a["kind"] == "original"]
    uris: list[str] = []
    seen: set[str] = set()
    for p in ref_paths:
        if p in seen:
            continue
        seen.add(p)
        uri = image_for_m3(p)
        if uri:
            uris.append(uri)
        if len(uris) >= C.REVIEW_MAX_REF_IMAGES:
            break
    return {"shot_text": "\n".join(lines), "ref_image_uris": uris,
            "expect_voice": expect_voice, "expected_duration": req.get("duration"),
            "request_ratio": req.get("ratio")}
