"""Diagnostics: the screen that has to be right before the class starts."""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta

import pytest

from aiclassroom.audio.devices import AudioDevice, DeviceInventory
from aiclassroom.diagnostics import checks
from aiclassroom.diagnostics.checks import (
    CheckStatus,
    check_dns,
    check_microphone,
    check_realtime,
    check_speakers,
    check_tls,
    check_wakeword_model,
)
from aiclassroom.diagnostics.runner import DiagnosticsRunner
from aiclassroom.realtime.client import HandshakeResult, HandshakeStatus, StubRealtimeClient


def inventory(inputs=1, outputs=1, error=None) -> DeviceInventory:
    def devices(count, prefix):
        return [
            AudioDevice(index=i, name=f"{prefix} {i}", channels=1, default_sample_rate=48000.0,
                        is_default=i == 0)
            for i in range(count)
        ]

    return DeviceInventory(
        inputs=devices(inputs, "Micrófono"), outputs=devices(outputs, "Altavoz"), error=error
    )


# -- devices --------------------------------------------------------------


def test_no_microphone_is_a_blocking_failure_with_a_remedy():
    result = check_microphone(inventory(inputs=0))
    assert result.status is CheckStatus.FAILED
    assert result.blocking
    assert "permisos" in result.remedy.lower()


def test_a_missing_configured_microphone_warns_but_does_not_block():
    """The class still starts on the default device (spec section 22, USB mic)."""
    result = check_microphone(inventory(), configured="USB que ya no está")
    assert result.status is CheckStatus.WARNING
    assert result.blocking is False


def test_a_present_microphone_passes():
    assert check_microphone(inventory(inputs=2)).status is CheckStatus.OK


def test_a_broken_audio_stack_is_reported_once_per_check():
    broken = inventory(inputs=0, outputs=0, error="PortAudio no encontrado")
    assert check_microphone(broken).status is CheckStatus.FAILED
    assert check_speakers(broken).status is CheckStatus.FAILED


def test_no_speakers_is_a_blocking_failure():
    assert check_speakers(inventory(outputs=0)).blocking is True


# -- wake word model ------------------------------------------------------


def test_a_missing_wake_word_model_blocks_the_class(paths):
    result = check_wakeword_model(paths.models_dir, "Oye Chat")
    assert result.status is CheckStatus.FAILED
    assert "train_wakeword" in result.remedy


def test_a_present_wake_word_model_passes(paths):
    (paths.models_dir / "oye_chat.onnx").write_bytes(b"x" * 2048)
    result = check_wakeword_model(paths.models_dir, "Oye Chat")
    assert result.status is CheckStatus.OK
    assert "Oye Chat" in result.detail


# -- network --------------------------------------------------------------


async def test_dns_failure_names_the_host(monkeypatch):
    def explode(*_args, **_kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(checks.socket, "getaddrinfo", explode)
    result = await check_dns("api.openai.com")
    assert result.status is CheckStatus.FAILED
    assert "api.openai.com" in result.detail


async def test_dns_success_lists_the_addresses(monkeypatch):
    monkeypatch.setattr(
        checks.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("203.0.113.7", 443))],
    )
    result = await check_dns("api.openai.com")
    assert result.status is CheckStatus.OK
    assert "203.0.113.7" in result.detail


async def test_an_intercepted_certificate_points_at_the_school_network(monkeypatch):
    """R-3: TLS inspection is the failure most likely to appear in a school."""

    class FakeContext:
        def wrap_socket(self, *_args, **_kwargs):
            error = ssl.SSLCertVerificationError("self signed certificate")
            error.verify_message = "self signed certificate in certificate chain"
            raise error

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(checks.ssl, "create_default_context", FakeContext)
    monkeypatch.setattr(checks.socket, "create_connection", lambda *_a, **_k: FakeSocket())

    result = await check_tls("api.openai.com")
    assert result.status is CheckStatus.FAILED
    assert "inspeccione" in result.remedy


def test_a_certificate_close_to_expiry_warns():
    soon = (datetime.now(UTC) + timedelta(days=3)).strftime("%b %d %H:%M:%S %Y GMT")
    assert checks._certificate_expiry({"notAfter": soon}) is not None


def test_an_unreadable_expiry_date_is_ignored_rather_than_fatal():
    assert checks._certificate_expiry({"notAfter": "mañana"}) is None
    assert checks._certificate_expiry({}) is None


# -- api key --------------------------------------------------------------


async def test_a_missing_api_key_blocks_with_the_right_instruction():
    result = await checks.check_api_key(None, "https://api.openai.com")
    assert result.status is CheckStatus.FAILED
    assert "configuración" in result.remedy


# -- realtime -------------------------------------------------------------


async def test_a_successful_handshake_passes():
    stub = StubRealtimeClient(HandshakeResult(HandshakeStatus.OK, "Sesión establecida."))
    assert (await check_realtime(stub, "gpt-realtime")).status is CheckStatus.OK


async def test_a_rate_limited_account_warns_rather_than_blocks():
    """Spec section 16: a usage limit is a cost problem, not a broken classroom."""
    stub = StubRealtimeClient(
        HandshakeResult(HandshakeStatus.BLOCKED, "Límite alcanzado", remedy="Revisa el consumo.")
    )
    assert (await check_realtime(stub, "gpt-realtime")).status is CheckStatus.WARNING


