"""Audio capture and playback (D-05).

The whole audio path lives in Python so the wake word detector reads the same
buffers the capture thread fills, with no copy between processes. The frontend
never touches the microphone: it only renders state.

Everything sits behind `AudioEngine` so the tests -- and development on Linux --
can run against `FakeAudioEngine` with no PortAudio present.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

import numpy as np

from .devices import (
    CAPTURE_SAMPLE_RATE,
    FRAME_SAMPLES,
    PLAYBACK_SAMPLE_RATE,
    probe_devices,
    resolve_device,
)

logger = logging.getLogger(__name__)

FrameCallback = Callable[[np.ndarray], None]


class AudioError(RuntimeError):
    """Raised when capture or playback cannot be started."""


class AudioEngine(Protocol):
    """Microphone in, speaker out."""

    @property
    def is_capturing(self) -> bool: ...

    def start_capture(self, on_frame: FrameCallback) -> None:
        """Begin delivering `FRAME_SAMPLES`-long int16 frames to `on_frame`."""

    def stop_capture(self) -> None: ...

    def play(self, samples: np.ndarray, sample_rate: int = PLAYBACK_SAMPLE_RATE) -> None:
        """Play int16 mono samples, returning once they have been queued."""

    def stop_playback(self) -> None:
        """Cut playback immediately (spec section 6.2: interruptions)."""


class SoundDeviceAudioEngine:
    """PortAudio-backed engine used on Windows."""

    def __init__(
        self,
        input_device: str | None = None,
        output_device: str | None = None,
    ) -> None:
        self._input_device_name = input_device
        self._output_device_name = output_device
        self._stream = None
        self._on_frame: FrameCallback | None = None
        self._lock = threading.RLock()
        self._pending = np.empty(0, dtype=np.int16)

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            return self._stream is not None

    def start_capture(self, on_frame: FrameCallback) -> None:
        with self._lock:
            if self._stream is not None:
                return
            import sounddevice

            inventory = probe_devices()
            if inventory.error:
                raise AudioError(inventory.error)
            if not inventory.has_input:
                raise AudioError(
                    "Windows no ofrece ningún micrófono. Comprueba que está conectado "
                    "y que la aplicación tiene permiso para usarlo."
                )
            device_index = resolve_device(inventory.inputs, self._input_device_name)
            self._on_frame = on_frame
            self._pending = np.empty(0, dtype=np.int16)
            try:
                self._stream = sounddevice.InputStream(
                    samplerate=CAPTURE_SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=FRAME_SAMPLES,
                    device=device_index,
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001 - PortAudio raises broadly
                self._stream = None
                self._on_frame = None
                raise AudioError(f"No se pudo abrir el micrófono: {exc}") from exc

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            # Overflows mean dropped audio, which shows up as missed wake words.
            logger.warning("Estado de captura de audio: %s", status)
        with self._lock:
            callback = self._on_frame
            if callback is None:
                return
            # PortAudio honours blocksize on Windows, but a device that ignores
            # it must not desynchronise the detector, so frames are re-cut here.
            self._pending = np.concatenate([self._pending, indata[:, 0].copy()])
            ready: list[np.ndarray] = []
            while self._pending.size >= FRAME_SAMPLES:
                ready.append(self._pending[:FRAME_SAMPLES].copy())
                self._pending = self._pending[FRAME_SAMPLES:]
        # Deliver outside the lock: the detector must not block the audio thread.
        for frame in ready:
            try:
                callback(frame)
            except Exception:  # noqa: BLE001 - a detector fault must not kill capture
                logger.exception("El consumidor de audio falló procesando un frame.")

    def stop_capture(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._on_frame = None
            self._pending = np.empty(0, dtype=np.int16)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                logger.exception("Fallo al cerrar el flujo de captura.")

    def play(self, samples: np.ndarray, sample_rate: int = PLAYBACK_SAMPLE_RATE) -> None:
        import sounddevice

        inventory = probe_devices()
        if inventory.error:
            raise AudioError(inventory.error)
        if not inventory.has_output:
            raise AudioError("Windows no ofrece ningún dispositivo de salida de audio.")
        device_index = resolve_device(inventory.outputs, self._output_device_name)
        try:
            sounddevice.play(samples, samplerate=sample_rate, device=device_index, blocking=True)
        except Exception as exc:  # noqa: BLE001
            raise AudioError(f"No se pudo reproducir el audio: {exc}") from exc

    def stop_playback(self) -> None:
        try:
            import sounddevice

            sounddevice.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Fallo al detener la reproducción.")


class FakeAudioEngine:
    """In-memory engine for tests and for development off Windows.

    `feed` pushes frames as if they had come from the microphone, so the wake
    word detector and the state machine can be driven from a test with no
    hardware involved.
    """

    def __init__(self) -> None:
        self._on_frame: FrameCallback | None = None
        self.played: list[tuple[np.ndarray, int]] = []
        self.playback_stopped = 0
        self.fail_on_capture: str | None = None

    @property
    def is_capturing(self) -> bool:
        return self._on_frame is not None

    def start_capture(self, on_frame: FrameCallback) -> None:
        if self.fail_on_capture:
            raise AudioError(self.fail_on_capture)
        self._on_frame = on_frame

    def stop_capture(self) -> None:
        self._on_frame = None

    def feed(self, samples: np.ndarray) -> None:
        """Deliver `samples` as whole frames, exactly as PortAudio would."""
        if self._on_frame is None:
            raise AudioError("La captura no está activa.")
        buffer = np.asarray(samples, dtype=np.int16)
        for start in range(0, buffer.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            self._on_frame(buffer[start : start + FRAME_SAMPLES])

    def play(self, samples: np.ndarray, sample_rate: int = PLAYBACK_SAMPLE_RATE) -> None:
        self.played.append((np.asarray(samples), sample_rate))

    def stop_playback(self) -> None:
        self.playback_stopped += 1


def tone(seconds: float = 0.4, frequency: float = 440.0, sample_rate: int = PLAYBACK_SAMPLE_RATE):
    """A short sine used by the speaker check, faded at both ends to avoid a click."""
    count = int(seconds * sample_rate)
    t = np.arange(count, dtype=np.float32) / sample_rate
    wave = 0.25 * np.sin(2 * np.pi * frequency * t)
    fade = min(int(0.02 * sample_rate), count // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]
    return (wave * np.iinfo(np.int16).max).astype(np.int16)
