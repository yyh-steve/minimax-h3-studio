"""FastAPI 入口：API + 托管前端。

上传改走 /api/projects/{id}/assets（要落库并生成画幅变体），
M1 的 /api/uploads 保留兼容（不关联项目，供单镜调试页）。
"""
from __future__ import annotations

import base64
import contextlib
import mimetypes
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ASSETS_DIR, ensure_dirs
from . import db
from .api.routes import router
from . import services

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_ALLOWED_UPLOAD = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    db.connect()
    services.get_runner()
    yield
    await services.shutdown_runner()


app = FastAPI(title="H3 图文短视频流水线", lifespan=lifespan)
app.include_router(router)


@app.post("/api/uploads")
async def upload_image(file: UploadFile = File(...)):
    """M1 兼容：上传一张图，返回 data URI（不关联项目）。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD:
        raise HTTPException(400, f"不支持的图片格式 {suffix}")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(400, "图片超过 30MB")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{suffix}"
    (ASSETS_DIR / name).write_bytes(data)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    uri = f"data:{mime.lower()};base64,{base64.b64encode(data).decode('ascii')}"
    return {"asset_id": name, "data_uri": uri, "bytes": len(data)}


@app.get("/")
def index():
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"message": "前端缺失；API 见 /docs"})


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
