#
# Unit test: EngineTTSService synthesis pacing (TTS_PACING) and eager policy (TTS_EAGER).
#
# Greedy synthesis puts a whole reply on the GPU right after it starts playing, and a
# barge-in throws away everything synthesized past the cut. "lead" pacing holds each
# clause until less than the lead target of synthesized audio is still unplayed;
# "auto" sizes that target from the clause's learned synth time.
# The eager prefix costs ~10% more synthesis per clause and only pays when playout is
# about to run dry, so "first"/"low_lead" ask for it only then. "lead" falls back to greedy
# on a pipecat without the hooks it needs (however pipecat writes them), and /health
# and `teaport doctor` say so, as they do for a value that is not a pacing (issues #87,
# #138). The last test runs a real pipeline: a long pacing wait must keep the reply's
# audio context alive, or the context watchdog ends the reply between clauses; with
# the keepalive a no-op, it does.
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
    # An instance whose watchdog timeout is not a positive number (#87 review): the
    # wait still runs, refreshing on a 0.5 s step, instead of raising mid-reply.
    for bad in (None, 0, -1.0):
        odd = _Paced(lead_s=0.0).at(queued=0.2)
        odd._stop_frame_timeout_s = bad
        waited_odd = await asyncio.wait_for(odd._pace("Odd.", "ctx"), 2.0)
        assert 0.15 <= waited_odd < 0.5 and odd.refreshed >= 1, \
            f"timeout {bad!r}: waited {waited_odd:.2f}s, {odd.refreshed} refreshes"
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
    """A session built with TTS_PACING=`pacing` (None: unset) in what is, to
    pacing_status, a fresh process: its pacing, the WARNINGs logged, how many times the
    pipecat hooks were checked, and /health's "tts_pacing" (issue #138), which must
    agree with it."""
    from fastapi.testclient import TestClient
    from loguru import logger
    from teaport_brain import gateway_server
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    saved = os.environ.pop("TTS_PACING", None)
    if pacing is not None:
        os.environ["TTS_PACING"] = pacing
    calls = []
    real_check = engine_tts._missing_pacing_hooks
    engine_tts._missing_pacing_hooks = lambda: calls.append(1) or real_check()
    engine_tts.pacing_status.cache_clear()
    try:
        tts = EngineTTSService(voice="af_heart")
        health = TestClient(gateway_server.app).get("/health").json()["tts_pacing"]
    finally:
        os.environ.pop("TTS_PACING", None)
        if saved is not None:
            os.environ["TTS_PACING"] = saved
        engine_tts._missing_pacing_hooks = real_check
        engine_tts.pacing_status.cache_clear()
        logger.remove(sink)
    # One answer: the session and /health read the same check, made once, and the
    # two differ from what was set exactly when /health says why.
    assert health["effective"] == tts._pacing and health["pipecat"] not in ("", "unknown") \
        and (health["configured"] != tts._pacing) == bool(health["missing"]
                                                          or health["invalid"]), \
        f"health {health} vs {tts._pacing}"
    assert len(calls) <= 1, f"the hooks were checked {len(calls)} times, not once"
    return tts._pacing, [w for w in warnings if "TTS_PACING" in w], calls, health


_MARK = {"✓": "OK", "!": "WARN", "✗": "FAIL"}


def _doctor_run(health_json, answers=True, etc=None):
    """`teaport doctor` (cli/teaport cmd_doctor, its functions as they are: everything
    above its dispatch, as test_cli_sip_conf.py takes them) on a box with nothing
    installed (the prefix, secrets and, unless `etc` is given, /etc/teaport are empty
    temp dirs) whose brain's /health is `health_json`: its exit code and its checks as
    "OK|WARN|FAIL text". Only brain_health and the probes of units and ports are
    stubbed, so no real service is asked."""
    import json
    import pathlib
    import re
    import shlex
    import subprocess
    from tempdirs import tempdir
    text = (pathlib.Path(__file__).resolve().parents[2] / "cli" / "teaport").read_text()
    stub = (f"brain_health() {{ printf '%s' {shlex.quote(json.dumps(health_json))}; }}"
            if answers else "brain_health() { return 7; }")
    script = (text[:text.index('cmd="${1:-}"; shift || true')] + stub + "\n"
              "active() { echo inactive; }; installed() { return 1; }\n"
              "sip_enabled() { return 1; }; listening() { return 1; }\n"
              "cmd_doctor\n")
    home = tempdir("teaport-doctor-")
    env = dict(os.environ, HOME=home, TEAPORT_SECRETS_DIR=home,
               TEAPORT_PREFIX=os.path.join(home, "opt"),
               TEAPORT_ETC=etc or os.path.join(home, "etc"))
    env.pop("LLM_API_KEY", None)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    checks = [f"{_MARK[m[1]]} {m[2]}" for m in
              re.finditer(r"^  \x1b\[[\d;]+m(.)\x1b\[0m (.*)$", p.stdout, re.M)]
    assert "doctor: " in p.stdout and not p.stderr, p.stdout + p.stderr
    return p.returncode, checks


