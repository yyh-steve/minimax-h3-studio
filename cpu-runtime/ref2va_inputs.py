"""Local-only, ordered reference input contract; imports no model libraries."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def resolve_workflow(first_frame=None, references_json=None):
    for value in (first_frame, references_json):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("An explicitly supplied reference/keyframe path cannot be empty")
    if first_frame and references_json:
        raise ValueError("Reference conditioning and first-frame conditioning are distinct workflows")
    return "ref2va" if references_json else ("fl2va" if first_frame else "t2va")


def components_for_workflow(workflow):
    if workflow not in {"t2va", "fl2va", "ref2va"}:
        raise ValueError("Unknown H3 workflow")
    return ("tokenizer", "processor", "scheduler", "audio_scheduler", "text_encoder",
            "transformer_ref" if workflow == "ref2va" else "transformer", "vae", "audio_vae")


def read_reference_spec(spec_path):
    spec_path = Path(spec_path).resolve()
    data = json.loads(spec_path.read_text())
    if not isinstance(data, list) or not 1 <= len(data) <= 12:
        raise ValueError("References must be a nonempty ordered list with at most 12 entries")
    counts = Counter()
    resolved = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"type", "path"}:
            raise ValueError("Each reference must contain exactly type and path")
        kind, name = item["type"], item["path"]
        if kind not in {"image", "video", "audio"}:
            raise ValueError("Reference type must be image, video or audio")
        if not isinstance(name, str) or not name or urlsplit(name).scheme or name.startswith("//"):
            raise ValueError("References must use local filesystem paths, not URLs")
        path = (spec_path.parent / name).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Missing or empty local reference: " + str(path))
        counts[kind] += 1
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        resolved.append({"type": kind, "path": str(path), "bytes": path.stat().st_size,
                         "sha256": digest.hexdigest()})
    if counts["image"] > 9 or counts["video"] > 3 or counts["audio"] > 3:
        raise ValueError("H3 supports at most 9 images, 3 videos, and 3 audio references")
    if not counts["image"] and not counts["video"]:
        raise ValueError("Audio references require at least one image or video reference")
    return resolved


def decode_references(spec):
    from diffusers.modular_pipelines.minimax_h3 import (
        MiniMaxH3AudioReference, MiniMaxH3ImageReference, MiniMaxH3VideoReference,
    )
    from PIL import Image

    decoded = []
    for item in spec:
        if item["type"] == "image":
            with Image.open(item["path"]) as source:
                ref = MiniMaxH3ImageReference(image=source.convert("RGB"))
        else:
            # Match the pinned Diffusers decoder, but enforce file-only protocols
            # on the decoder itself (including any nested playlist references).
            import av
            import numpy as np
            from diffusers.modular_pipelines.minimax_h3.references import _decode_reference_soundtrack
            with av.open(item["path"], options={"protocol_whitelist": "file"}) as container:
                if item["type"] == "video":
                    if not container.streams.video:
                        raise ValueError("Video reference has no video stream")
                    stream = container.streams.video[0]
                    frames, rotation = [], 0.0
                    for frame in container.decode(stream):
                        rotation = frame.rotation
                        frames.append(frame.to_ndarray(format="rgb24"))
                    if not frames:
                        raise ValueError("Video reference has no decoded frames")
                    fps = float(stream.average_rate or stream.guessed_rate)
                    frames = np.stack(frames)
                    turns = round(rotation / 90.0) % 4
                    if turns:
                        frames = np.ascontiguousarray(np.rot90(frames, k=-turns, axes=(1, 2)))
                    waveform, rate = None, None
                    if container.streams.audio:
                        container.seek(0)
                        waveform, rate = _decode_reference_soundtrack(av, container, container.streams.audio[0])
                    ref = MiniMaxH3VideoReference(frames=frames, fps=fps, audio=waveform, sample_rate=rate)
                else:
                    if not container.streams.audio:
                        raise ValueError("Audio reference has no audio stream")
                    waveform, rate = _decode_reference_soundtrack(av, container, container.streams.audio[0])
                    ref = MiniMaxH3AudioReference(audio=waveform, sample_rate=rate)
                if ref.has_audio:
                    from ref_audio_resample import resample_reference_audio
                    ref.audio_resample_metadata = resample_reference_audio(ref)
                    print(json.dumps({"event": "reference_audio_resample", "reference_path": item["path"],
                                      **ref.audio_resample_metadata}), flush=True)
        decoded.append(ref)
    return decoded
