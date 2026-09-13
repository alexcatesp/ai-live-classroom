"""End-to-end validation of the Phase 0 chain.

Everything else in the suite runs against test doubles, which is what lets it
run anywhere. This file does the opposite: real openWakeWord models, real
speech, the real listener, the real state machine and the real HTTP API, with
only the sound card faked -- and the sound card is faked by feeding it the same
frames PortAudio would have delivered.

It needs two things the repository does not carry: the models, and espeak-ng to
speak with. Both are set up by:

    python scripts/fetch_wakeword_runtime.py --output <models>
    python scripts/train_wakeword.py --phrase "Oye Chat" --models <models>
    AICLASSROOM_TEST_MODELS=<models> pytest backend/tests/test_end_to_end.py

Without them the file skips, so the everyday suite stays offline and fast.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from aiclassroom.api.app import TOKEN_HEADER, AppContext, create_app
from aiclassroom.audio.devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import create_detector, missing_base_models, model_filename
from aiclassroom.config.settings import Settings
from aiclassroom.diagnostics.runner import DiagnosticsRunner
from aiclassroom.session.controller import SessionController
from aiclassroom.session.state import State

MODELS = Path(os.environ.get("AICLASSROOM_TEST_MODELS", "/nonexistent"))
PHRASE = "Oye Chat"

pytestmark = [
    pytest.mark.skipif(
        bool(missing_base_models(MODELS)) or not (MODELS / model_filename(PHRASE)).exists(),
        reason="define AICLASSROOM_TEST_MODELS con los modelos de openWakeWord y la frase",
    ),
    pytest.mark.skipif(
        shutil.which("espeak-ng") is None, reason="hace falta espeak-ng para sintetizar voz"
    ),
]


# -- speech ---------------------------------------------------------------


def say(text: str, voice: str = "es", speed: int = 160) -> np.ndarray:
    """Real synthesized speech at the rate the detector consumes."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "clip.wav"
        subprocess.run(
            ["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(path), text],
            check=True,
            capture_output=True,
        )
        with wave.open(str(path), "rb") as clip:
            rate = clip.getframerate()
            samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)

    if rate != CAPTURE_SAMPLE_RATE:
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(rate, CAPTURE_SAMPLE_RATE)
        samples = resample_poly(samples, CAPTURE_SAMPLE_RATE // divisor, rate // divisor)
    return np.clip(samples, -32768, 32767).astype(np.int16)


def with_silence(samples: np.ndarray, before: float = 2.2, after: float = 0.6) -> np.ndarray:
    """Pad so the detector has the history it always looks back over."""
    rng = np.random.default_rng(0)

    def room_tone(seconds: float) -> np.ndarray:
        return (rng.normal(0, 60, int(seconds * CAPTURE_SAMPLE_RATE))).astype(np.int16)

    padded = np.concatenate([room_tone(before), samples, room_tone(after)])
    # Whole frames only, exactly as PortAudio delivers them.
    usable = padded.size - (padded.size % FRAME_SAMPLES)
    return padded[:usable]


# -- fixtures -------------------------------------------------------------


@pytest.fixture
def real_controller(store, machine):
    """A controller wired to the real detector, with only the sound card faked."""
    engine = FakeAudioEngine()
    settings = Settings(wake_phrase=PHRASE, wake_sensitivity=0.5)
    store.save(settings)

    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda current: create_detector(
            models_dir=MODELS,
            phrase=current.wake_phrase,
            sensitivity=current.wake_sensitivity,
            refractory_seconds=current.wake_refractory_seconds,
            vad_threshold=current.wake_vad_threshold,
            confirmation_frames=current.wake_confirmation_frames,
        ),
    )
    return controller, engine


# -- the chain ------------------------------------------------------------


def test_the_real_detector_wakes_on_the_real_phrase(real_controller):
    """The whole point of Phase 0: somebody says it, the assistant wakes up."""
    controller, engine = real_controller
    controller.prepare()
    assert controller.start_class() is State.PASSIVE_LISTENING

    engine.feed(with_silence(say(PHRASE)))

    assert controller.machine.state is State.ACTIVATED
    assert controller.listening_status().activations == 1


def test_the_voice_gate_and_the_model_both_load_for_real(real_controller):
    controller, _engine = real_controller
    controller.prepare()
    controller.start_class()

    status = controller.listening_status()
    assert status.vad_enabled is True
    assert status.phrase == PHRASE
    assert status.confirmation_frames == 2


def test_room_tone_alone_never_wakes_it(real_controller):
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()

    rng = np.random.default_rng(1)
    quiet = (rng.normal(0, 80, FRAME_SAMPLES * 100)).astype(np.int16)
    engine.feed(quiet)

    assert controller.machine.state is State.PASSIVE_LISTENING
    assert controller.listening_status().activations == 0


