"""Realtime handshake (D-03): each failure must name its own remedy."""

from __future__ import annotations

import asyncio
import json

import pytest

from aiclassroom.realtime.client import (
    HandshakeResult,
    HandshakeStatus,
    StubRealtimeClient,
    WebSocketRealtimeClient,
)


def client() -> WebSocketRealtimeClient:
    return WebSocketRealtimeClient(api_key="sk-test", timeout=0.1)


def test_session_created_is_a_successful_handshake():
    raw = json.dumps(
        {"type": "session.created", "session": {"id": "sess_1", "model": "gpt-realtime"}}
    )
    result = client()._interpret_first_event(raw, "gpt-realtime")
    assert result.ok
    assert result.session_id == "sess_1"
    assert result.model == "gpt-realtime"


def test_an_unauthorised_model_is_distinguished_from_a_bad_key():
    raw = json.dumps(
        {"type": "error", "error": {"code": "model_not_found", "message": "no such model"}}
    )
    result = client()._interpret_first_event(raw, "gpt-realtime")
    assert result.status is HandshakeStatus.MODEL_NOT_AUTHORISED
    assert "gpt-realtime" in result.remedy


def test_an_unparseable_response_does_not_raise():
    result = client()._interpret_first_event("<html>proxy</html>", "gpt-realtime")
    assert result.status is HandshakeStatus.UNREACHABLE


def test_an_unexpected_event_type_is_reported():
    raw = json.dumps({"type": "session.updated"})
    result = client()._interpret_first_event(raw, "gpt-realtime")
    assert result.status is HandshakeStatus.UNREACHABLE


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeInvalidStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(str(status_code))
        self.response = FakeResponse(status_code)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, HandshakeStatus.INVALID_KEY),
        (403, HandshakeStatus.INVALID_KEY),
        (404, HandshakeStatus.MODEL_NOT_AUTHORISED),
        (429, HandshakeStatus.BLOCKED),
        (407, HandshakeStatus.BLOCKED),
        (503, HandshakeStatus.BLOCKED),
        (418, HandshakeStatus.UNREACHABLE),
    ],
)
def test_http_rejections_are_classified(status_code, expected):
    result = client()._interpret_http_status(FakeInvalidStatus(status_code), "gpt-realtime")
    assert result.status is expected
    assert result.detail


def test_a_proxy_rejection_points_at_the_school_network():
    """The most likely cause in a school is an intercepting proxy (R-3)."""
    result = client()._interpret_http_status(FakeInvalidStatus(407), "gpt-realtime")
    assert "proxy" in result.remedy.lower()


async def test_a_hanging_connection_times_out_with_a_remedy(monkeypatch):
    async def never_answers(_model):
        await asyncio.sleep(10)

    subject = client()
    monkeypatch.setattr(subject, "_handshake", never_answers)

    result = await subject.check_connection("gpt-realtime")
    assert result.status is HandshakeStatus.TIMEOUT
    assert "websocket" in result.remedy.lower()


async def test_the_stub_records_the_model_it_was_asked_about():
    stub = StubRealtimeClient(HandshakeResult(HandshakeStatus.OK, "listo"))
    assert (await stub.check_connection("gpt-realtime")).ok
    assert stub.calls == ["gpt-realtime"]
