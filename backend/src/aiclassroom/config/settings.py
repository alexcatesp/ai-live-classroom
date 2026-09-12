"""Application settings (spec section 18) and the on-disk layout (spec section 14)."""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_WAKE_PHRASE = "Oye Chat"
DEFAULT_REALTIME_MODEL = "gpt-realtime"
DEFAULT_VOICE = "marin"
OPENAI_HOST = "api.openai.com"


class TranscriptRetention(StrEnum):
    """Spec section 14: transcripts are only kept if the teacher asks for it."""

    DISCARD = "discard"
    SESSION_ONLY = "session_only"
    KEEP = "keep"


class Settings(BaseModel):
    """Everything the teacher can configure. The API key is *not* here -- it
    lives encrypted in its own field of the settings file (see SettingsStore)."""

    realtime_model: str = DEFAULT_REALTIME_MODEL
    voice: str = DEFAULT_VOICE

    input_device: str | None = None
    output_device: str | None = None

    wake_phrase: str = DEFAULT_WAKE_PHRASE
    wake_sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)
    wake_refractory_seconds: float = Field(default=2.0, ge=0.0, le=30.0)
    # Defences against false positives in a noisy classroom (risk R-1).
    # 0 disables the voice gate; 1 disables the confirmation requirement.
    wake_vad_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    wake_confirmation_frames: int = Field(default=2, ge=1, le=10)
    # Extra margin the detector must clear while the assistant is speaking, so
    # it cannot hear itself through the speakers (risk R-6). 0 disables it.
    echo_guard_margin: float = Field(default=0.15, ge=0.0, le=0.6)

    max_response_seconds: int = Field(default=45, ge=5, le=300)
    materials_dir: str | None = None
    transcript_retention: TranscriptRetention = TranscriptRetention.DISCARD

    daily_cost_limit_eur: float | None = Field(default=None, ge=0.0)
    session_cost_limit_eur: float | None = Field(default=None, ge=0.0)


class DataPaths(BaseModel):
    """Spec section 14 asks for configuration, materials, sessions and metrics to
    be kept apart. The root defaults to a `data/` folder next to the executable
    so the portable folder stays self-contained and writable by a user without
    administrator rights."""

    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def materials_dir(self) -> Path:
        return self.root / "materials"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def metrics_dir(self) -> Path:
        return self.root / "metrics"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    def ensure(self) -> DataPaths:
        for directory in (
            self.config_dir,
            self.materials_dir,
            self.sessions_dir,
            self.metrics_dir,
            self.models_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


# In the portable layout of spec section 17 the backend sits two folders below
# the root that holds `data/`:
#
#     AI-Classroom-Live/          <- the root the teacher copies
#       AI-Classroom-Live.exe
#       runtime/backend/aiclassroom-backend.exe
#       data/
PORTABLE_DEPTH = 3  # runtime/backend/aiclassroom-backend.exe -> the root


def portable_root(executable: Path) -> Path:
    """The folder holding `data/`, given the backend executable's path."""
    resolved = executable.resolve()
    for _ in range(PORTABLE_DEPTH):
        resolved = resolved.parent
    return resolved


def default_data_root() -> Path:
    """Where the application keeps its data.

    The shell passes `--data-dir` explicitly, because it knows where it was
    started from; this is the fallback for running the backend by hand and for
    the environment override the tests and the CI use.
    """
    override = os.environ.get("AICLASSROOM_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return portable_root(Path(sys.executable)) / "data"
    return Path.cwd() / "data"
