"""Fine-tuning the shipped detector with the teacher's recordings (D-12).

The first version retrained the classifier from scratch on the synthetic corpus
plus the teacher's takes. In a real classroom it woke up more often than the
model it replaced. Two causes, both addressed here:

* **Starting from scratch threw away what the original did well.** Now the
  shipped model's weights are loaded and trained further, gently, with the
  synthetic corpus replayed alongside so it does not forget.
* **Every real-microphone example was short.** Five phrases and five near
  misses, and not a second of the teacher simply talking, so "this voice
  through this microphone" could itself start to look like the phrase. Half a
  minute of ordinary speech is now recorded and used as negatives.

And a third change, the one that actually protects the classroom: the tuned
model is **compared with the original on audio neither was trained on** --
the last part of that speech and a held-out slice of the synthetic corpus --
and is only recommended when it is not worse.

Neither comparison is perfectly fair, and each leans the safe way where it
matters. The held-out synthetic slice was part of what the original trained
on at build time, which favours the original; only the held-out speech is
unseen by both, and it is the measure a regression is judged on first.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..audio.wakeword import model_filename
from .training import (
    SAMPLE_RATE,
    WINDOW_SECONDS,
    build_features,
    corpus_filename,
    count_activations,
    export_onnx,
    fine_tune,
    load_classifier,
    load_corpus,
    place,
    positive_windows,
    score_windows,
    stack,
    verify,
    windows_of,
)
from .training import augment as augment_clip

# Copies of each short take. Fewer than a from-scratch run would need: the
# network already knows the phrase, it only has to adjust to this voice.
AUGMENTATIONS = 24
# Copies of the training part of the ordinary-speech take.
SPEECH_AUGMENTATIONS = 6
# The end of the speech take is kept aside to compare the two models on.
SPEECH_HELD_OUT_SHARE = 0.35
# Share of the synthetic corpus kept aside for the same comparison.
BASE_HELD_OUT_SHARE = 0.15
# Synthetic windows replayed per personal window, so the task is not forgotten.
REPLAY_RATIO = 2.0

EPOCHS = 12
LEARNING_RATE = 3e-4

# How much worse on the synthetic held-out set the tuned model may be and still
# be recommended. A tenth of a percentage point: noise, not a regression.
BASE_FALSE_RATE_TOLERANCE = 0.001

Progress = Callable[[float, str], None]


class PersonalTrainingUnavailable(RuntimeError):
    """Something training needs is missing from this installation."""


@dataclass(frozen=True)
class ModelScore:
    """One model, measured on the same audio as the other."""

    #: Of the teacher's phrase takes, how many it fires on.
    phrase_detected: int
    #: Of the teacher's near misses, how many it fires on.
    near_misses_triggered: int
    #: Times it would have woken during held-out ordinary speech.
    speech_activations: int
    #: On the held-out slice of the synthetic corpus.
    base_detection: float
    base_false_rate: float


@dataclass(frozen=True)
class PersonalResult:
    original: ModelScore
    tuned: ModelScore
    phrase_total: int
    near_misses_total: int
    speech_held_out_seconds: float
    recommended: bool
    #: Why it is or is not recommended, in plain Spanish.
    verdict: str
    seconds: float


def corpus_path(models_dir: Path, phrase: str) -> Path:
    return models_dir / corpus_filename(model_filename(phrase))


def original_model_path(models_dir: Path, phrase: str) -> Path:
    """Always the shipped model: tuning a tuned model would compound drift."""
    return models_dir / model_filename(phrase)


def training_available(models_dir: Path, phrase: str) -> str | None:
    """None when training can run here, otherwise the reason it cannot."""
    if not original_model_path(models_dir, phrase).exists():
        return "Falta el modelo original del detector en data/models."
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


def judge(original: ModelScore, tuned: ModelScore, phrase_total: int) -> tuple[bool, str]:
    """Recommend the tuned model only when it is not worse where it matters."""
    problems = []
    if tuned.speech_activations > original.speech_activations:
        problems.append(
            f"se activa más con tu habla normal ({tuned.speech_activations} frente a "
            f"{original.speech_activations})"
        )
    if tuned.base_false_rate > original.base_false_rate + BASE_FALSE_RATE_TOLERANCE:
        problems.append("se equivoca más con otras voces y frases")
    if tuned.phrase_detected < original.phrase_detected:
        problems.append("reconoce menos veces tu «Oye Chat»")
    if problems:
        return False, "No se recomienda: " + "; ".join(problems) + "."

    gains = []
    if tuned.phrase_detected > original.phrase_detected:
        gains.append(
            f"reconoce tu frase {tuned.phrase_detected} de {phrase_total} veces "
            f"(el original, {original.phrase_detected})"
        )
    if tuned.near_misses_triggered < original.near_misses_triggered:
        gains.append("se confunde menos con las frases parecidas")
    if tuned.speech_activations < original.speech_activations:
        gains.append("se activa menos con tu habla normal")
    if not gains:
        return False, "No mejora al original en nada que se pueda medir: no merece la pena."
    return True, "Recomendado: " + "; ".join(gains) + "."


def train_personal(
    models_dir: Path,
    phrase: str,
    phrase_takes: Sequence[np.ndarray],
    near_miss_takes: Sequence[np.ndarray],
    speech_takes: Sequence[np.ndarray],
    destination: Path,
    threshold: float,
    progress: Progress = lambda _fraction, _message: None,
    seed: int = 20260913,
) -> PersonalResult:
    started = time.monotonic()
    reason = training_available(models_dir, phrase)
    if reason:
        raise PersonalTrainingUnavailable(reason)

    rng = np.random.default_rng(seed)
    progress(0.02, "Cargando el modelo original")
    original = original_model_path(models_dir, phrase)
    classifier = load_classifier(original, seed=seed)
    base_positives, base_negatives = load_corpus(corpus_path(models_dir, phrase))
    features = build_features(models_dir)

    # -- held-out data, set aside before anything is trained ---------------

    def split(windows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        order = rng.permutation(windows.shape[0])
        cut = int(windows.shape[0] * BASE_HELD_OUT_SHARE)
        return windows[order[cut:]], windows[order[:cut]]

    replay_positives, held_positives = split(base_positives)
    replay_negatives, held_negatives = split(base_negatives)

    speech_train: list[np.ndarray] = []
    speech_held: list[np.ndarray] = []
    for take in speech_takes:
        cut = int(take.size * (1 - SPEECH_HELD_OUT_SHARE))
        speech_train.append(take[:cut])
        speech_held.append(take[cut:])

    # -- the teacher's examples --------------------------------------------

    total_takes = max(1, len(phrase_takes) + len(near_miss_takes) + len(speech_train))
    done = 0

    def prepared() -> None:
        nonlocal done
        done += 1
        progress(0.05 + 0.3 * done / total_takes, "Preparando tus grabaciones")

    personal_positives: list[np.ndarray] = []
    for take in phrase_takes:
        for attempt in range(AUGMENTATIONS):
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
        for attempt in range(AUGMENTATIONS):
            body = augment_clip(take, rng, clean=attempt == 0)
            ends_at = rng.uniform(WINDOW_SECONDS + 0.05, WINDOW_SECONDS + 0.9)
            lead = max(0.1, ends_at - body.size / SAMPLE_RATE)
            clip, utterance_end = place(body, lead, 0.6, rng)
            # Negative both where the phrase would be labelled and everywhere
            # else: a near miss must not fire wherever it falls.
            personal_negatives.append(positive_windows(features, clip, utterance_end))
            personal_negatives.append(windows_of(features, clip))
        prepared()

    for take in speech_train:
        for attempt in range(SPEECH_AUGMENTATIONS):
            personal_negatives.append(
                windows_of(features, augment_clip(take, rng, clean=attempt == 0))
            )
        prepared()

    positives = stack(personal_positives)
    negatives = stack(personal_negatives)

    # Replay: the old task, in proportion, so fine-tuning adjusts rather than
    # replaces. Negatives stay the majority, as in every training run.
    personal_count = positives.shape[0] + negatives.shape[0]
    replay_total = int(personal_count * REPLAY_RATIO)
    replay_pos_count = min(replay_positives.shape[0], replay_total // 3)
    replay_neg_count = min(replay_negatives.shape[0], replay_total - replay_pos_count)
    replay_pos = replay_positives[
        rng.choice(replay_positives.shape[0], replay_pos_count, replace=False)
    ]
    replay_neg = replay_negatives[
        rng.choice(replay_negatives.shape[0], replay_neg_count, replace=False)
    ]

    all_positives = np.concatenate([positives, replay_pos])
    all_negatives = np.concatenate([negatives, replay_neg])
    matrix = np.concatenate([all_positives, all_negatives])
    labels = np.concatenate(
        [
            np.ones(all_positives.shape[0], dtype=np.int64),
            np.zeros(all_negatives.shape[0], dtype=np.int64),
        ]
    )

    # -- fine-tuning -------------------------------------------------------

    def on_epoch(epoch: int) -> None:
        progress(0.35 + 0.45 * epoch / EPOCHS, f"Ajustando el modelo ({epoch} de {EPOCHS})")

    fine_tune(classifier, matrix, labels, EPOCHS, LEARNING_RATE, seed, on_epoch)

    export_onnx(classifier, destination)
    verify(destination)

    # -- the comparison ----------------------------------------------------

    progress(0.82, "Comparando con el modelo original")
    phrase_windows = []
    for take in phrase_takes:
        clip, _ = place(take, WINDOW_SECONDS + 0.1, 0.5, rng)
        phrase_windows.append(windows_of(features, clip))
    near_windows = []
    for take in near_miss_takes:
        clip, _ = place(take, WINDOW_SECONDS + 0.1, 0.5, rng)
        near_windows.append(windows_of(features, clip))
    speech_windows = [windows_of(features, take) for take in speech_held]

    def measure(model: Path) -> ModelScore:
        def fires(windows: np.ndarray) -> bool:
            scores = score_windows(model, windows)
            return count_activations(scores, threshold) > 0

        held_pos = score_windows(model, held_positives)
        held_neg = score_windows(model, held_negatives)
        return ModelScore(
            phrase_detected=sum(fires(windows) for windows in phrase_windows),
            near_misses_triggered=sum(fires(windows) for windows in near_windows),
            speech_activations=sum(
                count_activations(score_windows(model, windows), threshold)
                for windows in speech_windows
            ),
            base_detection=float((held_pos >= threshold).mean()) if held_pos.size else 0.0,
            base_false_rate=float((held_neg >= threshold).mean()) if held_neg.size else 0.0,
        )

    original_score = measure(original)
    progress(0.92, "Comparando con el modelo original")
    tuned_score = measure(destination)
    recommended, verdict = judge(original_score, tuned_score, len(phrase_takes))
    progress(1.0, "Listo")

    return PersonalResult(
        original=original_score,
        tuned=tuned_score,
        phrase_total=len(phrase_takes),
        near_misses_total=len(near_miss_takes),
        speech_held_out_seconds=sum(take.size for take in speech_held) / SAMPLE_RATE,
        recommended=recommended,
        verdict=verdict,
        seconds=time.monotonic() - started,
    )
