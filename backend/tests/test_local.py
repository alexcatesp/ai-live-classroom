"""The conversation on the teacher's own server (D-14), with no server at all."""

from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np
import pytest
from test_turn import FIRE, speech, wait_until

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import ScriptedWakeWordDetector
from aiclassroom.config.settings import AiProvider
from aiclassroom.diagnostics.checks import CheckStatus
from aiclassroom.diagnostics.runner import DiagnosticsRunner
from aiclassroom.realtime import events
from aiclassroom.realtime.local import (
    EnergySpeechGate,
    HttpLocalServices,
    LocalConfig,
    LocalConversationSession,
    sentences,
)
from aiclassroom.realtime.session import ConnectionState, RealtimeUnavailable
from aiclassroom.session.controller import SessionController
from aiclassroom.session.state import Event, SessionStateMachine, State
from aiclassroom.session.turn import TurnController

CONFIG = LocalConfig(
    stt_url="http://casa:8000",
    stt_model="whisper",
    llm_url="http://casa:11434",
    llm_model="qwen3:14b",
    tts_url="http://casa:8880",
    tts_voice="ef_dora",
    instructions="Eres un asistente.",
    silence_ms=400,
)


class FakeServices:
    """The three services, scripted."""

    def __init__(
        self,
        transcript: str = "¿Qué es HTML?",
        answer: tuple[str, ...] = ("HTML es ", "un lenguaje. ", "Sirve para ", "marcar."),
        chunk_bytes: int = 4_801,  # odd on purpose: a sample torn across chunks
        chunks_per_sentence: int = 3,
        delay: float = 0.0,
        unavailable: RealtimeUnavailable | None = None,
    ) -> None:
        self.transcript = transcript
        self.answer = answer
        self.chunk_bytes = chunk_bytes
        self.chunks_per_sentence = chunks_per_sentence
        self.delay = delay
        self.unavailable = unavailable
        self.transcribed: list[np.ndarray] = []
        self.conversations: list[list[dict]] = []
        self.spoken: list[str] = []
        self.closed = False

    async def check(self) -> None:
        if self.unavailable is not None:
            raise self.unavailable

    async def transcribe(self, pcm16k):
        self.transcribed.append(pcm16k)
        return self.transcript

    async def chat(self, messages):
        self.conversations.append([dict(m) for m in messages])
        for piece in self.answer:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield piece

    async def speak(self, text):
        self.spoken.append(text)
        for index in range(self.chunks_per_sentence):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield bytes([index + 1, 0]) * (self.chunk_bytes // 2) + (
                b"\x00" if self.chunk_bytes % 2 else b""
            )

    async def close(self) -> None:
        self.closed = True


class Collector:
    def __init__(self) -> None:
        self.events: list[events.ServerEvent] = []

    def __call__(self, event: events.ServerEvent) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]


def frames(count: int, value: int) -> list[np.ndarray]:
    return [np.full(FRAME_SAMPLES, value, dtype=np.int16) for _ in range(count)]


# -- pieces --------------------------------------------------------------------


def test_an_answer_is_cut_into_sentences_as_it_is_written():
    assert sentences("Hola. Esto es") == (["Hola."], "Esto es")
    assert sentences("¿Qué? ¡Nada! Bien;") == (["¿Qué?", "¡Nada!"], "Bien;")
    assert sentences("Uno\nDos") == (["Uno"], "Dos")
    assert sentences("3.5 metros") == ([], "3.5 metros")


def test_the_configuration_says_which_address_is_missing():
    config = LocalConfig("", "m", "http://llm", "q", "", "v", "i")
    assert config.missing() == ["transcripción", "voz"]


# -- the HTTP requests ---------------------------------------------------------


def http_services(handler) -> HttpLocalServices:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpLocalServices(CONFIG, client=client)


async def test_the_question_goes_to_speaches_as_a_wav_in_spanish():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " ¿Qué es HTML? "})

    services = http_services(handler)
    text = await services.transcribe(np.zeros(1600, dtype=np.int16))

    assert text == "¿Qué es HTML?"
    assert seen["url"] == "http://casa:8000/v1/audio/transcriptions"
    assert b"RIFF" in seen["body"] and b'name="model"' in seen["body"]
    assert b"whisper" in seen["body"] and b"es" in seen["body"]


async def test_qwen_is_asked_not_to_think_and_its_stream_is_read():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        lines = [
            {"message": {"content": "Hola"}, "done": False},
            {"message": {"content": ", clase."}, "done": False},
            {"message": {"content": ""}, "done": True},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines))

    services = http_services(handler)
    pieces = [piece async for piece in services.chat([{"role": "user", "content": "hola"}])]

    assert pieces == ["Hola", ", clase."]
    assert seen["url"] == "http://casa:11434/api/chat"
    assert seen["body"]["think"] is False
    assert seen["body"]["stream"] is True
    assert seen["body"]["model"] == "qwen3:14b"


