"""Streaming playback: the answer plays as it arrives (plan-fase-1, H2).

The Realtime API sends an answer in pieces, faster than real time. Waiting for
the whole of it before playing would add its full length to the wait -- thirty
seconds for the first answer measured in class -- so each piece is queued the
moment it arrives and the speaker drains the queue.

Three promises, each one something a later milestone depends on:

* **No gaps.** Playback starts once a little audio is buffered, so the jitter
  between network pieces does not become stutter. If the queue runs dry mid-
  answer, silence fills in and the answer resumes -- it is never cut short.
* **Stops at once.** An interruption (H4) drops everything queued and aborts
  the stream. The stream is fed in 10 ms blocks, so stopping is bounded by one
  block plus the device's own latency.
* **Knows what was heard.** `played_ms` counts only audio that actually left
  through the speaker, minus the device latency still in flight. H4 sends it
  to the API so the model does not "believe" it said what nobody heard.

`PlaybackBuffer` holds all of that logic and has no device, so it is tested
directly; `SoundDevicePlaybackStream` is the thin PortAudio wrapper around it.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Protocol

import numpy as np

from .resample import StreamingResampler

logger = logging.getLogger(__name__)

# What nearly every Windows output device accepts natively; 24 kHz can be
# refused by some drivers.
DEVICE_RATE = 48_000
# 10 ms per callback: the longest a stop can wait for the current block.
BLOCK_SAMPLES = 480
# Audio buffered before playback starts, to absorb network jitter.
DEFAULT_PREBUFFER_MS = 150


class PlaybackBuffer:
    """A thread-safe queue of int16 samples at the device rate."""

    def __init__(self, rate: int = DEVICE_RATE, prebuffer_ms: int = DEFAULT_PREBUFFER_MS) -> None:
        self.rate = rate
        self._prebuffer = int(rate * prebuffer_ms / 1000)
        self._chunks: deque[np.ndarray] = deque()
        self._queued = 0
        self._lock = threading.Lock()
        self._started = False
        self._finished = False
        self._stopped = False
        self.consumed = 0  # samples that actually went to the device
        self.underruns = 0  # times the queue ran dry mid-answer
        self.done = threading.Event()

    # -- producer side -----------------------------------------------------

    def push(self, pcm: np.ndarray) -> None:
        samples = np.asarray(pcm, dtype=np.int16).ravel()
        if samples.size == 0:
            return
        with self._lock:
            if self._stopped or self._finished:
                return
            self._chunks.append(samples)
            self._queued += samples.size

    def finish(self) -> None:
        """No more audio is coming; play what is queued, then be done."""
        with self._lock:
            self._finished = True
            if self._queued == 0:
                self.done.set()

    def stop(self) -> None:
        """Drop everything queued. Nothing more will play."""
        with self._lock:
            self._stopped = True
            self._chunks.clear()
            self._queued = 0
            self.done.set()

    # -- device side -------------------------------------------------------

    @property
    def queued(self) -> int:
        with self._lock:
            return self._queued

    @property
    def stopped(self) -> bool:
        return self._stopped

    def read(self, count: int) -> np.ndarray:
        """Fill one device block of `count` samples. Never blocks."""
        out = np.zeros(count, dtype=np.int16)
        with self._lock:
            if self._stopped:
                return out
            if not self._started:
                if self._queued < self._prebuffer and not self._finished:
                    return out  # still filling: silence, not counted as heard
                self._started = True

            written = 0
            while written < count and self._chunks:
                chunk = self._chunks[0]
                take = min(count - written, chunk.size)
                out[written : written + take] = chunk[:take]
                written += take
                if take == chunk.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
            self._queued -= written
            self.consumed += written

            if written < count:
                if self._finished:
                    self.done.set()
                else:
                    self.underruns += 1
        return out


class PlaybackStream(Protocol):
    """One answer, playing as it arrives."""

    @property
    def is_active(self) -> bool:
        """True until everything has played or the stream was stopped."""

    @property
    def played_ms(self) -> int:
        """Milliseconds of audio that have actually been heard."""

    def feed(self, pcm: np.ndarray) -> None:
        """Queue int16 samples at the stream's source rate."""

    def finish(self) -> None: ...

    def stop(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the answer has played or was stopped."""


class _BufferedStream:
    """What both implementations share: resampling into a PlaybackBuffer."""

    def __init__(self, source_rate: int, prebuffer_ms: int) -> None:
        self._buffer = PlaybackBuffer(DEVICE_RATE, prebuffer_ms)
        self._resampler = StreamingResampler(source_rate, DEVICE_RATE)
        self._latency_ms = 0.0

    @property
    def buffer(self) -> PlaybackBuffer:
        return self._buffer

    @property
    def is_active(self) -> bool:
        return not self._buffer.done.is_set()

    @property
    def played_ms(self) -> int:
        heard = self._buffer.consumed * 1000 / DEVICE_RATE - self._latency_ms
        return max(0, int(heard))

    def feed(self, pcm: np.ndarray) -> None:
        self._buffer.push(self._resampler.process(pcm))

    def finish(self) -> None:
        self._buffer.finish()

    def wait(self, timeout: float | None = None) -> bool:
        return self._buffer.done.wait(timeout)


class SoundDevicePlaybackStream(_BufferedStream):
    """PortAudio output fed by a PlaybackBuffer."""

    def __init__(
        self,
        source_rate: int,
        device: int | None = None,
        prebuffer_ms: int = DEFAULT_PREBUFFER_MS,
    ) -> None:
        super().__init__(source_rate, prebuffer_ms)
        import sounddevice

        self._sounddevice = sounddevice
        self._stream = sounddevice.OutputStream(
            samplerate=DEVICE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_SAMPLES,
            latency="low",
            device=device,
            callback=self._callback,
            finished_callback=self._buffer.done.set,
        )
        # What the device still holds after a sample leaves the callback: not
        # heard yet, so not counted as heard.
        self._latency_ms = float(self._stream.latency or 0.0) * 1000
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            logger.debug("Estado de reproducción: %s", status)
        outdata[:, 0] = self._buffer.read(frames)
        if self._buffer.done.is_set():
            raise self._sounddevice.CallbackStop

    def stop(self) -> None:
        self._buffer.stop()
        try:
            # abort() discards what the device holds instead of draining it.
            self._stream.abort()
            self._stream.close()
        except Exception:  # noqa: BLE001 - a stream already closed is fine
            logger.debug("El flujo de reproducción ya estaba cerrado.", exc_info=True)

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            logger.debug("No se pudo cerrar el flujo de reproducción.", exc_info=True)


class FakePlaybackStream(_BufferedStream):
    """A speaker driven by the test clock: `advance` plays milliseconds."""

    def __init__(self, source_rate: int, prebuffer_ms: int = DEFAULT_PREBUFFER_MS) -> None:
        super().__init__(source_rate, prebuffer_ms)
        self.heard: list[np.ndarray] = []
        self.stopped_at_ms: int | None = None

    def advance(self, milliseconds: int) -> None:
        blocks = max(1, milliseconds * DEVICE_RATE // 1000 // BLOCK_SAMPLES)
        for _ in range(blocks):
            if self._buffer.done.is_set():
                return
            self.heard.append(self._buffer.read(BLOCK_SAMPLES))

    def play_out(self, limit_ms: int = 600_000) -> None:
        """Advance until everything queued has played.

        Stops, rather than spinning, once nothing is left to play: a stream
        that was never finished would otherwise play silence forever -- which
        is exactly how a real bug once ate a CI runner's memory.
        """
        elapsed = 0
        while not self._buffer.done.is_set() and elapsed < limit_ms:
            if self._buffer.queued == 0 and (
                self._buffer.consumed or self._buffer.stopped or elapsed > 1_000
            ):
                self.advance(10)  # one more block lets a finished stream end
                return
            self.advance(10)
            elapsed += 10
        self.heard = self.heard[-1_000:]  # keep memory bounded in long tests

    def stop(self) -> None:
        self.stopped_at_ms = self.played_ms
        self._buffer.stop()
