"""Offline duration planning/crop/CLI checks and real tiny FFmpeg media checks."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

import exact_duration
import h3_cpu_trial


HERE = Path(__file__).resolve().parent
NATIVE_FRAMES = {4: 124, 5: 124, 6: 158, 7: 175, 8: 192, 9: 226,
                 10: 243, 11: 277, 12: 294, 13: 328, 14: 345, 15: 362}
AUDIO_LATENTS = {4: 207, 5: 207, 6: 263, 7: 292, 8: 320, 9: 377,
                 10: 405, 11: 462, 12: 490, 13: 547, 14: 575, 15: 603}


class DurationSecondsTest(unittest.TestCase):
    def plan(self, seconds, **kwargs):
        self.assertIn("duration_seconds", inspect.signature(exact_duration.make_frame_plan).parameters,
                      "Frame planning must accept the new duration_seconds contract")
        return exact_duration.make_frame_plan(duration_seconds=seconds, **kwargs)

    def validate_metadata(self, metadata, seconds):
        validate = getattr(exact_duration, "validate_duration_media", None)
        self.assertTrue(callable(validate), "Strict duration validation must support 4..15 seconds")
        return validate(metadata, seconds)

    @staticmethod
    def metadata(seconds):
        return {"format": {"duration": f"{seconds}.000000"}, "streams": [
            {"codec_type": "video", "nb_read_frames": str(seconds * 24), "avg_frame_rate": "24/1",
             "duration_ts": seconds * 12288, "time_base": "1/12288", "duration": str(seconds)},
            {"codec_type": "audio", "sample_rate": "32000", "channels": 2,
             "duration_ts": seconds * 32000, "time_base": "1/32000", "duration": str(seconds)},
        ]}

    def test_every_integer_duration_has_distinct_native_and_delivery_contracts(self):
        for seconds in range(4, 16):
            with self.subTest(seconds=seconds):
                plan = self.plan(seconds)
                self.assertEqual(plan["duration_seconds"], seconds)
                self.assertFalse(plan["exact_15_seconds"], "The legacy CLI flag must remain distinct")
                self.assertEqual(plan["native_generation_frames"], NATIVE_FRAMES[seconds])
                self.assertEqual(plan["native_video_latent_frames"], (NATIVE_FRAMES[seconds] - 5) // 17 * 5 + 2)
                self.assertEqual(plan["native_audio_latent_frames"], AUDIO_LATENTS[seconds])
                self.assertEqual(plan["output_frames"], seconds * 24)
                self.assertEqual(plan["output_duration_seconds"], seconds)
                adaptation = plan["duration_adaptation"]
                self.assertEqual(adaptation["native_audio_samples_expected"], AUDIO_LATENTS[seconds] * 800)
                self.assertEqual(adaptation["output_audio_samples"], seconds * 32000)
                self.assertEqual(adaptation["effective_max_duration_seconds"], 362 / 24 if seconds == 15 else 15.0)
                self.assertFalse(adaptation["installed_framework_modified"])
                self.assertFalse(adaptation["checkpoint_modified"])

    def test_duration_rejects_noninteger_types_and_out_of_range_values(self):
        for value in (True, False, "4", 4.0, 15.0, [], {}, float("nan"), -1, 0, 3, 16, 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plan(value)

    def test_new_reports_do_not_reuse_the_legacy_exact15_only_mode_identifier(self):
        legacy = exact_duration.make_frame_plan(exact_15_seconds=True)
        self.assertEqual(legacy["duration_adaptation"]["mode"], "generate_aligned_then_crop")
        for seconds in range(4, 16):
            with self.subTest(seconds=seconds):
                self.assertEqual(self.plan(seconds)["duration_adaptation"]["mode"],
                                 "generate_aligned_then_crop_seconds")

    def test_programmatic_duration_options_are_mutually_exclusive(self):
        for kwargs in ({"frames": 124}, {"exact_15_seconds": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.plan(4, **kwargs)

    def test_all_durations_crop_only_real_prefixes(self):
        for seconds in range(4, 16):
            with self.subTest(seconds=seconds):
                plan = self.plan(seconds)
                video = np.arange(NATIVE_FRAMES[seconds]).reshape(-1, 1, 1, 1)
                samples = AUDIO_LATENTS[seconds] * 800
                audio = np.stack([np.arange(samples), np.arange(samples) + 100])
                output_video, output_audio, audit = exact_duration.prepare_output_media(video, audio, 32000, plan)
                np.testing.assert_array_equal(output_video, video[:seconds * 24])
                np.testing.assert_array_equal(output_audio, audio[:, :seconds * 32000])
                self.assertEqual(audit["discarded_video_frames"], NATIVE_FRAMES[seconds] - seconds * 24)
                self.assertEqual(audit["discarded_audio_samples"], samples - seconds * 32000)
                self.assertEqual(audit["applied"], seconds != 8)
                self.assertFalse(audit["resampled_or_time_stretched"])

    def test_wrong_native_outputs_fail_instead_of_being_padded_or_relabelled(self):
        for seconds in (4, 8, 15):
            plan = self.plan(seconds)
            n, samples = NATIVE_FRAMES[seconds], AUDIO_LATENTS[seconds] * 800
            cases = [(n - 1, samples, 32000, 2), (n + 1, samples, 32000, 2),
                     (n, samples - 1, 32000, 2), (n, samples + 1, 32000, 2),
                     (n, samples, 44100, 2), (n, samples, 32000, 1)]
            for frames, count, rate, channels in cases:
                with self.subTest(seconds=seconds, frames=frames, count=count, rate=rate, channels=channels), \
                        self.assertRaises(ValueError):
                    exact_duration.prepare_output_media(list(range(frames)), np.zeros((channels, count)), rate, plan)

    def test_strict_metadata_checks_all_durations_and_rejects_presentation_drift(self):
        for seconds in range(4, 16):
            metadata = self.metadata(seconds)
            self.validate_metadata(metadata, seconds)
            for field in ("video_duration", "audio_duration", "container", "frames", "rate", "channels"):
                bad = copy.deepcopy(metadata)
                if field == "video_duration":
                    bad["streams"][0]["duration_ts"] += 1
                elif field == "audio_duration":
                    bad["streams"][1]["duration_ts"] += 256
                elif field == "container":
                    bad["format"]["duration"] = str(seconds + 0.008)
                elif field == "frames":
                    bad["streams"][0]["nb_read_frames"] = str(seconds * 24 + 1)
                elif field == "rate":
                    bad["streams"][1]["sample_rate"] = "44100"
                else:
                    bad["streams"][1]["channels"] = 1
                with self.subTest(seconds=seconds, field=field), self.assertRaises(ValueError):
                    self.validate_metadata(bad, seconds)

    def test_old_native_and_exact15_behaviors_remain_available(self):
        native = exact_duration.make_frame_plan()
        self.assertEqual(native["native_generation_frames"], 124)
        self.assertEqual(native["output_frames"], 124)
        self.assertIsNone(native["duration_adaptation"])
        video, audio = list(range(124)), np.zeros((2, 165600))
        out_video, out_audio, audit = exact_duration.prepare_output_media(video, audio, 32000, native)
        self.assertIs(out_video, video)
        self.assertIs(out_audio, audio)
        self.assertFalse(audit["applied"])
        legacy = exact_duration.make_frame_plan(exact_15_seconds=True)
        new = self.plan(15)
        self.assertTrue(legacy["exact_15_seconds"])
        for key in ("native_generation_frames", "native_video_latent_frames", "native_audio_latent_frames",
                    "output_frames", "output_duration_seconds"):
            self.assertEqual(legacy[key], new[key])
        exact_duration.validate_exact15_media(self.metadata(15))
        for frames in (96, 120, 360, 362):
            with self.assertRaises(ValueError):
                exact_duration.make_frame_plan(frames)

    def test_cli_duration_dry_run_is_lightweight_and_preserves_requested_geometry(self):
        for seconds in (4, 8, 15):
            script = ("import sys; import h3_cpu_trial; "
                      f"sys.argv=['h3_cpu_trial.py','infer','--duration-seconds','{seconds}']; "
                      "h3_cpu_trial.main(); assert 'torch' not in sys.modules; assert 'diffusers' not in sys.modules")
            run = subprocess.run([sys.executable, "-c", script], cwd=HERE, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            plan = json.loads(run.stdout)
            self.assertEqual(plan["duration_seconds"], seconds)
            self.assertEqual(plan["native_generation_frames"], NATIVE_FRAMES[seconds])
            self.assertEqual(plan["output_frames"], seconds * 24)

    def test_cli_rejects_conflicting_options_and_noninteger_or_out_of_range_seconds(self):
        options = [(["--duration-seconds", "4", "--frames", "124"]),
                   (["--duration-seconds", "15", "--exact-15-seconds"]),
                   (["--frames", "124", "--exact-15-seconds"])]
        options += [["--duration-seconds", value] for value in ("3", "16", "4.0", "true", "nan")]
        for extra in options:
            with self.subTest(extra=extra):
                run = subprocess.run([sys.executable, str(HERE / "h3_cpu_trial.py"), "infer", *extra],
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 2, run.stderr)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Real FFmpeg/ffprobe are required")
class RealDurationMediaTest(unittest.TestCase):
    """Encode tiny cropped RGB/PCM inputs, then exercise the runner's real validator."""
    plan = DurationSecondsTest.plan

    def encode(self, directory, seconds, *, extra_audio_samples=0):
        plan = self.plan(seconds)
        video = np.zeros((plan["native_generation_frames"], 32, 32, 3), dtype=np.uint8)
        video[:, :, :, 0] = (np.arange(len(video)) % 256)[:, None, None]
        time = np.arange(AUDIO_LATENTS[seconds] * 800, dtype=np.float32) / 32000
        audio = np.stack([0.1 * np.sin(2 * np.pi * frequency * time) for frequency in (440, 660)])
        video, audio, _ = exact_duration.prepare_output_media(video, audio, 32000, plan)
        if extra_audio_samples:
            audio = np.concatenate([audio, np.zeros((2, extra_audio_samples), dtype=np.float32)], axis=1)
        rgb, pcm, output = directory / "video.rgb", directory / "audio.f32", directory / "encoded.mp4"
        rgb.write_bytes(video.tobytes())
        pcm.write_bytes(audio.T.astype("<f4").tobytes())
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", "32x32", "-framerate", "24", "-i", str(rgb),
            "-f", "f32le", "-ar", "32000", "-ac", "2", "-i", str(pcm),
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", str(output),
        ], check=True, capture_output=True, timeout=30)
        return output

    def test_real_media_preserves_exact_4_5_8_and_15_second_presentations(self):
        self.assertIn("duration_seconds", inspect.signature(h3_cpu_trial.validate).parameters,
                      "Runner media validation must expose strict generic duration checks")
        for seconds in (4, 5, 8, 15):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as folder:
                output = self.encode(Path(folder), seconds)
                result = h3_cpu_trial.validate(output, 32, 32, seconds * 24, duration_seconds=seconds)
                self.assertTrue(result["full_decode_ok"])
                self.assertEqual(result["duration_seconds_validated"], seconds)
                self.assertEqual(result["format"]["duration"], f"{seconds}.000000")

    def test_real_media_rejects_extra_8ms_audio_while_video_length_is_correct(self):
        self.assertIn("duration_seconds", inspect.signature(h3_cpu_trial.validate).parameters,
                      "Runner media validation must reject audio presentation drift")
        with tempfile.TemporaryDirectory() as folder:
            output = self.encode(Path(folder), 5, extra_audio_samples=256)
            with self.assertRaises(ValueError):
                h3_cpu_trial.validate(output, 32, 32, 120, duration_seconds=5)