async def test_kokoro_is_asked_for_raw_pcm_in_the_chosen_voice():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x01\x00\x02\x00")

    services = http_services(handler)
    audio = b"".join([chunk async for chunk in services.speak("Hola.")])

    assert audio == b"\x01\x00\x02\x00"
    assert seen["url"] == "http://casa:8880/v1/audio/speech"
    assert seen["body"]["response_format"] == "pcm"
    assert seen["body"]["voice"] == "ef_dora"


async def test_a_server_that_does_not_answer_can_be_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta", request=request)

    with pytest.raises(RealtimeUnavailable) as failure:
        await http_services(handler).check()
    assert failure.value.fatal is False
    assert "Tailscale" in str(failure.value)


async def test_a_missing_model_is_named_and_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
        return httpx.Response(200, json={"status": "ok"})

    with pytest.raises(RealtimeUnavailable) as failure:
        await http_services(handler).check()
    assert failure.value.fatal is True
    assert "ollama pull qwen3:14b" in str(failure.value)


async def test_an_address_left_empty_stops_the_class_with_the_reason():
    services = HttpLocalServices(LocalConfig("", "m", "", "q", "", "v", "i"))
    with pytest.raises(RealtimeUnavailable) as failure:
        await services.check()
    assert failure.value.fatal is True
    assert "Configuración" in str(failure.value)


# -- the session ---------------------------------------------------------------


async def open_session(services: FakeServices) -> tuple[LocalConversationSession, Collector]:
    session = LocalConversationSession(CONFIG, services=services, speech_gate=EnergySpeechGate())
    received = Collector()
    session.subscribe(received)
    await session.start()
    return session, received


