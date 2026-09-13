"""The controller is what the interface actually drives."""

from __future__ import annotations

import pytest

from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import WakeWordUnavailable
from aiclassroom.session.controller import ClassNotReady, SessionController
from aiclassroom.session.state import InvalidTransition, State


def test_preparing_moves_from_idle_to_ready(controller: SessionController):
    assert controller.prepare() is State.READY


def test_starting_a_class_opens_the_microphone(
    controller: SessionController, engine: FakeAudioEngine
):
    controller.prepare()
    assert controller.start_class() is State.PASSIVE_LISTENING
    assert engine.is_capturing
    assert controller.machine.is_microphone_active


def test_a_class_cannot_start_before_it_is_prepared(controller: SessionController):
    with pytest.raises(InvalidTransition):
        controller.start_class()


def test_a_missing_wake_word_model_fails_the_class_with_an_explanation(store, machine, engine):
    """D-04: no model, no passive listening -- and the teacher is told why."""

    def broken_detector(_settings):
        raise WakeWordUnavailable("No se encontró el modelo para 'Oye Chat'.")

    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=broken_detector,
    )
    controller.prepare()

    with pytest.raises(ClassNotReady, match="modelo"):
        controller.start_class()

    assert controller.machine.state is State.ERROR
    # The failure must not leave the microphone looking open (spec section 19).
    assert controller.machine.is_microphone_active is False
    assert engine.is_capturing is False


def test_a_microphone_failure_never_leaves_the_state_claiming_to_listen(
    store, machine, detector, engine: FakeAudioEngine
):
    engine.fail_on_capture = "El micrófono está en uso por otra aplicación."
    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: detector,
    )
    controller.prepare()

    with pytest.raises(ClassNotReady, match="micrófono"):
        controller.start_class()

    assert controller.machine.state is State.ERROR
    assert controller.machine.is_microphone_active is False


def test_pausing_closes_the_microphone_and_resuming_reopens_it(
    controller: SessionController, engine: FakeAudioEngine
):
    controller.prepare()
    controller.start_class()

    assert controller.pause() is State.PAUSED
    assert engine.is_capturing is False

    assert controller.resume() is State.PASSIVE_LISTENING
    assert engine.is_capturing is True


def test_start_on_a_paused_class_resumes_it(controller: SessionController):
    """The interface shows one button; pressing it twice must not be an error."""
    controller.prepare()
    controller.start_class()
    controller.pause()
    assert controller.start_class() is State.PASSIVE_LISTENING


def test_stopping_releases_the_devices(controller: SessionController, engine: FakeAudioEngine):
    controller.prepare()
    controller.start_class()
    assert controller.stop() is State.STOPPED
    assert engine.is_capturing is False


def test_a_session_can_be_prepared_again_after_stopping(controller: SessionController):
    controller.prepare()
    controller.start_class()
    controller.stop()
    assert controller.prepare() is State.READY
    assert controller.start_class() is State.PASSIVE_LISTENING


def test_recovering_from_an_error_returns_to_ready(store, machine, engine):
    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: (_ for _ in ()).throw(
            WakeWordUnavailable("sin modelo")
        ),
    )
    controller.prepare()
    with pytest.raises(ClassNotReady):
        controller.start_class()

    assert controller.recover() is State.READY


def test_an_activation_is_reported_through_the_controller(
    controller: SessionController, engine: FakeAudioEngine, frames
):
    seen = []
    controller.detection_subscribers.append(seen.append)
    controller.prepare()
    controller.start_class()

    engine.feed(frames(3))  # the scripted detector fires on the third frame

    assert len(seen) == 1
    assert seen[0].phrase == "Oye Chat"
    assert controller.machine.state is State.ACTIVATED