@pytest.mark.parametrize(
    "sentence",
    [
        "Oye, Marta, baja la persiana por favor.",
        "¿Alguien ha usado un chat de soporte?",
        "Vamos a ver cómo funciona una petición HTTP.",
    ],
)
def test_ordinary_teaching_does_not_wake_it(real_controller, sentence):
    """Near misses matter more than silence: "oye" and "chat" appear in class."""
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()

    engine.feed(with_silence(say(sentence)))

    assert controller.machine.state is State.PASSIVE_LISTENING, sentence


def test_a_paused_class_hears_nothing(real_controller):
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()
    controller.pause()

    assert engine.is_capturing is False
    assert controller.machine.is_microphone_active is False


def test_the_session_survives_a_full_cycle(real_controller):
    """Wake, answer, go quiet, wake again -- the loop a class actually runs."""
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()

    engine.feed(with_silence(say(PHRASE)))
    assert controller.machine.state is State.ACTIVATED

    # Phase 1 will do the talking; here the turn is driven by hand.
    from aiclassroom.session.state import Event

    for event in (
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
        Event.RESPONSE_FINISHED,
    ):
        controller.machine.dispatch(event)
    assert controller.machine.state is State.PASSIVE_LISTENING

    engine.feed(with_silence(say(PHRASE, voice="es-419", speed=190)))
    assert controller.machine.state is State.ACTIVATED
    assert controller.listening_status().activations == 2


# -- over HTTP, the way the interface sees it -----------------------------


def test_a_whole_session_over_the_api(store, machine):
    """Everything a teacher does in Phase 0, through the real HTTP surface."""
    engine = FakeAudioEngine()
    store.save(Settings(wake_phrase=PHRASE))
    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda current: create_detector(
            models_dir=MODELS, phrase=current.wake_phrase
        ),
    )
    context = AppContext(
        store=store,
        controller=controller,
        runner=DiagnosticsRunner(store=store),
        token="e2e", conversation=False,
    )

    with TestClient(create_app(context)) as client:
        client.headers.update({TOKEN_HEADER: "e2e"})

        assert client.get("/api/state").json()["state"] == "IDLE"
        assert client.post("/api/class/prepare").json()["state"] == "READY"

        started = client.post("/api/class/start").json()
        assert started["state"] == "PASSIVE_LISTENING"
        assert started["microphone_active"] is True

        engine.feed(with_silence(say(PHRASE)))

        state = client.get("/api/state").json()
        assert state["state"] == "ACTIVATED"

        listening = client.get("/api/listening").json()
        assert listening["activations"] == 1
        assert listening["vad_enabled"] is True

        assert client.post("/api/class/stop").json()["state"] == "STOPPED"
        assert engine.is_capturing is False


def test_the_wake_word_reaches_the_interface_as_an_event(store, machine):
    """The interface learns about an activation without polling for it."""
    engine = FakeAudioEngine()
    store.save(Settings(wake_phrase=PHRASE))
    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda current: create_detector(
            models_dir=MODELS, phrase=current.wake_phrase
        ),
    )
    context = AppContext(
        store=store,
        controller=controller,
        runner=DiagnosticsRunner(store=store),
        token="e2e",
        conversation=False,
    )

    with TestClient(create_app(context)) as client:
        client.headers.update({TOKEN_HEADER: "e2e"})
        with client.websocket_connect("/ws/events?token=e2e") as socket:
            socket.receive_json()  # the opening snapshot

            client.post("/api/class/prepare")
            client.post("/api/class/start")
            engine.feed(with_silence(say(PHRASE)))

            seen = [socket.receive_json() for _ in range(5)]

    kinds = [event["type"] for event in seen]
    assert "wakeword" in kinds

    activation = next(event for event in seen if event["type"] == "wakeword")
    assert activation["payload"]["phrase"] == PHRASE
    assert activation["payload"]["score"] > 0.0


# -- what the detector must not have learnt -------------------------------


@pytest.mark.parametrize("utterance", ["mesa", "el perro", "buenos días", "ocho", "hola"])
def test_a_short_unrelated_utterance_does_not_wake_it(real_controller, utterance):
    """The failure this caught: a model that learnt "brief speech between
    silences" instead of the phrase, and scored 1.0 on "mesa".

    Positives are short utterances surrounded by room tone, so unless the
    negatives are laid out the same way, isolation is the easiest signal for
    the classifier to key on -- and it will.
    """
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()

    engine.feed(with_silence(say(utterance)))

    assert controller.machine.state is State.PASSIVE_LISTENING, utterance


def test_the_phrase_is_heard_inside_continuous_speech(real_controller):
    """A teacher does not pause before saying it."""
    controller, engine = real_controller
    controller.prepare()
    controller.start_class()

    sentence = say("Vamos a ver el ejemplo. Oye Chat, explica esto.")
    engine.feed(with_silence(sentence))

    assert controller.machine.state is State.ACTIVATED
