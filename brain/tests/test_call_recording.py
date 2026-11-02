#
# test_call_recording.py — the owner's phone-call recording (#114).
#
# The issue's acceptance, each pinned here:
#   - with recording off (the default) a call leaves no audio or transcript file, and the
#     session is built without a recording;
#   - with audio / transcript / both, a call leaves the expected files, named by time and
#     call id: one stereo wav (caller left, bot right) through pipecat's
#     AudioBufferProcessor, and a transcript of the ledger's lines;
#   - old recordings are pruned by age and by size, a call's files as one, ours only;
#     the diagnostic dump is pruned the same way;
#   - with the notice on, a recorded call starts with the notice, in the call's
#     language, before the greeting -- and a resumed call, whose caller heard it, does not.
#
# Run: python test_call_recording.py   (or via test_suite.py)
#
import asyncio
import os
import re
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.task import PipelineParams  # noqa: E402
from pipecat.tests.utils import run_test  # noqa: E402

from teaport_brain import audio_dump, call_recording, recordings  # noqa: E402
from teaport_brain.agent_session import AgentSession, _build_initial_messages  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger, Utterance  # noqa: E402

import test_sip_call_lifecycle as lifecycle  # noqa: E402

SR = 16000


# ---------------------------------------------------------------- retention

def _touch(path: str, size: int, age_days: float, now: float) -> None:
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    t = now - age_days * 86400
    os.utime(path, (t, t))


def test_prune_by_age_then_size_whole_calls_ours_only():
    now = time.time()
    names = call_recording._NAMES
    with tempfile.TemporaryDirectory() as d:
        mb = 1024 * 1024
        # A 40-day-old call (wav + txt), two newer calls of 1 MB each, a resumed call's
        # two parts, and a file that is not ours.
        _touch(f"{d}/20260901-100000-old.wav", mb, 40, now)
        _touch(f"{d}/20260901-100000-old.txt", 10, 40, now)
        _touch(f"{d}/20261020-100000-a.wav", mb, 10, now)
        _touch(f"{d}/20261025-100000-b.wav", mb, 5, now)
        _touch(f"{d}/20261101-100000-c.wav", mb, 1, now)
        _touch(f"{d}/20261101-100000-c.r1.wav", mb, 0.5, now)
        _touch(f"{d}/notes.txt", 5 * mb, 60, now)
        gone = recordings.prune(d, keep_days=30, max_mb=2.5, pattern=names,
                                keep=frozenset({"20261101-100000-c"}), now=now)
        left = sorted(os.listdir(d))
        # Age first (old, both files), then oldest by size until under 2.5 MB: a, then b.
        # c is kept whole (and is the one being written), notes.txt is not ours.
        assert gone == ["20260901-100000-old", "20261020-100000-a", "20261025-100000-b"], gone
        assert left == ["20261101-100000-c.r1.wav", "20261101-100000-c.wav", "notes.txt"], left
        # 0 and 0: nothing is pruned.
        assert recordings.prune(d, 0, 0, names, now=now + 1e9) == []


def test_the_dump_prunes_its_own_files():
    with tempfile.TemporaryDirectory() as d:
        now = time.time()
        _touch(f"{d}/caller-old.wav", 10, 100, now)
        _touch(f"{d}/caller-old.marks", 10, 100, now)
        _touch(f"{d}/20261101-100000-call.wav", 10, 100, now)   # a call recording: not the dump's
        gone = recordings.prune(d, 30, 0, audio_dump._NAMES, now=now)
        assert gone == ["caller-old"], gone
        assert os.listdir(d) == ["20261101-100000-call.wav"], os.listdir(d)


def test_free_stem_never_overwrites():
    with tempfile.TemporaryDirectory() as d:
        stem = os.path.join(d, "x")
        assert recordings.free_stem(stem) == stem
        open(stem + ".txt", "w").close()
        assert recordings.free_stem(stem) == stem + ".r1"


