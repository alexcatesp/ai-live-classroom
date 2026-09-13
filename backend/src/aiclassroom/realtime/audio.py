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

# Lives with the audio engine, which also needs it for playback (H2).
from ..audio.resample import StreamingResampler  # noqa: F401 - re-exported

MICROPHONE_RATE = 16_000
REALTIME_RATE = 24_000


def to_base64(pcm: np.ndarray) -> str:
    """int16 samples as the base64 little-endian PCM the API expects."""
    return base64.b64encode(np.asarray(pcm, dtype="<i2").tobytes()).decode("ascii")


def from_base64(payload: str) -> np.ndarray:
    """The API's base64 PCM as int16 samples."""
    raw = base64.b64decode(payload)
    if len(raw) % 2:
        raw = raw[:-1]  # a torn final byte is dropped, not allowed to shift the rest
    return np.frombuffer(raw, dtype="<i2").astype(np.int16)
