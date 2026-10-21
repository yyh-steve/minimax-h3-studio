"""Tiny real-FFmpeg signal tests; no model weights or network access."""
from types import SimpleNamespace
import unittest

import numpy as np
import torch

torch.set_num_threads(2)
torch.set_num_interop_threads(2)

try:
    from ref_audio_resample import resample_reference_audio
except ModuleNotFoundError:
    resample_reference_audio = None


class ReferenceAudioResampleTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(resample_reference_audio, "The input audio resampler is not implemented")

    @staticmethod
    def reference(rate, channels):
        time = np.arange(rate * 2, dtype=np.float64) / rate
        audio = [0.2 * np.sin(2 * np.pi * 440 * time)]
        if channels == 2:
            audio.append(0.1 * np.sin(2 * np.pi * 660 * time))
        return SimpleNamespace(audio=torch.from_numpy(np.asarray(audio, dtype=np.float32)), sample_rate=rate)

    def test_real_resampling_preserves_duration_channels_and_tone(self):
        for rate in (44100, 48000):
            for channels in (1, 2):
                with self.subTest(rate=rate, channels=channels):
                    reference = self.reference(rate, channels)
                    info = resample_reference_audio(reference)
                    self.assertEqual(reference.sample_rate, 32000)
                    self.assertEqual(tuple(reference.audio.shape), (channels, 64000))
                    self.assertEqual(reference.audio.dtype, torch.float32)
                    self.assertEqual(reference.audio.device.type, "cpu")
                    self.assertEqual(reference.audio.shape[1] / reference.sample_rate, 2.0)
                    for channel, frequency in enumerate((440, 660)[:channels]):
                        samples = reference.audio[channel].numpy()
                        bins = np.fft.rfftfreq(samples.size, 1 / reference.sample_rate)
                        observed = bins[np.argmax(np.abs(np.fft.rfft(samples)))]
                        self.assertAlmostEqual(observed, frequency, places=3)
                        expected = (0.2 if channel == 0 else 0.1) / np.sqrt(2)
                        self.assertAlmostEqual(float(np.sqrt(np.mean(samples[64:-64] ** 2))), expected, delta=0.001)
                    self.assertEqual(info["source_rate"], rate)
                    self.assertEqual(info["target_rate"], 32000)
                    self.assertEqual(info["source_samples"], rate * 2)
                    self.assertEqual(info["target_samples"], 64000)
                    self.assertEqual(info["backend"], "ffmpeg/libswresample")
                    self.assertIn("Output", info["backend_log"])

    def test_32000_hz_is_bit_exact_and_does_not_need_ffmpeg(self):
        reference = self.reference(32000, 2)
        original = reference.audio
        before = original.clone()
        info = resample_reference_audio(reference, ffmpeg="/nonexistent/ffmpeg")
        self.assertIs(reference.audio, original)
        self.assertTrue(torch.equal(reference.audio, before))
        self.assertEqual(reference.sample_rate, 32000)
        self.assertEqual(info["backend"], "passthrough")

    def test_backend_failure_leaves_samples_and_rate_unchanged(self):
        reference = self.reference(44100, 2)
        original = reference.audio
        before = original.clone()
        with self.assertRaises(RuntimeError):
            resample_reference_audio(reference, ffmpeg="/usr/bin/false")
        self.assertEqual(reference.sample_rate, 44100)
        self.assertIs(reference.audio, original)
        self.assertTrue(torch.equal(reference.audio, before))

    def test_missing_backend_leaves_original_rate(self):
        reference = self.reference(48000, 1)
        with self.assertRaises(RuntimeError):
            resample_reference_audio(reference, ffmpeg="/nonexistent/ffmpeg")
        self.assertEqual(reference.sample_rate, 48000)
        self.assertEqual(tuple(reference.audio.shape), (1, 96000))

    def test_nonfinite_input_is_rejected_without_relabeling(self):
        reference = self.reference(44100, 1)
        reference.audio[0, 7] = float("nan")
        with self.assertRaises(ValueError):
            resample_reference_audio(reference)
        self.assertEqual(reference.sample_rate, 44100)


if __name__ == "__main__":
    unittest.main()