class PromptFileTest(unittest.TestCase):
    def resolve(self, **kwargs):
        resolve = getattr(h3_cpu_trial, "resolve_prompt", None)
        self.assertTrue(callable(resolve), "The runner must resolve and validate file/text prompts before inference")
        return resolve(**kwargs)

    def cli(self, *options):
        return subprocess.run([sys.executable, str(HERE / "h3_cpu_trial.py"), "infer", *options],
                              capture_output=True, text=True)

    def test_utf8_file_is_read_without_changing_its_text(self):
        text = "测试角色在海边挥手。\nNo spoken dialogue.\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(self.resolve(prompt_file=path), text)
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_default_and_explicit_prompt_remain_supported(self):
        expected = "A red fox walks slowly through a snowy forest. Quiet footsteps and a gentle breeze."
        self.assertEqual(self.resolve(), expected)
        self.assertEqual(self.resolve(prompt="A synthetic test prompt."), "A synthetic test prompt.")

    def test_blank_overlong_nontext_and_invalid_utf8_prompts_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            for text in ("", " \t\r\n", "x" * 7001):
                with self.subTest(kind="file", length=len(text)):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.resolve(prompt_file=path)
                with self.subTest(kind="text", length=len(text)), self.assertRaises(ValueError):
                    self.resolve(prompt=text)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(ValueError):
                self.resolve(prompt_file=path)
            for value in (True, 42, {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.resolve(prompt=value)

    def test_length_limit_counts_unicode_characters_and_accepts_7000(self):
        text = "中" * 7000
        self.assertEqual(self.resolve(prompt=text), text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(self.resolve(prompt_file=path), text)

    def test_missing_nonfile_and_conflicting_prompt_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for path in (directory, directory / "missing.txt", "https://example.invalid/prompt.txt"):
                with self.subTest(path=str(path)), self.assertRaises(ValueError):
                    self.resolve(prompt_file=path)
            path = directory / "prompt.txt"
            path.write_text("A synthetic file prompt.", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.resolve(prompt="A synthetic inline prompt.", prompt_file=path)

    def test_cli_reads_file_before_dry_run_and_does_not_echo_prompt(self):
        text = "SYNTHETIC_PRIVATE_PROMPT: 测试角色。\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            path.write_text(text, encoding="utf-8")
            run = self.cli("--duration-seconds", "4", "--prompt-file", str(path))
            self.assertEqual(run.returncode, 0, run.stderr)
            data = json.loads(run.stdout)
            self.assertEqual(data["event"], "dry_run")
            self.assertEqual(data["prompt_characters"], len(text))
            self.assertEqual(data["prompt_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
            self.assertNotIn(text, run.stdout)
            self.assertNotIn("SYNTHETIC_PRIVATE_PROMPT", run.stdout + run.stderr)

    def test_cli_prompt_sources_are_mutually_exclusive_and_blank_file_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            path.write_text("Synthetic text.", encoding="utf-8")
            run = self.cli("--prompt", "Synthetic inline.", "--prompt-file", str(path))
            self.assertEqual(run.returncode, 2, run.stderr)
            path.write_text(" \n", encoding="utf-8")
            run = self.cli("--prompt-file", str(path))
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stdout)["type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
