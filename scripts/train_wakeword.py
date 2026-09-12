#!/usr/bin/env python3
"""Generate the openWakeWord model for the activation phrase (D-04).

openWakeWord trains a small classifier on top of a frozen feature extractor,
using speech synthesised in many voices as positive examples. Its training
pipeline is a heavy dependency (torch, a TTS model, hours of negative audio),
so it is deliberately kept out of the application and out of the CI: this
script is run once, by hand, and the resulting .onnx is copied into
data/models/ of the portable folder.

    python scripts/train_wakeword.py --phrase "Oye Chat" --output data/models

Risk R-1 -- the false positive rate in a noisy classroom -- is not settled by
this script. Measure it with the detector meter in the application during a
real class, and record the result in docs/decisiones-tecnicas.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MISSING_TRAINING_DEPENDENCIES = """\
Faltan las dependencias de entrenamiento de openWakeWord.

Instálalas en un entorno aparte (no forman parte de la aplicación):

    python -m venv .venv-train
    .venv-train/bin/pip install openwakeword[training] piper-phonemize

Después vuelve a ejecutar este script.
"""


def slug(phrase: str) -> str:
    return "_".join(part for part in phrase.lower().split() if part) or "wakeword"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phrase", default="Oye Chat", help="frase de activación")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/models"),
        help="carpeta donde se escribe el modelo .onnx",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=2000,
        help="número de ejemplos positivos sintetizados",
    )
    parser.add_argument(
        "--language",
        default="es",
        help="idioma de las voces sintetizadas (spec sección 12: español de España)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)

    try:
        from openwakeword.train import train_model  # type: ignore[import-not-found]
    except ImportError:
        print(MISSING_TRAINING_DEPENDENCIES, file=sys.stderr)
        return 1

    arguments.output.mkdir(parents=True, exist_ok=True)
    target = arguments.output / f"{slug(arguments.phrase)}.onnx"

    print(f"Entrenando el detector para «{arguments.phrase}» ({arguments.samples} ejemplos)…")
    train_model(
        target_phrase=[arguments.phrase],
        model_name=slug(arguments.phrase),
        n_samples=arguments.samples,
        language=arguments.language,
        output_dir=str(arguments.output),
    )

    if not target.exists():
        print(f"El entrenamiento terminó pero no se encontró {target}.", file=sys.stderr)
        return 1

    print(f"\nModelo escrito en {target}")
    print("Cópialo a data/models/ de la carpeta portable y comprueba los falsos")
    print("positivos en un aula real antes de darlo por bueno (riesgo R-1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
