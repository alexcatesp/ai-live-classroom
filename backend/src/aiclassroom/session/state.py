"""Session state machine (spec section 7).

Two rules drive the design, both taken from the specification:

* Spec section 19 -- "los errores no deben provocar que la aplicación parezca
  estar escuchando cuando no lo está". The microphone is therefore not a flag
  somebody remembers to clear: it is derived from the state, so no state can
  disagree with what the indicator shows.
* Spec section 6.2 -- a new intervention cancels the response immediately, so
  INTERRUPTED is reachable from SPEAKING at any moment.

Transitions that are not in the table raise instead of being ignored. A wake
word arriving while the assistant is already speaking is a real situation with
a defined answer (interrupt), not something to swallow silently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class State(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    READY = "READY"
    PASSIVE_LISTENING = "PASSIVE_LISTENING"
    ACTIVATED = "ACTIVATED"
    CAPTURING_REQUEST = "CAPTURING_REQUEST"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class Event(StrEnum):
    CREATE_SESSION = "CREATE_SESSION"
    SESSION_PREPARED = "SESSION_PREPARED"
    START_CLASS = "START_CLASS"
    WAKE_WORD_DETECTED = "WAKE_WORD_DETECTED"
    #: Activated, but no request followed. In Phase 0 that is every activation,
    #: because there is no conversation yet; in Phase 1 it is the timeout for
    #: someone who said the phrase and then nothing.
    ACTIVATION_EXPIRED = "ACTIVATION_EXPIRED"
    CAPTURE_STARTED = "CAPTURE_STARTED"
    REQUEST_CAPTURED = "REQUEST_CAPTURED"
    RESPONSE_STARTED = "RESPONSE_STARTED"
    RESPONSE_FINISHED = "RESPONSE_FINISHED"
    INTERRUPT = "INTERRUPT"
    INTERRUPTION_HANDLED = "INTERRUPTION_HANDLED"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    FAIL = "FAIL"
    RECOVER = "RECOVER"
    RESET = "RESET"


# The microphone is open only while the assistant is listening for the wake
# word or capturing a request. Every other state -- including ERROR, PAUSED and
# STOPPED -- must show the microphone as closed (spec section 19).
MIC_ACTIVE_STATES: frozenset[State] = frozenset(
    {State.PASSIVE_LISTENING, State.ACTIVATED, State.CAPTURING_REQUEST}
)

# States from which the class can be paused or stopped: anything that happens
# once the class is running.
_IN_CLASS: tuple[State, ...] = (
    State.PASSIVE_LISTENING,
    State.ACTIVATED,
    State.CAPTURING_REQUEST,
    State.THINKING,
    State.SPEAKING,
    State.INTERRUPTED,
)

_TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.IDLE, Event.CREATE_SESSION): State.PREPARING,
    (State.PREPARING, Event.SESSION_PREPARED): State.READY,
    (State.READY, Event.START_CLASS): State.PASSIVE_LISTENING,
    (State.PASSIVE_LISTENING, Event.WAKE_WORD_DETECTED): State.ACTIVATED,
    # Without this, ACTIVATED had no way out but pause or stop: the first
    # activation of a class was also the last one.
    (State.ACTIVATED, Event.ACTIVATION_EXPIRED): State.PASSIVE_LISTENING,
    (State.ACTIVATED, Event.CAPTURE_STARTED): State.CAPTURING_REQUEST,
    (State.CAPTURING_REQUEST, Event.REQUEST_CAPTURED): State.THINKING,
    (State.THINKING, Event.RESPONSE_STARTED): State.SPEAKING,
    (State.SPEAKING, Event.RESPONSE_FINISHED): State.PASSIVE_LISTENING,
    # A new intervention cancels the response (spec section 6.2). Thinking can be
    # cancelled too: the teacher may move on before the first audio arrives.
    (State.SPEAKING, Event.INTERRUPT): State.INTERRUPTED,
    (State.THINKING, Event.INTERRUPT): State.INTERRUPTED,
    (State.INTERRUPTED, Event.INTERRUPTION_HANDLED): State.PASSIVE_LISTENING,
    (State.PAUSED, Event.RESUME): State.PASSIVE_LISTENING,
    (State.STOPPED, Event.RESET): State.IDLE,
    (State.ERROR, Event.RECOVER): State.READY,
    (State.ERROR, Event.RESET): State.IDLE,
    (State.PREPARING, Event.STOP): State.STOPPED,
    (State.READY, Event.STOP): State.STOPPED,
    (State.PAUSED, Event.STOP): State.STOPPED,
}
_TRANSITIONS.update({(state, Event.PAUSE): State.PAUSED for state in _IN_CLASS})
_TRANSITIONS.update({(state, Event.STOP): State.STOPPED for state in _IN_CLASS})
# Anything that is not already finished can fail into ERROR.
_TRANSITIONS.update(
    {
        (state, Event.FAIL): State.ERROR
        for state in State
        if state not in (State.ERROR, State.STOPPED)
    }
)


class InvalidTransition(RuntimeError):
    """Raised when an event does not apply to the current state."""

    def __init__(self, state: State, event: Event) -> None:
        super().__init__(f"El evento {event} no es válido en el estado {state}.")
        self.state = state
        self.event = event


@dataclass(frozen=True)
class Transition:
    """One recorded move, used by the UI log and by the session metrics."""

    source: State
    event: Event
    target: State
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None


Listener = Callable[[Transition], None]


class SessionStateMachine:
    """Thread-safe holder of the current state.

    Audio callbacks arrive on a PortAudio thread while the HTTP handlers run on
    another, so every read and write goes through one lock.
    """

    def __init__(self, initial: State = State.IDLE, history_limit: int = 200) -> None:
        self._state = initial
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._history: list[Transition] = []
        self._history_limit = history_limit

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def is_microphone_active(self) -> bool:
        """What the microphone indicator must show (spec section 6.1)."""
        return self.state in MIC_ACTIVE_STATES

    @property
    def history(self) -> list[Transition]:
        with self._lock:
            return list(self._history)

    def can(self, event: Event) -> bool:
        with self._lock:
            return (self._state, event) in _TRANSITIONS

    def available_events(self) -> list[Event]:
        with self._lock:
            state = self._state
        return [event for event in Event if (state, event) in _TRANSITIONS]

    def dispatch(self, event: Event, reason: str | None = None) -> Transition:
        """Apply `event`, notify listeners and return the recorded transition."""
        with self._lock:
            source = self._state
            try:
                target = _TRANSITIONS[(source, event)]
            except KeyError:
                raise InvalidTransition(source, event) from None
            self._state = target
            transition = Transition(source=source, event=event, target=target, reason=reason)
            self._history.append(transition)
            del self._history[: max(0, len(self._history) - self._history_limit)]
            listeners = list(self._listeners)

        for listener in listeners:
            # A broken listener must not strand the machine in a state nobody
            # was told about, so failures here are contained.
            try:
                listener(transition)
            except Exception:  # noqa: BLE001 - defensive: UI listeners are untrusted
                import logging

                logging.getLogger(__name__).exception("Un observador de estado falló.")
        return transition

    def try_dispatch(self, event: Event, reason: str | None = None) -> Transition | None:
        """Dispatch when legal, otherwise return None.

        Used where a late event is genuinely expected -- a wake word arriving
        one frame after the class was paused, for instance.
        """
        try:
            return self.dispatch(event, reason)
        except InvalidTransition:
            return None

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe
