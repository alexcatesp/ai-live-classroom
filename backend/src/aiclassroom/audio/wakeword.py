"""Wake word detection for "Oye Chat" (D-04).

openWakeWord was chosen for its licence rather than its accuracy, so risk R-1 --
the false positive rate in a noisy classroom -- is what this module is built
against. Three defences stack, cheapest first:

1. **Voice activity gating.** Silero's VAD runs alongside the detector and
   zeroes any score that does not coincide with speech. Chairs scraping, doors,
   a projector fan: none of them can activate the assistant, whatever the
   detector thinks it heard.
2. **Confirmation frames.** A single frame above the threshold is a spike, not
   a wake phrase. The phrase spans several frames, so N consecutive frames must
   agree before the assistant wakes.
3. **The refractory window**, which stops one activation counting twice.

Everything above is measurable offline with `scripts/measure_wakeword.py`, and
visible live in the detector meter, so the settings can be tuned against
recordings from the actual classroom rather than guessed (spec section 22).

The detector is reached only through `WakeWordDetector`, so swapping in a local
STT approach later does not touch the audio engine.

Timing is counted in frames, not wall-clock seconds. A frame is a fixed 80 ms,
which makes the refractory and confirmation windows exactly reproducible.
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

# Consecutive frames that must clear the threshold before the assistant wakes.
# Two frames is 160 ms, comfortably shorter than "Oye Chat" and long enough to
# reject the single-frame spikes that noise produces.
DEFAULT_CONFIRMATION_FRAMES = 2

# Silero VAD score below which a detection is discarded. Speech in a classroom
# scores well above this; a chair or a door does not score at all.
DEFAULT_VAD_THRESHOLD = 0.5


def threshold_for(sensitivity: float) -> float:
    """Higher sensitivity means a lower score is enough to fire."""
    sensitivity = min(max(sensitivity, 0.0), 1.0)
    return _MAX_THRESHOLD - (_MAX_THRESHOLD - _MIN_THRESHOLD) * sensitivity


# openWakeWord needs two shared models besides the phrase model: a
# melspectrogram front end and a speech embedding model. It downloads them on
# first use, which on a classroom PC would mean a download seconds before a
# lesson on a network that may block it. The build ships them instead, in
# data/models/openwakeword/ (scripts/fetch_wakeword_runtime.py).
BASE_MODELS_SUBFOLDER = "openwakeword"
MELSPEC_MODEL = "melspectrogram.onnx"
EMBEDDING_MODEL = "embedding_model.onnx"
VAD_MODEL = "silero_vad.onnx"
BASE_MODELS = (MELSPEC_MODEL, EMBEDDING_MODEL, VAD_MODEL)


class WakeWordUnavailable(RuntimeError):
    """Raised when no usable detector can be built."""


def base_model_paths(models_dir: Path) -> tuple[Path, ...]:
    """Where the shipped feature extractor and the VAD live."""
    base = models_dir / BASE_MODELS_SUBFOLDER
    return tuple(base / name for name in BASE_MODELS)


def missing_base_models(models_dir: Path) -> list[Path]:
    return [path for path in base_model_paths(models_dir) if not path.exists()]


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
    """Shared bookkeeping: threshold, confirmation, refractory and history."""

    def __init__(
        self,
        phrase: str,
        sensitivity: float,
        refractory_seconds: float,
        confirmation_frames: int = DEFAULT_CONFIRMATION_FRAMES,
    ) -> None:
        self._phrase = phrase
        self._threshold = threshold_for(sensitivity)
        self._refractory_frames = math.ceil(max(refractory_seconds, 0.0) / FRAME_SECONDS)
        self._confirmation_frames = max(1, confirmation_frames)
        self._cooldown = 0
        self._consecutive = 0
        self._peak = 0.0
        self._scores: deque[float] = deque(maxlen=120)  # about 10 seconds
        self._lock = threading.Lock()

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def confirmation_frames(self) -> int:
        return self._confirmation_frames

    def observe(self, score: float) -> Detection | None:
        with self._lock:
            self._scores.append(score)

            if self._cooldown > 0:
                # Still inside the refractory window: the tail of an activation
                # that already fired must not fire a second time.
                self._cooldown -= 1
                self._consecutive = 0
                return None

            if score < self._threshold:
                # The run is broken, so a later spike starts counting afresh.
                self._consecutive = 0
                self._peak = 0.0
                return None

            self._consecutive += 1
            self._peak = max(self._peak, score)
            if self._consecutive < self._confirmation_frames:
                # One frame above the threshold is a spike; the wake phrase
                # lasts longer than 80 ms (defence 2 against R-1).
                return None

            peak = self._peak
            self._cooldown = self._refractory_frames
            self._consecutive = 0
            self._peak = 0.0

        return Detection(phrase=self._phrase, score=peak, at=datetime.now(UTC))

    def reset(self) -> None:
        with self._lock:
            self._cooldown = 0
            self._consecutive = 0
            self._peak = 0.0
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
        melspec_model: Path | None = None,
        embedding_model: Path | None = None,
        vad_model: Path | None = None,
        vad_threshold: float = DEFAULT_VAD_THRESHOLD,
        confirmation_frames: int = DEFAULT_CONFIRMATION_FRAMES,
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

        extra: dict[str, str] = {}
        if melspec_model is not None and embedding_model is not None:
            # Passing these explicitly is what stops openWakeWord reaching for
            # the network mid-class.
            extra = {
                "melspec_model_path": str(melspec_model),
                "embedding_model_path": str(embedding_model),
            }

        try:
            # Built without VAD here on purpose: openWakeWord would look for
            # silero_vad.onnx at a path fixed inside its own package, which the
            # portable folder does not have. The VAD is attached below from the
            # copy that ships in data/models instead.
            self._model = Model(
                wakeword_models=[str(model_path)], inference_framework="onnx", **extra
            )
        except Exception as exc:  # noqa: BLE001 - the library raises broadly
            raise WakeWordUnavailable(f"No se pudo cargar el modelo de activación: {exc}") from exc

        self._attach_vad(vad_model, vad_threshold)

        # The key of the prediction dict is the model file stem.
        self._model_key = model_path.stem
        self._tracker = _ScoreTracker(
            phrase, sensitivity, refractory_seconds, confirmation_frames
        )

    def _attach_vad(self, vad_model: Path | None, vad_threshold: float) -> None:
        """Gate detections on speech being present (defence 1 against R-1).

        openWakeWord checks `vad_threshold` on every prediction and calls
        `self.vad`, so supplying both is enough to turn the gate on with a model
        loaded from wherever we put it.
        """
        self.vad_enabled = False
        if vad_model is None or vad_threshold <= 0:
            return
        if not vad_model.exists():
            logger.warning(
                "No se encontró el modelo de VAD en %s: el detector funcionará sin "
                "filtro de voz y habrá más falsos positivos.",
                vad_model,
            )
            return
        try:
            from openwakeword.vad import VAD

            self._model.vad = VAD(model_path=str(vad_model))
            self._model.vad_threshold = vad_threshold
            self.vad_enabled = True
        except Exception:  # noqa: BLE001 - never let the gate break the detector
            logger.exception("No se pudo activar el filtro de voz (VAD).")

    @property
    def phrase(self) -> str:
        return self._tracker.phrase

    @property
    def threshold(self) -> float:
        return self._tracker.threshold

    @property
    def confirmation_frames(self) -> int:
        return self._tracker.confirmation_frames

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
        confirmation_frames: int = 1,
    ) -> None:
        self._scripted = list(scores or [])
        self._index = 0
        # Defaults to 1 so a test that only cares about thresholds does not have
        # to pad every script; the confirmation logic has its own tests.
        self._tracker = _ScoreTracker(
            phrase, sensitivity, refractory_seconds, confirmation_frames
        )
        self.frames_seen = 0
        self.vad_enabled = False

    @property
    def phrase(self) -> str:
        return self._tracker.phrase

    @property
    def threshold(self) -> float:
        return self._tracker.threshold

    @property
    def confirmation_frames(self) -> int:
        return self._tracker.confirmation_frames

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
    vad_threshold: float = DEFAULT_VAD_THRESHOLD,
    confirmation_frames: int = DEFAULT_CONFIRMATION_FRAMES,
) -> WakeWordDetector:
    """Build the real detector for `phrase`, or explain why it is unavailable."""
    missing = missing_base_models(models_dir)
    if missing:
        names = ", ".join(path.name for path in missing)
        raise WakeWordUnavailable(
            f"Faltan los modelos base de openWakeWord ({names}) en "
            f"{models_dir / BASE_MODELS_SUBFOLDER}. "
            "Descárgalos con scripts/fetch_wakeword_runtime.py desde un equipo con conexión."
        )

    melspec, embedding, vad = base_model_paths(models_dir)
    return OpenWakeWordDetector(
        model_path=models_dir / model_filename(phrase),
        phrase=phrase,
        sensitivity=sensitivity,
        refractory_seconds=refractory_seconds,
        melspec_model=melspec,
        embedding_model=embedding,
        vad_model=vad,
        vad_threshold=vad_threshold,
        confirmation_frames=confirmation_frames,
    )
