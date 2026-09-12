"""Audio plumbing: framing, device resolution and failure reporting."""

from __future__ import annotations

import numpy as np
import pytest

from aiclassroom.audio.devices import (
    FRAME_SAMPLES,
    AudioDevice,
    DeviceInventory,
    resolve_device,
)
from aiclassroom.audio.engine import AudioError, FakeAudioEngine, tone


def device(index: int, name: str, is_default: bool = False) -> AudioDevice:
    return AudioDevice(
        index=index, name=name, channels=1, default_sample_rate=48000.0, is_default=is_default
    )


def test_capture_delivers_whole_frames():
    engine = FakeAudioEngine()
    received: list[np.ndarray] = []
    engine.start_capture(received.append)
    engine.feed(np.zeros(FRAME_SAMPLES * 3, dtype=np.int16))
    assert len(received) == 3
    assert all(frame.size == FRAME_SAMPLES for frame in received)


def test_a_partial_frame_is_not_delivered():
    """openWakeWord needs exactly 1280 samples; a short tail must wait."""
    engine = FakeAudioEngine()
    received: list[np.ndarray] = []
    engine.start_capture(received.append)
    engine.feed(np.zeros(FRAME_SAMPLES + 100, dtype=np.int16))
    assert len(received) == 1


def test_feeding_without_capture_is_an_error():
    with pytest.raises(AudioError):
        FakeAudioEngine().feed(np.zeros(FRAME_SAMPLES, dtype=np.int16))


def test_stopping_capture_detaches_the_callback():
    engine = FakeAudioEngine()
    engine.start_capture(lambda _frame: None)
    assert engine.is_capturing
    engine.stop_capture()
    assert not engine.is_capturing


def test_devices_are_matched_by_name_not_index():
    """Indices shuffle when a USB microphone is plugged in (spec section 22)."""
    devices = [device(0, "Micrófono integrado"), device(3, "USB Audio Device")]
    assert resolve_device(devices, "USB Audio Device") == 3


def test_device_matching_falls_back_to_a_partial_name():
    devices = [device(2, "Auriculares USB (Realtek)")]
    assert resolve_device(devices, "auriculares usb") == 2


def test_an_unknown_device_falls_back_to_the_system_default():
    """The class still starts; the diagnostics raise the warning."""
    assert resolve_device([device(0, "Micrófono integrado")], "Micro que ya no existe") is None
    assert resolve_device([], None) is None


def test_an_empty_inventory_reports_why():
    inventory = DeviceInventory(error="PortAudio no encontrado")
    assert inventory.has_input is False
    assert inventory.has_output is False


def test_the_test_tone_is_audible_and_fades_at_both_ends():
    """A tone that starts at full amplitude clicks in the classroom speakers."""
    samples = tone(seconds=0.2)
    assert samples.dtype == np.int16
    assert samples.size == int(0.2 * 24_000)

    # Compare envelopes rather than single samples: one sample of a sine says
    # nothing about its amplitude.
    window = int(0.005 * 24_000)
    middle = samples.size // 2
    peak_middle = int(np.abs(samples[middle : middle + window]).max())
    assert peak_middle > 0
    assert int(np.abs(samples[:window]).max()) < peak_middle
    assert int(np.abs(samples[-window:]).max()) < peak_middle
