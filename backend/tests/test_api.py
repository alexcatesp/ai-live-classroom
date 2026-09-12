"""HTTP surface, including the smoke test the specification asks for (section 24)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiclassroom.api.app import TOKEN_HEADER, AppContext, create_app
from aiclassroom.diagnostics.runner import DiagnosticsRunner
from aiclassroom.main import READY_PREFIX, find_free_port, parse_args
from aiclassroom.realtime.client import HandshakeResult, HandshakeStatus, StubRealtimeClient

TOKEN = "token-de-prueba"


@pytest.fixture
def client(store, controller, monkeypatch):
    """A full application wired to fakes: no devices, no network."""
    from aiclassroom.diagnostics import checks

    monkeypatch.setattr(
        checks.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("203.0.113.7", 443))]
    )

    async def fake_tls(host, timeout=8.0):  # noqa: ARG001
        return checks.CheckResult("tls", "Certificados TLS", checks.CheckStatus.OK, "ok")

    async def fake_key(api_key, base_url, timeout=10.0):  # noqa: ARG001
        status = checks.CheckStatus.OK if api_key else checks.CheckStatus.FAILED
        return checks.CheckResult("api_key", "Clave de la API", status, "clave")

    monkeypatch.setattr("aiclassroom.diagnostics.runner.check_tls", fake_tls)
    monkeypatch.setattr("aiclassroom.diagnostics.runner.check_api_key", fake_key)

    context = AppContext(
        store=store,
        controller=controller,
        runner=DiagnosticsRunner(
            store=store,
            realtime_client_factory=lambda _key: StubRealtimeClient(
                HandshakeResult(HandshakeStatus.OK, "Sesión establecida.")
            ),
        ),
        token=TOKEN,
    )
    with TestClient(create_app(context)) as test_client:
        test_client.headers.update({TOKEN_HEADER: TOKEN})
        yield test_client


# -- authentication -------------------------------------------------------


def test_health_needs_no_token(client):
    """The shell polls this before it knows anything else."""
    client.headers.pop(TOKEN_HEADER)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_every_api_route_rejects_a_missing_token(client):
    client.headers.pop(TOKEN_HEADER)
    for method, path in [
        ("get", "/api/state"),
        ("get", "/api/settings"),
        ("get", "/api/devices"),
        ("post", "/api/class/prepare"),
        ("post", "/api/diagnostics/run"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_a_wrong_token_is_rejected(client):
    client.headers.update({TOKEN_HEADER: "token-equivocado"})
    assert client.get("/api/state").status_code == 401


def test_the_api_key_is_never_returned_to_the_frontend(client):
    """Spec section 4.2: the key does not belong in the interface."""
    stored = client.post("/api/settings/api-key", json={"api_key": "sk-muy-secreta"})
    assert stored.status_code == 204

    body = client.get("/api/settings").text
    assert "sk-muy-secreta" not in body
    assert client.get("/api/settings").json()["api_key_configured"] is True


def test_the_api_key_can_be_removed(client):
    client.post("/api/settings/api-key", json={"api_key": "sk-abc"})
    assert client.delete("/api/settings/api-key").status_code == 204
    assert client.get("/api/settings").json()["api_key_configured"] is False


def test_an_empty_api_key_is_refused(client):
    assert client.post("/api/settings/api-key", json={"api_key": "  "}).status_code == 400


# -- state ----------------------------------------------------------------


def test_the_initial_state_is_idle_with_the_microphone_closed(client):
    body = client.get("/api/state").json()
    assert body["state"] == "IDLE"
    assert body["microphone_active"] is False


def test_the_state_route_lists_what_can_be_done_next(client):
    assert "CREATE_SESSION" in client.get("/api/state").json()["available_events"]


def test_an_illegal_transition_is_a_conflict_not_a_crash(client):
    response = client.post("/api/state/event", json={"event": "RESPONSE_STARTED"})
    assert response.status_code == 409
    assert "IDLE" in response.json()["detail"]


def test_an_unknown_event_is_rejected(client):
    assert client.post("/api/state/event", json={"event": "BAILAR"}).status_code == 400


# -- the class lifecycle over HTTP ---------------------------------------


def test_a_class_can_be_prepared_started_paused_and_stopped(client):
    assert client.post("/api/class/prepare").json()["state"] == "READY"

    started = client.post("/api/class/start").json()
    assert started["state"] == "PASSIVE_LISTENING"
    assert started["microphone_active"] is True

    paused = client.post("/api/class/pause").json()
    assert paused["state"] == "PAUSED"
    assert paused["microphone_active"] is False

    assert client.post("/api/class/resume").json()["state"] == "PASSIVE_LISTENING"
    assert client.post("/api/class/stop").json()["state"] == "STOPPED"


def test_starting_before_preparing_is_a_conflict(client):
    assert client.post("/api/class/start").status_code == 409


def test_the_history_lets_the_interface_show_what_happened(client):
    client.post("/api/class/prepare")
    history = client.get("/api/state").json()["history"]
    assert [item["event"] for item in history] == ["CREATE_SESSION", "SESSION_PREPARED"]


def test_listening_status_is_served(client):
    client.post("/api/class/prepare")
    client.post("/api/class/start")
    body = client.get("/api/listening").json()
    assert body["listening"] is True
    assert body["phrase"] == "Oye Chat"


# -- settings and devices -------------------------------------------------


def test_settings_round_trip_over_http(client):
    current = client.get("/api/settings").json()["settings"]
    current["wake_sensitivity"] = 0.75
    assert client.put("/api/settings", json=current).status_code == 200
    assert client.get("/api/settings").json()["settings"]["wake_sensitivity"] == 0.75


def test_invalid_settings_are_rejected(client):
    current = client.get("/api/settings").json()["settings"]
    current["wake_sensitivity"] = 9
    assert client.put("/api/settings", json=current).status_code == 422


def test_devices_are_listed_with_their_error_when_there_are_none(client):
    body = client.get("/api/devices").json()
    assert body["inputs"] == []
    assert body["error"]  # the container has no PortAudio, and that is reported


# -- diagnostics ----------------------------------------------------------


def test_there_is_no_report_until_one_is_run(client):
    assert client.get("/api/diagnostics/last").json() is None


def test_running_the_diagnostics_returns_every_check(client):
    client.post("/api/settings/api-key", json={"api_key": "sk-test"})
    body = client.post("/api/diagnostics/run").json()

    assert {result["id"] for result in body["results"]} >= {
        "microphone",
        "speakers",
        "wakeword_model",
        "dns",
        "tls",
        "api_key",
        "realtime",
    }
    assert body["status"] in {"ok", "warning", "failed"}


def test_the_last_report_is_remembered(client):
    client.post("/api/settings/api-key", json={"api_key": "sk-test"})
    first = client.post("/api/diagnostics/run").json()
    assert client.get("/api/diagnostics/last").json()["started_at"] == first["started_at"]


def test_each_failed_check_explains_itself(client):
    body = client.post("/api/diagnostics/run").json()
    for result in body["results"]:
        assert result["detail"]
        if result["status"] == "failed":
            assert result["remedy"], f"{result['id']} falla sin decir qué hacer"


# -- events ---------------------------------------------------------------


def test_the_event_socket_rejects_a_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events?token=incorrecto") as socket:
            socket.receive_json()


def test_the_event_socket_pushes_the_current_state_then_transitions(client):
    with client.websocket_connect(f"/ws/events?token={TOKEN}") as socket:
        first = socket.receive_json()
        assert first["type"] == "state"
        assert first["payload"]["state"] == "IDLE"

        client.post("/api/class/prepare")
        events = [socket.receive_json() for _ in range(2)]
        assert [event["payload"]["target"] for event in events] == ["PREPARING", "READY"]


# -- the executable itself ------------------------------------------------


def test_a_free_port_is_found():
    assert 1024 < find_free_port() <= 65535


def test_the_handshake_prefix_is_stable():
    """The Tauri shell parses this line; changing it breaks startup."""
    assert READY_PREFIX == "AICLASSROOM_READY "


def test_the_command_line_defaults_to_an_ephemeral_port():
    arguments = parse_args([])
    assert arguments.port == 0
    assert arguments.token is None
    assert arguments.selftest is False


def test_the_selftest_flag_is_recognised():
    assert parse_args(["--selftest"]).selftest is True
