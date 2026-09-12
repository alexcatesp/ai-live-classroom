"""Storage for the API key.

D-07: on Windows the key is encrypted with DPAPI in user scope, which needs no
administrator rights and ties the ciphertext to the Windows account. Everywhere
else -- development machines, CI -- DPAPI does not exist, so a clearly marked
plaintext store stands in. Both sit behind `SecretStore` so nothing else in the
codebase knows which one is in use.

Tokens are tagged with the scheme that produced them (`dpapi:` / `plain:`) so a
settings file copied between machines fails loudly instead of returning rubbish.
"""

from __future__ import annotations

import base64
import logging
import sys
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretError(RuntimeError):
    """Raised when a secret cannot be protected or recovered."""


class SecretStore(Protocol):
    """Turns a secret into a token safe to write to disk, and back."""

    scheme: str

    def protect(self, value: str) -> str:
        """Encrypt `value` into a tagged token."""

    def unprotect(self, token: str) -> str:
        """Recover the secret from a token this store produced."""


def _split_token(token: str, expected_scheme: str) -> str:
    scheme, separator, payload = token.partition(":")
    if not separator:
        raise SecretError("El token guardado no indica con qué método se cifró.")
    if scheme != expected_scheme:
        raise SecretError(
            f"El token se cifró con '{scheme}' y este equipo usa '{expected_scheme}'. "
            "Vuelve a introducir la clave de la API en la pantalla de configuración."
        )
    return payload


class DpapiSecretStore:
    """Windows DPAPI (CryptProtectData) in user scope.

    The ciphertext can only be recovered by the same Windows user on the same
    machine. Copying the portable folder to another PC therefore requires
    entering the key again -- the cost accepted in D-07.
    """

    scheme = "dpapi"

    def __init__(self, description: str = "AI Classroom Live API key") -> None:
        if sys.platform != "win32":  # pragma: no cover - guarded by the factory
            raise SecretError("DPAPI solo está disponible en Windows.")
        self._description = description

    def protect(self, value: str) -> str:
        blob = self._crypt(value.encode("utf-8"), encrypt=True)
        return f"{self.scheme}:{base64.b64encode(blob).decode('ascii')}"

    def unprotect(self, token: str) -> str:
        payload = _split_token(token, self.scheme)
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise SecretError("El token cifrado está corrupto.") from exc
        return self._crypt(raw, encrypt=False).decode("utf-8")

    def _crypt(self, data: bytes, *, encrypt: bool) -> bytes:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def to_blob(payload: bytes) -> DataBlob:
            buffer = ctypes.create_string_buffer(payload, len(payload))
            return DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        source = to_blob(data)
        result = DataBlob()
        function = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
        description = ctypes.c_wchar_p(self._description) if encrypt else None

        ok = function(
            ctypes.byref(source),
            description,
            None,  # no extra entropy: the Windows account is the protection boundary
            None,
            None,
            0,  # user scope, no UI, no administrator rights needed
            ctypes.byref(result),
        )
        if not ok:
            action = "cifrar" if encrypt else "descifrar"
            raise SecretError(
                f"Windows no pudo {action} la clave (error {ctypes.GetLastError()}). "
                "Si has copiado la carpeta desde otro equipo o usuario, "
                "vuelve a introducir la clave."
            )
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)


class PlaintextSecretStore:
    """Development stand-in. Stores the key obfuscated but *not* encrypted.

    Only selected when DPAPI is unavailable, which on the target platform never
    happens. It exists so the rest of the application can be developed and
    tested off Windows.
    """

    scheme = "plain"

    def __init__(self) -> None:
        logger.warning(
            "DPAPI no disponible en esta plataforma: la clave de la API se guardará "
            "SIN cifrar. Aceptable solo para desarrollo, nunca en un aula."
        )

    def protect(self, value: str) -> str:
        return f"{self.scheme}:{base64.b64encode(value.encode('utf-8')).decode('ascii')}"

    def unprotect(self, token: str) -> str:
        payload = _split_token(token, self.scheme)
        try:
            return base64.b64decode(payload, validate=True).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise SecretError("El token guardado está corrupto.") from exc


def create_secret_store() -> SecretStore:
    """Pick the right store for the platform we are running on."""
    if sys.platform == "win32":
        return DpapiSecretStore()
    return PlaintextSecretStore()
