#
# test_call_recording.py — the owner's phone-call recording (#114).
#
# The issue's acceptance, each pinned here:
#   - with recording off (the default) a call leaves no audio or transcript file, and the
#     session is built without a recording;
#   - with audio / transcript / both, a call leaves the expected files, named by time and
#     call id: one stereo wav at the call's rate (caller left, bot right) through
#     pipecat's AudioBufferProcessor, and a transcript of what was said and heard;
#   - nothing is recorded, and no file exists, before the caller is told: the recording
#     starts with the start frame greet() queues just ahead of the notice, never on a call
#     that cannot be heard;
#   - old recordings are pruned by age and by size, a call's files as one (a resumed
#     call's parts included), ours only; the diagnostic dump is pruned the same way;
#   - with the notice on, a recorded call starts with the notice, in the call's language,
#     before the greeting, with the caller unheard while it plays.
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
    AudioBufferStartRecordingFrame,
    InputAudioRawFrame,
    InterruptionWorkerFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402

from teaport_brain import audio_dump, call_recording, recordings  # noqa: E402
from teaport_brain.agent_session import AgentSession, InputMute, _build_initial_messages  # noqa: E402
from teaport_brain.followup_gate import Delivery  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger, Utterance  # noqa: E402

import test_sip_call_lifecycle as lifecycle  # noqa: E402

SR = 16000
NOTICE = "This call is being recorded."


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


def test_a_resumed_call_continues_its_recording_as_one_group():
    with tempfile.TemporaryDirectory() as d:
        first = call_recording.CallRecording("abc", mode="transcript", directory=d)
        first.begin()
        first.close()
        time.sleep(1.1)   # a later start time: the stem it must NOT take
        rest = call_recording.CallRecording("abc", mode="transcript", directory=d,
                                            resumed=True)
        assert rest.stem == first.stem + ".r1", (first.stem, rest.stem)
        other = call_recording.CallRecording("xyz", mode="transcript", directory=d,
                                             resumed=True)
        assert re.fullmatch(r"\d{8}-\d{6}-xyz", os.path.basename(other.stem)), other.stem


# ---------------------------------------------------------------- transcript

def test_line_times_carry_into_the_minute():
    clock = call_recording._clock
    assert clock(0) == "00:00.0"
    assert clock(5.34) == "00:05.3"
    assert clock(59.96) == "01:00.0", "never 00:60.0"
    assert clock(119.99) == "02:00.0"
    assert clock(754.2) == "12:34.2"


def test_nothing_is_written_before_the_start():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("abc@host", "+15550100", mode="both",
                                           directory=d + "/recordings")
        ledger = TranscriptLedger()
        rec.attach(ledger)
        ledger._add(Utterance("user", "hello?", 0.5, 1.0))
        rec.close()
        assert not os.path.exists(d + "/recordings"), "a file before the caller was told"


def test_the_transcript_is_the_ledgers_lines():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("abc@host", "+15550100", mode="transcript",
                                           directory=d)
        assert re.fullmatch(r"\d{8}-\d{6}-abchost", os.path.basename(rec.stem)), rec.stem
        ledger = TranscriptLedger()
        rec.attach(ledger)
        rec.begin()
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
            "[00:03.2] Caller: what time do you open",
            "[00:05.4] Assistant: We open at nine.",
            "[00:08.0] Assistant (cut off): On Sundays we",
        ], lines
        assert not os.path.exists(rec.wav_path)


