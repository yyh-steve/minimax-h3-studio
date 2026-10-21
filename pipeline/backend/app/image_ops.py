"""图片预处理：为目标画幅生成首帧候选（居中裁切 / 模糊补边）。

官网首帧方式的画幅由输入图决定（ratio=adaptive），所以要先把首帧图处理到目标比例。
M3 不支持 HEIC（H3 支持），送给 M3 看的图统一转成 JPEG。
"""
from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

_RATIO = {
    "9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1),
    "3:4": (3, 4), "4:3": (4, 3), "21:9": (21, 9),
}
# 与 H3-Base 竖版画幅对齐的短边尺寸（768 的整数化，再取 32 倍数）
_SHORT_EDGE = 768


def ratio_size(ratio: str) -> tuple[int, int]:
    """返回目标 (宽, 高)，短边 768、对齐到 32（向下取整，避免超过短边面积）。"""
    rw, rh = _RATIO[ratio]
    if rw <= rh:                       # 竖版/方形：宽=768
        w = _SHORT_EDGE
        h = int(w * rh / rw // 32) * 32
    else:                              # 横版：高=768
        h = _SHORT_EDGE
        w = int(h * rw / rh // 32) * 32
    return w, h


def _target_box(ratio: str) -> tuple[int, int]:
    return ratio_size(ratio)


def center_crop(img: Image.Image, ratio: str) -> Image.Image:
    tw, th = _target_box(ratio)
    tr = tw / th
    out = ImageOps.fit(img, (tw, th), method=Image.LANCZOS,
                       centering=(0.5, 0.5))
    # ImageOps.fit 按源图比例裁，目标比例由 size 决定；确保不拉伸
    return out


def blur_pad(img: Image.Image, ratio: str) -> Image.Image:
    """等比缩放放进目标画布，空白用放大+模糊的同图填充。"""
    tw, th = _target_box(ratio)
    src = img.convert("RGB")
    scale = min(tw / src.width, th / src.height)
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    fg = src.resize((nw, nh), Image.LANCZOS)

    bg_scale = max(tw / src.width, th / src.height)
    bw, bh = int(src.width * bg_scale) + 2, int(src.height * bg_scale) + 2
    bg = src.resize((bw, bh), Image.LANCZOS)
    left, top = (bw - tw) // 2, (bh - th) // 2
    bg = bg.crop((left, top, left + tw, top + th)).filter(
        ImageFilter.GaussianBlur(24))
    # 压暗一点，突出前景
    dark = Image.new("RGB", (tw, th), (0, 0, 0))
    bg = Image.blend(bg, dark, 0.25)
    bg.paste(fg, ((tw - nw) // 2, (th - nh) // 2))
    return bg


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_variants(src_path: Path, ratio: str, out_dir: Path) -> list[dict]:
    """生成 crop_<ratio> 与 pad_<ratio> 两个 JPEG 变体。返回写入信息列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        variants = {
            f"crop_{ratio.replace(':', 'x')}": center_crop(im, ratio),
            f"pad_{ratio.replace(':', 'x')}": blur_pad(im, ratio),
        }
        results = []
        for variant, image in variants.items():
            name = f"{uuid.uuid4().hex[:12]}_{variant}.jpg"
            path = out_dir / name
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
            path.write_bytes(data)
            results.append({
                "variant": variant, "path": str(path),
                "width": image.width, "height": image.height,
                "mime": "image/jpeg", "bytes": len(data),
                "sha256": sha256_bytes(data),
            })
        return results
