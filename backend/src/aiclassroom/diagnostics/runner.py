"""Runs the checks in order and produces the report the teacher sees."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..audio.devices import probe_devices
from ..config.settings import OPENAI_HOST, Settings
from ..config.store import SettingsStore
from ..realtime.client import RealtimeClient, WebSocketRealtimeClient
from .checks import (
    CheckResult,
    CheckStatus,
    check_api_key,
    check_dns,
    check_microphone,
    check_realtime,
    check_speakers,
    check_tls,
    check_wakeword_model,
)

logger = logging.getLogger(__name__)

API_BASE_URL = f"https://{OPENAI_HOST}"


@dataclass(frozen=True)
class DiagnosticsReport:
    results: list[CheckResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def ready_to_start(self) -> bool:
        """Spec section 4.2: the class may only start once the checks pass."""
        return not any(result.blocking for result in self.results)

    @property
    def status(self) -> CheckStatus:
        if any(result.status is CheckStatus.FAILED for result in self.results):
            return CheckStatus.FAILED
        if any(result.status is CheckStatus.WARNING for result in self.results):
            return CheckStatus.WARNING
        return CheckStatus.OK

    @property
    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


def _skipped(check_id: str, label: str, because: str) -> CheckResult:
    return CheckResult(
        check_id,
        label,
        CheckStatus.SKIPPED,
        f"No se comprobó porque {because}.",
    )


class DiagnosticsRunner:
    """Orchestrates the checks, skipping the ones whose premise already failed.

    Order matters: without DNS there is no point attempting TLS, and without a
    valid key the Realtime handshake would only produce a second, confusing
    authentication error.
    """

    def __init__(
        self,
        store: SettingsStore,
        realtime_client_factory=None,
        host: str = OPENAI_HOST,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._store = store
        self._host = host
        self._base_url = base_url
        self._realtime_client_factory = realtime_client_factory or self._default_realtime_client

    @staticmethod
    def _default_realtime_client(api_key: str) -> RealtimeClient:
        return WebSocketRealtimeClient(api_key=api_key)

    async def run(self, settings: Settings | None = None) -> DiagnosticsReport:
        settings = settings or self._store.load()
        started_at = datetime.now(UTC)
        results: list[CheckResult] = []

        inventory = probe_devices()
        results.append(check_microphone(inventory, settings.input_device))
        results.append(check_speakers(inventory, settings.output_device))
        results.append(
            check_wakeword_model(self._store.paths.models_dir, settings.wake_phrase)
        )

        dns_result = await check_dns(self._host)
        results.append(dns_result)

        if dns_result.blocking:
            reason = "la resolución DNS falló"
            results.append(_skipped("tls", "Certificados TLS", reason))
            results.append(_skipped("api_key", "Clave de la API", reason))
            results.append(_skipped("realtime", "Conexión Realtime", reason))
            return DiagnosticsReport(results, started_at, datetime.now(UTC))

        tls_result = await check_tls(self._host)
        results.append(tls_result)

        if tls_result.blocking:
            reason = "la conexión TLS falló"
            results.append(_skipped("api_key", "Clave de la API", reason))
            results.append(_skipped("realtime", "Conexión Realtime", reason))
            return DiagnosticsReport(results, started_at, datetime.now(UTC))

        api_key = self._store.get_api_key()
        key_result = await check_api_key(api_key, self._base_url)
        results.append(key_result)

        if key_result.blocking or not api_key:
            results.append(
                _skipped("realtime", "Conexión Realtime", "la clave de la API no es utilizable")
            )
            return DiagnosticsReport(results, started_at, datetime.now(UTC))

        try:
            client = self._realtime_client_factory(api_key)
            results.append(await check_realtime(client, settings.realtime_model))
        except Exception as exc:  # noqa: BLE001 - the screen must always render
            logger.exception("El diagnóstico de Realtime falló de forma inesperada.")
            results.append(
                CheckResult(
                    "realtime",
                    "Conexión Realtime",
                    CheckStatus.FAILED,
                    f"Error inesperado al comprobar la conexión: {exc}",
                )
            )

        return DiagnosticsReport(results, started_at, datetime.now(UTC))
