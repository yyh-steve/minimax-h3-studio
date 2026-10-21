"""Reject a wrong QA reference using real local images, media, and CLI output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


class ReferenceQaCliTest(unittest.TestCase):
    def test_wrong_reference_fails_qa_while_the_recorded_reference_passes(self):
        script = Path(__file__).with_name("ref2va-example") / "qa_ref2va.py"
        with tempfile.TemporaryDirectory(prefix="h3-reference-qa-") as folder:
            root = Path(folder)
            recorded, wrong = root / "recorded.png", root / "wrong.png"
            Image.new("RGB", (8, 8), "red").save(recorded)
            Image.new("RGB", (8, 8), "green").save(wrong)
            video = root / "clip.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "2",
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=1",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads", "2",
                "-c:a", "aac", "-ar", "32000", "-ac", "2", str(video),
            ], check=True, capture_output=True, timeout=30)
            report = root / "run.report.json"
            report.write_text(json.dumps({
                "status": "completed_media_validated", "workflow": "ref2va",
                "observed_denoiser_forwards": 49,
                "references": [{"type": "image", "path": str(recorded),
                                "sha256": hashlib.sha256(recorded.read_bytes()).hexdigest()}],
                "arguments": {"width": 64, "height": 64, "frames": 24,
                              "steps": 49, "references_json": "references.json"},
            }))
            for reference, matches in ((recorded, True), (wrong, False)):
                with self.subTest(reference=reference.name):
                    output = root / reference.stem
                    result = subprocess.run([
                        sys.executable, "-B", str(script), str(video), str(report),
                        "--reference", str(reference), "--output-dir", str(output),
                    ], capture_output=True, text=True, timeout=30)
                    data = json.loads((output / "qa.json").read_text())
                    self.assertNotIn("error", data, result.stderr)
                    self.assertEqual(data["checks"]["reference_sha256_present_in_run_report"], matches)
                    self.assertEqual(data["automatic_media_ok"], matches)
                    self.assertEqual(result.returncode == 0, matches, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
