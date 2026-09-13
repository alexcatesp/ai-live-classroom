"""Training with the teacher's voice (D-12): takes, privacy and the model swap."""

from __future__ import annotations

import numpy as np
import pytest

from aiclassroom.audio.devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import model_filename, personal_model_path, phrase_model_path
from aiclassroom.voice.personal import PersonalResult
from aiclassroom.voice.session import (
    NEAR_MISS_PROMPTS,
    PHRASE_TAKES,
    Kind,
    TrainingState,
    VoiceBusy,
    VoiceNotReady,
    VoiceTrainingSession,
    capture_with_engine,
)
from aiclassroom.voice.takes import TAKE_SECONDS, RecordingRejected, analyse
from aiclassroom.voice.training import corpus_filename

# -- takes ---------------------------------------------------------------


def recording(
    speech_from: float = 0.8,
    speech_seconds: float = 0.9,
    amplitude: float = 0.3,
    noise: float = 0.001,
    total: float = TAKE_SECONDS,
) -> np.ndarray:
    rng = np.random.default_rng(1)
    samples = rng.normal(0, noise, int(total * CAPTURE_SAMPLE_RATE))
    start = int(speech_from * CAPTURE_SAMPLE_RATE)
    end = min(samples.size, start + int(speech_seconds * CAPTURE_SAMPLE_RATE))
    t = np.arange(end - start) / CAPTURE_SAMPLE_RATE
    samples[start:end] += amplitude * np.sin(2 * np.pi * 220 * t)
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16)


def test_a_clear_take_is_trimmed_to_the_speech():
    take = analyse(recording(speech_from=0.8, speech_seconds=0.9))
    # The speech plus a margin either side, not the whole three seconds.
    assert 0.9 <= take.seconds <= 1.3
    assert take.peak_level > 0.5


def test_silence_is_rejected_with_something_to_do_about_it():
    with pytest.raises(RecordingRejected, match="Acércate"):
        analyse(recording(amplitude=0.0))


def test_speech_buried_in_noise_is_rejected():
    with pytest.raises(RecordingRejected, match="ruido"):
        analyse(recording(amplitude=0.05, noise=0.08))


def test_a_phrase_cut_off_at_the_end_is_rejected():
    """Labelled as the phrase, half of it would teach the detector "oye"."""
    with pytest.raises(RecordingRejected, match="cortado"):
        analyse(recording(speech_from=2.5, speech_seconds=1.0))


def test_a_click_is_too_short_to_be_the_phrase():
    with pytest.raises(RecordingRejected, match="corto"):
        analyse(recording(speech_seconds=0.05))


def test_a_one_syllable_word_is_long_enough():
    """Reported in use: "Chat" alone was rejected, and only "Chat, chat" passed."""
    take = analyse(recording(speech_seconds=0.25))
    assert take.seconds >= 0.25


def test_quiet_consonants_at_the_edges_are_kept():
    """A soft onset and tail must not be trimmed off a short word."""
    rng = np.random.default_rng(3)
    samples = rng.normal(0, 0.001, int(TAKE_SECONDS * CAPTURE_SAMPLE_RATE))
    start = int(1.0 * CAPTURE_SAMPLE_RATE)
    # 80 ms of soft "ch", 160 ms of loud vowel, 80 ms of soft "t".
    for offset, seconds, amplitude in ((0.0, 0.08, 0.04), (0.08, 0.16, 0.4), (0.24, 0.08, 0.04)):
        begin = start + int(offset * CAPTURE_SAMPLE_RATE)
        t = np.arange(int(seconds * CAPTURE_SAMPLE_RATE)) / CAPTURE_SAMPLE_RATE
        samples[begin : begin + t.size] += amplitude * np.sin(2 * np.pi * 300 * t)
    take = analyse((np.clip(samples, -1, 1) * 32767).astype(np.int16))
    # The whole word plus the margins, not just the vowel.
    assert take.seconds >= 0.3 + 0.2 - 0.08


def test_talking_the_whole_time_is_too_long():
    with pytest.raises(RecordingRejected, match="largo"):
        analyse(recording(speech_from=0.1, speech_seconds=2.7))


# -- capture through the engine --------------------------------------------


def test_capture_collects_the_requested_length_and_closes_the_microphone():
    import threading

    engine = FakeAudioEngine()
    capture = capture_with_engine(lambda _settings: engine)

    def speak():
        while not engine.is_capturing:
            pass
        engine.feed(np.zeros(FRAME_SAMPLES * 40, dtype=np.int16))

    feeder = threading.Thread(target=speak)
    feeder.start()
    samples = capture(None, 1.0)
    feeder.join()

    assert samples.size == CAPTURE_SAMPLE_RATE
    assert engine.is_capturing is False


# -- the session -----------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _settings, _seconds):
        self.calls += 1
        return recording()


def fake_trainer(**kwargs) -> PersonalResult:
    destination = kwargs["destination"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"modelo nuevo")
    kwargs["progress"](0.5, "Entrenando")
    fake_trainer.last_call = kwargs
    return PersonalResult(5, 5, 0, 5, 0.98, 0.001, 12.0)


@pytest.fixture
def ready_models(paths):
    """A models folder with what training needs besides the recordings."""
    (paths.models_dir / model_filename("Oye Chat")).write_bytes(b"modelo original")
    (paths.models_dir / corpus_filename(model_filename("Oye Chat"))).write_bytes(b"corpus")
    return paths.models_dir


