"""faster-whisper as a Windows service, outside Docker (D-14).

Speaches in a container was the first home for transcription, and on a 16 GB
card it did not fit: WSL cannot use the last few gigabytes Windows holds back,
so Whisper failed with "CUDA out of memory" while nvidia-smi still showed 3 GB
free -- twice taking Docker's whole engine down with it. A native process has
the whole card available and, when it runs short, Windows moves it to shared
memory instead of failing.

It answers the same requests the application already sends (the OpenAI shape
speaches implements), so switching between the two is a change of address:

    GET  /health                    -> {"status": "ok", "model": ...}
    GET  /v1/models                 -> the model it has loaded
    POST /v1/audio/transcriptions   -> multipart: file, model, language
                                       {"text": "..."}

Run it on the teacher's PC (see docs/servidor-local.md):

    python scripts/whisper_server.py --host 0.0.0.0 --port 8000

The model loads at startup and stays loaded: the first question of a class must
not pay for 50 seconds of loading.

Deliberately without `from __future__ import annotations`: FastAPI resolves a
handler's annotations at import time, and deferred ones are strings it cannot
turn back into types.
"""

import argparse
import asyncio
import io
import logging
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("whisper-aula")

DEFAULT_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
DEFAULT_PORT = 8000
# int8 weights with float16 arithmetic: half the memory of float16, and no
# audible loss on classroom speech.
DEFAULT_COMPUTE_TYPE = "int8_float16"
SAMPLE_RATE = 16_000

# Declared once: FastAPI reads a handler's parameters from its defaults, and
# ruff rejects those calls written inline (B008).
UPLOADED_FILE = File(...)
FORM_MODEL = Form(default="")
FORM_LANGUAGE = Form(default="")
FORM_RESPONSE_FORMAT = Form(default="json")
FORM_PROMPT = Form(default="")


def add_cuda_libraries_to_path() -> None:
    """Let ctranslate2 find cuBLAS and cuDNN when they come from pip wheels.

    The NVIDIA wheels drop their DLLs inside site-packages rather than on PATH,
    and ctranslate2 loads them by name.
    """
    for package in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
        for root in {Path(sys.prefix), Path(sys.base_prefix)}:
            folder = root / "Lib" / "site-packages" / package
            if folder.is_dir():
                os.add_dll_directory(str(folder))
                os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ['PATH']}"


def pcm_from_wav(raw: bytes) -> np.ndarray:
    """The application sends 16 kHz mono WAV; this is all it takes to read it."""
    with wave.open(io.BytesIO(raw), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("se esperaba WAV mono de 16 bits")
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    audio = samples.astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:
        # Linear resampling is enough: this only happens for a stray client.
        target = int(audio.size * SAMPLE_RATE / rate)
        audio = np.interp(
            np.linspace(0, audio.size - 1, target, dtype=np.float32),
            np.arange(audio.size, dtype=np.float32),
            audio,
        ).astype(np.float32)
    return audio


def transcribe_audio(model, audio: np.ndarray, language: str, prompt: str) -> str:
    segments, _info = model.transcribe(
        audio,
        language=language or None,
        # One beam: the fastest, and classroom speech does not need more.
        beam_size=1,
        vad_filter=False,
        initial_prompt=prompt or None,
    )
    return "".join(segment.text for segment in segments).strip()


def build_app(model_id: str, device: str, compute_type: str, default_language: str) -> FastAPI:
    from faster_whisper import WhisperModel

    logger.info("Cargando %s en %s (%s)…", model_id, device, compute_type)
    started = time.monotonic()
    model = WhisperModel(model_id, device=device, compute_type=compute_type)
    logger.info("Modelo listo en %.1f s", time.monotonic() - started)

    app = FastAPI(title="Whisper del aula", docs_url=None, redoc_url=None)
    # One at a time: two transcriptions at once would double the memory a class
    # has no spare gigabytes for.
    lock = asyncio.Lock()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "model": model_id, "device": device}

    @app.get("/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": model_id, "object": "model"}]}

    @app.post("/v1/audio/transcriptions")
    async def transcribe(
        file: UploadFile = UPLOADED_FILE,
        model_name: str = FORM_MODEL,  # only one model is loaded
        language: str = FORM_LANGUAGE,
        response_format: str = FORM_RESPONSE_FORMAT,  # always json
        prompt: str = FORM_PROMPT,
    ) -> dict:
        raw = await file.read()
        try:
            audio = pcm_from_wav(raw)
        except Exception as exc:  # a bad upload is the caller's fault
            raise HTTPException(status_code=400, detail=f"Audio ilegible: {exc}") from exc

        started = time.monotonic()
        async with lock:
            text = await asyncio.to_thread(
                transcribe_audio, model, audio, language or default_language, prompt
            )
        logger.info(
            "%.1f s de audio transcritos en %.2f s",
            audio.size / SAMPLE_RATE,
            time.monotonic() - started,
        )
        return {"text": text}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="whisper_server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu", "auto"])
    parser.add_argument("--compute-type", default=DEFAULT_COMPUTE_TYPE)
    parser.add_argument("--language", default="es")
    arguments = parser.parse_args()

    add_cuda_libraries_to_path()
    import uvicorn

    app = build_app(arguments.model, arguments.device, arguments.compute_type, arguments.language)
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
