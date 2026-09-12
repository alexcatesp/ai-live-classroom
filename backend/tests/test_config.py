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


# -- passphrase mode: the key travels with the folder (risk R-5) ----------


def test_a_passphrase_protected_key_opens_on_any_machine(paths):
    """The whole point of R-5: copy the folder, type the passphrase, done."""
    from aiclassroom.config.store import SettingsStore

    original = SettingsStore(paths=paths)
    original.set_api_key("sk-portatil", passphrase="la clase de 2 DAW")

    # A different machine: a fresh store over the same settings file, with no
    # access to the Windows account that wrote it.
    on_another_pc = SettingsStore(paths=paths)
    on_another_pc.unlock("la clase de 2 DAW")

    assert on_another_pc.get_api_key() == "sk-portatil"


def test_the_key_is_sealed_until_the_passphrase_is_given(paths, store):
    from aiclassroom.config.secrets import PassphraseRequired
    from aiclassroom.config.store import SettingsStore

    store.set_api_key("sk-portatil", passphrase="contraseña larga")

    fresh = SettingsStore(paths=paths)
    assert fresh.has_api_key() is True
    assert fresh.requires_passphrase is True
    assert fresh.unlocked is False

    with pytest.raises(PassphraseRequired):
        fresh.get_api_key()


def test_a_wrong_passphrase_is_rejected_rather_than_silently_ignored(paths, store):
    from aiclassroom.config.secrets import WrongPassphrase
    from aiclassroom.config.store import SettingsStore

    store.set_api_key("sk-portatil", passphrase="la buena")

    with pytest.raises(WrongPassphrase):
        SettingsStore(paths=paths).unlock("la mala")


def test_the_passphrase_is_never_written_to_disk(store):
    store.set_api_key("sk-portatil", passphrase="mi contraseña secreta")
    raw = store.paths.settings_file.read_text(encoding="utf-8")

    assert "mi contraseña secreta" not in raw
    assert "sk-portatil" not in raw


def test_each_save_uses_a_fresh_nonce(paths, store):
    """Reusing a nonce with AES-GCM would leak the key across saves."""
    store.set_api_key("sk-igual", passphrase="contraseña")
    first = store.paths.settings_file.read_text(encoding="utf-8")
    store.set_api_key("sk-igual", passphrase="contraseña")
    second = store.paths.settings_file.read_text(encoding="utf-8")

    assert first != second


def test_saving_again_while_unlocked_keeps_the_key_portable(paths, store):
    """Re-entering the key must not quietly re-bind the folder to one PC."""
    store.set_api_key("sk-uno", passphrase="contraseña")
    store.set_api_key("sk-dos")  # no passphrase given this time

    on_another_pc = SettingsStore(paths=paths)
    assert on_another_pc.requires_passphrase is True
    on_another_pc.unlock("contraseña")
    assert on_another_pc.get_api_key() == "sk-dos"


def test_locking_forgets_the_passphrase(paths, store):
    from aiclassroom.config.secrets import PassphraseRequired

    store.set_api_key("sk-portatil", passphrase="contraseña")
    assert store.get_api_key() == "sk-portatil"

    store.lock()
    with pytest.raises(PassphraseRequired):
        store.get_api_key()


def test_clearing_the_key_also_unlocks_the_store(store):
    store.set_api_key("sk-portatil", passphrase="contraseña")
    store.clear_api_key()

    assert store.has_api_key() is False
    assert store.requires_passphrase is False
    assert store.unlocked is True


def test_a_store_with_no_key_counts_as_unlocked(store):
    assert store.unlocked is True
    assert store.requires_passphrase is False


def test_a_tampered_ciphertext_is_detected(store):
    """AES-GCM authenticates, so an edited settings file does not decrypt."""
    import json

    from aiclassroom.config.secrets import WrongPassphrase

    store.set_api_key("sk-portatil", passphrase="contraseña")
    document = json.loads(store.paths.settings_file.read_text(encoding="utf-8"))
    token = document["api_key_token"]
    document["api_key_token"] = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    store.paths.settings_file.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(WrongPassphrase):
        store.unlock("contraseña")


def test_an_empty_passphrase_is_refused():
    from aiclassroom.config.secrets import PassphraseRequired, PassphraseSecretStore

    with pytest.raises(PassphraseRequired):
        PassphraseSecretStore("")


def test_the_store_is_chosen_by_whether_a_passphrase_was_given(monkeypatch):
    from aiclassroom.config.secrets import create_secret_store

    monkeypatch.setattr("aiclassroom.config.secrets.sys.platform", "win32")
    # A passphrase is an explicit choice for portability, so it wins over DPAPI.
    assert create_secret_store("contraseña").scheme == "pass"


def test_tokens_say_which_store_wrote_them():
    from aiclassroom.config.secrets import PassphraseSecretStore, token_scheme

    assert token_scheme(PassphraseSecretStore("x").protect("sk")) == "pass"
    assert token_scheme("dpapi:AAAA") == "dpapi"
    assert token_scheme("sin-esquema") is None
    assert token_scheme(None) is None
