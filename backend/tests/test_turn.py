"""The spoken turn inside a class (plan-fase-1, H3), against the fake API."""

from __future__ import annotations

import asyncio
import base64

import numpy as np
import pytest
from fake_realtime import FakeRealtimeServer, Script

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import ScriptedWakeWordDetector
from aiclassroom.realtime.session import ManagedRealtimeSession
from aiclassroom.session.controller import SessionController
from aiclassroom.session.state import Event, SessionStateMachine, State
from aiclassroom.session.turn import TurnController

FIRE = 0.9


def speech(frames: int, value: int = 1000) -> np.ndarray:
    return np.full(frames * FRAME_SAMPLES, value, dtype=np.int16)


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.01)


class Class:
    """A class wired end to end: fake microphone, scripted detector, fake API."""

    def __init__(self, store, server: FakeRealtimeServer, scores: list[float]) -> None:
        self.engine = FakeAudioEngine()
        self.machine = SessionStateMachine()
        self.detector = ScriptedWakeWordDetector(scores=scores, refractory_seconds=0.1)
        self.controller = SessionController(
            store=store,
            machine=self.machine,
            engine_factory=lambda _settings: self.engine,
            detector_factory=lambda _settings: self.detector,
        )
        self.published: list[dict] = []
        self.turns = TurnController(
            controller=self.controller,
            store=store,
            session_factory=lambda key, config, settings: ManagedRealtimeSession(
                api_key=key, config=config, url=server.url, reconnect_delays=(0.05,),
                connect_timeout=2.0,
            ),
            publish=self.published.append,
        )
        self.server = server

    async def start(self) -> None:
        await self.turns.open()
        self.controller.prepare()
        self.controller.start_class()

    async def say_wake_phrase(self) -> None:
        self.engine.feed(speech(1))  # the detector's scripted score fires here
        await wait_until(lambda: self.machine.state is State.CAPTURING_REQUEST)

    def ask(self, frames: int = 16) -> None:
        # 16 frames = 1.28 s, past the fake API's one-second "end of question".
        self.engine.feed(speech(frames))

    @property
    def stream(self):
        return self.engine.streams[-1] if self.engine.streams else None

    def appended(self) -> list[dict]:
        return self.server.all_events("input_audio_buffer.append")

    def states(self) -> list[State]:
        return [transition.target for transition in self.machine.history]

    async def stop(self) -> None:
        self.controller.stop()
        await self.turns.close()


@pytest.fixture
def keyed_store(store):
    store.set_api_key("sk-clase")
    return store


async def test_oye_chat_a_question_and_an_answer_then_back_to_listening(keyed_store):
    async with FakeRealtimeServer() as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask()
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            await wait_until(lambda: lesson.stream is not None and lesson.stream.buffer.queued > 0)
            await asyncio.sleep(0.05)
            lesson.stream.play_out()
            await wait_until(lambda: lesson.machine.state is State.PASSIVE_LISTENING)

            assert lesson.states()[-5:] == [
                State.ACTIVATED,
                State.CAPTURING_REQUEST,
                State.THINKING,
                State.SPEAKING,
                State.PASSIVE_LISTENING,
            ]
            last = lesson.turns.last
            assert last.question == "¿Qué es HTML?"
            assert last.answer.startswith("HTML es")
            assert last.outcome == "completada"
            assert last.first_audio_seconds is not None
        finally:
            await lesson.stop()


async def test_nothing_leaves_the_machine_while_the_class_only_listens(keyed_store):
    """Spec section 14: passive listening sends no audio, however long it lasts."""
    async with FakeRealtimeServer() as server:
        lesson = Class(keyed_store, server, scores=[])
        await lesson.start()
        try:
            lesson.engine.feed(speech(200))  # sixteen seconds of a lesson
            await asyncio.sleep(0.2)
            assert lesson.appended() == []
        finally:
            await lesson.stop()


