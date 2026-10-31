#
# thinking_sound types the bed a long consult plays, live: a keyboard of modal keys
# (modelled on a recorded mechanical keyboard) built when the brain starts, and a
# typist per bed that schedules and renders each keystroke as it plays, in 40 ms
# chunks; TEAPORT_THINKING_WAV loops the user's own wav instead. These hold the module
# to what it promises: typing that a seed reproduces and that never repeats, as loud
# as the bed it replaced and never louder than -6 dBFS, every key press reported
# before its chunk and where its click is heard, and no numpy.random (its import
# alone is 2-3 MB resident, and nothing else in the brain loads it). An override
# plays as given, and a bad one plays live typing instead: it neither raises (warm()
# runs at the brain's start, where an exception would crash-loop the service) nor is
# written over.
#
# Run: python test_thinking_sound.py   (or via pytest test_suite.py)
#
import asyncio
import os
import struct
import subprocess
import sys
import time
import wave

BRAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BRAIN)

from tempdirs import tempdir  # noqa: E402

os.environ.pop("TEAPORT_THINKING_WAV", None)   # live typing, whatever the shell set

from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import thinking_sound as ts  # noqa: E402
from teaport_brain.followup_gate import RunSilentFrame  # noqa: E402

KEYBOARD = ts._Keyboard()


