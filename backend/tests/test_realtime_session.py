"""The class-long Realtime session (plan-fase-1, H1), against a fake API."""

from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest
from fake_realtime import FakeRealtimeServer, Script

from aiclassroom.realtime import events
from aiclassroom.realtime.audio import StreamingResampler, from_base64, to_base64
from aiclassroom.realtime.prompt import DEFAULT_INSTRUCTIONS
from aiclassroom.realtime.session import (
    ConnectionChanged,
    ConnectionState,
    ManagedRealtimeSession,
    RealtimeUnavailable,
)

pytest.importorskip("websockets")

CONFIG = events.SessionConfig(
    model="gpt-realtime", instructions=DEFAULT_INSTRUCTIONS, voice="marin"
)
FRAME = 1_280  # 80 ms at 16 kHz, as the microphone delivers it


def speech_frames(seconds: float) -> list[np.ndarray]:
    count = int(seconds * 16_000 / FRAME)
    return [np.full(FRAME, 1000, dtype=np.int16) for _ in range(count)]


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.01)


def session_for(server: FakeRealtimeServer, **kwargs) -> ManagedRealtimeSession:
    options = {"reconnect_delays": (0.05,), "connect_timeout": 2.0, **kwargs}
    return ManagedRealtimeSession(
        api_key="sk-prueba", config=CONFIG, url=server.url, **options
    )


class Collector:
    def __init__(self) -> None:
        self.events: list[events.ServerEvent] = []

    def __call__(self, event: events.ServerEvent) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]


# -- audio -------------------------------------------------------------------


def test_resampling_in_chunks_matches_resampling_all_at_once():
    """Chunk boundaries must be inaudible: the stream equals one long resample."""
    rng = np.random.default_rng(0)
    signal = (rng.normal(0, 3000, 16_000)).astype(np.int16)

    whole = StreamingResampler().process(signal)
    streaming = StreamingResampler()
    pieces = np.concatenate([streaming.process(signal[i : i + FRAME])
                             for i in range(0, signal.size, FRAME)])

    assert np.array_equal(whole, pieces)


def test_one_second_at_16k_becomes_one_second_at_24k():
    out = StreamingResampler().process(np.zeros(16_000, dtype=np.int16))
    assert abs(out.size - 24_000) <= 1


def test_a_tone_keeps_its_pitch_through_resampling():
    t = np.arange(16_000) / 16_000
    tone = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    out = StreamingResampler().process(tone).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(out))
    peak_hz = np.argmax(spectrum) * 24_000 / out.size
    assert abs(peak_hz - 440) < 5


def test_pcm_survives_the_base64_round_trip():
    pcm = np.array([0, 1, -1, 32767, -32768], dtype=np.int16)
    assert np.array_equal(from_base64(to_base64(pcm)), pcm)


# -- configuration -----------------------------------------------------------


def test_the_session_is_configured_for_a_classroom():
    session = events.session_update(CONFIG)["session"]
    turn = session["audio"]["input"]["turn_detection"]

    # Decided with the teacher: the question ends two seconds after speech does.
    assert turn["silence_duration_ms"] == 2000
    assert turn["create_response"] is True
    # What interrupts an answer is the application's call, not any murmur.
    assert turn["interrupt_response"] is False
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert session["audio"]["input"]["transcription"]["language"] == "es"
    assert session["audio"]["output"]["voice"] == "marin"
    assert "español de España" in session["instructions"]


def test_unknown_server_events_are_kept_not_dropped():
    event = events.parse(json.dumps({"type": "rate_limits.updated", "rate_limits": []}))
    assert isinstance(event, events.Other)
    assert event.type == "rate_limits.updated"


def test_garbage_from_the_network_becomes_an_error_not_an_exception():
    assert isinstance(events.parse("<html>proxy</html>"), events.ApiError)


# -- the session against the fake API ----------------------------------------


async def test_opening_authenticates_and_configures_the_session():
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        await session.start()
        try:
            assert session.state is ConnectionState.READY
            connection = server.connections[0]
            assert connection.headers["authorization"] == "Bearer sk-prueba"
            assert "model=gpt-realtime" in connection.path
            assert server.events("session.update")[0]["session"]["type"] == "realtime"
        finally:
            await session.stop()


async def test_a_question_goes_out_and_the_answer_comes_back():
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        received = Collector()
        session.subscribe(received)
        await session.start()
        try:
            for frame in speech_frames(1.2):
                assert session.append_audio(frame)
            await wait_until(lambda: received.of(events.ResponseDone))

            transcripts = received.of(events.InputTranscript)
            assert transcripts[-1].text == "¿Qué es HTML?"
            audio = np.concatenate([delta.pcm for delta in received.of(events.AudioDelta)])
            assert audio.size == 5 * 2_400
            said = [e for e in received.of(events.OutputTranscript) if e.final]
            assert said[0].text.startswith("HTML es")
            assert received.of(events.ResponseDone)[0].usage["total_tokens"] == 170
        finally:
            await session.stop()


async def test_audio_is_sent_at_24k_as_the_api_requires():
    async with FakeRealtimeServer(Script(speech_bytes=10**9)) as server:
        session = session_for(server)
        await session.start()
        try:
            for frame in speech_frames(1.0):
                session.append_audio(frame)
            await wait_until(lambda: len(server.events("input_audio_buffer.append")) >= 12)
            sent = sum(len(base64.b64decode(e["audio"]))
                       for e in server.events("input_audio_buffer.append")) // 2
            # 12 frames of 80 ms at 16 kHz is 0.96 s: 23,040 samples at 24 kHz.
            assert abs(sent - 23_040) <= 2
        finally:
            await session.stop()