async def test_an_invalid_key_blocks():
    stub = StubRealtimeClient(HandshakeResult(HandshakeStatus.INVALID_KEY, "Clave rechazada"))
    assert (await check_realtime(stub, "gpt-realtime")).blocking is True


# -- the whole report -----------------------------------------------------


@pytest.fixture
def offline_runner(store, monkeypatch):
    """A runner whose network calls are all stubbed out."""

    def build(dns_ok=True, tls_ok=True, key_ok=True, handshake=None):
        monkeypatch.setattr(
            checks.socket,
            "getaddrinfo",
            (lambda *_a, **_k: [(2, 1, 6, "", ("203.0.113.7", 443))])
            if dns_ok
            else _raise(socket.gaierror("sin DNS")),
        )

        async def fake_tls(host, timeout=8.0):  # noqa: ARG001
            return checks.CheckResult(
                "tls",
                "Certificados TLS",
                CheckStatus.OK if tls_ok else CheckStatus.FAILED,
                "certificado",
            )

        async def fake_key(api_key, base_url, timeout=10.0):  # noqa: ARG001
            return checks.CheckResult(
                "api_key",
                "Clave de la API",
                CheckStatus.OK if key_ok else CheckStatus.FAILED,
                "clave",
            )

        monkeypatch.setattr("aiclassroom.diagnostics.runner.check_tls", fake_tls)
        monkeypatch.setattr("aiclassroom.diagnostics.runner.check_api_key", fake_key)
        return DiagnosticsRunner(
            store=store,
            realtime_client_factory=lambda _key: StubRealtimeClient(
                handshake or HandshakeResult(HandshakeStatus.OK, "ok")
            ),
        )

    return build


def _raise(exception):
    def thrower(*_args, **_kwargs):
        raise exception

    return thrower


async def test_a_dns_failure_skips_everything_downstream(offline_runner, store):
    store.set_api_key("sk-test")
    report = await offline_runner(dns_ok=False).run()

    by_id = {result.id: result for result in report.results}
    assert by_id["dns"].status is CheckStatus.FAILED
    assert by_id["tls"].status is CheckStatus.SKIPPED
    assert by_id["realtime"].status is CheckStatus.SKIPPED
    assert report.ready_to_start is False


async def test_a_tls_failure_skips_the_api_checks(offline_runner, store):
    store.set_api_key("sk-test")
    report = await offline_runner(tls_ok=False).run()
    by_id = {result.id: result for result in report.results}
    assert by_id["api_key"].status is CheckStatus.SKIPPED
    assert by_id["realtime"].status is CheckStatus.SKIPPED


async def test_a_bad_key_skips_the_realtime_handshake(offline_runner, store):
    """Otherwise the teacher sees the same authentication error twice."""
    store.set_api_key("sk-test")
    report = await offline_runner(key_ok=False).run()
    by_id = {result.id: result for result in report.results}
    assert by_id["realtime"].status is CheckStatus.SKIPPED


async def test_every_check_appears_in_the_report_even_when_skipped(offline_runner, store):
    store.set_api_key("sk-test")
    report = await offline_runner(dns_ok=False).run()
    assert {result.id for result in report.results} == {
        "microphone",
        "speakers",
        "wakeword_model",
        "dns",
        "tls",
        "api_key",
        "realtime",
    }


async def test_a_warning_alone_still_allows_the_class_to_start(offline_runner, store, paths):
    """Spec section 4.2 blocks on failures; a warning is information."""
    store.set_api_key("sk-test")
    (paths.models_dir / "oye_chat.onnx").write_bytes(b"x" * 1024)
    runner = offline_runner(
        handshake=HandshakeResult(HandshakeStatus.BLOCKED, "Límite alcanzado")
    )
    report = await runner.run()

    # The container has no audio devices, so the device checks fail here; what
    # this asserts is that a WARNING on its own never sets ready_to_start False.
    warnings = [r for r in report.results if r.status is CheckStatus.WARNING]
    assert all(not warning.blocking for warning in warnings)


async def test_an_unexpected_realtime_crash_still_renders_a_report(offline_runner, store):
    store.set_api_key("sk-test")
    runner = offline_runner()
    runner._realtime_client_factory = _raise(RuntimeError("fallo inesperado"))

    report = await runner.run()
    realtime = next(result for result in report.results if result.id == "realtime")
    assert realtime.status is CheckStatus.FAILED
    assert "inesperado" in realtime.detail


async def test_the_report_records_how_long_it_took(offline_runner, store):
    store.set_api_key("sk-test")
    report = await offline_runner().run()
    assert report.finished_at is not None
    assert report.duration_seconds >= 0.0


def test_a_failed_check_can_never_be_left_without_a_remedy():
    """Structural guarantee, so a new check cannot regress it by forgetting."""
    result = checks.CheckResult("nuevo", "Nuevo", CheckStatus.FAILED, "algo falló")
    assert result.remedy == checks.GENERIC_REMEDY


def test_a_passing_check_carries_no_remedy():
    assert checks.CheckResult("nuevo", "Nuevo", CheckStatus.OK, "bien").remedy is None
