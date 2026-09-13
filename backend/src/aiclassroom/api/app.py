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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..audio.devices import probe_devices
from ..audio.engine import AudioError
from ..config.secrets import WrongPassphrase
from ..config.settings import Settings
from ..config.store import SettingsStore
from ..diagnostics.runner import DiagnosticsReport, DiagnosticsRunner
from ..realtime.probe import ConversationProbe, ProbeUnavailable
from ..realtime.session import RealtimeUnavailable
from ..session.controller import ClassNotReady, SessionController
from ..session.state import Event, InvalidTransition, State
from ..session.turn import TurnController
from ..voice.session import (
    Kind,
    VoiceBusy,
    VoiceNotReady,
    VoiceTrainingSession,
    capture_with_engine,
)
from ..voice.takes import RecordingRejected
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

# The window is not served from this backend: Tauri serves the built page from
# its own origin and the page then calls http://127.0.0.1:<port>, which the
# browser treats as cross-origin. Without these headers every request fails the
# preflight and the application cannot talk to itself at all.
#
# The origins Tauri uses differ by platform and by whether it is a development
# run: tauri://localhost on Linux and macOS, http://tauri.localhost on Windows,
# and the Vite server during development. Anything else is refused -- a page
# the teacher happens to visit has no business reaching this API, even though
# it would also need the session token.
ALLOWED_ORIGIN_PATTERN = (
    r"^(tauri://localhost"
    r"|https?://tauri\.localhost"
    r"|https?://localhost(:\d+)?"
    r"|https?://127\.0\.0\.1(:\d+)?)$"
)


