from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

try:
    from h3_api import media
except ImportError:
    media = None


def image_bytes(fmt="PNG", size=(320, 256), *, orientation=None):
    image = Image.new("RGB", size, (42, 95, 170))
    output = io.BytesIO()
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    image.save(output, format=fmt, **options)
    return output.getvalue()


def data_uri(data, mime="png"):
    return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.body = io.BytesIO(body)
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @contextmanager
    def __call__(self, target, address, timeout):
        self.calls.append((target, address, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        try:
            yield response
        finally:
            response.close()


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(media, "The media input module has not been implemented")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def destination(self, name="image.png"):
        return self.root / name

    def test_source_validation_accepts_only_supported_shapes_without_network(self):
        valid = data_uri(image_bytes())
        with mock.patch.object(media.socket, "getaddrinfo", side_effect=AssertionError("No DNS in fast validation")):
            self.assertIsNone(media.validate_source(valid))
            self.assertIsNone(media.validate_source("https://cdn.example.com/image.png?signature=opaque"))
        for source in ("http://example.com/x", "/tmp/x.png", "file:///tmp/x.png", "mm_file://123",
                       "https://user:secret@example.com/x", "https://", "https://example.com:0/x", "https://example.com/\r\nx",
                       "data:image/gif;base64,AAAA", "data:image/png;base64,abc", "data:image/png;base64,@@@=",
                       "data:image/png;base64,AA==\n", "data:image/png;base64,", ""):
            with self.subTest(source=source):
                with self.assertRaises(media.MediaError) as error:
                    media.validate_source(source)
                self.assertNotIn("secret", str(error.exception))

    def test_jpeg_png_and_webp_are_written_as_rgb_png_with_matching_hash(self):
        for fmt, mime in (("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp")):
            with self.subTest(fmt=fmt):
                target = self.destination(fmt + ".png")
                info = media.prepare_image(data_uri(image_bytes(fmt), mime), target)
                self.assertIsInstance(info, media.ImageInfo)
                self.assertEqual(info.path, target)
                self.assertEqual((info.width, info.height), (320, 256))
                self.assertEqual(info.sha256, hashlib.sha256(target.read_bytes()).hexdigest())
                with Image.open(target) as normalized:
                    self.assertEqual((normalized.format, normalized.mode), ("PNG", "RGB"))

    def test_exif_orientation_is_applied_before_recording_size(self):
        info = media.prepare_image(data_uri(image_bytes("JPEG", (512, 256), orientation=6), "jpeg"), self.destination())
        self.assertEqual((info.width, info.height), (256, 512))
        with Image.open(info.path) as image:
            self.assertNotIn(274, image.getexif())

    def test_invalid_image_bytes_and_mismatched_mime_do_not_create_files(self):
        for source in (data_uri(b"not an image"), data_uri(image_bytes("JPEG"), "png")):
            with self.subTest(source_length=len(source)), self.assertRaises(media.MediaError):
                media.prepare_image(source, self.destination())
            self.assertFalse(self.destination().exists())

    def test_truncated_image_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(image_bytes("JPEG")[:-100], "jpeg"), self.destination())
        self.assertFalse(self.destination().exists())

    def test_dimension_aspect_ratio_and_pixel_limits_are_enforced(self):
        for size in ((255, 256), (5761, 256), (641, 256), (256, 641)):
            with self.subTest(size=size), self.assertRaises(media.MediaError):
                media.prepare_image(data_uri(image_bytes(size=size)), self.destination())
        with mock.patch.object(media, "MAX_IMAGE_PIXELS", 65535), self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(image_bytes(size=(256, 256))), self.destination())
        for index, size in enumerate(((640, 256), (256, 640))):
            result = media.prepare_image(data_uri(image_bytes(size=size)), self.destination(f"edge-{index}.png"))
            self.assertEqual((result.width, result.height), size)

    def test_animated_webp_is_rejected(self):
        frames = [Image.new("RGB", (256, 256), color) for color in ("red", "blue")]
        stream = io.BytesIO()
        frames[0].save(stream, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
        with self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(stream.getvalue(), "webp"), self.destination())

    def test_source_size_limit_is_checked_before_decode(self):
        with mock.patch.object(media, "MAX_SOURCE_BYTES", 100), self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(image_bytes()), self.destination())
        self.assertFalse(self.destination().exists())

    def test_existing_destination_is_never_overwritten(self):
        self.destination().write_bytes(b"keep")
        with self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(image_bytes()), self.destination())
        self.assertEqual(self.destination().read_bytes(), b"keep")

    def test_broken_destination_symlink_is_rejected_without_following_it(self):
        other = self.root / "unexpected.png"
        self.destination().symlink_to(other)
        with self.assertRaises(media.MediaError):
            media.prepare_image(data_uri(image_bytes()), self.destination())
        self.assertTrue(self.destination().is_symlink())
        self.assertFalse(other.exists())

    def test_https_resolves_once_and_transports_a_validated_ip_with_original_host(self):
        resolver = mock.Mock(return_value=["8.8.8.8", "1.1.1.1"])
        transport = FakeTransport([FakeResponse(image_bytes())])
        info = media.prepare_image("https://cdn.example.com:8443/a.png?q=one", self.destination(),
                                   resolver=resolver, transport=transport)
        self.assertEqual(info.width, 320)
        self.assertEqual(resolver.call_count, 1)
        target, address, timeout = transport.calls[0]
        self.assertEqual((target.hostname, target.port, target.authority, target.request_target),
                         ("cdn.example.com", 8443, "cdn.example.com:8443", "/a.png?q=one"))
        self.assertEqual(address, "8.8.8.8")
        self.assertGreater(timeout, 0)

    def test_any_nonpublic_dns_address_blocks_the_entire_answer(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fe80::1",
                        "::ffff:127.0.0.1", "224.0.0.1", "fec0::1", "64:ff9b::7f00:1"):
            transport = FakeTransport([])
            with self.subTest(address=address), self.assertRaises(media.MediaError):
                media.prepare_image("https://cdn.example.com/a.png", self.destination(),
                                    resolver=lambda *args: ["8.8.8.8", address], transport=transport)
            self.assertEqual(transport.calls, [])

    def test_private_ip_literal_cannot_be_made_safe_by_an_injected_dns_answer(self):
        transport = FakeTransport([])
        with self.assertRaises(media.MediaError):
            media.prepare_image("https://127.0.0.1/a.png", self.destination(),
                                resolver=lambda *args: ["8.8.8.8"], transport=transport)
        self.assertEqual(transport.calls, [])

    def test_redirect_rechecks_dns_and_rejects_rebinding_to_private_address(self):
        resolver = mock.Mock(side_effect=[["8.8.8.8"], ["127.0.0.1"]])
        transport = FakeTransport([FakeResponse(status=302, headers={"Location": "/next.png"})])
        with self.assertRaises(media.MediaError):
            media.prepare_image("https://cdn.example.com/a.png", self.destination(), resolver=resolver, transport=transport)
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(len(transport.calls), 1)

    def test_redirect_cannot_downgrade_to_http_or_local_file(self):
        for target in ("http://example.com/b.png", "file:///etc/passwd", "https://user:secret@example.com/b.png"):
            transport = FakeTransport([FakeResponse(status=302, headers={"Location": target})])
            with self.subTest(target=target), self.assertRaises(media.MediaError):
                media.prepare_image("https://cdn.example.com/a.png", self.destination(),
                                    resolver=lambda *args: ["8.8.8.8"], transport=transport)
            self.assertEqual(len(transport.calls), 1)

    def test_three_redirects_succeed_but_four_are_rejected(self):
        for redirect_count in (3, 4):
            responses = [FakeResponse(status=302, headers={"Location": f"/hop-{i}.png"}) for i in range(redirect_count)]
            transport = FakeTransport(responses + [FakeResponse(image_bytes())])
            target = self.destination(f"redirects-{redirect_count}.png")
            if redirect_count == 3:
                media.prepare_image("https://cdn.example.com/a.png", target,
                                    resolver=lambda *args: ["8.8.8.8"], transport=transport)
                self.assertTrue(target.exists())
            else:
                with self.assertRaises(media.MediaError):
                    media.prepare_image("https://cdn.example.com/a.png", target,
                                        resolver=lambda *args: ["8.8.8.8"], transport=transport)
                self.assertFalse(target.exists())

    def test_declared_and_streamed_size_limits_close_the_response(self):
        for headers in ({"Content-Length": "101"}, {}):
            response = FakeResponse(b"x" * 101, headers=headers)
            transport = FakeTransport([response])
            with mock.patch.object(media, "MAX_SOURCE_BYTES", 100), self.assertRaises(media.MediaError):
                media.prepare_image("https://cdn.example.com/a.png", self.destination(),
                                    resolver=lambda *args: ["8.8.8.8"], transport=transport)
            self.assertTrue(response.closed)
            self.assertFalse(self.destination().exists())

    def test_total_deadline_is_enforced_after_slow_resolution(self):
        clock = {"now": 0.0}
        def resolver(*args):
            clock["now"] += media.DOWNLOAD_TIMEOUT_SECONDS + 1
            return ["8.8.8.8"]
        transport = FakeTransport([])
        with mock.patch.object(media.time, "monotonic", side_effect=lambda: clock["now"]), self.assertRaises(media.MediaError):
            media.prepare_image("https://cdn.example.com/a.png", self.destination(), resolver=resolver, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_default_dns_uses_isolated_bounded_process_with_data_only_arguments(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps(["8.8.8.8"]), stderr="")
        answers = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with mock.patch("subprocess.run", return_value=result) as run, \
             mock.patch.object(media.socket, "getaddrinfo", return_value=answers):
            self.assertEqual(media._default_resolver("cdn.example.com", 443, 1.5), ["8.8.8.8"])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, "-I", "-S", "-c"])
        self.assertEqual(command[-2:], ["cdn.example.com", "443"])
        self.assertNotIn("cdn.example.com", command[4])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 1.5)

    def test_dns_process_timeout_returns_safe_failure(self):
        failure = subprocess.TimeoutExpired(["python", "secret-value"], 0.01)
        answers = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with mock.patch("subprocess.run", side_effect=failure), \
             mock.patch.object(media.socket, "getaddrinfo", return_value=answers), \
             self.assertRaises(media.MediaError) as error:
            media._default_resolver("cdn.example.com", 443, 0.01)
        self.assertNotIn("secret-value", str(error.exception))

    def test_network_errors_have_a_safe_message(self):
        transport = FakeTransport([OSError("internal secret token=abc123")])
        with self.assertRaises(media.MediaError) as error:
            media.prepare_image("https://cdn.example.com/a.png", self.destination(),
                                resolver=lambda *args: ["8.8.8.8"], transport=transport)
        self.assertNotIn("abc123", str(error.exception))
        self.assertLess(len(error.exception.message), 160)

    def test_production_connection_pins_ip_but_uses_hostname_for_tls(self):
        sock = mock.Mock()
        context = SimpleNamespace(check_hostname=True, verify_mode=ssl.CERT_REQUIRED, post_handshake_auth=False,
                                  wrap_socket=mock.Mock(return_value=sock))
        with mock.patch.object(media.socket, "socket", return_value=sock), \
             mock.patch.object(media.socket, "getaddrinfo", side_effect=AssertionError("No second DNS lookup")), \
             mock.patch.object(media.ssl, "create_default_context", return_value=context) as make_context:
            connection = media._PinnedHTTPSConnection("cdn.example.com", 443, "8.8.8.8", timeout=2)
            connection.connect()
        make_context.assert_called_once_with()
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "cdn.example.com")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_transport_watchdog_interrupts_stalled_headers_without_real_network(self):
        aborted = threading.Event()
        connection = mock.Mock()
        connection.abort.side_effect = aborted.set
        def stalled_headers():
            if not aborted.wait(0.5):
                raise AssertionError("The whole-request watchdog did not abort the connection")
            raise TimeoutError("simulated stalled headers")
        connection.getresponse.side_effect = stalled_headers
        target = media._parse_https("https://cdn.example.com/image.png")
        with mock.patch.object(media, "_PinnedHTTPSConnection", return_value=connection), self.assertRaises(TimeoutError):
            with media._default_transport(target, "8.8.8.8", 0.01):
                self.fail("A stalled response must not be yielded")
        connection.close.assert_called_once()
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Host"], "cdn.example.com")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertNotIn("Authorization", headers)

    def test_canvas_buckets_are_bounded_and_adaptive_selects_nearest_official_ratio(self):
        expected = {"1:1": (768, 768), "16:9": (1344, 768), "9:16": (768, 1344),
                    "4:3": (1024, 768), "3:4": (768, 1024), "21:9": (1536, 672)}
        for ratio, size in expected.items():
            canvas = media.choose_canvas(ratio, 512, 512)
            self.assertEqual((canvas["width"], canvas["height"]), size)
            self.assertEqual(canvas["ratio"], ratio)
            self.assertEqual(canvas["width"] % 32, 0)
            self.assertEqual(canvas["height"] % 32, 0)
            self.assertLessEqual(canvas["width"] * canvas["height"], 768 * 1344)
        self.assertEqual(media.choose_canvas("adaptive", 1184, 880)["ratio"], "4:3")
        self.assertEqual(media.choose_canvas("adaptive", 256, 600)["ratio"], "9:16")
        self.assertEqual(media.choose_canvas("adaptive", 600, 256)["ratio"], "21:9")
        for ratio, width, height in (("bad", 320, 256), ("adaptive", 0, 256), ("16:9", 256, -1)):
            with self.subTest(ratio=ratio), self.assertRaises(media.MediaError):
                media.choose_canvas(ratio, width, height)


if __name__ == "__main__":
    unittest.main()
