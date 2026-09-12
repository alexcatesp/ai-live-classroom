"""The state machine carries the guarantees the classroom relies on."""

from __future__ import annotations

import threading

import pytest

from aiclassroom.session.state import (
    MIC_ACTIVE_STATES,
    Event,
    InvalidTransition,
    SessionStateMachine,
    State,
)


def drive(machine: SessionStateMachine, *events: Event) -> State:
    for event in events:
        machine.dispatch(event)
    return machine.state


def test_full_question_answer_cycle_returns_to_listening(machine):
    state = drive(
        machine,
        Event.CREATE_SESSION,
        Event.SESSION_PREPARED,
        Event.START_CLASS,
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
        Event.RESPONSE_FINISHED,
    )
    assert state is State.PASSIVE_LISTENING


def test_interrupting_a_response_returns_to_listening(machine):
    drive(
        machine,
        Event.CREATE_SESSION,
        Event.SESSION_PREPARED,
        Event.START_CLASS,
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
        Event.RESPONSE_STARTED,
    )
    assert machine.dispatch(Event.INTERRUPT).target is State.INTERRUPTED
    assert machine.dispatch(Event.INTERRUPTION_HANDLED).target is State.PASSIVE_LISTENING


def test_thinking_can_also_be_interrupted(machine):
    """The teacher may move on before the first audio arrives."""
    drive(
        machine,
        Event.CREATE_SESSION,
        Event.SESSION_PREPARED,
        Event.START_CLASS,
        Event.WAKE_WORD_DETECTED,
        Event.CAPTURE_STARTED,
        Event.REQUEST_CAPTURED,
    )
    assert machine.dispatch(Event.INTERRUPT).target is State.INTERRUPTED


@pytest.mark.parametrize(
    "state",
    [state for state in State if state not in MIC_ACTIVE_STATES],
)
def test_microphone_is_closed_outside_the_listening_states(state):
    """Spec section 19: never look like we are listening when we are not."""
    assert SessionStateMachine(initial=state).is_microphone_active is False


@pytest.mark.parametrize("state", sorted(MIC_ACTIVE_STATES))
def test_microphone_is_open_only_while_listening_or_capturing(state):
    assert SessionStateMachine(initial=state).is_microphone_active is True


def test_error_closes_the_microphone_from_every_live_state():
    """A failure mid-class must not leave the indicator lit."""
    for state in MIC_ACTIVE_STATES:
        machine = SessionStateMachine(initial=state)
        machine.dispatch(Event.FAIL, reason="micrófono desconectado")
        assert machine.state is State.ERROR
        assert machine.is_microphone_active is False


def test_pausing_closes_the_microphone():
    machine = SessionStateMachine(initial=State.PASSIVE_LISTENING)
    machine.dispatch(Event.PAUSE)
    assert machine.state is State.PAUSED
    assert machine.is_microphone_active is False
    machine.dispatch(Event.RESUME)
    assert machine.is_microphone_active is True


def test_no_state_is_a_dead_end():
    """The teacher can always get back to a clean session.

    IDLE is excluded because it *is* the clean session -- there is nothing to
    stop or reset from there.
    """
    for state in State:
        if state is State.IDLE:
            continue
        machine = SessionStateMachine(initial=state)
        assert machine.can(Event.STOP) or machine.can(Event.RESET), state


def test_unknown_transition_raises_instead_of_being_ignored(machine):
    with pytest.raises(InvalidTransition) as error:
        machine.dispatch(Event.RESPONSE_STARTED)
    assert error.value.state is State.IDLE
    assert "IDLE" in str(error.value)


def test_try_dispatch_returns_none_for_a_late_event(machine):
    assert machine.try_dispatch(Event.WAKE_WORD_DETECTED) is None
    assert machine.state is State.IDLE


def test_a_stopped_session_can_be_started_again(machine):
    drive(machine, Event.CREATE_SESSION, Event.SESSION_PREPARED, Event.STOP, Event.RESET)
    assert machine.state is State.IDLE
    assert drive(machine, Event.CREATE_SESSION) is State.PREPARING


def test_listeners_receive_transitions_and_a_broken_one_is_contained(machine):
    seen = []
    machine.subscribe(lambda _transition: (_ for _ in ()).throw(RuntimeError("boom")))
    unsubscribe = machine.subscribe(seen.append)

    machine.dispatch(Event.CREATE_SESSION)
    assert [transition.target for transition in seen] == [State.PREPARING]

    unsubscribe()
    machine.dispatch(Event.SESSION_PREPARED)
    assert len(seen) == 1


def test_history_is_recorded_and_bounded():
    machine = SessionStateMachine(history_limit=3)
    for _ in range(3):
        machine.dispatch(Event.CREATE_SESSION)
        machine.dispatch(Event.SESSION_PREPARED)
        machine.dispatch(Event.STOP)
        machine.dispatch(Event.RESET)
    assert len(machine.history) == 3
    assert machine.history[-1].event is Event.RESET


def test_concurrent_dispatches_keep_one_consistent_state(machine):
    """Audio and HTTP threads both touch the machine."""
    machine.dispatch(Event.CREATE_SESSION)
    machine.dispatch(Event.SESSION_PREPARED)
    machine.dispatch(Event.START_CLASS)

    accepted: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        accepted.append(machine.try_dispatch(Event.WAKE_WORD_DETECTED) is not None)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one wake word can move PASSIVE_LISTENING -> ACTIVATED.
    assert sum(accepted) == 1
    assert machine.state is State.ACTIVATED
