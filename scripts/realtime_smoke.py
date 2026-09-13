#!/usr/bin/env python3
"""Ask the real Realtime API a recorded question, the way a class will (plan-fase-1, H1).

    set OPENAI_API_KEY=sk-...
    python scripts/realtime_smoke.py pregunta.wav --output respuesta.wav

The WAV is any sample rate, mono or stereo: a question recorded on a phone is
fine. It is streamed in 80 ms frames through the same session class the
application uses, followed by silence so the server notices the question has
ended. The answer is written to `--output` and the script prints:

* the transcript of the question and of the answer;
* **time to first audio**, from the end of the question to the first sound of
  the answer, minus the configured silence -- the number risk R-2 is about;
* the token usage the API reported (spec section 15).

It spends real money, a few cents per run. The key is read from the
environment, never from a file in the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from aiclassroom.realtime import events  # noqa: E402
from aiclassroom.realtime.audio import REALTIME_RATE, StreamingResampler  # noqa: E402
from aiclassroom.realtime.prompt import DEFAULT_INSTRUCTIONS  # noqa: E402
from aiclassroom.realtime.session import (  # noqa: E402
    ManagedRealtimeSession,
    RealtimeUnavailable,
)

FRAME = 1_280  # 80 ms at 16 kHz


def read_question(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as clip:
        rate, channels, width = clip.getframerate(), clip.getnchannels(), clip.getsampwidth()
        raw = clip.readframes(clip.getnframes())
    if width != 2:
        raise SystemExit("El WAV debe ser PCM de 16 bits.")
    samples = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return StreamingResampler(rate, 16_000).process(samples) if rate != 16_000 else samples


def relaxed_tls() -> ssl.SSLContext:
    """Risk R-8: Python 3.13 rejects some antivirus inspection roots."""
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


async def run(arguments: argparse.Namespace) -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Falta OPENAI_API_KEY en el entorno.", file=sys.stderr)
        return 1

    question = read_question(arguments.question)
    config = events.SessionConfig(
        model=arguments.model,
        instructions=DEFAULT_INSTRUCTIONS,
        voice=arguments.voice,
        silence_ms=arguments.silence_ms,
    )
    session = ManagedRealtimeSession(
        api_key=api_key,
        config=config,
        ssl_context=relaxed_tls() if arguments.relaxed_tls else None,
    )

    answer: list[np.ndarray] = []
    asked: list[str] = []
    said: list[str] = []
    done = asyncio.Event()
    first_audio_at: float | None = None
    usage = None

    def on_event(event: events.ServerEvent) -> None:
        nonlocal first_audio_at, usage
        if isinstance(event, events.InputTranscript) and event.final:
            asked.append(event.text)
        elif isinstance(event, events.AudioDelta):
            if first_audio_at is None:
                first_audio_at = time.monotonic()
            answer.append(event.pcm)
        elif isinstance(event, events.OutputTranscript) and event.final:
            said.append(event.text)
        elif isinstance(event, events.ResponseDone):
            usage = event.usage
            done.set()
        elif isinstance(event, events.ApiError):
            print(f"Error de la API: {event.message}", file=sys.stderr)

    session.subscribe(on_event)
    opened = time.monotonic()
    try:
        await session.start()
    except RealtimeUnavailable as exc:
        print(f"No se pudo abrir la sesión: {exc}", file=sys.stderr)
        return 1
    print(f"Sesión abierta en {time.monotonic() - opened:.2f} s.")

    # In real time, as a microphone would deliver it.
    for start in range(0, question.size, FRAME):
        session.append_audio(question[start : start + FRAME])
        await asyncio.sleep(0.08)
    question_ended = time.monotonic()
    silence = np.zeros(FRAME, dtype=np.int16)
    for _ in range(int((arguments.silence_ms / 1000 + 1.5) / 0.08)):
        if done.is_set() or first_audio_at is not None:
            break
        session.append_audio(silence)
        await asyncio.sleep(0.08)

    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except TimeoutError:
        print("La respuesta no terminó en 60 s.", file=sys.stderr)
    finally:
        await session.stop()

    print(f"\nPregunta: {' '.join(asked) or '(sin transcripción)'}")
    print(f"Respuesta: {' '.join(said) or '(sin transcripción)'}")
    if first_audio_at is not None:
        waited = first_audio_at - question_ended
        print(
            f"\nPrimer audio {waited:.2f} s después de terminar la pregunta, de los que "
            f"{arguments.silence_ms / 1000:.1f} s son el silencio configurado: "
            f"{max(0.0, waited - arguments.silence_ms / 1000):.2f} s de red y modelo."
        )
    if usage:
        print(f"Consumo: {usage}")

    if answer:
        pcm = np.concatenate(answer)
        with wave.open(str(arguments.output), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(REALTIME_RATE)
            out.writeframes(pcm.astype("<i2").tobytes())
        print(f"Respuesta guardada en {arguments.output} ({pcm.size / REALTIME_RATE:.1f} s).")
    return 0 if answer else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="WAV con la pregunta")
    parser.add_argument("--output", type=Path, default=Path("respuesta.wav"))
    parser.add_argument("--model", default="gpt-realtime-2")
    parser.add_argument("--voice", default="marin")
    parser.add_argument("--silence-ms", type=int, default=2000)
    parser.add_argument(
        "--relaxed-tls",
        action="store_true",
        help="tolera raíces de inspección HTTPS mal formadas con Python 3.13 (R-8)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
