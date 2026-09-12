#!/usr/bin/env python3
"""Download openWakeWord's feature extractor into the portable folder.

openWakeWord fetches two shared models the first time it runs -- a
melspectrogram front end and Google's speech embedding model. Letting that
happen on a classroom PC would mean a download seconds before a lesson, on a
network that may well block it. So they are fetched at build time instead and
shipped inside data/models/openwakeword/.

Run from the build machine:

    python scripts/fetch_wakeword_runtime.py --output data/models
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# What openWakeWord needs regardless of which wake phrase is used: the feature
# extractor, plus Silero's voice activity model, which is what keeps a scraping
# chair from waking the assistant (risk R-1).
BASE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx")
SUBFOLDER = "openwakeword"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/models"),
        help="carpeta de modelos de la aplicación",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    target = arguments.output / SUBFOLDER
    target.mkdir(parents=True, exist_ok=True)

    missing = [name for name in BASE_MODELS if not (target / name).exists()]
    if not missing:
        print(f"Los modelos base ya están en {target}.")
        return 0

    try:
        from openwakeword.utils import download_models
    except ImportError:
        print(
            "openWakeWord no está instalado. Instálalo con:\n"
            '    pip install -e "backend[wakeword]"',
            file=sys.stderr,
        )
        return 1

    print(f"Descargando {', '.join(missing)} en {target}…")
    download_models(model_names=[], target_directory=str(target))

    # The download also brings openWakeWord's pretrained English wake words
    # ("alexa", "hey jarvis", "timer"...) and the tflite duplicates of the base
    # models. None of them is ever loaded here -- the phrase is Spanish and the
    # detector runs on ONNX -- so they are removed. Not for weight, which is no
    # longer a constraint, but because a model that is never loaded is a model
    # nobody will notice has gone stale.
    removed = 0
    for extra in sorted(target.iterdir()):
        if extra.is_file() and extra.name not in BASE_MODELS:
            extra.unlink()
            removed += 1
    if removed:
        print(f"Descartados {removed} modelos que esta aplicación no usa.")

    still_missing = [name for name in BASE_MODELS if not (target / name).exists()]
    if still_missing:
        print(
            f"No se descargaron: {', '.join(still_missing)}. "
            "Comprueba la conexión de la máquina de construcción.",
            file=sys.stderr,
        )
        return 1

    for name in BASE_MODELS:
        size = (target / name).stat().st_size / (1024 * 1024)
        print(f"  {name}  {size:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
