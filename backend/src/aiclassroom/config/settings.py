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


def default_data_root() -> Path:
    """Where the portable build keeps its data.

    `AICLASSROOM_DATA_DIR` wins, so the CI and the tests can redirect it. Then
    the `data/` folder beside the frozen executable, which is what the portable
    layout in spec section 17 describes. Falling back to the current working
    directory only happens when running from source.
    """
    override = os.environ.get("AICLASSROOM_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "data"
    return Path.cwd() / "data"
