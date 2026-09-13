"""Training the classifier that sits on top of openWakeWord's features (D-04).

openWakeWord splits a detector into three pieces: a melspectrogram front end, a
frozen speech embedding model, and a small classifier trained per phrase. Only
the third is trained here, in two places that share this module:

* `scripts/train_wakeword.py`, on the build machine, from synthetic voices. It
  also saves the corpus it built, as embedding windows, next to the model.
* The application itself, when a teacher trains with their own voice (D-12).
  The synthetic corpus comes from that saved file -- the classroom PC has no
  speech synthesiser -- and the teacher's recordings are added on top.

Everything here works on embedding windows, shaped (n, 16, 96): 16 frames of 96
values, which is exactly what the detector feeds the classifier.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..audio.wakeword import BASE_MODELS_SUBFOLDER, EMBEDDING_MODEL, MELSPEC_MODEL

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# The detector consumes a window of 16 embedding frames of 96 values each; the
# model written here has to accept exactly that (see the loaded model's input
# shape in openwakeword.model.Model).
WINDOW_FRAMES = 16
EMBEDDING_DIMS = 96
EMBEDDING_HOP_SECONDS = 0.08  # one embedding frame per 80 ms of audio

# Each embedding frame is computed from 76 melspectrogram frames, so frame k
# summarises the audio from 0.08*k up to 0.08*k + 0.76 seconds. Forgetting this
# lag puts the labelled window most of a second past the phrase, which teaches
# the model almost nothing.
EMBEDDING_CONTEXT_SECONDS = 0.76

# A 16-frame window therefore covers 0.08*15 + 0.76 seconds of audio. The
# detector always looks at that much history, so a training clip has to place
# the phrase at least this far in: a phrase ending sooner cannot sit at the end
# of any window, and the clip yields no positive example at all.
WINDOW_SECONDS = EMBEDDING_HOP_SECONDS * (WINDOW_FRAMES - 1) + EMBEDDING_CONTEXT_SECONDS

# How many negative examples to keep per positive. Above one, the model is
# pushed towards silence, which is the safer direction for a classroom.
NEGATIVE_RATIO = 2.0

# A negative the model scores above this is one it nearly fired on, and the
# only kind worth emphasising in a second round.
HARD_NEGATIVE_SCORE = 0.1

# The embedding model consumes 76 melspectrogram frames at a time, so a clip
# shorter than roughly a second cannot produce a single embedding. Short words
# like "oye" are exactly that, and they are among the most important negatives,
# so they are padded with room tone rather than dropped.
MIN_CLIP_SECONDS = 2.0


def corpus_filename(model_name: str) -> str:
    """`oye_chat.onnx` -> `oye_chat.corpus.npz`, saved beside the model."""
    return f"{Path(model_name).stem}.corpus.npz"


Log = Callable[[str], None]


def _print(message: str) -> None:
    print(message)


# -- augmentation ----------------------------------------------------------


def augment(samples: np.ndarray, rng: np.random.Generator, clean: bool = False) -> np.ndarray:
    """Make one clip sound like it was recorded in a room, badly.

    With `clean` the clip is passed through untouched. Every training example
    being augmented sounds thorough and is a trap: clean audio then falls
    outside everything the model has seen, and its behaviour there is
    arbitrary. The first model trained this way scored 10% of ordinary lesson
    windows at 1.0 -- 869 false activations an hour -- while reporting 0.09% on
    its own held-out set, which was augmented like the rest.
    """
    if clean:
        return np.clip(samples.astype(np.float32), -1.0, 1.0)

    out = samples.astype(np.float32)

    # A single delayed reflection: the cheapest thing that is still reverb.
    if rng.random() < 0.6:
        delay = int(rng.uniform(0.01, 0.06) * SAMPLE_RATE)
        decay = rng.uniform(0.15, 0.45)
        echoed = np.zeros(out.size + delay, dtype=np.float32)
        echoed[: out.size] += out
        echoed[delay:] += out * decay
        out = echoed

    if rng.random() < 0.8:
        noise_level = rng.uniform(0.002, 0.03)
        out = out + rng.normal(0, noise_level, out.size).astype(np.float32)

    out = out * rng.uniform(0.3, 1.2)

    # Clipping is what a microphone does when somebody leans into it.
    return np.clip(out, -1.0, 1.0)


def place(
    samples: np.ndarray, lead_seconds: float, tail_seconds: float, rng: np.random.Generator
) -> tuple[np.ndarray, float]:
    """Put `samples` at exactly `lead_seconds` into a clip of room tone.

    Returns the clip and the time the speech ends, because the caller needs to
    label a window by that instant and cannot know it otherwise. An earlier
    version placed the audio at a random offset and then labelled as if it were
    at the requested one: the positive windows pointed at the wrong moments,
    the model learnt from noise, and it fired on any speech at all.
    """
    lead = int(lead_seconds * SAMPLE_RATE)
    tail = int(tail_seconds * SAMPLE_RATE)
    canvas = rng.normal(0, 0.002, lead + samples.size + tail).astype(np.float32)
    canvas[lead : lead + samples.size] += samples
    return np.clip(canvas, -1.0, 1.0), (lead + samples.size) / SAMPLE_RATE


# -- features --------------------------------------------------------------


def build_features(models_dir: Path):
    """openWakeWord's front end, loaded from the models shipped in data/models."""
    from openwakeword.utils import AudioFeatures

    base = models_dir / BASE_MODELS_SUBFOLDER
    melspec, embedding = base / MELSPEC_MODEL, base / EMBEDDING_MODEL
    for path in (melspec, embedding):
        if not path.exists():
            raise FileNotFoundError(
                f"Falta {path}. Descárgalo con scripts/fetch_wakeword_runtime.py."
            )
    return AudioFeatures(
        melspec_model_path=str(melspec),
        embedding_model_path=str(embedding),
        inference_framework="onnx",
    )


