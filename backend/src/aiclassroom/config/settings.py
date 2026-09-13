"""Application settings (spec section 18) and the on-disk layout (spec section 14)."""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_WAKE_PHRASE = "Oye Chat"
DEFAULT_REALTIME_MODEL = "gpt-realtime-2"
# The default until September 2026. A file that still holds it never chose a
# model, so it follows the default (SettingsStore.load).
PREVIOUS_DEFAULT_REALTIME_MODEL = "gpt-realtime"
# Bumped when a stored setting has to be reinterpreted on load.
SETTINGS_VERSION = 1
DEFAULT_VOICE = "marin"
OPENAI_HOST = "api.openai.com"


class ReasoningEffort(StrEnum):
    """How much a reasoning-capable Realtime model thinks before answering.

    The API has no "off": minimal is the least, and the cheapest and fastest.
    """

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TranscriptRetention(StrEnum):
    """Spec section 14: transcripts are only kept if the teacher asks for it."""

    DISCARD = "discard"
    SESSION_ONLY = "session_only"
    KEEP = "keep"


class Settings(BaseModel):
    """Everything the teacher can configure. The API key is *not* here -- it
    lives encrypted in its own field of the settings file (see SettingsStore)."""

    realtime_model: str = DEFAULT_REALTIME_MODEL
    # Classroom questions need no deliberation, and reasoning is billed as
    # text output and adds latency. Sent only to models that reason.
    reasoning_effort: ReasoningEffort = ReasoningEffort.MINIMAL
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

    # The spoken turn (plan-fase-1). A question ends after this much silence:
    # two seconds, decided for a calm room with a directional microphone.
    turn_silence_ms: int = Field(default=2000, ge=300, le=5000)
    # Speech-to-text for the transcript panel (D-11); empty disables it.
    transcription_model: str | None = "gpt-4o-mini-transcribe"
    # "near_field", "far_field" or None for the API's noise reduction.
    realtime_noise_reduction: str | None = "far_field"
    # The conversation is re-read as input on every new question (spec 16).
    # Re-read from the cache it costs 80 times less, so the history is never
    # edited turn by turn, which would miss the cache every time. When it
    # outgrows this many tokens the server drops the older half in one go,
    # and the cache is missed once. About six questions with 30 s answers.
    history_max_tokens: int = Field(default=4000, ge=1000, le=32000)
    # Audio kept from just before an activation and sent with the question, so
    # "Oye Chat, ¿qué...?" said without a pause does not lose its first words.
    preroll_ms: int = Field(default=500, ge=0, le=2000)
    # After "Oye Chat", how long to wait for a question to start.
    activation_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    # A question still going after this long is ended and answered.
    max_question_seconds: float = Field(default=30.0, ge=5.0, le=120.0)

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