@pytest.fixture
def session(store, ready_models, monkeypatch):
    # Training availability also checks the packages; they may be absent here.
    monkeypatch.setattr(
        "aiclassroom.voice.session.training_available", lambda *_args: None
    )
    return VoiceTrainingSession(
        store=store,
        capture=Recorder(),
        microphone_in_use=lambda: False,
        trainer=fake_trainer,
    )


def record_everything(session: VoiceTrainingSession) -> None:
    for slot in range(PHRASE_TAKES):
        session.record(Kind.PHRASE, slot)
    for slot in range(len(NEAR_MISS_PROMPTS)):
        session.record(Kind.NEAR_MISS, slot)


def test_takes_are_listed_as_they_are_recorded(session):
    session.record(Kind.PHRASE, 2)
    status = session.status()
    assert status["phrase_takes"][2] is not None
    assert status["phrase_takes"][0] is None
    assert status["near_miss_prompts"] == list(NEAR_MISS_PROMPTS)


def test_a_take_can_be_redone(session):
    session.record(Kind.PHRASE, 0)
    session.forget(Kind.PHRASE, 0)
    assert session.status()["phrase_takes"][0] is None


def test_the_microphone_is_not_taken_from_a_running_class(store, ready_models):
    session = VoiceTrainingSession(
        store=store, capture=Recorder(), microphone_in_use=lambda: True, trainer=fake_trainer
    )
    with pytest.raises(VoiceBusy, match="Pausa"):
        session.record(Kind.PHRASE, 0)


def test_training_needs_every_take(session):
    session.record(Kind.PHRASE, 0)
    with pytest.raises(VoiceNotReady, match="Faltan 9"):
        session.start_training()


def test_the_recordings_are_gone_once_training_ends(session):
    """The teacher chose: recordings are used to train and then discarded."""
    record_everything(session)
    session.start_training()
    session.wait(5)

    status = session.status()
    assert status["state"] == TrainingState.READY
    assert all(take is None for take in status["phrase_takes"])
    assert all(take is None for take in status["near_miss_takes"])


def test_the_recordings_are_gone_even_when_training_fails(store, ready_models, monkeypatch):
    monkeypatch.setattr("aiclassroom.voice.session.training_available", lambda *_a: None)

    def broken(**_kwargs):
        raise RuntimeError("sin memoria")

    session = VoiceTrainingSession(
        store=store, capture=Recorder(), microphone_in_use=lambda: False, trainer=broken
    )
    record_everything(session)
    session.start_training()
    session.wait(5)

    status = session.status()
    assert status["state"] == TrainingState.FAILED
    assert "sin memoria" in status["error"]
    assert all(take is None for take in status["phrase_takes"])


def test_the_recordings_never_touch_the_disk(session, paths):
    record_everything(session)
    written = [path for path in paths.root.rglob("*") if path.suffix in {".wav", ".npy", ".raw"}]
    assert written == []


def test_the_trainer_gets_the_teachers_takes_and_the_current_threshold(session):
    record_everything(session)
    session.start_training()
    session.wait(5)

    call = fake_trainer.last_call
    assert len(call["phrase_takes"]) == PHRASE_TAKES
    assert len(call["near_miss_takes"]) == len(NEAR_MISS_PROMPTS)
    assert 0.0 < call["threshold"] < 1.0


def test_a_trained_model_is_only_used_once_accepted(session, ready_models):
    record_everything(session)
    session.start_training()
    session.wait(5)

    original = ready_models / model_filename("Oye Chat")
    assert phrase_model_path(ready_models, "Oye Chat") == original

    session.accept()
    personal = personal_model_path(ready_models, "Oye Chat")
    assert phrase_model_path(ready_models, "Oye Chat") == personal
    assert personal.read_bytes() == b"modelo nuevo"
    # The model that ships is never overwritten.
    assert original.read_bytes() == b"modelo original"
    assert session.status()["personal_model"] is True


def test_a_discarded_model_leaves_nothing_behind(session, ready_models):
    record_everything(session)
    session.start_training()
    session.wait(5)
    session.discard()

    assert phrase_model_path(ready_models, "Oye Chat") == ready_models / "oye_chat.onnx"
    assert not (ready_models / "personal" / "pending").exists()
    assert session.status()["state"] == TrainingState.IDLE


def test_going_back_to_the_original_model_is_deleting_a_file(session, ready_models):
    record_everything(session)
    session.start_training()
    session.wait(5)
    session.accept()

    session.restore_original()
    assert phrase_model_path(ready_models, "Oye Chat") == ready_models / "oye_chat.onnx"
    assert session.status()["personal_model"] is False


def test_nothing_can_be_accepted_before_training(session):
    with pytest.raises(VoiceNotReady):
        session.accept()


def test_a_pending_model_from_an_unfinished_run_is_not_kept(store, ready_models):
    pending = ready_models / "personal" / "pending" / "oye_chat.onnx"
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"de una ejecucion anterior")

    VoiceTrainingSession(store=store, capture=Recorder(), microphone_in_use=lambda: False)
    assert not pending.exists()


def test_training_is_unavailable_without_the_base_corpus(store, paths):
    session = VoiceTrainingSession(
        store=store, capture=Recorder(), microphone_in_use=lambda: False
    )
    assert "corpus" in session.status()["unavailable_reason"]
