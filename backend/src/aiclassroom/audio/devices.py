"""Audio device discovery.

sounddevice binds to PortAudio at import time, and that fails on a machine with
no audio stack at all -- a CI runner, a container. Import errors are therefore
turned into an empty inventory carrying the reason, which the diagnostics screen
shows as a failed check rather than a crash (spec section 19).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# The Realtime API speaks 24 kHz PCM16; openWakeWord expects 16 kHz. Capture at
# 16 kHz and resample upwards for the API in Phase 1, rather than the reverse:
# the wake word runs on every frame and the API only on activation.
CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, the window openWakeWord consumes


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    channels: int
    default_sample_rate: float
    is_default: bool = False


@dataclass(frozen=True)
class DeviceInventory:
    inputs: list[AudioDevice] = field(default_factory=list)
    outputs: list[AudioDevice] = field(default_factory=list)
    error: str | None = None
    #: Whether PortAudio itself loaded. A machine with no sound card is a
    #: different problem from a build that forgot to bundle the library: the
    #: first is the classroom's to fix, the second is ours, and only the second
    #: should ever fail a build.
    library_available: bool = True

    @property
    def has_input(self) -> bool:
        return bool(self.inputs)

    @property
    def has_output(self) -> bool:
        return bool(self.outputs)


def _import_sounddevice():
    """Import sounddevice, or explain in Spanish why it is not usable."""
    try:
        import sounddevice
    except OSError as exc:  # PortAudio library missing or unloadable
        raise RuntimeError(
            "No se encontró la biblioteca de audio PortAudio. En la versión portable "
            f"debe distribuirse junto al ejecutable. Detalle: {exc}"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(f"El módulo de audio no está instalado: {exc}") from exc
    return sounddevice


def probe_devices() -> DeviceInventory:
    """List the input and output devices Windows is offering us."""
    try:
        sounddevice = _import_sounddevice()
    except RuntimeError as exc:
        return DeviceInventory(error=str(exc), library_available=False)

    try:
        raw_devices = sounddevice.query_devices()
        default_input, default_output = sounddevice.default.device
    except Exception as exc:  # noqa: BLE001 - PortAudio raises a wide range
        # The library is there; the host audio stack is not answering.
        logger.exception("Fallo al consultar los dispositivos de audio.")
        return DeviceInventory(
            error=f"No se pudieron consultar los dispositivos de audio: {exc}"
        )

    inputs: list[AudioDevice] = []
    outputs: list[AudioDevice] = []
    for index, device in enumerate(raw_devices):
        name = str(device.get("name", f"Dispositivo {index}"))
        sample_rate = float(device.get("default_samplerate") or 0.0)
        if device.get("max_input_channels", 0) > 0:
            inputs.append(
                AudioDevice(
                    index=index,
                    name=name,
                    channels=int(device["max_input_channels"]),
                    default_sample_rate=sample_rate,
                    is_default=index == default_input,
                )
            )
        if device.get("max_output_channels", 0) > 0:
            outputs.append(
                AudioDevice(
                    index=index,
                    name=name,
                    channels=int(device["max_output_channels"]),
                    default_sample_rate=sample_rate,
                    is_default=index == default_output,
                )
            )
    return DeviceInventory(inputs=inputs, outputs=outputs)


def resolve_device(inventory_entries: list[AudioDevice], wanted: str | None) -> int | None:
    """Match a configured device name to an index.

    Devices are stored by name rather than index because indices shuffle when a
    USB microphone is plugged in -- exactly the scenario spec section 22 asks us
    to test. An unknown name falls back to the system default instead of
    failing: the class starts, and the diagnostics warn about the mismatch.
    """
    if not wanted:
        return None
    for device in inventory_entries:
        if device.name == wanted:
            return device.index
    for device in inventory_entries:
        if wanted.lower() in device.name.lower():
            return device.index
    logger.warning("El dispositivo '%s' ya no existe; se usará el predeterminado.", wanted)
    return None
