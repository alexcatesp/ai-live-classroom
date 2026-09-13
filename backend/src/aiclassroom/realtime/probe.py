"""Ask one real question from the application (plan-fase-1, H1).

Until H3 wires the spoken turn into the class, this is how a teacher tries the
Realtime session for real, without a script or an environment variable: the
microphone configured in settings, the API key saved in settings, and the same
session class a class will use.

It is a turn in miniature, and it measures the turn the way H3 will need it
measured:

* The question streams to the API as it is spoken. The server decides when it
  has ended -- after the configured silence -- exactly as in class.
* The microphone closes the moment the question ends, so the answer is never
  recorded and nothing is sent while it plays (spec section 14).
* Time to first audio is taken from the server's "speech stopped" to the first
  sound of the answer: network plus model, the part R-2 is about. The silence
  itself is reported separately, because it is a setting, not a delay.

* The answer plays as it arrives, through the streaming playback of H2, and
  can be stopped at once.

The question's audio is not kept. The answer's audio stays in memory only
until the next test, so it can be played back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from ..audio.engine import AudioEngine, AudioError
from ..audio.playback import PlaybackStream
from ..config.secrets import PassphraseRequired
from ..config.settings import Settings
from ..config.store import SettingsStore
from . import events
from .audio import REALTIME_RATE
from .prompt import DEFAULT_INSTRUCTIONS
from .session import ManagedRealtimeSession, RealtimeUnavailable

logger = logging.getLogger(__name__)

# Nobody has started speaking by now: the test is over.
WAIT_FOR_SPEECH_SECONDS = 8.0
# A question longer than this is ended by hand.
MAX_QUESTION_SECONDS = 20.0
ANSWER_TIMEOUT_SECONDS = 60.0

SessionFactory = Callable[[str, events.SessionConfig], ManagedRealtimeSession]


class ProbeState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    WAITING = "waiting"
    ANSWERING = "answering"
    DONE = "done"
    FAILED = "failed"


class ProbeUnavailable(RuntimeError):
    """The test cannot start, and the message says why."""


@dataclass(frozen=True)
class ProbeResult:
    question: str
    answer: str
    session_open_seconds: float
    #: From the server deciding the question ended to the first sound back.
    first_audio_seconds: float | None
    silence_seconds: float
    answer_seconds: float
    status: str | None
    usage: dict[str, Any] | None


def _default_session(api_key: str, config: events.SessionConfig) -> ManagedRealtimeSession:
    # A one-question session: no renewal, and a lost connection is simply a
    # failed test rather than something to keep retrying.
    return ManagedRealtimeSession(api_key=api_key, config=config, reconnect_delays=(1.0,))


class ConversationProbe:
    def __init__(
        self,
        store: SettingsStore,
        engine_factory: Callable[[Settings], AudioEngine],
        microphone_in_use: Callable[[], bool],
        session_factory: SessionFactory = _default_session,
        wait_for_speech: float = WAIT_FOR_SPEECH_SECONDS,
        max_question: float = MAX_QUESTION_SECONDS,
        answer_timeout: float = ANSWER_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self._engine_factory = engine_factory
        self._microphone_in_use = microphone_in_use
        self._session_factory = session_factory
        self._wait_for_speech = wait_for_speech
        self._max_question = max_question
        self._answer_timeout = answer_timeout

        self.state = ProbeState.IDLE
        self.error: str | None = None
        self.result: ProbeResult | None = None
        self.question_text = ""
        self.answer_text = ""
        self._answer: list[np.ndarray] = []
        self._task: asyncio.Task | None = None
        # The answer plays as it arrives (H2); the same stream is replaced when
        # the teacher asks to hear it again.
        self._stream: PlaybackStream | None = None
        self.playback_error: str | None = None

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "error": self.error,
            "question": self.question_text,
            "answer": self.answer_text,
            "result": asdict(self.result) if self.result else None,
            "can_play": bool(self._answer) and self.state is ProbeState.DONE,
            "playing": self.playing,
            "played_ms": self._stream.played_ms if self._stream is not None else 0,
            "playback_error": self.playback_error,
        }

    @property
    def playing(self) -> bool:
        return self._stream is not None and self._stream.is_active

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- the test ----------------------------------------------------------

    async def start(self) -> None:
        if self.running:
            raise ProbeUnavailable("Ya hay una prueba en marcha.")
        if self._microphone_in_use():
            raise ProbeUnavailable(
                "El micrófono está escuchando la clase. Pausa o finaliza la clase para probar."
            )
        try:
            api_key = self.store.get_api_key()
        except PassphraseRequired as exc:
            raise ProbeUnavailable(str(exc)) from exc
        if not api_key:
            raise ProbeUnavailable(
                "No hay ninguna clave de la API guardada. Añádela en Configuración."
            )

        self.state, self.error, self.result = ProbeState.CONNECTING, None, None
        self.question_text = self.answer_text = ""
        self.playback_error = None
        self.stop_playback()
        self._answer = []
        self._task = asyncio.create_task(self._run(api_key), name="conversation-probe")

    async def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.state not in (ProbeState.DONE, ProbeState.FAILED):
            self.state = ProbeState.IDLE

    async def wait(self) -> None:
        """For tests."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self, api_key: str) -> None:
        settings = self.store.load()
        config = events.config_from_settings(settings, DEFAULT_INSTRUCTIONS)
        session = self._session_factory(api_key, config)

        speech_started = asyncio.Event()
        question_ended = asyncio.Event()
        response_done = asyncio.Event()
        marks: dict[str, float] = {}
        final: dict[str, Any] = {}

        def on_event(event: events.ServerEvent) -> None:
            now = time.monotonic()
            if isinstance(event, events.SpeechStarted):
                speech_started.set()
            elif isinstance(event, events.SpeechStopped):
                marks.setdefault("question_ended", now)
                question_ended.set()
            elif isinstance(event, events.InputTranscript):
                self.question_text = event.text if event.final else self.question_text + event.text
            elif isinstance(event, events.AudioDelta):
                marks.setdefault("first_audio", now)
                self.state = ProbeState.ANSWERING
                self._answer.append(event.pcm)
                if self._stream is not None:
                    self._stream.feed(event.pcm)  # heard as it arrives (H2)
            elif isinstance(event, events.OutputTranscript):
                self.answer_text = event.text if event.final else self.answer_text + event.text
            elif isinstance(event, events.ResponseDone):
                final["status"], final["usage"] = event.status, event.usage
                if self._stream is not None:
                    self._stream.finish()
                response_done.set()
            elif isinstance(event, events.ApiError):
                final.setdefault("error", event.message)

        session.subscribe(on_event)
        engine: AudioEngine | None = None
        try:
            opened = time.monotonic()
            await session.start()
            open_seconds = time.monotonic() - opened

            engine = self._engine_factory(settings)
            engine.start_capture(session.append_audio)
            self.state = ProbeState.LISTENING

            try:
                await asyncio.wait_for(speech_started.wait(), self._wait_for_speech)
            except TimeoutError:
                raise ProbeUnavailable(
                    "No se oyó ninguna pregunta. Pulsa el botón y habla en seguida."
                ) from None

            try:
                await asyncio.wait_for(question_ended.wait(), self._max_question)
            except TimeoutError:
                # Too long to wait for silence: end the question by hand.
                marks.setdefault("question_ended", time.monotonic())
                session.commit_audio()
                session.create_response()

            # The question is over: the microphone closes before any answer.
            engine.stop_capture()
            engine = None
            self.state = ProbeState.WAITING
            self._open_stream(settings)

            try:
                await asyncio.wait_for(response_done.wait(), self._answer_timeout)
            except TimeoutError:
                raise ProbeUnavailable("La respuesta no terminó a tiempo.") from None

            if final.get("status") not in (None, "completed"):
                raise ProbeUnavailable(
                    final.get("error") or f"La respuesta terminó como «{final.get('status')}»."
                )

            samples = sum(chunk.size for chunk in self._answer)
            first = marks.get("first_audio")
            ended = marks.get("question_ended")
            self.result = ProbeResult(
                question=self.question_text,
                answer=self.answer_text,
                session_open_seconds=round(open_seconds, 2),
                first_audio_seconds=(
                    round(first - ended, 2) if first is not None and ended is not None else None
                ),
                silence_seconds=settings.turn_silence_ms / 1000,
                answer_seconds=round(samples / REALTIME_RATE, 1),
                status=final.get("status"),
                usage=final.get("usage"),
            )
            self.state = ProbeState.DONE
        except (ProbeUnavailable, RealtimeUnavailable, AudioError) as exc:
            self.state, self.error = ProbeState.FAILED, str(exc)
        except asyncio.CancelledError:
            self.state = ProbeState.IDLE
            self.stop_playback()
            raise
        except Exception as exc:  # noqa: BLE001 - whatever breaks, the teacher is told
            logger.exception("La prueba de conversación falló.")
            self.state, self.error = ProbeState.FAILED, f"La prueba falló: {exc}"
        finally:
            if engine is not None:
                with contextlib.suppress(Exception):
                    engine.stop_capture()
            await session.stop()

    # -- playback ----------------------------------------------------------

    def _open_stream(self, settings: Settings) -> None:
        """Open the speakers before the answer arrives, so its first piece plays at once.

        A speaker that cannot be opened does not fail the test: the answer is
        still received, shown and measured, and the problem is reported.
        """
        try:
            self._stream = self._engine_factory(settings).open_playback(REALTIME_RATE)
        except AudioError as exc:
            self._stream = None
            self.playback_error = str(exc)

    async def play(self) -> None:
        """Hear the last answer again, through the same streaming path."""
        if not self._answer or self.state is not ProbeState.DONE:
            raise ProbeUnavailable("No hay ninguna respuesta que escuchar.")
        if self.playing:
            raise ProbeUnavailable("La respuesta ya se está reproduciendo.")
        self._open_stream(self.store.load())
        if self._stream is None:
            raise ProbeUnavailable(self.playback_error or "No se pudo abrir los altavoces.")
        self._stream.feed(np.concatenate(self._answer))
        self._stream.finish()

    def stop_playback(self) -> None:
        """Cut the answer at once (H2); H4's interruptions go through the same stop."""
        if self._stream is not None and self._stream.is_active:
            self._stream.stop()

    async def stop(self) -> None:
        self.stop_playback()
