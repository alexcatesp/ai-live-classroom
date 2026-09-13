"""The Realtime API's events, as the rest of the application needs them.

Client events are built here, in one place, so a renamed field in the API is a
one-line change. Server events are parsed into a small set of typed events; the
many the application does not act on are passed through as `Other` rather than
dropped, so they still reach the log.

Event names follow the GA Realtime API as documented in September 2026.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .audio import REALTIME_RATE, from_base64, to_base64

# -- session configuration ---------------------------------------------------


@dataclass(frozen=True)
class SessionConfig:
    """What a class's Realtime session is set up with (spec section 18)."""

    model: str
    instructions: str
    voice: str
    #: Speech-to-text for the transcript panel (D-11). None disables it.
    transcription_model: str | None = "gpt-4o-mini-transcribe"
    language: str = "es"
    #: Silence that ends a question. Two seconds, decided with the teacher:
    #: a calm room and a directional microphone (plan-fase-1, decision 1).
    silence_ms: int = 2000
    vad_threshold: float = 0.5
    prefix_padding_ms: int = 300
    #: "near_field", "far_field" or None.
    noise_reduction: str | None = "far_field"
    #: Tokens per response; "inf" leaves it to the instructions.
    max_output_tokens: int | str = "inf"


def config_from_settings(settings: Any, instructions: str) -> SessionConfig:
    """The session a class opens, from what the teacher configured."""
    return SessionConfig(
        model=settings.realtime_model,
        instructions=instructions,
        voice=settings.voice,
        transcription_model=settings.transcription_model or None,
        silence_ms=settings.turn_silence_ms,
        noise_reduction=settings.realtime_noise_reduction or None,
    )


def session_update(config: SessionConfig) -> dict[str, Any]:
    transcription = (
        {"model": config.transcription_model, "language": config.language}
        if config.transcription_model
        else None
    )
    noise_reduction = {"type": config.noise_reduction} if config.noise_reduction else None
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": config.instructions,
            "max_output_tokens": config.max_output_tokens,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_RATE},
                    "transcription": transcription,
                    "noise_reduction": noise_reduction,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": config.vad_threshold,
                        "prefix_padding_ms": config.prefix_padding_ms,
                        "silence_duration_ms": config.silence_ms,
                        # The server answers when the question ends...
                        "create_response": True,
                        # ...but does not decide what interrupts an answer: in
                        # a classroom any murmur would. "Oye Chat" and the
                        # stop button do (plan-fase-1, H4).
                        "interrupt_response": False,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_RATE},
                    "voice": config.voice,
                },
            },
        },
    }


# -- client events -----------------------------------------------------------


def append_audio(pcm24: Any) -> dict[str, Any]:
    return {"type": "input_audio_buffer.append", "audio": to_base64(pcm24)}


def commit_audio() -> dict[str, Any]:
    return {"type": "input_audio_buffer.commit"}


def clear_audio() -> dict[str, Any]:
    return {"type": "input_audio_buffer.clear"}


def create_response() -> dict[str, Any]:
    return {"type": "response.create"}


def cancel_response() -> dict[str, Any]:
    return {"type": "response.cancel"}


def truncate_item(item_id: str, audio_end_ms: int) -> dict[str, Any]:
    """Tell the server how much of an answer was actually heard (H4)."""
    return {
        "type": "conversation.item.truncate",
        "item_id": item_id,
        "content_index": 0,
        "audio_end_ms": max(0, int(audio_end_ms)),
    }


def delete_item(item_id: str) -> dict[str, Any]:
    return {"type": "conversation.item.delete", "item_id": item_id}


# -- server events -----------------------------------------------------------


@dataclass(frozen=True)
class ServerEvent:
    type: str


@dataclass(frozen=True)
class SpeechStarted(ServerEvent):
    item_id: str | None


@dataclass(frozen=True)
class SpeechStopped(ServerEvent):
    item_id: str | None


@dataclass(frozen=True)
class ItemCreated(ServerEvent):
    item_id: str
    role: str | None


@dataclass(frozen=True)
class InputTranscript(ServerEvent):
    """What was asked, as text. `final` once the transcription is complete."""

    item_id: str | None
    text: str
    final: bool


@dataclass(frozen=True)
class ResponseStarted(ServerEvent):
    response_id: str | None


@dataclass(frozen=True)
class AudioDelta(ServerEvent):
    response_id: str | None
    item_id: str | None
    pcm: Any = field(repr=False)


@dataclass(frozen=True)
class OutputTranscript(ServerEvent):
    """What the assistant is saying, as text."""

    response_id: str | None
    item_id: str | None
    text: str
    final: bool


@dataclass(frozen=True)
class ResponseDone(ServerEvent):
    response_id: str | None
    #: "completed", "cancelled", "incomplete" or "failed".
    status: str | None
    #: As reported by the API; recorded for costs (spec section 15, H7).
    usage: dict[str, Any] | None
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ApiError(ServerEvent):
    code: str | None
    message: str


@dataclass(frozen=True)
class Other(ServerEvent):
    payload: dict[str, Any] = field(repr=False)


def parse(raw: str | bytes) -> ServerEvent:
    """Turn one WebSocket message into an event. Never raises on content."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ApiError(type="error", code="unparseable", message="Mensaje ilegible de la API.")
    if not isinstance(data, dict):
        return ApiError(type="error", code="unparseable", message="Mensaje inesperado de la API.")

    kind = str(data.get("type", ""))

    if kind == "input_audio_buffer.speech_started":
        return SpeechStarted(kind, data.get("item_id"))
    if kind == "input_audio_buffer.speech_stopped":
        return SpeechStopped(kind, data.get("item_id"))
    if kind in ("conversation.item.created", "conversation.item.added"):
        item = data.get("item") or {}
        if item.get("id"):
            return ItemCreated(kind, item["id"], item.get("role"))
    if kind == "conversation.item.input_audio_transcription.delta":
        return InputTranscript(kind, data.get("item_id"), data.get("delta", ""), final=False)
    if kind == "conversation.item.input_audio_transcription.completed":
        return InputTranscript(kind, data.get("item_id"), data.get("transcript", ""), final=True)
    if kind == "response.created":
        return ResponseStarted(kind, (data.get("response") or {}).get("id"))
    if kind == "response.output_audio.delta":
        return AudioDelta(
            kind, data.get("response_id"), data.get("item_id"), from_base64(data.get("delta", ""))
        )
    if kind == "response.output_audio_transcript.delta":
        return OutputTranscript(
            kind, data.get("response_id"), data.get("item_id"), data.get("delta", ""), final=False
        )
    if kind == "response.output_audio_transcript.done":
        return OutputTranscript(
            kind, data.get("response_id"), data.get("item_id"), data.get("transcript", ""),
            final=True,
        )
    if kind == "response.done":
        response = data.get("response") or {}
        items = tuple(
            output["id"] for output in response.get("output") or [] if output.get("id")
        )
        return ResponseDone(
            kind, response.get("id"), response.get("status"), response.get("usage"), items
        )
    if kind == "error":
        error = data.get("error") or {}
        return ApiError(kind, error.get("code"), error.get("message", "Error de la API."))

    return Other(kind, data)
