#!/usr/bin/env python3
"""Submit or resume a local H3 task using only Python's standard library."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_IMAGE_BYTES = 30_000_000
MAX_JSON_BYTES = 1_048_576


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward a Bearer key or silently transform a POST after a redirect.
        return None


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def positive_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def checked_url(value, *, origin_only=False):
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError
        if origin_only and (parts.path not in ("", "/") or parts.query):
            raise ValueError
    except ValueError:
        raise ClientError("Use an HTTP(S) URL without credentials; --base-url must be an origin without a path.") from None
    return value.rstrip("/") if origin_only else value


def api_key():
    value = os.environ.get("H3_API_KEY", "")
    if len(value) < 24 or not value.isascii() or any(char.isspace() for char in value):
        raise ClientError("Set H3_API_KEY to the service's ASCII key, at least 24 characters with no whitespace.")
    return value


def image_source(value):
    if value.lower().startswith("https://"):
        checked_url(value)
        return value
    if value.lower().startswith("data:"):
        header, comma, encoded = value.partition(",")
        allowed = {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}
        if not comma or header.lower() not in allowed or not encoded:
            raise ClientError("Use a JPEG, PNG or WEBP base64 data URI.")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ClientError("The image source exceeds 30,000,000 bytes.")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ClientError("The image data URI has invalid base64.") from None
        if not decoded or len(decoded) > MAX_IMAGE_BYTES:
            raise ClientError("The image source is empty or exceeds 30,000,000 bytes.")
        return value
    if urllib.parse.urlsplit(value).scheme and not Path(value).is_file():
        raise ClientError("--image must be a local image, public HTTPS URL or supported data URI.")
    path = Path(value).expanduser()
    if not path.is_file():
        raise ClientError("The local image file does not exist.")
    with path.open("rb") as source:
        content = source.read(MAX_IMAGE_BYTES + 1)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ClientError("The image source is empty or exceeds 30,000,000 bytes.")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif content.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        kind = "webp"
    else:
        raise ClientError("The local file is not a JPEG, PNG or WEBP image.")
    return f"data:image/{kind};base64," + base64.b64encode(content).decode("ascii")


def prompt_text(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ClientError("--prompt-file must be an existing local UTF-8 text file.")
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            text = source.read(7001)
    except (OSError, UnicodeError):
        raise ClientError("--prompt-file must be a readable local UTF-8 text file.") from None
    if not text.strip() or len(text) > 7000:
        raise ClientError("The prompt must contain non-whitespace text and at most 7000 characters.")
    return text


def request_json(opener, key, timeout, method, url, *, payload=None, idempotency_key=None):
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Each invocation sends at most one HTTP request. POST is never retried.
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as error:
        message = ""
        try:
            body = json.loads(error.read(65536).decode("utf-8"))
            message = str(body.get("error", {}).get("message", ""))
        except (ValueError, UnicodeError, AttributeError, OSError, http.client.HTTPException):
            pass
        message = " ".join(message.replace(key, "[redacted]").split())[:500]
        raise ClientError(f"API returned HTTP {error.code}" + (f": {message}" if message else ".")) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise ClientError("The API request failed or timed out; no automatic retry was made.") from None
    if len(raw) > MAX_JSON_BYTES:
        raise ClientError("The API JSON response is too large.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ClientError("The API did not return valid JSON.") from None
    if not isinstance(result, dict):
        raise ClientError("The API response must be a JSON object.")
    return result


def save_receipt(path, data, *, new=False):
    """Save no API key, prompt, image body or signed download URL."""
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if new:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(serialized)
            destination.flush()
            os.fsync(destination.fileno())
        return
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(serialized)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download(opener, url, output, timeout):
    checked_url(url)
    if output.exists() or output.is_symlink():
        raise ClientError("The output already exists; choose a new --output path.")
    descriptor, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".part", dir=output.parent)
    digest, count = hashlib.sha256(), 0
    try:
        # Signed media URLs are bearer capabilities themselves. Do not send the API key.
        request = urllib.request.Request(url, headers={"Accept": "video/mp4"}, method="GET")
        with os.fdopen(descriptor, "wb") as destination:
            try:
                with opener.open(request, timeout=timeout) as response:
                    expected = response.headers.get("Content-Length")
                    expected = int(expected) if expected is not None else None
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        destination.write(block)
                        digest.update(block)
                        count += len(block)
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
                raise ClientError("The download failed or timed out. Resume by task ID to obtain a fresh signed URL.") from None
            if count == 0 or (expected is not None and count != expected):
                raise ClientError("The downloaded file is empty or incomplete; resume by task ID.")
            destination.flush()
            os.fsync(destination.fileno())
        # Same-directory hard link publishes a complete file and fails if the destination appeared meanwhile.
        os.link(temporary, output)
        return {"bytes": count, "sha256": digest.hexdigest()}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Local JPEG/PNG/WEBP, public HTTPS URL, or supported data URI; new tasks only")
    parser.add_argument("--prompt-file", type=Path, help="Local UTF-8 prompt, 1..7000 characters; new tasks only")
    parser.add_argument("--duration", type=int, choices=range(4, 16), help="Exact output seconds; default 5 for a new task")
    parser.add_argument("--ratio", choices=RATIOS, help="Canvas label; default adaptive for a new task")
    parser.add_argument("--base-url", default=os.environ.get("H3_PUBLIC_BASE_URL", "http://127.0.0.1:30010"))
    parser.add_argument("--output", type=Path, required=True, help="New local MP4 path; existing files are never overwritten")
    parser.add_argument("--task-id", help="Resume querying/downloading an existing task; never submits a POST")
    parser.add_argument("--receipt", type=Path, help="Receipt JSON; default: <output>.task.json")
    parser.add_argument("--idempotency-key", help="Local service extension; a random key is generated for each new submission")
    parser.add_argument("--timeout", type=positive_seconds, default=30.0, help="HTTP socket timeout in seconds; default 30")
    parser.add_argument("--poll-interval", type=positive_seconds, default=15.0, help="Seconds between successful status polls; default 15")
    args = parser.parse_args()
    if args.task_id:
        if not TASK_ID.fullmatch(args.task_id):
            parser.error("--task-id must be the service's 32-character lowercase hexadecimal ID")
        if any(value is not None for value in (args.image, args.prompt_file, args.duration, args.ratio, args.idempotency_key)):
            parser.error("--task-id cannot be combined with new-task image, prompt, duration, ratio or idempotency options")
    elif args.image is None or args.prompt_file is None:
        parser.error("New tasks require --image and --prompt-file; use --task-id to resume")
    base = checked_url(args.base_url, origin_only=True)
    key = api_key()
    output_argument = args.output.expanduser()
    if output_argument.exists() or output_argument.is_symlink():
        raise ClientError("The output already exists; choose a new --output path before submitting.")
    output = output_argument.resolve()
    receipt_path = (args.receipt or output.with_name(output.name + ".task.json")).expanduser()
    if receipt_path.is_symlink():
        raise ClientError("The receipt path must not be a symlink.")
    receipt_path = receipt_path.resolve()
    if receipt_path == output:
        raise ClientError("--receipt and --output must name different files.")
    output.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(NoRedirect())
    task_id = args.task_id
    receipt = {"schema": 1, "base_url": base, "output": str(output), "created_at": timestamp(), "task_id": task_id}
    if task_id:
        if receipt_path.exists():
            try:
                previous = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise ClientError("The existing receipt is invalid; choose a new --receipt path.") from None
            if not isinstance(previous, dict) or previous.get("task_id") != task_id or previous.get("base_url") != base:
                raise ClientError("The receipt belongs to another task or service; choose a new --receipt path.")
            receipt = {**previous, "output": str(output)}
        else:
            save_receipt(receipt_path, receipt, new=True)
        print(f"task_id: {task_id}", flush=True)
    else:
        if receipt_path.exists():
            raise ClientError("A receipt already exists. Use its task_id with --task-id; do not blindly submit again.")
        idem = args.idempotency_key or uuid.uuid4().hex
        if not 1 <= len(idem) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in idem):
            raise ClientError("--idempotency-key must contain 1..128 printable, non-space ASCII characters.")
        payload = {"model": "MiniMax-H3", "resolution": "768P", "duration": args.duration or 5,
                   "ratio": args.ratio or "adaptive", "content": [
                       {"type": "text", "text": prompt_text(args.prompt_file)},
                       {"type": "image_url", "role": "reference_image", "image_url": {"url": image_source(args.image)}},
                   ]}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt.update(status="submission_pending", idempotency_key=idem,
                       request_sha256=hashlib.sha256(encoded).hexdigest())
        save_receipt(receipt_path, receipt, new=True)
        print(f"receipt: {receipt_path}", flush=True)
        try:
            created = request_json(opener, key, args.timeout, "POST", base + "/v2/video_generation",
                                   payload=payload, idempotency_key=idem)
            task_id = created.get("task_id")
            if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
                raise ClientError("Submission returned no valid task_id.")
        except (ClientError, KeyboardInterrupt):
            receipt.update(status="submission_unknown", updated_at=timestamp())
            save_receipt(receipt_path, receipt)
            print("No confirmed task_id was received. Do not submit again with a new idempotency key; inspect the receipt and task list.", file=sys.stderr)
            raise
        print(f"task_id: {task_id}", flush=True)
        receipt.update(task_id=task_id, status="submitted", updated_at=timestamp())
        save_receipt(receipt_path, receipt)
    resume = [sys.executable, str(Path(__file__).resolve()), "--task-id", task_id,
              "--base-url", base, "--output", str(output), "--receipt", str(receipt_path)]
    print("Resume: " + shlex.join(resume), flush=True)
    previous_status = None
    while True:
        response = request_json(opener, key, args.timeout, "GET", base + "/v2/query/video_generation/" + task_id)
        task = response.get("task")
        if not isinstance(task, dict) or task.get("id") != task_id:
            raise ClientError("Query returned an unexpected task.")
        status = task.get("status")
        if status not in ("queued", "running", "succeeded", "failed", "cancelled"):
            raise ClientError("Query returned an unsupported task status.")
        if status != previous_status:
            print(f"{timestamp()}  {status}", flush=True)
            receipt.update(status=status, updated_at=timestamp())
            save_receipt(receipt_path, receipt)
            previous_status = status
        if status == "succeeded":
            url = task.get("content", {}).get("url")
            if not isinstance(url, str):
                raise ClientError("The successful task has no download URL.")
            receipt.update(download=download(opener, url, output, args.timeout), downloaded_at=timestamp())
            save_receipt(receipt_path, receipt)
            print(f"Saved {receipt['download']['bytes']} bytes to {output}", flush=True)
            return 0
        if status in ("failed", "cancelled"):
            error = task.get("error", {})
            if isinstance(error, dict) and error.get("code"):
                print("Task error code: " + str(error["code"]), file=sys.stderr)
            return 1
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Client stopped. The server task was not cancelled; resume using the saved task_id.", file=sys.stderr)
        raise SystemExit(130)
    except (ClientError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
