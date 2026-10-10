#
# Unit test: EngineTTSService synthesis pacing (TTS_PACING) and eager policy (TTS_EAGER).
#
# Greedy synthesis puts a whole reply on the GPU right after it starts playing, and a
# barge-in throws away everything synthesized past the cut. "lead" pacing holds each
# clause until less than the lead target of synthesized audio is still unplayed;
# "auto" sizes that target from the clause's learned synth time.
# The eager prefix costs ~10% more synthesis per clause and only pays when playout is
# about to run dry, so "first"/"low_lead" ask for it only then. The last test runs a real
# pipeline: a long pacing wait must keep the reply's audio context alive, or the
# context watchdog ends the reply between clauses.
#
# Run: python test_tts_pacing.py   (or via the suite)
#

import asyncio
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the PACKAGE (not just a submodule) before pipecat: teaport_brain/__init__.py
# sets HF_HUB_OFFLINE, and that only guards imports that come after it runs.
import teaport_brain  # noqa: E402, F401
from pinned_pipecat import require_pinned  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402

from teaport_brain import engine_tts  # noqa: E402
from teaport_brain.engine_tts import _SAMPLE_RATE as SR, EngineTTSService  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402

from test_tts_stop_frame import Probe, StubOutput  # noqa: E402


class _Paced(EngineTTSService):
    """No pipeline: `queued` seconds of emitted audio are still to play (_play_end, the
    playout model _note_playout keeps), and context "ctx" has emitted `emitted`."""

    def __init__(self, pacing="lead", lead_s=None, eager="always"):
        super().__init__(voice="af_heart")
        self._pacing, self._lead_s, self._eager = pacing, lead_s, eager
        self.refreshed = 0

    def _refresh_audio_context(self, context_id):
        self.refreshed += 1

    def at(self, queued, emitted=1.0):
        self._play_end = time.monotonic() + queued if queued else 0.0
        self._ctx_audio_secs = {"ctx": emitted} if emitted else {}
        return self


async def test_greedy_never_waits():
    svc = _Paced(pacing="greedy", lead_s=0.1).at(queued=30.0)
    assert await svc._pace("Some clause.", "ctx") == 0.0
    print("  PASS greedy pacing submits at once")


async def test_lead_waits_until_the_queue_drains_to_the_target():
    # 0.5 s queued, target 0.2 s: wait ~0.3 s of playout.
    svc = _Paced(lead_s=0.2).at(queued=0.5)
    waited = await svc._pace("Some clause.", "ctx")
    assert 0.25 <= waited < 0.6, f"waited {waited:.2f}s, want ~0.3"
    assert svc._lead_secs() < 0.2 + 0.02
    assert svc.refreshed >= 1, "the audio context must be kept alive while waiting"
    assert await svc._pace("Next.", "ctx") < 0.05, "below the target: no wait"
    assert await _Paced(lead_s=0.2).at(queued=0)._pace("Idle.", "ctx") < 0.05, \
        "nothing playing: no wait"
    zero = _Paced(lead_s=0.0).at(queued=0.2)
    caught_up = await asyncio.wait_for(zero._pace("Caught up.", "ctx"), 2.0)
    assert 0.15 <= caught_up < 0.5, \
        f"TTS_LEAD_S=0 waited {caught_up:.2f}s, want the playout (~0.2 s)"
    print(f"  PASS lead pacing waited {waited:.2f}s for the queue to drain to the target")


async def test_a_long_wait_is_capped():
    svc = _Paced(lead_s=0.1).at(queued=60.0)
    cap, engine_tts._PACE_MAX_WAIT_S = engine_tts._PACE_MAX_WAIT_S, 0.2
    try:
        waited = await svc._pace("Some clause.", "ctx")
    finally:
        engine_tts._PACE_MAX_WAIT_S = cap
    assert 0.2 <= waited < 1.5, f"waited {waited:.2f}s — the cap must release it"
    print("  PASS a wait longer than the cap is released at the cap")


