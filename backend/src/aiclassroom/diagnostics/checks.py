"""Individual diagnostic checks (spec section 4.2, D-02).

Each check answers one question the teacher would otherwise discover in front
of the class, and each failure carries a remedy in plain Spanish. No check
raises: an exception becomes a FAILED result, because a diagnostics screen that
crashes tells the teacher nothing (spec section 19).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ..audio.devices import DeviceInventory
from ..audio.wakeword import (
    BASE_MODELS_SUBFOLDER,
    missing_base_models,
    personal_model_path,
    phrase_model_path,
)
from ..realtime.client import (
    NETWORK_WARNING_SECONDS,
    SESSION_WARNING_SECONDS,
    HandshakeResult,
    HandshakeStatus,
    RealtimeClient,
)

logger = logging.getLogger(__name__)

TLS_EXPIRY_WARNING_DAYS = 14

# Risk R-3. A school that inspects HTTPS re-issues certificates from its own
# authority, so the chain validates against the machine's trust store but the
# issuer is not the public CA the API actually uses. Naming the issuer turns
# "something is wrong with the network" into "this proxy is in the way", which
# is the sentence the administrator needs to hear.
EXPECTED_ISSUER_HINTS = ("google", "digicert", "let's encrypt", "isrg", "globalsign", "amazon")


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


GENERIC_REMEDY = (
    "Vuelve a ejecutar el diagnóstico. Si el fallo persiste, anota el detalle "
    "y consúltalo con el administrador del aula."
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    label: str
    status: CheckStatus
    detail: str
    remedy: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        # A failure the teacher cannot act on is not a diagnostic, it is an
        # alarm. Rather than relying on every call site to remember, a failure
        # without a remedy gets the generic one.
        if self.status is CheckStatus.FAILED and not self.remedy:
            object.__setattr__(self, "remedy", GENERIC_REMEDY)

    @property
    def blocking(self) -> bool:
        """A failed check stops the class from starting; a warning does not."""
        return self.status is CheckStatus.FAILED


def check_microphone(inventory: DeviceInventory, configured: str | None = None) -> CheckResult:
    label = "Micrófono"
    if inventory.error:
        return CheckResult(
            "microphone",
            label,
            CheckStatus.FAILED,
            inventory.error,
            "Comprueba que el equipo tiene tarjeta de sonido y que el micrófono está conectado.",
        )
    if not inventory.has_input:
        return CheckResult(
            "microphone",
            label,
            CheckStatus.FAILED,
            "Windows no ofrece ningún dispositivo de entrada.",
            "Conecta un micrófono y revisa los permisos de micrófono en Windows.",
        )
    names = [device.name for device in inventory.inputs]
    if configured and configured not in names:
        return CheckResult(
            "microphone",
            label,
            CheckStatus.WARNING,
            f"El micrófono configurado ('{configured}') no está disponible; "
            f"se usará el predeterminado del sistema.",
            "Selecciona otro micrófono en la configuración si el predeterminado no sirve.",
        )
    chosen = configured or next(
        (device.name for device in inventory.inputs if device.is_default), names[0]
    )
    return CheckResult(
        "microphone",
        label,
        CheckStatus.OK,
        f"{len(names)} dispositivo(s) de entrada. En uso: {chosen}.",
    )


def check_speakers(inventory: DeviceInventory, configured: str | None = None) -> CheckResult:
    label = "Altavoces"
    if inventory.error:
        return CheckResult(
            "speakers",
            label,
            CheckStatus.FAILED,
            inventory.error,
            "Comprueba que el equipo tiene tarjeta de sonido y una salida de audio activa.",
        )
    if not inventory.has_output:
        return CheckResult(
            "speakers",
            label,
            CheckStatus.FAILED,
            "Windows no ofrece ningún dispositivo de salida.",
            "Conecta altavoces o auriculares y vuelve a ejecutar el diagnóstico.",
        )
    names = [device.name for device in inventory.outputs]
    if configured and configured not in names:
        return CheckResult(
            "speakers",
            label,
            CheckStatus.WARNING,
            f"La salida configurada ('{configured}') no está disponible; "
            "se usará la predeterminada.",
            "Selecciona otra salida en la configuración.",
        )
    chosen = configured or next(
        (device.name for device in inventory.outputs if device.is_default), names[0]
    )
    return CheckResult(
        "speakers",
        label,
        CheckStatus.OK,
        f"{len(names)} dispositivo(s) de salida. En uso: {chosen}.",
    )


def check_wakeword_model(models_dir: Path, phrase: str) -> CheckResult:
    label = "Modelo de activación"
    path = phrase_model_path(models_dir, phrase)
    personal = path == personal_model_path(models_dir, phrase)
    if not path.exists():
        return CheckResult(
            "wakeword_model",
            label,
            CheckStatus.FAILED,
            f"No se encontró el modelo para '{phrase}' en {path}.",
            "Genera el modelo con scripts/train_wakeword.py y colócalo en data/models.",
        )

    # The phrase model alone is not enough: without the shared feature
    # extractor the detector cannot start, and openWakeWord would otherwise try
    # to download it in the middle of a class.
    missing = missing_base_models(models_dir)
    if missing:
        names = ", ".join(item.name for item in missing)
        return CheckResult(
            "wakeword_model",
            label,
            CheckStatus.FAILED,
            f"Faltan los modelos base de openWakeWord ({names}) en "
            f"data/models/{BASE_MODELS_SUBFOLDER}.",
            "Descárgalos con scripts/fetch_wakeword_runtime.py desde un equipo con conexión "
            "y copia la carpeta a data/models.",
        )

    size_mb = path.stat().st_size / (1024 * 1024)
    return CheckResult(
        "wakeword_model",
        label,
        CheckStatus.OK,
        f"Modelo para '{phrase}' "
        f"{'entrenado con tu voz' if personal else 'original'} ({size_mb:.1f} MB).",
    )


async def check_dns(host: str, timeout: float = 5.0) -> CheckResult:
    label = "Resolución DNS"

    def resolve() -> list[str]:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return sorted({info[4][0] for info in infos})

    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(resolve), timeout=timeout)
    except TimeoutError:
        return CheckResult(
            "dns",
            label,
            CheckStatus.FAILED,
            f"La resolución de '{host}' no respondió en {timeout:.0f} segundos.",
            "Puede que el servidor DNS del centro no responda. Consúltalo con el administrador.",
        )
    except socket.gaierror as exc:
        return CheckResult(
            "dns",
            label,
            CheckStatus.FAILED,
            f"No se pudo resolver '{host}': {exc}.",
            "Comprueba la conexión de red del equipo.",
        )
    return CheckResult(
        "dns", label, CheckStatus.OK, f"'{host}' resuelve a {', '.join(addresses)}."
    )


async def check_tls(host: str, timeout: float = 8.0) -> CheckResult:
    """Validate the certificate chain against the system trust store.

    A school network that inspects TLS presents its own certificate, which fails
    here with a clear message instead of a puzzling error mid-class.
    """
    label = "Certificados TLS"

    def connect() -> dict:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                return secured.getpeercert() or {}

    try:
        certificate = await asyncio.wait_for(asyncio.to_thread(connect), timeout=timeout + 2)
    except ssl.SSLCertVerificationError as exc:
        return CheckResult(
            "tls",
            label,
            CheckStatus.FAILED,
            f"El certificado de '{host}' no se pudo verificar: {exc.verify_message or exc}.",
            "Es posible que la red del centro inspeccione el tráfico HTTPS. "
            "Consúltalo con el administrador del aula.",
        )
    except (TimeoutError, OSError) as exc:
        return CheckResult(
            "tls",
            label,
            CheckStatus.FAILED,
            f"No se pudo establecer la conexión TLS con '{host}': {exc}.",
            "Comprueba el cortafuegos y el proxy del equipo.",
        )

    issuer = _certificate_issuer(certificate)
    intercepted = issuer is not None and not any(
        hint in issuer.lower() for hint in EXPECTED_ISSUER_HINTS
    )
    if intercepted:
        # The chain validated, so traffic flows; but it validated against a
        # certificate the school installed, which means someone is reading it.
        return CheckResult(
            "tls",
            label,
            CheckStatus.WARNING,
            f"El certificado de '{host}' lo emite '{issuer}', no una autoridad pública. "
            "La red del centro está inspeccionando el tráfico HTTPS.",
            "La conexión funcionará, pero el tráfico con la API pasa por un equipo del "
            "centro. Coméntalo con el administrador antes de usar la aplicación con "
            "datos del alumnado.",
        )

    described = f"Certificado de '{host}' verificado"
    if issuer:
        described += f", emitido por '{issuer}'"

    expires_at = _certificate_expiry(certificate)
    if expires_at is None:
        return CheckResult("tls", label, CheckStatus.OK, described + ".")
    days_left = (expires_at - datetime.now(UTC)).days
    if days_left < 0:
        return CheckResult(
            "tls",
            label,
            CheckStatus.FAILED,
            f"El certificado de '{host}' está caducado.",
            "Comprueba la fecha y la hora del equipo: un reloj mal ajustado "
            "invalida certificados correctos.",
        )
    if days_left <= TLS_EXPIRY_WARNING_DAYS:
        return CheckResult(
            "tls",
            label,
            CheckStatus.WARNING,
            f"El certificado de '{host}' caduca en {days_left} día(s).",
        )
    return CheckResult("tls", label, CheckStatus.OK, f"{described}, válido {days_left} día(s) más.")


def _certificate_issuer(certificate: dict) -> str | None:
    """The organisation named in the certificate's issuer, if any.

    Python hands the issuer over as nested tuples of (key, value) pairs; the
    organisation name is the part a person recognises.
    """
    for group in certificate.get("issuer", ()):
        for entry in group:
            if len(entry) == 2 and entry[0] == "organizationName":
                return str(entry[1])
    return None


def _certificate_expiry(certificate: dict) -> datetime | None:
    raw = certificate.get("notAfter")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except ValueError:
        logger.warning("Formato de caducidad de certificado no reconocido: %s", raw)
        return None


async def check_api_key(api_key: str | None, base_url: str, timeout: float = 10.0) -> CheckResult:
    """One HTTPS request that proves reachability and key validity at once."""
    label = "Clave de la API"
    if not api_key:
        return CheckResult(
            "api_key",
            label,
            CheckStatus.FAILED,
            "No hay ninguna clave guardada en este equipo.",
            "Introduce la clave en la pantalla de configuración.",
        )

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/v1/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.TimeoutException:
        return CheckResult(
            "api_key",
            label,
            CheckStatus.FAILED,
            f"La API no respondió en {timeout:.0f} segundos.",
            "Comprueba la conexión y si un proxy del centro bloquea la salida HTTPS.",
        )
    except httpx.HTTPError as exc:
        return CheckResult(
            "api_key",
            label,
            CheckStatus.FAILED,
            f"No se pudo contactar con la API: {exc}.",
            "Comprueba la conexión a internet del equipo.",
        )

    if response.status_code == 200:
        return CheckResult(
            "api_key", label, CheckStatus.OK, "La clave es válida y la API responde."
        )
    if response.status_code in (401, 403):
        return CheckResult(
            "api_key",
            label,
            CheckStatus.FAILED,
            f"La API rechazó la clave (HTTP {response.status_code}).",
            "La clave puede ser incorrecta o haber caducado. Introdúcela de nuevo.",
        )
    if response.status_code == 429:
        return CheckResult(
            "api_key",
            label,
            CheckStatus.WARNING,
            "La cuenta ha alcanzado un límite de uso (HTTP 429).",
            "Revisa el consumo de la cuenta antes de empezar la clase.",
        )
    return CheckResult(
        "api_key",
        label,
        CheckStatus.FAILED,
        f"La API respondió con HTTP {response.status_code}.",
        "El servicio puede estar teniendo una incidencia. Inténtalo de nuevo en unos minutos.",
    )


async def check_realtime(client: RealtimeClient, model: str) -> CheckResult:
    """D-03: open the Realtime session, confirm it, close it. No conversation.

    The time is reported because risk R-2 -- WebSocket from the backend versus
    WebRTC from the frontend -- is a question about latency, and this is the
    first place a real number for it exists. It is split in two because only
    the network half is the school's; the session half is OpenAI's, and a
    single number sent teachers after the wrong culprit.
    """
    label = "Conexión Realtime"
    result = await client.check_connection(model)

    if result.ok:
        detail = result.detail + _realtime_timing(result)
        remedies = []
        if result.slow_network:
            remedies.append(
                f"La red tarda más de {NETWORK_WARNING_SECONDS:.1f} s en abrir la conexión. "
                "La clase funcionará, pero las respuestas empezarán con retraso. "
                "Anótalo: es la medida que decide si hace falta cambiar de transporte."
            )
        if result.slow_session:
            remedies.append(
                f"OpenAI tarda más de {SESSION_WARNING_SECONDS:.1f} s en crear la sesión. "
                "No depende de la red del aula y solo se paga una vez al empezar; "
                "suele variar con la carga del servicio."
            )
        if remedies:
            return CheckResult(
                "realtime", label, CheckStatus.WARNING, detail, " ".join(remedies)
            )
        return CheckResult("realtime", label, CheckStatus.OK, detail)
    status = (
        CheckStatus.WARNING if result.status is HandshakeStatus.BLOCKED else CheckStatus.FAILED
    )
    return CheckResult("realtime", label, status, result.detail, result.remedy)


def _realtime_timing(result: HandshakeResult) -> str:
    parts = []
    if result.network_seconds is not None:
        parts.append(f"Red (DNS, TLS y WebSocket): {result.network_seconds:.2f} s")
    if result.session_seconds is not None:
        parts.append(f"creación de la sesión en OpenAI: {result.session_seconds:.2f} s")
    if parts:
        return " " + "; ".join(parts) + "."
    # A client that could not split the time still reports the whole of it.
    if result.elapsed_seconds is not None:
        return f" Tiempo de establecimiento: {result.elapsed_seconds:.2f} s."
    return ""
