"""HTTP and WebSocket surface, served on localhost only.

The backend holds the API key, so the loopback interface alone is not treated as
sufficient protection: every request must carry the token the backend generates
at startup and hands to the Tauri shell over stdout. Any other process on the
machine that guesses the port still cannot read the key.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__
from ..audio.devices import probe_devices
from ..config.secrets import WrongPassphrase
from ..config.settings import Settings
from ..config.store import SettingsStore
from ..diagnostics.runner import DiagnosticsReport, DiagnosticsRunner
from ..session.controller import ClassNotReady, SessionController
from ..session.state import Event, InvalidTransition
from .schemas import (
    ApiKeyIn,
    DevicesOut,
    DiagnosticsOut,
    EventIn,
    ListeningOut,
    SettingsOut,
    StateOut,
    TransitionOut,
    UnlockIn,
)

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-AIClassroom-Token"


@dataclass
class AppContext:
    """Everything a request handler may need, assembled once at startup."""

    store: SettingsStore
    controller: SessionController
    runner: DiagnosticsRunner
    token: str
    last_report: DiagnosticsReport | None = None


class EventHub:
    """Fans state transitions out to every connected UI window."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def publish_threadsafe(self, message: dict) -> None:
        """Called from the audio thread, where there is no running loop."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._publish, message)

    def _publish(self, message: dict) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A window that cannot keep up loses intermediate frames rather
                # than stalling the audio thread that produced them.
                logger.debug("Cola de eventos llena; se descarta un mensaje.")


def create_app(context: AppContext) -> FastAPI:
    hub = EventHub()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        # The audio thread publishes through the hub, so it needs the loop that
        # is only running once the server has actually started.
        hub.bind(asyncio.get_running_loop())
        context.controller.machine.subscribe(
            lambda transition: hub.publish_threadsafe(
                {"type": "state", "payload": TransitionOut.of(transition).model_dump(mode="json")}
            )
        )
        context.controller.detection_subscribers.append(
            lambda detection: hub.publish_threadsafe(
                {
                    "type": "wakeword",
                    "payload": {
                        "phrase": detection.phrase,
                        "score": detection.score,
                        "at": detection.at.isoformat(),
                    },
                }
            )
        )
        yield
        context.controller.stop()

    app = FastAPI(
        title="AI Classroom Live",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.hub = hub

    def require_token(token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> None:
        if not secrets.compare_digest(token or "", context.token):
            raise HTTPException(status_code=401, detail="Token de sesión no válido.")

    guarded = [Depends(require_token)]

    @app.exception_handler(InvalidTransition)
    async def _invalid_transition(_request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ClassNotReady)
    async def _class_not_ready(_request, exc: ClassNotReady) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # -- health -----------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        """Unguarded on purpose: the shell polls it before it has anything else."""
        return {"status": "ok", "version": __version__}

    # -- state ------------------------------------------------------------

    @app.get("/api/state", dependencies=guarded, response_model=StateOut)
    async def get_state() -> StateOut:
        return StateOut.of(context.controller.machine)

    @app.post("/api/state/event", dependencies=guarded, response_model=StateOut)
    async def dispatch_event(body: EventIn) -> StateOut:
        try:
            event = Event(body.event)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Evento desconocido: {body.event}"
            ) from None
        context.controller.machine.dispatch(event, reason=body.reason)
        return StateOut.of(context.controller.machine)

    # -- class lifecycle --------------------------------------------------

    @app.post("/api/class/prepare", dependencies=guarded, response_model=StateOut)
    async def prepare_class() -> StateOut:
        await asyncio.to_thread(context.controller.prepare)
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/start", dependencies=guarded, response_model=StateOut)
    async def start_class() -> StateOut:
        # Opening PortAudio blocks, so it must not run on the event loop.
        await asyncio.to_thread(context.controller.start_class)
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/pause", dependencies=guarded, response_model=StateOut)
    async def pause_class() -> StateOut:
        await asyncio.to_thread(context.controller.pause)
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/resume", dependencies=guarded, response_model=StateOut)
    async def resume_class() -> StateOut:
        await asyncio.to_thread(context.controller.resume)
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/stop", dependencies=guarded, response_model=StateOut)
    async def stop_class() -> StateOut:
        await asyncio.to_thread(context.controller.stop)
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/recover", dependencies=guarded, response_model=StateOut)
    async def recover_class() -> StateOut:
        await asyncio.to_thread(context.controller.recover)
        return StateOut.of(context.controller.machine)

    @app.get("/api/listening", dependencies=guarded, response_model=ListeningOut)
    async def listening_status() -> ListeningOut:
        return ListeningOut.of(context.controller.listening_status())

    # -- devices and settings --------------------------------------------

    @app.get("/api/devices", dependencies=guarded, response_model=DevicesOut)
    async def devices() -> DevicesOut:
        return DevicesOut.of(await asyncio.to_thread(probe_devices))

    def settings_payload(settings: Settings | None = None) -> SettingsOut:
        return SettingsOut(
            settings=settings if settings is not None else context.store.load(),
            api_key_configured=context.store.has_api_key(),
            requires_passphrase=context.store.requires_passphrase,
            unlocked=context.store.unlocked,
        )

    @app.get("/api/settings", dependencies=guarded, response_model=SettingsOut)
    async def get_settings() -> SettingsOut:
        return settings_payload()

    @app.put("/api/settings", dependencies=guarded, response_model=SettingsOut)
    async def put_settings(body: Settings) -> SettingsOut:
        context.store.save(body)
        return settings_payload(body)

    @app.post("/api/settings/api-key", dependencies=guarded, status_code=204)
    async def put_api_key(body: ApiKeyIn) -> None:
        try:
            context.store.set_api_key(body.api_key, passphrase=body.passphrase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - DPAPI can refuse
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/settings/unlock", dependencies=guarded, response_model=SettingsOut)
    async def unlock(body: UnlockIn) -> SettingsOut:
        try:
            # scrypt is deliberately slow, so it must not block the event loop.
            await asyncio.to_thread(context.store.unlock, body.passphrase)
        except WrongPassphrase as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return settings_payload()

    @app.delete("/api/settings/api-key", dependencies=guarded, status_code=204)
    async def delete_api_key() -> None:
        context.store.clear_api_key()

    # -- diagnostics ------------------------------------------------------

    @app.post("/api/diagnostics/run", dependencies=guarded, response_model=DiagnosticsOut)
    async def run_diagnostics() -> DiagnosticsOut:
        report = await context.runner.run()
        context.last_report = report
        return DiagnosticsOut.of(report)

    @app.get("/api/diagnostics/last", dependencies=guarded, response_model=DiagnosticsOut | None)
    async def last_diagnostics() -> DiagnosticsOut | None:
        if context.last_report is None:
            return None
        return DiagnosticsOut.of(context.last_report)

    # -- events -----------------------------------------------------------

    @app.websocket("/ws/events")
    async def events(websocket: WebSocket) -> None:
        if not secrets.compare_digest(
            websocket.query_params.get("token", ""), context.token
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = hub.subscribe()
        try:
            await websocket.send_json(
                {"type": "state", "payload": StateOut.of(context.controller.machine).model_dump(
                    mode="json"
                )}
            )
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    return app


def build_context(
    store: SettingsStore | None = None,
    controller: SessionController | None = None,
    runner: DiagnosticsRunner | None = None,
    token: str | None = None,
) -> AppContext:
    store = store or SettingsStore()
    controller = controller or SessionController(store)
    return AppContext(
        store=store,
        controller=controller,
        runner=runner or DiagnosticsRunner(store),
        token=token or secrets.token_urlsafe(32),
    )

