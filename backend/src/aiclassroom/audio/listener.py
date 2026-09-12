"""Passive listening: joins the microphone, the detector and the state machine.

Spec section 16 is the load-bearing requirement here -- no audio leaves the
machine while the assistant is waiting. This class never touches the network:
frames go from PortAudio into the detector and are dropped. Only an activation
produces an event, and in Phase 0 that event just moves the state machine and
increments a counter.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from ..session.state import Event, SessionStateMachine, State
from .engine import AudioEngine
from .wakeword import Detection, WakeWordDetector

logger = logging.getLogger(__name__)


@dataclass
class ListenerStats:
    """Spec section 15 asks for activation counts and timings per session."""

    activations: int = 0
    frames_processed: int = 0
    interruptions: int = 0
    started_at: datetime | None = None
    last_activation_at: datetime | None = None
    detections: list[Detection] = field(default_factory=list)

    @property
    def seconds_listening(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now(UTC) - self.started_at).total_seconds()


class WakeWordListener:
    """Runs passive listening for the length of a class."""

    def __init__(
        self,
        engine: AudioEngine,
        detector: WakeWordDetector,
        machine: SessionStateMachine,
        on_detection: Callable[[Detection], None] | None = None,
    ) -> None:
        self._engine = engine
        self._detector = detector
        self._machine = machine
        self._on_detection = on_detection
        self._lock = threading.RLock()
        self.stats = ListenerStats()

    @property
    def is_listening(self) -> bool:
        return self._engine.is_capturing

    def start(self) -> None:
        """Open the microphone. The caller owns the state transition."""
        with self._lock:
            if self._engine.is_capturing:
                return
            self._detector.reset()
            self.stats = ListenerStats(started_at=datetime.now(UTC))
            self._engine.start_capture(self._handle_frame)

    def stop(self) -> None:
        with self._lock:
            self._engine.stop_capture()
            self._detector.reset()

    def _handle_frame(self, frame: np.ndarray) -> None:
        """Called on the PortAudio thread, once per 80 ms."""
        self.stats.frames_processed += 1
        detection = self._detector.process(frame)
        if detection is None:
            return
        self._handle_detection(detection)

    def _handle_detection(self, detection: Detection) -> None:
        state = self._machine.state
        if state in (State.SPEAKING, State.THINKING):
            # Somebody spoke over the assistant: cancel the response at once
            # (spec section 6.2) rather than queueing a second activation.
            self._engine.stop_playback()
            if self._machine.try_dispatch(
                Event.INTERRUPT, reason=f"Nueva intervención ({detection.score:.2f})"
            ):
                self.stats.interruptions += 1
            return

        transition = self._machine.try_dispatch(
            Event.WAKE_WORD_DETECTED, reason=f"'{detection.phrase}' ({detection.score:.2f})"
        )
        if transition is None:
            # Paused or already activated: a late frame, not an error.
            logger.debug("Activación descartada en el estado %s.", state)
            return

        self.stats.activations += 1
        self.stats.last_activation_at = detection.at
        self.stats.detections.append(detection)
        if self._on_detection is not None:
            try:
                self._on_detection(detection)
            except Exception:  # noqa: BLE001 - a subscriber must not kill capture
                logger.exception("El manejador de activación falló.")
