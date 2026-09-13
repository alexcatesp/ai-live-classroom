"""Realtime API client (D-06).

Phase 0 only performs the handshake (D-03): open the WebSocket, confirm the key
is valid and the model is authorised for the account, then close. Streaming a
real turn is Phase 1 work, which is why this interface exposes just one method
for now -- adding `stream_turn` later does not change what already exists.

The key lives on this side of the localhost boundary and never reaches the
frontend (spec section 4.2).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import ssl
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_TIMEOUT = 15.0


class HandshakeStatus(StrEnum):
    OK = "ok"
    INVALID_KEY = "invalid_key"
    MODEL_NOT_AUTHORISED = "model_not_authorised"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"


# Risk R-2: whether a WebSocket from the backend is fast enough, or whether
# WebRTC from the frontend is needed. The handshake is timed in two parts
# because they answer different questions. The network part (DNS, TCP, TLS and
# the upgrade) is what the school contributes and what varies between
# classrooms; above this, the network is worth investigating first.
NETWORK_WARNING_SECONDS = 1.5
# The session part is OpenAI creating the session once the socket is open. It
# depends on the service, not on the classroom, and is paid once per class, so
# it gets a looser bar and a remedy that does not send anyone after the network.
SESSION_WARNING_SECONDS = 3.0


@dataclass(frozen=True)
class HandshakeResult:
    status: HandshakeStatus
    detail: str
    session_id: str | None = None
    model: str | None = None
    remedy: str | None = None
    #: Seconds until the WebSocket was open: DNS, TCP, TLS and the upgrade.
    network_seconds: float | None = None
    #: Seconds from the open socket to `session.created`: time spent at OpenAI.
    session_seconds: float | None = None
    #: Seconds for the whole check, including any phase that never finished.
    elapsed_seconds: float | None = None

    @property
    def ok(self) -> bool:
        return self.status is HandshakeStatus.OK

    @property
    def slow_network(self) -> bool:
        return (
            self.ok
            and self.network_seconds is not None
            and self.network_seconds > NETWORK_WARNING_SECONDS
        )

    @property
    def slow_session(self) -> bool:
        return (
            self.ok
            and self.session_seconds is not None
            and self.session_seconds > SESSION_WARNING_SECONDS
        )


class RealtimeClient(Protocol):
    async def check_connection(self, model: str) -> HandshakeResult:
        """Open a Realtime session, verify it, and close it again."""


class WebSocketRealtimeClient:
    """WebSocket transport, per D-06."""

    def __init__(
        self,
        api_key: str,
        url: str = REALTIME_URL,
        timeout: float = DEFAULT_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = url
        self._timeout = timeout
        self._ssl_context = ssl_context
        # Set by the handshake as soon as the socket opens, so a timeout can
        # still say whether the network or the service was the one waiting.
        self._network_seconds: float | None = None

    async def check_connection(self, model: str) -> HandshakeResult:
        # Loaded before the clock starts: in the packaged application the first
        # import takes a noticeable fraction of a second, and it would otherwise
        # be reported as network latency.
        importlib.import_module("websockets")

        self._network_seconds = None
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(self._handshake(model), timeout=self._timeout)
        except TimeoutError:
            elapsed = time.monotonic() - started
            if self._network_seconds is not None:
                # The socket opened, so the network did its part.
                return HandshakeResult(
                    status=HandshakeStatus.TIMEOUT,
                    detail=(
                        f"La conexión se abrió en {self._network_seconds:.2f} s, pero la API "
                        f"no creó la sesión en {self._timeout:.0f} segundos."
                    ),
                    remedy=(
                        "La red funciona; el retraso está en el servicio de OpenAI. "
                        "Vuelve a comprobarlo en unos minutos."
                    ),
                    network_seconds=self._network_seconds,
                    elapsed_seconds=elapsed,
                )
            return HandshakeResult(
                status=HandshakeStatus.TIMEOUT,
                detail=f"La API no respondió en {self._timeout:.0f} segundos.",
                remedy=(
                    "Puede que la red del centro esté filtrando las conexiones WebSocket. "
                    "Consúltalo con el administrador del aula."
                ),
                elapsed_seconds=elapsed,
            )

        return replace(result, elapsed_seconds=time.monotonic() - started)

    async def _handshake(self, model: str) -> HandshakeResult:
        import websockets
        from websockets.exceptions import InvalidStatus, WebSocketException

        url = f"{self._url}?model={model}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            kwargs = {"additional_headers": headers, "open_timeout": self._timeout}
            if self._ssl_context is not None:
                kwargs["ssl"] = self._ssl_context
            started = time.monotonic()
            async with websockets.connect(url, **kwargs) as connection:
                opened = time.monotonic()
                self._network_seconds = opened - started
                raw = await connection.recv()
                result = self._interpret_first_event(raw, model)
                return replace(
                    result,
                    network_seconds=self._network_seconds,
                    session_seconds=time.monotonic() - opened,
                )
        except InvalidStatus as exc:
            return self._interpret_http_status(exc, model)
        except (OSError, WebSocketException) as exc:
            return HandshakeResult(
                status=HandshakeStatus.UNREACHABLE,
                detail=f"No se pudo abrir la conexión con la API: {exc}",
                remedy="Comprueba la conexión a internet y el cortafuegos del equipo.",
            )

    def _interpret_first_event(self, raw: str | bytes, model: str) -> HandshakeResult:
        """The first frame of a healthy session is `session.created`."""
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return HandshakeResult(
                status=HandshakeStatus.UNREACHABLE,
                detail="La API respondió algo que no se pudo interpretar.",
                remedy=(
                    "Puede que un proxy del centro esté respondiendo en lugar de la API. "
                    "Consúltalo con el administrador del aula."
                ),
            )

        event_type = event.get("type")
        if event_type == "session.created":
            session = event.get("session") or {}
            return HandshakeResult(
                status=HandshakeStatus.OK,
                detail=f"Sesión Realtime establecida con el modelo {session.get('model', model)}.",
                session_id=session.get("id"),
                model=session.get("model", model),
            )
        if event_type == "error":
            error = event.get("error") or {}
            message = error.get("message", "La API devolvió un error.")
            if error.get("code") in {"model_not_found", "invalid_model"}:
                return HandshakeResult(
                    status=HandshakeStatus.MODEL_NOT_AUTHORISED,
                    detail=message,
                    remedy=(
                        f"La cuenta no tiene acceso al modelo '{model}'. "
                        "Revisa en la configuración cuál está autorizado."
                    ),
                )
            return HandshakeResult(
                status=HandshakeStatus.BLOCKED,
                detail=message,
                remedy="Revisa el estado y los límites de la cuenta antes de empezar la clase.",
            )
        return HandshakeResult(
            status=HandshakeStatus.UNREACHABLE,
            detail=f"Respuesta inesperada de la API: {event_type}.",
            remedy="Vuelve a ejecutar el diagnóstico; si persiste, revisa el modelo configurado.",
        )

    def _interpret_http_status(self, exc, model: str) -> HandshakeResult:
        """The upgrade was rejected before any WebSocket frame arrived."""
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (401, 403):
            return HandshakeResult(
                status=HandshakeStatus.INVALID_KEY,
                detail=f"La API rechazó la clave (HTTP {code}).",
                remedy="Introduce de nuevo la clave en la pantalla de configuración.",
            )
        if code == 404:
            return HandshakeResult(
                status=HandshakeStatus.MODEL_NOT_AUTHORISED,
                detail=f"El modelo '{model}' no existe o no está autorizado en esta cuenta.",
                remedy="Elige otro modelo en la configuración.",
            )
        if code == 429:
            return HandshakeResult(
                status=HandshakeStatus.BLOCKED,
                detail="La cuenta ha alcanzado su límite de uso (HTTP 429).",
                remedy="Revisa el consumo y los límites de la cuenta.",
            )
        if code == 407 or (code is not None and 500 <= code < 600):
            return HandshakeResult(
                status=HandshakeStatus.BLOCKED,
                detail=f"La conexión fue rechazada con HTTP {code}.",
                remedy=(
                    "Es posible que un proxy del centro se interponga. "
                    "Consúltalo con el administrador."
                ),
            )
        return HandshakeResult(
            status=HandshakeStatus.UNREACHABLE,
            detail=f"La conexión fue rechazada (HTTP {code}).",
            remedy="Comprueba la conexión del equipo y el cortafuegos del centro.",
        )


class StubRealtimeClient:
    """Returns a fixed result. Used by the tests and when no key is configured."""

    def __init__(self, result: HandshakeResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def check_connection(self, model: str) -> HandshakeResult:
        self.calls.append(model)
        return self._result