@dataclass
class AppContext:
    """Everything a request handler may need, assembled once at startup."""

    store: SettingsStore
    controller: SessionController
    runner: DiagnosticsRunner
    token: str
    last_report: DiagnosticsReport | None = None
    voice: VoiceTrainingSession = None  # type: ignore[assignment]
    probe: ConversationProbe = None  # type: ignore[assignment]
    turns: TurnController = None  # type: ignore[assignment]
    #: Whether a class talks to the Realtime API (H3). Off, a class only
    #: listens for the wake phrase, as in Phase 0 -- which is what the API
    #: surface tests need, with no key and no network.
    conversation: bool = True

    def __post_init__(self) -> None:
        if self.turns is None:
            self.turns = TurnController(controller=self.controller, store=self.store)
        # Both record through the controller's engine, so they use the same
        # microphone as a class, and each refuses while the class or the other
        # one holds it.
        controller = self.controller
        if self.voice is None:
            self.voice = VoiceTrainingSession(
                store=self.store,
                capture=capture_with_engine(controller.engine_factory),
                microphone_in_use=lambda: (
                    controller.machine.is_microphone_active or self.probe.running
                ),
            )
        if self.probe is None:
            self.probe = ConversationProbe(
                store=self.store,
                engine_factory=controller.engine_factory,
                microphone_in_use=lambda: (
                    controller.machine.is_microphone_active
                    or self.voice.status()["recording"]
                ),
            )


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
        # Questions and answers as text reach the interface over the same socket.
        context.turns.set_publisher(hub.publish_threadsafe)
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
        await context.probe.cancel()
        context.controller.stop()
        await context.turns.close()

    app = FastAPI(
        title="AI Classroom Live",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=ALLOWED_ORIGIN_PATTERN,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
        max_age=600,
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
        if context.probe.running or context.voice.status()["recording"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "El micrófono está ocupado con una prueba o una grabación. "
                    "Espera a que termine."
                ),
            )
        resuming = context.controller.machine.state is State.PAUSED
        if context.conversation and not resuming and not context.turns.active:
            # The session opens before the microphone: a refused key stops the
            # class here, with the reason, instead of after it seems to start.
            try:
                await context.turns.open()
            except RealtimeUnavailable as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            # Opening PortAudio blocks, so it must not run on the event loop.
            await asyncio.to_thread(context.controller.start_class)
        except Exception:
            if not resuming:
                await context.turns.close()
            raise
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
        await context.turns.close()
        return StateOut.of(context.controller.machine)

    @app.post("/api/class/recover", dependencies=guarded, response_model=StateOut)
    async def recover_class() -> StateOut:
        await asyncio.to_thread(context.controller.recover)
        await context.turns.close()
        return StateOut.of(context.controller.machine)

    @app.get("/api/turn", dependencies=guarded)
    async def turn_status() -> dict:
        """The Realtime connection and the current or last question (H3)."""
        return context.turns.status()

    @app.post("/api/turn/stop", dependencies=guarded)
    async def stop_answer() -> dict:
        """The emergency stop: cut the answer as "Oye Chat" would (plan-fase-1, H4)."""
        machine = context.controller.machine
        if machine.state not in (State.THINKING, State.SPEAKING):
            raise HTTPException(status_code=409, detail="No hay ninguna respuesta que parar.")
        engine = context.controller.engine
        if engine is not None:
            engine.stop_playback()
        machine.try_dispatch(Event.INTERRUPT, reason="Parada manual")
        return context.turns.status()

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

    # response_class matters here: FastAPI would otherwise label the empty 204
    # as application/json, and a 204 carrying content headers makes Chromium
    # abort the response. The write succeeds and the interface still reports a
    # failure, which is a confusing way to save a key.
    @app.post(
        "/api/settings/api-key",
        dependencies=guarded,
        status_code=204,
        response_class=Response,
    )
    async def put_api_key(body: ApiKeyIn) -> Response:
        try:
            context.store.set_api_key(body.api_key, passphrase=body.passphrase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - DPAPI can refuse
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.post("/api/settings/unlock", dependencies=guarded, response_model=SettingsOut)
    async def unlock(body: UnlockIn) -> SettingsOut:
        try:
            # scrypt is deliberately slow, so it must not block the event loop.
            await asyncio.to_thread(context.store.unlock, body.passphrase)
        except WrongPassphrase as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return settings_payload()

    @app.delete(
        "/api/settings/api-key",
        dependencies=guarded,
        status_code=204,
        response_class=Response,
    )
    async def delete_api_key() -> Response:
        context.store.clear_api_key()
        return Response(status_code=204)

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

    # -- training with the teacher's voice (D-12) ---------------------------

    def voice_kind(kind: str) -> Kind:
        try:
            return Kind(kind)
        except ValueError:
            raise HTTPException(
                status_code=404, detail=f"Tipo de grabación desconocido: {kind}"
            ) from None

    def voice_call(action) -> dict:
        try:
            action()
        except VoiceBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VoiceNotReady as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return context.voice.status()

    @app.get("/api/voice", dependencies=guarded)
    async def voice_status() -> dict:
        return context.voice.status()

    @app.post("/api/voice/takes/{kind}/{slot}", dependencies=guarded)
    async def record_take(kind: str, slot: int) -> dict:
        chosen = voice_kind(kind)
        try:
            # Blocks for the length of the take, so it runs off the event loop.
            await asyncio.to_thread(context.voice.record, chosen, slot)
        except RecordingRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (VoiceBusy, VoiceNotReady) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AudioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return context.voice.status()

    @app.delete("/api/voice/takes/{kind}/{slot}", dependencies=guarded)
    async def forget_take(kind: str, slot: int) -> dict:
        chosen = voice_kind(kind)
        return voice_call(lambda: context.voice.forget(chosen, slot))

    @app.post("/api/voice/train", dependencies=guarded)
    async def train_voice() -> dict:
        return voice_call(context.voice.start_training)

    @app.post("/api/voice/accept", dependencies=guarded)
    async def accept_voice_model() -> dict:
        return voice_call(context.voice.accept)

    @app.post("/api/voice/discard", dependencies=guarded)
    async def discard_voice_model() -> dict:
        return voice_call(context.voice.discard)

    @app.post("/api/voice/restore", dependencies=guarded)
    async def restore_original_model() -> dict:
        return voice_call(context.voice.restore_original)

    # -- one real question (plan-fase-1, H1) -----------------------------

    async def probe_call(action) -> dict:
        try:
            await action()
        except ProbeUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return context.probe.status()

    @app.get("/api/conversation-test", dependencies=guarded)
    async def conversation_test_status() -> dict:
        return context.probe.status()

    @app.post("/api/conversation-test/start", dependencies=guarded)
    async def start_conversation_test() -> dict:
        return await probe_call(context.probe.start)

    @app.post("/api/conversation-test/cancel", dependencies=guarded)
    async def cancel_conversation_test() -> dict:
        return await probe_call(context.probe.cancel)

    @app.post("/api/conversation-test/play", dependencies=guarded)
    async def play_conversation_test() -> dict:
        return await probe_call(context.probe.play)

    @app.post("/api/conversation-test/stop-playback", dependencies=guarded)
    async def stop_conversation_playback() -> dict:
        return await probe_call(context.probe.stop)

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
    voice: VoiceTrainingSession | None = None,
) -> AppContext:
    store = store or SettingsStore()
    controller = controller or SessionController(store)
    return AppContext(
        store=store,
        controller=controller,
        runner=runner or DiagnosticsRunner(store),
        token=token or secrets.token_urlsafe(32),
        voice=voice,
    )

