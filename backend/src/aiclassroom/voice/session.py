"""The recording and training session behind "Entrenar con mi voz" (D-12).

Privacy (spec section 14) shapes this class more than anything else:

* Takes live in memory only. They are never written to disk, so there is
  nothing to clean up after a crash and nothing a copied folder could carry.
* They are discarded as soon as training ends, whether it succeeded or not.
  Retraining means recording again; that was the teacher's choice.
* Nothing here touches the network.

A trained model waits in `personal/pending/` until the teacher accepts it, and
the model that ships is never overwritten: going back is deleting a file.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from ..audio.devices import CAPTURE_SAMPLE_RATE
from ..audio.engine import AudioEngine, AudioError
from ..audio.wakeword import (
    PERSONAL_SUBFOLDER,
    model_filename,
    personal_model_path,
    threshold_for,
)
from ..config.settings import Settings
from ..config.store import SettingsStore
from .personal import PersonalResult, train_personal, training_available
from .takes import SPEECH_TAKE_SECONDS, TAKE_SECONDS, Take, analyse, analyse_speech

logger = logging.getLogger(__name__)

PHRASE_TAKES = 5

# The false activations reported from the first classroom test, plus two more
# that share the phrase's sounds. Recorded by the teacher, they teach the
# detector precisely the confusions it was making.
NEAR_MISS_PROMPTS = ("Oye chico", "Oye Chechu", "Oye cat", "Oye, ¿qué tal?", "Chat")

Capture = Callable[[Settings, float], np.ndarray]


class Kind(StrEnum):
    PHRASE = "phrase"
    NEAR_MISS = "near_miss"
    #: Half a minute of ordinary talk, without the phrase.
    SPEECH = "speech"


TAKE_LENGTH = {
    Kind.PHRASE: TAKE_SECONDS,
    Kind.NEAR_MISS: TAKE_SECONDS,
    Kind.SPEECH: SPEECH_TAKE_SECONDS,
}


class TrainingState(StrEnum):
    IDLE = "idle"
    TRAINING = "training"
    #: A model is trained and waits for the teacher to accept or discard it.
    READY = "ready"
    FAILED = "failed"


class VoiceBusy(RuntimeError):
    """The microphone or the trainer is already in use."""


class VoiceNotReady(RuntimeError):
    """The requested step needs something that is not there yet."""


def capture_with_engine(engine_factory: Callable[[Settings], AudioEngine]) -> Capture:
    """Record `seconds` from the configured microphone through the audio engine."""

    def capture(settings: Settings, seconds: float) -> np.ndarray:
        engine = engine_factory(settings)
        wanted = int(seconds * CAPTURE_SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        collected = 0
        done = threading.Event()

        def on_frame(frame: np.ndarray) -> None:
            nonlocal collected
            if done.is_set():
                return
            chunks.append(frame.copy())
            collected += frame.size
            if collected >= wanted:
                done.set()

        engine.start_capture(on_frame)
        try:
            if not done.wait(timeout=seconds + 3.0):
                raise AudioError("El micrófono no ha entregado audio. Revisa el dispositivo.")
        finally:
            engine.stop_capture()
        return np.concatenate(chunks)[:wanted]

    return capture


@dataclass(frozen=True)
class TakeInfo:
    seconds: float
    level: float


class VoiceTrainingSession:
    def __init__(
        self,
        store: SettingsStore,
        capture: Capture,
        microphone_in_use: Callable[[], bool],
        trainer: Callable[..., PersonalResult] = train_personal,
    ) -> None:
        self.store = store
        self._capture = capture
        self._microphone_in_use = microphone_in_use
        self._trainer = trainer
        self._lock = threading.RLock()
        self._takes: dict[Kind, list[Take | None]] = {}
        self._clear_takes()
        self._recording = False
        self.state = TrainingState.IDLE
        self.progress = 0.0
        self.message = ""
        self.error: str | None = None
        self.result: PersonalResult | None = None
        self._thread: threading.Thread | None = None
        # A pending model from a run the application never got to finish is
        # not something the teacher ever saw, so it is not kept.
        shutil.rmtree(self._pending_dir, ignore_errors=True)

    # -- paths -------------------------------------------------------------

    @property
    def _models_dir(self) -> Path:
        return self.store.paths.models_dir

    @property
    def _pending_dir(self) -> Path:
        return self._models_dir / PERSONAL_SUBFOLDER / "pending"

    def _phrase(self) -> str:
        return self.store.load().wake_phrase

    def _pending_model(self) -> Path:
        return self._pending_dir / model_filename(self._phrase())

    # -- takes -------------------------------------------------------------

    def _clear_takes(self) -> None:
        self._takes = {
            Kind.PHRASE: [None] * PHRASE_TAKES,
            Kind.NEAR_MISS: [None] * len(NEAR_MISS_PROMPTS),
            Kind.SPEECH: [None],
        }

    def _slot(self, kind: Kind, slot: int) -> None:
        if not 0 <= slot < len(self._takes[kind]):
            raise VoiceNotReady(f"No existe la grabación {slot + 1}.")

    def record(self, kind: Kind, slot: int) -> TakeInfo:
        """Blocks for the length of a take. Call it off the event loop."""
        with self._lock:
            self._slot(kind, slot)
            if self.state is TrainingState.TRAINING:
                raise VoiceBusy("Espera a que termine el entrenamiento.")
            if self._recording:
                raise VoiceBusy("Ya se está grabando otra toma.")
            if self._microphone_in_use():
                raise VoiceBusy(
                    "El micrófono está escuchando la clase. Pausa o finaliza la clase para grabar."
                )
            self._recording = True
        try:
            recording = self._capture(self.store.load(), TAKE_LENGTH[kind])
            take = analyse_speech(recording) if kind is Kind.SPEECH else analyse(recording)
        finally:
            with self._lock:
                self._recording = False
        with self._lock:
            self._takes[kind][slot] = take
        return TakeInfo(seconds=take.seconds, level=take.peak_level)

    def forget(self, kind: Kind, slot: int) -> None:
        with self._lock:
            self._slot(kind, slot)
            self._takes[kind][slot] = None

    # -- training ----------------------------------------------------------

    def start_training(self) -> None:
        with self._lock:
            if self.state is TrainingState.TRAINING:
                raise VoiceBusy("Ya hay un entrenamiento en marcha.")
            missing = sum(take is None for takes in self._takes.values() for take in takes)
            if missing:
                raise VoiceNotReady(f"Faltan {missing} grabaciones por hacer.")
            reason = training_available(self._models_dir, self._phrase())
            if reason:
                raise VoiceNotReady(reason)

            takes = {
                kind: [take.samples for take in self._takes[kind] if take] for kind in Kind
            }
            settings = self.store.load()
            self.state = TrainingState.TRAINING
            self.progress, self.message, self.error, self.result = 0.0, "Empezando", None, None
            self._thread = threading.Thread(
                target=self._run,
                args=(settings, takes),
                name="voice-training",
                daemon=True,
            )
            self._thread.start()

    def _run(self, settings: Settings, takes: dict[Kind, list[np.ndarray]]) -> None:
        destination = self._pending_model()
        shutil.rmtree(self._pending_dir, ignore_errors=True)
        try:
            result = self._trainer(
                models_dir=self._models_dir,
                phrase=settings.wake_phrase,
                phrase_takes=takes[Kind.PHRASE],
                near_miss_takes=takes[Kind.NEAR_MISS],
                speech_takes=takes[Kind.SPEECH],
                destination=destination,
                threshold=threshold_for(settings.wake_sensitivity),
                progress=self._report,
            )
        except Exception as exc:  # noqa: BLE001 - whatever fails, the teacher is told
            logger.exception("El entrenamiento con la voz del profesor falló.")
            with self._lock:
                self.state = TrainingState.FAILED
                self.error = f"No se pudo entrenar: {exc}"
            shutil.rmtree(self._pending_dir, ignore_errors=True)
        else:
            with self._lock:
                self.state = TrainingState.READY
                self.result = result
                self.progress, self.message = 1.0, "Listo"
        finally:
            # The recordings go, success or failure (D-12). The local lists
            # held by this thread go with the frame.
            with self._lock:
                self._clear_takes()
            takes.clear()

    def _report(self, fraction: float, message: str) -> None:
        with self._lock:
            self.progress = max(0.0, min(fraction, 1.0))
            self.message = message

    def wait(self, timeout: float | None = None) -> None:
        """For tests: block until a training run has finished."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- decisions ---------------------------------------------------------

    def accept(self) -> None:
        with self._lock:
            if self.state is not TrainingState.READY:
                raise VoiceNotReady("No hay ningún modelo nuevo que usar.")
            pending = self._pending_model()
            target = personal_model_path(self._models_dir, self._phrase())
            target.parent.mkdir(parents=True, exist_ok=True)
            pending.replace(target)
            shutil.rmtree(self._pending_dir, ignore_errors=True)
            self._reset_run()

    def discard(self) -> None:
        with self._lock:
            if self.state is TrainingState.TRAINING:
                raise VoiceBusy("Espera a que termine el entrenamiento.")
            shutil.rmtree(self._pending_dir, ignore_errors=True)
            self._reset_run()

    def restore_original(self) -> None:
        with self._lock:
            personal_model_path(self._models_dir, self._phrase()).unlink(missing_ok=True)

    def _reset_run(self) -> None:
        self.state = TrainingState.IDLE
        self.progress, self.message, self.error, self.result = 0.0, "", None, None

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            phrase = self._phrase()

            def describe(takes: list[Take | None]) -> list[dict | None]:
                return [
                    None if take is None
                    else {"seconds": round(take.seconds, 2), "level": round(take.peak_level, 2)}
                    for take in takes
                ]

            return {
                "phrase": phrase,
                "near_miss_prompts": list(NEAR_MISS_PROMPTS),
                "phrase_takes": describe(self._takes[Kind.PHRASE]),
                "near_miss_takes": describe(self._takes[Kind.NEAR_MISS]),
                "speech_take": describe(self._takes[Kind.SPEECH])[0],
                "take_seconds": {kind.value: seconds for kind, seconds in TAKE_LENGTH.items()},
                "recording": self._recording,
                "state": self.state.value,
                "progress": self.progress,
                "message": self.message,
                "error": self.error,
                "result": asdict(self.result) if self.result else None,
                "personal_model": personal_model_path(self._models_dir, phrase).exists(),
                "unavailable_reason": training_available(self._models_dir, phrase),
            }
