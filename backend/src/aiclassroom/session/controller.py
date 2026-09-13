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

# Phase 0 has no conversation, so an activation has nothing to lead into. The
# state is held this long -- enough for the interface to show "Activado" -- and
# then returns to passive listening so the next "Oye Chat" can be heard.
ACTIVATION_HOLD_SECONDS = 2.0

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
    #: Loudest microphone level of the last half second, 0..1.
    input_level: float = 0.0
    #: Silero's speech probability, or None when the voice filter is off.
    speech_probability: float | None = None


def _speech_probability(detector: WakeWordDetector | None) -> float | None:
    """Only the real detector has a voice filter to ask."""
    probe = getattr(detector, "speech_probability", None)
    if not callable(probe):
        return None
    try:
        return probe()
    except Exception:  # noqa: BLE001 - a meter must never break the status
        logger.debug("No se pudo leer la probabilidad de voz.", exc_info=True)
        return None


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
        activation_hold_seconds: float = ACTIVATION_HOLD_SECONDS,
    ) -> None:
        self.store = store
        self._activation_hold_seconds = activation_hold_seconds
        self._activation_timer: threading.Timer | None = None
        self._timer_lock = threading.RLock()
        self.machine = machine or SessionStateMachine()
        self._engine_factory = engine_factory or _default_engine
        self._detector_factory = detector_factory or self._default_detector
        self._engine: AudioEngine | None = None
        self._detector: WakeWordDetector | None = None
        self._listener: WakeWordListener | None = None
        self._lock = threading.RLock()
        self.detection_subscribers: list[Callable[[Detection], None]] = []

    @property
    def engine_factory(self) -> EngineFactory:
        """Shared with voice training, so a take uses the same microphone as a class."""
        return self._engine_factory

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
            self._cancel_activation_timer()
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
        self._cancel_activation_timer()
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
            input_level=stats.input_level if stats else 0.0,
            speech_probability=_speech_probability(detector),
        )

    def _notify_detection(self, detection: Detection) -> None:
        self._schedule_activation_expiry()
        for subscriber in list(self.detection_subscribers):
            try:
                subscriber(detection)
            except Exception:  # noqa: BLE001
                logger.exception("Un suscriptor de activaciones falló.")

    # -- phase 0: return to listening after an activation -----------------

    def _schedule_activation_expiry(self) -> None:
        """Called on the audio thread, so the wait happens on a timer thread.

        Deliberately not under `self._lock`: stop() holds that lock while it
        closes the stream, and closing waits for this very callback to return.
        """
        with self._timer_lock:
            self._cancel_activation_timer()
            timer = threading.Timer(self._activation_hold_seconds, self._expire_activation)
            timer.daemon = True
            self._activation_timer = timer
            timer.start()

    def _expire_activation(self) -> None:
        with self._timer_lock:
            self._activation_timer = None
        # The machine has its own lock. Paused or stopped in the meantime means
        # there is nothing to return to, which try_dispatch simply declines.
        self.machine.try_dispatch(
            Event.ACTIVATION_EXPIRED,
            reason="Fase 0: sin conversación todavía, vuelve a escuchar",
        )

    def _cancel_activation_timer(self) -> None:
        with self._timer_lock:
            if self._activation_timer is not None:
                self._activation_timer.cancel()
                self._activation_timer = None
