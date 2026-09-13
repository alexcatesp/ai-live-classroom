"""The spoken turn: from "Oye Chat" to the end of the answer (plan-fase-1, H3).

The state machine has had the states of a turn since Phase 0; this is what
finally walks through them:

    PASSIVE_LISTENING --"Oye Chat"--> ACTIVATED
        --capture opens--> CAPTURING_REQUEST
        --the question ends--> THINKING
        --first sound of the answer--> SPEAKING
        --the answer has played--> PASSIVE_LISTENING

and cutting an answer (H4):

    THINKING | SPEAKING --"Oye Chat"--> INTERRUPTED --> ACTIVATED --> CAPTURING_REQUEST
    THINKING | SPEAKING --"Parar"-----> INTERRUPTED --> PASSIVE_LISTENING

Either way the answer stops sounding, is cancelled, and the API is told how
much of it was actually heard. The phrase goes on to capture what was said
after it; the button only stops.

Privacy decides where the microphone's audio may go (spec section 14):

* During passive listening, frames only feed a **local pre-roll** of half a
  second, overwritten as it goes. Nothing leaves the machine.
* From the activation until the server says the question ended, frames stream
  to the API -- starting with that pre-roll, so "Oye Chat, ¿qué...?" said in
  one breath keeps its first words.
* From the end of the question on, nothing is sent. The microphone stays open
  only so "Oye Chat" can still interrupt, which is local; the frames go back to
  the pre-roll, so the question after an interruption keeps its first words too.

The pre-roll has a consequence worth stating: it nearly always holds the tail
of "Oye Chat" itself, so the server hears speech the moment a turn opens. If
nobody goes on to ask anything, the server ends that "question" after the
configured silence and answers it. The instructions ask for a short "¿Sí?" in
that case -- what a person would say -- instead of adding a transcription
round trip to every question to tell the two apart. The "no question" timeout
below applies when the pre-roll is off.

Every way a turn can go wrong ends in PASSIVE_LISTENING with a reason, not in
ERROR: an unreachable API or a dropped connection costs one question, not the
class (spec section 19). The class keeps listening, and the session keeps
reconnecting in the background.

Runs on the backend's event loop. Activations and frames arrive on the audio
thread and are handed over thread-safely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from ..audio.devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES
from ..audio.playback import PlaybackStream
from ..config.secrets import PassphraseRequired
from ..config.settings import Settings
from ..config.store import SettingsStore
from ..realtime import events
from ..realtime.audio import REALTIME_RATE
from ..realtime.prompt import DEFAULT_INSTRUCTIONS
from ..realtime.session import (
    ConnectionChanged,
    ConnectionState,
    ManagedRealtimeSession,
    RealtimeUnavailable,
)
from .controller import SessionController
from .state import Event, State, Transition

logger = logging.getLogger(__name__)

# How often to check whether the answer has finished playing.
PLAYBACK_POLL_SECONDS = 0.05

SessionFactory = Callable[[str, events.SessionConfig, Settings], ManagedRealtimeSession]
Publisher = Callable[[dict], None]


def _default_session(
    api_key: str, config: events.SessionConfig, settings: Settings
) -> ManagedRealtimeSession:
    return ManagedRealtimeSession(
        api_key=api_key, config=config, history_turns=settings.history_turns
    )


@dataclass
class TurnRecord:
    """One question and its answer, for the interface and the log (spec 15)."""

    question: str = ""
    answer: str = ""
    started_at: float = field(default_factory=time.time)
    #: From the end of the question to the first sound of the answer.
    first_audio_seconds: float | None = None
    answer_seconds: float = 0.0
    outcome: str = "en curso"
    usage: dict[str, Any] | None = None


class TurnController:
    def __init__(
        self,
        controller: SessionController,
        store: SettingsStore,
        session_factory: SessionFactory = _default_session,
        publish: Publisher | None = None,
    ) -> None:
        self._controller = controller
        self._store = store
        self._session_factory = session_factory
        self._publish = publish or (lambda _message: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ManagedRealtimeSession | None = None
        self._settings: Settings | None = None

        # The pre-roll and the decision to stream are touched from the audio
        # thread; this lock keeps "send the pre-roll, then go live" atomic.
        self._audio_lock = threading.Lock()
        self._preroll: deque[np.ndarray] = deque()
        self._streaming = False

        self._stream: PlaybackStream | None = None
        self._timers: list[asyncio.TimerHandle] = []
        self._question_ended_at: float | None = None
        self._answer_samples = 0
        self._response_finished = False
        self._watcher: asyncio.Task | None = None
        self._answer_item_id: str | None = None
        # The response the current turn is waiting for. A cancelled answer
        # keeps sending events for a moment -- its last audio, and response.done
        # with status "cancelled" -- which must not be taken for the next one.
        self._response_id: str | None = None
        self._cancelled_responses: set[str] = set()
        self._unsubscribe_machine: Callable[[], None] | None = None
        self.current: TurnRecord | None = None
        self.last: TurnRecord | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection_state(self) -> ConnectionState:
        return self._session.state if self._session else ConnectionState.DISCONNECTED

    @property
    def active(self) -> bool:
        return self._session is not None

    async def open(self) -> None:
        """Open the class's session and take over activations.

        A key that is missing or refused stops the class from starting: the
        teacher can fix that now. A network that is down does not: the class
        listens locally and the session keeps trying (spec section 19).
        """
        self._loop = asyncio.get_running_loop()
        settings = self._store.load()
        self._settings = settings
        try:
            api_key = self._store.get_api_key()
        except PassphraseRequired as exc:
            raise RealtimeUnavailable(str(exc), fatal=True) from exc
        if not api_key:
            raise RealtimeUnavailable(
                "No hay ninguna clave de la API guardada. Añádela en Configuración.", fatal=True
            )

        config = events.config_from_settings(settings, DEFAULT_INSTRUCTIONS)
        session = self._session_factory(api_key, config, settings)
        session.subscribe(self._on_event)
        self._session = session

        with self._audio_lock:
            # Zero is allowed: no pre-roll at all.
            frames = int(settings.preroll_ms / 1000 * CAPTURE_SAMPLE_RATE / FRAME_SAMPLES)
            self._preroll = deque(maxlen=frames)
            self._streaming = False
        self._controller.frame_observers.append(self._on_frame)
        self._controller.turn_handler = self._on_activation
        self._unsubscribe_machine = self._controller.machine.subscribe(self._on_transition)

        try:
            await session.start(keep_trying=True)
        except RealtimeUnavailable:
            # Only a fatal refusal gets here; the network being down does not.
            await self.close()
            raise

    def set_publisher(self, publish: Publisher) -> None:
        """Where turn events go: the interface's event socket, once it exists."""
        self._publish = publish

    async def close(self) -> None:
        self._abort_turn(reason=None)
        if self._on_frame in self._controller.frame_observers:
            self._controller.frame_observers.remove(self._on_frame)
        if self._controller.turn_handler == self._on_activation:
            self._controller.turn_handler = None
        if self._unsubscribe_machine is not None:
            self._unsubscribe_machine()
            self._unsubscribe_machine = None
        session, self._session = self._session, None
        if session is not None:
            await session.stop()

    # -- from the audio thread ---------------------------------------------

    def _on_frame(self, frame: np.ndarray) -> None:
        with self._audio_lock:
            if self._streaming:
                session = self._session
                if session is not None:
                    session.append_audio(frame)
            else:
                self._preroll.append(frame.copy())

    def _on_activation(self, _detection) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._begin_turn)

    # -- the turn ----------------------------------------------------------

    def _dispatch(self, event: Event, reason: str | None = None) -> bool:
        return self._controller.machine.try_dispatch(event, reason) is not None

    def _begin_turn(self) -> None:
        machine = self._controller.machine
        if machine.state is not State.ACTIVATED:
            return  # paused or stopped between the detection and now
        session = self._session
        settings = self._settings or self._store.load()

        if session is None or not session.ready:
            why = (
                "Sin conexión con la API: la pregunta no se puede enviar. "
                "La aplicación sigue escuchando y reintentando."
            )
            self._notify("turn_failed", reason=why)
            self._dispatch(Event.TURN_FAILED, why)
            return

        self.current = TurnRecord()
        self._question_ended_at = None
        self._answer_samples = 0
        self._answer_item_id = None
        self._response_id = None
        self._response_finished = False
        session.clear_audio()

        with self._audio_lock:
            for frame in self._preroll:
                session.append_audio(frame)
            self._preroll.clear()
            self._streaming = True

        self._dispatch(Event.CAPTURE_STARTED, "Escuchando la pregunta")
        self._notify("turn_started")
        self._after(settings.activation_timeout_seconds, self._no_question)

    def _no_question(self) -> None:
        if self._controller.machine.state is not State.CAPTURING_REQUEST:
            return
        if self.current is not None and self.current.question:
            return
        self._stop_streaming()
        if self._session is not None:
            self._session.clear_audio()
            self._session.end_turn()
        self._finish_record("sin pregunta")
        self._dispatch(Event.ACTIVATION_EXPIRED, "No llegó ninguna pregunta")

    def _question_too_long(self) -> None:
        if self._controller.machine.state is not State.CAPTURING_REQUEST:
            return
        # Answer what has been said so far rather than listen forever.
        self._end_question()
        if self._session is not None:
            self._session.commit_audio()
            self._session.create_response()

    def _end_question(self) -> None:
        self._stop_streaming()
        self._cancel_timers()
        self._question_ended_at = time.monotonic()
        self._dispatch(Event.REQUEST_CAPTURED, "Pregunta recibida")

    # -- events from the API -----------------------------------------------

    def _on_event(self, event: events.ServerEvent) -> None:
        state = self._controller.machine.state
        record = self.current

        if self._is_stale(event):
            return

        if isinstance(event, ConnectionChanged):
            self._notify("connection", state=event.state.value, reason=event.reason)
            # Only a turn that had actually begun was lost; an activation still
            # waiting to begin is told "sin conexión" by _begin_turn itself.
            if (
                event.state is not ConnectionState.READY
                and state in _TURN_STATES
                and self.current is not None
            ):
                self._abort_turn("Se perdió la conexión con la API a mitad de la pregunta.")
            return

        if isinstance(event, events.SpeechStarted) and state is State.CAPTURING_REQUEST:
            self._cancel_timers()
            settings = self._settings or self._store.load()
            self._after(settings.max_question_seconds, self._question_too_long)

        elif isinstance(event, events.SpeechStopped) and state is State.CAPTURING_REQUEST:
            self._end_question()

        elif isinstance(event, events.InputTranscript) and record is not None:
            record.question = event.text if event.final else record.question + event.text
            self._notify("question", text=record.question, final=event.final)

        elif isinstance(event, events.ResponseStarted) and record is not None:
            if state in (State.THINKING, State.SPEAKING) and event.response_id:
                self._response_id = event.response_id

        elif isinstance(event, events.AudioDelta) and record is not None:
            if state not in (State.THINKING, State.SPEAKING):
                return  # a late piece of a turn that was aborted
            if self._stream is None:
                if not self._open_stream():
                    return
                if self._question_ended_at is not None:
                    record.first_audio_seconds = round(
                        time.monotonic() - self._question_ended_at, 2
                    )
                self._dispatch(Event.RESPONSE_STARTED, "Respondiendo")
            if event.item_id:
                self._answer_item_id = event.item_id
            self._stream.feed(event.pcm)
            self._answer_samples += event.pcm.size

        elif isinstance(event, events.OutputTranscript) and record is not None:
            record.answer = event.text if event.final else record.answer + event.text
            self._notify("answer", text=record.answer, final=event.final)

        elif isinstance(event, events.ResponseDone) and record is not None:
            record.usage = event.usage
            if event.status not in (None, "completed"):
                self._abort_turn(f"La respuesta no se completó ({event.status}).")
                return
            self._response_finished = True
            if self._stream is not None:
                self._stream.finish()
                self._watch_playback()
            else:
                self._finish_record("sin audio")
                self._dispatch(Event.RESPONSE_FINISHED, "Respuesta sin audio")

        elif isinstance(event, events.ApiError) and state in _TURN_STATES:
            if event.code in _HARMLESS_ERRORS:
                return  # e.g. a cancel that crossed the answer's own end
            self._abort_turn(f"La API devolvió un error: {event.message}")

    def _is_stale(self, event: events.ServerEvent) -> bool:
        """Whether this event belongs to an answer that is no longer wanted."""
        response_id = getattr(event, "response_id", None)
        if response_id is None:
            return False
        if response_id in self._cancelled_responses:
            return True
        return self._response_id is not None and response_id != self._response_id

    def _open_stream(self) -> bool:
        engine = self._controller.engine
        if engine is None:
            self._abort_turn("No hay motor de audio para reproducir la respuesta.")
            return False
        try:
            self._stream = engine.open_playback(REALTIME_RATE)
        except Exception as exc:  # noqa: BLE001 - AudioError or a PortAudio surprise
            self._abort_turn(f"No se pudo reproducir la respuesta: {exc}")
            return False
        return True

    def _watch_playback(self) -> None:
        async def watch() -> None:
            stream = self._stream
            while stream is not None and stream.is_active:
                await asyncio.sleep(PLAYBACK_POLL_SECONDS)
            if self._controller.machine.state is State.SPEAKING:
                self._finish_record("completada")
                self._stream = None
                self._dispatch(Event.RESPONSE_FINISHED, "Respuesta terminada")

        if self._watcher is not None:
            self._watcher.cancel()
        self._watcher = asyncio.ensure_future(watch())

    # -- the state machine, from wherever it was moved ---------------------

    def _on_transition(self, transition: Transition) -> None:
        loop = self._loop
        if transition.target in (State.PAUSED, State.STOPPED, State.ERROR, State.INTERRUPTED):
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._on_turn_cut, transition)

    def _on_turn_cut(self, transition: Transition) -> None:
        target = transition.target
        interrupted = target is State.INTERRUPTED
        if self.current is not None or self._stream is not None or self._streaming:
            if self._session is not None:
                self._session.cancel_response()
                self._truncate_heard()
                self._session.end_turn()
            if self._response_id is not None:
                self._cancelled_responses.add(self._response_id)
            self._response_id = None
            self._stop_streaming()
            self._stop_playback()
            self._cancel_timers()
            self._finish_record("interrumpida" if interrupted else "cortada")
        if not interrupted or self._controller.machine.state is not State.INTERRUPTED:
            return
        if transition.event is Event.WAKE_WORD_DETECTED:
            # "Oye Chat" over the answer: what follows the phrase is the next
            # question (plan-fase-1, H4). The pre-roll already holds its start.
            if self._dispatch(Event.WAKE_WORD_DETECTED, "Nueva pregunta tras la interrupción"):
                self._begin_turn()
        else:
            self._dispatch(Event.INTERRUPTION_HANDLED, "Interrupción atendida")

    def _truncate_heard(self) -> None:
        """Tell the API how much of the answer was heard (plan-fase-1, H4)."""
        stream, session = self._stream, self._session
        item_id = self._answer_item_id
        if stream is None or session is None or item_id is None:
            return
        session.truncate(item_id, stream.played_ms)

    # -- cleanup -----------------------------------------------------------

    def _abort_turn(self, reason: str | None) -> None:
        self._stop_streaming()
        self._stop_playback()
        self._cancel_timers()
        if self._session is not None:
            self._session.cancel_response()
            self._session.end_turn()
        if self._response_id is not None:
            self._cancelled_responses.add(self._response_id)
            self._response_id = None
        if self.current is not None:
            self._finish_record("fallida")
        if reason:
            self._notify("turn_failed", reason=reason)
            self._dispatch(Event.TURN_FAILED, reason)

    def _stop_streaming(self) -> None:
        with self._audio_lock:
            self._streaming = False

    def _stop_playback(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        stream, self._stream = self._stream, None
        if stream is not None and stream.is_active:
            stream.stop()

    def _after(self, seconds: float, callback: Callable[[], None]) -> None:
        if self._loop is not None:
            self._timers.append(self._loop.call_later(seconds, callback))

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def _finish_record(self, outcome: str) -> None:
        record = self.current
        if record is None:
            return
        record.outcome = outcome
        record.answer_seconds = round(self._answer_samples / REALTIME_RATE, 1)
        self.last, self.current = record, None
        self._notify("turn_finished", turn=asdict(record))

    def _notify(self, kind: str, **payload: Any) -> None:
        with contextlib.suppress(Exception):
            self._publish({"type": "turn", "payload": {"kind": kind, **payload}})

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict:
        return {
            "connection": self.connection_state.value,
            "last_error": self._session.last_error if self._session else None,
            "current": asdict(self.current) if self.current else None,
            "last": asdict(self.last) if self.last else None,
        }


_TURN_STATES = {State.ACTIVATED, State.CAPTURING_REQUEST, State.THINKING, State.SPEAKING}

# Errors that say nothing about the turn in progress: cancelling an answer that
# had just finished on its own, or had not started yet.
_HARMLESS_ERRORS = {"response_cancel_not_active"}
