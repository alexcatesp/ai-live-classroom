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


# -- integration with the real engine ------------------------------------
#
# These run only where the openWakeWord models are actually present, which the
# build machine arranges with scripts/fetch_wakeword_runtime.py. The CI stays
# offline, so they are skipped there; locally they are what proves the wiring
# to the library is real and not just mocked.

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from aiclassroom.audio.wakeword import (  # noqa: E402
    OpenWakeWordDetector,
    WakeWordUnavailable,
    base_model_paths,
    create_detector,
    missing_base_models,
)

REAL_MODELS = Path(os.environ.get("AICLASSROOM_TEST_MODELS", "/nonexistent"))
needs_real_models = pytest.mark.skipif(
    bool(missing_base_models(REAL_MODELS)),
    reason="define AICLASSROOM_TEST_MODELS con los modelos de openWakeWord",
)


def test_base_models_are_looked_for_in_the_shipped_subfolder(tmp_path):
    melspec, embedding, vad = base_model_paths(tmp_path)
    assert melspec == tmp_path / "openwakeword" / "melspectrogram.onnx"
    assert embedding == tmp_path / "openwakeword" / "embedding_model.onnx"
    assert vad == tmp_path / "openwakeword" / "silero_vad.onnx"


def test_a_missing_feature_extractor_is_explained_not_downloaded(tmp_path):
    """Silently downloading mid-class is the failure mode being avoided."""
    (tmp_path / "oye_chat.onnx").write_bytes(b"x")

    with pytest.raises(WakeWordUnavailable, match="melspectrogram.onnx"):
        create_detector(tmp_path, "Oye Chat")


def test_a_missing_phrase_model_is_explained(wakeword_models):
    with pytest.raises(WakeWordUnavailable, match="train_wakeword"):
        create_detector(wakeword_models, "Otra Frase")


@needs_real_models
def test_the_real_engine_scores_frames_without_touching_the_network():
    """Loads openWakeWord with the shipped models and runs inference on silence."""
    phrase_models = sorted(REAL_MODELS.glob("*.onnx"))
    assert phrase_models, "no hay ningún modelo de frase para la prueba"

    melspec, embedding, vad = base_model_paths(REAL_MODELS)
    detector = OpenWakeWordDetector(
        model_path=phrase_models[0],
        phrase="Prueba",
        melspec_model=melspec,
        embedding_model=embedding,
        vad_model=vad,
    )
    assert detector.vad_enabled, "el filtro de voz no se activó"

    # Enough frames to fill the model's internal buffers.
    for _ in range(25):
        detector.process(FRAME)

    scores = detector.recent_scores()
    assert len(scores) == 25
    assert all(0.0 <= score <= 1.0 for score in scores)
    # Silence must never activate the assistant.
    assert max(scores) < detector.threshold


# -- defences against false positives (risk R-1) -------------------------


def test_a_single_frame_spike_does_not_wake_the_assistant():
    """Noise produces one-frame spikes; the wake phrase lasts longer."""
    detector = ScriptedWakeWordDetector(scores=[0.9, 0.0, 0.9, 0.0], confirmation_frames=2)
    assert all(result is None for result in run(detector, 4))


def test_consecutive_frames_above_the_threshold_do_wake_it():
    detector = ScriptedWakeWordDetector(scores=[0.9, 0.92], confirmation_frames=2)
    results = run(detector, 2)
    assert results[0] is None
    assert results[1] is not None


def test_the_reported_score_is_the_peak_of_the_confirmed_run():
    """The meter should show how strong the activation was, not its last frame."""
    detector = ScriptedWakeWordDetector(scores=[0.95, 0.70], confirmation_frames=2)
    detection = run(detector, 2)[1]
    assert detection is not None
    assert detection.score == pytest.approx(0.95)


def test_a_broken_run_starts_counting_again():
    detector = ScriptedWakeWordDetector(
        scores=[0.9, 0.1, 0.9, 0.1, 0.9], confirmation_frames=3
    )
    assert all(result is None for result in run(detector, 5))


def test_requiring_more_frames_rejects_a_shorter_burst():
    burst = [0.9, 0.9, 0.9]
    three = ScriptedWakeWordDetector(burst, confirmation_frames=3)
    four = ScriptedWakeWordDetector(burst, confirmation_frames=4)

    assert any(result is not None for result in run(three, 3))
    assert all(result is None for result in run(four, 3))


def test_confirmation_cannot_be_switched_off_below_one_frame():
    detector = ScriptedWakeWordDetector(scores=[0.9], confirmation_frames=0)
    assert detector.confirmation_frames == 1
    assert detector.process(FRAME) is not None


def test_the_refractory_window_clears_a_run_in_progress():
    """A run interrupted by an activation must not carry over into the next."""
    detector = ScriptedWakeWordDetector(
        scores=[0.9, 0.9, 0.9], confirmation_frames=2, refractory_seconds=0.16
    )
    results = run(detector, 3)
    assert results[1] is not None
    assert results[2] is None


def test_the_voice_gate_is_reported_so_the_interface_can_show_it():
    assert ScriptedWakeWordDetector().vad_enabled is False


def test_a_missing_vad_model_degrades_instead_of_failing(tmp_path, caplog):
    """Better a noisier detector than a class that cannot start."""
    from aiclassroom.audio.wakeword import OpenWakeWordDetector

    detector = OpenWakeWordDetector.__new__(OpenWakeWordDetector)
    detector._model = type("FakeModel", (), {})()
    detector._attach_vad(tmp_path / "no-existe.onnx", 0.5)

    assert detector.vad_enabled is False


@needs_real_models
def test_the_voice_gate_loads_with_the_real_engine():
    phrase_models = sorted(REAL_MODELS.glob("*.onnx"))
    melspec, embedding, vad = base_model_paths(REAL_MODELS)

    detector = OpenWakeWordDetector(
        model_path=phrase_models[0],
        phrase="Prueba",
        melspec_model=melspec,
        embedding_model=embedding,
        vad_model=vad,
        vad_threshold=0.5,
    )

    assert detector.vad_enabled is True
    for _ in range(15):
        detector.process(FRAME)
    # Silence carries no speech, so the gate holds every score at zero.
    assert max(detector.recent_scores()) == 0.0
