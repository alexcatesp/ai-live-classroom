#!/usr/bin/env python3
"""Generate synthetic speech for the end-to-end detector tests.

Recording a real classroom is the only way to settle risk R-1, but it cannot be
done from a build machine, and a test suite that needs someone to speak into a
microphone never runs. espeak-ng fills that gap: it produces real speech --
formants, pauses, voicing -- which is what the detector and the voice gate
actually consume, unlike white noise.

It builds two piles:

    negatives/  continuous Spanish, a teacher explaining things, never saying
                the wake phrase. This is the hard case: the voice gate cannot
                help, because there genuinely is a voice.
    positives/  the wake phrase, in several voices and speeds.

Synthetic speech is not a substitute for a real classroom -- the voices are
robotic and there is no room acoustics -- so treat the numbers as a floor, not
a prediction.

    python scripts/make_test_audio.py --output /tmp/audio-pruebas
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16_000

# A lesson that never says the wake phrase. Deliberately includes words that
# share sounds with it -- "oye", "chat", "choque" -- because near misses are
# what actually trips a wake word detector.
LESSON = [
    "Buenos días. Hoy vamos a ver cómo funciona una petición HTTP.",
    "Oye, Marta, ¿puedes bajar la persiana? Gracias.",
    "El cliente envía una petición y el servidor devuelve una respuesta.",
    "¿Alguien ha usado alguna vez un chat de soporte por internet?",
    "Fijaos en la cabecera: ahí viaja el tipo de contenido.",
    "Si el servidor no responde, el navegador muestra un error de tiempo agotado.",
    "Un choque de nombres en el código se resuelve con espacios de nombres.",
    "Abrid el editor y escribid una función que reciba dos parámetros.",
    "Cuidado con las mayúsculas, que el intérprete distingue.",
    "Para la semana que viene, leed el apartado tres del tema.",
]

VOICES = ("es", "es-419")
SPEEDS = (140, 175, 210)


def synthesize(text: str, path: Path, voice: str = "es", speed: int = 175) -> Path:
    """Render `text` with espeak-ng, resampled to what the detector expects."""
    raw = path.with_suffix(".raw.wav")
    subprocess.run(
        ["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(raw), text],
        check=True,
        capture_output=True,
    )

    with wave.open(str(raw), "rb") as source:
        rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)

    if rate != TARGET_RATE:
        from math import gcd

        divisor = gcd(rate, TARGET_RATE)
        from scipy.signal import resample_poly

        samples = resample_poly(samples, TARGET_RATE // divisor, rate // divisor)
        samples = np.clip(samples, -32768, 32767).astype(np.int16)

    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(TARGET_RATE)
        target.writeframes(samples.tobytes())

    raw.unlink()
    return path


def build(output: Path, phrase: str) -> tuple[int, int]:
    negatives = output / "negatives"
    positives = output / "positives"
    for folder in (negatives, positives):
        folder.mkdir(parents=True, exist_ok=True)

    for index, sentence in enumerate(LESSON):
        voice = VOICES[index % len(VOICES)]
        speed = SPEEDS[index % len(SPEEDS)]
        synthesize(sentence, negatives / f"clase_{index:02d}.wav", voice, speed)

    made = 0
    for voice in VOICES:
        for speed in SPEEDS:
            synthesize(
                f"{phrase}, ¿puedes explicar esto?",
                positives / f"frase_{voice}_{speed}.wav",
                voice,
                speed,
            )
            made += 1

    return len(LESSON), made


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, default=Path("/tmp/audio-pruebas"))
    parser.add_argument("--phrase", default="Oye Chat")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)

    if shutil.which("espeak-ng") is None:
        print(
            "Hace falta espeak-ng. Instálalo con:\n"
            "    sudo apt-get install espeak-ng      (Linux)\n"
            "    choco install espeak                (Windows)",
            file=sys.stderr,
        )
        return 1

    negatives, positives = build(arguments.output, arguments.phrase)
    print(f"{negatives} clips de clase en {arguments.output / 'negatives'}")
    print(f"{positives} clips de la frase en {arguments.output / 'positives'}")
    print(
        "\nVoces sintéticas: sirven para comprobar que la cadena funciona, no\n"
        "para predecir un aula real. Para eso hay que grabar una clase."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
