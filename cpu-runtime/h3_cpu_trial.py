#!/usr/bin/env python3
"""Explicitly staged H3 CPU experiment. No arguments and --help do no heavy work."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from fractions import Fraction
import exact_duration
from ref2va_inputs import components_for_workflow, decode_references, read_reference_spec, resolve_workflow

MODEL_ID = "MiniMaxAI/MiniMax-H3"
MODEL_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
DIFFUSERS_COMMIT = "b800f828f3951c4d206e154411bcbfa693cd322e"
TRANSFORMERS_COMMIT = "d4804f9d76b8c071b993d813e9e87067b780cbee"
DEFAULT_PROMPT = "A red fox walks slowly through a snowy forest. Quiet footsteps and a gentle breeze."
MAX_PROMPT_CHARACTERS = 7000
GIB = 1024**3
ROOTS = (
    "transformer", "text_encoder", "tokenizer", "processor", "vae",
    "audio_vae", "scheduler", "audio_scheduler",
)
SMALL_FILES = {"modular_model_index.json", "model_index.json", "LICENSE", "README.md"}
COMPONENTS = (
    "tokenizer", "processor", "scheduler", "audio_scheduler",
    "text_encoder", "transformer", "vae", "audio_vae",
)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def require_linux():
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Heavy stages require Linux x86_64. Refusing this host, including the local Mac.")


def versions():
    result = {}
    for name in ("torch", "torchvision", "diffusers", "transformers", "accelerate",
                 "huggingface-hub", "safetensors", "av", "pillow", "numpy"):
        try:
            dist = importlib.metadata.distribution(name)
            item = {"version": dist.version}
            direct = dist.read_text("direct_url.json")
            if direct:
                # Record only the commit, never a package URL that might carry credentials.
                item["commit"] = json.loads(direct).get("vcs_info", {}).get("commit_id")
                if item["commit"]:
                    item["commit_source"] = "pep610_vcs"
            if name in {"diffusers", "transformers"} and not item.get("commit"):
                try:
                    from source_provenance import verified_install
                    item.update(verified_install(name, dist))
                except Exception as exc:
                    item["source_verification_error"] = str(exc)
            result[name] = item
        except importlib.metadata.PackageNotFoundError:
            result[name] = {"missing": True}
    return result


def system_info():
    info = {"system": platform.system(), "machine": platform.machine(),
            "python": sys.version, "logical_cpus": os.cpu_count(), "packages": versions()}
    if hasattr(os, "sched_getaffinity"):
        info["affinity_cpus"] = len(os.sched_getaffinity(0))
    if Path("/proc/meminfo").exists():
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                mem[key] = int(value.split()[0]) * 1024
        available = mem.get("MemAvailable", 0)
        # v2 cgroup accounting matters inside a container.
        limit_file = Path("/sys/fs/cgroup/memory.max")
        usage_file = Path("/sys/fs/cgroup/memory.current")
        if limit_file.exists() and usage_file.exists():
            limit = limit_file.read_text().strip()
            if limit.isdigit():
                available = min(available, max(0, int(limit) - int(usage_file.read_text())))
                mem["cgroup_limit"] = int(limit)
        info["memory_bytes"] = mem
        info["available_memory_gib"] = round(available / GIB, 2)
    if Path("/proc/cpuinfo").exists():
        cpuinfo = Path("/proc/cpuinfo").read_text()
        flags = next((line.split(":", 1)[1].split() for line in cpuinfo.splitlines()
                      if line.startswith("flags")), [])
        info["cpu_features"] = {flag: flag in flags for flag in
                                ("amx_tile", "amx_bf16", "amx_int8", "avx512_bf16")}
    info["tools"] = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    return info


def configure_cpu(threads):
    require_linux()
    if threads < 1:
        raise ValueError("--threads must be positive")
    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    if available and threads > available:
        raise ValueError(f"--threads={threads} exceeds available CPU affinity={available}")
    # Set before importing torch. No accelerator may be selected implicitly.
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HIP_VISIBLE_DEVICES"] = ""
    os.environ["OMP_NUM_THREADS"] = str(threads)
    os.environ["MKL_NUM_THREADS"] = str(threads)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    torch.set_default_device("cpu")
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    return torch


def check(args):
    torch = configure_cpu(args.threads)
    from diffusers import ModularPipeline, MiniMaxH3Transformer3DModel  # noqa: F401
    from transformers import Qwen3VLForConditionalGeneration  # noqa: F401
    from diffusers.utils.export_utils import encode_video  # noqa: F401
    from diffusers.models.attention_dispatch import attention_backend  # noqa: F401
    report = system_info()
    report["torch_cpu"] = {"threads": torch.get_num_threads(),
                           "mkldnn_available": torch.backends.mkldnn.is_available(),
                           "build": torch.__config__.show()}
    # Tiny compatibility checks only. These are NOT H3 throughput benchmarks.
    tests = {}
    for name, dtype in (("float32", torch.float32), ("bfloat16", torch.bfloat16)):
        started = time.perf_counter()
        try:
            with torch.inference_mode():
                x = torch.randn(128, 128, device="cpu", dtype=dtype)
                y = x @ x.T
                q = torch.randn(1, 2, 32, 64, device="cpu", dtype=dtype)
                attn = torch.nn.functional.scaled_dot_product_attention(q, q, q)
                finite = bool(torch.isfinite(y).all() and torch.isfinite(attn).all())
            tests[name] = {"ok": finite, "device": str(y.device),
                           "seconds": time.perf_counter() - started}
        except Exception as exc:
            tests[name] = {"ok": False, "error": str(exc)}
    report["small_operator_checks"] = tests
    report["amx_note"] = "CPU flags and operator success do not prove AMX was used by every H3 operation."
    save_json(args.report, report)
    emit("dependency_check", report=args.report, checks=tests)
    if not tests["float32"]["ok"]:
        raise RuntimeError("FP32 CPU operator check failed")


def selected_file(name):
    return name in SMALL_FILES or name.split("/", 1)[0] in ROOTS


def download(args):
    require_linux()
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().model_info(MODEL_ID, revision=MODEL_REVISION, files_metadata=True)
    if info.sha != MODEL_REVISION:
        raise RuntimeError("Unexpected model revision from Hub")
    files = [{"path": item.rfilename, "size": item.size}
             for item in info.siblings if selected_file(item.rfilename)]
    if not files or any(item["size"] is None for item in files):
        raise RuntimeError("Cannot determine the complete download size")
    model_dir = Path(args.model_dir).resolve()
    needed = sum(item["size"] for item in files)
    emit("download_plan", model=MODEL_ID, revision=MODEL_REVISION,
         model_dir=model_dir, files=len(files), bytes=needed, gib=round(needed / GIB, 2),
         execution_requested=args.execute_download)
    if not args.execute_download:
        emit("dry_run", message="Metadata only. Add --execute-download to download model weights.")
        return
    if (model_dir / ".h3-cpu-manifest.json").exists():
        old = json.loads((model_dir / ".h3-cpu-manifest.json").read_text())
        if old.get("revision") != MODEL_REVISION:
            raise RuntimeError("Choose an empty directory for a different model revision")
    model_dir.mkdir(parents=True, exist_ok=True)
    missing_bytes = sum(item["size"] for item in files
                        if not (model_dir / item["path"]).exists()
                        or (model_dir / item["path"]).stat().st_size != item["size"])
    if shutil.disk_usage(model_dir).free < missing_bytes + 10 * GIB:
        raise RuntimeError("Insufficient disk: selected missing files plus 10 GiB headroom required")
    started = time.perf_counter()
    snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION,
                      allow_patterns=[item["path"] for item in files],
                      local_dir=str(model_dir), max_workers=args.workers)
    for item in files:
        if (model_dir / item["path"]).stat().st_size != item["size"]:
            raise RuntimeError(f"Downloaded file size mismatch: {item['path']}")
    save_json(model_dir / ".h3-cpu-manifest.json", {
        "model": MODEL_ID, "revision": MODEL_REVISION, "files": files,
        "download_seconds": time.perf_counter() - started,
    })
    emit("download_complete", model_dir=model_dir, seconds=time.perf_counter() - started)


def validate(path, width=None, height=None, frames=None, *, exact_15_seconds=False, duration_seconds=None):
    if exact_15_seconds and duration_seconds is not None:
        raise ValueError("exact_15_seconds and duration_seconds are mutually exclusive")
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg and ffprobe are required")
    result = subprocess.run([
        "ffprobe", "-v", "error", "-count_frames", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames,nb_read_frames,sample_rate,channels,duration,duration_ts,time_base",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if not video or not audio:
        raise RuntimeError("Output must contain both video and audio")
    if Fraction(video["avg_frame_rate"]) != 24:
        raise RuntimeError("Output is not 24 fps")
    if width is not None and (video["width"], video["height"]) != (width, height):
        raise RuntimeError("Output geometry differs from request")
    if int(audio["sample_rate"]) != 32000 or int(audio["channels"]) != 2:
        raise RuntimeError("Output audio is not 32 kHz stereo")
    if frames is not None:
        if int(video.get("nb_read_frames", 0)) != frames:
            raise RuntimeError("Decoded output frame count differs from the requested delivery frame count")
        expected = frames / 24
        if abs(float(data["format"]["duration"]) - expected) > 0.25:
            raise RuntimeError("Output duration differs from the aligned frame count")
    if exact_15_seconds:
        exact_duration.validate_exact15_media(data)
        data["exact_15_seconds_validated"] = True
    elif duration_seconds is not None:
        exact_duration.validate_duration_media(data, duration_seconds)
        data["duration_seconds_validated"] = duration_seconds
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
                    "-f", "null", "-"], check=True, capture_output=True, timeout=300)
    data["full_decode_ok"] = True
    return data


def module_audit(torch, name, module):
    counts = {}
    if isinstance(module, torch.nn.Module):
        for tensor in list(module.parameters()) + list(module.buffers()):
            if tensor.device.type != "cpu":
                raise RuntimeError(f"{name}: found non-CPU or meta tensor on {tensor.device}")
            key = str(tensor.dtype)
            counts[key] = counts.get(key, 0) + tensor.numel()
    return {"device": "cpu" if counts else "non-tensor component", "elements_by_dtype": counts}


def process_memory():
    """Linux RSS and peak RSS, distinct from host free RAM and tensor-size sums."""
    result = {}
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "VmHWM"}:
                result["rss_bytes" if key == "VmRSS" else "peak_rss_bytes"] = int(value.split()[0]) * 1024
    return result


def resolve_prompt(prompt=None, prompt_file=None):
    """Read a bounded local UTF-8 prompt without putting its text in process argv."""
    if prompt is not None and prompt_file is not None:
        raise ValueError("--prompt and --prompt-file are mutually exclusive")
    if prompt_file is not None:
        path = Path(prompt_file)
        if not path.is_file():
            raise ValueError("--prompt-file must be an existing local UTF-8 text file")
        try:
            with path.open("r", encoding="utf-8", newline="") as source:
                prompt = source.read(MAX_PROMPT_CHARACTERS + 1)
        except (OSError, UnicodeError) as exc:
            raise ValueError("--prompt-file could not be read as UTF-8 text") from exc
    elif prompt is None:
        prompt = DEFAULT_PROMPT
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must contain non-whitespace text")
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        raise ValueError(f"Prompt must contain at most {MAX_PROMPT_CHARACTERS} characters")
    return prompt


def infer(args):
    prompt = resolve_prompt(getattr(args, "prompt", None), getattr(args, "prompt_file", None))
    frame_plan = exact_duration.make_frame_plan(
        args.frames, exact_15_seconds=getattr(args, "exact_15_seconds", False),
        duration_seconds=getattr(args, "duration_seconds", None))
    duration_adaptation = frame_plan["duration_adaptation"]
    needs_extended_pipeline = frame_plan["native_generation_duration_seconds"] > 15
    workflow = resolve_workflow(args.first_frame, args.references_json)
    reference_spec = read_reference_spec(args.references_json) if args.references_json else []
    components = components_for_workflow(workflow)
    transformer_name = "transformer_ref" if workflow == "ref2va" else "transformer"
    if not args.execute_inference:
        emit("dry_run", message="No weights loaded. Add --execute-inference on the Linux trial host.",
             workflow=workflow, components=components, references=reference_spec,
             denoiser_forwards=args.steps, sigma_points=args.steps + 1,
             width=args.width, height=args.height, frames=frame_plan["output_frames"], dtype=args.dtype,
             prompt_characters=len(prompt), prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
             max_runtime_seconds=args.max_runtime_seconds, **frame_plan)
        return
    require_linux()
    if args.width < 32 or args.height < 32 or args.width % 32 or args.height % 32:
        raise ValueError("width/height must be positive multiples of 32")
    if not 1 <= args.steps <= 100:
        raise ValueError("--steps is the denoiser forward count, expected 1..100")
    if args.max_runtime_seconds <= 0:
        raise ValueError("--max-runtime-seconds must be positive")
    if args.first_frame and not Path(args.first_frame).is_file():
        raise ValueError("--first-frame must be an existing local file, not a remote URL")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Install ffmpeg/ffprobe before loading weights")
    system = system_info()
    if system.get("available_memory_gib", 0) < args.min_free_gib:
        raise RuntimeError(f"Available RAM below safety threshold {args.min_free_gib} GiB")
    installed = system["packages"]
    for package, commit in (("diffusers", DIFFUSERS_COMMIT), ("transformers", TRANSFORMERS_COMMIT)):
        if installed[package].get("commit") != commit:
            detail = installed[package].get("source_verification_error", "missing pinned VCS metadata")
            raise RuntimeError(f"Pinned {package} source is unverified: {detail}")
    model_dir = Path(args.model_dir).resolve()
    manifest_path = model_dir / ".h3-cpu-manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("Missing download manifest: run the explicit download stage first")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("revision") != MODEL_REVISION:
        raise RuntimeError("Model manifest revision mismatch")
    if workflow == "ref2va":
        from download_from_modelscope import validate_completed_manifest
        validate_completed_manifest(manifest, required_selection="base")
        validate_completed_manifest(manifest, required_selection="ref")
    for item in manifest["files"]:
        file = model_dir / item["path"]
        if not file.is_file() or file.stat().st_size != item["size"]:
            raise RuntimeError(f"Missing/incomplete local model file: {item['path']}")
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError("Output exists; choose a new output filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".report.json")
    report = {"status": "started", "system": system, "model_revision": MODEL_REVISION,
              "arguments": {**vars(args), "prompt": prompt}, "workflow": workflow, "references": reference_spec,
              "component_load_seconds": {}, "component_audits": {}, "memory_at_start": process_memory(),
              **frame_plan}
    if duration_adaptation is not None:
        report["duration_adapter_sources"] = {
            "helper_sha256": hashlib.sha256(Path(exact_duration.__file__).read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "base_diffusers_commit": DIFFUSERS_COMMIT,
        }
    started = time.perf_counter()
    termination_signal = [None]
    def on_terminate(signum, frame):
        termination_signal[0] = signum
        raise InterruptedError(f"Inference terminated by signal {signum}")

    previous_sigterm = signal.signal(signal.SIGTERM, on_terminate)
    def check_deadline():
        if time.perf_counter() - started >= args.max_runtime_seconds:
            raise TimeoutError("Inference deadline reached; this stops the job, not cloud billing")

    save_json(report_path, report)
    # The inference stage is offline, even if a config refers back to the Hub.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    try:
        torch = configure_cpu(args.threads)
        from diffusers import ModularPipeline
        from diffusers.models.attention_dispatch import attention_backend
        from diffusers.utils.export_utils import encode_video
        from PIL import Image
        dtype = torch.bfloat16 if args.dtype == "bf16" else torch.float32
        decoded_references = None
        if reference_spec:
            tick = time.perf_counter()
            decoded_references = decode_references(reference_spec)
            report["reference_decode_seconds"] = time.perf_counter() - tick
            report["reference_objects"] = [type(ref).__name__ for ref in decoded_references]
            save_json(report_path, report)
        pipeline_class = ModularPipeline
        if needs_extended_pipeline:
            from diffusers.modular_pipelines.minimax_h3.modular_pipeline import MiniMaxH3ModularPipeline
            pipeline_class = exact_duration.exact15_pipeline_class(MiniMaxH3ModularPipeline)
        pipe = pipeline_class.from_pretrained(
            str(model_dir), workflow=workflow,
            local_files_only=True, trust_remote_code=False,
        )
        if needs_extended_pipeline and type(pipe) is not pipeline_class:
            raise RuntimeError("The local exact-15 pipeline subclass was not retained during loading")
        report["pipeline_class"] = type(pipe).__name__
        for name in components:
            check_deadline()
            tick = time.perf_counter()
            emit("component_loading", component=name)
            kwargs = {"pretrained_model_name_or_path": str(model_dir), "local_files_only": True}
            if name in {"transformer", "transformer_ref", "text_encoder"}:
                kwargs["dtype"] = dtype
            elif name in {"vae", "audio_vae"}:
                # Conservative CPU decoding: preserve FP32 VAEs; BF16 only for the two large components.
                kwargs["dtype"] = torch.float32
            if name == "text_encoder":
                kwargs["attn_implementation"] = "sdpa"
            pipe.load_components(names=name, **kwargs)
            module = getattr(pipe, name, None)
            # load_components logs failures rather than reliably raising: check every required component.
            if module is None:
                raise RuntimeError(f"Required component failed to load: {name}")
            if isinstance(module, torch.nn.Module):
                module.to(device="cpu")
                module.eval().requires_grad_(False)
            report["component_load_seconds"][name] = time.perf_counter() - tick
            report["component_audits"][name] = module_audit(torch, name, module)
            save_json(report_path, report)
            emit("component_loaded", component=name, seconds=time.perf_counter() - tick)
        pipe.to(device="cpu")
        if duration_adaptation is not None and (
                pipe.fps != 24 or pipe.audio_sampling_rate != 32000 or pipe.vae_frames_per_chunk != 17 or
                pipe.vae_latents_per_chunk != 5 or
                pipe.max_duration != duration_adaptation["effective_max_duration_seconds"]):
            raise RuntimeError("Loaded pipeline geometry differs from the exact-duration adapter contract")
        transformer = getattr(pipe, transformer_name)
        transformer.set_attention_backend("native")
        forward_times = []
        forward_start = [0.0]
        pipeline_started = [None]

        def before_forward(module, inputs):
            check_deadline()
            forward_start[0] = time.perf_counter()
            if not forward_times and pipeline_started[0] is not None:
                report["preprocess_and_reference_encoding_seconds"] = forward_start[0] - pipeline_started[0]
                report["memory_before_first_forward"] = process_memory()
                save_json(report_path, report)
            emit("denoiser_start", forward=len(forward_times) + 1)

        def after_forward(module, inputs, result):
            forward_times.append(time.perf_counter() - forward_start[0])
            report["denoiser_forward_seconds"] = list(forward_times)
            report["observed_denoiser_forwards"] = len(forward_times)
            report["memory_latest"] = process_memory()
            report["elapsed_seconds"] = time.perf_counter() - started
            save_json(report_path, report)
            emit("denoiser_complete", forward=len(forward_times), seconds=forward_times[-1],
                 memory=report["memory_latest"], elapsed_seconds=report["elapsed_seconds"])
            check_deadline()

        transformer.register_forward_pre_hook(before_forward)
        transformer.register_forward_hook(after_forward)
        request = dict(prompt=prompt, height=args.height, width=args.width,
                       num_frames=frame_plan["native_generation_frames"], num_inference_steps=args.steps + 1,
                       generator=torch.Generator(device="cpu").manual_seed(args.seed),
                       output=["videos", "audio", "sampling_rate"])
        if args.first_frame:
            request["image"] = Image.open(args.first_frame).convert("RGB")
        if reference_spec:
            request["references"] = decoded_references
        check_deadline()
        tick = time.perf_counter()
        pipeline_started[0] = tick
        emit("inference_started", workflow=workflow, references=reference_spec,
             request={k: v for k, v in request.items() if k not in {"generator", "image", "references"}})
        with torch.inference_mode(), attention_backend("native"):
            result = pipe(**request)
        report["pipeline_seconds"] = time.perf_counter() - tick
        report["denoiser_forward_seconds"] = forward_times
        report["observed_denoiser_forwards"] = len(forward_times)
        if len(forward_times) != args.steps:
            raise RuntimeError("Observed forward count differs from requested count; inspect scheduler semantics")
        for name in components:
            module_audit(torch, name, getattr(pipe, name))
        video, audio, crop_audit = exact_duration.prepare_output_media(
            result["videos"][0], result["audio"][0], result["sampling_rate"], frame_plan)
        report["output_crop"] = crop_audit
        save_json(report_path, report)
        tick = time.perf_counter()
        encode_video(video, fps=24, output_path=str(output),
                     audio=audio, audio_sample_rate=result["sampling_rate"])
        report["encode_seconds"] = time.perf_counter() - tick
        report["media_validation"] = validate(output, args.width, args.height, frame_plan["output_frames"],
                                              exact_15_seconds=frame_plan["exact_15_seconds"],
                                              duration_seconds=frame_plan.get("duration_seconds"))
        report["status"] = "completed_media_validated"
        report["quality_note"] = ("Few-step/small-canvas compatibility experiment; not a quality validation."
                                  if args.steps < 49 or min(args.width, args.height) < 768 else
                                  "Normal sampling configuration; reference adherence and visual/audio quality need human review.")
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if termination_signal[0] is not None:
            report["termination_signal"] = termination_signal[0]
            report["status"] = "terminated"
        report["total_seconds"] = time.perf_counter() - started
        report["memory_final"] = process_memory()
        save_json(report_path, report)
        signal.signal(signal.SIGTERM, previous_sigterm)
        emit("trial_end", status=report["status"], report=report_path, seconds=report["total_seconds"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage")
    inspect = sub.add_parser("inspect", help="Standard-library host/package inspection; no torch import or download")
    inspect.add_argument("--report")
    checks = sub.add_parser("check", help="Linux only: dependency imports and tiny CPU operator checks; no model")
    checks.add_argument("--threads", type=int, default=32)
    checks.add_argument("--report", default="cpu-check.json")
    dl = sub.add_parser("download", help="Linux only: metadata plan; weights require --execute-download")
    dl.add_argument("--model-dir", default="/data/h3-cpu/model")
    dl.add_argument("--workers", type=int, default=4)
    dl.add_argument("--execute-download", action="store_true")
    run = sub.add_parser("infer", help="Offline, Linux x86_64 only; requires --execute-inference")
    run.add_argument("--model-dir", default="/data/h3-cpu/model")
    run.add_argument("--output", default="/data/h3-cpu/output/smoke.mp4")
    run.add_argument("--execute-inference", action="store_true")
    run.add_argument("--dtype", choices=("bf16", "fp32"), default="bf16")
    run.add_argument("--threads", type=int, default=32)
    run.add_argument("--steps", type=int, help="Actual forwards: default 49 for ref2va, 2 for the original t2va/fl2va smoke test")
    run.add_argument("--width", type=int, help="Default 768 for ref2va, 256 for the original smoke test")
    run.add_argument("--height", type=int, help="Default 768 for ref2va, 256 for the original smoke test")
    frame_options = run.add_mutually_exclusive_group()
    frame_options.add_argument("--frames", type=int, help="Native aligned frame count (default: 124; maximum: 345)")
    frame_options.add_argument("--exact-15-seconds", action="store_true",
                               help="Explicit runner extension: generate 362 real frames, then deliver 360 frames and 15 s audio")
    frame_options.add_argument("--duration-seconds", type=int, choices=range(4, 16),
                               help="Generate aligned native frames and deliver an exact integer duration from 4 to 15 seconds")
    run.add_argument("--seed", type=int, default=2101)
    run.add_argument("--min-free-gib", type=float, default=256, help="Conservative RAM guard, not an official minimum")
    run.add_argument("--first-frame", help="Optional LOCAL image path; enables fl2va")
    run.add_argument("--references-json", help="Ordered local image/video/audio paths in a JSON list; enables ref2va")
    run.add_argument("--max-runtime-seconds", type=int, default=3600,
                     help="Job deadline checked between stages/forwards; NOT a cloud billing stop")
    prompt_options = run.add_mutually_exclusive_group()
    prompt_options.add_argument("--prompt", help="Prompt text; the original smoke prompt is used if neither source is set")
    prompt_options.add_argument("--prompt-file", type=Path,
                                help="Local UTF-8 prompt file, nonempty and at most 7000 characters; keeps text out of argv")
    val = sub.add_parser("validate", help="Check an existing MP4 using ffprobe and full ffmpeg decode")
    val.add_argument("video")
    args = parser.parse_args()
    if not args.stage:
        parser.print_help()
        return
    if args.stage == "inspect":
        data = system_info()
        if args.report:
            save_json(args.report, data)
        emit("inspection", **data)
    elif args.stage == "check":
        check(args)
    elif args.stage == "download":
        download(args)
    elif args.stage == "infer":
        reference_run = bool(args.references_json)
        if args.steps is None:
            args.steps = 49 if reference_run else 2
        if args.width is None:
            args.width = 768 if reference_run else 256
        if args.height is None:
            args.height = 768 if reference_run else 256
        infer(args)
    elif args.stage == "validate":
        emit("media_validation", **validate(args.video))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("error", type=type(exc).__name__, message=str(exc))
        sys.exit(1)
