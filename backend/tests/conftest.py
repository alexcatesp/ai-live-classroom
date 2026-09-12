from __future__ import annotations

import numpy as np
import pytest

from aiclassroom.audio.devices import FRAME_SAMPLES
from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.wakeword import ScriptedWakeWordDetector
from aiclassroom.config.secrets import PlaintextSecretStore
from aiclassroom.config.settings import DataPaths
from aiclassroom.config.store import SettingsStore
from aiclassroom.session.controller import SessionController
from aiclassroom.session.state import SessionStateMachine


@pytest.fixture
def paths(tmp_path) -> DataPaths:
    return DataPaths(root=tmp_path / "data").ensure()


@pytest.fixture
def store(paths) -> SettingsStore:
    return SettingsStore(paths=paths, secrets=PlaintextSecretStore())


@pytest.fixture
def machine() -> SessionStateMachine:
    return SessionStateMachine()


@pytest.fixture
def engine() -> FakeAudioEngine:
    return FakeAudioEngine()


@pytest.fixture
def detector() -> ScriptedWakeWordDetector:
    """Silence, then one frame that clears the default threshold."""
    return ScriptedWakeWordDetector(scores=[0.0, 0.0, 0.9])


@pytest.fixture
def controller(store, machine, engine, detector) -> SessionController:
    return SessionController(
        store=store,
        machine=machine,
        engine_factory=lambda _settings: engine,
        detector_factory=lambda _settings: detector,
    )


@pytest.fixture
def wakeword_models(paths):
    """A models folder laid out the way the portable build ships it."""
    from aiclassroom.audio.wakeword import base_model_paths

    (paths.models_dir / "oye_chat.onnx").write_bytes(b"x" * 2048)
    for path in base_model_paths(paths.models_dir):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1024)
    return paths.models_dir


@pytest.fixture
def frames():
    """Builds `count` frames of silence, shaped as PortAudio would deliver them."""

    def build(count: int) -> np.ndarray:
        return np.zeros(FRAME_SAMPLES * count, dtype=np.int16)

    return build
