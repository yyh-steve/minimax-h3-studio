"""Bounded image ingestion without model imports or environment proxy support.

HTTPS connections pin an address only after every DNS answer has been checked;
the original hostname remains the HTTP Host and TLS verification/SNI identity.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import warnings

from PIL import Image, ImageOps

__all__ = ["MediaError", "ImageInfo", "validate_source", "prepare_image", "choose_canvas"]

MAX_SOURCE_BYTES = 30_000_000
MAX_IMAGE_PIXELS = 5760 * 5760
DOWNLOAD_TIMEOUT_SECONDS = 20.0
IO_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
CHUNK_BYTES = 64 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_PROGRAM = (
    "import json,socket,sys; "
    "answers=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),0,socket.SOCK_STREAM); "
    "print(json.dumps([answer[4][0] for answer in answers]))"
)
_DATA_HEADERS = {"data:image/jpeg;base64": "JPEG", "data:image/png;base64": "PNG", "data:image/webp;base64": "WEBP"}
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}\Z", re.ASCII)
_DNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z", re.ASCII)


class MediaError(Exception):
    """An input error whose short message is safe to return to an API client."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class _HttpsTarget:
    url: str
    hostname: str
    port: int
    authority: str
    request_target: str


def _parse_https(source: str) -> _HttpsTarget:
    if len(source) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in source):
        raise MediaError("Image URL is invalid.")
    try:
        parts = urlsplit(source)
        if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
            raise ValueError
        hostname = parts.hostname
        port = 443 if parts.port is None else parts.port
        if not hostname or "%" in hostname or not 1 <= port <= 65535 or parts.netloc.endswith(":"):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.removesuffix(".").encode("idna").decode("ascii").lower()
            if len(hostname) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in hostname.split(".")):
                raise ValueError
            host_header = hostname
        else:
            hostname = str(address)
            if address.version == 6:
                if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc):
                    raise ValueError
                host_header = f"[{hostname}]"
            else:
                host_header = hostname
        authority = host_header if port == 443 else f"{host_header}:{port}"
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        request_target = path + ("?" + query if query else "")
        return _HttpsTarget(urlunsplit(("https", authority, path, query, "")), hostname, port, authority, request_target)
    except (ValueError, UnicodeError):
        raise MediaError("Only HTTPS image URLs without user information are supported.") from None


def validate_source(source: str) -> None:
    """Check source syntax without DNS, network, base64 decoding or image loading."""
    if not isinstance(source, str) or not source:
        raise MediaError("Image source is required.")
    if source[:5].lower() == "data:":
        header, separator, encoded = source.partition(",")
        if not separator or header.lower() not in _DATA_HEADERS:
            raise MediaError("Only JPEG, PNG and WEBP base64 data URIs are supported.")
        if len(encoded) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
            raise MediaError("Image exceeds the 30 MB source limit.")
        if not encoded or len(encoded) % 4 or not _BASE64.fullmatch(encoded):
            raise MediaError("Image base64 encoding is invalid.")
        return
    _parse_https(source)


def _public_address(value: str) -> str:
    try:
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified or
                address.is_loopback or address.is_link_local or getattr(address, "is_site_local", False)):
            raise ValueError
        if address.version == 6:
            embedded = address.ipv4_mapped or address.sixtofour
            if embedded is not None:
                _public_address(str(embedded))
            # Avoid globally labelled transition addresses tunnelling to a private IPv4 host.
            if address.teredo is not None:
                raise ValueError
            if address in ipaddress.IPv6Network("64:ff9b::/96"):
                _public_address(str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)))
        return str(address)
    except (ValueError, TypeError, MediaError):
        raise MediaError("Image URL must resolve only to public addresses.") from None


