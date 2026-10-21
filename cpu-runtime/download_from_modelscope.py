#!/usr/bin/env python3
"""Download an audited H3 base or Ref2VA supplement; default is a local plan only."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HF_MODEL = "MiniMaxAI/MiniMax-H3"
HF_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
MS_MODEL = "MiniMax/MiniMax-H3"
MS_REVISION = "57559a6720ddaa2eaee52b09ec2ef011127997b2"
MANIFEST_SHA256 = "9bc11dfa6abbf7faef7ab80159f522a4fe3cc64c98d72ca949a34ffed74c5346"
REF_MANIFEST_SHA256 = "a0f8ba4322a6288bf5bc27dac34dc95706f4847aeec0f7acbde4800b9671d5fd"
GIB = 1024**3
ROOTS = {"transformer", "text_encoder", "tokenizer", "processor", "vae",
         "audio_vae", "scheduler", "audio_scheduler"}
SMALL_FILES = {"model_index.json", "modular_model_index.json", "LICENSE", "README.md"}
AUDITED_MANIFESTS = {
    "base": {"filename": "modelscope-h3-manifest.json", "sha256": MANIFEST_SHA256,
             "count": 63, "roots": ROOTS},
    "ref": {"filename": "modelscope-h3-ref-manifest.json", "sha256": REF_MANIFEST_SHA256,
            "count": 16, "roots": {"transformer_ref"}},
}


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def check_running(stop, deadline):
    if stop.is_set():
        raise RuntimeError("Download cancelled after another file failed")
    if time.monotonic() >= deadline:
        raise TimeoutError("Download time limit reached; partial files retained")


def file_hash(path, stop, deadline):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            check_running(stop, deadline)
            block = source.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _manifest_files(data, roots):
    """Validate untrusted completion-record fields before looking up audited entries."""
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise RuntimeError("Invalid manifest schema")
    if (data.get("model"), data.get("revision"), data.get("source_model"),
            data.get("source_revision")) != (HF_MODEL, HF_REVISION, MS_MODEL, MS_REVISION):
        raise RuntimeError("Unexpected source or reference revision")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("Missing manifest files")
    seen = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("Invalid manifest file entry")
        name = item["path"]
        pure = PurePosixPath(name)
        if (not name or pure.is_absolute() or ".." in pure.parts or str(pure) != name
                or "\\" in name or "\x00" in name or name in seen):
            raise RuntimeError("Unsafe or duplicate manifest path")
        if name not in SMALL_FILES and (not pure.parts or pure.parts[0] not in roots):
            raise RuntimeError("File outside the audited CPU selection: " + name)
        if type(item.get("size")) is not int or item["size"] <= 0:
            raise RuntimeError("Invalid file size")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise RuntimeError("Invalid SHA256")
        seen[name] = item
    if type(data.get("total_bytes")) is not int or sum(x["size"] for x in files) != data["total_bytes"]:
        raise RuntimeError("Incomplete audited file selection")
    return seen


def load_manifest(path):
    """Load either immutable companion audit; no URL access or model files are touched."""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    selection = next((name for name, spec in AUDITED_MANIFESTS.items()
                      if digest == spec["sha256"]), None)
    if selection is None:
        raise RuntimeError("Manifest digest mismatch; use the audited companion JSON unchanged")
    data = json.loads(raw)
    spec = AUDITED_MANIFESTS[selection]
    files = _manifest_files(data, spec["roots"])
    if len(files) != spec["count"] or data.get("selection", "base") != selection:
        raise RuntimeError("Incomplete audited file selection")
    return data


def validate_completed_manifest(data, required_selection=None):
    """Return completed selections after checking against both immutable audits.

    This verifies the completion *record*, not current file contents. Inference must
    also check the required on-disk files; this function never hashes 144+ GB itself.
    """
    if required_selection is not None and required_selection not in AUDITED_MANIFESTS:
        raise RuntimeError("Unknown required selection: " + str(required_selection))
    files = _manifest_files(data, ROOTS | {"transformer_ref"})
    if data.get("all_files_sha256_verified") is not True:
        raise RuntimeError("Manifest has no completed SHA256 verification")
    needed_audits = {"ref" if path.split("/", 1)[0] == "transformer_ref" else "base"
                     for path in files}
    audits = {name: load_manifest(Path(__file__).with_name(spec["filename"]))
              for name, spec in AUDITED_MANIFESTS.items() if name in needed_audits}
    expected = {item["path"]: item for audit in audits.values() for item in audit["files"]}
    for name, item in files.items():
        reference = expected.get(name)
        if reference is None or (item["size"], item["sha256"]) != (reference["size"], reference["sha256"]):
            raise RuntimeError("Installed manifest differs from audited file: " + name)
    present = set(files)
    selections = set()
    for name, audit in audits.items():
        paths = {item["path"] for item in audit["files"]}
        if present & paths:
            if not paths <= present:
                raise RuntimeError("Incomplete audited " + name + " selection")
            selections.add(name)
    if required_selection is not None and required_selection not in selections:
        raise RuntimeError("Missing completed " + required_selection + " selection")
    return selections


def merge_completed_manifest(existing, completed):
    """Preserve all previously audited entries; a ref supplement requires the base."""
    incoming_selections = validate_completed_manifest(completed)
    existing_selections = validate_completed_manifest(existing) if existing is not None else set()
    selections = incoming_selections | existing_selections
    if "ref" in selections and "base" not in selections:
        raise RuntimeError("Ref supplement requires the completed base manifest")
    merged = dict(existing if existing is not None else completed)
    indexed = {item["path"]: item for item in merged["files"]}
    indexed.update({item["path"]: item for item in completed["files"]})
    merged["files"] = [indexed[name] for name in sorted(indexed)]
    merged["total_bytes"] = sum(item["size"] for item in merged["files"])
    merged["selections"] = sorted(selections)
    merged["audited_manifest_sha256"] = {name: AUDITED_MANIFESTS[name]["sha256"]
                                          for name in sorted(selections)}
    merged["last_download_seconds"] = completed.get("download_seconds", 0)
    merged["all_files_sha256_verified"] = True
    validate_completed_manifest(merged)
    return merged


def download_one(item, directory, stop, deadline, timeout):
    target = directory / item["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(directory):
        raise RuntimeError("Destination escapes model directory")
    if target.exists():
        if not target.is_file() or target.stat().st_size != item["size"]:
            raise RuntimeError("Existing destination has wrong size; refusing overwrite: " + item["path"])
        if file_hash(target, stop, deadline) != item["sha256"]:
            raise RuntimeError("Existing destination SHA256 mismatch: " + item["path"])
        emit("verified_existing", path=item["path"])
        return
    partial = target.with_name(target.name + ".part")
    if partial.is_symlink():
        raise RuntimeError("Refusing symlink partial file")
    url = "https://modelscope.cn/api/v1/models/" + MS_MODEL + "/repo?" + urllib.parse.urlencode({
        "Revision": MS_REVISION, "FilePath": item["path"],
    })
    started = time.monotonic()
    for attempt in range(4):
        check_running(stop, deadline)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item["size"]:
            raise RuntimeError("Oversize partial file: " + item["path"])
        if offset == item["size"]:
            break
        headers = {"User-Agent": "H3-verified-ModelScope-download", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        emit("file_start", path=item["path"], bytes=item["size"], resume_at=offset, attempt=attempt + 1)
        try:
            request = urllib.request.Request(url, headers=headers)
            remaining = max(1, min(timeout, deadline - time.monotonic()))
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if offset or response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if response.status != 206 or not match or tuple(map(int, match.groups())) != (
                            offset, item["size"] - 1, item["size"]):
                        raise RuntimeError("Server did not honor exact resume range")
                elif response.status != 200:
                    raise RuntimeError("Unexpected HTTP status")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != item["size"] - offset:
                    raise RuntimeError("Unexpected response size: " + item["path"])
                written = offset
                report_at = time.monotonic()
                with partial.open("ab") as output:
                    while True:
                        check_running(stop, deadline)
                        block = response.read(min(1024 * 1024, item["size"] - written + 1))
                        if not block:
                            break
                        if written + len(block) > item["size"]:
                            raise RuntimeError("Response exceeds audited file size")
                        output.write(block)
                        written += len(block)
                        if time.monotonic() - report_at >= 20:
                            emit("file_progress", path=item["path"], bytes=written, total=item["size"])
                            report_at = time.monotonic()
            if partial.stat().st_size != item["size"]:
                raise OSError("Incomplete response; retained partial file")
            break
        except (urllib.error.URLError, OSError) as error:
            if attempt == 3:
                raise
            emit("retry", path=item["path"], error=type(error).__name__)
            check_running(stop, deadline)
            time.sleep(min(2**attempt, max(0, deadline - time.monotonic())))
    if partial.stat().st_size != item["size"] or file_hash(partial, stop, deadline) != item["sha256"]:
        raise RuntimeError("Downloaded SHA256/size mismatch; partial retained: " + item["path"])
    partial.replace(target)
    emit("file_complete", path=item["path"], bytes=item["size"],
         seconds=round(time.monotonic() - started, 2), sha256=item["sha256"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("modelscope-h3-manifest.json"),
                        help="Unchanged audited base JSON, or modelscope-h3-ref-manifest.json for ref-only supplement")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--execute-download", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--timeout", type=int, default=30, help="Socket timeout in seconds")
    parser.add_argument("--max-seconds", type=int, default=3600, help="Download and hash deadline; retains partials")
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_seconds <= 0:
        parser.error("Timeout and max-seconds must be positive")
    manifest = load_manifest(args.manifest)
    directory = args.model_dir.resolve()
    emit("download_plan", selection=manifest.get("selection", "base"),
         files=len(manifest["files"]), bytes=manifest["total_bytes"],
         gib=round(manifest["total_bytes"] / GIB, 3), model_dir=str(directory),
         source_model=MS_MODEL, source_revision=MS_REVISION, reference_revision=HF_REVISION,
         execute=args.execute_download, max_seconds=args.max_seconds)
    if not args.execute_download:
        emit("dry_run", message="Local audited manifest only: no network request or model-directory mutation")
        return
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Weight downloads are restricted to the Linux x86_64 trial host; refusing this machine")
    directory.mkdir(parents=True, exist_ok=True)
    final_manifest = directory / ".h3-cpu-manifest.json"
    if final_manifest.is_symlink():
        raise RuntimeError("Refusing symlink completion manifest")
    existing = json.loads(final_manifest.read_text()) if final_manifest.exists() else None
    # Validate the merge before any weight requests. The merged record is written
    # only after every selected file has actually passed its SHA256 check below.
    completion_candidate = dict(manifest, all_files_sha256_verified=True)
    merge_completed_manifest(existing, completion_candidate)
    needed = 0
    for item in manifest["files"]:
        target = directory / item["path"]
        partial = target.with_name(target.name + ".part")
        if target.exists():
            continue  # Hash validation below refuses any invalid existing file.
        needed += max(0, item["size"] - (partial.stat().st_size if partial.exists() else 0))
    if shutil.disk_usage(directory).free < needed + 10 * GIB:
        raise RuntimeError("Insufficient free disk for missing bytes plus 10 GiB headroom")
    started = time.monotonic()
    deadline = started + args.max_seconds
    stop = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download_one, item, directory, stop, deadline, args.timeout)
                   for item in manifest["files"]]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    completed = dict(manifest)
    completed["download_seconds"] = time.monotonic() - started
    completed["all_files_sha256_verified"] = True
    merged = merge_completed_manifest(existing, completed)
    temporary = final_manifest.with_name(final_manifest.name + ".tmp")
    if temporary.is_symlink():
        raise RuntimeError("Refusing symlink temporary completion manifest")
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, final_manifest)
    emit("download_complete", model_dir=str(directory), files=len(manifest["files"]),
         total_recorded_files=len(merged["files"]), selections=merged["selections"],
         seconds=round(completed["download_seconds"], 2), all_files_sha256_verified=True)


if __name__ == "__main__":
    main()