async def test_a_caller_line_after_a_wordless_segment_carries_its_own_time():
    # #288: a cough the STT closes with no words, then a question. The recording's
    # Caller line is stamped from the question's own VAD start, not the cough's.
    from types import SimpleNamespace

    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    from teaport_brain.endpointing import SegmentDoneFrame
    from teaport_brain.stt import FinalTranscriptionFrame

    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c288", mode="transcript", directory=d)
        ledger = TranscriptLedger()
        rec.attach(ledger)
        rec.begin()
        for f, t in [
            (VADUserStartedSpeakingFrame(), 4.0), (VADUserStoppedSpeakingFrame(), 4.3),
            (SegmentDoneFrame(stop_n=1), 4.9),
            (VADUserStartedSpeakingFrame(), 12.5), (VADUserStoppedSpeakingFrame(), 14.0),
            (FinalTranscriptionFrame("can I book a table", "caller", "", None,
                                     finalized=True, stop_n=2), 14.6),
        ]:
            await ledger.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9),
                                                          processor=None))
        rec.close()
        lines = open(rec.txt_path, encoding="utf-8").read().splitlines()[2:]
        assert lines == ["[00:12.5] Caller: can I book a table"], lines


def _words(ctx, *words):
    return [TTSTextFrame(text=w, aggregated_by="word", context_id=ctx) for w in words]


def _own_started(ctx):
    f = TTSStartedFrame(context_id=ctx)
    f.append_to_context = False
    return f


async def test_the_notice_starts_the_recording_as_it_plays_and_lines_are_written():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c9", mode="transcript", directory=d)
        [tap] = rec.processors
        rec.arm("notice")
        frames = [
            # A reply's words before the notice plays: not the caller's to record yet.
            TTSStartedFrame(context_id="x"), *_words("x", "unrecorded"),
            _own_started("n"), *_words("n", "This", "call", "is", "being", "recorded."),
            TTSStoppedFrame(context_id="n"),
            # A reply that played through: the ledger's to chart, not this tap's.
            TTSStartedFrame(context_id="r"), *_words("r", "We", "open", "at", "nine."),
            TTSStoppedFrame(context_id="r"),
            # A filler and a reply still playing when the call ends.
            _own_started("f"), *_words("f", "One", "moment."),
            TTSStartedFrame(context_id="q"), *_words("q", "On", "Sundays"),
        ]
        down, _ = await run_test(tap, frames_to_send=frames)
        rec.close()
        kinds = [type(f).__name__ for f in down]
        first_notice_word = next(i for i, f in enumerate(down)
                                 if isinstance(f, TTSTextFrame) and f.text == "This")
        assert kinds[first_notice_word - 1] == "AudioBufferStartRecordingFrame", kinds
        assert kinds.count("AudioBufferStartRecordingFrame") == 1, kinds
        assert down[first_notice_word - 1].interruptible is False, (
            "an interruption in the recorder's queue would lose the start")
        lines = open(rec.txt_path, encoding="utf-8").read().splitlines()[2:]
        assert [re.sub(r"^\[[^\]]*\] ", "", ln) for ln in lines] == [
            "Assistant: This call is being recorded.",
            "Assistant (call ended): One moment.",
            "Assistant (call ended): On Sundays",
        ], lines


async def test_a_line_already_playing_when_armed_does_not_start_it():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c11", mode="transcript", directory=d)
        [tap] = rec.processors
        # A pipeline line that started before greet() armed the recording (its
        # TTSStartedFrame passed unarmed): its later words must not start it ahead of the
        # notice.
        tap._own.add("e")
        rec.arm("notice")
        await run_test(tap, frames_to_send=_words("e", "still", "talking"))
        assert not rec.started


async def test_a_notice_that_never_plays_starts_nothing():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c10", mode="both", directory=d)
        tap = rec.processors[0]
        rec.arm("notice")
        frames = [TTSStartedFrame(context_id="g"), *_words("g", "Hello", "there.")]
        down, _ = await run_test(tap, frames_to_send=frames)
        assert not rec.started and os.listdir(d) == []
        assert not any(isinstance(f, AudioBufferStartRecordingFrame) for f in down)