def _doctor(health_json, answers=True):
    """What `teaport doctor` says about pacing (brain_pacing_check), "" for nothing."""
    said = [c for c in _doctor_run(health_json, answers)[1] if "pacing" in c.lower()]
    assert len(said) <= 1, said
    return said[0] if said else ""


async def _async_refresh(self, context_id):
    pass


def test_missing_pipecat_hooks_fall_back_to_greedy():
    """Issue #87: lead pacing calls private pipecat internals; a pipecat without them
    must drop the session to greedy at build, not raise mid-reply. Issue #138: and
    /health must say so (_build checks it agrees with the session every time)."""
    from pipecat.services.tts_service import TTSService
    pacing, warned, calls, health = _build("lead")
    assert pacing == "lead" and not warned and len(calls) == 1, \
        f"hooks present: {pacing}, {warned}, {calls}"
    assert {k: health[k] for k in ("configured", "effective", "invalid", "missing")} == \
        {"configured": "lead", "effective": "lead", "invalid": None, "missing": []}, health
    assert _doctor({"ok": True, "tts_pacing": health}) == "OK TTS pacing: lead"
    # A brain that does not answer, or predates the field: doctor says nothing here.
    assert _doctor(None, answers=False) == "" and _doctor({"ok": True}) == ""

    real_refresh, real_init = TTSService._refresh_audio_context, TTSService.__init__
    real_watchdog = TTSService._handle_audio_context
    for how, patch in [
        ("gone", lambda: delattr(TTSService, "_refresh_audio_context")),
        ("without context_id", lambda: setattr(TTSService, "_refresh_audio_context",
                                               lambda self: None)),
        ("async", lambda: setattr(TTSService, "_refresh_audio_context",
                                  _async_refresh)),
    ]:
        patch()
        try:
            pacing, warned, _, health = _build("lead")
        finally:
            TTSService._refresh_audio_context = real_refresh
        assert pacing == "greedy" and len(warned) == 1 \
            and "_refresh_audio_context" in warned[0], f"refresh {how}: {pacing}, {warned}"
        assert health["missing"] == ["no sync _refresh_audio_context(context_id)"], health
        said = _doctor({"ok": True, "tts_pacing": health})
        assert said.startswith(
            f"WARN TTS_PACING=lead, but the brain paces greedy: it does not fit pipecat "
            f"{health['pipecat']} (no sync _refresh_audio_context(context_id)) — "), said

    # The watchdog must still time out on the timeout and skip the keepalive. (Both
    # still end a context the way _require_context_end_hook asks.)
    async def watchdog_without_keepalive(self, context_id):
        await asyncio.wait_for(asyncio.sleep(1), timeout=self._stop_frame_timeout_s)
        await self._maybe_reset_word_timestamps(context_id)

    async def watchdog_without_timeout(self, context_id):
        await self._maybe_reset_word_timestamps(context_id)
        return TTSService._CONTEXT_KEEPALIVE

    for marker, watchdog in [("_CONTEXT_KEEPALIVE", watchdog_without_keepalive),
                             ("_stop_frame_timeout_s", watchdog_without_timeout)]:
        TTSService._handle_audio_context = watchdog
        try:
            pacing, warned, _, health = _build("lead")
        finally:
            TTSService._handle_audio_context = real_watchdog
        assert pacing == "greedy" and len(warned) == 1 and marker in warned[0], \
            f"watchdog without {marker}: {pacing}, {warned}"
        assert health["missing"] == \
            [f"its watchdog (_handle_audio_context) no longer uses {marker}"], health

    # A constructor that does not keep the timeout it is given: it ignores it, or
    # stores something _pace cannot divide. Or one that no longer takes it at all.
    def init_ignores_it(self, *a, stop_frame_timeout_s=3.0, **kw):
        real_init(self, *a, **kw)

    def init_stores_none(self, *a, stop_frame_timeout_s=3.0, **kw):
        real_init(self, *a, **kw)
        self._stop_frame_timeout_s = None

    def init_without_it(self, *a, **kw):
        if "stop_frame_timeout_s" in kw:
            raise TypeError("unexpected keyword argument 'stop_frame_timeout_s'")
        real_init(self, *a, **kw)

    for init, want in [
        (init_ignores_it, "its constructor does not keep stop_frame_timeout_s in "
                          "_stop_frame_timeout_s"),
        (init_stores_none, "its constructor does not keep stop_frame_timeout_s in "
                           "_stop_frame_timeout_s"),
        (init_without_it, "TTSService(stop_frame_timeout_s=...) raises TypeError("
                          "\"unexpected keyword argument 'stop_frame_timeout_s'\")"),
    ]:
        TTSService.__init__ = init
        try:
            assert engine_tts._missing_pacing_hooks() == (want,), init.__name__
            if init is init_ignores_it:
                pacing, warned, _, health = _build("lead")
                assert pacing == "greedy" and len(warned) == 1 \
                    and "_stop_frame_timeout_s" in warned[0], f"{pacing}, {warned}"
                assert health["missing"] == [want], health
                # Pacing off never looks, so a pipecat bump can't touch a box that
                # doesn't pace, and /health reports it plainly.
                pacing, warned, calls, health = _build("greedy")
                assert pacing == "greedy" and not warned and not calls, \
                    f"greedy checked the hooks: {warned}, {calls}"
                assert (health["configured"], health["effective"], health["missing"]) == \
                    ("greedy", "greedy", []), health
                assert _doctor({"ok": True, "tts_pacing": health}) == "OK TTS pacing: greedy"
        finally:
            TTSService.__init__ = real_init
    print("  PASS missing pipecat pacing hook -> warning + greedy, /health names it, "
          "doctor warns; greedy -> never looks")


