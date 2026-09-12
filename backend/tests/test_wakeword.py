"""Wake word behaviour that R-1 (false positives) depends on."""

from __future__ import annotations

import numpy as np
import pytest

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.wakeword import (
    FRAME_SECONDS,
    ScriptedWakeWordDetector,
    model_filename,
    threshold_for,
)

FRAME = np.zeros(FRAME_SAMPLES, dtype=np.int16)


def run(detector: ScriptedWakeWordDetector, frames: int):
    return [detector.process(FRAME) for _ in range(frames)]


def test_higher_sensitivity_means_a_lower_threshold():
    assert threshold_for(1.0) < threshold_for(0.5) < threshold_for(0.0)


def test_threshold_never_reaches_the_useless_extremes():
    """0 would fire on silence, 1 would never fire at all."""
    assert 0.0 < threshold_for(0.0) < 1.0
    assert 0.0 < threshold_for(1.0) < 1.0
    assert threshold_for(-5) == threshold_for(0.0)
    assert threshold_for(5) == threshold_for(1.0)


def test_a_score_above_the_threshold_fires_once():
    detector = ScriptedWakeWordDetector(scores=[0.1, 0.2, 0.9])
    results = run(detector, 3)
    assert results[0] is None and results[1] is None
    assert results[2] is not None
    assert results[2].phrase == "Oye Chat"
    assert results[2].score == pytest.approx(0.9)


def test_a_score_below_the_threshold_never_fires():
    detector = ScriptedWakeWordDetector(scores=[0.5] * 10, sensitivity=0.0)
    assert all(result is None for result in run(detector, 10))


def test_the_tail_of_one_activation_does_not_fire_twice():
    """The phrase spans several frames; only the first may count."""
    detector = ScriptedWakeWordDetector(scores=[0.9, 0.9, 0.9, 0.9], refractory_seconds=1.0)
    results = run(detector, 4)
    assert sum(result is not None for result in results) == 1


def test_a_second_activation_fires_after_the_refractory_window():
    refractory = 0.4
    silent_frames = int(refractory / FRAME_SECONDS)
    scores = [0.9] + [0.0] * silent_frames + [0.9]
    detector = ScriptedWakeWordDetector(scores=scores, refractory_seconds=refractory)
    results = run(detector, len(scores))
    assert results[0] is not None
    assert results[-1] is not None


def test_zero_refractory_allows_consecutive_activations():
    detector = ScriptedWakeWordDetector(scores=[0.9, 0.9], refractory_seconds=0.0)
    assert all(result is not None for result in run(detector, 2))


def test_scores_are_kept_for_the_sensitivity_meter():
    """R-1 is measured in a real classroom, so the scores must be visible."""
    detector = ScriptedWakeWordDetector(scores=[0.1, 0.4, 0.9])
    run(detector, 3)
    assert detector.recent_scores() == pytest.approx([0.1, 0.4, 0.9])


def test_reset_clears_the_history_and_the_cooldown():
    detector = ScriptedWakeWordDetector(scores=[0.9], refractory_seconds=10.0)
    assert detector.process(FRAME) is not None
    detector.reset()
    assert detector.recent_scores() == []
    assert detector.process(FRAME) is not None


def test_model_filename_follows_the_phrase():
    assert model_filename("Oye Chat") == "oye_chat.onnx"
    assert model_filename("  Oye   Chat  ") == "oye_chat.onnx"
    assert model_filename("") == "wakeword.onnx"
