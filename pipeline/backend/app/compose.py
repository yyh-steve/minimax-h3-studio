"""合成：把每个镜头当前选用的成片拼成一条完整短视频。

流程（全部用 ffmpeg 命令行 + Pillow，不依赖 libass/drawtext，Mac 自带的 ffmpeg 也能跑）：
1. 逐镜归一化成中间文件 seg_i.mkv（H.264 + PCM，时长精确到帧）
   - 缩放到项目画幅（短边 768、对齐 32），比例不同则补黑边，不拉伸
   - 24fps；成片比分镜短就用最后一帧补齐，长了就截掉
   - 原声：保留则重采样为 48k 立体声并补静音到精确时长；不保留或没有音轨则静音
   - 字幕：Pillow 渲染成透明 PNG，overlay 叠加（字幕后期叠加，不进生成画面）
2. 拼接：硬切（concat）或淡入淡出（xfade + acrossfade）
3. BGM：循环铺满、调音量、首尾淡入淡出，与原声混音（amix normalize=0 + alimiter 防爆音）
4. 输出 H.264 yuv420p + AAC，faststart，便于直接上传平台
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import constants as C

# 常见中文字体位置（macOS / Linux / Windows）；SUBTITLE_FONT 环境变量优先
_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


class ComposeError(RuntimeError):
    pass


def find_cjk_font() -> str | None:
    env = os.environ.get("SUBTITLE_FONT", "").strip()
    candidates = ([env] if env else []) + list(_FONT_CANDIDATES)
    for path in candidates:
        if path and Path(path).exists():
            try:
                ImageFont.truetype(path, 24)
                return path
            except Exception:
                continue
    return None


def expected_total(durations: list[float], transition: str,
                   fade: float = C.RENDER_FADE_SECONDS) -> float:
    total = float(sum(durations))
    if transition == "fade" and len(durations) > 1:
        total -= fade * (len(durations) - 1)
    return round(total, 3)


# ---------- 字幕 ----------

def _tokens(text: str) -> list[str]:
    """中日韩文字逐字断行，连续的拉丁字母/数字按词断行。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ord(ch) >= 0x2E80 or ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for tok in _tokens(text):
        trial = cur + tok
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur = trial
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def render_subtitle_png(text: str, width: int, height: int, out: str | Path,
                        font_path: str) -> Path:
    """整帧大小的透明 PNG：底部居中，最多两行，白字黑描边 + 半透明底。"""
    text = " ".join(text.split())
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    max_w = width * 0.86
    base = max(20, round(min(width, height) * 0.058))
    font, lines = None, []
    for scale in (1.0, 0.9, 0.8, 0.7):
        font = ImageFont.truetype(font_path, max(16, round(base * scale)))
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= 2:
            break
    if len(lines) > 2:
        last = lines[1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines = [lines[0], last + "…"]

    stroke = max(2, round(font.size * 0.07))
    line_h = font.size * 1.3
    block_h = line_h * len(lines)
    bottom = height * (0.88 if height >= width else 0.90)
    top = bottom - block_h
    boxes = []
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        x = (width - tw) / 2
        y = top + i * line_h
        boxes.append((x, y, line, draw.textbbox((x, y), line, font=font, stroke_width=stroke)))
    pad = round(font.size * 0.35)
    left = min(b[3][0] for b in boxes) - pad
    right = max(b[3][2] for b in boxes) + pad
    upper = min(b[3][1] for b in boxes) - pad
    lower = max(b[3][3] for b in boxes) + pad
    draw.rounded_rectangle((left, upper, right, lower), radius=pad, fill=(0, 0, 0, 115))
    for x, y, line, _ in boxes:
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    out = Path(out)
    img.save(out)
    return out


# ---------- ffmpeg ----------

def _run(cmd: list[str], timeout: float = 900) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ComposeError(f"ffmpeg 失败：{proc.stderr.strip()[-600:]}")


def normalize_segment(*, src: str, duration: float, out: str | Path, width: int, height: int,
                      has_audio: bool, keep_audio: bool, original_volume: float,
                      subtitle_png: str | None) -> Path:
    d = f"{duration:.3f}"
    fps = C.RENDER_FPS
    inputs = ["-i", src]
    idx = 1
    sub_idx = None
    if subtitle_png:
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", subtitle_png]
        sub_idx = idx
        idx += 1
    use_src_audio = has_audio and keep_audio
    if not use_src_audio:
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r={C.RENDER_AUDIO_RATE}:cl=stereo"]
        silence_idx = idx
        idx += 1

    vchain = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
              f"format=yuv420p,tpad=stop_mode=clone:stop_duration={d},trim=duration={d},"
              f"setpts=PTS-STARTPTS")
    if sub_idx is not None:
        graph = [vchain + "[v0]",
                 f"[v0][{sub_idx}:v]overlay=0:0:shortest=1,format=yuv420p[v]"]
    else:
        graph = [vchain + "[v]"]
    if use_src_audio:
        graph.append(f"[0:a]aresample={C.RENDER_AUDIO_RATE},"
                     f"aformat=sample_fmts=s16:channel_layouts=stereo,volume={original_volume:.3f},"
                     f"apad,atrim=duration={d},asetpts=PTS-STARTPTS[a]")
    else:
        graph.append(f"[{silence_idx}:a]atrim=duration={d},asetpts=PTS-STARTPTS[a]")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", d,
           "-c:v", "libx264", "-preset", C.RENDER_PRESET, "-crf", "16", "-r", str(fps),
           "-c:a", "pcm_s16le", str(out)]
    _run(cmd)
    return Path(out)