# ---------------------------------------------------------------- transcript

def test_the_transcript_is_the_ledgers_lines():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("abc@host", "+15550100", mode="transcript",
                                           directory=d)
        assert rec.processors == [], "a transcript-only recording takes no audio"
        assert re.fullmatch(r"\d{8}-\d{6}-abchost", os.path.basename(rec.stem)), rec.stem
        ledger = TranscriptLedger()
        rec.attach(ledger)
        rec.note("This call is being recorded.")
        ledger._add(Utterance("user", "what time do you open", 3.2, 4.9))
        ledger._add(Utterance("assistant", "We open at nine.", 5.4, 7.0))
        ledger._add(Utterance("assistant", "On Sundays we open at noon, and", 8.0, 9.0,
                              interrupted=True, heard_fraction=0.4,
                              heard_text="On Sundays we"))
        rec.close()
        text = open(rec.txt_path, encoding="utf-8").read()
        assert text.startswith("Call abc@host from +15550100, started "), text
        lines = text.splitlines()[2:]
        assert lines == [
            "[notice] Assistant: This call is being recorded.",
            "[00:03.2] Caller: what time do you open",
            "[00:05.4] Assistant: We open at nine.",
            "[00:08.0] Assistant (cut off): On Sundays we",
        ], lines
        assert not os.path.exists(rec.wav_path)


def test_the_model_is_told_only_when_something_is_kept():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c1", mode="both", directory=d)
        assert "being recorded" in rec.prompt_line and "audio and a transcript" in rec.prompt_line
        msgs = _build_initial_messages("persona", None, recorded=rec.prompt_line)
        assert msgs[-1] == {"role": "system", "content": rec.prompt_line}, msgs
        rec.close()
    blocked = tempfile.NamedTemporaryFile()   # a FILE where the directory should be
    rec = call_recording.CallRecording("c2", mode="both", directory=blocked.name + "/x")
    assert not rec.active and rec.prompt_line is None and rec.processors == []
    blocked.close()


# ---------------------------------------------------------------- audio

async def test_audio_is_one_stereo_wav_caller_left_bot_right():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c3", mode="audio", directory=d)
        assert not os.path.exists(rec.txt_path), "an audio-only recording keeps no transcript"
        [buf] = rec.processors
        caller = (1000).to_bytes(2, "little", signed=True) * 320      # 20 ms of caller
        bot = (-2000).to_bytes(2, "little", signed=True) * 320        # 20 ms of bot
        frames = []
        for _ in range(10):
            frames.append(InputAudioRawFrame(audio=caller, sample_rate=SR, num_channels=1))
            frames.append(OutputAudioRawFrame(audio=bot, sample_rate=SR, num_channels=1))
        # The recorder writes at the pipeline's output rate: a call's, here.
        params = PipelineParams(audio_in_sample_rate=SR, audio_out_sample_rate=SR)
        down, _ = await run_test(buf, frames_to_send=frames, pipeline_params=params)
        assert sum(isinstance(f, InputAudioRawFrame) for f in down) == 10, "caller audio passes on"
        assert sum(isinstance(f, OutputAudioRawFrame) for f in down) == 10, "bot audio passes on"
        await buf.cleanup()   # pipecat waits out the handler tasks here
        rec.close()
        with wave.open(rec.wav_path, "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, SR)
            pcm = w.readframes(w.getnframes())
        samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True)
                   for i in range(0, len(pcm), 2)]
        left, right = samples[0::2], samples[1::2]
        assert 1000 in left and -2000 not in left, "the caller is the left channel"
        assert -2000 in right and 1000 not in right, "the bot is the right channel"


# ---------------------------------------------------------------- notice

class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


class _Context:
    def __init__(self):
        self.messages = []

    def add_message(self, m):
        self.messages.append(m)


class _Stt:
    stt_available = True
    slot_busy = False


