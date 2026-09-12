"""The measurement harness that produces the evidence for risk R-1.

It only earns trust if its own arithmetic is checked: a tool that reports the
wrong false positive rate is worse than no tool, because the number looks
authoritative.
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.wakeword import ScriptedWakeWordDetector

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_wakeword.py"


def load_harness():
    specification = importlib.util.spec_from_file_location("measure_wakeword", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    sys.modules["measure_wakeword"] = module
    specification.loader.exec_module(module)
    return module


harness = load_harness()


def write_wav(path: Path, seconds: float, rate: int = 16_000, channels: int = 1) -> Path:
    frames = int(seconds * rate)
    samples = np.zeros(frames * channels, dtype=np.int16)
    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(samples.tobytes())
    return path


# -- reading audio --------------------------------------------------------


def test_a_clip_is_cut_into_the_frames_the_detector_expects(tmp_path):
    path = write_wav(tmp_path / "clip.wav", seconds=1.0)
    frames = list(harness.read_frames(path))

    assert len(frames) == 12  # 1 s / 80 ms, the partial tail dropped
    assert all(frame.size == FRAME_SAMPLES for frame in frames)


def test_a_partial_tail_is_never_fed_to_the_detector(tmp_path):
    # 0.5 frames of audio: openWakeWord would reject a short block.
    path = write_wav(tmp_path / "corto.wav", seconds=FRAME_SAMPLES / 2 / 16_000)
    assert list(harness.read_frames(path)) == []


def test_a_stereo_recording_is_mixed_down_rather_than_refused(tmp_path):
    """A classroom recorded in stereo is still a perfectly good negative."""
    path = write_wav(tmp_path / "estereo.wav", seconds=1.0, channels=2)
    frames = list(harness.read_frames(path))

    assert len(frames) == 12
    assert all(frame.size == FRAME_SAMPLES for frame in frames)


def test_the_wrong_sample_rate_is_explained_with_the_command_to_fix_it(tmp_path):
    path = write_wav(tmp_path / "44k.wav", seconds=0.5, rate=44_100)

    with pytest.raises(harness.AudioFormatError, match="ffmpeg"):
        list(harness.read_frames(path))


def test_clip_duration_is_read_from_the_header(tmp_path):
    path = write_wav(tmp_path / "clip.wav", seconds=2.5)
    assert harness.clip_seconds(path) == pytest.approx(2.5)


# -- the arithmetic -------------------------------------------------------


def test_false_activations_are_reported_per_hour():
    measurement = harness.Measurement(
        sensitivity=0.5,
        vad_threshold=0.5,
        confirmation_frames=2,
        activations=3,
        clips_with_activation=2,
        clips=4,
        audio_seconds=600.0,  # ten minutes
    )
    assert measurement.activations_per_hour == pytest.approx(18.0)
    assert measurement.clip_hit_rate == pytest.approx(0.5)


def test_an_empty_measurement_does_not_divide_by_zero():
    empty = harness.Measurement(0.5, 0.5, 2, 0, 0, 0, 0.0)
    assert empty.activations_per_hour == 0.0
    assert empty.clip_hit_rate == 0.0


# -- running the detector over clips -------------------------------------


def test_activations_and_duration_are_accumulated_across_clips(tmp_path):
    clips = [write_wav(tmp_path / f"{index}.wav", seconds=1.0) for index in range(3)]
    # One activation in each clip: fires on the first frame, then the
    # refractory window covers the rest of the second.
    detector = ScriptedWakeWordDetector(scores=[0.9] * 12, refractory_seconds=2.0)

    activations, clips_with_activation, seconds = harness.run_detector(detector, clips)

    assert activations == 3
    assert clips_with_activation == 3
    assert seconds == pytest.approx(3.0)


def test_each_clip_starts_from_a_clean_detector(tmp_path):
    """Without a reset between clips, one clip's refractory window would hide
    the activation at the start of the next, understating the false rate."""
    clips = [write_wav(tmp_path / f"{index}.wav", seconds=0.4) for index in range(2)]
    detector = ScriptedWakeWordDetector(scores=[0.9] * 5, refractory_seconds=30.0)

    activations, clips_with_activation, _ = harness.run_detector(detector, clips)

    assert activations == 2
    assert clips_with_activation == 2


def test_silence_produces_no_activations(tmp_path):
    clips = [write_wav(tmp_path / "silencio.wav", seconds=2.0)]
    detector = ScriptedWakeWordDetector(scores=[])

    activations, clips_with_activation, seconds = harness.run_detector(detector, clips)

    assert activations == 0
    assert clips_with_activation == 0
    assert seconds == pytest.approx(2.0)


# -- the command line -----------------------------------------------------


def test_it_refuses_to_pretend_it_measured_something(capsys):
    """No recordings means no measurement; saying so is the useful answer."""
    assert harness.main(["--models", "data/models"]) == 1
    assert "graba una clase" in capsys.readouterr().err


def test_a_missing_folder_is_named(tmp_path, capsys):
    assert harness.main(["--negatives", str(tmp_path / "no-existe")]) == 1
    assert "No existe la carpeta" in capsys.readouterr().err


def test_the_default_sweep_covers_both_ends_of_the_sensitivity_range():
    arguments = harness.parse_args([])
    assert min(arguments.sensitivities) <= 0.2
    assert max(arguments.sensitivities) >= 0.8
    assert arguments.confirmation_frames == 2
    assert arguments.vad_threshold == 0.5