def join_segments(*, segments: list[Path], durations: list[float], transition: str,
                  out: str | Path, bgm_path: str | None, bgm_volume: float) -> float:
    n = len(segments)
    total = expected_total(durations, transition)
    fade = C.RENDER_FADE_SECONDS
    inputs: list[str] = []
    for seg in segments:
        inputs += ["-i", str(seg)]
    graph: list[str] = []

    if n == 1:
        vlab, alab = "0:v", "0:a"
    elif transition == "fade":
        prev, offset = "0:v", 0.0
        for k in range(1, n):
            offset += durations[k - 1] - fade
            graph.append(f"[{prev}][{k}:v]xfade=transition=fade:duration={fade}:"
                         f"offset={offset:.3f}[vx{k}]")
            prev = f"vx{k}"
        vlab = prev
        prev = "0:a"
        for k in range(1, n):
            graph.append(f"[{prev}][{k}:a]acrossfade=d={fade}:c1=tri:c2=tri[ax{k}]")
            prev = f"ax{k}"
        alab = prev
    else:
        pairs = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        graph.append(f"{pairs}concat=n={n}:v=1:a=1[vcat][acat]")
        vlab, alab = "vcat", "acat"

    fade_out_start = max(total - 0.3, 0)
    if bgm_path:
        inputs += ["-stream_loop", "-1", "-i", bgm_path]
        b = n
        graph.append(f"[{b}:a]aresample={C.RENDER_AUDIO_RATE},"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={bgm_volume:.3f},"
                     f"atrim=duration={total:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[bgm]")
        graph.append(f"[{alab}]aformat=sample_fmts=fltp:channel_layouts=stereo[orig]")
        graph.append("[orig][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                     f"alimiter=limit=0.95,apad,atrim=duration={total:.3f},"
                     f"afade=t=out:st={fade_out_start:.3f}:d=0.3[aout]")
    else:
        graph.append(f"[{alab}]apad,atrim=duration={total:.3f},"
                     f"afade=t=out:st={fade_out_start:.3f}:d=0.3[aout]")

    vmap = f"[{vlab}]" if ":" not in vlab else vlab
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", ";".join(graph), "-map", vmap, "-map", "[aout]",
           "-t", f"{total:.3f}",
           "-c:v", "libx264", "-preset", C.RENDER_PRESET, "-crf", str(C.RENDER_CRF),
           "-pix_fmt", "yuv420p", "-r", str(C.RENDER_FPS),
           "-c:a", "aac", "-b:a", "192k", "-ar", str(C.RENDER_AUDIO_RATE),
           "-movflags", "+faststart", str(out)]
    _run(cmd, timeout=1800)
    return total


def compose(*, items: list[dict], width: int, height: int, transition: str,
            subtitles: bool, keep_audio: bool, original_volume: float,
            bgm_path: str | None, bgm_volume: float,
            out_path: str | Path, work_dir: str | Path) -> dict:
    """items: [{shot_key, path, duration, subtitle, has_audio}]，按播放顺序。"""
    if not items:
        raise ComposeError("没有可合成的镜头")
    if transition not in ("cut", "fade"):
        raise ComposeError(f"不支持的转场：{transition}")
    work = Path(work_dir)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    font = find_cjk_font() if subtitles else None
    if subtitles and any(i.get("subtitle") for i in items) and font is None:
        warnings.append("没有找到中文字体，本次未叠加字幕（可设置 SUBTITLE_FONT 指向 .ttf/.ttc）")

    segments, durations = [], []
    for k, item in enumerate(items):
        sub_png = None
        if font and subtitles and (item.get("subtitle") or "").strip():
            sub_png = str(render_subtitle_png(item["subtitle"], width, height,
                                              work / f"sub_{k}.png", font))
        if keep_audio and not item.get("has_audio"):
            warnings.append(f"{item['shot_key']} 没有音轨，用静音补齐")
        seg = normalize_segment(src=item["path"], duration=float(item["duration"]),
                                out=work / f"seg_{k}.mkv", width=width, height=height,
                                has_audio=bool(item.get("has_audio")), keep_audio=keep_audio,
                                original_volume=original_volume, subtitle_png=sub_png)
        segments.append(seg)
        durations.append(float(item["duration"]))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    total = join_segments(segments=segments, durations=durations, transition=transition,
                          out=tmp, bgm_path=bgm_path, bgm_volume=bgm_volume)
    tmp.replace(out_path)
    shutil.rmtree(work, ignore_errors=True)
    return {"expected_duration": total, "warnings": warnings, "font": font}
