"""Shapes exchanged with the frontend.

The API key is never part of any response: the frontend only ever learns
whether one is configured (spec section 4.2).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..audio.devices import DeviceInventory
from ..config.settings import Settings
from ..diagnostics.checks import CheckResult
from ..diagnostics.runner import DiagnosticsReport
from ..session.controller import ListeningStatus
from ..session.state import SessionStateMachine, Transition


class TransitionOut(BaseModel):
    source: str
    event: str
    target: str
    at: datetime
    reason: str | None = None

    @classmethod
    def of(cls, transition: Transition) -> TransitionOut:
        return cls(
            source=transition.source,
            event=transition.event,
            target=transition.target,
            at=transition.at,
            reason=transition.reason,
        )


class StateOut(BaseModel):
    state: str
    microphone_active: bool
    available_events: list[str]
    history: list[TransitionOut]

    @classmethod
    def of(cls, machine: SessionStateMachine, history_limit: int = 25) -> StateOut:
        return cls(
            state=machine.state,
            microphone_active=machine.is_microphone_active,
            available_events=[event.value for event in machine.available_events()],
            history=[TransitionOut.of(item) for item in machine.history[-history_limit:]],
        )


class EventIn(BaseModel):
    event: str
    reason: str | None = None


class DeviceOut(BaseModel):
    index: int
    name: str
    channels: int
    is_default: bool


class DevicesOut(BaseModel):
    inputs: list[DeviceOut]
    outputs: list[DeviceOut]
    error: str | None = None

    @classmethod
    def of(cls, inventory: DeviceInventory) -> DevicesOut:
        def convert(devices):
            return [
                DeviceOut(
                    index=device.index,
                    name=device.name,
                    channels=device.channels,
                    is_default=device.is_default,
                )
                for device in devices
            ]

        return cls(
            inputs=convert(inventory.inputs),
            outputs=convert(inventory.outputs),
            error=inventory.error,
        )


class SettingsOut(BaseModel):
    settings: Settings
    api_key_configured: bool
    # Risk R-5: a key stored under a passphrase travels with the folder, but
    # has to be unlocked before each session.
    requires_passphrase: bool = False
    unlocked: bool = True


class ApiKeyIn(BaseModel):
    api_key: str = Field(min_length=1)
    # When given, the key is encrypted so it works on any computer.
    passphrase: str | None = Field(default=None, min_length=1)


class UnlockIn(BaseModel):
    passphrase: str = Field(min_length=1)


class CheckOut(BaseModel):
    id: str
    label: str
    status: str
    detail: str
    remedy: str | None = None
    at: datetime

    @classmethod
    def of(cls, result: CheckResult) -> CheckOut:
        return cls(
            id=result.id,
            label=result.label,
            status=result.status,
            detail=result.detail,
            remedy=result.remedy,
            at=result.at,
        )


class DiagnosticsOut(BaseModel):
    status: str
    ready_to_start: bool
    duration_seconds: float
    started_at: datetime
    finished_at: datetime | None
    results: list[CheckOut]

    @classmethod
    def of(cls, report: DiagnosticsReport) -> DiagnosticsOut:
        return cls(
            status=report.status,
            ready_to_start=report.ready_to_start,
            duration_seconds=report.duration_seconds,
            started_at=report.started_at,
            finished_at=report.finished_at,
            results=[CheckOut.of(result) for result in report.results],
        )


class ListeningOut(BaseModel):
    listening: bool
    activations: int
    interruptions: int
    echo_suppressions: int
    frames_processed: int
    seconds_listening: float
    threshold: float | None
    recent_scores: list[float]
    phrase: str | None
    vad_enabled: bool
    confirmation_frames: int | None
    input_level: float
    speech_probability: float | None

    @classmethod
    def of(cls, status: ListeningStatus) -> ListeningOut:
        return cls(**status.__dict__)
