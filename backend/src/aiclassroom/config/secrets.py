"""Storage for the API key.

Three stores, one interface, so nothing else in the codebase knows which is in
use:

* **DPAPI** (D-07, the Windows default). Encrypted in user scope, needing no
  administrator rights. The ciphertext is bound to the Windows account, which
  is why the key does not travel with the portable folder.
* **Passphrase** (risk R-5). scrypt derives a key from a passphrase the teacher
  chooses, and AES-GCM encrypts with it. This one *does* travel: the same
  folder works on any classroom PC, at the cost of typing the passphrase when
  the application opens. Offered as an option, never as the default, because it
  trades a secret Windows holds for one a person has to remember.
* **Plaintext**, for development off Windows, and clearly marked as such.

Tokens are tagged with the scheme that produced them (`dpapi:`, `pass:`,
`plain:`) so a settings file copied between machines fails loudly instead of
returning rubbish.
"""

from __future__ import annotations

import base64
import logging
import sys
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretError(RuntimeError):
    """Raised when a secret cannot be protected or recovered."""


class PassphraseRequired(SecretError):
    """Raised when the stored key needs a passphrase that has not been given."""


class WrongPassphrase(SecretError):
    """Raised when the passphrase does not open the stored key."""


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


# scrypt parameters. n=2**15 keeps unlocking to a fraction of a second on a
# classroom PC while making a brute force over the ciphertext expensive.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
# 128 * n * r is 32 MiB, which is exactly OpenSSL's default ceiling, so the
# limit has to be raised explicitly or the derivation refuses to run.
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12


class PassphraseSecretStore:
    """AES-GCM under a scrypt-derived key (risk R-5).

    The ciphertext depends on nothing but the passphrase, so the settings file
    can be copied between computers and USB sticks and still open. The salt and
    nonce are stored alongside it -- neither is secret, and a fresh nonce per
    write is what keeps AES-GCM safe across repeated saves.
    """

    scheme = "pass"

    def __init__(self, passphrase: str) -> None:
        if not passphrase:
            raise PassphraseRequired("Hace falta una contraseña para abrir la clave.")
        self._passphrase = passphrase.encode("utf-8")

    def _derive(self, salt: bytes) -> bytes:
        import hashlib

        return hashlib.scrypt(
            self._passphrase,
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_KEY_BYTES,
            maxmem=_SCRYPT_MAXMEM,
        )

    def protect(self, value: str) -> str:
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._derive(salt)).encrypt(nonce, value.encode("utf-8"), None)
        payload = base64.b64encode(salt + nonce + ciphertext).decode("ascii")
        return f"{self.scheme}:{payload}"

    def unprotect(self, token: str) -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        payload = _split_token(token, self.scheme)
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise SecretError("El token cifrado está corrupto.") from exc

        if len(raw) <= _SALT_BYTES + _NONCE_BYTES:
            raise SecretError("El token cifrado está incompleto.")

        salt = raw[:_SALT_BYTES]
        nonce = raw[_SALT_BYTES : _SALT_BYTES + _NONCE_BYTES]
        ciphertext = raw[_SALT_BYTES + _NONCE_BYTES :]
        try:
            plaintext = AESGCM(self._derive(salt)).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            # AES-GCM cannot tell a wrong passphrase from tampering, and for the
            # teacher the answer is the same either way: type it again.
            raise WrongPassphrase(
                "La contraseña no es correcta, o el archivo de configuración se ha alterado."
            ) from exc
        return plaintext.decode("utf-8")


def token_scheme(token: str | None) -> str | None:
    """Which store wrote this token, without needing to open it."""
    if not token:
        return None
    scheme, separator, _ = token.partition(":")
    return scheme if separator else None


def create_secret_store(passphrase: str | None = None) -> SecretStore:
    """Pick the store to use.

    A passphrase always wins: asking for one is an explicit choice to make the
    key portable. Otherwise DPAPI on Windows, and the development stand-in
    everywhere else.
    """
    if passphrase:
        return PassphraseSecretStore(passphrase)
    if sys.platform == "win32":
        return DpapiSecretStore()
    return PlaintextSecretStore()
