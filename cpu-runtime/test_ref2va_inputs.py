import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

import ref2va_inputs as inputs


class ReferenceContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.asset = self.root / "reference.png"
        self.asset.write_bytes(b"local reference fixture")

    def write_spec(self, items):
        path = self.root / "references.json"
        path.write_text(json.dumps(items))
        return path

    def test_local_reference_order_is_preserved_and_hashed(self):
        items = inputs.read_reference_spec(self.write_spec([
            {"type": "video", "path": "reference.png"},
            {"type": "image", "path": "reference.png"},
            {"type": "audio", "path": "reference.png"},
        ]))
        self.assertEqual([item["type"] for item in items], ["video", "image", "audio"])
        self.assertTrue(all(item["path"] == str(self.asset.resolve()) for item in items))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in items))

    def test_audio_only_and_empty_requests_are_rejected_before_loading(self):
        for items in ([], [{"type": "audio", "path": "reference.png"}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                inputs.read_reference_spec(self.write_spec(items))

    def test_remote_missing_and_misspelled_fields_are_rejected(self):
        for item in (
            {"type": "image", "path": "https://example.com/a.png"},
            {"type": "image", "path": "missing.png"},
            {"type": "image", "path": "reference.png", "roll": "subject"},
            {"type": "text", "path": "reference.png"},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                inputs.read_reference_spec(self.write_spec([item]))

    def test_modality_and_total_limits(self):
        image = {"type": "image", "path": "reference.png"}
        video = {"type": "video", "path": "reference.png"}
        audio = {"type": "audio", "path": "reference.png"}
        for items in ([image] * 10, [video] * 4, [image] + [audio] * 4,
                      [image] * 9 + [video] * 3 + [audio]):
            with self.subTest(count=len(items)), self.assertRaises(ValueError):
                inputs.read_reference_spec(self.write_spec(items))

    def test_reference_workflow_cannot_silently_fall_back_to_t2va(self):
        self.assertEqual(inputs.resolve_workflow(None, "refs.json"), "ref2va")
        self.assertEqual(inputs.resolve_workflow("first.png", None), "fl2va")
        self.assertEqual(inputs.resolve_workflow(None, None), "t2va")
        with self.assertRaises(ValueError):
            inputs.resolve_workflow("first.png", "refs.json")
        for first, refs in ((None, ""), (None, " "), ("", None)):
            with self.subTest(first=first, refs=refs), self.assertRaises(ValueError):
                inputs.resolve_workflow(first, refs)
        self.assertIn("transformer_ref", inputs.components_for_workflow("ref2va"))
        self.assertNotIn("transformer", inputs.components_for_workflow("ref2va"))

    def test_cli_reference_dry_run_defaults_to_normal_sampling_without_model_imports(self):
        path = self.write_spec([{"type": "image", "path": "reference.png"}])
        script = ("import sys; import h3_cpu_trial; "
                  "sys.argv=['h3_cpu_trial.py','infer','--references-json',sys.argv[1]]; "
                  "h3_cpu_trial.main(); "
                  "assert 'torch' not in sys.modules; assert 'diffusers' not in sys.modules")
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["workflow"], "ref2va")
        self.assertEqual(plan["denoiser_forwards"], 49)
        self.assertEqual((plan["width"], plan["height"]), (768, 768))


if __name__ == "__main__":
    unittest.main()
