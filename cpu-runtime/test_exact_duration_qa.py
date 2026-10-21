"""Local, tiny FFmpeg fixtures for delivered-duration QA; no model or network."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import wave


SCRIPT = Path(__file__).with_name("ref2va-example") / "qa_ref2va.py"
SPEC = importlib.util.spec_from_file_location("qa_ref2va", SCRIPT)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


class ExactDurationQaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="h3-exact-duration-qa-")
        cls.root = Path(cls.temporary.name)
        cls.exact_video = cls.make_video("exact.mp4", "15")
        cls.long_audio_video = cls.make_video("audio-15.008.mp4", "15.008")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def make_video(cls, filename, audio_duration):
        target = cls.root / filename
        command = [
            "ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "2",
            "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=32000:duration={audio_duration}",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-ar", "32000", "-ac", "2", str(target),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return target

    @staticmethod
    def report(*, exact=True):
        report = {
            "status": "complete", "workflow": "ref2va", "references": [{"type": "image"}],
            "observed_denoiser_forwards": 49,
            "arguments": {"width": 64, "height": 64, "frames": 362 if exact else 360,
                          "steps": 49, "references_json": "fixture-references.json"},
        }
        if exact:
            report.update(exact_15_seconds=True, native_generation_frames=362,
                          output_frames=360, output_duration_seconds=15.0,
                          duration_adaptation={"mode": "generate_aligned_then_crop",
                                               "output_audio_samples": 480000})
        return report

    def inspect(self, video, report):
        output = self.root / self.id().split(".")[-1]
        output.mkdir()
        report_path = output / "report.json"
        report_path.write_text(json.dumps(report))
        data = {
            "inputs": {"video": str(video), "run_report": str(report_path)},
            "checked_at_utc": "test", "checks": {}, "artifacts": {},
            "automatic_media_ok": False,
        }
        qa.inspect(SimpleNamespace(video=str(video), report=str(report_path), reference=None), data, output)
        qa.write_markdown(data, output)
        return data, output

    def test_output_frames_overrides_native_generation_frame_request(self):
        data, output = self.inspect(self.exact_video, self.report())
        self.assertTrue(data["automatic_media_ok"], data["checks"])
        self.assertEqual(data["reported_run"]["native_generation_frames"], 362)
        self.assertEqual(data["reported_run"]["output_frames"], 360)
        self.assertEqual(data["media"]["decoded_frames"], 360)
        for field in ("video_duration_seconds", "audio_duration_seconds", "container_duration_seconds"):
            self.assertEqual(data["media"][field], 15.0)
        self.assertIn("362 / 360", (output / "qa.md").read_text())
        with wave.open(str(output / "audio-preview.wav"), "rb") as audio:
            # This AAC fixture really has decoded tail padding, while its
            # presentation duration is exactly 480000 / 32000 seconds.
            self.assertGreater(audio.getnframes(), 480000)
            self.assertEqual(audio.getframerate(), 32000)

    def test_exact_mode_rejects_15_008_audio_and_container_duration(self):
        report = self.report()
        # Keep the old frame matcher satisfied: this isolates the loose duration check.
        report["arguments"]["frames"] = 360
        data, _ = self.inspect(self.long_audio_video, report)
        self.assertEqual(data["media"]["audio_duration_seconds"], 15.008)
        self.assertEqual(data["media"]["container_duration_seconds"], 15.008)
        self.assertFalse(data["automatic_media_ok"], data["checks"])

    def test_adaptation_metadata_enables_strict_checks_without_flag(self):
        report = self.report()
        report.pop("exact_15_seconds")
        report["arguments"]["frames"] = 360
        data, _ = self.inspect(self.long_audio_video, report)
        self.assertFalse(data["automatic_media_ok"], data["checks"])

    def test_exact_mode_rejects_wrong_native_generation_frame_claim(self):
        report = self.report()
        report["native_generation_frames"] = 360
        report["arguments"]["frames"] = 360
        data, _ = self.inspect(self.exact_video, report)
        self.assertFalse(data["automatic_media_ok"], data["checks"])

    def test_legacy_arguments_frames_remains_supported(self):
        data, _ = self.inspect(self.exact_video, self.report(exact=False))
        self.assertTrue(data["automatic_media_ok"], data["checks"])
        self.assertTrue(data["checks"]["frame_count_matches_report"])


if __name__ == "__main__":
    unittest.main()
