"""Reads and writes `config/settings.json`, keeping the API key encrypted."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .secrets import (
    PassphraseRequired,
    SecretError,
    SecretStore,
    create_secret_store,
    token_scheme,
)
from .settings import DataPaths, Settings, default_data_root

logger = logging.getLogger(__name__)

_API_KEY_FIELD = "api_key_token"


class SettingsStore:
    """The single door to persisted configuration.

    The API key never travels alongside the rest of the settings: callers ask
    for it explicitly, which keeps it out of the payload the frontend receives.
    """

    def __init__(self, paths: DataPaths | None = None, secrets: SecretStore | None = None) -> None:
        self.paths = (paths or DataPaths(root=default_data_root())).ensure()
        self.secrets = secrets or create_secret_store()
        self._passphrase: str | None = None

    # -- passphrase mode (risk R-5) ---------------------------------------

    @property
    def requires_passphrase(self) -> bool:
        """Whether the stored key was protected with a passphrase."""
        return token_scheme(self._read_document().get(_API_KEY_FIELD)) == "pass"

    @property
    def unlocked(self) -> bool:
        """Whether the stored key can be read right now."""
        if not self.has_api_key():
            return True
        if not self.requires_passphrase:
            return True
        return self._passphrase is not None

    def unlock(self, passphrase: str) -> None:
        """Open a passphrase-protected key for the rest of this run.

        Raises WrongPassphrase if it does not open, so the interface can say so
        rather than silently behaving as if no key were configured.
        """
        candidate = create_secret_store(passphrase)
        token = self._read_document().get(_API_KEY_FIELD)
        if token:
            candidate.unprotect(token)  # raises WrongPassphrase on a bad one
        self.secrets = candidate
        self._passphrase = passphrase

    def lock(self) -> None:
        """Forget the passphrase, e.g. when the session ends."""
        self._passphrase = None
        self.secrets = create_secret_store()

    # -- settings ---------------------------------------------------------

    def load(self) -> Settings:
        document = self._read_document()
        try:
            return Settings.model_validate(document.get("settings", {}))
        except ValidationError:
            logger.exception(
                "settings.json contiene valores no válidos; se usan los predeterminados."
            )
            return Settings()

    def save(self, settings: Settings) -> Settings:
        document = self._read_document()
        document["settings"] = settings.model_dump(mode="json")
        self._write_document(document)
        return settings

    # -- API key ----------------------------------------------------------

    def set_api_key(self, api_key: str, passphrase: str | None = None) -> None:
        """Store the key.

        With a passphrase the key becomes portable between computers (R-5);
        without one it is protected by the platform store and stays on this
        machine (D-07).
        """
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("La clave de la API no puede estar vacía.")

        if passphrase is not None:
            self.secrets = create_secret_store(passphrase)
            self._passphrase = passphrase
        elif self._passphrase is not None:
            # Already unlocked with a passphrase: re-encrypting under the
            # platform store would silently make the folder non-portable again.
            pass
        else:
            self.secrets = create_secret_store()

        document = self._read_document()
        document[_API_KEY_FIELD] = self.secrets.protect(api_key)
        self._write_document(document)

    def get_api_key(self) -> str | None:
        """Return the stored key, or None when absent or unreadable.

        An unreadable token is not fatal: it means the folder was copied from
        another machine or user (D-07). The diagnostics report it as a check
        failure with the remedy, rather than crashing the backend.
        """
        token = self._read_document().get(_API_KEY_FIELD)
        if not token:
            return None
        if token_scheme(token) == "pass" and self._passphrase is None:
            raise PassphraseRequired(
                "La clave está protegida con contraseña. Introdúcela para poder usarla."
            )
        try:
            return self.secrets.unprotect(token)
        except PassphraseRequired:
            raise
        except SecretError as exc:
            logger.warning("No se pudo recuperar la clave guardada: %s", exc)
            return None

    def has_api_key(self) -> bool:
        return bool(self._read_document().get(_API_KEY_FIELD))

    def clear_api_key(self) -> None:
        document = self._read_document()
        document.pop(_API_KEY_FIELD, None)
        self._write_document(document)
        self.lock()

    # -- disk -------------------------------------------------------------

    def _read_document(self) -> dict:
        path: Path = self.paths.settings_file
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("No se pudo leer %s; se parte de una configuración vacía.", path)
            return {}
        return document if isinstance(document, dict) else {}

    def _write_document(self, document: dict) -> None:
        path: Path = self.paths.settings_file
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling file first so an interrupted save cannot leave the
        # teacher with a truncated settings file minutes before a class.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(path)
