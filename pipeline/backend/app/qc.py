"""技术质检：用 ffmpeg 自带滤镜拦住明显坏片。

一次 ffmpeg 解码同时跑三个检测（只读，不出文件）：
- blackdetect   黑场（开头/结尾/中间整段发黑）
- freezedetect  冻帧（画面基本不动，像一张图）
- silencedetect 静音（只有脚本要求画内口播时才作为问题）

再结合 ffprobe 结果检查：时长是否明显短于请求、分辨率/画幅是否与预期一致、有没有视频流。
返回 blocker / warning 两级问题：blocker 会让这一条 take 判不通过并触发分镜级重试。
阈值见 constants.QC_*，是可调整的初始参数。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import constants as C

_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
_FREEZE_START_RE = re.compile(r"freezedetect\.freeze_start:\s*([\d.]+)")
_FREEZE_END_RE = re.compile(r"freezedetect\.freeze_end:\s*([\d.]+)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")

_RATIO_VALUE = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "3:4": 3 / 4,
                "4:3": 4 / 3, "21:9": 21 / 9}


@dataclass
class QcIssue:
    severity: str          # blocker | warning
    code: str
    message: str


@dataclass
class TechReport:
    passed: bool
    duration: float
    issues: list[QcIssue] = field(default_factory=list)
    black_segments: list[list[float]] = field(default_factory=list)
    freeze_segments: list[list[float]] = field(default_factory=list)
    silence_segments: list[list[float]] = field(default_factory=list)
    black_ratio: float = 0.0
    freeze_ratio: float = 0.0
    silence_ratio: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = [asdict(i) for i in self.issues]
        return d


def detect_segments(path: str | Path, *, has_audio: bool, timeout: float = 300) -> dict:
    """跑一遍 ffmpeg，解析黑场/冻帧/静音区间。没有音轨时不跑 silencedetect。"""
    vf = (f"blackdetect=d={C.QC_BLACK_MIN_SECONDS}:pix_th={C.QC_BLACK_PIX_TH},"
          f"freezedetect=n={C.QC_FREEZE_NOISE_DB}dB:d={C.QC_FREEZE_MIN_SECONDS}")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-map", "0:v:0", "-vf", vf]
    if has_audio:
        af = f"silencedetect=noise={C.QC_SILENCE_NOISE_DB}dB:d={C.QC_SILENCE_MIN_SECONDS}"
        cmd += ["-map", "0:a:0", "-af", af]
    cmd += ["-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"质检解码失败：{proc.stderr.strip()[-300:]}")
    return parse_detect_log(proc.stderr)


def parse_detect_log(log: str) -> dict:
    """从 ffmpeg stderr 解析区间。区间到文件结尾没有 end 时，end 记为 None（调用方补齐）。"""
    black = [[float(a), float(b)] for a, b, _ in _BLACK_RE.findall(log)]

    freeze: list[list[float | None]] = []
    for line in log.splitlines():
        m = _FREEZE_START_RE.search(line)
        if m:
            freeze.append([float(m.group(1)), None])
            continue
        m = _FREEZE_END_RE.search(line)
        if m and freeze and freeze[-1][1] is None:
            freeze[-1][1] = float(m.group(1))

    silence: list[list[float | None]] = []
    for line in log.splitlines():
        m = _SIL_START_RE.search(line)
        if m:
            silence.append([max(0.0, float(m.group(1))), None])
        m = _SIL_END_RE.search(line)
        if m and silence and silence[-1][1] is None:
            silence[-1][1] = float(m.group(1))
    return {"black": black, "freeze": freeze, "silence": silence}


def _close(segments: list[list[float | None]], duration: float) -> list[list[float]]:
    out = []
    for start, end in segments:
        end = duration if end is None else min(end, duration)
        if end > start:
            out.append([round(start, 3), round(end, 3)])
    return out


def _total(segments: list[list[float]]) -> float:
    return sum(e - s for s, e in segments)


def evaluate(*, probe: dict, segments: dict, expected_duration: float | None,
             expected_ratio: str | None, expect_voice: bool) -> TechReport:
    """纯函数：根据 ffprobe 与检测区间给出结论。便于单测。"""
    duration = float(probe.get("duration") or 0.0)
    issues: list[QcIssue] = []

    if not probe.get("width") or not probe.get("height"):
        issues.append(QcIssue("blocker", "no_video", "成片没有可用的视频流"))
    if duration <= 0:
        issues.append(QcIssue("blocker", "zero_duration", "成片时长为 0"))
        return TechReport(passed=False, duration=duration, issues=issues)

    # 时长
    if expected_duration:
        if duration < expected_duration - C.QC_DURATION_SHORT_TOLERANCE:
            issues.append(QcIssue("blocker", "too_short",
                                  f"成片 {duration:.2f}s，明显短于请求的 {expected_duration:.0f}s"))
        elif duration > expected_duration + C.QC_DURATION_LONG_TOLERANCE:
            issues.append(QcIssue("warning", "too_long",
                                  f"成片 {duration:.2f}s，比请求的 {expected_duration:.0f}s 长，合成时会裁掉多余部分"))

    # 画幅
    if expected_ratio in _RATIO_VALUE and probe.get("width") and probe.get("height"):
        actual = probe["width"] / probe["height"]
        want = _RATIO_VALUE[expected_ratio]
        if abs(actual - want) / want > C.QC_RATIO_TOLERANCE:
            issues.append(QcIssue("warning", "ratio_mismatch",
                                  f"成片画幅 {probe['width']}x{probe['height']} 与项目 {expected_ratio} 不一致，"
                                  "合成时会加黑边适配"))

    black = _close(segments.get("black", []), duration)
    freeze = _close(segments.get("freeze", []), duration)
    silence = _close(segments.get("silence", []), duration)
    black_ratio = _total(black) / duration
    freeze_ratio = _total(freeze) / duration

    long_black = [seg for seg in black if seg[1] - seg[0] >= C.QC_BLACK_BLOCK_SECONDS]
    if long_black or black_ratio >= C.QC_BLACK_BLOCK_RATIO:
        where = "、".join(f"{s:.1f}-{e:.1f}s" for s, e in black[:3])
        issues.append(QcIssue("blocker", "black_frames",
                              f"检测到黑场 {where}（合计占 {black_ratio:.0%}）"))
    elif black:
        issues.append(QcIssue("warning", "short_black",
                              f"有短暂黑场（合计 {_total(black):.1f}s），建议人工看一眼"))

    if freeze_ratio >= C.QC_FREEZE_BLOCK_RATIO:
        issues.append(QcIssue("blocker", "frozen",
                              f"画面 {freeze_ratio:.0%} 的时间几乎不动，像一张静态图"))
    elif freeze:
        issues.append(QcIssue("warning", "partly_frozen",
                              f"有 {_total(freeze):.1f}s 画面几乎不动"))

    silence_ratio: float | None = None
    if probe.get("has_audio"):
        silence_ratio = _total(silence) / duration
        if expect_voice and silence_ratio >= C.QC_VOICE_SILENCE_BLOCK_RATIO:
            issues.append(QcIssue("blocker", "voice_missing",
                                  f"脚本要求画内口播，但 {silence_ratio:.0%} 的时间是静音"))
    elif expect_voice:
        issues.append(QcIssue("blocker", "voice_missing", "脚本要求画内口播，但成片没有音轨"))

    passed = not any(i.severity == "blocker" for i in issues)
    return TechReport(passed=passed, duration=round(duration, 3), issues=issues,
                      black_segments=black, freeze_segments=freeze,
                      silence_segments=silence, black_ratio=round(black_ratio, 4),
                      freeze_ratio=round(freeze_ratio, 4),
                      silence_ratio=None if silence_ratio is None else round(silence_ratio, 4))


def run_tech_check(path: str | Path, probe: dict, *, expected_duration: float | None,
                   expected_ratio: str | None, expect_voice: bool) -> TechReport:
    segments = detect_segments(path, has_audio=bool(probe.get("has_audio")))
    return evaluate(probe=probe, segments=segments, expected_duration=expected_duration,
                    expected_ratio=expected_ratio, expect_voice=expect_voice)
