#
# Unit test: the caller-audio tap records what arrived, and never breaks the call.
#
# Measured live 2026-09-09, per second of SIP call:
#
#     VAD sees speech start      bot-speaking 0.087   bot-quiet 0.112   (1.3x)
#     STT produced an interim    bot-speaking 0.009   bot-quiet 0.165   (18.6x)
#
# The caller's audio arrives during bot playout and Silero calls it speech; the engine's
# STT transcribes almost none of it. Not the echo canceller — caller finals per second
# while the bot spoke measured 0.012 / 0.011 / 0.009 at pjmedia default, conservative,
# and `aec = false`, i.e. invariant across the entire range. So the remaining question is
# about the BYTES, and this tap records them instead of inferring a fourth theory.
#
# What matters in a capture is finding the double-talk seconds inside a ten-minute file,
# so the marks are SAMPLE OFFSETS into the wav itself — no clock arithmetic, no
# dependence on log timestamps lining up with audio.
#
# Run: python test_audio_dump.py   (or via the suite)
#

import asyncio
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
import teaport_brain.audio_dump as ad  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

SR = 16000
CHUNK = b"\x11\x22" * 320          # 320 samples = 20 ms


def _tap(tmp, call_id="c1"):
    ad.DUMP_DIR = tmp
    tap = ad.CallerAudioTap(call_id, sample_rate=SR)
    tap.pushed = []

    async def push(frame, direction=None):
        tap.pushed.append(frame)
    tap.push_frame = push
    return tap


async def _send(tap, frame):
    await ad.CallerAudioTap.process_frame(tap, frame, FrameDirection.DOWNSTREAM)


async def test_caller_audio_lands_in_the_wav():
    with tempfile.TemporaryDirectory() as tmp:
        tap = _tap(tmp)
        for _ in range(5):
            await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))
        await tap.cleanup()
        with wave.open(tap._wav_path) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == SR
            assert w.getnframes() == 5 * 320, w.getnframes()


async def test_bot_spans_are_marked_at_the_sample_offset():
    with tempfile.TemporaryDirectory() as tmp:
        tap = _tap(tmp)
        await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))
        await _send(tap, BotStartedSpeakingFrame())
        await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))
        await _send(tap, BotStoppedSpeakingFrame())
        await tap.cleanup()
        with open(tap._marks_path) as f:
            marks = [ln.split() for ln in f if not ln.startswith("#")]
        assert marks == [["320", "BOT_START"], ["640", "BOT_STOP"]], marks


async def test_every_frame_is_forwarded_including_the_audio():
    # A tap that swallowed frames would silence the call it is measuring.
    with tempfile.TemporaryDirectory() as tmp:
        tap = _tap(tmp)
        sent = [InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1),
                BotStartedSpeakingFrame(), TextFrame("hello")]
        for f in sent:
            await _send(tap, f)
        await tap.cleanup()
        assert len(tap.pushed) == len(sent), f"{len(tap.pushed)} of {len(sent)} forwarded"


async def test_the_cap_stops_recording_but_not_the_call():
    with tempfile.TemporaryDirectory() as tmp:
        real, ad.MAX_SECS = ad.MAX_SECS, 640 / SR      # room for exactly two chunks
        try:
            tap = _tap(tmp)
            for _ in range(6):
                await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR,
                                                    num_channels=1))
            await tap.cleanup()
        finally:
            ad.MAX_SECS = real
        with wave.open(tap._wav_path) as w:
            assert w.getnframes() == 640, w.getnframes()
        assert "CAP reached" in open(tap._marks_path).read()
        assert len(tap.pushed) == 6, "frames must keep flowing after the cap"


async def test_a_write_failure_disables_the_tap_and_keeps_the_call():
    with tempfile.TemporaryDirectory() as tmp:
        tap = _tap(tmp)
        tap._wav.close()                # writes will now raise ValueError
        await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))
        await _send(tap, InputAudioRawFrame(audio=CHUNK, sample_rate=SR, num_channels=1))
        assert tap._wav is None, "a failing tap must disable itself"
        assert len(tap.pushed) == 2, "...and must not drop the caller's audio doing it"
        await tap.cleanup()


async def test_an_unwritable_directory_is_not_fatal():
    tap = ad.CallerAudioTap.__new__(ad.CallerAudioTap)
    ad.DUMP_DIR = "/proc/teaport-cannot-exist"
    tap = ad.CallerAudioTap("c9", sample_rate=SR)     # must not raise
    assert tap._wav is None


def main():
    async def run_all():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {name}")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
    print("ALL PASS")