def _default_resolver(hostname: str, port: int, timeout: float) -> list[str]:
    # A timed-out thread cannot terminate getaddrinfo. Isolate it in a bounded
    # child process instead; subprocess.run kills and reaps the child on timeout.
    deadline = time.monotonic() + timeout
    if not _DNS_SLOTS.acquire(timeout=timeout):
        raise MediaError("Image host could not be resolved within the time limit.")
    try:
        remaining = _remaining(deadline)
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _DNS_PROGRAM, hostname, str(port)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=remaining,
            )
            if result.returncode:
                raise ValueError
            addresses = json.loads(result.stdout)
            if not isinstance(addresses, list) or not addresses or not all(isinstance(item, str) for item in addresses):
                raise ValueError
            return addresses
        except (subprocess.TimeoutExpired, OSError, ValueError):
            raise MediaError("Image host could not be resolved within the time limit.") from None
    finally:
        _DNS_SLOTS.release()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, *, timeout: float):
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self._address = _public_address(address)
        self._active_socket = None
        self._aborted = False
        self._socket_lock = threading.Lock()

    def _track(self, sock):
        with self._socket_lock:
            if self._aborted:
                sock.close()
                raise TimeoutError
            self._active_socket = sock

    def connect(self):
        address = ipaddress.ip_address(self._address)
        sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        self._track(sock)
        try:
            sock.settimeout(self.timeout)
            endpoint = (self._address, self.port, 0, 0) if address.version == 6 else (self._address, self.port)
            sock.connect(endpoint)  # Numeric IP only: no second hostname resolution.
            tls = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self._track(tls)
            tls.do_handshake()
            self.sock = tls
        except Exception:
            self.abort()
            raise

    def abort(self):
        with self._socket_lock:
            self._aborted = True
            sock = self._active_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


@contextmanager
def _default_transport(target: _HttpsTarget, address: str, timeout: float):
    connection = _PinnedHTTPSConnection(target.hostname, target.port, address, timeout=min(IO_TIMEOUT_SECONDS, timeout))
    # A socket timeout alone is an inactivity limit. This watchdog also bounds
    # headers/slow streaming even when individual bytes keep arriving.
    watchdog = threading.Timer(timeout, connection.abort)
    watchdog.daemon = True
    response = None
    watchdog.start()
    try:
        connection.request("GET", target.request_target, headers={
            "Host": target.authority, "Accept": "image/jpeg,image/png,image/webp", "Accept-Encoding": "identity",
            "Connection": "close", "User-Agent": "h3-api-image-ingest/1",
        })
        response = connection.getresponse()
        yield response
    finally:
        watchdog.cancel()
        connection.abort()
        if response is not None:
            response.close()
        connection.close()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaError("Image download exceeded the time limit.")
    return remaining


def _read_response(response, deadline):
    if response.status in {301, 302, 303, 307, 308}:
        location = response.getheader("Location")
        if not location:
            raise MediaError("Image redirect is invalid.")
        return None, location
    if response.status != 200:
        raise MediaError("Image server did not return a complete image.")
    encoding = response.getheader("Content-Encoding", "identity")
    if encoding.strip().lower() != "identity":
        raise MediaError("Compressed HTTP image responses are not supported.")
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (ValueError, TypeError):
            raise MediaError("Image response size is invalid.") from None
        if declared < 0 or declared > MAX_SOURCE_BYTES:
            raise MediaError("Image exceeds the 30 MB source limit.")
    data = bytearray()
    while True:
        _remaining(deadline)
        chunk = response.read1(CHUNK_BYTES)
        _remaining(deadline)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise MediaError("Image response is invalid.")
        if len(data) + len(chunk) > MAX_SOURCE_BYTES:
            raise MediaError("Image exceeds the 30 MB source limit.")
        data.extend(chunk)
    return bytes(data), None


def _download_https(source, resolver, transport):
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
    current = source
    for redirects in range(MAX_REDIRECTS + 1):
        target = _parse_https(current)
        try:
            literal = ipaddress.ip_address(target.hostname)
        except ValueError:
            answers = resolver(target.hostname, target.port, min(IO_TIMEOUT_SECONDS, _remaining(deadline)))
        else:
            answers = [str(literal)]
        _remaining(deadline)
        addresses = list(dict.fromkeys(_public_address(str(answer)) for answer in answers))
        if not addresses:
            raise MediaError("Image host did not resolve to a public address.")
        for address in addresses:
            try:
                with transport(target, address, _remaining(deadline)) as response:
                    data, location = _read_response(response, deadline)
                break
            except MediaError:
                raise
            except (OSError, http.client.HTTPException):
                continue
        else:
            raise MediaError("Image download failed.")
        if location is None:
            return data
        if redirects == MAX_REDIRECTS:
            raise MediaError("Image redirect limit exceeded.")
        current = urljoin(target.url, location)
        _parse_https(current)  # Never switch to HTTP, files, credentials or data URIs on redirect.
    raise MediaError("Image download failed.")


