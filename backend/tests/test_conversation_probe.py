"""One real question from the application, against the fake Realtime API."""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest
from fake_realtime import FakeRealtimeServer, Script

from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.realtime.probe import ConversationProbe, ProbeState, ProbeUnavailable
from aiclassroom.realtime.session import ManagedRealtimeSession

FRAME = 1_280


class TalkingEngine(FakeAudioEngine):
    """A microphone that speaks for as long as capture stays open."""

    def __init__(self) -> None:
        super().__init__()
        self.captures = 0
        self.frames_sent = 0
        self._stop = threading.Event()

    def start_capture(self, on_frame) -> None:
        super().start_capture(on_frame)
        self.captures += 1
        self._stop.clear()

        def talk() -> None:
            while not self._stop.is_set():
                on_frame(np.full(FRAME, 1000, dtype=np.int16))
                self.frames_sent += 1
                self._stop.wait(0.005)

        threading.Thread(target=talk, daemon=True).start()

    def stop_capture(self) -> None:
        self._stop.set()
        super().stop_capture()

    def play(self, samples, sample_rate=24_000) -> None:
        self.played.append((samples, sample_rate))


@pytest.fixture
def engine() -> TalkingEngine:
    return TalkingEngine()


def probe_for(store, server, engine, in_use=lambda: False, **kwargs) -> ConversationProbe:
    return ConversationProbe(
        store=store,
        engine_factory=lambda _settings: engine,
        microphone_in_use=in_use,
        session_factory=lambda key, config: ManagedRealtimeSession(
            api_key=key, config=config, url=server.url, reconnect_delays=(0.05,),
            connect_timeout=2.0,
        ),
        **kwargs,
    )


async def test_a_question_is_asked_and_answered_with_the_saved_key(store, engine):
    store.set_api_key("sk-guardada")
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)

        status = probe.status()
        assert status["state"] == ProbeState.DONE, status["error"]
        # The key saved in settings, not an environment variable.
        assert server.connections[0].headers["authorization"] == "Bearer sk-guardada"
        assert status["result"]["question"] == "¿Qué es HTML?"
        assert status["result"]["answer"].startswith("HTML es")
        assert status["result"]["first_audio_seconds"] is not None
        assert status["result"]["silence_seconds"] == 2.0
        assert status["can_play"] is True


async def test_the_microphone_closes_before_the_answer_arrives(store, engine):
    """Spec section 14: the answer is never recorded, nothing is sent while it plays."""
    store.set_api_key("sk-guardada")
    script = Script(audio_chunks=20, chunk_delay=0.02, answer_delay=0.2)
    async with FakeRealtimeServer(script) as server:
        probe = probe_for(store, server, engine)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)

        assert probe.status()["state"] == ProbeState.DONE
        assert engine.is_capturing is False
        connection = server.connections[0]
        during_answer = connection.received[connection.received_before_answer :]
        assert [e for e in during_answer if e["type"] == "input_audio_buffer.append"] == []


async def test_the_session_is_closed_after_the_test(store, engine):
    store.set_api_key("sk-guardada")
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)
        await asyncio.sleep(0.1)
        assert server._sockets == []


async def test_without_a_saved_key_the_test_says_where_to_add_it(store, engine):
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine)
        with pytest.raises(ProbeUnavailable, match="Configuración"):
            await probe.start()


async def test_the_test_does_not_take_the_microphone_from_a_class(store, engine):
    store.set_api_key("sk-guardada")
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine, in_use=lambda: True)
        with pytest.raises(ProbeUnavailable, match="Pausa"):
            await probe.start()


async def test_silence_ends_the_test_with_an_explanation(store):
    store.set_api_key("sk-guardada")
    quiet = FakeAudioEngine()  # never delivers a frame
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, quiet, wait_for_speech=0.2)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)
        assert probe.status()["state"] == ProbeState.FAILED
        assert "No se oyó" in probe.status()["error"]


async def test_a_rejected_key_is_reported(store, engine):
    store.set_api_key("sk-mala")
    async with FakeRealtimeServer(Script(reject_status=401)) as server:
        probe = probe_for(store, server, engine)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)
        assert probe.status()["state"] == ProbeState.FAILED
        assert "clave" in probe.status()["error"]


async def test_the_answer_can_be_played_back(store, engine):
    store.set_api_key("sk-guardada")
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine)
        await probe.start()
        await asyncio.wait_for(probe.wait(), 10)
        await probe.play()

        samples, rate = engine.played[-1]
        assert rate == 48_000
        # Five 100 ms chunks at 24 kHz, upsampled to 48 kHz.
        assert abs(samples.size - 24_000) <= 2


async def test_nothing_to_play_before_an_answer(store, engine):
    async with FakeRealtimeServer() as server:
        probe = probe_for(store, server, engine)
        with pytest.raises(ProbeUnavailable):
            await probe.play()
