"""A stand-in for the Realtime API, served on localhost for the tests.

It speaks just enough of the protocol to exercise the session end to end:
the handshake, configuration, audio in, a scripted answer out, cancellation,
item deletion, rejected keys and connections that drop. Every event the client
sends is recorded, so a test can assert on what actually went over the wire.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Script:
    """How the fake answers."""

    #: Bytes of input audio after which the fake "hears" a question end.
    speech_bytes: int = 24_000 * 2  # one second at 24 kHz, 16-bit
    transcript: str = "¿Qué es HTML?"
    answer: str = "HTML es el lenguaje de marcado de la web."
    audio_chunks: int = 5
    samples_per_chunk: int = 2_400  # 100 ms at 24 kHz
    #: Seconds between audio chunks, so a cancel can land mid-answer.
    chunk_delay: float = 0.0
    #: Reject the upgrade with this HTTP status.
    reject_status: int | None = None
    #: Reply to session.update with an error instead of session.updated.
    refuse_configuration: str | None = None


@dataclass
class Connection:
    headers: dict
    path: str
    received: list[dict] = field(default_factory=list)


class FakeRealtimeServer:
    def __init__(self, script: Script | None = None) -> None:
        self.script = script or Script()
        self.connections: list[Connection] = []
        self._server = None
        self._sockets: list = []
        self.url = ""

    async def __aenter__(self) -> FakeRealtimeServer:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        )
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/realtime"
        return self

    async def __aexit__(self, *_exc) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    # -- controls ----------------------------------------------------------

    async def drop(self) -> None:
        """Cut every open connection, as a flaky school network would."""
        for socket in list(self._sockets):
            with contextlib.suppress(Exception):
                await socket.close(code=1011, reason="red perdida")

    def events(self, kind: str, connection: int = -1) -> list[dict]:
        return [event for event in self.connections[connection].received if event["type"] == kind]

    def all_events(self, kind: str) -> list[dict]:
        return [e for c in self.connections for e in c.received if e["type"] == kind]

    # -- protocol ----------------------------------------------------------

    def _process_request(self, connection, request):
        if self.script.reject_status is not None:
            return connection.respond(self.script.reject_status, "rechazado\n")
        return None

    async def _handle(self, socket) -> None:
        # HTTP header names are case-insensitive; keep them as lower case.
        headers = {name.lower(): value for name, value in socket.request.headers.raw_items()}
        record = Connection(headers=headers, path=socket.request.path)
        self.connections.append(record)
        self._sockets.append(socket)
        buffered = 0
        answering: asyncio.Task | None = None
        turn = 0

        async def send(event: dict) -> None:
            await socket.send(json.dumps(event))

        try:
            await send({"type": "session.created", "session": {"id": "sess_fake"}})
            async for raw in socket:
                event = json.loads(raw)
                record.received.append(event)
                kind = event["type"]

                if kind == "session.update":
                    if self.script.refuse_configuration:
                        await send({
                            "type": "error",
                            "error": {"code": "invalid_value",
                                      "message": self.script.refuse_configuration},
                        })
                    else:
                        await send({"type": "session.updated", "session": event["session"]})

                elif kind == "input_audio_buffer.append":
                    if buffered == 0:
                        await send({"type": "input_audio_buffer.speech_started",
                                    "item_id": f"user_{turn}"})
                    buffered += len(base64.b64decode(event["audio"]))
                    if buffered >= self.script.speech_bytes and answering is None:
                        buffered = 0
                        answering = asyncio.create_task(self._answer(send, turn))
                        turn += 1

                elif kind == "response.cancel":
                    if answering is not None and not answering.done():
                        answering.cancel()
                        await send({
                            "type": "response.done",
                            "response": {"id": f"resp_{turn - 1}", "status": "cancelled",
                                         "output": [{"id": f"asst_{turn - 1}"}], "usage": None},
                        })

                elif kind == "conversation.item.delete":
                    await send({"type": "conversation.item.deleted",
                                "item_id": event["item_id"]})

                if answering is not None and answering.done():
                    answering = None
        except Exception:  # noqa: BLE001 - a dropped client ends the handler
            pass
        finally:
            if answering is not None:
                answering.cancel()
            self._sockets.remove(socket)

    async def _answer(self, send, turn: int) -> None:
        script = self.script
        user, assistant, response = f"user_{turn}", f"asst_{turn}", f"resp_{turn}"
        await send({"type": "input_audio_buffer.speech_stopped", "item_id": user})
        await send({"type": "conversation.item.created",
                    "item": {"id": user, "role": "user"}})
        await send({"type": "conversation.item.input_audio_transcription.completed",
                    "item_id": user, "transcript": script.transcript})
        await send({"type": "response.created", "response": {"id": response}})
        await send({"type": "conversation.item.created",
                    "item": {"id": assistant, "role": "assistant"}})

        words = script.answer.split(" ")
        for index in range(script.audio_chunks):
            pcm = np.full(script.samples_per_chunk, index + 1, dtype="<i2")
            await send({"type": "response.output_audio.delta", "response_id": response,
                        "item_id": assistant,
                        "delta": base64.b64encode(pcm.tobytes()).decode()})
            if index < len(words):
                await send({"type": "response.output_audio_transcript.delta",
                            "response_id": response, "item_id": assistant,
                            "delta": words[index] + " "})
            if script.chunk_delay:
                await asyncio.sleep(script.chunk_delay)

        await send({"type": "response.output_audio_transcript.done", "response_id": response,
                    "item_id": assistant, "transcript": script.answer})
        await send({
            "type": "response.done",
            "response": {
                "id": response, "status": "completed",
                "output": [{"id": assistant}],
                "usage": {"input_token_details": {"audio_tokens": 50},
                          "output_token_details": {"audio_tokens": 120},
                          "total_tokens": 170},
            },
        })