def _typed(secs, seed):
    """`secs` of a seeded typist: the PCM (as floats, before TEAPORT_THINKING_GAIN) and
    every key press as (absolute sample, letter, the chunk it was reported with)."""
    t = ts._Typist(KEYBOARD, seed=seed)
    pcm, presses = [], []
    for i in range(int(secs * 1000) // ts._CHUNK_MS):
        chunk, pressed = t.chunk()
        pcm.append(chunk)
        presses += [(i * ts._CHUNK + at, letter, i) for at, letter in pressed]
    return np.frombuffer(b"".join(pcm), dtype=np.int16) / 32767 / ts._GAIN, presses


def _write_wav(path, pcm, rate=ts._SR, channels=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


def _load_with(path):
    saved, ts._WAV = ts._WAV, path
    try:
        return ts._load_override()
    finally:
        ts._WAV = saved


def _contents(path):
    """The file's bytes; None for a directory or a missing file."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def test_the_typing_is_reproducible_and_never_repeats():
    a, pa = _typed(35, seed=1)
    b, pb = _typed(35, seed=1)
    c, _ = _typed(5, seed=2)
    assert np.array_equal(a, b) and pa == pb
    assert not np.array_equal(a[:len(c)], c)
    # Not a loop of any length up to 30 s: its first 5 s turn up at no later lag (a loop
    # correlates ~1 at its own length; the 15 s loop this replaced did).
    n = 5 * ts._SR
    seg, rest = a[:n], a[ts._SR // 2:]
    m = 1 << int(np.ceil(np.log2(len(rest) + n)))
    num = np.fft.irfft(np.fft.rfft(rest, m) * np.conj(np.fft.rfft(seg, m)), m)[:len(rest) - n]
    energy = np.concatenate([[0], np.cumsum(rest ** 2)])
    den = np.sqrt(np.dot(seg, seg) * (energy[n:len(rest)] - energy[:len(rest) - n]))
    assert np.max(num / den) < 0.3, np.max(num / den)


def test_the_typing_is_as_loud_as_before_and_peaks_under_minus_6_dbfs():
    x, presses = _typed(60, seed=1)
    rms_db = 20 * np.log10(np.sqrt(np.mean(x ** 2)))
    assert -36.5 < rms_db < -34.0, rms_db           # the loop it replaced: -35.4 RMS
    assert np.max(np.abs(x)) <= 0.5 + 1e-4, np.max(np.abs(x))
    assert 6.5 < len(presses) / 60 < 9.5, len(presses)   # steady touch typing, ~8 keys/s


def test_every_key_press_is_reported_before_its_chunk_and_heard_on_its_click():
    x, presses = _typed(30, seed=3)
    # The click is the brightest thing in a keystroke: above 5 kHz, its onset is the
    # loudest sample near the press (the switch's second strike comes 0.6-1.5 ms later
    # and ~4 dB softer).
    f = np.fft.rfftfreq(len(x), 1 / ts._SR)
    hp = np.abs(np.fft.irfft(np.fft.rfft(x) * (f > 5000), len(x)))
    w, near = int(ts._SR * 0.003), int(ts._SR * 0.002)
    on_click = 0
    for at, letter, reported in presses:
        assert letter in ts._LETTERS or letter == "space", letter
        assert at >= (reported + 1) * ts._CHUNK, (at, reported)   # announced ahead of its audio
        if w <= at < len(x) - w:
            on_click += abs(int(np.argmax(hp[at - w:at + w])) - w) <= near
    assert on_click >= 0.95 * len(presses), (on_click, len(presses))


def test_every_chunk_is_40_ms():
    t = ts._Typist(KEYBOARD, seed=4)
    assert {len(t.chunk()[0]) for _ in range(200)} == {2 * ts._CHUNK}


def test_nothing_the_brain_starts_with_imports_numpy_random():
    # Not the bed, nor anything else the brain imports or warms up at start: the bed's
    # RAM saving holds only while nothing does.
    code = ("import sys\n"
            "from teaport_brain import gateway_server, thinking_sound as ts\n"
            "gateway_server.warm_first_use_imports()\n"
            "ts.warm()\n"
            "t = ts._Typist(ts._KEYBOARD)\n"
            "[t.chunk() for _ in range(100)]\n"
            "print('numpy.random' in sys.modules)\n")
    env = dict(os.environ, PYTHONPATH=BRAIN, TEAPORT_THINKING_WAV="")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout


def _bed(consults_async=True, keys=True, gate=None):
    """A ThinkingSound with what it pushes recorded as (when, frame) and its tasks run on
    the test loop (there is no pipeline TaskManager here)."""
    bed = ts.ThinkingSound(consults_async=consults_async, keys=keys, gate=gate)
    out = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append((time.monotonic(), frame))

    async def cancel(task, *a, **k):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    bed.push_frame = push
    bed.create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    bed.cancel_task = cancel
    return bed, out


def _with(grace=0.1, max_s=None, chunk=None):
    """Run an async test with TEAPORT_THINKING_GRACE_S (and _MAX_S, the typist's chunk)
    set for it."""
    def wrap(fn):
        def run():
            saved = ts._GRACE_S, ts._MAX_S, ts._Typist.chunk
            ts._GRACE_S = grace
            ts._MAX_S = ts._MAX_S if max_s is None else max_s
            ts._Typist.chunk = chunk or ts._Typist.chunk
            try:
                asyncio.run(fn())
            finally:
                ts._GRACE_S, ts._MAX_S, ts._Typist.chunk = saved
        run.__name__ = fn.__name__
        return run
    return wrap


async def _send(bed, frame, direction=FrameDirection.DOWNSTREAM):
    await bed.process_frame(frame, direction)


def _working(call_id, done=False, answered=True):
    return OutputTransportMessageUrgentFrame(message={
        "type": "working", "call_id": call_id,
        **({"done": True, "answered": answered} if done else {"secs": 145})})


def _audio(out):
    return [t for t, f in out if type(f) is OutputAudioRawFrame]


def _said(out, kind):
    return [f.message for _, f in out
            if isinstance(f, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame))
            and f.message.get("type") == kind]


async def _soon(check, within=1.5):
    """Whether check() comes true within `within` seconds. For what the bed does at its
    own pace: the typist pauses between words (up to ~0.7 s without a keystroke), and a
    slow machine renders fewer chunks in a fixed window."""
    end = time.monotonic() + within
    while not check():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.02)
    return True


@_with(grace=0.1)
async def test_a_background_consult_types_from_its_start_to_its_answer():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.6)
    await _send(bed, _working("c1", done=True))
    assert _said(out, "typing") == [{"type": "typing", "on": True, "first_key_ms": 100},
                                    {"type": "typing", "on": False, "done": True}]
    assert len(_said(out, "working")) == 2                   # forwarded, as ever
    assert 8 <= len(_audio(out)) <= 13                       # heard from 0.1 s, 40 ms a chunk
    assert not any(isinstance(f, TTSAudioRawFrame) for _, f in out)   # never "bot speech"
    keys = _said(out, "keys")
    assert keys and all(k["anchor"] == "audio" for k in keys)
    assert all(letter in ts._LETTERS or letter == "space"
               for k in keys for letter, ms in k["keys"])
    n = len(out)
    await asyncio.sleep(0.2)
    assert len(out) == n                                     # nothing once it is answered


@_with(grace=0.15)
async def test_the_typing_is_heard_only_while_the_line_is_quiet_and_seen_all_along():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.4)
    heard = len(_audio(out))
    assert heard >= 4
    await _send(bed, VADUserStartedSpeakingFrame())          # the user talks over it
    await asyncio.sleep(0.05)
    mark = len(out)
    await asyncio.sleep(1.0)                                 # (the typist pauses up to ~0.7 s)
    talking = out[mark:]
    assert not _audio(talking)                               # not heard...
    assert all(k["anchor"] == "now" for k in _said(talking, "keys"))   # ...but typed,
    assert all(type(f) is OutputTransportMessageUrgentFrame  # never queued behind a reply
               for _, f in talking if isinstance(f, (OutputTransportMessageFrame,
                                                     OutputTransportMessageUrgentFrame)))
    assert _said(talking, "keys")
    await _send(bed, VADUserStoppedSpeakingFrame())
    await asyncio.sleep(0.1)
    assert not _audio(out[mark:])                            # the grace after them
    await asyncio.sleep(0.25)
    assert _audio(out[mark:])                                # quiet again: heard
    mark = len(out)
    await _send(bed, TTSAudioRawFrame(b"\0\0" * 960, 24000, 1))   # a reply on its way
    await _send(bed, BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await asyncio.sleep(0.3)
    assert len(_audio(out[mark:])) <= 1                      # (one chunk may have been in flight)
    await _send(bed, BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    await asyncio.sleep(0.3)
    assert len(_audio(out[mark:])) >= 3
    await _send(bed, _working("c1", done=True))


@_with(grace=0.05)
async def test_two_consults_type_until_the_last_is_answered_and_a_barge_in_types_on():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await _send(bed, _working("c2"))
    await _send(bed, InterruptionFrame())                    # a background consult goes on
    await _send(bed, _working("c1", done=True))
    await asyncio.sleep(0.2)
    assert _said(out, "typing") == [{"type": "typing", "on": True, "first_key_ms": 50}]
    await _send(bed, _working("c2", done=True))
    assert _said(out, "typing")[-1] == {"type": "typing", "on": False, "done": True}


@_with(grace=0.05)
async def test_an_agent_first_consult_types_until_its_result_or_a_barge_in():
    bed, out = _bed(consults_async=False)
    started = FunctionCallsStartedFrame(function_calls=[SimpleNamespace(function_name="openclaw_agent_consult")])
    await _send(bed, _working("c1"))                         # (not this mode's signal)
    assert not _said(out, "typing")
    await _send(bed, LLMFullResponseStartFrame())            # the call alone: its End is held
    await _send(bed, started)
    await asyncio.sleep(0.2)
    await _send(bed, FunctionCallResultFrame(function_name="openclaw_agent_consult", tool_call_id="t1",
                                             arguments={}, result={"answer": "the answer"}))
    await _send(bed, started)
    await _send(bed, InterruptionFrame())
    assert _said(out, "typing") == [
        {"type": "typing", "on": True, "first_key_ms": 50}, {"type": "typing", "on": False, "done": True},
        {"type": "typing", "on": True, "first_key_ms": 50}, {"type": "typing", "on": False, "done": False}]
    assert _audio(out)


@_with(grace=0.0)
async def test_a_consult_with_no_answer_ends_the_typing_without_the_finish():
    """The face finishes (done) only on an answer: not on a consult that timed out,
    failed or came back empty, in either mode."""
    background, out = _bed()
    await _send(background, _working("c1"))
    await _send(background, _working("c1", done=True, answered=False))
    first, first_out = _bed(consults_async=False)
    await _send(first, FunctionCallsStartedFrame(
        function_calls=[SimpleNamespace(function_name="openclaw_agent_consult")]))
    await _send(first, FunctionCallResultFrame(
        function_name="openclaw_agent_consult", tool_call_id="t1", arguments={},
        result={"error": "the desktop agent is taking too long"}))
    for said in (out, first_out):
        assert _said(said, "typing")[-1] == {"type": "typing", "on": False, "done": False}


@_with(grace=0.0, max_s=0.2)
async def test_at_most_max_s_is_heard_and_the_face_types_on():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.6)
    await _send(bed, _working("c1", done=True))
    assert len(_audio(out)) == 5                             # 0.2 s of 40 ms chunks
    late = [k for k in _said(out, "keys") if k["anchor"] == "now"]
    assert late                                              # typed on, unheard


@_with(grace=0.0)
async def test_a_client_that_does_not_type_hears_it_and_is_sent_no_key_presses():
    bed, out = _bed(keys=False)
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.3)
    await _send(bed, _working("c1", done=True))
    assert _audio(out)
    assert not _said(out, "typing") and not _said(out, "keys")


@_with(grace=0.1)
async def test_a_run_that_said_nothing_is_no_reply_on_its_way():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await _send(bed, LLMFullResponseStartFrame())            # a run of the brain's own...
    await asyncio.sleep(0.3)
    assert not _audio(out)
    await _send(bed, RunSilentFrame())                       # ...that said nothing (End held)
    await asyncio.sleep(0.3)
    assert _audio(out)
    await _send(bed, _working("c1", done=True))


@_with(grace=0.2)
async def test_a_line_that_starts_during_a_render_gets_no_typing_behind_it():
    bed, out = _bed(keys=False)
    real, calls, line = ts._Typist.chunk, [], {}

    def chunk(self, heard=True):
        calls.append(None)
        if len(calls) == 5:                                  # a progress line begins mid-render
            bed._track_line(TTSStartedFrame())
            line["t"] = time.monotonic()
        return real(self, heard)

    ts._Typist.chunk = chunk
    try:
        await _send(bed, _working("c1"))
        give_up = time.monotonic() + 5                       # (however slow the runner)
        while len(calls) < 5 and time.monotonic() < give_up:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)                             # (within the line's grace)
        await _send(bed, _working("c1", done=True))
    finally:
        ts._Typist.chunk = real
    assert len(_audio(out)) == 4                             # the 5th was rendered, not pushed
    assert not [t for t in _audio(out) if t >= line["t"]]


@_with(grace=0.0)
async def test_the_typing_ends_with_its_session():
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.2)
    await bed.cleanup()                                      # the pipeline is torn down
    assert bed._task is None
    n = len(out)
    await asyncio.sleep(0.2)
    assert len(out) == n


@_with(grace=0.05)
async def test_a_held_reply_start_that_comes_after_its_own_call_mutes_only_briefly():
    """The call (a system frame) can overtake its response's Start, held at the TTS
    behind earlier audio; that Start's End is held too. A reply is on its way for at
    most _REPLY_WAIT_S, so the typing is heard all the same."""
    saved, ts._REPLY_WAIT_S = ts._REPLY_WAIT_S, 0.3
    try:
        bed, out = _bed(consults_async=False)
        await _send(bed, FunctionCallsStartedFrame(
            function_calls=[SimpleNamespace(function_name="openclaw_agent_consult")]))
        await _send(bed, LLMFullResponseStartFrame())
        await asyncio.sleep(0.2)
        assert not _audio(out)                               # a reply might be on its way...
        await asyncio.sleep(0.3)
        assert _audio(out)                                   # ...but none came
        await _send(bed, InterruptionFrame())
    finally:
        ts._REPLY_WAIT_S = saved


@_with(grace=0.0)
async def test_no_typing_is_heard_while_a_control_call_waits():
    """The Talk client holds openclaw_agent_control's result while it plays audio
    (gateway-relay.ts submitToolResult): typing heard through the wait kept it held past
    the brain's own wait. Unheard until the call's result or cancel; the face types on."""
    bed, out = _bed()
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.15)
    assert _audio(out)
    for settle in (
            FunctionCallResultFrame(function_name="openclaw_agent_control", tool_call_id="k1",
                                    arguments={}, result={"ok": True}),
            FunctionCallCancelFrame(function_name="openclaw_agent_control", tool_call_id="k1")):
        await _send(bed, FunctionCallsStartedFrame(function_calls=[SimpleNamespace(
            function_name="openclaw_agent_control", tool_call_id="k1")]))
        await asyncio.sleep(0.1)
        mark = len(out)
        await asyncio.sleep(0.3)                             # a fixed window, heard as quiet
        assert not _audio(out[mark:])
        assert not _said(out[mark:], "typing"), "the consult's typing goes on"
        assert await _soon(lambda: _said(out[mark:], "keys")), "the face types on"
        await _send(bed, settle)
        assert await _soon(lambda: _audio(out[mark:])), type(settle).__name__
    # Neither ever comes by: heard again past the brain's own wait.
    saved = ts._CONTROL_MAX_S
    ts._CONTROL_MAX_S = 0.2
    try:
        mark, t0 = len(out), time.monotonic()
        await _send(bed, FunctionCallsStartedFrame(function_calls=[SimpleNamespace(
            function_name="openclaw_agent_control", tool_call_id="k2")]))
        assert await _soon(lambda: _audio(out[mark:]))
        assert ts._CONTROL_MAX_S <= _audio(out[mark:])[0] - t0 < ts._CONTROL_MAX_S + 0.4
    finally:
        ts._CONTROL_MAX_S = saved
    await _send(bed, _working("c1", done=True))


@_with(grace=0.0)
async def test_a_session_held_for_a_phone_call_types_unheard():
    gate = SimpleNamespace(held=True)                        # (the FollowupGate's hold)
    bed, out = _bed(gate=gate)
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.2)
    assert not _audio(out) and _said(out, "keys")            # the face types on
    gate.held = False                                        # back from the call
    await asyncio.sleep(0.2)
    await _send(bed, _working("c1", done=True))
    assert _audio(out)


@_with(grace=0.0)
async def test_a_half_duplex_phone_line_drops_the_caller_while_the_typing_is_heard():
    """In agent-first a phone call is typed through: no bot speech, but echo all the
    same, so the half-duplex gate (SIP_HALF_DUPLEX) mutes the caller through it."""
    from pipecat.frames.frames import InputAudioRawFrame
    from teaport_brain.sip_server import HalfDuplexInputGate
    bed, out = _bed(keys=False)
    hd, passed = HalfDuplexInputGate(tail_s=0.1), []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        passed.append(frame)
    hd.push_frame = push
    hd.thinking_sound = bed

    async def mic():
        await hd.process_frame(InputAudioRawFrame(b"\0\0" * 320, 16000, 1),
                               FrameDirection.DOWNSTREAM)
        return len(passed)

    assert await mic() == 1                                  # no typing: the caller is heard
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.15)
    assert _audio(out) and await mic() == 1                  # typing heard: dropped
    await _send(bed, _working("c1", done=True))
    await asyncio.sleep(0.2)                                 # its tail is over
    assert await mic() == 2


def _counting_chunk():
    real = ts._Typist.chunk

    def chunk(self, heard=True):
        chunk.heard.append(heard)
        return real(self, heard)
    chunk.heard = []
    return chunk


@_with(grace=0.0, max_s=0.1, chunk=_counting_chunk())
async def test_with_nothing_to_play_and_no_face_nothing_is_typed():
    """Past MAX_S (or while the line is busy), a client that does not type along has
    nothing to render: the typist waits."""
    bed, out = _bed(keys=False)
    await _send(bed, _working("c1"))
    await asyncio.sleep(0.5)
    await _send(bed, _working("c1", done=True))
    heard = ts._Typist.chunk.heard
    assert heard and all(heard) and len(heard) == len(_audio(out)) <= 4


def test_an_override_wav_has_no_key_presses_for_the_face():
    saved = ts._LOOP
    ts._LOOP = [bytes(2 * ts._CHUNK)]
    try:
        assert not ts.ThinkingSound(keys=True)._keys
    finally:
        ts._LOOP = saved
    assert ts.ThinkingSound(keys=True)._keys


def _stalling_chunk():
    real, renders = ts._Typist.chunk, []

    def chunk(self, heard=True):
        renders.append(None)
        if len(renders) == 10:
            time.sleep(0.2)                                  # a render stalls 200 ms
        return real(self, heard)
    return chunk


@_with(grace=0.0, chunk=_stalling_chunk())
async def test_the_typing_keeps_time_and_catches_up_one_chunk_after_a_stall():
    """Paced on the clock: a chunk every 40 ms however long each takes to render, and
    after a stall one chunk at once, no more (a longer catch-up would queue ahead of the
    reply)."""
    bed, out = _bed(keys=False)
    await _send(bed, _working("c1"))
    await asyncio.sleep(1.2)
    await _send(bed, _working("c1", done=True))
    gaps = np.diff(_audio(out))
    stall = int(np.argmax(gaps))
    assert 0.18 < gaps[stall] < 1.0, gaps[stall]          # (a loaded runner may stretch it)
    caught_up = 0
    for g in gaps[stall + 1:]:                      # back to back right after it, then 40 ms
        if g >= 0.02:
            break
        caught_up += 1
    assert caught_up == 1, gaps[stall:stall + 4]
    steady = np.delete(gaps, [stall, stall + 1])
    assert abs(np.median(steady) - 0.04) < 0.005, np.median(steady)


def test_an_override_wav_plays_as_given_and_loops_every_chunk():
    path = os.path.join(tempdir(), "mine.wav")
    mine = (np.sin(np.arange(ts._SR) / 7) * 8000).astype(np.int16)
    _write_wav(path, mine)
    assert np.array_equal(_load_with(path), mine)
    assert _contents(path) is not None
    saved = ts._WAV, ts._LOOP, ts._KEYBOARD
    ts._WAV, ts._LOOP, ts._KEYBOARD = path, None, None
    try:
        ts._prepare()
        assert ts._KEYBOARD is None
        assert len(ts._LOOP) == len(mine) // ts._CHUNK      # every whole chunk, the last included
        assert {len(c) for c in ts._LOOP} == {2 * ts._CHUNK}
    finally:
        ts._WAV, ts._LOOP, ts._KEYBOARD = saved


def test_a_bad_override_plays_live_typing_and_is_left_alone():
    d = tempdir()
    stereo = os.path.join(d, "stereo-48k.wav")
    _write_wav(stereo, np.zeros(9600), rate=48000, channels=2)
    text = os.path.join(d, "notes.wav")
    with open(text, "w") as f:
        f.write("not a wav")
    torn = os.path.join(d, "torn.wav")
    _write_wav(torn, np.arange(4800))
    with open(torn, "r+b") as f:
        f.truncate(os.path.getsize(torn) - 1)        # an odd byte count of samples
    # A LIST chunk that claims 2 bytes past the RIFF end: wave raises RuntimeError.
    past_end = os.path.join(d, "past-riff-end.wav")
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, ts._SR, 2 * ts._SR, 2, 16)
    info = b"INFOISFT" + struct.pack("<I", 6) + b"Lavf\0\0"
    with open(past_end, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + len(fmt) + 8 + len(info)) + b"WAVE" + fmt
                + b"LIST" + struct.pack("<I", len(info) + 2) + info
                + b"data" + struct.pack("<I", 9600) + bytes(9600))
    short = os.path.join(d, "short.wav")
    _write_wav(short, np.ones(ts._CHUNK - 1))       # not one whole chunk
    for path in (d, stereo, text, torn, past_end, short, os.path.join(d, "missing.wav")):
        before = _contents(path)
        assert _load_with(path) is None, path
        assert _contents(path) == before, f"{path} was written"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
