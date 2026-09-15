"""A class's conversation on the teacher's own server (D-14).

The cloud path hands the whole turn to one Realtime model. This one splits it
across three services the teacher runs at home, reached over Tailscale:

    question audio --faster-whisper (speaches)--> text
    text + history --Qwen on Ollama--> answer, streamed as text
    each sentence --Kokoro--> 24 kHz PCM, streamed

Nothing here is billed per token. What it costs is latency: the question must
end and be transcribed before the model starts, where the Realtime API
listened while it was being asked.

`LocalConversationSession` speaks the same language as `ManagedRealtimeSession`
-- the same methods, the same normalised events -- so the spoken turn, the
interruption and the privacy rules in session/turn.py work unchanged over
either. The server VAD that ended a question in the cloud is replaced by a
local one: Silero, the voice filter the wake word detector already ships.

Runs on the backend's event loop. Audio arrives from the PortAudio thread, so
`append_audio` is safe to call from any thread and hands events to the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import threading
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from ..audio.listener import frame_level
from . import events
from .audio import MICROPHONE_RATE, REALTIME_RATE
from .session import ConnectionChanged, ConnectionState, RealtimeUnavailable

logger = logging.getLogger(__name__)

# Silero's probability above which a frame counts as speech.
SPEECH_PROBABILITY = 0.5
# Without Silero, a frame this loud counts as speech (about -39 dBFS).
ENERGY_SPEECH_LEVEL = 0.35
# Earlier exchanges sent back to the model. They cost nothing locally; the
# limit keeps the prompt, and so the wait for the first word, short.
HISTORY_MESSAGES = 12
# How often an unreachable server is tried again during a class.
RETRY_SECONDS = 5.0
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0
# Every service is slow the first time and fast afterwards: loading a 27B
# model from disk took 72 s and its first prompt two minutes more (13/09/2026),
# and faster-whisper's first transcription took 13.8 s against 0.18 s once warm
# (15/09/2026). The class warms all three as it starts, so no question pays it.
WARM_UP_TIMEOUT = 300.0
#: Half a second of silence, enough to make Whisper do a real transcription.
WARM_UP_SILENCE_MS = 500
WARM_UP_SENTENCE = "Hola."

# The context and the answer ceiling live in the model's Modelfile
# (docs/ollama/Modelfile.aula), not in each request: a request that set
# num_ctx would reload a model another application keeps with a larger one.
LLM_KEEP_ALIVE = "60m"

# A sentence ends at one of these followed by a space, or at a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?…;:])\s+|\n+")
# Markdown a voice would read aloud.
_UNSPOKEN = re.compile(r"[*#_`>|]")


@dataclass(frozen=True)
class LocalConfig:
    stt_url: str
    stt_model: str
    llm_url: str
    llm_model: str
    tts_url: str
    tts_voice: str
    instructions: str
    language: str = "es"
    silence_ms: int = 2000

    @classmethod
    def from_settings(cls, settings: Any, instructions: str) -> LocalConfig:
        return cls(
            stt_url=settings.local_stt_url.strip().rstrip("/"),
            stt_model=settings.local_stt_model,
            llm_url=settings.local_llm_url.strip().rstrip("/"),
            llm_model=settings.local_llm_model,
            tts_url=settings.local_tts_url.strip().rstrip("/"),
            tts_voice=settings.local_tts_voice,
            instructions=instructions,
            silence_ms=settings.turn_silence_ms,
        )

    def missing(self) -> list[str]:
        names = {
            "transcripción": self.stt_url,
            "modelo de lenguaje": self.llm_url,
            "voz": self.tts_url,
        }
        return [name for name, url in names.items() if not url]


class ServerUnreachable(RuntimeError):
    """A service did not answer: the network, Tailscale or the PC is down."""


class ServiceFailed(RuntimeError):
    """A service answered with an error, said in words a teacher can act on."""


def _raise_for_status(response: httpx.Response, service: str) -> None:
    """An HTTP status turned into something worth reading in a classroom.

    "Server error '500 Internal Server Error' for url ..." told the teacher
    nothing; running out of video memory, which is what a 500 from the local
    server usually is, deserves to be named.
    """
    if response.status_code < 400:
        return
    if response.status_code >= 500:
        detail = (
            "Suele ser falta de memoria de vídeo: comprueba que caben el modelo "
            "de lenguaje, la transcripción y la voz en la tarjeta."
        )
    else:
        detail = "La petición fue rechazada."
    raise ServiceFailed(
        f"El servidor local {service} respondió con un error "
        f"(HTTP {response.status_code}). {detail}"
    )


# -- the three services ----------------------------------------------------------


class LocalServices(Protocol):
    async def check(self) -> None:
        """Raise RealtimeUnavailable if the server cannot hold a class."""

    async def warm_up(self) -> None:
        """Wake all three services before the first question needs them."""

    async def transcribe(self, pcm16k: np.ndarray) -> str: ...

    def chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...

    def speak(self, text: str) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


def wav_bytes(pcm16k: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(MICROPHONE_RATE)
        wav.writeframes(np.asarray(pcm16k, dtype="<i2").tobytes())
    return buffer.getvalue()


class HttpLocalServices:
    """speaches, Ollama and Kokoro-FastAPI over HTTP."""

    def __init__(self, config: LocalConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        )

    async def _get(self, url: str) -> httpx.Response:
        try:
            return await self._client.get(url)
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"{url}: {exc.__class__.__name__}") from exc

    async def check(self) -> None:
        config = self._config
        missing = config.missing()
        if missing:
            names = missing[0] if len(missing) == 1 else (
                ", ".join(missing[:-1]) + " y " + missing[-1]
            )
            raise RealtimeUnavailable(
                f"Falta en Configuración la dirección del servidor local para: {names}.",
                fatal=True,
            )
        try:
            await self._get(config.stt_url + "/health")
            await self._get(config.tts_url + "/health")
            tags = await self._get(config.llm_url + "/api/tags")
        except ServerUnreachable as exc:
            raise RealtimeUnavailable(
                f"No se alcanza el servidor local ({exc}). Comprueba que el PC está "
                "encendido y que Tailscale está conectado en los dos equipos."
            ) from exc
        try:
            names = {model.get("name", "") for model in tags.json().get("models", [])}
        except ValueError:
            names = set()
        wanted = config.llm_model
        if names and wanted not in names and f"{wanted}:latest" not in names:
            raise RealtimeUnavailable(
                f"Ollama no tiene el modelo «{wanted}». Descárgalo en el servidor con "
                f"«ollama pull {wanted}».",
                fatal=True,
            )

    async def warm_up(self) -> None:
        # The model first, which is the slowest to load, and then a real
        # transcription and a real sentence of speech: loading is not enough,
        # each one is slow again on its first piece of work.
        await self._client.post(
            self._config.llm_url + "/api/generate",
            json={
                "model": self._config.llm_model,
                "keep_alive": LLM_KEEP_ALIVE,
            },
            timeout=WARM_UP_TIMEOUT,
        )
        silence = np.zeros(MICROPHONE_RATE * WARM_UP_SILENCE_MS // 1000, dtype=np.int16)
        await self.transcribe(silence)
        async for _chunk in self.speak(WARM_UP_SENTENCE):
            pass

    async def transcribe(self, pcm16k: np.ndarray) -> str:
        config = self._config
        try:
            response = await self._client.post(
                config.stt_url + "/v1/audio/transcriptions",
                files={"file": ("pregunta.wav", wav_bytes(pcm16k), "audio/wav")},
                data={
                    "model": config.stt_model,
                    "language": config.language,
                    "response_format": "json",
                },
            )
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"transcripción: {exc.__class__.__name__}") from exc
        _raise_for_status(response, "de transcripción")
        return str(response.json().get("text", "")).strip()

    async def chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        config = self._config
        body = {
            "model": config.llm_model,
            "messages": messages,
            "stream": True,
            # A classroom question needs no deliberation, and Qwen's thinking
            # would be seconds of silence before the first word.
            "think": False,
            "keep_alive": LLM_KEEP_ALIVE,
        }
        try:
            async with self._client.stream(
                "POST", config.llm_url + "/api/chat", json=body
            ) as response:
                _raise_for_status(response, "del modelo de lenguaje")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    text = (chunk.get("message") or {}).get("content", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        return
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise
            raise ServerUnreachable(f"modelo de lenguaje: {exc.__class__.__name__}") from exc

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        config = self._config
        body = {
            "model": "kokoro",
            "input": text,
            "voice": config.tts_voice,
            "response_format": "pcm",
            "stream": True,
        }
        try:
            async with self._client.stream(
                "POST", config.tts_url + "/v1/audio/speech", json=body
            ) as response:
                _raise_for_status(response, "de voz")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                raise
            raise ServerUnreachable(f"voz: {exc.__class__.__name__}") from exc

    async def close(self) -> None:
        await self._client.aclose()


async def warm_up_local_server(
    settings: Any, instructions: str, services: LocalServices | None = None
) -> bool:
    """Wake the teacher's server, if that is where this class would be answered.

    Called when the application starts and whenever the settings point it
    somewhere new, so the ten seconds the three services need are spent while
    the teacher is still setting up rather than at the first question.
    """
    config = LocalConfig.from_settings(settings, instructions)
    if config.missing():
        return False
    services = services or HttpLocalServices(config)
    try:
        await services.check()
        await services.warm_up()
    except Exception as exc:  # noqa: BLE001 - a class can still start and say why
        logger.info("No se pudo preparar el servidor local todavía: %s", exc)
        return False
    finally:
        with contextlib.suppress(Exception):
            await services.close()
    return True


# -- where a question ends -------------------------------------------------------


class SpeechGate(Protocol):
    def probability(self, frame: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class EnergySpeechGate:
    """Loudness as speech: the fallback when Silero is not installed."""

    def probability(self, frame: np.ndarray) -> float:
        return 1.0 if frame_level(frame) >= ENERGY_SPEECH_LEVEL else 0.0

    def reset(self) -> None:
        pass


class SileroSpeechGate:
    """Silero VAD, from the copy that ships with the wake word models."""

    def __init__(self, model_path: Path) -> None:
        from openwakeword.vad import VAD

        self._vad = VAD(model_path=str(model_path))

    def probability(self, frame: np.ndarray) -> float:
        return float(self._vad.predict(frame))

    def reset(self) -> None:
        self._vad.reset_states()


def create_speech_gate(models_dir: Path | None) -> SpeechGate:
    if models_dir is not None:
        from ..audio.wakeword import BASE_MODELS_SUBFOLDER, VAD_MODEL

        path = models_dir / BASE_MODELS_SUBFOLDER / VAD_MODEL
        if path.exists():
            try:
                return SileroSpeechGate(path)
            except Exception:  # noqa: BLE001 - the energy gate still works
                logger.exception("No se pudo cargar Silero; se usa el nivel de sonido.")
    return EnergySpeechGate()


# -- the session -----------------------------------------------------------------


Listener = Callable[[events.ServerEvent], None]


@dataclass
class _Spoken:
    """One sentence of an answer and how many samples of it were played."""

    text: str
    samples: int = 0


def sentences(buffer: str) -> tuple[list[str], str]:
    """Split finished sentences off `buffer`; return them and the rest."""
    parts = _SENTENCE_END.split(buffer)
    finished = [part.strip() for part in parts[:-1] if part.strip()]
    return finished, parts[-1]


class LocalConversationSession:
    """The class-long conversation, on the teacher's server."""

    def __init__(
        self,
        config: LocalConfig,
        services: LocalServices | None = None,
        speech_gate: SpeechGate | None = None,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._config = config
        self._services = services or HttpLocalServices(config)
        self._gate = speech_gate or EnergySpeechGate()
        self._retry_seconds = retry_seconds

        self.state = ConnectionState.DISCONNECTED
        self.last_error: str | None = None
        self._listeners: list[Listener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._supervisor: asyncio.Task | None = None
        self._warm_up: asyncio.Task | None = None
        self._stopping = False

        # Touched from the audio thread.
        self._lock = threading.Lock()
        self._frames: list[np.ndarray] = []
        self._heard_speech = False
        self._silence_ms = 0.0
        self._question_closed = False

        self._turns = 0
        self._history: list[dict[str, str]] = []
        self._answer: asyncio.Task | None = None
        self._response_id: str | None = None
        self._assistant_item: str | None = None
        self._spoken: list[_Spoken] = []

    # -- listeners ---------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, event: events.ServerEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                logger.exception("Un oyente de la sesión local falló.")

    def _emit_soon(self, event: events.ServerEvent) -> None:
        """Deliver on the loop, never inside the caller: the turn controller
        expects events to arrive the way the Realtime socket delivers them."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._emit, event)

    def _set_state(self, state: ConnectionState, reason: str | None = None) -> None:
        if state is self.state and reason is None:
            return
        self.state = state
        if reason:
            self.last_error = reason
        self._emit(ConnectionChanged("session", state, reason))

    # -- lifecycle ---------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.state is ConnectionState.READY

    async def start(self, keep_trying: bool = False) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._set_state(ConnectionState.CONNECTING)
        try:
            await self._services.check()
        except RealtimeUnavailable as exc:
            if keep_trying and not exc.fatal:
                self._set_state(ConnectionState.RECONNECTING, str(exc))
                self._supervisor = asyncio.create_task(self._retry(), name="local-supervisor")
                return
            self._set_state(
                ConnectionState.FAILED if exc.fatal else ConnectionState.DISCONNECTED, str(exc)
            )
            raise
        self._set_state(ConnectionState.READY)
        self._warm_up_soon()

    async def _retry(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._retry_seconds)
            try:
                await self._services.check()
            except RealtimeUnavailable as exc:
                if exc.fatal:
                    self._set_state(ConnectionState.FAILED, str(exc))
                    return
                self._set_state(ConnectionState.RECONNECTING, str(exc))
                continue
            self._set_state(ConnectionState.READY)
            self._warm_up_soon()
            return

    def _warm_up_soon(self) -> None:
        async def warm_up() -> None:
            try:
                await self._services.warm_up()
            except Exception as exc:  # noqa: BLE001 - the first question will say why
                logger.warning("No se pudo precargar el modelo local: %s", exc)

        self._warm_up = asyncio.ensure_future(warm_up())

    def _lost(self, why: str) -> None:
        if self._stopping:
            return
        self._set_state(ConnectionState.RECONNECTING, why)
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.ensure_future(self._retry())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._supervisor, self._answer, self._warm_up):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._supervisor = self._answer = self._warm_up = None
        with contextlib.suppress(Exception):
            await self._services.close()
        self._set_state(ConnectionState.DISCONNECTED)

    # -- the question, from the audio thread -------------------------------

    def append_audio(self, pcm16k: np.ndarray) -> bool:
        if not self.ready:
            return False
        frame = np.asarray(pcm16k, dtype=np.int16)
        started = ended = False
        with self._lock:
            if self._question_closed:
                return True
            self._frames.append(frame.copy())
            if self._gate.probability(frame) >= SPEECH_PROBABILITY:
                self._silence_ms = 0.0
                if not self._heard_speech:
                    self._heard_speech = started = True
            elif self._heard_speech:
                self._silence_ms += frame.size / MICROPHONE_RATE * 1000
                if self._silence_ms >= self._config.silence_ms:
                    self._question_closed = ended = True
        if started:
            self._emit_soon(events.SpeechStarted("input_audio_buffer.speech_started",
                                                 f"user_{self._turns}"))
        if ended:
            self._close_question_soon()
        return True

    def _close_question_soon(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._question_ended)

    def _take_question(self) -> np.ndarray:
        with self._lock:
            frames, self._frames = self._frames, []
            self._heard_speech = False
            self._silence_ms = 0.0
        self._gate.reset()
        if not frames:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(frames)

    def _question_ended(self) -> None:
        if not self.ready:
            return
        user_item = f"user_{self._turns}"
        self._emit(events.SpeechStopped("input_audio_buffer.speech_stopped", user_item))
        pcm = self._take_question()
        turn = self._turns
        self._turns += 1
        self._answer = asyncio.ensure_future(self._respond(turn, user_item, pcm))

    def clear_audio(self) -> bool:
        with self._lock:
            self._frames = []
            self._heard_speech = False
            self._silence_ms = 0.0
            self._question_closed = False
        self._gate.reset()
        return self.ready

    def commit_audio(self) -> bool:
        """End the question now, as the server does on a commit."""
        with self._lock:
            if self._question_closed:
                return True
            self._question_closed = True
        self._close_question_soon()
        return self.ready

    def create_response(self) -> bool:
        # Every question ends in an answer on its own, as create_response: true
        # does in the cloud, so there is nothing more to ask for.
        return self.ready

    def end_turn(self) -> None:
        with self._lock:
            self._frames = []
            self._heard_speech = False
            self._silence_ms = 0.0

    # -- the answer --------------------------------------------------------

    async def _respond(self, turn: int, user_item: str, pcm: np.ndarray) -> None:
        response_id, assistant_item = f"resp_{turn}", f"asst_{turn}"
        # Until the answer starts there is nothing of it to cancel or truncate.
        self._response_id = self._assistant_item = None
        try:
            question = await self._services.transcribe(pcm) if pcm.size else ""
            self._emit(events.InputTranscript(
                "conversation.item.input_audio_transcription.completed",
                user_item, question, final=True,
            ))
            # The pre-roll often brings "Oye Chat" alone; the instructions
            # answer that with "¿Sí?", so the model must see it was called.
            self._remember("user", question or "Oye Chat")

            self._response_id, self._assistant_item = response_id, assistant_item
            self._spoken = []
            self._emit(events.ResponseStarted("response.created", response_id))
            answer = await self._speak_answer(response_id, assistant_item)
            self._remember("assistant", answer)
            self._emit(events.OutputTranscript(
                "response.output_audio_transcript.done", response_id, assistant_item,
                answer, final=True,
            ))
            self._emit(events.ResponseDone(
                "response.done", response_id, "completed", None, (user_item, assistant_item)
            ))
        except asyncio.CancelledError:
            raise
        except ServerUnreachable as exc:
            logger.warning("Servidor local inalcanzable: %s", exc)
            self._lost(f"Se perdió el servidor local ({exc}).")
        except ServiceFailed as exc:
            logger.warning("El servidor local falló: %s", exc)
            self._emit(events.ApiError("error", "local_error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - one failed answer, not the class
            logger.exception("La respuesta local falló.")
            self._emit(events.ApiError("error", "local_error", f"Servidor local: {exc}"))
        finally:
            with self._lock:
                self._question_closed = False

    async def _speak_answer(self, response_id: str, item: str) -> str:
        """Stream the model's words into sentences, and each sentence into sound.

        The model keeps writing while a sentence is being spoken, so the voice
        starts after the first sentence, not after the whole answer.
        """
        messages = [{"role": "system", "content": self._config.instructions}, *self._history]
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        full: list[str] = []

        async def write() -> None:
            pending = ""
            try:
                async for piece in self._services.chat(messages):
                    piece = _UNSPOKEN.sub("", piece)
                    full.append(piece)
                    finished, pending = sentences(pending + piece)
                    for sentence in finished:
                        await queue.put(sentence)
                if pending.strip():
                    await queue.put(pending.strip())
            finally:
                await queue.put(None)

        writer = asyncio.ensure_future(write())
        try:
            while (sentence := await queue.get()) is not None:
                spoken = _Spoken(sentence)
                self._spoken.append(spoken)
                self._emit(events.OutputTranscript(
                    "response.output_audio_transcript.delta", response_id, item,
                    sentence + " ", final=False,
                ))
                carry = b""
                async for chunk in self._services.speak(sentence):
                    data = carry + chunk
                    whole = len(data) - len(data) % 2
                    carry = data[whole:]
                    if not whole:
                        continue
                    samples = np.frombuffer(data[:whole], dtype="<i2").astype(np.int16)
                    spoken.samples += samples.size
                    self._emit(events.AudioDelta(
                        "response.output_audio.delta", response_id, item, samples
                    ))
            await writer  # surfaces an error from the model
        finally:
            if not writer.done():
                writer.cancel()
        return "".join(full).strip()

    def cancel_response(self) -> bool:
        task, response_id = self._answer, self._response_id
        if task is None or task.done():
            return False
        task.cancel()
        with self._lock:
            self._question_closed = False
        if response_id is not None:
            # After the caller has recorded the cut, as the API's own
            # response.done "cancelled" arrives after the cancel was sent.
            self._emit_soon(events.ResponseDone(
                "response.done", response_id, "cancelled", None,
                tuple(i for i in (self._assistant_item,) if i),
            ))
        return True

    def truncate(self, item_id: str, audio_end_ms: int) -> bool:
        """Keep in the history only what was heard of a cut answer."""
        if item_id != self._assistant_item or not self._spoken:
            return False
        remaining = max(0, int(audio_end_ms)) * REALTIME_RATE // 1000
        heard: list[str] = []
        for spoken in self._spoken:
            if remaining <= 0:
                break
            if spoken.samples and remaining < spoken.samples:
                words = spoken.text.split()
                keep = max(1, len(words) * remaining // spoken.samples)
                heard.append(" ".join(words[:keep]) + "…")
                break
            heard.append(spoken.text)
            remaining -= spoken.samples
        if heard:
            self._remember("assistant", " ".join(heard))
        self._spoken = []
        return True

    def _remember(self, role: str, text: str) -> None:
        self._history.append({"role": role, "content": text})
        del self._history[: max(0, len(self._history) - HISTORY_MESSAGES)]
