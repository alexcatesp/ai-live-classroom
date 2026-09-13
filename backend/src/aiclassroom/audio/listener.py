"""Passive listening: joins the microphone, the detector and the state machine.

Spec section 16 is the load-bearing requirement here -- no audio leaves the
machine while the assistant is waiting. This class never touches the network:
frames go from PortAudio into the detector and are dropped. Only an activation
produces an event, and in Phase 0 that event just moves the state machine and
increments a counter.

It also carries the echo guard (risk R-6). The microphone hears the speakers,
so without acoustic echo cancellation the assistant can hear itself say the
wake phrase and interrupt its own answer. Rather than close the microphone
while it speaks -- which would cost the barge-in the specification asks for in
section 6.2 -- the guard raises the bar during playback: a person speaking over
the assistant from a metre away is louder and clearer to the microphone than
the speaker output bleeding back into it. Every suppressed detection is
counted, so how often the assistant nearly heard itself is a number rather than
a guess.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from ..session.state import Event, SessionStateMachine, State
from .engine import AudioEngine
from .wakeword import Detection, WakeWordDetector

logger = logging.getLogger(__name__)


# Added to the detector threshold while the assistant is speaking. Chosen so a
# marginal detection -- the kind the assistant's own voice produces after a
# round trip through the room -- is discarded, while a deliberate interruption
# still gets through. Tune it with scripts/measure_wakeword.py.
DEFAULT_ECHO_GUARD_MARGIN = 0.15

# The microphone level meter spans this range. Below -60 dBFS is a silent room
# on any laptop microphone; 0 dBFS is clipping.
LEVEL_FLOOR_DB = -60.0
# The meter shows the loudest recent frame rather than the latest one, so a
# poll every half second does not miss a word spoken between two polls.
LEVEL_WINDOW_FRAMES = 7  # about 0.5 s


def frame_level(frame: np.ndarray) -> float:
    """Loudness of an int16 frame, 0 for silence and 1 for full scale.

    Mapped from dBFS rather than raw RMS because a voice at a few metres is a
    small fraction of full scale: on a linear bar it would barely move.
    """
    if frame.size == 0:
        return 0.0
    samples = frame.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms <= 0.0:
        return 0.0
    decibels = 20.0 * np.log10(rms)
    return float(min(max((decibels - LEVEL_FLOOR_DB) / -LEVEL_FLOOR_DB, 0.0), 1.0))


@dataclass
class ListenerStats:
    """Spec section 15 asks for activation counts and timings per session."""

    activations: int = 0
    frames_processed: int = 0
    interruptions: int = 0
    echo_suppressions: int = 0
    started_at: datetime | None = None
    last_activation_at: datetime | None = None
    detections: list[Detection] = field(default_factory=list)
    #: Recent microphone levels, 0..1, newest last. Only a number per frame is
    #: kept, never the audio (spec section 14).
    recent_levels: deque[float] = field(
        default_factory=lambda: deque(maxlen=LEVEL_WINDOW_FRAMES)
    )

    @property
    def seconds_listening(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now(UTC) - self.started_at).total_seconds()

    @property
    def input_level(self) -> float:
        return max(self.recent_levels, default=0.0)


class WakeWordListener:
    """Runs passive listening for the length of a class."""

    def __init__(
        self,
        engine: AudioEngine,
        detector: WakeWordDetector,
        machine: SessionStateMachine,
        on_detection: Callable[[Detection], None] | None = None,
        echo_guard_margin: float = DEFAULT_ECHO_GUARD_MARGIN,
    ) -> None:
        self._engine = engine
        self._detector = detector
        self._machine = machine
        self._on_detection = on_detection
        self._echo_guard_margin = max(0.0, echo_guard_margin)
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
        self.stats.recent_levels.append(frame_level(frame))
        detection = self._detector.process(frame)
        if detection is None:
            return
        if self._suppressed_as_echo(detection):
            return
        self._handle_detection(detection)

    def _suppressed_as_echo(self, detection: Detection) -> bool:
        """Whether this detection is probably the assistant hearing itself."""
        if self._echo_guard_margin <= 0 or not self._engine.is_playing:
            return False

        threshold = getattr(self._detector, "threshold", 0.0)
        if detection.score >= threshold + self._echo_guard_margin:
            # Loud and clear enough to be somebody in the room, not the echo.
            return False

        self.stats.echo_suppressions += 1
        logger.debug(
            "Activación descartada como eco del propio asistente (%.2f).", detection.score
        )
        return True

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
