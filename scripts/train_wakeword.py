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

from aiclassroom.audio.wakeword import model_filename  # noqa: E402

# Augmentation, features, training and export live in the backend, because the
# application reuses them to train with a teacher's own voice (D-12). What stays
# here is what only a build machine can do: synthesise voices with espeak-ng.
from aiclassroom.voice.training import (  # noqa: E402
    SAMPLE_RATE,
    WINDOW_SECONDS,
    InvalidModel,
    augment,
    balance,
    build_features,
    corpus_filename,
    export_onnx,
    place,
    positive_windows,
    save_corpus,
    stack,
    train,
    verify,
    windows_of,
)

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

    positive_windows_array = stack(positives)
    negative_windows_array = stack(negatives)

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

    print(
        f"\nEjemplos: {positive_windows_array.shape[0]} positivos, "
        f"{negative_windows_array.shape[0]} negativos."
    )
    return positive_windows_array, negative_windows_array


def as_training_set(positives: np.ndarray, negatives: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    features_matrix = np.concatenate([positives, negatives])
    labels = np.concatenate(
        [
            np.ones(positives.shape[0], dtype=np.int64),
            np.zeros(negatives.shape[0], dtype=np.int64),
        ]
    )
    return features_matrix, labels


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

    try:
        features = build_features(arguments.models)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    positives, negatives = build_corpus(
        features,
        arguments.phrase,
        arguments.voices,
        arguments.augmentations,
        arguments.extra_positives,
        arguments.extra_negatives,
    )
    classifier, _ = train(*as_training_set(positives, negatives))

    destination = output / model_filename(arguments.phrase)
    export_onnx(classifier, destination)
    try:
        verify(destination)
    except InvalidModel as exc:
        print(exc, file=sys.stderr)
        return 1

    # The application retrains from this when a teacher adds their own voice:
    # the classroom PC has no espeak-ng, so the synthetic half has to travel.
    corpus = save_corpus(output / corpus_filename(destination.name), positives, negatives)

    size_kb = destination.stat().st_size / 1024
    print(f"\nModelo escrito en {destination} ({size_kb:.0f} KB)")
    print(f"Corpus guardado en {corpus} ({corpus.stat().st_size / 1024 / 1024:.1f} MB)")
    print(
        "\nEstá entrenado con voces sintéticas: sirve para comprobar que la cadena\n"
        "funciona de extremo a extremo, no para llevarlo a un aula. Añade\n"
        "grabaciones reales con --extra-positives y mide después con\n"
        "scripts/measure_wakeword.py (riesgo R-1)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