async def test_nothing_is_sent_before_the_session_is_open():
    """Spec section 14: no audio leaves unless a session was deliberately opened."""
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        assert session.append_audio(np.zeros(FRAME, dtype=np.int16)) is False
        assert server.connections == []


async def test_a_cancelled_answer_is_reported_as_cancelled():
    async with FakeRealtimeServer(Script(audio_chunks=50, chunk_delay=0.02)) as server:
        session = session_for(server)
        received = Collector()
        session.subscribe(received)
        await session.start()
        try:
            for frame in speech_frames(1.2):
                session.append_audio(frame)
            await wait_until(lambda: received.of(events.AudioDelta))
            assert session.cancel_response()
            await wait_until(lambda: received.of(events.ResponseDone))
            assert received.of(events.ResponseDone)[0].status == "cancelled"
            assert len(received.of(events.AudioDelta)) < 50
        finally:
            await session.stop()


async def test_truncation_says_how_much_was_actually_heard():
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        await session.start()
        try:
            assert session.truncate("asst_0", 1_250)
            await wait_until(lambda: server.events("conversation.item.truncate"))
            sent = server.events("conversation.item.truncate")[0]
            assert sent == {"type": "conversation.item.truncate", "item_id": "asst_0",
                            "content_index": 0, "audio_end_ms": 1_250}
        finally:
            await session.stop()


def test_the_history_is_truncated_in_large_steps_by_the_server():
    """The re-read conversation is cheap only from the prompt cache (spec 16)."""
    truncation = events.session_update(CONFIG)["session"]["truncation"]
    assert truncation == {
        "type": "retention_ratio",
        "retention_ratio": 0.5,
        "token_limits": {"post_instructions": 4000},
    }


async def test_nothing_is_deleted_between_turns_so_the_history_stays_cached():
    """Deleting the oldest turn on every question changed the conversation's
    start every time, and every question paid for the whole history again."""
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        received = Collector()
        session.subscribe(received)
        await session.start()
        try:
            for turn in range(6):
                for frame in speech_frames(1.2):
                    session.append_audio(frame)
                await wait_until(lambda t=turn: len(received.of(events.ResponseDone)) > t)
            await asyncio.sleep(0.1)
            assert server.events("conversation.item.delete") == []
        finally:
            await session.stop()


async def test_a_rejected_key_fails_at_once_and_is_not_retried():
    async with FakeRealtimeServer(Script(reject_status=401)) as server:
        session = session_for(server)
        with pytest.raises(RealtimeUnavailable) as failure:
            await session.start()
        assert failure.value.fatal is True
        assert "clave" in str(failure.value)
        assert session.state is ConnectionState.FAILED


async def test_a_refused_configuration_is_explained():
    async with FakeRealtimeServer(Script(refuse_configuration="voz desconocida")) as server:
        session = session_for(server)
        with pytest.raises(RealtimeUnavailable, match="voz desconocida"):
            await session.start()


async def test_a_dropped_connection_is_reopened_and_reconfigured():
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        changes = Collector()
        session.subscribe(changes)
        await session.start()
        try:
            await server.drop()
            await wait_until(lambda: session.sessions_opened == 2 and session.ready)

            states = [e.state for e in changes.of(ConnectionChanged)]
            assert ConnectionState.RECONNECTING in states
            assert states[-1] is ConnectionState.READY
            # The new connection is configured again, not left on defaults.
            assert len(server.all_events("session.update")) == 2
        finally:
            await session.stop()


async def test_the_session_is_not_ready_while_it_reconnects():
    """Spec section 19: never claim to be reachable when it is not."""
    async with FakeRealtimeServer() as server:
        session = session_for(server, reconnect_delays=(0.3,))
        await session.start()
        try:
            server.script.reject_status = 503
            await server.drop()
            await wait_until(lambda: session.state is ConnectionState.RECONNECTING)
            assert session.append_audio(np.zeros(FRAME, dtype=np.int16)) is False
            server.script.reject_status = None
            await wait_until(lambda: session.ready, timeout=5.0)
        finally:
            await session.stop()


async def test_an_old_session_is_renewed_between_turns():
    async with FakeRealtimeServer() as server:
        session = session_for(server, max_session_seconds=0.2)
        await session.start()
        try:
            await wait_until(lambda: session.sessions_opened >= 2, timeout=3.0)
            assert session.ready
        finally:
            await session.stop()


async def test_renewal_waits_for_the_answer_to_finish():
    async with FakeRealtimeServer(Script(audio_chunks=40, chunk_delay=0.05)) as server:
        session = session_for(server, max_session_seconds=0.3)
        received = Collector()
        session.subscribe(received)
        await session.start()
        try:
            for frame in speech_frames(1.2):
                session.append_audio(frame)
            await wait_until(lambda: received.of(events.ResponseDone), timeout=6.0)
            # The answer arrived whole on the first connection.
            assert len(received.of(events.AudioDelta)) == 40
            assert received.of(events.ResponseDone)[0].status == "completed"
        finally:
            await session.stop()


async def test_stopping_closes_the_connection_without_reconnecting():
    async with FakeRealtimeServer() as server:
        session = session_for(server)
        await session.start()
        await session.stop()
        await asyncio.sleep(0.2)
        assert session.state is ConnectionState.DISCONNECTED
        assert len(server.connections) == 1