def test_hook_check_sees_how_pipecat_writes_them():
    """A pipecat that still has every hook, written another way, fits: the timeout set
    through a setattr loop, a closure, an __init__ decorated without functools.wraps,
    a helper method or a base class; the watchdog's names in a closure or behind
    functools.wraps; the keepalive wrapped or its argument renamed; another method made
    abstract. Read from __init__'s bytecode, each of the first four read as "missing"
    (a false alarm in doctor). The watchdog is read from its own code alone, so one
    that stopped skipping the keepalive is caught even when it calls the method that
    queues it (PR #147 review), at the price of a false "missing" for one whose names
    moved into a helper: that only costs pacing's savings."""
    import functools
    import inspect
    from abc import abstractmethod
    from pipecat.services.tts_service import TTSService
    real_init, real_watchdog = TTSService.__init__, TTSService._handle_audio_context
    real_refresh = TTSService._refresh_audio_context

    def no_wraps(f):
        def wrapper(*a, **kw):
            return f(*a, **kw)
        return wrapper

    def init_setattr_loop(self, *a, stop_frame_timeout_s=3.0, **kw):
        real_init(self, *a, **kw)
        for name, value in [("_stop_frame_timeout_s", stop_frame_timeout_s)]:
            setattr(self, name, value)

    def init_closure(self, *a, stop_frame_timeout_s=3.0, **kw):
        real_init(self, *a, **kw)

        def keep():
            self._stop_frame_timeout_s = stop_frame_timeout_s
        keep()

    @no_wraps
    def init_decorated(self, *a, stop_frame_timeout_s=None, **kw):
        real_init(self, *a, **kw)
        self._stop_frame_timeout_s = stop_frame_timeout_s

    def init_helper(self, *a, stop_frame_timeout_s=3.0, **kw):
        real_init(self, *a, **kw)
        self._keep_timeout(stop_frame_timeout_s)

    async def watchdog_closure(self, context_id):
        def is_keepalive(frame):
            return frame is TTSService._CONTEXT_KEEPALIVE
        return is_keepalive(await asyncio.wait_for(asyncio.sleep(0),
                                                   timeout=self._stop_frame_timeout_s))

    def refresh_renamed(self, ctx):
        pass

    @functools.wraps(real_watchdog)
    async def watchdog_wrapped(self, context_id):
        return await real_watchdog(self, context_id)

    cases = [("__init__", init_setattr_loop), ("__init__", init_closure),
             ("__init__", init_decorated), ("__init__", init_helper),
             ("_handle_audio_context", watchdog_closure),
             ("_handle_audio_context", watchdog_wrapped),
             ("_refresh_audio_context", no_wraps(real_refresh)),
             ("_refresh_audio_context", refresh_renamed)]
    TTSService._keep_timeout = lambda self, t: setattr(self, "_stop_frame_timeout_s", t)
    real_getsource = inspect.getsource

    def no_source(obj):  # a stripped install: bytecode only
        raise OSError("no source")
    inspect.getsource = no_source
    try:
        for name, f in cases:
            setattr(TTSService, name, f)
            try:
                got = engine_tts._missing_pacing_hooks()
            finally:
                TTSService.__init__, TTSService._handle_audio_context = real_init, real_watchdog
                TTSService._refresh_audio_context = real_refresh
            assert got == (), f"{name} as {f.__qualname__}: {got}"
        # ...and without .py sources the check still sees a watchdog that no longer
        # skips the keepalive: one that never names it, one that hands it downstream
        # and then queues another itself (through self, or through another object),
        # and one whose names moved into a helper (read as missing: errs to greedy).
        async def watchdog_without_keepalive(self, context_id):
            await asyncio.wait_for(asyncio.sleep(0), timeout=self._stop_frame_timeout_s)

        async def watchdog_refreshes(self, context_id):
            frame = await asyncio.wait_for(self._audio_contexts[context_id].get(),
                                           timeout=self._stop_frame_timeout_s)
            await self.push_frame(frame)   # the keepalive too: not skipped
            self._refresh_audio_context(context_id)

        async def watchdog_refreshes_elsewhere(self, context_id):
            frame = await asyncio.wait_for(self._audio_contexts[context_id].get(),
                                           timeout=self._stop_frame_timeout_s)
            await self.push_frame(frame)
            self._helper_obj._refresh_audio_context(context_id)

        async def watchdog_helper(self, context_id):
            return await self._next_frame(context_id)

        async def next_frame(self, context_id):
            frame = await asyncio.wait_for(asyncio.sleep(0),
                                           timeout=self._stop_frame_timeout_s)
            return frame is TTSService._CONTEXT_KEEPALIVE
        TTSService._next_frame = next_frame
        for watchdog in (watchdog_without_keepalive, watchdog_refreshes,
                         watchdog_refreshes_elsewhere, watchdog_helper):
            TTSService._handle_audio_context = watchdog
            try:
                got = engine_tts._missing_pacing_hooks()
            finally:
                TTSService._handle_audio_context = real_watchdog
            assert "its watchdog (_handle_audio_context) no longer uses " \
                   "_CONTEXT_KEEPALIVE" in got, f"{watchdog.__name__}: {got}"
    finally:
        inspect.getsource = real_getsource
        del TTSService._keep_timeout
        if hasattr(TTSService, "_next_frame"):
            del TTSService._next_frame

    # A pipecat that makes another method abstract: the probe stubs every one, so
    # that is not "TTSService(...) raises".
    class _MoreAbstract(TTSService):
        @abstractmethod
        async def flush_audio(self):
            ...
    assert engine_tts._missing_pacing_hooks(_MoreAbstract) == (), \
        engine_tts._missing_pacing_hooks(_MoreAbstract)

    # A keepalive only callable by keyword: _pace passes the context id positionally.
    TTSService._refresh_audio_context = lambda self, *, context_id: None
    try:
        assert engine_tts._missing_pacing_hooks() == (
            "no sync _refresh_audio_context(context_id)",)
    finally:
        TTSService._refresh_audio_context = real_refresh

    # pipecat moving the timeout into a base class's __init__ (a mixin) still fits.
    class _TimeoutBase:
        def __init__(self, *, stop_frame_timeout_s: float = 2.0, **kw):
            self._stop_frame_timeout_s = stop_frame_timeout_s

    class _Split(_TimeoutBase):
        _refresh_audio_context = TTSService._refresh_audio_context
        _handle_audio_context = TTSService._handle_audio_context

        def __init__(self, *, push_stop_frames: bool = False, **kw):
            super().__init__(**kw)

    class _Neither:
        def __init__(self, **kw):
            pass

    class _SplitWithout(_Neither):
        _refresh_audio_context = TTSService._refresh_audio_context
        _handle_audio_context = TTSService._handle_audio_context

    assert engine_tts._missing_pacing_hooks(_Split) == (), \
        engine_tts._missing_pacing_hooks(_Split)
    assert engine_tts._missing_pacing_hooks(_SplitWithout) == (
        "its constructor does not keep stop_frame_timeout_s in _stop_frame_timeout_s",), \
        engine_tts._missing_pacing_hooks(_SplitWithout)
    print(f"  PASS {len(cases) + 2} ways of writing the same hooks fit, with or without "
          "sources; a keyword-only keepalive, or a watchdog that refreshes instead of "
          "skipping it, does not")