def test_the_model_is_told_only_when_something_is_kept():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c1", mode="both", directory=d)
        assert "being recorded" in rec.prompt_line and "audio and a transcript" in rec.prompt_line
        msgs = _build_initial_messages("persona", None, recorded=rec.prompt_line)
        assert msgs[-1] == {"role": "system", "content": rec.prompt_line}, msgs
    blocked = tempfile.NamedTemporaryFile()   # a FILE where the directory should be
    rec = call_recording.CallRecording("c2", mode="both", directory=blocked.name + "/x")
    rec.begin()
    assert not rec.active and rec.prompt_line is None, "nothing could be written"
    blocked.close()


# ---------------------------------------------------------------- audio

async def test_audio_is_one_stereo_wav_at_the_calls_rate_from_the_start_frame_on():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c3", mode="audio", directory=d)
        tap, buf = rec.processors
        caller = (1000).to_bytes(2, "little", signed=True) * 320      # 20 ms of caller
        bot = (-2000).to_bytes(2, "little", signed=True) * 320        # 20 ms of bot
        early = (7).to_bytes(2, "little", signed=True) * 320          # before the start
        # The caller's audio is a SystemFrame, so it overtakes the (control) start frame
        # unless the test waits for it to land, as pipecat's own tests do (SleepFrame).
        frames = [InputAudioRawFrame(audio=early, sample_rate=SR, num_channels=1),
                  AudioBufferStartRecordingFrame(), SleepFrame(sleep=0.1)]
        for _ in range(10):
            frames.append(InputAudioRawFrame(audio=caller, sample_rate=SR, num_channels=1))
            frames.append(OutputAudioRawFrame(audio=bot, sample_rate=SR, num_channels=1))
        # The pipeline's default rates, as the session's task runs (24 kHz out): the
        # recording is at the call's 16 kHz all the same.
        down, _ = await run_test(buf, frames_to_send=frames)
        assert sum(isinstance(f, InputAudioRawFrame) for f in down) == 11, "caller audio passes on"
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
        assert 7 not in left, "audio from before the start frame was recorded"


# ---------------------------------------------------------------- notice

class _Task:
    def __init__(self, on_queue=None):
        self.queued = []
        self.on_queue = on_queue

    async def queue_frames(self, frames):
        self.queued.extend(frames)
        if self.on_queue is not None:
            self.on_queue(frames)


class _Context:
    def __init__(self):
        self.messages = []

    def add_message(self, m):
        self.messages.append(m)


class _Stt:
    stt_available = True
    slot_busy = False


def _session(plays=True, recording=None):
    """An AgentSession with fakes around greet(). `plays`: the notice plays out (its
    delivery settles once it is queued, with the input mute's state at each queue
    recorded); False, it never does, and greet() waits out NOTICE_MAX_SECS."""
    s = object.__new__(AgentSession)
    s.context, s.stt, s.llm, s.tts = _Context(), _Stt(), None, None
    s.input_mute = InputMute()
    s.should_end = False
    s.recording = recording
    s.muted_while_queued = []
    s._OWN_LINE_SETTLE_S = 0.0
    deliveries = []

    class _Gate:
        def watch_delivery(self):
            deliveries.append(Delivery())
            return deliveries[-1]

        def drop_delivery(self, d):
            deliveries.remove(d)

    def on_queue(frames):
        s.muted_while_queued.append(s.input_mute.muted)
        if plays and deliveries and any(isinstance(f, TTSSpeakFrame) for f in frames):
            deliveries.pop()._settle(interrupted=False)
    s.followup_gate = _Gate()
    s.task = _Task(on_queue)
    return s


def _spoken(s):
    return [f for f in s.task.queued if not isinstance(f, InterruptionWorkerFrame)]


