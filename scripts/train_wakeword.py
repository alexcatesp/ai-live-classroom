#!/usr/bin/env python3
"""Train the wake word model for the activation phrase (D-04).

openWakeWord splits a detector into three pieces: a melspectrogram front end, a
frozen speech embedding model, and a small classifier trained per phrase. The
first two ship with the application; this script trains the third and writes it
as `<frase>.onnx`, which is the file the detector loads.

    python scripts/train_wakeword.py --phrase "Oye Chat" --models data/models

Training data comes from espeak-ng, which speaks Spanish in a hundred or so
voices. Positives are the phrase; negatives are lesson sentences, near misses
that share sounds with it ("oye", "chat", "choque") and noise. Everything is
augmented with gain, noise, time shifts and a crude room reflection, because a
classroom is not a recording booth.

WHAT THIS MODEL IS AND IS NOT
-----------------------------
Synthetic voices are not human voices. A model trained only on espeak learns to
recognise espeak saying the phrase, and will do noticeably worse on a teacher
standing four metres from a laptop microphone. That is enough to bring up the
whole Phase 0 chain and to prove it works end to end, and it is NOT enough to
take into a classroom and trust.

Before real use, add recordings of actual people saying the phrase:

    python scripts/train_wakeword.py --phrase "Oye Chat" \\
        --extra-positives grabaciones/frase \\
        --extra-negatives grabaciones/aula

Thirty seconds of a few real voices is worth more than a thousand synthetic
clips. Then measure with scripts/measure_wakeword.py and record the result in
docs/decisiones-tecnicas.md (risk R-1).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from aiclassroom.audio.wakeword import (  # noqa: E402
    BASE_MODELS_SUBFOLDER,
    EMBEDDING_MODEL,
    MELSPEC_MODEL,
    model_filename,
)

SAMPLE_RATE = 16_000

# The detector consumes a window of 16 embedding frames of 96 values each; the
# model this script writes has to accept exactly that (see the loaded model's
# input shape in openwakeword.model.Model).
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

# Things a teacher says immediately before invoking the assistant. Without
# these, every positive has silence in front of the phrase and the model does
# not recognise it mid-sentence -- which is exactly how it gets used.
LEAD_INS = [
    "vamos a ver",
    "mirad",
    "un momento",
    "a ver",
    "entonces",
    "fijaos",
    "pues nada",
    "bueno",
    "y ahora",
    "escuchad esto",
]

# Words and phrases that are not the wake phrase but share sounds with it.
# Training against near misses is what stops "oye, Marta" waking the assistant.
NEAR_MISSES = [
    "oye", "oye Marta", "oye un momento", "escuchad", "chat", "el chat",
    "un chat de soporte", "choque", "coche", "ocho", "hoy", "ollé",
    "oye qué tal", "chatear", "la charla", "ya está", "o sea",
]

# Negative content has to be *varied*, not merely plentiful. Trained against a
# couple of dozen sentences the classifier learns "those sentences are not the
# phrase" and treats everything else, including all other speech, as a maybe --
# which is exactly what the first trained model did: 782 false activations an
# hour on ordinary Spanish. Random word sequences give cheap phonetic breadth.
VOCABULARY = """
casa mesa libro perro gato agua fuego tierra aire cielo sol luna estrella nube
lluvia viento frío calor mañana tarde noche semana mes año tiempo hora minuto
hombre mujer niño niña gente persona familia amigo vecino profesor alumno clase
escuela instituto aula pizarra cuaderno lápiz bolígrafo papel carpeta mochila
ordenador pantalla teclado ratón programa archivo carpeta datos número letra
código función variable bucle lista tabla base consulta servidor cliente red
internet página enlace correo mensaje respuesta pregunta problema solución
ejemplo ejercicio práctica examen nota trabajo proyecto grupo equipo tarea
comer beber dormir correr andar hablar escuchar mirar leer escribir pensar
hacer decir tener poner venir salir entrar subir bajar abrir cerrar empezar
terminar seguir parar cambiar buscar encontrar perder ganar jugar aprender
grande pequeño alto bajo largo corto nuevo viejo joven bueno malo mejor peor
rápido lento fácil difícil claro oscuro limpio sucio lleno vacío abierto
también además entonces aunque mientras porque cuando donde como siempre nunca
mucho poco bastante demasiado todo nada algo alguien nadie cada otro mismo
"""

LESSON = [
    "Buenos días, hoy vamos a ver cómo funciona una petición HTTP.",
    "El cliente envía una petición y el servidor devuelve una respuesta.",
    "¿Alguien ha usado alguna vez un chat de soporte por internet?",
    "Fijaos en la cabecera, ahí viaja el tipo de contenido.",
    "Si el servidor no responde, el navegador muestra un error.",
    "Abrid el editor y escribid una función con dos parámetros.",
    "Cuidado con las mayúsculas, que el intérprete distingue.",
    "Para la semana que viene leed el apartado tres del tema.",
    "Un choque de nombres se resuelve con espacios de nombres.",
    "Vamos a repasar lo que vimos ayer sobre bases de datos.",
    "¿Alguna duda hasta aquí? Preguntad sin problema.",
    "El resultado se guarda en una variable y se muestra por pantalla.",
]


# -- synthesis -------------------------------------------------------------


def random_short_utterances(rng: np.random.Generator, count: int) -> list[str]:
    """One to three words, the same shape as the wake phrase.

    These matter more than any other negative. Positives are short utterances
    surrounded by room tone; if every negative is a long continuous sentence,
    the quickest thing for the classifier to learn is "brief speech between
    silences", which it duly did -- scoring 1.0 on "mesa" and "ocho". Negatives
    have to be laid out exactly like positives so that isolation carries no
    information at all.
    """
    words = VOCABULARY.split()
    utterances = []
    for _ in range(count):
        length = int(rng.integers(1, 4))
        utterances.append(" ".join(rng.choice(words, size=length, replace=True)))
    return utterances


def random_sentences(rng: np.random.Generator, count: int) -> list[str]:
    """Throwaway Spanish sentences, for phonetic variety rather than meaning."""
    words = VOCABULARY.split()
    sentences = []
    for _ in range(count):
        length = int(rng.integers(4, 13))
        chosen = rng.choice(words, size=length, replace=True)
        sentences.append(" ".join(chosen) + ".")
    return sentences


def spanish_variants(limit: int) -> list[tuple[str, int, int]]:
    """(voice, speed, pitch) combinations, as varied as espeak-ng allows."""
    base = ["es", "es-419"]
    variants = _installed_variants()
    voices = [f"{language}+{variant}" for language in base for variant in variants]
    voices = base + voices

    combinations = [
        (voice, speed, pitch)
        for voice in voices
        for speed in (130, 160, 190)
        for pitch in (30, 50, 70)
    ]
    rng = np.random.default_rng(20260912)
    rng.shuffle(combinations)
    return combinations[:limit]


def _installed_variants() -> list[str]:
    """Voice variants espeak-ng has on this machine, whatever they are called."""
    try:
        listing = subprocess.run(
            ["espeak-ng", "--voices=variant"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[1:]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return []

    names = []
    for line in listing:
        parts = line.split()
        if len(parts) >= 4:
            # The file column carries the name espeak accepts after "+".
            names.append(parts[-1].split("/")[-1])
    return [name for name in names if name]


def speak(text: str, voice: str, speed: int, pitch: int, destination: Path) -> np.ndarray | None:
    raw = destination / "raw.wav"
    try:
        subprocess.run(
            ["espeak-ng", "-v", voice, "-s", str(speed), "-p", str(pitch),
             "-w", str(raw), text],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    try:
        with wave.open(str(raw), "rb") as clip:
            rate = clip.getframerate()
            samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError):
        return None
    finally:
        raw.unlink(missing_ok=True)

    if samples.size == 0:
        return None
    return resample(samples.astype(np.float32) / 32768.0, rate, SAMPLE_RATE)


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    from math import gcd

    from scipy.signal import resample_poly

    divisor = gcd(source_rate, target_rate)
    return resample_poly(samples, target_rate // divisor, source_rate // divisor)


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
    from openwakeword.utils import AudioFeatures

    base = models_dir / BASE_MODELS_SUBFOLDER
    melspec, embedding = base / MELSPEC_MODEL, base / EMBEDDING_MODEL
    for path in (melspec, embedding):
        if not path.exists():
            raise SystemExit(
                f"Falta {path}. Descárgalo antes con:\n"
                f"    python scripts/fetch_wakeword_runtime.py --output {models_dir}"
            )
    return AudioFeatures(
        melspec_model_path=str(melspec),
        embedding_model_path=str(embedding),
        inference_framework="onnx",
    )


# The embedding model consumes 76 melspectrogram frames at a time, so a clip
# shorter than roughly a second cannot produce a single embedding. Short words
# like "oye" are exactly that, and they are among the most important negatives,
# so they are padded with room tone rather than dropped.
MIN_CLIP_SECONDS = 2.0


def pad_to_minimum(clip: np.ndarray, rng: np.random.Generator | None = None) -> np.ndarray:
    minimum = int(MIN_CLIP_SECONDS * SAMPLE_RATE)
    if clip.size >= minimum:
        return clip
    generator = rng or np.random.default_rng(0)
    padded = generator.normal(0, 0.002, minimum).astype(np.float32)
    padded[: clip.size] += clip
    return np.clip(padded, -1.0, 1.0)


def windows_of(features, clip: np.ndarray) -> np.ndarray:
    """Every 16-frame window of embeddings in `clip`, shaped (n, 16, 96)."""
    clip = pad_to_minimum(clip)
    embeddings = features._get_embeddings(np.int16(clip * 32767))
    embeddings = np.atleast_2d(embeddings)
    if embeddings.shape[0] < WINDOW_FRAMES:
        return np.empty((0, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32)
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
    return windows[chosen] if chosen else np.empty(
        (0, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32
    )


# -- corpus ----------------------------------------------------------------


def read_wav(path: Path) -> np.ndarray | None:
    try:
        with wave.open(str(path), "rb") as clip:
            rate = clip.getframerate()
            channels = clip.getnchannels()
            samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError):
        return None
    if samples.size == 0:
        return None
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return resample(samples.astype(np.float32) / 32768.0, rate, SAMPLE_RATE)


def extra_clips(folder: Path | None) -> Iterator[np.ndarray]:
    if folder is None:
        return
    for path in sorted(folder.glob("*.wav")):
        clip = read_wav(path)
        if clip is not None:
            yield clip


def build_corpus(
    features,
    phrase: str,
    voice_count: int,
    augmentations: int,
    extra_positives: Path | None,
    extra_negatives: Path | None,
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(20260912)
    combinations = spanish_variants(voice_count)
    positives: list[np.ndarray] = []
    negatives: list[np.ndarray] = []

    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch)

        print(f"Sintetizando «{phrase}» en {len(combinations)} voces...")
        for index, (voice, speed, pitch) in enumerate(combinations):
            spoken = speak(phrase, voice, speed, pitch, workspace)
            if spoken is None:
                continue
            for attempt in range(augmentations):
                # The first pass is the clip as synthesised, so clean speech is
                # in-distribution; the rest are roughed up.
                processed = augment(spoken, rng, clean=attempt == 0)

                # Half the examples have someone talking right up to the
                # phrase, so it is recognised mid-sentence and not only after a
                # pause. The phrase still ends the audio, which is the instant
                # the window is labelled by.
                body = processed
                if attempt % 2 == 1:
                    spoken_lead = speak(
                        LEAD_INS[int(rng.integers(0, len(LEAD_INS)))],
                        voice, speed, pitch, workspace,
                    )
                    if spoken_lead is not None:
                        body = np.concatenate([spoken_lead, processed])

                # Land the phrase so it ends past the window length, varying
                # where, so the model tolerates different speaking rates.
                ends_at = rng.uniform(WINDOW_SECONDS + 0.05, WINDOW_SECONDS + 0.9)
                lead = max(0.1, ends_at - body.size / SAMPLE_RATE)
                clip, phrase_end = place(body, lead, 1.0, rng)
                positives.append(positive_windows(features, clip, phrase_end))
            if index and index % 25 == 0:
                print(f"  {index}/{len(combinations)}")

        # Negatives get the same spread of voices as the positives. Trained
        # against a handful of voices the model learns "this is not Pedro
        # saying it" rather than "this is not the phrase", and then fires on
        # ordinary teaching -- which is exactly what happened the first time.
        print("Sintetizando negativos (clase, casi-aciertos, habla variada y ruido)...")
        # Curated sentences first -- the ones that actually happen in a class --
        # then as much random speech as there are voice combinations.
        negative_texts = (
            LESSON + NEAR_MISSES + random_sentences(rng, max(len(combinations), 60))
        )
        rng.shuffle(negative_texts)
        for index, (voice, speed, pitch) in enumerate(combinations):
            text = negative_texts[index % len(negative_texts)]
            spoken = speak(text, voice, speed, pitch, workspace)
            if spoken is None:
                continue
            for attempt in range(max(2, augmentations // 2)):
                negatives.append(
                    windows_of(features, augment(spoken, rng, clean=attempt == 0))
                )
            if index and index % 25 == 0:
                print(f"  {index}/{len(combinations)}")

        # Short utterances placed exactly like the positives: same lead, same
        # tail, same aligned window. This is what forces the model to listen to
        # the phrase rather than to the silence around it.
        print("Sintetizando enunciados cortos con la misma forma que la frase...")
        short_negatives = NEAR_MISSES + random_short_utterances(rng, len(combinations))
        rng.shuffle(short_negatives)
        for index, (voice, speed, pitch) in enumerate(combinations):
            text = short_negatives[index % len(short_negatives)]
            spoken = speak(text, voice, speed, pitch, workspace)
            if spoken is None:
                continue
            for attempt in range(max(2, augmentations // 2)):
                processed = augment(spoken, rng, clean=attempt == 0)
                utterance_seconds = processed.size / SAMPLE_RATE
                ends_at = rng.uniform(WINDOW_SECONDS + 0.05, WINDOW_SECONDS + 0.9)
                lead = max(0.1, ends_at - utterance_seconds)
                clip, utterance_end = place(processed, lead, 1.0, rng)
                # Both the aligned window and everything around it: a near miss
                # is negative wherever it falls.
                negatives.append(positive_windows(features, clip, utterance_end))
                negatives.append(windows_of(features, clip))
            if index and index % 25 == 0:
                print(f"  {index}/{len(combinations)}")

        # Pure noise, so silence and room tone are firmly negative.
        for _ in range(60):
            noise = rng.normal(0, rng.uniform(0.005, 0.08), int(3.0 * SAMPLE_RATE))
            negatives.append(windows_of(features, np.clip(noise, -1, 1).astype(np.float32)))

    for clip in extra_clips(extra_positives):
        # A real recording is trimmed to the phrase, so its end is the clip end.
        positives.append(positive_windows(features, clip, clip.size / SAMPLE_RATE))
    for clip in extra_clips(extra_negatives):
        negatives.append(windows_of(features, clip))

    positive_windows_array = _stack(positives)
    negative_windows_array = _stack(negatives)

    if positive_windows_array.size == 0:
        raise SystemExit("No se generó ningún ejemplo positivo. ¿Está espeak-ng instalado?")
    if negative_windows_array.size == 0:
        raise SystemExit("No se generó ningún ejemplo negativo.")

    # Negatives must not be outnumbered. A classifier fed four positives for
    # every negative learns that saying yes is usually right, and a wake word
    # that is usually right is useless: a class has one activation and
    # thousands of chances to fire wrongly.
    positive_windows_array, negative_windows_array = balance(
        positive_windows_array, negative_windows_array, rng
    )

    features_matrix = np.concatenate([positive_windows_array, negative_windows_array])
    labels = np.concatenate(
        [
            np.ones(positive_windows_array.shape[0], dtype=np.int64),
            np.zeros(negative_windows_array.shape[0], dtype=np.int64),
        ]
    )
    print(
        f"\nEjemplos: {positive_windows_array.shape[0]} positivos, "
        f"{negative_windows_array.shape[0]} negativos."
    )
    return features_matrix, labels


# How many negative examples to keep per positive. Above one, the model is
# pushed towards silence, which is the safer direction for a classroom.
NEGATIVE_RATIO = 2.0


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


def _stack(chunks: list[np.ndarray]) -> np.ndarray:
    usable = [chunk for chunk in chunks if chunk.size]
    if not usable:
        return np.empty((0, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32)
    return np.concatenate(usable).astype(np.float32)


# -- training --------------------------------------------------------------


# A negative the model scores above this is one it nearly fired on, and the
# only kind worth emphasising in a second round.
HARD_NEGATIVE_SCORE = 0.1


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


def train(features_matrix: np.ndarray, labels: np.ndarray, seed: int = 20260912, rounds: int = 2):
    """Fit, then mine the negatives it gets wrong and fit again.

    Window-level accuracy flatters a wake word badly: a class produces tens of
    thousands of windows an hour, so a 0.1% error rate is dozens of false
    activations. Hard negative mining -- retraining with emphasis on the
    negatives the model scored highest -- is what closes that gap.
    """
    from sklearn.model_selection import train_test_split

    flat = features_matrix.reshape(features_matrix.shape[0], -1)
    train_x, test_x, train_y, test_y = train_test_split(
        flat, labels, test_size=0.2, random_state=seed, stratify=labels
    )

    classifier = build_classifier(seed)
    print("Entrenando el clasificador...")
    classifier.fit(train_x, train_y)
    report(classifier.predict_proba(test_x)[:, 1], test_y, "ronda 1")

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
            print(f"\nRonda {round_number}: ningún negativo difícil que minar.")
            break

        print(
            f"\nRonda {round_number}: reentrenando con {hard.shape[0]} negativos "
            f"difíciles (puntuación >= {HARD_NEGATIVE_SCORE})."
        )
        # Repeated so the optimiser cannot average them away.
        augmented_x = np.concatenate([train_x, np.repeat(hard, 3, axis=0)])
        augmented_y = np.concatenate(
            [train_y, np.zeros(hard.shape[0] * 3, dtype=train_y.dtype)]
        )

        classifier = build_classifier(seed + round_number)
        classifier.fit(augmented_x, augmented_y)
        report(classifier.predict_proba(test_x)[:, 1], test_y, f"ronda {round_number}")

    return classifier


def report(scores: np.ndarray, truth: np.ndarray, label: str = "") -> None:
    print(f"\nSobre el 20% reservado para prueba ({label}):" if label
          else "\nSobre el 20% reservado para prueba:")
    print(f"{'Umbral':>8} {'Detección':>10} {'Falsos':>8}")
    for threshold in (0.35, 0.5, 0.65, 0.8, 0.95):
        predicted = scores >= threshold
        positives = truth == 1
        detection = predicted[positives].mean() if positives.any() else 0.0
        false_rate = predicted[~positives].mean() if (~positives).any() else 0.0
        print(f"{threshold:>8.2f} {detection:>9.1%} {false_rate:>8.2%}")


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
    layers = list(zip(classifier.coefs_, classifier.intercepts_))
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


def verify(path: Path) -> None:
    """Load the written model the way the application will."""
    import onnxruntime as ort

    session = ort.InferenceSession(str(path))
    shape_in = session.get_inputs()[0].shape
    shape_out = session.get_outputs()[0].shape
    if list(shape_in) != [1, WINDOW_FRAMES, EMBEDDING_DIMS]:
        raise SystemExit(f"La entrada del modelo es {shape_in}, no [1, 16, 96].")
    if list(shape_out) != [1, 1]:
        raise SystemExit(f"La salida del modelo es {shape_out}, no [1, 1].")

    probe = np.zeros((1, WINDOW_FRAMES, EMBEDDING_DIMS), dtype=np.float32)
    score = session.run(None, {session.get_inputs()[0].name: probe})[0][0][0]
    if not 0.0 <= score <= 1.0:
        raise SystemExit(f"El modelo devuelve {score}, que no es una probabilidad.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--phrase", default="Oye Chat")
    parser.add_argument("--models", type=Path, default=Path("data/models"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--voices", type=int, default=120)
    parser.add_argument("--augmentations", type=int, default=6)
    parser.add_argument("--extra-positives", type=Path, default=None)
    parser.add_argument("--extra-negatives", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    output = arguments.output or arguments.models

    if shutil.which("espeak-ng") is None and arguments.extra_positives is None:
        print(
            "Hace falta espeak-ng para sintetizar las voces, o grabaciones reales\n"
            "en --extra-positives. Instálalo con: sudo apt-get install espeak-ng",
            file=sys.stderr,
        )
        return 1

    features = build_features(arguments.models)
    matrix, labels = build_corpus(
        features,
        arguments.phrase,
        arguments.voices,
        arguments.augmentations,
        arguments.extra_positives,
        arguments.extra_negatives,
    )
    classifier = train(matrix, labels)

    destination = output / model_filename(arguments.phrase)
    export_onnx(classifier, destination)
    verify(destination)

    size_kb = destination.stat().st_size / 1024
    print(f"\nModelo escrito en {destination} ({size_kb:.0f} KB)")
    print(
        "\nEstá entrenado con voces sintéticas: sirve para comprobar que la cadena\n"
        "funciona de extremo a extremo, no para llevarlo a un aula. Añade\n"
        "grabaciones reales con --extra-positives y mide después con\n"
        "scripts/measure_wakeword.py (riesgo R-1)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