async def test_the_start_of_a_question_said_in_one_breath_is_kept(keyed_store):
    """The pre-roll: audio from just before the activation goes with the question."""
    async with FakeRealtimeServer(Script(speech_bytes=10**9)) as server:
        lesson = Class(keyed_store, server, scores=[0.0] * 10 + [FIRE])
        await lesson.start()
        try:
            lesson.engine.feed(speech(10, value=777))  # "Oye Chat, ¿qu..."
            await lesson.say_wake_phrase()
            await wait_until(lambda: lesson.appended())

            first = np.frombuffer(base64.b64decode(lesson.appended()[0]["audio"]), dtype="<i2")
            assert (first == 777).any()
        finally:
            await lesson.stop()


async def test_the_microphone_stops_streaming_when_the_question_ends(keyed_store):
    async with FakeRealtimeServer(Script(answer_delay=0.3)) as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask()
            await wait_until(lambda: lesson.machine.state is State.THINKING)
            sent = len(lesson.appended())

            lesson.engine.feed(speech(30))  # the class carries on talking
            await asyncio.sleep(0.2)
            assert len(lesson.appended()) == sent
        finally:
            await lesson.stop()


async def test_saying_oye_chat_and_then_nothing_returns_to_listening(keyed_store):
    # Without a pre-roll, so no tail of the wake phrase reaches the server; with
    # one, the server answers the bare phrase with "¿Sí?" instead (turn.py).
    keyed_store.save(
        keyed_store.load().model_copy(
            update={"activation_timeout_seconds": 1.0, "preroll_ms": 0}
        )
    )
    async with FakeRealtimeServer() as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            await wait_until(lambda: lesson.machine.state is State.PASSIVE_LISTENING, timeout=3)
            assert lesson.turns.last.outcome == "sin pregunta"

            sent = len(lesson.appended())
            lesson.engine.feed(speech(20))
            await asyncio.sleep(0.1)
            assert len(lesson.appended()) == sent  # and the microphone is local again
        finally:
            await lesson.stop()


async def test_a_missing_api_key_stops_the_class_from_starting(store):
    async with FakeRealtimeServer() as server:
        lesson = Class(store, server, scores=[FIRE])
        with pytest.raises(Exception, match="clave"):
            await lesson.turns.open()
        assert lesson.controller.turn_handler is None


async def test_without_a_connection_the_class_listens_and_says_why_it_cannot_answer(
    keyed_store,
):
    """Spec section 19: one unreachable API costs a question, not the class."""
    async with FakeRealtimeServer(Script(reject_status=503)) as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            assert lesson.machine.state is State.PASSIVE_LISTENING
            lesson.engine.feed(speech(1))
            await wait_until(
                lambda: any(t.event is Event.TURN_FAILED for t in lesson.machine.history)
            )
            assert lesson.machine.state is State.PASSIVE_LISTENING
            failures = [m for m in lesson.published if m["payload"]["kind"] == "turn_failed"]
            assert "Sin conexión" in failures[-1]["payload"]["reason"]
        finally:
            await lesson.stop()


async def test_pausing_mid_answer_silences_it_and_cancels_it(keyed_store):
    async with FakeRealtimeServer(Script(audio_chunks=40, chunk_delay=0.02)) as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask()
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            stream = lesson.stream

            lesson.controller.pause()
            await wait_until(lambda: not stream.is_active)
            await wait_until(lambda: server.all_events("response.cancel"))
            assert lesson.machine.state is State.PAUSED
            assert lesson.turns.last.outcome == "cortada"
        finally:
            await lesson.stop()


async def test_oye_chat_during_an_answer_cuts_it_and_says_how_much_was_heard(keyed_store):
    """H4: the interruption cancels the answer and truncates it to what was heard."""
    scores = [FIRE] + [0.0] * 16 + [0.0] * 5 + [FIRE]
    async with FakeRealtimeServer(Script(audio_chunks=40, chunk_delay=0.02)) as server:
        lesson = Class(keyed_store, server, scores=scores)
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask(frames=16)
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            await wait_until(lambda: lesson.stream.buffer.queued > 24_000)
            lesson.stream.advance(500)

            lesson.engine.feed(speech(6))  # "Oye Chat" over the answer
            await wait_until(lambda: server.all_events("conversation.item.truncate"))

            assert State.INTERRUPTED in lesson.states()
            assert server.all_events("response.cancel")
            truncated = server.all_events("conversation.item.truncate")
            assert truncated[-1]["item_id"] == "asst_0"
            assert 0 < truncated[-1]["audio_end_ms"] <= 600
            assert lesson.turns.last.outcome == "interrumpida"
        finally:
            await lesson.stop()