def test_a_value_that_is_not_a_pacing_is_reported():
    """TTS_PACING=fast reads as the default (greedy) with one WARNING at startup, which
    scrolls away. /health's "configured" is what the operator set, so it differs from
    "effective" and says why, and doctor flags it instead of "OK TTS pacing: greedy".
    A value that reads ("LEAD") is the schema's spelling."""
    pacing, warned, calls, health = _build("fast")
    assert pacing == "greedy" and not calls, f"{pacing}, {calls}"
    assert len(warned) == 1 and "TTS_PACING='fast'" in warned[0], warned
    assert {k: health[k] for k in ("configured", "effective", "invalid", "missing")} == \
        {"configured": "fast", "effective": "greedy",
         "invalid": "must be one of 'greedy', 'lead'", "missing": []}, health
    from tempdirs import tempdir
    etc = tempdir("teaport-etc-")
    rc, checks = _doctor_run({"ok": True, "tts_pacing": health}, etc=etc)
    said = [c for c in checks if "pacing" in c.lower()]
    assert said == [f"WARN TTS_PACING=fast must be one of 'greedy', 'lead', so the brain "
                    f"paces greedy — set it on the config page or in {etc}/brain.env"], said
    pacing, warned, _, health = _build(" LEAD ")
    assert (pacing, health["configured"], health["invalid"], warned) == \
        ("lead", "lead", None, []), health
    pacing, _, _, health = _build(None)
    assert (pacing, health["configured"], health["invalid"]) == ("greedy", "greedy", None)
    print("  PASS TTS_PACING=fast: /health configured 'fast', effective greedy, why; "
          "doctor warns")