def _session():
    s = object.__new__(AgentSession)
    s.task, s.context, s.stt = _Task(), _Context(), _Stt()
    s.should_end = False
    return s


async def test_the_notice_plays_before_the_greeting_outside_the_context():
    for prepared in (None, "Hello, how can I help?"):
        s = _session()
        await s.greet(prepared=prepared, notice="This call is being recorded.")
        first, second = s.task.queued
        assert isinstance(first, TTSSpeakFrame) and first.text == "This call is being recorded."
        assert first.append_to_context is False, "the notice is not the model's words"
        assert (isinstance(second, LLMRunFrame) if prepared is None
                else second.text == prepared), s.task.queued
    s = _session()
    s.stt = type("S", (), {"stt_available": False, "slot_busy": False})()
    await s.greet(notice="This call is being recorded.")
    assert not any(getattr(f, "text", "") == "This call is being recorded."
                   for f in s.task.queued), "no notice on a call that cannot be heard"


def test_the_notice_is_in_the_calls_language():
    assert call_recording.notice_line("en-us") == "This call is being recorded."
    assert call_recording.notice_line("es") == "Esta llamada está siendo grabada."
    assert call_recording.notice_line("cmn") == "本次通话正在录音。"


# ---------------------------------------------------------------- a call, end to end

class _Settings:
    """call_recording's settings for one test, put back after."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(call_recording, k)
            setattr(call_recording, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(call_recording, k, v)


async def test_off_by_default_a_call_records_nothing():
    assert call_recording.MODE == "off" and not call_recording.ENABLED
    h = lifecycle._Harness()
    try:
        await h.start()
        await h.call_state("A", "confirmed")
        assert await lifecycle.wait_until(lambda: "A" in lifecycle._FakeSession.greeted)
        assert lifecycle._FakeSession.build_kw["A"].get("recording") is None
        assert lifecycle._FakeSession.notices["A"] is None
    finally:
        await h.stop()


async def test_a_recorded_call_gets_its_files_and_the_notice_a_resumed_one_no_notice():
    with tempfile.TemporaryDirectory() as d, \
            _Settings(MODE="both", ENABLED=True, NOTICE=True, DIR=d):
        h = lifecycle._Harness()
        try:
            await h.start()
            await h.call_state("A", "confirmed")
            assert await lifecycle.wait_until(lambda: "A" in lifecycle._FakeSession.greeted)
            rec = lifecycle._FakeSession.build_kw["A"]["recording"]
            assert rec.active and rec.audio and rec.transcript
            assert rec.processors, "the audio recorder joins the pipeline"
            assert lifecycle._FakeSession.notices["A"] == "This call is being recorded."
            assert os.path.basename(rec.txt_path).endswith("-A.txt"), rec.txt_path
            assert "[notice] Assistant: This call is being recorded." in open(rec.txt_path).read()

            # The brain restarted under a call: its rest is recorded, its caller is not
            # told twice.
            await h.send_raw({"type": "call.incoming", "call_id": "B",
                              "from": "sip:caller@example.invalid",
                              "to": "sip:svc@example.invalid", "replay": True})
            await h.call_state("B", "confirmed", replay=True)
            assert await lifecycle.wait_until(lambda: "B" in lifecycle._FakeSession.greeted)
            assert lifecycle._FakeSession.build_kw["B"]["recording"].active
            assert lifecycle._FakeSession.notices["B"] is None
        finally:
            await h.stop()
    with tempfile.TemporaryDirectory() as d, \
            _Settings(MODE="transcript", ENABLED=True, NOTICE=False, DIR=d):
        h = lifecycle._Harness()
        try:
            await h.start()
            await h.call_state("C", "confirmed")
            assert await lifecycle.wait_until(lambda: "C" in lifecycle._FakeSession.greeted)
            assert lifecycle._FakeSession.notices["C"] is None, "the notice was turned off"
            assert [f for f in os.listdir(d) if f.endswith(".wav")] == []
        finally:
            await h.stop()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
