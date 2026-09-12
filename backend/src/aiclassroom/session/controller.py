"""Coordinates the state machine, the microphone and the detector.

Everything the user interface can do to a class goes through here, so the HTTP
layer stays a thin translation of requests into these calls, and the tests can
drive a whole class without starting a server.

The engine and the detector arrive as factories: production passes the PortAudio
engine and the openWakeWord detector, tests pass their fakes (D-04, D-05).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..audio.engine import AudioEngine, AudioError, SoundDeviceAudioEngine
from ..audio.listener import WakeWordListener
from ..audio.wakeword import Detection, WakeWordDetector, WakeWordUnavailable, create_detector
from ..config.settings import Settings
from ..config.store import SettingsStore
from .state import Event, InvalidTransition, SessionStateMachine, State

logger = logging.getLogger(__name__)

EngineFactory = Callable[[Settings], AudioEngine]
DetectorFactory = Callable[[Settings], WakeWordDetector]


class ClassNotReady(RuntimeError):
    """Raised when a class is asked to start but something prevents it."""


@dataclass(frozen=True)
class ListeningStatus:
    listening: bool
    activations: int
    interruptions: int
    echo_suppressions: int
    frames_processed: int
    seconds_listening: float
    threshold: float | None
    recent_scores: list[float]
    phrase: str | None
    vad_enabled: bool
    confirmation_frames: int | None


def _default_engine(settings: Settings) -> AudioEngine:
    return SoundDeviceAudioEngine(
        input_device=settings.input_device, output_device=settings.output_device
    )


class SessionController:
    def __init__(
        self,
        store: SettingsStore,
        machine: SessionStateMachine | None = None,
        engine_factory: EngineFactory | None = None,
        detector_factory: DetectorFactory | None = None,
    ) -> None:
        self.store = store
        self.machine = machine or SessionStateMachine()
        self._engine_factory = engine_factory or _default_engine
        self._detector_factory = detector_factory or self._default_detector
        self._engine: AudioEngine | None = None
        self._detector: WakeWordDetector | None = None
        self._listener: WakeWordListener | None = None
        self._lock = threading.RLock()
        self.detection_subscribers: list[Callable[[Detection], None]] = []

    def _default_detector(self, settings: Settings) -> WakeWordDetector:
        return create_detector(
            models_dir=self.store.paths.models_dir,
            phrase=settings.wake_phrase,
            sensitivity=settings.wake_sensitivity,
            refractory_seconds=settings.wake_refractory_seconds,
            vad_threshold=settings.wake_vad_threshold,
            confirmation_frames=settings.wake_confirmation_frames,
        )

    # -- lifecycle --------------------------------------------------------

    def prepare(self) -> State:
        """Move IDLE -> READY.

        Phase 0 has no materials to load, so preparation is immediate. Phase 1
        will do its document work between these two transitions, which is why
        PREPARING exists as a state of its own rather than being skipped.
        """
        with self._lock:
            if self.machine.state is State.READY:
                return State.READY
            if self.machine.state in (State.STOPPED, State.ERROR):
                self.machine.dispatch(Event.RESET, reason="Nueva sesión")
            self.machine.dispatch(Event.CREATE_SESSION)
            self.machine.dispatch(Event.SESSION_PREPARED)
            return self.machine.state

    def start_class(self) -> State:
        """Open the microphone and begin passive listening."""
        with self._lock:
            if self.machine.state is State.PAUSED:
                return self.resume()
            if not self.machine.can(Event.START_CLASS):
                raise InvalidTransition(self.machine.state, Event.START_CLASS)

            settings = self.store.load()
            try:
                self._detector = self._detector_factory(settings)
            except WakeWordUnavailable as exc:
                self.machine.dispatch(Event.FAIL, reason=str(exc))
                raise ClassNotReady(str(exc)) from exc

            self._engine = self._engine_factory(settings)
            self._listener = WakeWordListener(
                engine=self._engine,
                detector=self._detector,
                machine=self.machine,
                on_detection=self._notify_detection,
                echo_guard_margin=settings.echo_guard_margin,
            )
            try:
                self._listener.start()
            except AudioError as exc:
                self._teardown()
                self.machine.dispatch(Event.FAIL, reason=str(exc))
                raise ClassNotReady(str(exc)) from exc

            # Only now is the microphone genuinely open, so only now may the
            # state say so (spec section 19).
            self.machine.dispatch(Event.START_CLASS)
            return self.machine.state

    def pause(self) -> State:
        with self._lock:
            self.machine.dispatch(Event.PAUSE, reason="Pausa solicitada")
            # Close the microphone straight away: PAUSED must not keep listening.
            if self._listener is not None:
                self._listener.stop()
            return self.machine.state

    def resume(self) -> State:
        with self._lock:
            if self.machine.state is not State.PAUSED:
                raise InvalidTransition(self.machine.state, Event.RESUME)
            if self._listener is None:
                raise ClassNotReady("La sesión no tiene un motor de audio activo.")
            try:
                self._listener.start()
            except AudioError as exc:
                self.machine.dispatch(Event.FAIL, reason=str(exc))
                raise ClassNotReady(str(exc)) from exc
            self.machine.dispatch(Event.RESUME)
            return self.machine.state

    def stop(self) -> State:
        with self._lock:
            self._teardown()
            if self.machine.can(Event.STOP):
                self.machine.dispatch(Event.STOP, reason="Sesión finalizada")
            return self.machine.state

    def acknowledge_interruption(self) -> State:
        """Return to passive listening after a cancelled response."""
        with self._lock:
            self.machine.dispatch(Event.INTERRUPTION_HANDLED)
            return self.machine.state

    def recover(self) -> State:
        with self._lock:
            self._teardown()
            self.machine.dispatch(Event.RECOVER, reason="Error resuelto")
            return self.machine.state

    def _teardown(self) -> None:
        if self._listener is not None:
            self._listener.stop()
        if self._engine is not None:
            self._engine.stop_playback()

    # -- reporting --------------------------------------------------------

    def listening_status(self) -> ListeningStatus:
        listener, detector = self._listener, self._detector
        stats = listener.stats if listener is not None else None
        return ListeningStatus(
            listening=bool(listener and listener.is_listening),
            activations=stats.activations if stats else 0,
            interruptions=stats.interruptions if stats else 0,
            echo_suppressions=stats.echo_suppressions if stats else 0,
            frames_processed=stats.frames_processed if stats else 0,
            seconds_listening=stats.seconds_listening if stats else 0.0,
            threshold=getattr(detector, "threshold", None),
            recent_scores=detector.recent_scores() if detector else [],
            phrase=detector.phrase if detector else None,
            vad_enabled=bool(getattr(detector, "vad_enabled", False)),
            confirmation_frames=getattr(detector, "confirmation_frames", None),
        )

    def _notify_detection(self, detection: Detection) -> None:
        for subscriber in list(self.detection_subscribers):
            try:
                subscriber(detection)
            except Exception:  # noqa: BLE001
                logger.exception("Un suscriptor de activaciones falló.")
