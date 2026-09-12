#!/usr/bin/env python3
"""Measure the wake word detector against real recordings (risk R-1).

This is what turns "we think the sensitivity is about right" into a number.
Point it at two folders of 16 kHz mono WAV files:

    negatives/   a class being taught, with nobody saying the wake phrase
    positives/   short clips of people actually saying "Oye Chat"

and it sweeps the settings, reporting for each one how often the assistant
would have woken up by mistake, and how often it would have missed a real
activation:

    python scripts/measure_wakeword.py \\
        --models data/models --negatives grabaciones/aula --positives grabaciones/frase

A useful classroom target is zero false activations per hour with a detection
rate above 90%. Record the numbers you get in docs/decisiones-tecnicas.md:
they are the evidence R-1 asks for.

Recording the negatives matters more than the positives. An hour of a real
class -- chairs, chatter, a projector fan, the teacher talking continuously --
is what a detector has to survive, and it is the part no synthetic test covers.
"""

from __future__ import annotations

import argparse
import sys
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from aiclassroom.audio.devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES  # noqa: E402
from aiclassroom.audio.wakeword import (  # noqa: E402
    WakeWordDetector,
    WakeWordUnavailable,
    create_detector,
    threshold_for,
)

SECONDS_PER_HOUR = 3600.0


class AudioFormatError(ValueError):
    """Raised for a clip the detector cannot consume as-is."""


@dataclass(frozen=True)
class Measurement:
    """What one combination of settings did on one pile of audio."""

    sensitivity: float
    vad_threshold: float
    confirmation_frames: int
    activations: int
    clips_with_activation: int
    clips: int
    audio_seconds: float

    @property
    def activations_per_hour(self) -> float:
        if self.audio_seconds <= 0:
            return 0.0
        return self.activations * SECONDS_PER_HOUR / self.audio_seconds

    @property
    def clip_hit_rate(self) -> float:
        """Share of clips where the assistant woke at least once."""
        if self.clips == 0:
            return 0.0
        return self.clips_with_activation / self.clips


def read_frames(path: Path) -> Iterator[np.ndarray]:
    """Yield 80 ms frames from a 16 kHz mono 16-bit WAV, exactly as PortAudio would."""
    with wave.open(str(path), "rb") as clip:
        if clip.getframerate() != CAPTURE_SAMPLE_RATE:
            raise AudioFormatError(
                f"{path.name}: {clip.getframerate()} Hz. Conviértelo a "
                f"{CAPTURE_SAMPLE_RATE} Hz mono de 16 bits, por ejemplo con:\n"
                f"    ffmpeg -i {path.name} -ar {CAPTURE_SAMPLE_RATE} -ac 1 "
                f"-sample_fmt s16 salida.wav"
            )
        if clip.getsampwidth() != 2:
            raise AudioFormatError(f"{path.name}: debe ser de 16 bits por muestra.")

        channels = clip.getnchannels()
        while True:
            raw = clip.readframes(FRAME_SAMPLES)
            samples = np.frombuffer(raw, dtype=np.int16)
            if channels > 1:
                # Mix down rather than refuse: a stereo classroom recording is
                # still a perfectly good negative.
                samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
            if samples.size < FRAME_SAMPLES:
                return  # a partial tail is never fed to the detector
            yield samples


def clip_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as clip:
        return clip.getnframes() / clip.getframerate()


def run_detector(detector: WakeWordDetector, clips: list[Path]) -> tuple[int, int, float]:
    """Returns (activations, clips that activated, seconds of audio)."""
    activations = 0
    clips_with_activation = 0
    seconds = 0.0

    for path in clips:
        detector.reset()
        seconds += clip_seconds(path)
        in_this_clip = sum(
            1 for frame in read_frames(path) if detector.process(frame) is not None
        )
        activations += in_this_clip
        if in_this_clip:
            clips_with_activation += 1

    return activations, clips_with_activation, seconds


def measure(
    models_dir: Path,
    phrase: str,
    clips: list[Path],
    sensitivity: float,
    vad_threshold: float,
    confirmation_frames: int,
) -> Measurement:
    detector = create_detector(
        models_dir=models_dir,
        phrase=phrase,
        sensitivity=sensitivity,
        # Each clip is measured independently, so nothing should be suppressed
        # across clips; within a clip the real refractory window still applies.
        refractory_seconds=2.0,
        vad_threshold=vad_threshold,
        confirmation_frames=confirmation_frames,
    )
    activations, clips_with_activation, seconds = run_detector(detector, clips)
    return Measurement(
        sensitivity=sensitivity,
        vad_threshold=vad_threshold,
        confirmation_frames=confirmation_frames,
        activations=activations,
        clips_with_activation=clips_with_activation,
        clips=len(clips),
        audio_seconds=seconds,
    )


def collect(folder: Path | None) -> list[Path]:
    if folder is None:
        return []
    if not folder.is_dir():
        raise AudioFormatError(f"No existe la carpeta {folder}.")
    return sorted(folder.glob("*.wav"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", type=Path, default=Path("data/models"))
    parser.add_argument("--phrase", default="Oye Chat")
    parser.add_argument("--negatives", type=Path, help="audio de aula SIN la frase")
    parser.add_argument("--positives", type=Path, help="clips CON la frase")
    parser.add_argument(
        "--sensitivities",
        type=float,
        nargs="+",
        default=[0.2, 0.35, 0.5, 0.65, 0.8],
        help="valores de sensibilidad a probar",
    )
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--confirmation-frames", type=int, default=2)
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="mide sin el filtro de voz, para ver cuánto aporta",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    vad_threshold = 0.0 if arguments.no_vad else arguments.vad_threshold

    try:
        negatives = collect(arguments.negatives)
        positives = collect(arguments.positives)
    except AudioFormatError as error:
        print(error, file=sys.stderr)
        return 1

    if not negatives and not positives:
        print(
            "Indica al menos --negatives o --positives con archivos .wav.\n"
            "Sin grabaciones reales esto no mide nada útil: graba una clase.",
            file=sys.stderr,
        )
        return 1

    print(f"Frase: «{arguments.phrase}»   VAD: {vad_threshold or 'desactivada'}   "
          f"Confirmación: {arguments.confirmation_frames} frames")
    print(f"Negativos: {len(negatives)} clips   Positivos: {len(positives)} clips\n")
    print(f"{'Sens.':>6} {'Umbral':>7} {'Falsos/hora':>12} {'Detección':>10}")
    print("-" * 40)

    try:
        for sensitivity in arguments.sensitivities:
            settings = (sensitivity, vad_threshold, arguments.confirmation_frames)

            false_rate = "n/d"
            if negatives:
                on_noise = measure(arguments.models, arguments.phrase, negatives, *settings)
                false_rate = f"{on_noise.activations_per_hour:.1f}"

            detection = "n/d"
            if positives:
                on_phrase = measure(arguments.models, arguments.phrase, positives, *settings)
                detection = f"{on_phrase.clip_hit_rate:.0%}"

            print(
                f"{sensitivity:>6.2f} {threshold_for(sensitivity):>7.2f} "
                f"{false_rate:>12} {detection:>10}"
            )
    except WakeWordUnavailable as error:
        print(f"\n{error}", file=sys.stderr)
        return 1
    except AudioFormatError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    print(
        "\nObjetivo razonable en aula: 0 falsos por hora con detección > 90%.\n"
        "Anota estos números en docs/decisiones-tecnicas.md (riesgo R-1)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
