"""A Realtime session that lasts the whole class (plan-fase-1, H1).

Opening a session per question would add the handshake to every answer: 1.7 s
measured in the first classroom test, nearly all of it network (R-2). So the
session opens when the class starts and stays open, and this module is what
keeps that promise honest:

* **It reconnects.** A dropped connection is retried with backoff; the class
  keeps listening locally meanwhile, and the state says the assistant is not
  reachable rather than pretending (spec section 19).
* **It renews.** A session does not live forever. It is replaced before a
  configurable age, and only between turns, never in the middle of an answer.
* **It keeps the conversation cacheable.** Every earlier question and answer is
  re-read as input on each new turn. Read from the prompt cache it costs 80
  times less, but only while it stays unchanged, so nothing is deleted turn by
  turn: the server truncates the history in large steps once it outgrows a
  token budget (events.session_update, spec section 16).
* **It gives up on what retrying cannot fix.** A rejected key is reported once,
  not hammered every thirty seconds for the rest of the lesson.

Two layers: `RealtimeConnection` is one WebSocket, `ManagedRealtimeSession` is
the class-long session built from as many connections as it takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from . import events
from .audio import MICROPHONE_RATE, REALTIME_RATE, StreamingResampler

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"

CONNECT_TIMEOUT = 15.0
# The documented maximum age of a session is not something to rely on, so the
# session is renewed well before any plausible limit, between turns.
DEFAULT_MAX_SESSION_SECONDS = 50 * 60
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    #: Retrying cannot help: the key or the model was refused.
    FAILED = "failed"


class RealtimeUnavailable(RuntimeError):
    """The session could not be opened. `fatal` when retrying is pointless."""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


@dataclass(frozen=True)
class ConnectionChanged(events.ServerEvent):
    """Not from the API: the session's own state, for the interface."""

    state: ConnectionState
    reason: str | None = None


Listener = Callable[[events.ServerEvent], None]
Connector = Callable[..., Any]