def test_doctor_runs_every_check_on_any_box():
    """Doctor's pacing line comes after its env-file checks. Those used to end doctor on
    a box without /etc/teaport/engine.env: envval's `sed | head` failed under pipefail,
    and `fpt="$(envval ...)"` under set -e exited. Now every check runs, and an env
    file's keys are judged only when doctor can read the file."""
    from tempdirs import tempdir
    health = {"ok": True, "tts_pacing": {"configured": "greedy", "effective": "greedy"}}
    etc = tempdir("teaport-etc-")
    rc, checks = _doctor_run(health, etc=etc)
    assert rc == 1 and "OK TTS pacing: greedy" in checks, checks
    assert f"FAIL {etc}/engine.env missing (run install.sh)" in checks \
        and f"FAIL {etc}/brain.env missing (run install.sh)" in checks, checks
    assert not [c for c in checks if "KOKORO_RESERVE_FPT" in c or "LLM_BASE_URL" in c
                or "GATEWAY_TOKEN" in c], "keys judged in a file that is not there"
    with open(os.path.join(etc, "engine.env"), "w") as f:
        f.write("KOKORO_RESERVE_FPT=6\nKOKORO_RESERVE_FPT=50\n")   # the first one counts
    with open(os.path.join(etc, "brain.env"), "w") as f:
        f.write("LLM_BASE_URL=http://llm:8080/v1\nGATEWAY_TOKEN=\n")
    rc, checks = _doctor_run(health, etc=etc)
    assert {"OK KOKORO_RESERVE_FPT=6", "OK LLM_BASE_URL set",
            "WARN GATEWAY_TOKEN unset — /talk is unauthenticated",
            "OK TTS pacing: greedy"} <= set(checks), checks
    if os.geteuid() != 0:  # root reads a mode-000 file
        import grp
        import pwd
        f = os.path.join(etc, "brain.env")
        os.chmod(f, 0)
        rc, checks = _doctor_run(health, etc=etc)
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        # The repair, not "sudo teaport doctor" (HOME=/root moves its other lookups).
        assert [c for c in checks if "brain.env" in c or "LLM_BASE_URL" in c] == [
            f"WARN {f} is not readable by {user}, so its settings are not checked — "
            f"re-run install.sh, or: sudo chgrp {group} {f} && sudo chmod 640 {f}"], checks
        assert "OK KOKORO_RESERVE_FPT=6" in checks and "OK TTS pacing: greedy" in checks
    print("  PASS doctor runs every check with no install, judges only readable env files")


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