async def test_the_phrase_that_interrupts_opens_the_next_question(keyed_store):
    """H4: "Oye Chat, ¿y ...?" over an answer is answered as a question of its own.

    The cancelled answer still sends its response.done ("cancelled") after the
    new question has begun; it must not be taken for the new turn failing.
    """
    scores = [FIRE] + [0.0] * 16 + [0.0] * 5 + [FIRE]
    async with FakeRealtimeServer(Script(audio_chunks=40, chunk_delay=0.02)) as server:
        lesson = Class(keyed_store, server, scores=scores)
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask(frames=16)
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            await wait_until(lambda: lesson.stream.buffer.queued > 24_000)
            sent_before = len(lesson.appended())

            lesson.engine.feed(speech(6, value=555))  # "Oye Chat, ¿y..." over the answer
            await wait_until(lambda: lesson.machine.state is State.CAPTURING_REQUEST)
            await wait_until(lambda: len(lesson.appended()) > sent_before)

            # The words said over the answer travel with the new question.
            resumed = lesson.appended()[sent_before]
            first = np.frombuffer(base64.b64decode(resumed["audio"]), dtype="<i2")
            assert (first == 555).any()

            lesson.ask(frames=16)
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            await wait_until(
                lambda: lesson.stream is not None
                and lesson.stream.buffer.queued > 0
                and server.all_events("input_audio_buffer.append")
            )
            await wait_until(lambda: lesson.turns._response_finished, timeout=5)
            lesson.stream.play_out()
            await wait_until(lambda: lesson.machine.state is State.PASSIVE_LISTENING)

            assert not any(t.event is Event.TURN_FAILED for t in lesson.machine.history)
            states = lesson.states()
            cut = states.index(State.INTERRUPTED)
            assert states[cut : cut + 6] == [
                State.INTERRUPTED,
                State.ACTIVATED,
                State.CAPTURING_REQUEST,
                State.THINKING,
                State.SPEAKING,
                State.PASSIVE_LISTENING,
            ]
            assert lesson.turns.last.outcome == "completada"
        finally:
            await lesson.stop()


async def test_the_stop_button_cuts_the_answer_and_only_listens_again(keyed_store):
    """H4: unlike the phrase, the button opens no question and sends nothing more."""
    async with FakeRealtimeServer(Script(audio_chunks=40, chunk_delay=0.02)) as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask(frames=16)
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            stream = lesson.stream

            lesson.engine.stop_playback()  # what POST /api/turn/stop does
            lesson.machine.dispatch(Event.INTERRUPT, reason="Parada manual")
            await wait_until(lambda: lesson.machine.state is State.PASSIVE_LISTENING)

            assert not stream.is_active
            assert server.all_events("response.cancel")
            assert lesson.turns.last.outcome == "interrumpida"
            sent = len(lesson.appended())
            lesson.engine.feed(speech(20))
            await asyncio.sleep(0.2)
            assert len(lesson.appended()) == sent
        finally:
            await lesson.stop()


async def test_the_interface_hears_the_question_and_the_answer_as_text(keyed_store):
    """What H5's transcript panel (D-11) will show."""
    async with FakeRealtimeServer() as server:
        lesson = Class(keyed_store, server, scores=[FIRE])
        await lesson.start()
        try:
            await lesson.say_wake_phrase()
            lesson.ask()
            await wait_until(lambda: lesson.machine.state is State.SPEAKING)
            await wait_until(
                lambda: any(m["payload"]["kind"] == "answer" and m["payload"]["final"]
                            for m in lesson.published)
            )
            kinds = [m["payload"]["kind"] for m in lesson.published]
            assert "turn_started" in kinds
            assert "question" in kinds
        finally:
            await lesson.stop()


async def test_stopping_the_class_closes_the_session(keyed_store):
    async with FakeRealtimeServer() as server:
        lesson = Class(keyed_store, server, scores=[])
        await lesson.start()
        await lesson.stop()
        await asyncio.sleep(0.1)
        assert server._sockets == []
        assert lesson.controller.frame_observers == []
