#!/usr/bin/env python3
"""Inspect a local H3 result without importing torch or connecting to a model host."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime, timezone
from fractions import Fraction

from PIL import Image, ImageDraw, ImageFont


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, timeout=300):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else str(value)
    except (TypeError, ValueError):
        return None


def last_metric(text, expression):
    values = re.findall(expression, text, flags=re.MULTILINE)
    return number(values[-1]) if values else None


def contact_sheet(video, output, frame_count, fps):
    indices = sorted({round(index * (frame_count - 1) / 11) for index in range(12)})
    expression = "+".join(f"eq(n\\,{index})" for index in indices)
    frames_dir = output / "frames"
    frames_dir.mkdir()
    result = run([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(video),
        "-map", "0:v:0", "-vf", f"select={expression}", "-fps_mode", "vfr",
        "-frames:v", str(len(indices)), str(frames_dir / "frame-%02d.png"),
    ])
    (output / "frame-extraction.log").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError("Frame extraction failed; see frame-extraction.log")
    files = sorted(frames_dir.glob("frame-*.png"))
    if len(files) != len(indices):
        raise RuntimeError(f"Expected {len(indices)} sampled frames, extracted {len(files)}")
    cell_width, cell_height, label_height, gap = 320, 240, 28, 12
    rows = math.ceil(len(files) / 4)
    sheet = Image.new("RGB", (4 * cell_width + 5 * gap,
                               rows * (cell_height + label_height) + (rows + 1) * gap), "#15191f")
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default(size=16)
    samples = []
    for ordinal, (index, path) in enumerate(zip(indices, files)):
        x = gap + (ordinal % 4) * (cell_width + gap)
        y = gap + (ordinal // 4) * (cell_height + label_height + gap)
        with Image.open(path) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((cell_width, cell_height), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x + (cell_width - thumb.width) // 2,
                            y + (cell_height - thumb.height) // 2))
        timestamp = index / fps
        draw.text((x + 4, y + cell_height + 5), f"Frame {index:03d}  |  {timestamp:.3f} s",
                  font=font, fill="#eef0f3")
        samples.append({"frame_index": index, "timestamp_seconds": timestamp, "path": str(path)})
    target = output / "contact-sheet.png"
    sheet.save(target)
    return target, samples


def write_markdown(data, output):
    media = data.get("media", {})
    config = data.get("reported_run", {})
    audio = data.get("audio_analysis", {})
    checks = data.get("checks", {})
    lines = [
        "# Ref2VA 本地媒体验收", "",
        f"自动检查：**{'文件及媒体规格通过' if data.get('automatic_media_ok') else '存在失败或尚未完成的项目'}**。",
        "参考相似度、可辨认画质、运动自然度、声音语义和音画同步均须人工审核；本报告不会自动判定成片质量合格。", "",
        f"- 视频：`{data['inputs']['video']}`",
        f"- 推理报告：`{data['inputs']['run_report']}`",
        f"- 本地检查时间：{data['checked_at_utc']}", "",
        "## 媒体与记录", "",
        "| 项目 | 结果 |", "|---|---|",
        f"| 尺寸 / 帧率 / 实解帧数 | {media.get('width')} × {media.get('height')} / {media.get('fps')} fps / {media.get('decoded_frames')} |",
        f"| 视频 / 音频呈现 / 容器时长 | {media.get('video_duration_seconds')} / {media.get('audio_duration_seconds')} / {media.get('container_duration_seconds')} 秒 |",
        f"| 音频 | {media.get('sample_rate')} Hz，{media.get('channels')} 声道 |",
        f"| 报告原生生成 / 交付帧数 | {config.get('native_generation_frames')} / {config.get('output_frames')} |",
        f"| 精确 15 秒交付检查 | {config.get('exact_15_seconds')} |",
        f"| 报告工作流 / 实际 forward | {config.get('workflow')} / {config.get('observed_forwards')} |",
        f"| 报告 pipeline / 脚本总时长 | {config.get('pipeline_seconds')} / {config.get('total_seconds')} 秒 |",
        f"| 音轨平均 / 最大电平 | {audio.get('mean_volume_db')} / {audio.get('max_volume_db')} dB（volumedetect） |",
        f"| 综合响度 / 响度范围 / 真峰值 | {audio.get('integrated_loudness_lufs')} LUFS / {audio.get('loudness_range_lu')} LU / {audio.get('true_peak_dbfs')} dBFS |", "",
        "电平和响度只描述信号，不证明音轨符合提示词、听感自然或与画面同步。计时、CPU 设备和内存信息均引用输入报告，不是本地重新测得的模型性能。", "",
        "音频呈现时长取容器内音轨的时长。AAC 解码后的尾部填充允许保留在试听 WAV 中，但不能使精确交付的音轨或容器超过 15.000 秒。", "",
        "## 自动检查明细", "",
    ]
    for key, value in checks.items():
        lines.append(f"- `{key}`：{value}")
    lines += ["", "## 人工验收", "",
              "- 参考一致性：待审；按本次案例核对主体造型、材质、配色和标志性细节。",
              "- 可辨认画质：待审；查看主体轮廓、持续噪声、异常纹理、形变与细节稳定性。",
              "- 动态：待审；完整播放视频，核对动作连续性、闪烁、静止或突然跳变。",
              "- 声音与同步：待审；试听完整音轨，核对自然度、内容、响度和事件同步。",
              "- 最终可用性：待审；完整媒体通过不代表质量通过。", ""]
    if data.get("artifacts", {}).get("contact_sheet"):
        lines += ["![12 个均匀抽样时点](contact-sheet.png)", "",
                  "抽样静帧不能替代完整动态播放。", ""]
    if data.get("artifacts", {}).get("audio_preview"):
        lines += ["[试听音轨（PCM WAV）](audio-preview.wav)", ""]
    if data.get("reference"):
        lines += [f"参考图：`{data['reference']['path']}`", ""]
    if data.get("error"):
        lines += ["## 检查错误", "", data["error"], ""]
    (output / "qa.md").write_text("\n".join(lines))


def inspect(args, data, output):
    video, report_path = Path(args.video).resolve(), Path(args.report).resolve()
    report = json.loads(report_path.read_text())
    requested = report.get("arguments", {})
    expected_frames = report.get("output_frames", requested.get("frames"))
    native_frames = report.get("native_generation_frames", requested.get("frames"))
    adaptation = report.get("duration_adaptation") or {}
    exact_15_seconds = (report.get("exact_15_seconds") is True or
                        requested.get("exact_15_seconds") is True or
                        adaptation.get("mode") == "generate_aligned_then_crop")
    workflow = report.get("workflow") or ("ref2va" if requested.get("references_json") else
                                           "fl2va" if requested.get("first_frame") else "t2va")
    data["inputs"].update(video_sha256=sha256(video), report_sha256=sha256(report_path))
    data["reported_run"] = {
        "status": report.get("status"), "workflow": workflow,
        "requested_forwards": requested.get("steps"),
        "observed_forwards": report.get("observed_denoiser_forwards"),
        "seed": requested.get("seed"), "width": requested.get("width"), "height": requested.get("height"),
        "frames": requested.get("frames"), "native_generation_frames": native_frames,
        "output_frames": expected_frames, "exact_15_seconds": exact_15_seconds,
        "output_duration_seconds": report.get("output_duration_seconds"),
        "duration_adaptation": adaptation, "references": report.get("references", []),
        "reference_objects": report.get("reference_objects", []),
        "pipeline_seconds": report.get("pipeline_seconds"), "total_seconds": report.get("total_seconds"),
        "component_audits": report.get("component_audits", {}),
        "memory_final": report.get("memory_final"),
        "provenance_note": "These are supplied report claims, not model execution observed by this local QA tool.",
    }
    probe = run(["ffprobe", "-v", "error", "-count_frames", "-show_format", "-show_streams",
                 "-of", "json", str(video)])
    (output / "ffprobe.log").write_text(probe.stderr)
    if probe.returncode:
        raise RuntimeError("ffprobe failed; see ffprobe.log")
    metadata = json.loads(probe.stdout)
    (output / "ffprobe.json").write_text(json.dumps(metadata, indent=2))
    streams = metadata.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    decoded = run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(video),
                   "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"])
    (output / "full-decode.log").write_text(decoded.stderr)
    checks = data["checks"]
    checks.update(video_stream_present=bool(v), audio_stream_present=bool(a),
                  full_decode_ok=decoded.returncode == 0)
    if not v or not a:
        raise RuntimeError("H3 output requires both video and audio streams")
    fps = float(Fraction(v.get("avg_frame_rate", "0/1")))
    frames = int(v.get("nb_read_frames", 0))
    duration = number(metadata.get("format", {}).get("duration"))
    data["media"] = {
        "width": v.get("width"), "height": v.get("height"), "fps": fps, "decoded_frames": frames,
        "video_duration_seconds": number(v.get("duration")),
        "audio_duration_seconds": number(a.get("duration")), "container_duration_seconds": duration,
        "sample_rate": int(a.get("sample_rate", 0)), "channels": a.get("channels"),
        "video_codec": v.get("codec_name"), "audio_codec": a.get("codec_name"),
    }
    checks.update(
        fps_24=fps == 24,
        audio_32khz_stereo=int(a.get("sample_rate", 0)) == 32000 and a.get("channels") == 2,
        frame_count_matches_report=expected_frames is not None and frames == expected_frames,
        geometry_matches_report=(v.get("width"), v.get("height")) ==
                                (requested.get("width"), requested.get("height")),
        duration_matches_report=(duration == 15.0 if exact_15_seconds else
                                 bool(expected_frames and isinstance(duration, (int, float)) and
                                      abs(duration - expected_frames / 24) <= 0.25)),
    )
    if exact_15_seconds:
        checks.update(
            exact_native_generation_frames_362=native_frames == 362,
            exact_output_frames_360=expected_frames == frames == 360,
            exact_video_presentation_15_seconds=data["media"]["video_duration_seconds"] == 15.0,
            exact_audio_presentation_15_seconds=data["media"]["audio_duration_seconds"] == 15.0,
            exact_container_15_seconds=duration == 15.0,
        )
    if args.reference:
        reference = Path(args.reference).resolve()
        with Image.open(reference) as image:
            size = list(image.size)
        digest = sha256(reference)
        data["reference"] = {"path": str(reference), "size": size, "sha256": digest}
        checks["reference_sha256_present_in_run_report"] = any(
            item.get("sha256") == digest for item in report.get("references", [])
        )
    data["automatic_media_ok"] = all(checks.values())
    checks["observed_forwards_match_report_request"] = (
        report.get("observed_denoiser_forwards") is not None and
        report.get("observed_denoiser_forwards") == requested.get("steps")
    )
    checks["reported_ref2va_with_references"] = workflow == "ref2va" and bool(report.get("references"))
    checks["base_quality_sampling_candidate_only"] = bool(
        data["automatic_media_ok"] and checks["reported_ref2va_with_references"] and
        checks["observed_forwards_match_report_request"] and
        report.get("observed_denoiser_forwards", 0) >= 49 and
        min(v.get("width", 0), v.get("height", 0)) >= 768
    )
    if fps > 0 and frames > 0:
        sheet, samples = contact_sheet(video, output, frames, fps)
        data["artifacts"]["contact_sheet"] = str(sheet)
        data["sampled_frames"] = samples
    sound = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-map", "0:a:0",
                 "-af", "ebur128=peak=true,volumedetect", "-f", "null", "-"])
    (output / "audio-analysis.log").write_text(sound.stderr)
    if sound.returncode:
        raise RuntimeError("Audio analysis failed; see audio-analysis.log")
    data["audio_analysis"] = {
        "mean_volume_db": last_metric(sound.stderr, r"mean_volume:\s+([-\w.]+) dB"),
        "max_volume_db": last_metric(sound.stderr, r"max_volume:\s+([-\w.]+) dB"),
        "integrated_loudness_lufs": last_metric(sound.stderr, r"^\s+I:\s+([-\w.]+) LUFS"),
        "loudness_range_lu": last_metric(sound.stderr, r"^\s+LRA:\s+([-\w.]+) LU"),
        "true_peak_dbfs": last_metric(sound.stderr, r"^\s+Peak:\s+([-\w.]+) dBFS"),
        "measurement_note": "EBU R128 is measured before volumedetect converts to signed 16-bit samples; neither measurement implies semantic quality.",
    }
    preview = output / "audio-preview.wav"
    extracted = run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(video),
                     "-map", "0:a:0", "-c:a", "pcm_s16le", str(preview)])
    if extracted.returncode:
        raise RuntimeError("Audio preview extraction failed: " + extracted.stderr)
    data["artifacts"]["audio_preview"] = str(preview)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="Existing local MP4")
    parser.add_argument("report", help="Existing inference report JSON")
    parser.add_argument("--reference", default=str(Path(__file__).with_name("astronaut.jpg")))
    parser.add_argument("--output-dir", required=True, help="A NEW directory; existing paths are never overwritten")
    args = parser.parse_args()
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            parser.error(f"{executable} is required")
    for name in ("video", "report"):
        if not Path(getattr(args, name)).is_file():
            parser.error(f"Missing local {name}")
    output = Path(args.output_dir).resolve()
    if output.exists():
        parser.error("Output directory already exists; choose a new path")
    output.mkdir(parents=True)
    data = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {"video": str(Path(args.video).resolve()), "run_report": str(Path(args.report).resolve())},
        "checks": {}, "artifacts": {}, "automatic_media_ok": False,
        "human_review": {key: "pending_human_review" for key in
                         ("reference_adherence", "visual_quality", "motion", "audio_semantics_and_sync", "usable_result")},
    }
    try:
        inspect(args, data, output)
    except Exception as error:
        data["error"] = f"{type(error).__name__}: {error}"
    (output / "qa.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    write_markdown(data, output)
    print(json.dumps({"automatic_media_ok": data["automatic_media_ok"],
                      "human_review": "pending", "qa_json": str(output / "qa.json"),
                      "qa_markdown": str(output / "qa.md"), "error": data.get("error")}, ensure_ascii=False))
    return 0 if data["automatic_media_ok"] and not data.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