async def test_the_notice_plays_whole_before_the_greeting_outside_the_context():
    with tempfile.TemporaryDirectory() as d:
        for prepared in (None, "Hello, how can I help?"):
            rec = call_recording.CallRecording("c4", mode="both", directory=d)
            s = _session(recording=rec)
            await s.greet(prepared=prepared, notice=NOTICE)
            assert rec.armed == "notice", "it starts as the notice plays"
            # The fake plays the notice with no word frames (a voice without word
            # timings): told in full, the recording starts right after it.
            notice, start, greeting = _spoken(s)
            assert isinstance(notice, TTSSpeakFrame) and notice.text == NOTICE
            assert notice.append_to_context is False, "the notice is not the model's words"
            assert isinstance(start, AudioBufferStartRecordingFrame), _spoken(s)
            assert start.interruptible is False
            assert (isinstance(greeting, LLMRunFrame) if prepared is None
                    else greeting.text == prepared), _spoken(s)
            # The caller is unheard while the notice is queued and plays (no "hello?" cuts
            # it), and heard again by the time the greeting is queued.
            assert s.muted_while_queued[-1] is False and all(s.muted_while_queued[:-2])
            assert not s.input_mute.muted
    rec = call_recording.CallRecording("c6", mode="both", directory="/nonexistent/x")
    s = _session(recording=rec)
    s.stt = type("S", (), {"stt_available": False, "slot_busy": False})()
    await s.greet(notice=NOTICE)
    assert rec.armed is None and not any(
        isinstance(f, AudioBufferStartRecordingFrame) or getattr(f, "text", "") == NOTICE
        for f in s.task.queued), "a call that cannot be heard is neither told nor recorded"


async def test_with_the_notice_off_the_greeting_starts_the_recording():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c5", mode="audio", directory=d)
        s = _session(recording=rec)
        await s.greet(prepared="Hello.")
        assert rec.armed == "any"
        assert [type(f).__name__ for f in _spoken(s)] == ["TTSSpeakFrame"], _spoken(s)


async def test_a_notice_that_never_plays_out_still_lets_the_greeting_and_the_caller_in():
    with tempfile.TemporaryDirectory() as d:
        rec = call_recording.CallRecording("c7", mode="both", directory=d)
        s = _session(plays=False, recording=rec)
        s.NOTICE_MAX_SECS = 0.05
        await s.greet(prepared="Hello.", notice=NOTICE)
        assert [getattr(f, "text", None) for f in _spoken(s)] == [NOTICE, "Hello."], s.task.queued
        assert not s.input_mute.muted, "the caller must not stay unheard"
        assert rec.armed is None and not rec.started, "not told, so not recorded"


def test_the_notice_is_in_the_calls_language():
    assert call_recording.notice_line("en-us") == NOTICE
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


async def test_a_recorded_call_is_built_with_its_recording_and_told_resumed_too():
    with tempfile.TemporaryDirectory() as d, \
            _Settings(MODE="both", ENABLED=True, NOTICE=True, DIR=d):
        h = lifecycle._Harness()
        try:
            await h.start()
            await h.call_state("A", "confirmed")
            assert await lifecycle.wait_until(lambda: "A" in lifecycle._FakeSession.greeted)
            rec = lifecycle._FakeSession.build_kw["A"]["recording"]
            assert rec.active and rec.audio and rec.transcript
            assert len(rec.processors) == 2, "the tap and the audio recorder join the pipeline"
            assert lifecycle._FakeSession.notices["A"] == NOTICE
            assert os.path.basename(rec.stem).endswith("-A"), rec.stem
            assert os.listdir(d) == [], "files only once greet() starts the recording"

            # The brain restarted under a call: the caller is told again (the brain that
            # was cut off may not have finished telling them).
            await h.send_raw({"type": "call.incoming", "call_id": "B",
                              "from": "sip:caller@example.invalid",
                              "to": "sip:svc@example.invalid", "replay": True})
            await h.call_state("B", "confirmed", replay=True)
            assert await lifecycle.wait_until(lambda: "B" in lifecycle._FakeSession.greeted)
            assert lifecycle._FakeSession.build_kw["B"]["recording"].active
            assert lifecycle._FakeSession.notices["B"] == NOTICE
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
            assert len(lifecycle._FakeSession.build_kw["C"]["recording"].processors) == 1
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