def _check_image(image, expected_format):
    if image.format not in {"JPEG", "PNG", "WEBP"} or (expected_format and image.format != expected_format):
        raise MediaError("Image format is not supported or does not match its data URI.")
    width, height = image.size
    if not 256 <= width <= 5760 or not 256 <= height <= 5760 or width * height > MAX_IMAGE_PIXELS:
        raise MediaError("Image dimensions exceed the supported limits.")
    if not 0.4 <= width / height <= 2.5:
        raise MediaError("Image aspect ratio must be between 0.4 and 2.5.")
    if getattr(image, "n_frames", 1) != 1:
        raise MediaError("Animated images are not supported.")


def prepare_image(source: str, destination: Path, *, resolver=None, transport=None) -> ImageInfo:
    """Write a new EXIF-upright, metadata-free RGB PNG; hash the normalized file.

    The 30,000,000-byte cap applies to source bytes. Decoded images are additionally
    limited to 33,177,600 pixels and edges 256..5760; normalized PNGs can be larger
    than compressed source JPEGs. Existing destinations are never overwritten.
    Optional resolver(host, port, timeout) and transport(target, ip, timeout)
    injection points support offline tests; transport yields an HTTPResponse-like
    object with status, getheader(), read1() and close(). Neither path uses proxies.
    """
    validate_source(source)
    expected_format = None
    temporary = None
    normalized = None
    try:
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise MediaError("Image destination already exists.")
        if source[:5].lower() == "data:":
            header, _, encoded = source.partition(",")
            expected_format = _DATA_HEADERS[header.lower()]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise MediaError("Image base64 encoding is invalid.") from None
            if base64.b64encode(raw).decode("ascii") != encoded:
                raise MediaError("Image base64 encoding is invalid.")
        else:
            raw = _download_https(source, resolver or _default_resolver, transport or _default_transport)
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise MediaError("Image source is empty or exceeds the 30 MB limit.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                _check_image(probe, expected_format)
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.info.clear()
        width, height = normalized.size
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".h3-image-", suffix=".png", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            normalized.save(output, format="PNG")
        digest = hashlib.sha256()
        with temporary.open("rb") as saved:
            for block in iter(lambda: saved.read(CHUNK_BYTES), b""):
                digest.update(block)
        os.link(temporary, destination)  # Atomic create, never replace another request's file.
        return ImageInfo(destination, width, height, digest.hexdigest())
    except MediaError:
        raise
    except Exception:
        raise MediaError("Image could not be downloaded, decoded or saved.") from None
    finally:
        if normalized is not None:
            normalized.close()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


_CANVASES = {
    "1:1": (768, 768), "16:9": (1344, 768), "9:16": (768, 1344),
    "4:3": (1024, 768), "3:4": (768, 1024), "21:9": (1536, 672),
}


def choose_canvas(ratio: str, width: int, height: int) -> dict:
    """Lock a 32-pixel bucket no larger than 768*1344 pixels.

    Adaptive selects the nearest official ratio by logarithmic aspect-ratio
    distance. Returned ratio is the chosen API label, not an exact pixel quotient:
    16:9 uses 1344x768 (7:4), 9:16 its transpose, and 21:9 uses 1536x672 (16:7).
    1:1, 4:3 and 3:4 buckets match their mathematical ratios exactly.
    """
    if (not isinstance(width, int) or isinstance(width, bool) or width <= 0 or
            not isinstance(height, int) or isinstance(height, bool) or height <= 0):
        raise MediaError("Image dimensions are invalid.")
    if ratio == "adaptive":
        aspect = width / height
        ratio = min(_CANVASES, key=lambda label: abs(math.log(aspect / _ratio_value(label))))
    if not isinstance(ratio, str) or ratio not in _CANVASES:
        raise MediaError("Aspect ratio is not supported.")
    canvas_width, canvas_height = _CANVASES[ratio]
    return {"width": canvas_width, "height": canvas_height, "ratio": ratio}


def _ratio_value(label):
    numerator, denominator = label.split(":")
    return int(numerator) / int(denominator)
