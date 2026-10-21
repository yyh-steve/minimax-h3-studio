"""Exact-duration planning/cropping tests; optional installed-encoder toy check."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

try:
    import exact_duration
except ModuleNotFoundError:
    exact_duration = None


class ExactDurationTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(exact_duration, "The explicit exact-duration adapter is not implemented")

    def test_native_plans_keep_124_and_345_but_reject_unaligned_360_and_362(self):
        self.assertEqual(exact_duration.make_frame_plan()["native_generation_frames"], 124)
        for frames in (124, 243, 345):
            plan = exact_duration.make_frame_plan(frames)
            self.assertEqual(plan["native_generation_frames"], frames)
            self.assertEqual(plan["output_frames"], frames)
            self.assertFalse(plan["exact_15_seconds"])
        for frames in (120, 360, 362):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                exact_duration.make_frame_plan(frames)

    def test_exact_plan_distinguishes_generation_and_delivery(self):
        plan = exact_duration.make_frame_plan(exact_15_seconds=True)
        self.assertEqual(plan["native_generation_frames"], 362)
        self.assertEqual(plan["output_frames"], 360)
        self.assertEqual(plan["output_duration_seconds"], 15.0)
        self.assertEqual(plan["native_video_latent_frames"], 107)
        self.assertEqual(plan["native_audio_latent_frames"], 603)
        self.assertEqual(plan["duration_adaptation"]["native_audio_samples_expected"], 482400)
        self.assertEqual(plan["duration_adaptation"]["output_audio_samples"], 480000)
        with self.assertRaises(ValueError):
            exact_duration.make_frame_plan(124, exact_15_seconds=True)

    def test_crop_uses_actual_prefix_frames_and_samples_without_padding_or_retiming(self):
        plan = exact_duration.make_frame_plan(exact_15_seconds=True)
        video = np.arange(362).reshape(362, 1, 1, 1)
        audio = np.stack([np.arange(482400), np.arange(482400) + 10])
        output_video, output_audio, audit = exact_duration.prepare_output_media(video, audio, 32000, plan)
        np.testing.assert_array_equal(output_video, video[:360])
        np.testing.assert_array_equal(output_audio, audio[:, :480000])
        self.assertEqual(int(output_video[-1, 0, 0, 0]), 359)
        self.assertEqual(audit["discarded_video_frames"], 2)
        self.assertEqual(audit["discarded_audio_samples"], 2400)
        self.assertFalse(audit["resampled_or_time_stretched"])

    def test_incomplete_or_wrong_rate_generation_is_not_padded_into_a_result(self):
        plan = exact_duration.make_frame_plan(exact_15_seconds=True)
        for count, samples, rate in ((345, 482400, 32000), (362, 479000, 32000), (362, 482400, 44100)):
            with self.subTest(count=count, samples=samples, rate=rate), self.assertRaises(ValueError):
                exact_duration.prepare_output_media(list(range(count)), np.zeros((2, samples)), rate, plan)

    def test_native_media_is_returned_without_a_crop(self):
        video, audio = list(range(124)), np.zeros((2, 165600))
        out_video, out_audio, audit = exact_duration.prepare_output_media(
            video, audio, 32000, exact_duration.make_frame_plan(124))
        self.assertIs(out_video, video)
        self.assertIs(out_audio, audio)
        self.assertFalse(audit["applied"])

    def test_extension_only_overrides_instance_class_duration(self):
        class BasePipeline:
            @property
            def max_duration(self):
                return 15.0
        extended = exact_duration.exact15_pipeline_class(BasePipeline)
        self.assertEqual(BasePipeline().max_duration, 15.0)
        self.assertEqual(extended().max_duration, 362 / 24)
        self.assertTrue(issubclass(extended, BasePipeline))

    def test_exact_media_validation_rejects_presentation_padding(self):
        import copy
        metadata = {"format": {"duration": "15.000000"}, "streams": [
            {"codec_type": "video", "nb_read_frames": "360", "avg_frame_rate": "24/1",
             "duration_ts": 184320, "time_base": "1/12288", "duration": "15.000000"},
            {"codec_type": "audio", "sample_rate": "32000", "channels": 2,
             "duration_ts": 480000, "time_base": "1/32000", "duration": "15.000000"},
        ]}
        exact_duration.validate_exact15_media(metadata)
        for kind in ("container", "audio", "frames"):
            changed = copy.deepcopy(metadata)
            if kind == "container":
                changed["format"]["duration"] = "15.008000"
            elif kind == "audio":
                changed["streams"][1]["duration_ts"] = 480256
            else:
                changed["streams"][0]["nb_read_frames"] = "362"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                exact_duration.validate_exact15_media(changed)

    def test_cli_switch_is_exclusive_and_dry_run_does_not_import_models(self):
        directory = Path(__file__).parent
        rejected = subprocess.run([sys.executable, str(directory / "h3_cpu_trial.py"), "infer",
                                   "--frames", "124", "--exact-15-seconds"], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        script = ("import sys; import h3_cpu_trial; "
                  "sys.argv=['h3_cpu_trial.py','infer','--exact-15-seconds']; h3_cpu_trial.main(); "
                  "assert 'torch' not in sys.modules; assert 'diffusers' not in sys.modules")
        run = subprocess.run([sys.executable, "-c", script], cwd=directory, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        plan = json.loads(run.stdout)
        self.assertEqual(plan["native_generation_frames"], 362)
        self.assertEqual(plan["output_frames"], 360)


class PinnedSourceGeometryTest(unittest.TestCase):
    def test_fixed_decoder_chunking_can_return_362_frames_from_107_latents(self):
        fixture = Path(__file__).parent / "fixtures/diffusers/autoencoder_kl_minimax_h3.py"
        raw = fixture.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "58e0ed47fdee8227d738eec5bb1e7a47acc21c65ee3de01aa3795c97d679d015")
        source = raw.decode()
        model = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and
                     node.name == "AutoencoderKLMiniMaxH3")
        decode = next(node for node in model.body if isinstance(node, ast.FunctionDef) and node.name == "_decode")
        namespace = {"torch": SimpleNamespace(Tensor=np.ndarray, cat=lambda values, dim: np.concatenate(values, axis=dim))}
        exec(compile(ast.Module(body=[decode], type_ignores=[]), "pinned_vae_decode", "exec"), namespace)
        fake = SimpleNamespace(tokens_chunk_size=5, config=SimpleNamespace(token_drop=3, clip_length=17),
                               temporal_compression_ratio=4, token_overlap=2, frame_pre_padding=3, frame_overlap=5,
                               _decode_clip=lambda z: np.zeros((1, 1, z.shape[2] * 4, 1, 1)),
                               _blend=lambda old, new, overlap, dim: new)
        # Only exact source chunk geometry runs; no learned VAE or model weights are instantiated.
        actual = namespace["_decode"](fake, np.zeros((1, 1, 107, 1, 1)))
        self.assertEqual(actual.shape[2], 362)


@unittest.skipUnless(os.environ.get("H3_TEST_INSTALLED_ENCODER") == "1",
                     "Run on the existing ECS with H3_TEST_INSTALLED_ENCODER=1; local PyAV is unavailable")
class InstalledEncoderTest(unittest.TestCase):
    def test_actual_pipeline_subclass_is_retained_without_loading_weights(self):
        from diffusers.modular_pipelines.minimax_h3.modular_pipeline import MiniMaxH3ModularPipeline
        import exact_duration as adapter
        extended = adapter.exact15_pipeline_class(MiniMaxH3ModularPipeline)
        pipe = extended.from_pretrained(os.environ.get("H3_TEST_MODEL_DIR", "/data/h3-cpu/model"),
                                        workflow="ref2va", local_files_only=True, trust_remote_code=False)
        self.assertIs(type(pipe), extended)
        self.assertEqual(pipe.max_duration, 362 / 24)
        self.assertEqual(pipe.fps, 24)
        # No load_components call: this verifies pipeline metadata/class selection only.

    def test_actual_installed_diffusers_encoder_preserves_exact_presentation_duration(self):
        import torch
        torch.set_num_threads(2)
        torch.set_num_interop_threads(2)
        from diffusers.utils.export_utils import encode_video
        from h3_cpu_trial import validate
        import exact_duration as adapter
        video = torch.zeros(362, 32, 32, 3, dtype=torch.uint8)
        video[:, :, :, 0] = (torch.arange(362) % 256).to(torch.uint8)[:, None, None]
        time = torch.arange(482400, dtype=torch.float32) / 32000
        audio = torch.stack([0.1 * torch.sin(2 * torch.pi * frequency * time) for frequency in (440, 660)])
        video, audio, _ = adapter.prepare_output_media(video, audio, 32000, adapter.make_frame_plan(exact_15_seconds=True))
        with tempfile.TemporaryDirectory(prefix="h3-exact15-encoder-") as folder:
            output = Path(folder) / "toy-15s.mp4"
            encode_video(video, fps=24, output_path=str(output), audio=audio, audio_sample_rate=32000)
            result = validate(output, 32, 32, 360, exact_15_seconds=True)
            self.assertTrue(result["exact_15_seconds_validated"])


if __name__ == "__main__":
    unittest.main()
