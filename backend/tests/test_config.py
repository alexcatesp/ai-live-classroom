"""Settings persistence and the API key store (D-07)."""

from __future__ import annotations

import json

import pytest

from aiclassroom.config.secrets import (
    PlaintextSecretStore,
    SecretError,
    create_secret_store,
)
from aiclassroom.config.settings import DataPaths, Settings, TranscriptRetention
from aiclassroom.config.store import SettingsStore


def test_defaults_match_the_specification():
    settings = Settings()
    assert settings.wake_phrase == "Oye Chat"
    # Spec section 14: nothing is recorded unless the teacher turns it on.
    assert settings.transcript_retention is TranscriptRetention.DISCARD


def test_sensitivity_is_bounded():
    with pytest.raises(ValueError):
        Settings(wake_sensitivity=1.5)


def test_data_folders_are_separated(paths: DataPaths):
    """Spec section 14 asks for configuration, materials, sessions and metrics apart."""
    folders = {paths.config_dir, paths.materials_dir, paths.sessions_dir, paths.metrics_dir}
    assert len(folders) == 4
    assert all(folder.is_dir() for folder in folders)


def test_settings_round_trip(store: SettingsStore):
    store.save(Settings(voice="cedar", wake_sensitivity=0.8, max_response_seconds=30))
    reloaded = store.load()
    assert reloaded.voice == "cedar"
    assert reloaded.wake_sensitivity == 0.8
    assert reloaded.max_response_seconds == 30


def test_corrupt_settings_fall_back_to_defaults_instead_of_crashing(store: SettingsStore):
    """A broken file minutes before a class must not stop the application."""
    store.paths.settings_file.write_text("{not json", encoding="utf-8")
    assert store.load().wake_phrase == "Oye Chat"


def test_invalid_values_fall_back_to_defaults(store: SettingsStore):
    store.paths.settings_file.write_text(
        json.dumps({"settings": {"wake_sensitivity": 42}}), encoding="utf-8"
    )
    assert store.load().wake_sensitivity == 0.5


def test_api_key_is_stored_encrypted_not_in_the_clear(store: SettingsStore):
    store.set_api_key("sk-secreto-de-prueba")
    raw = store.paths.settings_file.read_text(encoding="utf-8")
    assert "sk-secreto-de-prueba" not in raw
    assert store.get_api_key() == "sk-secreto-de-prueba"


def test_api_key_survives_a_settings_save(store: SettingsStore):
    store.set_api_key("sk-abc")
    store.save(Settings(voice="cedar"))
    assert store.get_api_key() == "sk-abc"
    assert store.load().voice == "cedar"


def test_empty_api_key_is_rejected(store: SettingsStore):
    with pytest.raises(ValueError):
        store.set_api_key("   ")


def test_api_key_can_be_cleared(store: SettingsStore):
    store.set_api_key("sk-abc")
    store.clear_api_key()
    assert store.has_api_key() is False
    assert store.get_api_key() is None


def test_a_token_from_another_machine_is_reported_not_returned(store: SettingsStore):
    """D-07: copying the folder between PCs means re-entering the key.

    The store must say 'no key' rather than hand back rubbish, so the
    diagnostics can tell the teacher exactly what to do.
    """
    store.paths.settings_file.write_text(
        json.dumps({"api_key_token": "dpapi:QUJD"}), encoding="utf-8"
    )
    assert store.has_api_key() is True
    assert store.get_api_key() is None


def test_untagged_token_is_rejected():
    with pytest.raises(SecretError):
        PlaintextSecretStore().unprotect("sin-esquema")


def test_corrupt_payload_is_rejected():
    with pytest.raises(SecretError):
        PlaintextSecretStore().unprotect("plain:!!!not-base64!!!")


def test_the_platform_picks_the_right_store(monkeypatch):
    monkeypatch.setattr("aiclassroom.config.secrets.sys.platform", "linux")
    assert create_secret_store().scheme == "plain"


def test_saving_is_atomic(store: SettingsStore):
    """An interrupted save must not leave a truncated settings file."""
    store.save(Settings())
    assert not list(store.paths.config_dir.glob("*.tmp"))
    assert json.loads(store.paths.settings_file.read_text(encoding="utf-8"))


def test_the_portable_root_is_found_from_the_backend_executable():
    """Spec section 17 layout: runtime/backend/ sits two folders below the root.

    Getting this wrong would put the teacher's settings inside runtime/ instead
    of data/, where the next build would overwrite them.
    """
    from pathlib import Path

    from aiclassroom.config.settings import portable_root

    executable = Path("/media/usb/AI-Classroom-Live/runtime/backend/aiclassroom-backend.exe")
    assert portable_root(executable) == Path("/media/usb/AI-Classroom-Live")


def test_the_data_directory_override_wins(monkeypatch, tmp_path):
    from aiclassroom.config.settings import default_data_root

    monkeypatch.setenv("AICLASSROOM_DATA_DIR", str(tmp_path / "otra"))
    assert default_data_root() == tmp_path / "otra"