async def test_a_question_that_ends_in_silence_is_answered_sentence_by_sentence():
    services = FakeServices()
    session, received = await open_session(services)
    try:
        for frame in frames(10, 1000) + frames(6, 0):  # 0.8 s of speech, 0.48 s of silence
            session.append_audio(frame)
        await wait_until(lambda: received.of(events.ResponseDone))

        kinds = [type(event) for event in received.events]
        assert kinds.index(events.SpeechStarted) < kinds.index(events.SpeechStopped)
        assert kinds.index(events.SpeechStopped) < kinds.index(events.InputTranscript)
        assert kinds.index(events.ResponseStarted) < kinds.index(events.AudioDelta)
        assert received.of(events.ResponseDone)[0].status == "completed"
        assert received.of(events.InputTranscript)[0].text == "¿Qué es HTML?"

        assert services.spoken == ["HTML es un lenguaje.", "Sirve para marcar."]
        audio = np.concatenate([delta.pcm for delta in received.of(events.AudioDelta)])
        # A sample torn across chunks is joined, not lost; a lone final byte goes.
        assert audio.size == 2 * (3 * 4_801 // 2)
        final = received.of(events.OutputTranscript)[-1]
        assert final.text == "HTML es un lenguaje. Sirve para marcar."

        # The whole question, and nothing said after it ended, was transcribed.
        # 10 frames of speech and the 5 of silence (400 ms) that ended it.
        assert services.transcribed[0].size == 15 * FRAME_SAMPLES
    finally:
        await session.stop()


async def test_the_question_keeps_listening_through_a_short_pause():
    services = FakeServices()
    session, received = await open_session(services)
    try:
        for frame in frames(5, 1000) + frames(3, 0) + frames(5, 1000):  # 240 ms pause
            session.append_audio(frame)
        await asyncio.sleep(0.1)
        assert received.of(events.SpeechStopped) == []
    finally:
        await session.stop()


async def test_cancelling_confirms_afterwards_and_keeps_only_what_was_heard():
    services = FakeServices(chunks_per_sentence=6, delay=0.02, chunk_bytes=4_800)
    session, received = await open_session(services)
    try:
        for frame in frames(10, 1000) + frames(6, 0):
            session.append_audio(frame)
        await wait_until(lambda: len(received.of(events.AudioDelta)) >= 7)

        assert session.cancel_response()
        # Not inside the call: the turn records the cut before the confirmation.
        assert received.of(events.ResponseDone) == []
        # 2,400 samples a chunk at 24 kHz: 6 chunks (600 ms) fill the first
        # sentence, and 50 ms more is a word of the second.
        assert session.truncate("asst_0", 650)
        await wait_until(lambda: received.of(events.ResponseDone))
        assert received.of(events.ResponseDone)[0].status == "cancelled"

        for frame in frames(10, 1000) + frames(6, 0):
            session.append_audio(frame)
        await wait_until(lambda: len(services.conversations) == 2)
        history = services.conversations[1]
        assert history[2] == {"role": "assistant", "content": "HTML es un lenguaje. Sirve…"}
    finally:
        await session.stop()


async def test_an_unreachable_server_is_retried_while_the_class_listens():
    services = FakeServices(unavailable=RealtimeUnavailable("apagado"))
    session = LocalConversationSession(
        CONFIG, services=services, speech_gate=EnergySpeechGate(), retry_seconds=0.05
    )
    await session.start(keep_trying=True)
    try:
        assert session.state is ConnectionState.RECONNECTING
        assert session.append_audio(frames(1, 1000)[0]) is False  # nothing goes anywhere

        services.unavailable = None
        await wait_until(lambda: session.ready)
    finally:
        await session.stop()
    assert services.closed


# -- inside a class ------------------------------------------------------------


class LocalClass:
    def __init__(self, store, services: FakeServices, scores: list[float]) -> None:
        store.save(store.load().model_copy(update={
            "ai_provider": AiProvider.LOCAL, "turn_silence_ms": 400,
        }))
        self.engine = FakeAudioEngine()
        self.machine = SessionStateMachine()
        self.controller = SessionController(
            store=store,
            machine=self.machine,
            engine_factory=lambda _settings: self.engine,
            detector_factory=lambda _settings: ScriptedWakeWordDetector(
                scores=scores, refractory_seconds=0.1
            ),
        )
        self.services = services

        def local(settings, instructions, _models_dir):
            return LocalConversationSession(
                LocalConfig.from_settings(settings, instructions),
                services=services, speech_gate=EnergySpeechGate(),
            )

        self.turns = TurnController(
            controller=self.controller, store=store,
            session_factory=lambda *_: pytest.fail("la nube no debe usarse"),
            local_session_factory=local,
        )

    async def start(self) -> None:
        await self.turns.open()
        self.controller.prepare()
        self.controller.start_class()

    async def stop(self) -> None:
        self.controller.stop()
        await self.turns.close()


async def test_a_class_on_the_local_server_needs_no_api_key(store):
    services = FakeServices()
    lesson = LocalClass(store, services, scores=[FIRE])
    await lesson.start()
    try:
        lesson.engine.feed(speech(1))
        await wait_until(lambda: lesson.machine.state is State.CAPTURING_REQUEST)
        lesson.engine.feed(speech(10))
        lesson.engine.feed(speech(6, value=0))
        await wait_until(lambda: lesson.machine.state is State.SPEAKING)
        await wait_until(lambda: lesson.turns._response_finished)
        lesson.engine.streams[-1].play_out()
        await wait_until(lambda: lesson.machine.state is State.PASSIVE_LISTENING)

        assert lesson.turns.last.outcome == "completada"
        assert lesson.turns.last.question == "¿Qué es HTML?"
        assert lesson.turns.last.answer == "HTML es un lenguaje. Sirve para marcar."
    finally:
        await lesson.stop()


async def test_passive_listening_sends_nothing_to_the_local_server_either(store):
    services = FakeServices()
    lesson = LocalClass(store, services, scores=[])
    await lesson.start()
    try:
        lesson.engine.feed(speech(200))
        await asyncio.sleep(0.2)
        assert services.transcribed == []
        assert services.conversations == []
    finally:
        await lesson.stop()


async def test_oye_chat_over_a_local_answer_opens_the_next_question(store):
    services = FakeServices(chunks_per_sentence=20, delay=0.02, chunk_bytes=4_800)
    scores = [FIRE] + [0.0] * 16 + [0.0] * 5 + [FIRE]
    lesson = LocalClass(store, services, scores=scores)
    await lesson.start()
    try:
        lesson.engine.feed(speech(1))
        await wait_until(lambda: lesson.machine.state is State.CAPTURING_REQUEST)
        lesson.engine.feed(speech(10))
        lesson.engine.feed(speech(6, value=0))
        await wait_until(lambda: lesson.machine.state is State.SPEAKING)

        lesson.engine.feed(speech(6))  # "Oye Chat" over the answer
        await wait_until(lambda: lesson.machine.state is State.CAPTURING_REQUEST)
        lesson.engine.feed(speech(10))
        lesson.engine.feed(speech(6, value=0))
        await wait_until(lambda: len(services.transcribed) == 2)
        await wait_until(lambda: lesson.machine.state is State.SPEAKING)

        assert not any(t.event is Event.TURN_FAILED for t in lesson.machine.history)
        assert State.INTERRUPTED in [t.target for t in lesson.machine.history]
    finally:
        await lesson.stop()


# -- diagnostics ---------------------------------------------------------------


async def test_the_diagnostic_checks_the_local_server_instead_of_openai(store):
    store.save(store.load().model_copy(update={"ai_provider": AiProvider.LOCAL}))
    down = FakeServices(unavailable=RealtimeUnavailable("apagado"))
    runner = DiagnosticsRunner(store, local_services_factory=lambda _config: down)

    report = await runner.run()

    ids = [result.id for result in report.results]
    assert "api_key" not in ids and "realtime" not in ids
    local = next(result for result in report.results if result.id == "local_server")
    assert local.status is CheckStatus.FAILED
    assert local.blocking
    assert down.closed
