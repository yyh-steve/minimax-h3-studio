"""Resample already-decoded CPU audio through a file-free FFmpeg PCM pipe."""
from __future__ import annotations

import os
import subprocess


def resample_reference_audio(reference, *, ffmpeg="ffmpeg"):
    """Convert a typed reference to real 32 kHz samples, committing only on success.

    The reference carries a float32 CPU tensor shaped (channels, samples). This
    function preserves mono/stereo and returns an auditable conversion record.
    Native 32 kHz input keeps the exact tensor object and never invokes FFmpeg.
    """
    import numpy as np
    import torch

    waveform, source_rate = reference.audio, reference.sample_rate
    if not isinstance(source_rate, int) or isinstance(source_rate, bool) or source_rate <= 0:
        raise ValueError("A decoded reference must declare its real positive integer sample rate")
    if (not isinstance(waveform, torch.Tensor) or waveform.device.type != "cpu" or
            waveform.dtype != torch.float32 or waveform.ndim != 2 or
            waveform.shape[0] not in (1, 2) or waveform.shape[1] == 0):
        raise ValueError("Reference audio must be a nonempty float32 CPU (1 or 2 channels, samples) tensor")
    if not torch.isfinite(waveform).all().item():
        raise ValueError("Reference audio contains nonfinite samples")
    channels, source_samples = waveform.shape
    target_rate = 32000
    record = {
        "source_rate": source_rate, "target_rate": target_rate,
        "source_samples": source_samples, "target_samples": source_samples,
        "channels": channels, "source_duration_seconds": source_samples / source_rate,
        "target_duration_seconds": source_samples / source_rate,
        "backend": "passthrough", "backend_log": "32 kHz input retained byte-for-byte; FFmpeg not invoked.",
    }
    if source_rate == target_rate:
        return record

    # Interleave channels as raw little-endian float32. FFmpeg sees no container,
    # filename or URL: its only permitted media protocol is the two PCM pipes.
    pcm = waveform.detach().numpy().T.astype("<f4", copy=False).tobytes(order="C")
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
        "-protocol_whitelist", "pipe", "-f", "f32le", "-ar", str(source_rate),
        "-ac", str(channels), "-i", "pipe:0", "-map", "0:a:0",
        "-filter_threads", "1", "-threads", "1", "-af", "aresample=32000",
        "-ar", str(target_rate), "-ac", str(channels), "-c:a", "pcm_f32le",
        "-f", "f32le", "pipe:1",
    ]
    environment = os.environ.copy()
    environment.pop("FFREPORT", None)
    try:
        result = subprocess.run(command, input=pcm, capture_output=True, timeout=30, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Reference audio resampling failed; original rate remains {source_rate}: {error}") from error
    backend_log = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"FFmpeg audio resampling exited {result.returncode}; original rate remains "
                           f"{source_rate}: {backend_log}")
    if not result.stdout or len(result.stdout) % (4 * channels):
        raise RuntimeError("FFmpeg returned empty or incomplete float32 PCM; reference left unchanged")
    converted = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, channels).T.copy()
    target_samples = converted.shape[1]
    expected_samples = round(source_samples * target_rate / source_rate)
    if abs(target_samples - expected_samples) > 1 or not np.isfinite(converted).all():
        raise RuntimeError("FFmpeg returned invalid sample count or nonfinite PCM; reference left unchanged")
    converted_tensor = torch.from_numpy(converted)
    record.update(
        target_samples=target_samples, target_duration_seconds=target_samples / target_rate,
        backend="ffmpeg/libswresample", backend_log=backend_log, command=command,
    )
    # Do not relabel the original waveform, including on process/validation failure.
    reference.audio, reference.sample_rate = converted_tensor, target_rate
    return record
