"""Local H3 request preflight, kept independent of the model/service runtime.

This mirrors h3-api/h3_api/contracts.py and the syntax-only image-source check.
Image decoding, public-address resolution and resource limits remain server-side.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
_DATA_HEADERS = {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}\Z", re.ASCII)
_DNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z", re.ASCII)


def _validate_image_source(source: str) -> None:
    if not isinstance(source, str) or not source:
        raise ValueError("本地 H3 需要图片 URL 或 data URI")
    if source[:5].lower() == "data:":
        header, separator, encoded = source.partition(",")
        if not separator or header.lower() not in _DATA_HEADERS:
            raise ValueError("本地 H3 仅支持 JPEG/PNG/WEBP base64 data URI")
        if (not encoded or len(encoded) > 40_000_000 or len(encoded) % 4
                or not _BASE64.fullmatch(encoded)):
            raise ValueError("图片 base64 无效或超过 30 MB 源文件限制")
        return
    try:
        if len(source) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in source):
            raise ValueError
        url = urlsplit(source)
        host, port = url.hostname, url.port
        if (url.scheme != "https" or not host or "%" in host
                or url.username is not None or url.password is not None
                or url.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.removesuffix(".").encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split(".")):
                raise ValueError
        else:
            if address.version == 6 and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", url.netloc):
                raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("本地 H3 图片需使用无账号信息的 HTTPS URL，或 JPEG/PNG/WEBP data URI") from None


def validate_local_request(request: dict) -> None:
    """Raise ValueError before enqueueing unsupported local H3 work; never coerce it."""
    if not isinstance(request, dict):
        raise ValueError("本地 H3 请求必须是 JSON 对象")
    if set(request) - {"model", "content", "duration", "resolution", "ratio", "aigc_watermark"}:
        raise ValueError("本地 H3 请求包含不支持的字段，仅支持单图 reference 模式")
    if request.get("model") != "MiniMax-H3":
        raise ValueError("本地 H3 仅支持 MiniMax-H3，不支持 MiniMax-H3-Max")
    if request.get("resolution") != "768P":
        raise ValueError("本地 H3 仅支持 768P，不支持 480P 或 2K")
    duration = request.get("duration")
    if type(duration) is not int or not 4 <= duration <= 15:
        raise ValueError("本地 H3 duration 必须是 4–15 的整数")
    ratio = request.get("ratio", "adaptive")
    if not isinstance(ratio, str) or ratio not in _RATIOS:
        raise ValueError("本地 H3 不支持该画幅比例")
    if "aigc_watermark" in request and request["aigc_watermark"] is not False:
        raise ValueError("本地 H3 aigc_watermark 只能省略或设为 false")
    content = request.get("content")
    if not isinstance(content, list) or len(content) != 2 or any(not isinstance(item, dict) for item in content):
        raise ValueError("本地 H3 content 必须恰好包含一条 text 和一张 reference_image")
    texts = [item for item in content if item.get("type") == "text"]
    images = [item for item in content if item.get("type") == "image_url"]
    if len(texts) != 1 or len(images) != 1:
        raise ValueError("本地 H3 仅支持一条 text 和一张 reference_image")
    text, image = texts[0], images[0]
    if (set(text) != {"type", "text"} or not isinstance(text.get("text"), str)
            or not text["text"].strip() or len(text["text"]) > 7000):
        raise ValueError("本地 H3 text 必须非空且不超过 7000 字符")
    if set(image) != {"type", "role", "image_url"} or image.get("role") != "reference_image":
        raise ValueError("本地 H3 仅支持显式 reference_image，不支持首帧、尾帧或多图")
    value = image.get("image_url")
    if not isinstance(value, dict) or set(value) != {"url"} or not isinstance(value["url"], str):
        raise ValueError("本地 H3 image_url 必须只包含字符串 url")
    _validate_image_source(value["url"])


def local_planning_hint() -> str:
    return (
        "本地 H3 规划约束：仅支持 MiniMax-H3、768P，每镜时长必须为 4–15 的整数。"
        "每镜必须使用 mode=reference，images.references 恰好引用一张已提供的商品图片，"
        "first_frame 和 last_frame 必须为空；不支持 t2v、首尾帧、多图、MiniMax-H3-Max、480P 或 2K。"
        "每镜最终请求恰好包含一条非空且不超过 7000 字符的 text，和一张 role=reference_image 图片。"
        "图片只支持 JPEG/PNG/WEBP 的 HTTPS URL 或 base64 data URI。"
        "没有提供商品图片时请明确指出缺少参考图，不得编造图片引用。"
    )
