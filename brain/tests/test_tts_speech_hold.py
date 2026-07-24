#
# Unit test: EngineTTSService's GPU-yield hold during user speech.
#
# Kokoro synthesis and Voxtral STT share one CUDA context in the engine, and
# synthesis starves transcription exactly when a barge-in needs words FAST
# (measured live 2026-07-21). The service tracks VAD speech via SystemFrames
# and run_tts holds before each clause while the user is speaking, capped by
# _speech_hold_max_s so sustained non-barge speech can't stall the reply.
#
# Run: python test_tts_speech_hold.py   (or via pytest)
#

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipecat.frames.frames import (  # noqa: E402
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.engine_tts import EngineTTSService  # noqa: E402


def make_service():
    svc = EngineTTSService(voice="af_heart")

    async def swallow(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    svc.push_frame = swallow
    return svc


async def test_vad_frames_toggle_quiet_state():
    svc = make_service()
    assert svc._user_quiet.is_set(), "starts quiet"
    await svc.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert not svc._user_quiet.is_set(), "VAD start → speaking"
    await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert svc._user_quiet.is_set(), "VAD stop → quiet"
    print("  PASS VAD frames toggle the quiet state")


async def test_hold_noop_when_quiet():
    svc = make_service()
    t0 = time.monotonic()
    await svc._hold_for_user_speech()
    assert time.monotonic() - t0 < 0.05, "no hold while quiet"
    print("  PASS hold is a no-op while the user is quiet")


async def test_hold_releases_on_vad_stop():
    svc = make_service()
    svc._user_quiet.clear()  # user speaking

    async def vad_stop_later():
        await asyncio.sleep(0.15)
        await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    release = asyncio.create_task(vad_stop_later())
    t0 = time.monotonic()
    await svc._hold_for_user_speech()
    held = time.monotonic() - t0
    await release
    assert 0.1 <= held < 1.0, f"held {held:.2f}s — should release at VAD stop"
    print("  PASS hold releases when VAD reports the user stopped")


async def test_hold_capped_when_user_keeps_talking():
    svc = make_service()
    svc._user_quiet.clear()          # user speaking, never stops
    svc._speech_hold_max_s = 0.2     # tight cap for the test
    t0 = time.monotonic()
    await svc._hold_for_user_speech()
    held = time.monotonic() - t0
    assert 0.15 <= held < 1.0, f"held {held:.2f}s — should resume at the cap"
    print("  PASS hold resumes at the cap under sustained speech")


def test_tts_speech_hold():
    async def main():
        await test_vad_frames_toggle_quiet_state()
        await test_hold_noop_when_quiet()
        await test_hold_releases_on_vad_stop()
        await test_hold_capped_when_user_keeps_talking()
    asyncio.run(main())


if __name__ == "__main__":
    test_tts_speech_hold()
    print("ALL PASS")
