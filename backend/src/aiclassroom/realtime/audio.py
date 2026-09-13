"""Audio between the microphone (16 kHz) and the Realtime API (24 kHz).

The detector needs 16 kHz and the API only accepts 24 kHz PCM, so every frame
sent during a turn is resampled on the way out. It happens in 80 ms pieces as
they arrive, which is why the resampler keeps state: resampling each piece on
its own would put a small discontinuity at every boundary, a faint click twelve
times a second.
"""

from __future__ import annotations

import base64

import numpy as np

MICROPHONE_RATE = 16_000
REALTIME_RATE = 24_000


class StreamingResampler:
    """Linear interpolation, carried across chunks.

    Linear interpolation is not what a mastering engineer would pick, but for
    speech going into a speech model it is inaudible, needs no filter design
    and costs nothing on a classroom PC. What matters is that the output is
    exactly what one long resample would have produced, chunk boundaries or
    not -- and that is what the tests hold it to.
    """

    def __init__(
        self, source_rate: int = MICROPHONE_RATE, target_rate: int = REALTIME_RATE
    ) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("Las frecuencias de muestreo deben ser positivas.")
        self._step = source_rate / target_rate  # input samples per output sample
        self._position = 0.0  # next output sample, in input-sample coordinates
        self._previous: float | None = None  # last input sample of the last chunk

    def reset(self) -> None:
        self._position = 0.0
        self._previous = None

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """Resample an int16 chunk; returns int16."""
        samples = np.asarray(chunk, dtype=np.float64).ravel()
        if samples.size == 0:
            return np.empty(0, dtype=np.int16)

        # Prepend the previous chunk's last sample so interpolation can span
        # the boundary. Coordinates then start at -1 for that carried sample.
        if self._previous is None:
            joined = samples
            offset = 0.0
        else:
            joined = np.concatenate([[self._previous], samples])
            offset = 1.0

        last_index = samples.size - 1  # in this chunk's coordinates
        positions = np.arange(self._position, last_index + 1e-9, self._step)
        if positions.size:
            output = np.interp(positions + offset, np.arange(joined.size), joined)
            self._position = positions[-1] + self._step - samples.size
        else:
            output = np.empty(0)
            self._position -= samples.size

        self._previous = float(samples[-1])
        return np.clip(np.round(output), -32768, 32767).astype(np.int16)


def to_base64(pcm: np.ndarray) -> str:
    """int16 samples as the base64 little-endian PCM the API expects."""
    return base64.b64encode(np.asarray(pcm, dtype="<i2").tobytes()).decode("ascii")


def from_base64(payload: str) -> np.ndarray:
    """The API's base64 PCM as int16 samples."""
    raw = base64.b64decode(payload)
    if len(raw) % 2:
        raw = raw[:-1]  # a torn final byte is dropped, not allowed to shift the rest
    return np.frombuffer(raw, dtype="<i2").astype(np.int16)
