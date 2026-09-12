"""Passive listening: the privacy guarantee and the interruption rule."""

from __future__ import annotations

import numpy as np

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.listener import WakeWordListener
from aiclassroom.audio.wakeword import ScriptedWakeWordDetector
from aiclassroom.session.state import Event, SessionStateMachine, State


def listening_machine() -> SessionStateMachine:
    machine = SessionStateMachine()
    for event in (Event.CREATE_SESSION, Event.SESSION_PREPARED, Event.START_CLASS):
        machine.dispatch(event)
    return machine


def test_a_wake_word_activates_the_session(engine: FakeAudioEngine, frames):
    machine = listening_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.0, 0.9]), machine)
    listener.start()
    engine.feed(frames(2))

    assert machine.state is State.ACTIVATED
    assert listener.stats.activations == 1
    assert listener.stats.last_activation_at is not None


def test_silence_never_activates_and_never_leaves_the_machine(engine: FakeAudioEngine, frames):
    """Spec section 16: no audio leaves the equipment during passive listening.

    The listener holds no network client at all, so the strongest thing this can
    assert is that a hundred frames of silence produce no event of any kind.
    """
    machine = listening_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([]), machine)
    listener.start()
    engine.feed(frames(100))

    assert machine.state is State.PASSIVE_LISTENING
    assert listener.stats.activations == 0
    assert listener.stats.frames_processed == 100
    assert listener.stats.detections == []


def test_speaking_over_the_assistant_cancels_the_response(engine: FakeAudioEngine, frames):
    """Spec section 6.2: a new intervention stops playback immediately."""
    machine = listening_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.9]), machine)
    listener.start()
    for event in (
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
    ):
        machine.dispatch(event)

    engine.feed(frames(1))

    assert machine.state is State.INTERRUPTED
    assert engine.playback_stopped == 1
    assert listener.stats.interruptions == 1
    # An interruption is not a fresh question: it must not inflate the count.
    assert listener.stats.activations == 0


def test_an_activation_while_paused_is_discarded(engine: FakeAudioEngine, frames):
    machine = listening_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.9]), machine)
    listener.start()
    machine.dispatch(Event.PAUSE)

    engine.feed(frames(1))

    assert machine.state is State.PAUSED
    assert listener.stats.activations == 0


def test_a_failing_subscriber_does_not_stop_the_capture(engine: FakeAudioEngine, frames):
    machine = listening_machine()
    listener = WakeWordListener(
        engine,
        ScriptedWakeWordDetector([0.9]),
        machine,
        on_detection=lambda _detection: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    listener.start()
    engine.feed(frames(1))

    assert machine.state is State.ACTIVATED
    assert engine.is_capturing


def test_stopping_closes_the_microphone(engine: FakeAudioEngine):
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([]), listening_machine())
    listener.start()
    assert listener.is_listening
    listener.stop()
    assert not listener.is_listening


def test_restarting_resets_the_statistics(engine: FakeAudioEngine):
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([]), listening_machine())
    listener.start()
    engine.feed(np.zeros(FRAME_SAMPLES * 5, dtype=np.int16))
    assert listener.stats.frames_processed == 5

    listener.stop()
    listener.start()
    assert listener.stats.frames_processed == 0


# -- echo guard (risk R-6) -----------------------------------------------


def speaking_machine() -> SessionStateMachine:
    machine = listening_machine()
    for event in (
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
    ):
        machine.dispatch(event)
    return machine


def test_the_assistant_cannot_interrupt_itself(engine: FakeAudioEngine, frames):
    """A marginal detection while the speakers are on is the assistant's own
    voice coming back through the microphone, not somebody in the room."""
    machine = speaking_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.70]), machine)
    listener.start()
    engine.playing = True

    engine.feed(frames(1))

    assert machine.state is State.SPEAKING  # the answer keeps playing
    assert listener.stats.interruptions == 0
    assert listener.stats.echo_suppressions == 1


def test_a_person_speaking_over_the_assistant_still_interrupts(
    engine: FakeAudioEngine, frames
):
    """Barge-in survives the guard (spec section 6.2): someone a metre from the
    microphone is louder and clearer than the speakers bleeding back into it."""
    machine = speaking_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.95]), machine)
    listener.start()
    engine.playing = True

    engine.feed(frames(1))

    assert machine.state is State.INTERRUPTED
    assert listener.stats.interruptions == 1
    assert listener.stats.echo_suppressions == 0


def test_the_guard_only_applies_while_the_speakers_are_on(
    engine: FakeAudioEngine, frames
):
    machine = listening_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.70]), machine)
    listener.start()
    engine.playing = False

    engine.feed(frames(1))

    assert machine.state is State.ACTIVATED
    assert listener.stats.echo_suppressions == 0


def test_the_guard_can_be_switched_off(engine: FakeAudioEngine, frames):
    """A classroom with headphones has no echo path to guard against."""
    machine = speaking_machine()
    listener = WakeWordListener(
        engine, ScriptedWakeWordDetector([0.70]), machine, echo_guard_margin=0.0
    )
    listener.start()
    engine.playing = True

    engine.feed(frames(1))

    assert machine.state is State.INTERRUPTED


def test_suppressions_are_counted_so_the_margin_can_be_tuned(
    engine: FakeAudioEngine, frames
):
    machine = speaking_machine()
    listener = WakeWordListener(engine, ScriptedWakeWordDetector([0.7, 0.7, 0.7]), machine)
    listener.start()
    engine.playing = True

    engine.feed(frames(3))

    # The refractory window lets only the first of the three reach the guard.
    assert listener.stats.echo_suppressions == 1
    assert listener.stats.frames_processed == 3
