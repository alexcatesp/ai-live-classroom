"""Retraining the detector with the teacher's recordings on top of the base corpus.

The synthetic corpus saved at build time carries the breadth: a hundred voices,
lesson sentences, near misses and noise. The teacher's takes carry what that
corpus cannot, which is how this person, in this room, through this microphone
says the phrase -- and how they say the things it gets confused with.

Each take is multiplied by augmentation, as the build does with synthetic
voices, and half of the phrase takes are preceded by one of the teacher's own
near misses, so the phrase is also learnt mid-sentence.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..audio.wakeword import model_filename
from .training import (
    NEGATIVE_RATIO,
    SAMPLE_RATE,
    WINDOW_SECONDS,
    build_features,
    corpus_filename,
    export_onnx,
    load_corpus,
    place,
    positive_windows,
    score_windows,
    stack,
    train,
    verify,
    windows_of,
)
from .training import augment as augment_clip

# Copies of each take. Forty is enough for five takes to weigh against a few
# thousand synthetic windows without drowning them.
AUGMENTATIONS = 40

Progress = Callable[[float, str], None]


class PersonalTrainingUnavailable(RuntimeError):
    """Something training needs is missing from this installation."""


@dataclass(frozen=True)
class PersonalResult:
    #: Of the teacher's own phrase takes, how many the new model fires on.
    phrase_detected: int
    phrase_total: int
    #: Of the teacher's near misses, how many it still fires on.
    near_misses_triggered: int
    near_misses_total: int
    #: On windows held out of training, synthetic and personal together.
    held_out_detection: float
    held_out_false_rate: float
    seconds: float


def corpus_path(models_dir: Path, phrase: str) -> Path:
    return models_dir / corpus_filename(model_filename(phrase))


def training_available(models_dir: Path, phrase: str) -> str | None:
    """None when training can run here, otherwise the reason it cannot."""
    if not corpus_path(models_dir, phrase).exists():
        return (
            "Falta el corpus base del detector en data/models. Descarga una versión "
            "más reciente de la aplicación."
        )
    try:
        import onnx  # noqa: F401
        import sklearn.neural_network  # noqa: F401
    except ImportError:
        return "Esta instalación no incluye el entrenamiento del detector."
    return None


def train_personal(
    models_dir: Path,
    phrase: str,
    phrase_takes: Sequence[np.ndarray],
    near_miss_takes: Sequence[np.ndarray],
    destination: Path,
    threshold: float,
    progress: Progress = lambda _fraction, _message: None,
    augmentations: int = AUGMENTATIONS,
    seed: int = 20260913,
) -> PersonalResult:
    started = time.monotonic()
    reason = training_available(models_dir, phrase)
    if reason:
        raise PersonalTrainingUnavailable(reason)

    progress(0.02, "Cargando los ejemplos de base")
    base_positives, base_negatives = load_corpus(corpus_path(models_dir, phrase))
    features = build_features(models_dir)
    rng = np.random.default_rng(seed)

    total_takes = max(1, len(phrase_takes) + len(near_miss_takes))
    done = 0

    def prepared() -> None:
        nonlocal done
        done += 1
        progress(0.05 + 0.3 * done / total_takes, "Preparando tus grabaciones")

    personal_positives: list[np.ndarray] = []
    for take in phrase_takes:
        for attempt in range(augmentations):
            body = augment_clip(take, rng, clean=attempt == 0)
            if attempt % 2 == 1 and near_miss_takes:
                # Someone talking right up to the phrase, in the teacher's own
                # voice, so it is recognised mid-sentence too.
                lead_in = near_miss_takes[int(rng.integers(0, len(near_miss_takes)))]
                gap = np.zeros(int(rng.uniform(0.05, 0.3) * SAMPLE_RATE), dtype=np.float32)
                body = np.concatenate([augment_clip(lead_in, rng), gap, body])
            ends_at = rng.uniform(WINDOW_SECONDS + 0.05, WINDOW_SECONDS + 0.9)
            lead = max(0.1, ends_at - body.size / SAMPLE_RATE)
            clip, phrase_end = place(body, lead, 0.6, rng)
            personal_positives.append(positive_windows(features, clip, phrase_end))
        prepared()

    personal_negatives: list[np.ndarray] = []
    for take in near_miss_takes:
        for attempt in range(augmentations):
            body = augment_clip(take, rng, clean=attempt == 0)
            ends_at = rng.uniform(WINDOW_SECONDS + 0.05, WINDOW_SECONDS + 0.9)
            lead = max(0.1, ends_at - body.size / SAMPLE_RATE)
            clip, utterance_end = place(body, lead, 0.6, rng)
            # Negative both where the phrase would be labelled and everywhere
            # else: a near miss must not fire wherever it falls.
            personal_negatives.append(positive_windows(features, clip, utterance_end))
            personal_negatives.append(windows_of(features, clip))
        prepared()

    positives = np.concatenate([base_positives, stack(personal_positives)])
    negatives = np.concatenate([base_negatives, stack(personal_negatives)])

    # Negatives must stay the majority, but the teacher's examples are the
    # point of the exercise, so it is the synthetic positives that give way.
    wanted_positives = int(negatives.shape[0] / NEGATIVE_RATIO)
    if positives.shape[0] > wanted_positives:
        surplus = positives.shape[0] - wanted_positives
        kept = max(0, base_positives.shape[0] - surplus)
        keep = rng.choice(base_positives.shape[0], kept, replace=False)
        positives = np.concatenate([base_positives[keep], stack(personal_positives)])

    matrix = np.concatenate([positives, negatives])
    labels = np.concatenate(
        [
            np.ones(positives.shape[0], dtype=np.int64),
            np.zeros(negatives.shape[0], dtype=np.int64),
        ]
    )

    def on_round(number: int) -> None:
        progress(0.35 + 0.25 * (number - 1), f"Entrenando (ronda {number} de 2)")

    classifier, held_out = train(
        matrix, labels, seed=seed, log=lambda _line: None, on_round=on_round,
        threshold=threshold,
    )

    progress(0.88, "Comprobando el modelo nuevo")
    export_onnx(classifier, destination)
    verify(destination)

    def fires(take: np.ndarray) -> bool:
        clip, _ = place(take, WINDOW_SECONDS + 0.1, 0.5, rng)
        scores = score_windows(destination, windows_of(features, clip))
        return bool(scores.size and scores.max() >= threshold)

    phrase_detected = sum(fires(take) for take in phrase_takes)
    near_misses_triggered = sum(fires(take) for take in near_miss_takes)
    progress(1.0, "Listo")

    return PersonalResult(
        phrase_detected=phrase_detected,
        phrase_total=len(phrase_takes),
        near_misses_triggered=near_misses_triggered,
        near_misses_total=len(near_miss_takes),
        held_out_detection=held_out.detection,
        held_out_false_rate=held_out.false_rate,
        seconds=time.monotonic() - started,
    )