async def test_barge_in_cancels_a_pacing_wait():
    svc = _Paced(lead_s=0.0).at(queued=20.0)
    task = asyncio.create_task(svc._pace("Some clause.", "ctx"))
    await asyncio.sleep(0.05)
    task.cancel()
    t0 = time.monotonic()
    try:
        await task
        raise AssertionError("the wait finished instead of being cancelled")
    except asyncio.CancelledError:
        pass
    assert time.monotonic() - t0 < 0.1
    print("  PASS a barge-in (task cancel) ends a pacing wait at once")


def test_auto_lead_target_follows_the_learned_synth_time():
    svc = _Paced()
    short, long_ = "Hi there.", "x" * 200
    assert svc._lead_target(short) == engine_tts._LEAD_AUTO_FLOOR_S, "short clause: the floor"
    want = engine_tts._LEAD_AUTO_SYNTH_MULT * 200 * svc._secs_per_char * svc._rtf
    assert abs(svc._lead_target(long_) - want) < 1e-9 and want > engine_tts._LEAD_AUTO_FLOOR_S
    before = svc._lead_target(long_)
    for _ in range(20):                 # a fast GPU: 13 s of audio in 0.4 s
        svc._learn(long_, 13.0, 0.4)
    assert abs(svc._rtf - 0.4 / 13.0) < 0.01 and svc._lead_target(long_) < before
    svc._learn(long_, 0.1, 5.0)         # too short to learn from
    assert abs(svc._rtf - 0.4 / 13.0) < 0.01
    assert _Paced(lead_s=2.5)._lead_target(long_) == 2.5, "a fixed TTS_LEAD_S wins"
    print("  PASS auto lead = max(floor, 2x learned synth time); fixed overrides")


def test_lead_s_parsing():
    parse = engine_tts._parse_lead_s
    assert parse(None) is None and parse("") is None and parse(" Auto ") is None
    assert parse("2.5") == 2.5 and parse("0") == 0.0 and parse("-1") == 0.0
    for bad in ("fast", "inf", "nan"):
        assert parse(bad) is None, f"TTS_LEAD_S={bad!r} must fall back to auto"
    print("  PASS TTS_LEAD_S: auto / seconds (>= 0); a bad value falls back to auto")


