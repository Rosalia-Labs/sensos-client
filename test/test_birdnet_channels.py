#!/usr/bin/env python3

import importlib.util
from importlib.machinery import SourceFileLoader
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = REPO_ROOT / "overlay"
LIBEXEC_ROOT = OVERLAY_ROOT / "libexec"


def load_process_birdnet():
    interpreter = type("Interpreter", (), {})
    litert_module = types.ModuleType("ai_edge_litert")
    interpreter_module = types.ModuleType("ai_edge_litert.interpreter")
    interpreter_module.Interpreter = interpreter
    sys.modules.setdefault("ai_edge_litert", litert_module)
    sys.modules.setdefault("ai_edge_litert.interpreter", interpreter_module)
    soundfile_module = types.ModuleType("soundfile")
    soundfile_module.write = lambda *args, **kwargs: None
    sys.modules.setdefault("soundfile", soundfile_module)
    sys.path.insert(0, str(LIBEXEC_ROOT))
    spec = importlib.util.spec_from_file_location(
        "process_birdnet_test", LIBEXEC_ROOT / "process-birdnet.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_audio_generator():
    module_name = "generate_queued_wav_test"
    script_path = REPO_ROOT / "test" / "generate-queued-wav"
    spec = importlib.util.spec_from_loader(
        module_name, SourceFileLoader(module_name, str(script_path))
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BirdNETChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.env_patch = patch.dict(
            os.environ, {"SENSOS_CLIENT_ROOT": cls.temp_dir.name}
        )
        cls.env_patch.start()
        root = Path(cls.temp_dir.name)
        (root / "libexec").mkdir(parents=True)
        (root / "libexec" / "utils.py").write_text(
            "def read_kv_config(path): return {}\n"
            "def setup_logging(name): pass\n"
            "def ensure_runtime_dir(path, mode=0o2775):\n"
            "    from pathlib import Path\n"
            "    Path(path).mkdir(parents=True, exist_ok=True)\n",
            encoding="utf-8",
        )
        cls.module = load_process_birdnet()
        cls.generator = load_audio_generator()

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.temp_dir.cleanup()

    def test_default_input_mode_splits_channels(self):
        missing_config = Path(self.temp_dir.name) / "missing.env"
        self.assertEqual(
            self.module.read_input_mode(missing_config), "split-channels"
        )

    def test_four_channel_audio_is_split_without_mixing(self):
        audio = np.arange(24, dtype=np.int32).reshape(6, 4)
        channels = self.module.audio_channels(audio, "split-channels")
        self.assertEqual([index for index, _ in channels], [0, 1, 2, 3])
        for channel_index, channel_audio in channels:
            np.testing.assert_array_equal(channel_audio, audio[:, channel_index])

    def test_mock_microphone_geometry_uses_four_xy_positions(self):
        positions = self.module.parse_mic_positions_cm("0,200;200,0;0,-200;-200,0")
        self.assertEqual(positions.shape, (4, 2))
        np.testing.assert_allclose(positions[0], [0.0, 2.0])
        np.testing.assert_allclose(positions[3], [-2.0, 0.0])

    def test_north_plane_wave_is_strongest_in_north_beam(self):
        sample_rate = self.module.SAMPLE_RATE
        positions = self.module.parse_mic_positions_cm("0,200;200,0;0,-200;-200,0")
        centered = positions - positions.mean(axis=0)
        north_direction = self.module.CARDINAL_BEAMS[0][1]
        advances = (
            centered @ north_direction * sample_rate / self.module.SPEED_OF_SOUND_MPS
        )
        source = np.random.default_rng(7).normal(size=sample_rate).astype(np.float32)
        microphone_audio = np.column_stack(
            [
                self.module.shift_audio_fractional(source, -advance)
                for advance in advances
            ]
        )

        beams = dict(
            self.module.beamform_cardinal(microphone_audio, sample_rate, positions)
        )
        north_rms = np.sqrt(np.mean(np.square(beams[0][100:-100])))
        south_rms = np.sqrt(np.mean(np.square(beams[2][100:-100])))
        self.assertGreater(north_rms, south_rms * 1.5)

    def test_cardinal_cycle_generator_steers_each_three_second_segment(self):
        sample_rate = 8_000
        segment_frames = 3 * sample_rate
        mono_samples = np.random.default_rng(11).normal(
            scale=0.2, size=segment_frames * 4
        ).tolist()
        positions_text = "0,200;200,0;0,-200;-200,0"
        frames, channel_count = self.generator.render_samples(
            mono_samples,
            sample_rate,
            "cardinal-cycle",
            self.generator.parse_mic_positions_cm(positions_text),
        )
        microphone_audio = np.frombuffer(frames, dtype="<i2").reshape(-1, 4)
        positions = self.module.parse_mic_positions_cm(positions_text)

        for expected_beam in range(4):
            start = expected_beam * segment_frames
            end = start + segment_frames
            beams = dict(
                self.module.beamform_cardinal(
                    microphone_audio[start:end], sample_rate, positions
                )
            )
            rms_by_beam = [
                np.sqrt(np.mean(np.square(beams[index][100:-100])))
                for index in range(4)
            ]
            self.assertEqual(int(np.argmax(rms_by_beam)), expected_beam)

        self.assertEqual(channel_count, 4)

    def test_short_window_metadata_covers_padded_three_seconds(self):
        model = self.module.BirdNETModel(None, [], [], [])
        with patch.object(
            self.module,
            "invoke_birdnet_top_labels",
            return_value=("Test bird", 0.9, None, "Test bird", 0.9, None),
        ):
            detections = self.module.collect_detections(
                2,
                np.ones(self.module.SAMPLE_RATE, dtype=np.float32),
                self.module.SAMPLE_RATE,
                model,
                None,
                None,
                None,
                self.module.date(2026, 1, 1),
            )

        self.assertEqual(detections[0].channel_index, 2)
        self.assertEqual(detections[0].start_frame, 0)
        self.assertEqual(detections[0].end_frame, self.module.WINDOW_FRAMES)

    def test_short_mono_clip_is_padded_to_three_seconds(self):
        source_path = self.module.INPUT_ROOT / "2026" / "01" / "01" / "sensos_2026-01-01T00-00-00Z.flac"
        audio = np.ones((self.module.SAMPLE_RATE, 4), dtype=np.int32)
        detection = self.module.Detection(
            channel_index=3,
            window_index=0,
            start_frame=0,
            end_frame=self.module.SAMPLE_RATE,
            max_score_start_frame=0,
            volume=0.1,
            label="Test bird",
            score=0.9,
            likely_score=None,
            weighted_label="Test bird",
            weighted_score=0.9,
            weighted_likely_score=None,
        )
        written_audio = []

        def capture_write(path, chunk, sample_rate, format):
            written_audio.append(np.array(chunk))
            Path(path).touch()

        with patch.object(self.module.sf, "write", side_effect=capture_write):
            self.module.write_detection_clips(
                source_path,
                {3: audio[:, 3]},
                self.module.SAMPLE_RATE,
                [detection],
            )

        self.assertEqual(written_audio[0].ndim, 1)
        self.assertEqual(len(written_audio[0]), self.module.WINDOW_FRAMES)
        np.testing.assert_array_equal(
            written_audio[0][: self.module.SAMPLE_RATE], audio[:, 3]
        )
        self.assertFalse(written_audio[0][self.module.SAMPLE_RATE :].any())


if __name__ == "__main__":
    unittest.main()
