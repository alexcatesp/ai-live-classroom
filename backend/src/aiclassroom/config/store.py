"""Reads and writes `config/settings.json`, keeping the API key encrypted."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .secrets import SecretError, SecretStore, create_secret_store
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

    def set_api_key(self, api_key: str) -> None:
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("La clave de la API no puede estar vacía.")
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
        try:
            return self.secrets.unprotect(token)
        except SecretError as exc:
            logger.warning("No se pudo recuperar la clave guardada: %s", exc)
            return None

    def has_api_key(self) -> bool:
        return bool(self._read_document().get(_API_KEY_FIELD))

    def clear_api_key(self) -> None:
        document = self._read_document()
        document.pop(_API_KEY_FIELD, None)
        self._write_document(document)

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
