"""Streaming playback (plan-fase-1, H2): no gaps, stops at once, knows what was heard."""

from __future__ import annotations

import threading

import numpy as np

from aiclassroom.audio.engine import FakeAudioEngine
from aiclassroom.audio.playback import (
    BLOCK_SAMPLES,
    DEVICE_RATE,
    FakePlaybackStream,
    PlaybackBuffer,
)

SOURCE_RATE = 24_000


def ramp(samples: int, start: int = 0) -> np.ndarray:
    """Distinct values, so a lost or repeated sample shows up."""
    return (np.arange(start, start + samples) % 30_000).astype(np.int16)


def drain(buffer: PlaybackBuffer, blocks: int) -> np.ndarray:
    return np.concatenate([buffer.read(BLOCK_SAMPLES) for _ in range(blocks)])


# -- the buffer --------------------------------------------------------------


def test_what_goes_in_comes_out_in_order_across_blocks():
    buffer = PlaybackBuffer(prebuffer_ms=0)
    pieces = [ramp(1_000, 0), ramp(333, 1_000), ramp(2_000, 1_333)]
    for piece in pieces:
        buffer.push(piece)
    buffer.finish()

    out = drain(buffer, 10)[:3_333]
    assert np.array_equal(out, np.concatenate(pieces))
    assert buffer.done.is_set()


def test_playback_waits_for_a_little_audio_before_starting():
    """Network jitter must not become stutter at the start of an answer."""
    buffer = PlaybackBuffer(prebuffer_ms=150)
    buffer.push(ramp(DEVICE_RATE // 10))  # 100 ms: not enough yet

    assert not drain(buffer, 3).any()
    assert buffer.consumed == 0  # silence while filling is not "heard"

    buffer.push(ramp(DEVICE_RATE // 10, 4_800))  # now 200 ms queued
    assert drain(buffer, 1).any()


def test_a_short_answer_plays_even_below_the_prebuffer():
    buffer = PlaybackBuffer(prebuffer_ms=150)
    buffer.push(ramp(1_000))
    buffer.finish()

    assert drain(buffer, 3)[:1_000].any()
    assert buffer.done.is_set()


def test_running_dry_mid_answer_pauses_instead_of_ending():
    """A slow network piece must not cut the answer short."""
    buffer = PlaybackBuffer(prebuffer_ms=0)
    buffer.push(ramp(BLOCK_SAMPLES))
    drain(buffer, 3)

    assert not buffer.done.is_set()
    assert buffer.underruns >= 1

    buffer.push(ramp(BLOCK_SAMPLES, 7))
    buffer.finish()
    resumed = drain(buffer, 2)
    assert np.array_equal(resumed[:BLOCK_SAMPLES], ramp(BLOCK_SAMPLES, 7))
    assert buffer.done.is_set()


def test_stopping_drops_everything_queued_at_once():
    buffer = PlaybackBuffer(prebuffer_ms=0)
    buffer.push(ramp(DEVICE_RATE * 30))  # thirty seconds queued
    drain(buffer, 5)

    buffer.stop()

    assert buffer.done.is_set()
    assert buffer.queued == 0
    assert not drain(buffer, 3).any()
    buffer.push(ramp(1_000))  # a late piece of a cancelled answer
    assert buffer.queued == 0


def test_only_audio_that_went_to_the_device_counts_as_heard():
    buffer = PlaybackBuffer(prebuffer_ms=0)
    buffer.push(ramp(DEVICE_RATE))
    drain(buffer, 25)  # 250 ms
    assert buffer.consumed == 25 * BLOCK_SAMPLES


def test_the_buffer_is_safe_to_feed_while_the_device_reads():
    buffer = PlaybackBuffer(prebuffer_ms=0)
    total = 200 * 480

    def feed():
        for index in range(200):
            buffer.push(ramp(480, index * 480))
        buffer.finish()

    thread = threading.Thread(target=feed)
    thread.start()
    while not buffer.done.is_set():
        buffer.read(BLOCK_SAMPLES)
    thread.join()

    # Every sample fed was played exactly once, whatever the interleaving.
    assert buffer.consumed == total


# -- a stream: resampling and what was heard ---------------------------------


def test_a_24k_answer_plays_at_the_device_rate_without_losing_time():
    stream = FakePlaybackStream(SOURCE_RATE, prebuffer_ms=0)
    stream.feed(np.full(SOURCE_RATE, 1000, dtype=np.int16))  # one second
    stream.finish()
    stream.play_out()

    heard = np.concatenate(stream.heard)
    assert abs(np.count_nonzero(heard) - DEVICE_RATE) <= 2
    assert abs(stream.played_ms - 1000) <= 1


def test_a_thirty_second_answer_arriving_in_pieces_plays_whole():
    """H2's own criterion: thirty seconds, in pieces, with no cuts."""
    stream = FakePlaybackStream(SOURCE_RATE)
    piece = SOURCE_RATE // 5  # 200 ms per network piece
    for index in range(150):
        stream.feed(np.full(piece, 1000 + index, dtype=np.int16))
        stream.advance(150)  # the network is a little faster than real time
    stream.finish()
    stream.play_out()

    assert not stream.is_active
    assert abs(stream.played_ms - 30_000) <= 5


def test_stopping_mid_answer_reports_how_much_was_heard():
    stream = FakePlaybackStream(SOURCE_RATE, prebuffer_ms=0)
    stream.feed(np.full(SOURCE_RATE * 10, 1000, dtype=np.int16))
    stream.advance(1_250)
    stream.stop()

    assert not stream.is_active
    assert abs(stream.stopped_at_ms - 1_250) <= 10
    played = stream.played_ms
    stream.advance(500)
    assert stream.played_ms == played  # nothing more after the stop


# -- the engine --------------------------------------------------------------


def test_a_streaming_answer_counts_as_playing_for_the_echo_guard():
    """Risk R-6: the detector must know the assistant is speaking."""
    engine = FakeAudioEngine()
    stream = engine.open_playback(SOURCE_RATE)
    stream.feed(np.full(SOURCE_RATE, 1000, dtype=np.int16))
    assert engine.is_playing

    engine.stop_playback()
    assert not engine.is_playing


def test_a_new_answer_replaces_the_one_still_playing():
    engine = FakeAudioEngine()
    first = engine.open_playback(SOURCE_RATE)
    first.feed(np.full(SOURCE_RATE, 1000, dtype=np.int16))
    second = engine.open_playback(SOURCE_RATE)

    assert not first.is_active
    assert second.is_active