class _Driven(_Paced):
    """run_tts driven directly, no pipeline: records when each clause reached the engine."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._sample_rate = SR
        self.submitted = []

    async def start_tts_usage_metrics(self, text):
        pass

    async def add_word_timestamps(self, word_times, context_id=None, **kw):
        pass

    async def _synth_text(self, text, eager=True):
        self.submitted.append(time.monotonic())
        async for ev in _tone_segments(text, eager):
            yield ev


async def test_speech_hold_follows_the_pacing_wait():
    # 0.5 s queued, target 0.2 s: the wait ends at ~0.3 s. The user starts talking at
    # 0.1 s, inside the wait, and stops at 0.7 s: the clause must not go to the GPU
    # before then (hold on), and with TTS_HOLD_ON_USER_SPEECH off goes at ~0.3 s.
    for hold, lo, hi in [(True, 0.65, 1.2), (False, 0.25, 0.6)]:
        svc = _Driven(lead_s=0.2).at(queued=0.5)
        svc._hold_on_user_speech = hold
        t0 = time.monotonic()

        async def drain():
            async for _ in svc.run_tts("Hello there.", "ctx"):
                pass
        task = asyncio.create_task(drain())
        await asyncio.sleep(0.1)
        svc._user_quiet.clear()
        await asyncio.sleep(0.6)
        svc._user_quiet.set()
        await asyncio.wait_for(task, 3.0)
        assert len(svc.submitted) == 1
        at = svc.submitted[0] - t0
        assert lo <= at < hi, f"hold={hold}: clause submitted at {at:.2f}s, want {lo}-{hi}"
    print("  PASS the speech hold applies after the pacing wait; off, it does not hold")


def test_eager_policy():
    clause = "x" * 100
    # (reply opening after silence, mid-reply about to run dry, mid-reply plenty
    # queued, a reply's opening queued behind audio still playing)
    for mode, want in [
        ("always", (True, True, True, True)),
        ("never", (False, False, False, False)),
        ("first", (True, False, False, True)),
        ("low_lead", (True, True, False, False)),
    ]:
        svc = _Paced(eager=mode)
        got = (svc.at(queued=0, emitted=0)._want_eager(clause, "ctx"),
               svc.at(queued=0.01)._want_eager(clause, "ctx"),
               svc.at(queued=30.0)._want_eager(clause, "ctx"),
               svc.at(queued=30.0, emitted=0)._want_eager(clause, "ctx"))
        assert got == want, f"{mode}: got {got}, want {want}"
    saved, engine_tts._STREAM_AUDIO = engine_tts._STREAM_AUDIO, False
    try:
        assert not _Paced(eager="always").at(queued=0)._want_eager(clause, "ctx"), \
            "TTS_STREAM_AUDIO=0 never asks for eager"
    finally:
        engine_tts._STREAM_AUDIO = saved
    print("  PASS eager: always / never / first clause / low lead; off with streaming off")


def test_choice_setting(monkeypatch=None):
    assert setting("TTS_PACING", env={"TTS_PACING": " LEAD "}) == "lead"
    assert setting("TTS_PACING", env={"TTS_PACING": "fast"}) == "greedy"
    assert setting("TTS_PACING", env={"TTS_PACING": ""}) == "greedy"
    print("  PASS choice setting: case-insensitive, bad or empty value -> default")


def _build(pacing):
    """A service built with TTS_PACING=`pacing`: its pacing and the WARNINGs logged."""
    from loguru import logger
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    saved, engine_tts._PACING = engine_tts._PACING, pacing
    calls = []
    real_check = engine_tts._missing_pacing_hooks
    engine_tts._missing_pacing_hooks = lambda tts: calls.append(1) or real_check(tts)
    try:
        tts = EngineTTSService(voice="af_heart")
    finally:
        engine_tts._PACING, engine_tts._missing_pacing_hooks = saved, real_check
        logger.remove(sink)
    return tts._pacing, [w for w in warnings if "TTS_PACING" in w], calls


def test_missing_pipecat_hooks_fall_back_to_greedy():
    """Issue #87: lead pacing calls private pipecat internals; a pipecat without them
    must drop the session to greedy at build, not raise mid-reply."""
    from pipecat.services.tts_service import TTSService
    pacing, warned, _ = _build("lead")
    assert pacing == "lead" and not warned, f"hooks present: {pacing}, {warned}"

    real_refresh, real_init = TTSService._refresh_audio_context, TTSService.__init__
    for how, patch in [
        ("gone", lambda: delattr(TTSService, "_refresh_audio_context")),
        ("without context_id", lambda: setattr(TTSService, "_refresh_audio_context",
                                               lambda self: None)),
    ]:
        patch()
        try:
            pacing, warned, _ = _build("lead")
        finally:
            TTSService._refresh_audio_context = real_refresh
        assert pacing == "greedy" and len(warned) == 1 \
            and "_refresh_audio_context" in warned[0], f"refresh {how}: {pacing}, {warned}"

    def init_without_timeout(self, *a, **kw):
        real_init(self, *a, **kw)
        del self._stop_frame_timeout_s
    TTSService.__init__ = init_without_timeout
    try:
        pacing, warned, _ = _build("lead")
        assert pacing == "greedy" and len(warned) == 1 \
            and "_stop_frame_timeout_s" in warned[0], f"timeout gone: {pacing}, {warned}"
        # Pacing off never looks, so a pipecat bump can't touch a box that doesn't pace.
        pacing, warned, calls = _build("greedy")
        assert pacing == "greedy" and not warned and not calls, \
            f"greedy checked the hooks: {warned}, {calls}"
    finally:
        TTSService.__init__ = real_init
    print("  PASS missing pipecat pacing hook -> warning + greedy; present or greedy -> silent")


# ---- real pipeline: the reply survives a pacing wait longer than the watchdog

REPLY = ("This is the first clause of the reply, and here is a much longer second "
         "clause that follows right after it.")
CLAUSE_SECS = 1.2


async def _tone_segments(text, eager=True):
    n = int(CLAUSE_SECS * SR)
    t = np.arange(n, dtype=np.float32) / SR
    yield "audio", (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32), None
    words = text.split()
    yield "end", [(w, i * CLAUSE_SECS / len(words)) for i, w in enumerate(words)]


async def test_pacing_wait_keeps_the_reply_context_alive():
    tts = EngineTTSService(voice="af_heart")
    eagers = []

    async def synth(text, eager=True):
        eagers.append(eager)
        async for ev in _tone_segments(text, eager):
            yield ev
    tts._synth_text = synth
    tts._pacing, tts._lead_s, tts._eager = "lead", 0.2, "first"
    # Watchdog shorter than the ~1 s pacing wait: without the keepalive the context
    # closes between the clauses and the second clause lands after its stop frame.
    tts._stop_frame_timeout_s = 0.4
    probe, out = Probe(), StubOutput()
    task = PipelineTask(Pipeline([tts, probe, out]), observers=[])
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    await task.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame(REPLY),
                             LLMFullResponseEndFrame()])
    def done():  # both clauses synthesized and a stop frame behind the last audio
        kinds = [type(f) for _, f in probe.seen]
        return (len(eagers) >= 2 and TTSAudioRawFrame in kinds
                and TTSStoppedFrame in kinds[len(kinds) - kinds[::-1].index(TTSAudioRawFrame):])
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not done():
        await asyncio.sleep(0.05)
    await task.cancel()
    await running

    audio = [(ts, f) for ts, f in probe.seen if isinstance(f, TTSAudioRawFrame)]
    stops = [i for i, (_, f) in enumerate(probe.seen) if isinstance(f, TTSStoppedFrame)]
    assert len(eagers) >= 2, f"want a multi-clause reply, got {len(eagers)} clause(s)"
    assert len(stops) == 1, f"{len(stops)} stop frames: the context closed mid-reply"
    assert eagers[0] is True and not any(eagers[1:]), f"eager=first sent {eagers}"
    last_audio = max(i for i, (_, f) in enumerate(probe.seen) if isinstance(f, TTSAudioRawFrame))
    assert stops[0] > last_audio, "the stop frame came before the reply's last audio"
    contexts = {f.context_id for _, f in audio}
    assert len(contexts) == 1, f"the reply's audio split across contexts {contexts}"
    # The second clause waited for playout instead of following the first at once.
    gap = audio[-1][0] - audio[0][0]
    assert gap >= CLAUSE_SECS - 0.2 - 0.3, f"clause 2 followed {gap:.2f}s after clause 1"
    print(f"  PASS {len(eagers)} clauses, one context, one stop frame; clause 2 paced "
          f"{gap:.2f}s behind clause 1 (eager {eagers}), {time.monotonic() - t0:.1f}s total")


def test_tts_pacing():
    require_pinned()
    test_auto_lead_target_follows_the_learned_synth_time()
    test_lead_s_parsing()
    test_eager_policy()
    test_choice_setting()
    test_missing_pipecat_hooks_fall_back_to_greedy()

    async def main():
        await test_greedy_never_waits()
        await test_lead_waits_until_the_queue_drains_to_the_target()
        await test_a_long_wait_is_capped()
        await test_barge_in_cancels_a_pacing_wait()
        await test_speech_hold_follows_the_pacing_wait()
        await test_pacing_wait_keeps_the_reply_context_alive()
    asyncio.run(main())


if __name__ == "__main__":
    test_tts_pacing()
    print("ALL PASS")
