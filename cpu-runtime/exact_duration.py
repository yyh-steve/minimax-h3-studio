"""Explicit runner extensions for real-prefix clips of exactly 4 to 15 seconds.

No installed Diffusers source or checkpoint is modified. Model imports are kept
in the runner so duration planning and validation remain lightweight/offline.
"""
from __future__ import annotations

from fractions import Fraction


def make_frame_plan(frames=None, *, exact_15_seconds=False, duration_seconds=None):
    if sum((frames is not None, bool(exact_15_seconds), duration_seconds is not None)) > 1:
        raise ValueError("--duration-seconds, --exact-15-seconds and --frames are mutually exclusive")
    if duration_seconds is not None and (type(duration_seconds) is not int or not 4 <= duration_seconds <= 15):
        raise ValueError("duration_seconds must be an integer from 4 to 15")
    requested_seconds = 15 if exact_15_seconds else duration_seconds
    if requested_seconds is not None:
        # The native model minimum remains five seconds; a four-second delivery
        # uses an actual prefix of the minimum 124-frame generation.
        native_frames = max(124, 5 + 17 * ((24 * requested_seconds - 5 + 16) // 17))
    else:
        native_frames = 124 if frames is None else frames
    if (not isinstance(native_frames, int) or isinstance(native_frames, bool) or
            (requested_seconds is None and (native_frames < 124 or native_frames > 345 or (native_frames - 5) % 17))):
        raise ValueError("Native mode requires aligned frames 124..345; use --exact-15-seconds for 360 output frames")
    output_frames = requested_seconds * 24 if requested_seconds is not None else native_frames
    plan = {
        "exact_15_seconds": bool(exact_15_seconds),
        "native_generation_frames": native_frames,
        "native_generation_duration_seconds": native_frames / 24,
        "native_video_latent_frames": (native_frames - 5) // 17 * 5 + 2,
        "native_audio_latent_frames": round(native_frames / 24 * 40),
        "output_frames": output_frames,
        "output_duration_seconds": output_frames / 24,
        "duration_adaptation": None,
    }
    if duration_seconds is not None:
        plan["duration_seconds"] = duration_seconds
    if requested_seconds is not None:
        plan["duration_adaptation"] = {
            # The original mode name is consumed as an exact-15 marker by the
            # existing QA tool; generic deliveries need their own identifier.
            "mode": "generate_aligned_then_crop" if exact_15_seconds else "generate_aligned_then_crop_seconds",
            "implementation": ("local_runner_subclass_and_post_decode_crop_v1" if exact_15_seconds else
                               "local_runner_aligned_duration_and_post_decode_crop_v1"),
            "base_max_duration_seconds": 15.0,
            "effective_max_duration_seconds": max(15.0, native_frames / 24),
            "native_audio_samples_expected": plan["native_audio_latent_frames"] * 800,
            "output_audio_samples": requested_seconds * 32000,
            "installed_framework_modified": False,
            "checkpoint_modified": False,
        }
    return plan


def exact15_pipeline_class(base_pipeline_class):
    """Return a per-run subclass; the original pipeline class stays unchanged."""
    class H3Exact15SecondsPipeline(base_pipeline_class):
        @property
        def max_duration(self):
            if super().max_duration != 15.0:
                raise RuntimeError("The exact-15 extension requires the pinned 15-second base duration contract")
            return 362 / 24
    return H3Exact15SecondsPipeline


def prepare_output_media(video, audio, sampling_rate, plan):
    """Validate decoded shapes, then crop real prefixes without resampling/padding."""
    native_frames = plan["native_generation_frames"]
    if len(video) != native_frames:
        raise ValueError(f"Expected {native_frames} genuinely decoded video frames, got {len(video)}")
    if sampling_rate != 32000 or len(audio.shape) != 2 or audio.shape[0] != 2:
        raise ValueError("Decoded audio must be a stereo (2, samples) waveform at 32000 Hz")
    native_samples = int(audio.shape[-1])
    audit = {
        "applied": False, "native_video_frames_observed": len(video),
        "native_audio_samples_observed": native_samples,
        "output_video_frames": len(video), "output_audio_samples": native_samples,
        "discarded_video_frames": 0, "discarded_audio_samples": 0,
        "resampled_or_time_stretched": False,
    }
    adaptation = plan["duration_adaptation"]
    if adaptation is None:
        return video, audio, audit
    if native_samples != adaptation["native_audio_samples_expected"]:
        raise ValueError(f"Expected {adaptation['native_audio_samples_expected']} genuinely decoded audio samples, "
                         f"got {native_samples}; no padding is allowed")
    if plan["output_frames"] > native_frames or adaptation["output_audio_samples"] > native_samples:
        raise ValueError("Decoded media is shorter than the requested delivery; no padding is allowed")
    output_video = video[:plan["output_frames"]]
    output_audio = audio[..., :adaptation["output_audio_samples"]]
    audit.update(
        applied=(native_frames != len(output_video) or native_samples != int(output_audio.shape[-1])),
        output_video_frames=len(output_video), output_audio_samples=int(output_audio.shape[-1]),
        discarded_video_frames=native_frames - len(output_video),
        discarded_audio_samples=native_samples - int(output_audio.shape[-1]),
    )
    return output_video, output_audio, audit


def validate_exact15_media(metadata):
    """Preserve the original exact-15 validator entry point."""
    return validate_duration_media(metadata, 15)


def validate_duration_media(metadata, duration_seconds):
    """Require exact stream/container presentation time, ignoring hidden AAC pad."""
    if type(duration_seconds) is not int or not 4 <= duration_seconds <= 15:
        raise ValueError("duration_seconds must be an integer from 4 to 15")
    output_frames = duration_seconds * 24
    video = next((s for s in metadata["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in metadata["streams"] if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise ValueError("Exact-duration output requires both video and audio streams")
    if int(video.get("nb_read_frames", 0)) != output_frames or Fraction(video["avg_frame_rate"]) != 24:
        raise ValueError(f"Exact-duration output must decode to exactly {output_frames} frames at 24 fps")
    if int(audio["sample_rate"]) != 32000 or int(audio["channels"]) != 2:
        raise ValueError("Exact-duration output must carry 32000 Hz stereo audio")
    for stream in (video, audio):
        if stream.get("duration_ts") is not None and stream.get("time_base"):
            duration = Fraction(stream["duration_ts"]) * Fraction(stream["time_base"])
        else:
            duration = Fraction(stream["duration"])
        if duration != duration_seconds:
            raise ValueError(f"{stream['codec_type']} presentation duration must be {duration_seconds}.000 seconds, "
                             f"got {float(duration)}")
    if Fraction(metadata["format"]["duration"]) != duration_seconds:
        raise ValueError(f"Container duration must be {duration_seconds}.000 seconds")
