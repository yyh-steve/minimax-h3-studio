#!/usr/bin/env python3
"""Audit a normal local-directory install without asking pip to clone a shallow Git repo."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
from urllib.parse import unquote, urlparse

PINS = {
    "diffusers": "b800f828f3951c4d206e154411bcbfa693cd322e",
    "transformers": "d4804f9d76b8c071b993d813e9e87067b780cbee",
}
MANIFEST_NAME = "h3-source-install.json"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(source, *args):
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True, stderr=subprocess.PIPE, timeout=30
    ).strip()


def python_files(dist, name):
    files = {}
    for entry in dist.files or []:
        relative = Path(str(entry))
        if relative.parts and relative.parts[0] == name and relative.suffix == ".py":
            files[relative.as_posix()] = Path(dist.locate_file(entry))
    if not files:
        raise RuntimeError(f"No installed Python files found for {name}")
    return files


def directory_install_path(dist):
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    parsed = urlparse(direct.get("url", ""))
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or "dir_info" not in direct:
        raise RuntimeError("Expected a normal local-directory installation (file URL and dir_info)")
    if direct["dir_info"].get("editable"):
        raise RuntimeError("Editable installs are not accepted by this source audit")
    return Path(unquote(parsed.path)).resolve()


def record_distribution(name, expected, source, dist):
    source = Path(source).resolve()
    if directory_install_path(dist) != source:
        raise RuntimeError(f"{name}: installed direct_url does not match verified source directory")
    actual = git(source, "rev-parse", "HEAD")
    if actual != expected:
        raise RuntimeError(f"{name}: source HEAD {actual} != {expected}")
    # Builds can add ignored egg-info/build files; tracked source must remain unchanged.
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError(f"{name}: tracked source changed before/while installing")
    tracked = set(git(source, "ls-files", "-z").split("\0"))
    hashes = {}
    for relative, installed in python_files(dist, name).items():
        source_relative = "src/" + relative
        if source_relative not in tracked:
            raise RuntimeError(f"{name}: installed code is not tracked by source Git: {relative}")
        installed_hash = sha256(installed)
        if installed_hash != sha256(source / source_relative):
            raise RuntimeError(f"{name}: installed code differs from verified source: {relative}")
        hashes[relative] = installed_hash
    return {
        "commit": actual,
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "source_directory": str(source),
        "version": dist.version,
        "installed_python_sha256": hashes,
        "method": "verified_local_directory_install",
    }


def verified_install(name, dist, manifest_path=None):
    """Return provenance only if current installed code still matches the recorded installation."""
    path = Path(manifest_path) if manifest_path else Path(sys.prefix) / MANIFEST_NAME
    data = json.loads(path.read_text())
    if data.get("schema") != 1:
        raise RuntimeError("Unknown source-install manifest schema")
    entry = data["packages"][name]
    if entry["commit"] != PINS[name] or entry["version"] != dist.version:
        raise RuntimeError(f"{name}: manifest commit/version mismatch")
    if str(directory_install_path(dist)) != entry["source_directory"]:
        raise RuntimeError(f"{name}: installation source no longer matches manifest")
    current = python_files(dist, name)
    expected_hashes = entry["installed_python_sha256"]
    if set(current) != set(expected_hashes):
        raise RuntimeError(f"{name}: installed Python file list changed")
    for relative, file in current.items():
        if sha256(file) != expected_hashes[relative]:
            raise RuntimeError(f"{name}: installed file hash changed: {relative}")
    return {
        "commit": entry["commit"],
        "commit_source": "verified_source_install_manifest",
        "source_tree": entry["tree"],
        "verified_python_files": len(current),
        "source_manifest": str(path),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "verify"))
    parser.add_argument("--source-root", default="/data/h3-cpu/src")
    args = parser.parse_args()
    if args.action == "record":
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            raise RuntimeError("Recording is restricted to the Linux x86_64 trial host")
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("Use the trial virtual environment's Python")
        packages = {}
        for name, expected in PINS.items():
            packages[name] = record_distribution(
                name, expected, Path(args.source_root) / name, importlib.metadata.distribution(name)
            )
        path = Path(sys.prefix) / MANIFEST_NAME
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"schema": 1, "packages": packages}, indent=2) + "\n")
        temp.replace(path)
        print(f"Recorded verified local-source installation: {path}")
    for name in PINS:
        print(json.dumps({"package": name, **verified_install(name, importlib.metadata.distribution(name))}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Source provenance check failed: {exc}", file=sys.stderr)
        sys.exit(1)
