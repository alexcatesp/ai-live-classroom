"""Wake word detection for "Oye Chat" (D-04).

openWakeWord was chosen for its licence rather than its accuracy, so the open
risk R-1 is the false positive rate in a noisy classroom. Two things follow from
that and shape this module:

* the detector is reached only through `WakeWordDetector`, so swapping in a
  local STT approach later does not touch the audio engine;
* every score is kept in a small ring buffer and exposed, so the teacher can
  watch the meter during a real class and tune the sensitivity against evidence
  instead of guesswork (spec section 22).

Timing is counted in frames, not wall-clock seconds. A frame is a fixed 80 ms,
which makes the refractory window exactly reproducible in tests.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from .devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES

logger = logging.getLogger(__name__)

FRAME_SECONDS = FRAME_SAMPLES / CAPTURE_SAMPLE_RATE  # 0.08 s

# Sensitivity 0..1 maps onto the detector threshold. Deliberately never reaches
# 0 or 1: a threshold of 0 fires on silence and 1 never fires at all.
_MIN_THRESHOLD = 0.35
_MAX_THRESHOLD = 0.95


def threshold_for(sensitivity: float) -> float:
    """Higher sensitivity means a lower score is enough to fire."""
    sensitivity = min(max(sensitivity, 0.0), 1.0)
    return _MAX_THRESHOLD - (_MAX_THRESHOLD - _MIN_THRESHOLD) * sensitivity


class WakeWordUnavailable(RuntimeError):
    """Raised when no usable detector can be built."""


@dataclass(frozen=True)
class Detection:
    phrase: str
    score: float
    at: datetime


class WakeWordDetector(Protocol):
    """Consumes 80 ms frames and reports activations."""

    @property
    def phrase(self) -> str: ...

    def process(self, frame: np.ndarray) -> Detection | None:
        """Return a Detection when this frame completes the wake phrase."""

    def reset(self) -> None:
        """Forget accumulated audio, e.g. when the class is paused."""

    def recent_scores(self) -> list[float]:
        """Latest scores, newest last, for the sensitivity meter."""


class _ScoreTracker:
    """Shared bookkeeping: threshold, refractory window and score history."""

    def __init__(self, phrase: str, sensitivity: float, refractory_seconds: float) -> None:
        self._phrase = phrase
        self._threshold = threshold_for(sensitivity)
        self._refractory_frames = math.ceil(max(refractory_seconds, 0.0) / FRAME_SECONDS)
        self._cooldown = 0
        self._scores: deque[float] = deque(maxlen=120)  # about 10 seconds
        self._lock = threading.Lock()

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def threshold(self) -> float:
        return self._threshold

    def observe(self, score: float) -> Detection | None:
        with self._lock:
            self._scores.append(score)
            if self._cooldown > 0:
                # Still inside the refractory window: the tail of an activation
                # that already fired must not fire a second time.
                self._cooldown -= 1
                return None
            if score < self._threshold:
                return None
            self._cooldown = self._refractory_frames
        return Detection(phrase=self._phrase, score=score, at=datetime.now(UTC))

    def reset(self) -> None:
        with self._lock:
            self._cooldown = 0
            self._scores.clear()

    def recent_scores(self) -> list[float]:
        with self._lock:
            return list(self._scores)


class OpenWakeWordDetector:
    """Wraps an openWakeWord model trained for the configured phrase."""

    def __init__(
        self,
        model_path: Path,
        phrase: str,
        sensitivity: float = 0.5,
        refractory_seconds: float = 2.0,
    ) -> None:
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise WakeWordUnavailable(
                "El motor openWakeWord no está disponible en esta instalación."
            ) from exc

        if not model_path.exists():
            raise WakeWordUnavailable(
                f"No se encontró el modelo de palabra clave en {model_path}. "
                "Genéralo con scripts/train_wakeword.py y colócalo en data/models."
            )
        try:
            self._model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
        except Exception as exc:  # noqa: BLE001 - the library raises broadly
            raise WakeWordUnavailable(f"No se pudo cargar el modelo de activación: {exc}") from exc

        # The key of the prediction dict is the model file stem.
        self._model_key = model_path.stem
        self._tracker = _ScoreTracker(phrase, sensitivity, refractory_seconds)

    @property
    def phrase(self) -> str:
        return self._tracker.phrase

    @property
    def threshold(self) -> float:
        return self._tracker.threshold

    def process(self, frame: np.ndarray) -> Detection | None:
        if frame.size != FRAME_SAMPLES:
            raise ValueError(
                f"openWakeWord espera bloques de {FRAME_SAMPLES} muestras, "
                f"se recibieron {frame.size}."
            )
        try:
            predictions = self._model.predict(frame)
        except Exception:  # noqa: BLE001 - never let inference kill the audio thread
            logger.exception("Fallo en la inferencia de la palabra clave.")
            return None
        score = float(predictions.get(self._model_key, max(predictions.values(), default=0.0)))
        return self._tracker.observe(score)

    def reset(self) -> None:
        self._tracker.reset()
        reset_buffers = getattr(self._model, "reset", None)
        if callable(reset_buffers):
            reset_buffers()

    def recent_scores(self) -> list[float]:
        return self._tracker.recent_scores()


class ScriptedWakeWordDetector:
    """Test double driven by a list of scores, one per frame.

    Scores run out to 0.0, so a test can feed as much silence as it likes after
    the scripted part without the detector firing again.
    """

    def __init__(
        self,
        scores: list[float] | None = None,
        phrase: str = "Oye Chat",
        sensitivity: float = 0.5,
        refractory_seconds: float = 2.0,
    ) -> None:
        self._scripted = list(scores or [])
        self._index = 0
        self._tracker = _ScoreTracker(phrase, sensitivity, refractory_seconds)
        self.frames_seen = 0

    @property
    def phrase(self) -> str:
        return self._tracker.phrase

    @property
    def threshold(self) -> float:
        return self._tracker.threshold

    def process(self, frame: np.ndarray) -> Detection | None:
        self.frames_seen += 1
        score = self._scripted[self._index] if self._index < len(self._scripted) else 0.0
        self._index += 1
        return self._tracker.observe(score)

    def reset(self) -> None:
        self._index = 0
        self.frames_seen = 0
        self._tracker.reset()

    def recent_scores(self) -> list[float]:
        return self._tracker.recent_scores()


def model_filename(phrase: str) -> str:
    """`Oye Chat` -> `oye_chat.onnx`, the name train_wakeword.py writes."""
    slug = "_".join(part for part in phrase.lower().split() if part)
    return f"{slug or 'wakeword'}.onnx"


def create_detector(
    models_dir: Path,
    phrase: str,
    sensitivity: float = 0.5,
    refractory_seconds: float = 2.0,
) -> WakeWordDetector:
    """Build the real detector for `phrase`, or explain why it is unavailable."""
    return OpenWakeWordDetector(
        model_path=models_dir / model_filename(phrase),
        phrase=phrase,
        sensitivity=sensitivity,
        refractory_seconds=refractory_seconds,
    )