def test_listening_status_exposes_what_the_meter_needs(
    controller: SessionController, engine: FakeAudioEngine, frames
):
    """The teacher tunes the sensitivity against these numbers (R-1)."""
    controller.prepare()
    controller.start_class()
    engine.feed(frames(3))

    status = controller.listening_status()
    assert status.listening is True
    assert status.activations == 1
    assert status.phrase == "Oye Chat"
    assert status.threshold == pytest.approx(0.65)
    assert len(status.recent_scores) == 3
    assert status.frames_processed == 3
    # Silence in, silence on the level bar; no voice filter, no speech reading.
    assert status.input_level == 0.0
    assert status.speech_probability is None


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_an_activation_returns_to_listening_so_the_next_one_is_heard(
    store, machine, engine: FakeAudioEngine, frames
):
    """Reported from a real classroom: the first "Oye Chat" was also the last.

    Phase 0 has no conversation, so nothing moved ACTIVATED onwards and every
    later detection was discarded as arriving in the wrong state.
    """
    from aiclassroom.audio.wakeword import ScriptedWakeWordDetector

    # Two activations, far enough apart to clear the refractory window.
    scores = [0.9] + [0.0] * 40 + [0.9]
    detector = ScriptedWakeWordDetector(scores=scores, refractory_seconds=0.5)
    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: detector,
        activation_hold_seconds=0.05,
    )
    controller.prepare()
    controller.start_class()

    engine.feed(frames(1))
    assert controller.machine.state is State.ACTIVATED
    assert _wait_for(lambda: controller.machine.state is State.PASSIVE_LISTENING)

    engine.feed(frames(41))
    assert controller.listening_status().activations == 2


def test_the_activation_is_held_long_enough_to_be_seen(
    controller: SessionController, engine, frames
):
    controller.prepare()
    controller.start_class()
    engine.feed(frames(3))

    # Default hold: the interface polls every half second and must catch it.
    assert controller.machine.state is State.ACTIVATED
    controller.stop()


def test_pausing_during_an_activation_is_not_undone_by_the_timer(
    store, machine, engine: FakeAudioEngine, detector, frames
):
    import time

    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: detector,
        activation_hold_seconds=0.05,
    )
    controller.prepare()
    controller.start_class()
    engine.feed(frames(3))
    controller.pause()

    time.sleep(0.15)
    assert controller.machine.state is State.PAUSED


def test_stopping_during_an_activation_stays_stopped(
    store, machine, engine: FakeAudioEngine, detector, frames
):
    import time

    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: detector,
        activation_hold_seconds=0.05,
    )
    controller.prepare()
    controller.start_class()
    engine.feed(frames(3))
    controller.stop()

    time.sleep(0.15)
    assert controller.machine.state is State.STOPPED


def test_listening_status_before_any_class_is_empty(controller: SessionController):
    status = controller.listening_status()
    assert status.listening is False
    assert status.activations == 0
    assert status.phrase is None


def test_the_engine_is_built_from_the_saved_settings(store, machine, detector):
    from aiclassroom.config.settings import Settings

    store.save(Settings(input_device="USB Audio Device", output_device="Altavoces"))
    seen: dict = {}

    def engine_factory(settings):
        seen["input"] = settings.input_device
        seen["output"] = settings.output_device
        return FakeAudioEngine()

    controller = SessionController(
        store=store,
        machine=machine,
        engine_factory=engine_factory,
        detector_factory=lambda _settings: detector,
    )
    controller.prepare()
    controller.start_class()

    assert seen == {"input": "USB Audio Device", "output": "Altavoces"}


def test_resuming_a_class_whose_microphone_vanished_fails_cleanly(
    controller: SessionController, engine: FakeAudioEngine
):
    """Unplugging the USB microphone during a pause is a real classroom event."""
    controller.prepare()
    controller.start_class()
    controller.pause()
    engine.fail_on_capture = "El micrófono se ha desconectado."

    with pytest.raises(ClassNotReady):
        controller.resume()

    assert controller.machine.state is State.ERROR
    assert controller.machine.is_microphone_active is False


def test_acknowledging_an_interruption_returns_to_listening(
    controller: SessionController, engine: FakeAudioEngine, frames
):
    """After a cancelled response the assistant goes quiet again (spec section 8)."""
    from aiclassroom.session.state import Event

    controller.prepare()
    controller.start_class()
    for event in (
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
        Event.INTERRUPT,
    ):
        controller.machine.dispatch(event)

    assert controller.acknowledge_interruption() is State.PASSIVE_LISTENING
    assert controller.machine.is_microphone_active is True