async def _paced_reply(keepalive=True):
    """REPLY through a real pipeline, paced "lead" behind a watchdog (0.4 s) shorter
    than the ~1 s pacing wait before its second clause, with pipecat's keepalive or
    with it a no-op: the clauses' eager flags and what reached the output, (time,
    frame)."""
    tts = EngineTTSService(voice="af_heart")
    eagers = []

    async def synth(text, eager=True):
        eagers.append(eager)
        async for ev in _tone_segments(text, eager):
            yield ev
    tts._synth_text = synth
    tts._pacing, tts._lead_s, tts._eager = "lead", 0.2, "first"
    tts._stop_frame_timeout_s = 0.4
    if not keepalive:
        tts._refresh_audio_context = lambda context_id: None
    probe, out = Probe(), StubOutput()
    task = PipelineTask(Pipeline([tts, probe, out]), observers=[])
    running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(0.5)
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
    return eagers, probe.seen


def _one_reply(seen):
    """Whether `seen` is one whole reply: its audio in one context, one stop frame,
    after the last audio."""
    stops = [i for i, (_, f) in enumerate(seen) if isinstance(f, TTSStoppedFrame)]
    audio = [i for i, (_, f) in enumerate(seen) if isinstance(f, TTSAudioRawFrame)]
    return (len(stops) == 1 and bool(audio) and stops[0] > audio[-1]
            and len({seen[i][1].context_id for i in audio}) == 1)


async def test_pacing_wait_keeps_the_reply_context_alive():
    """What the hook check (pacing_status) cannot show: that a paced wait really keeps
    the reply alive. CI runs this against the pipecat in uv.lock, the only pipecat a
    box installs (install.sh --only brain, the .deb). The check must agree with it,
    and the keepalive must be what does it: with it a no-op, the same wait ends the
    reply early."""
    assert engine_tts._missing_pacing_hooks() == (), engine_tts._missing_pacing_hooks()
    t0 = time.monotonic()
    eagers, seen = await _paced_reply()
    audio = [(ts, f) for ts, f in seen if isinstance(f, TTSAudioRawFrame)]
    assert len(eagers) >= 2, f"want a multi-clause reply, got {len(eagers)} clause(s)"
    assert _one_reply(seen), \
        f"the context closed mid-reply: {[type(f).__name__ for _, f in seen]}"
    assert eagers[0] is True and not any(eagers[1:]), f"eager=first sent {eagers}"
    # The second clause waited for playout instead of following the first at once.
    gap = audio[-1][0] - audio[0][0]
    assert gap >= CLAUSE_SECS - 0.2 - 0.3, f"clause 2 followed {gap:.2f}s after clause 1"
    took = time.monotonic() - t0
    cut_eagers, cut = await _paced_reply(keepalive=False)
    assert len(cut_eagers) >= 2 and not _one_reply(cut), \
        f"no keepalive, yet one whole reply: {[type(f).__name__ for _, f in cut]}"
    print(f"  PASS {len(eagers)} clauses, one context, one stop frame; clause 2 paced "
          f"{gap:.2f}s behind clause 1 (eager {eagers}), {took:.1f}s total; without the "
          f"keepalive the reply is cut")


def test_tts_pacing():
    require_pinned()
    test_auto_lead_target_follows_the_learned_synth_time()
    test_lead_s_parsing()
    test_eager_policy()
    test_choice_setting()
    test_missing_pipecat_hooks_fall_back_to_greedy()
    test_hook_check_sees_how_pipecat_writes_them()
    test_a_value_that_is_not_a_pacing_is_reported()
    test_doctor_runs_every_check_on_any_box()

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
