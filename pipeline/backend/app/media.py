"""本地媒体工具：ffprobe 校验、data URI 编解码。"""
from __future__ import annotations

import base64
import json
import mimetypes
import subprocess
from pathlib import Path

_IMAGE_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
              "heif": "image/heif"}


def image_to_data_uri(path: str | Path) -> str:
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    mime = _IMAGE_EXT.get(ext) or mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    # 官网要求 data:image/<format>;base64,...，format 小写
    return f"data:{mime.lower()};base64,{b64}"


def probe_video(path: str | Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "width": int(video["width"]) if video else None,
        "height": int(video["height"]) if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "frame_rate": video.get("r_frame_rate") if video else None,
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_sample_rate": int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
    }