def _refusal(exc: Exception) -> RealtimeUnavailable:
    """Classify a rejected upgrade, as the diagnostic does (D-03)."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in (401, 403):
        return RealtimeUnavailable(f"La API rechazó la clave (HTTP {code}).", fatal=True)
    if code == 404:
        return RealtimeUnavailable("El modelo no existe o no está autorizado.", fatal=True)
    if code == 429:
        return RealtimeUnavailable("La cuenta ha alcanzado su límite de uso (HTTP 429).")
    return RealtimeUnavailable(f"La conexión fue rechazada (HTTP {code}).")


class RealtimeConnection:
    """One WebSocket to the Realtime API, configured and ready to use."""

    def __init__(
        self,
        api_key: str,
        config: events.SessionConfig,
        on_event: Listener,
        url: str = REALTIME_URL,
        ssl_context: ssl.SSLContext | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        connector: Connector | None = None,
    ) -> None:
        self._api_key = api_key
        self._config = config
        self._on_event = on_event
        self._url = url
        self._ssl_context = ssl_context
        self._connect_timeout = connect_timeout
        self._connector = connector
        self._socket = None
        self._reader: asyncio.Task | None = None
        self._closing = False
        self.closed = asyncio.Event()
        self.opened_at: float | None = None

    async def open(self) -> None:
        import websockets
        from websockets.exceptions import InvalidStatus, WebSocketException

        connect = self._connector or websockets.connect
        kwargs: dict[str, Any] = {
            "additional_headers": {"Authorization": f"Bearer {self._api_key}"},
            "open_timeout": self._connect_timeout,
            # Audio deltas are base64 and can be large; the API is trusted.
            "max_size": None,
        }
        if self._ssl_context is not None:
            kwargs["ssl"] = self._ssl_context

        try:
            self._socket = await connect(f"{self._url}?model={self._config.model}", **kwargs)
        except InvalidStatus as exc:
            raise _refusal(exc) from exc
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise RealtimeUnavailable(f"No se pudo abrir la conexión con la API: {exc}") from exc

        try:
            await asyncio.wait_for(self._handshake(), timeout=self._connect_timeout)
        except TimeoutError as exc:
            await self._abort()
            raise RealtimeUnavailable("La API no confirmó la sesión a tiempo.") from exc
        except RealtimeUnavailable:
            await self._abort()
            raise
        except (OSError, WebSocketException) as exc:
            await self._abort()
            raise RealtimeUnavailable(
                f"La conexión se cortó al configurar la sesión: {exc}"
            ) from exc

        self.opened_at = time.monotonic()
        self._reader = asyncio.create_task(self._read(), name="realtime-reader")

    async def _handshake(self) -> None:
        first = events.parse(await self._socket.recv())
        if isinstance(first, events.ApiError):
            raise RealtimeUnavailable(first.message, fatal=first.code in _FATAL_CODES)
        if first.type != "session.created":
            raise RealtimeUnavailable(f"Respuesta inesperada de la API: {first.type}.")

        await self.send(events.session_update(self._config))
        while True:
            event = events.parse(await self._socket.recv())
            if event.type == "session.updated":
                return
            if isinstance(event, events.ApiError):
                raise RealtimeUnavailable(
                    f"La API no aceptó la configuración de la sesión: {event.message}",
                    fatal=event.code in _FATAL_CODES,
                )

    async def send(self, event: dict[str, Any]) -> None:
        if self._socket is None:
            raise RealtimeUnavailable("La sesión no está abierta.")
        await self._socket.send(json.dumps(event))

    async def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        reason = None
        try:
            async for message in self._socket:
                try:
                    self._on_event(events.parse(message))
                except Exception:  # noqa: BLE001 - a listener must not kill the session
                    logger.exception("Un oyente de eventos Realtime falló.")
        except ConnectionClosed as exc:
            reason = str(exc)
        finally:
            self.closed.set()
            if not self._closing:
                self._on_event(
                    ConnectionChanged("connection", ConnectionState.DISCONNECTED, reason)
                )

    async def close(self) -> None:
        self._closing = True
        await self._abort()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self.closed.set()

    async def _abort(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()


# Errors that no amount of reconnecting will fix.
_FATAL_CODES = {"invalid_api_key", "model_not_found", "invalid_model", "insufficient_quota"}


class ManagedRealtimeSession:
    """The class-long session: reconnection, renewal and bounded history.

    Runs on the backend's event loop. Audio arrives from the PortAudio thread,
    so `append_audio` is safe to call from any thread.
    """

    def __init__(
        self,
        api_key: str,
        config: events.SessionConfig,
        url: str = REALTIME_URL,
        ssl_context: ssl.SSLContext | None = None,
        max_session_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
        connect_timeout: float = CONNECT_TIMEOUT,
        connector: Connector | None = None,
    ) -> None:
        self._api_key = api_key
        self._config = config
        self._url = url
        self._ssl_context = ssl_context
        self._max_session_seconds = max_session_seconds
        self._reconnect_delays = reconnect_delays or (1.0,)
        self._connect_timeout = connect_timeout
        self._connector = connector

        self.state = ConnectionState.DISCONNECTED
        self.last_error: str | None = None
        self._listeners: list[Listener] = []
        self._connection: RealtimeConnection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._supervisor: asyncio.Task | None = None
        self._stopping = False
        self._lost = asyncio.Event()
        self._resampler = StreamingResampler(MICROPHONE_RATE, REALTIME_RATE)
        self._resampler_lock = threading.Lock()

        # A turn runs from the first audio of a question to response.done.
        self._in_turn = False
        self.sessions_opened = 0

    # -- listeners ---------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, event: events.ServerEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                logger.exception("Un oyente de la sesión Realtime falló.")

    def _set_state(self, state: ConnectionState, reason: str | None = None) -> None:
        if state is self.state and reason is None:
            return
        self.state = state
        if reason:
            self.last_error = reason
        self._emit(ConnectionChanged("session", state, reason))

    # -- lifecycle ---------------------------------------------------------

    async def start(self, keep_trying: bool = False) -> None:
        """Open the session, or raise if it cannot be opened even once.

        The first attempt is awaited so "Iniciar clase" can say at once that
        the key is wrong. After that, reconnection happens in the background.

        With `keep_trying`, only a fatal refusal raises: a network that is down
        at the start of a class is retried in the background like one that
        drops later, and the class goes on listening meanwhile.
        """
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._set_state(ConnectionState.CONNECTING)
        try:
            await self._open()
        except RealtimeUnavailable as exc:
            if keep_trying and not exc.fatal:
                self._set_state(ConnectionState.RECONNECTING, str(exc))
                self._supervisor = asyncio.create_task(
                    self._retry_then_supervise(str(exc)), name="realtime-supervisor"
                )
                return
            self._set_state(
                ConnectionState.FAILED if exc.fatal else ConnectionState.DISCONNECTED, str(exc)
            )
            raise
        self._supervisor = asyncio.create_task(self._supervise(), name="realtime-supervisor")

    async def _retry_then_supervise(self, why: str) -> None:
        await self._reconnect(why)
        if not self._stopping:
            await self._supervise()

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self._set_state(ConnectionState.DISCONNECTED)

    async def _open(self) -> None:
        self._lost.clear()
        connection = RealtimeConnection(
            api_key=self._api_key,
            config=self._config,
            on_event=self._on_event,
            url=self._url,
            ssl_context=self._ssl_context,
            connect_timeout=self._connect_timeout,
            connector=self._connector,
        )
        await connection.open()
        previous, self._connection = self._connection, connection
        if previous is not None:
            await previous.close()
        with self._resampler_lock:
            self._resampler.reset()
        self._in_turn = False
        self.sessions_opened += 1
        self._set_state(ConnectionState.READY)

    async def _supervise(self) -> None:
        """Reconnect when the connection drops; renew it when it gets old."""
        while not self._stopping:
            renew_in = self._renewal_due_in()
            try:
                await asyncio.wait_for(self._lost.wait(), timeout=max(renew_in, 0.05))
            except TimeoutError:
                if self._in_turn:
                    # Never mid-answer: look again shortly.
                    await asyncio.sleep(1.0)
                    continue
                logger.info("Renovando la sesión Realtime antes de que caduque.")
                await self._reconnect("renovación")
                continue
            if self._stopping:
                return
            await self._reconnect("conexión perdida")

    def _renewal_due_in(self) -> float:
        opened = self._connection.opened_at if self._connection else None
        if opened is None:
            return self._max_session_seconds
        return self._max_session_seconds - (time.monotonic() - opened)

    async def _reconnect(self, why: str) -> None:
        self._set_state(ConnectionState.RECONNECTING, why if why != "renovación" else None)
        attempt = 0
        while not self._stopping:
            try:
                await self._open()
                return
            except RealtimeUnavailable as exc:
                if exc.fatal:
                    self._set_state(ConnectionState.FAILED, str(exc))
                    self._stopping = True
                    return
                delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
                self._set_state(ConnectionState.RECONNECTING, str(exc))
                attempt += 1
                await asyncio.sleep(delay)

    # -- events from the API -----------------------------------------------

    def _on_event(self, event: events.ServerEvent) -> None:
        if isinstance(event, ConnectionChanged):
            if event.state is ConnectionState.DISCONNECTED and not self._stopping:
                self._in_turn = False
                self._lost.set()
            return

        if isinstance(event, events.SpeechStarted):
            self._in_turn = True
        elif isinstance(event, events.ResponseDone):
            self._in_turn = False
        elif isinstance(event, events.ApiError):
            logger.warning("Error de la API Realtime: %s (%s)", event.message, event.code)

        self._emit(event)

    # -- sending -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.state is ConnectionState.READY and self._connection is not None

    def _send_soon(self, event: dict[str, Any]) -> bool:
        """Queue an event from any thread. False when there is no session."""
        loop, connection = self._loop, self._connection
        if loop is None or connection is None or not self.ready or loop.is_closed():
            return False

        async def send() -> None:
            try:
                await connection.send(event)
            except Exception as exc:  # noqa: BLE001 - a lost socket is handled by the reader
                logger.debug("No se pudo enviar %s: %s", event.get("type"), exc)

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(send()))
        return True

    def append_audio(self, pcm16k: np.ndarray) -> bool:
        """Send one microphone frame (16 kHz int16). Safe from the audio thread."""
        if not self.ready:
            return False
        with self._resampler_lock:
            pcm24 = self._resampler.process(pcm16k)
        if pcm24.size == 0:
            return True
        self._in_turn = True
        return self._send_soon(events.append_audio(pcm24))

    def commit_audio(self) -> bool:
        return self._send_soon(events.commit_audio())

    def clear_audio(self) -> bool:
        with self._resampler_lock:
            self._resampler.reset()
        return self._send_soon(events.clear_audio())

    def create_response(self) -> bool:
        return self._send_soon(events.create_response())

    def cancel_response(self) -> bool:
        return self._send_soon(events.cancel_response())

    def truncate(self, item_id: str, audio_end_ms: int) -> bool:
        return self._send_soon(events.truncate_item(item_id, audio_end_ms))

    def end_turn(self) -> None:
        """For a turn that ends without a response (nothing was asked)."""
        self._in_turn = False