def pad_to_minimum(clip: np.ndarray, rng: np.random.Generator | None = None) -> np.ndarray:
    minimum = int(MIN_CLIP_SECONDS * SAMPLE_RATE)
    if clip.size >= minimum:
        return clip
    generator = rng or np.random.default_rng(0)
    padded = generator.normal(0, 0.002, minimum).astype(np.float32)
    padded[: clip.size] += clip
    return np.clip(padded, -1.0, 1.0)


def empty_windows() -> np.ndarray:
    return np.empty((0, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32)


def windows_of(features, clip: np.ndarray) -> np.ndarray:
    """Every 16-frame window of embeddings in `clip`, shaped (n, 16, 96)."""
    clip = pad_to_minimum(clip)
    embeddings = features._get_embeddings(np.int16(clip * 32767))
    embeddings = np.atleast_2d(embeddings)
    if embeddings.shape[0] < WINDOW_FRAMES:
        return empty_windows()
    return np.stack(
        [
            embeddings[start : start + WINDOW_FRAMES]
            for start in range(embeddings.shape[0] - WINDOW_FRAMES + 1)
        ]
    ).astype(np.float32)


def window_index_for(end_seconds: float) -> int:
    """Index of the window whose audio coverage ends at `end_seconds`.

    windows[i] holds embedding frames i..i+15, and frame k covers audio up to
    0.08*k + 0.76 s, so the window ends at 0.08*(i + 15) + 0.76.
    """
    last_frame = (end_seconds - EMBEDDING_CONTEXT_SECONDS) / EMBEDDING_HOP_SECONDS
    return int(round(last_frame)) - (WINDOW_FRAMES - 1)


def positive_windows(features, clip: np.ndarray, phrase_end: float) -> np.ndarray:
    """Windows whose audio ends just after the phrase does.

    A window that only catches the first half of "Oye Chat" is not a positive
    example; labelling it as one is how a detector learns to fire on "oye".
    """
    windows = windows_of(features, clip)
    if windows.size == 0:
        return windows

    aligned = window_index_for(phrase_end)
    wanted = [aligned + offset for offset in (-2, -1, 0, 1, 2)]
    chosen = [index for index in wanted if 0 <= index < windows.shape[0]]
    return windows[chosen] if chosen else empty_windows()


def stack(chunks: list[np.ndarray]) -> np.ndarray:
    usable = [chunk for chunk in chunks if chunk.size]
    if not usable:
        return empty_windows()
    return np.concatenate(usable).astype(np.float32)


def balance(
    positives: np.ndarray, negatives: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Trim the larger class so the negatives are never the minority."""
    wanted_positives = int(min(positives.shape[0], negatives.shape[0] / NEGATIVE_RATIO))
    if wanted_positives < 1:
        return positives, negatives

    if positives.shape[0] > wanted_positives:
        keep = rng.choice(positives.shape[0], wanted_positives, replace=False)
        positives = positives[keep]

    wanted_negatives = int(positives.shape[0] * NEGATIVE_RATIO)
    if negatives.shape[0] > wanted_negatives:
        keep = rng.choice(negatives.shape[0], wanted_negatives, replace=False)
        negatives = negatives[keep]

    return positives, negatives


# -- the saved corpus ------------------------------------------------------


def save_corpus(path: Path, positives: np.ndarray, negatives: np.ndarray) -> Path:
    """Keep the synthetic corpus so the application can retrain without espeak.

    Stored as float16: the embeddings lose nothing a classifier with this much
    regularisation can use, and the file is half the size.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        positives=positives.astype(np.float16),
        negatives=negatives.astype(np.float16),
    )
    return path


def load_corpus(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path) as data:
        return data["positives"].astype(np.float32), data["negatives"].astype(np.float32)


# -- training --------------------------------------------------------------


def build_classifier(seed: int):
    from sklearn.neural_network import MLPClassifier

    return MLPClassifier(
        hidden_layer_sizes=(96, 48),
        activation="relu",
        # Heavier regularisation than the default: with 1536 inputs the network
        # will happily memorise which clips it was shown instead of learning
        # what the phrase sounds like.
        alpha=1e-2,
        batch_size=256,
        learning_rate_init=1e-3,
        max_iter=400,
        early_stopping=True,
        n_iter_no_change=20,
        random_state=seed,
    )


@dataclass(frozen=True)
class HeldOut:
    """How the classifier did on the 20% it never trained on."""

    detection: float
    false_rate: float


def train(
    features_matrix: np.ndarray,
    labels: np.ndarray,
    seed: int = 20260912,
    rounds: int = 2,
    log: Log = _print,
    on_round: Callable[[int], None] | None = None,
    threshold: float = 0.5,
):
    """Fit, then mine the negatives it gets wrong and fit again.

    Window-level accuracy flatters a wake word badly: a class produces tens of
    thousands of windows an hour, so a 0.1% error rate is dozens of false
    activations. Hard negative mining -- retraining with emphasis on the
    negatives the model scored highest -- is what closes that gap.

    Returns the classifier and its held-out result at `threshold`.
    """
    from sklearn.model_selection import train_test_split

    flat = features_matrix.reshape(features_matrix.shape[0], -1)
    train_x, test_x, train_y, test_y = train_test_split(
        flat, labels, test_size=0.2, random_state=seed, stratify=labels
    )

    if on_round:
        on_round(1)
    classifier = build_classifier(seed)
    log("Entrenando el clasificador...")
    classifier.fit(train_x, train_y)
    report(classifier.predict_proba(test_x)[:, 1], test_y, "ronda 1", log)

    for round_number in range(2, rounds + 1):
        negatives = train_y == 0
        scores = classifier.predict_proba(train_x[negatives])[:, 1]
        if scores.size == 0:
            break

        # Negatives the model actually got wrong, by an absolute score rather
        # than a quantile: nine out of ten negatives score a flat zero, so the
        # 90th percentile is zero too, and "the worst tenth" would really be an
        # arbitrary tenth of the easy ones.
        hard = train_x[negatives][scores >= HARD_NEGATIVE_SCORE]
        if hard.shape[0] == 0:
            log(f"\nRonda {round_number}: ningún negativo difícil que minar.")
            break

        log(
            f"\nRonda {round_number}: reentrenando con {hard.shape[0]} negativos "
            f"difíciles (puntuación >= {HARD_NEGATIVE_SCORE})."
        )
        if on_round:
            on_round(round_number)
        # Repeated so the optimiser cannot average them away.
        augmented_x = np.concatenate([train_x, np.repeat(hard, 3, axis=0)])
        augmented_y = np.concatenate(
            [train_y, np.zeros(hard.shape[0] * 3, dtype=train_y.dtype)]
        )

        classifier = build_classifier(seed + round_number)
        classifier.fit(augmented_x, augmented_y)
        report(classifier.predict_proba(test_x)[:, 1], test_y, f"ronda {round_number}", log)

    scores = classifier.predict_proba(test_x)[:, 1]
    return classifier, held_out(scores, test_y, threshold)


def held_out(scores: np.ndarray, truth: np.ndarray, threshold: float) -> HeldOut:
    predicted = scores >= threshold
    positives = truth == 1
    detection = float(predicted[positives].mean()) if positives.any() else 0.0
    false_rate = float(predicted[~positives].mean()) if (~positives).any() else 0.0
    return HeldOut(detection=detection, false_rate=false_rate)


def report(scores: np.ndarray, truth: np.ndarray, label: str = "", log: Log = _print) -> None:
    log(f"\nSobre el 20% reservado para prueba ({label}):" if label
        else "\nSobre el 20% reservado para prueba:")
    log(f"{'Umbral':>8} {'Detección':>10} {'Falsos':>8}")
    for threshold in (0.35, 0.5, 0.65, 0.8, 0.95):
        result = held_out(scores, truth, threshold)
        log(f"{threshold:>8.2f} {result.detection:>9.1%} {result.false_rate:>8.2%}")


# -- export ----------------------------------------------------------------


def export_onnx(classifier, destination: Path) -> Path:
    """Write the classifier as the graph openWakeWord expects.

    Built by hand rather than through a converter so the input is exactly
    (1, 16, 96) and the output exactly (1, 1): the detector reads those shapes
    to decide how many embedding frames to feed it.
    """
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes = []
    initialisers = []

    shape = numpy_helper.from_array(
        np.array([1, WINDOW_FRAMES * EMBEDDING_DIMS], dtype=np.int64), "flat_shape"
    )
    initialisers.append(shape)
    nodes.append(helper.make_node("Reshape", ["input", "flat_shape"], ["flat"]))

    current = "flat"
    layers = list(zip(classifier.coefs_, classifier.intercepts_, strict=True))
    for index, (weights, bias) in enumerate(layers):
        weight_name, bias_name = f"W{index}", f"b{index}"
        initialisers.append(
            numpy_helper.from_array(weights.astype(np.float32), weight_name)
        )
        initialisers.append(numpy_helper.from_array(bias.astype(np.float32), bias_name))

        linear = f"linear{index}"
        nodes.append(helper.make_node("Gemm", [current, weight_name, bias_name], [linear]))

        if index < len(layers) - 1:
            activated = f"relu{index}"
            nodes.append(helper.make_node("Relu", [linear], [activated]))
            current = activated
        else:
            # sklearn's binary MLP emits a logit; the detector expects a
            # probability, so the logistic output layer becomes a Sigmoid node.
            nodes.append(helper.make_node("Sigmoid", [linear], ["output"]))

    graph = helper.make_graph(
        nodes,
        "wakeword",
        [helper.make_tensor_value_info(
            "input", TensorProto.FLOAT, [1, WINDOW_FRAMES, EMBEDDING_DIMS]
        )],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1])],
        initialisers,
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="ai-classroom-live"
    )
    model.ir_version = 8  # what the onnxruntime bundled with openWakeWord accepts
    onnx.checker.check_model(model)

    destination.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(destination))
    return destination


class InvalidModel(RuntimeError):
    """The written model is not something the detector can load."""


def verify(path: Path) -> None:
    """Load the written model the way the application will."""
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    shape_in = session.get_inputs()[0].shape
    shape_out = session.get_outputs()[0].shape
    if list(shape_in) != [1, WINDOW_FRAMES, EMBEDDING_DIMS]:
        raise InvalidModel(f"La entrada del modelo es {shape_in}, no [1, 16, 96].")
    if list(shape_out) != [1, 1]:
        raise InvalidModel(f"La salida del modelo es {shape_out}, no [1, 1].")

    probe = np.zeros((1, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32)
    score = session.run(None, {session.get_inputs()[0].name: probe})[0][0][0]
    if not 0.0 <= score <= 1.0:
        raise InvalidModel(f"El modelo devuelve {score}, que no es una probabilidad.")


def score_windows(path: Path, windows: np.ndarray) -> np.ndarray:
    """Scores the written model gives each window, one at a time as in class."""
    import onnxruntime as ort

    if windows.size == 0:
        return np.empty(0, dtype=np.float32)
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    return np.array(
        [session.run(None, {name: window[np.newaxis]})[0][0][0] for window in windows],
        dtype=np.float32,
    )
