"""Streaming sample-rate conversion, shared by capture and playback.

Audio moves in small pieces -- 80 ms from the microphone, a few hundred from
the API -- and each piece is converted as it arrives. Converting pieces on
their own would put a small step at every boundary, a faint click several
times a second, so the converter keeps state between them.
"""

from __future__ import annotations

import numpy as np


class StreamingResampler:
    """Linear interpolation, carried across chunks.

    Linear interpolation is not what a mastering engineer would pick, but for
    speech it is inaudible, needs no filter design and costs nothing on a
    classroom PC. What matters is that the output is exactly what one long
    resample would have produced, chunk boundaries or not -- and that is what
    the tests hold it to.

    The defaults are the microphone (16 kHz) to the Realtime API (24 kHz).
    """

    def __init__(self, source_rate: int = 16_000, target_rate: int = 24_000) -> None:
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
