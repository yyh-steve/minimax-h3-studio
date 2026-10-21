"""Small synthetic FFmpeg fixtures generated inside pytest-owned directories."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def make_clip(directory: Path, name: str, *, video: str, duration: int,
              audio: str | None = None) -> Path:
    """Render a finite lavfi clip; missing media tools are test setup errors."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError("Media tests require FFmpeg and ffprobe on PATH; missing: "
                           + ", ".join(missing))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", video]
    if audio is not None:
        command += ["-f", "lavfi", "-i", audio]
    command += ["-map", "0:v:0"]
    if audio is not None:
        command += ["-map", "1:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        command += ["-an"]
    command += ["-t", str(duration), "-r", "24", "-c:v", "libx264",
                "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-threads", "1", "-movflags", "+faststart", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"FFmpeg could not create synthetic fixture {name}: "
                           + result.stderr.strip()[-2000:])
    return path
