"""Checking a recording before it is allowed anywhere near training.

A take that is silent, buried in noise, cut off or far too long does not just
fail to help: labelled as "the phrase", it teaches the detector the wrong thing.
Each problem is rejected here with a sentence the teacher can act on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..audio.devices import CAPTURE_SAMPLE_RATE, FRAME_SAMPLES
from ..audio.listener import frame_level

TAKE_SECONDS = 3.0

# Levels are on the 0..1 scale of the microphone meter (-60..0 dBFS).
MIN_PEAK_LEVEL = 0.3  # about -42 dBFS: below this nobody was speaking into it
MIN_CONTRAST = 0.15  # about 9 dB of speech over the room
# Two frames. Enough to reject a click or a tap on the desk, and short enough
# for a one-syllable word: "Chat" spoken normally is about a quarter of a
# second, and a 0.3 s minimum forced a teacher to say "Chat, chat" instead.
MIN_SPEECH_SECONDS = 0.16
# Where speech starts and ends, as a fraction of the way from the room's level
# to the peak. Low on purpose: the "ch" and the final "t" are quiet consonants,
# and a higher bar trimmed them off and made short words look shorter still.
SPEECH_EXTENT = 0.25
MAX_SPEECH_SECONDS = 2.5
MARGIN_SECONDS = 0.1


class RecordingRejected(ValueError):
    """The take cannot be used, and the message says why."""


@dataclass(frozen=True)
class Take:
    """A recording trimmed to the speech in it, as float samples in -1..1."""

    samples: np.ndarray
    peak_level: float

    @property
    def seconds(self) -> float:
        return self.samples.size / CAPTURE_SAMPLE_RATE


def analyse(recording: np.ndarray) -> Take:
    """Find the speech in `recording` (int16 mono) and trim to it."""
    samples = np.asarray(recording, dtype=np.int16).ravel()
    count = samples.size // FRAME_SAMPLES
    if count < 3:
        raise RecordingRejected("La grabación está vacía. Vuelve a intentarlo.")

    frames = samples[: count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES)
    levels = np.array([frame_level(frame) for frame in frames])
    peak = float(levels.max())
    # The room's level, from the quietest frames. A low percentile rather than
    # a median: someone who talks for most of the take would otherwise raise
    # the "floor" to their own voice and be told it was all noise.
    floor = float(np.percentile(levels, 5))

    if peak < MIN_PEAK_LEVEL:
        raise RecordingRejected(
            "Apenas se oye nada. Acércate al micrófono o comprueba en Configuración "
            "que está elegido el correcto."
        )
    if peak - floor < MIN_CONTRAST:
        raise RecordingRejected(
            "No se distingue la voz del ruido de fondo. Prueba en un momento más "
            "tranquilo o más cerca del micrófono."
        )

    speaking = np.flatnonzero(levels >= floor + SPEECH_EXTENT * (peak - floor))
    first, last = int(speaking[0]), int(speaking[-1])

    if last >= count - 1:
        raise RecordingRejected(
            "La frase se ha cortado al final. Empieza a hablar nada más pulsar el botón."
        )
    speech_seconds = (last - first + 1) * FRAME_SAMPLES / CAPTURE_SAMPLE_RATE
    if speech_seconds < MIN_SPEECH_SECONDS:
        raise RecordingRejected("Ha sido demasiado corto. Di la frase completa.")
    if speech_seconds > MAX_SPEECH_SECONDS:
        raise RecordingRejected(
            "Ha sido demasiado largo. Di solo la frase, sin nada antes ni después."
        )

    margin = int(MARGIN_SECONDS * CAPTURE_SAMPLE_RATE)
    start = max(0, first * FRAME_SAMPLES - margin)
    end = min(samples.size, (last + 1) * FRAME_SAMPLES + margin)
    trimmed = samples[start:end].astype(np.float32) / 32768.0
    return Take(samples=trimmed, peak_level=peak)
