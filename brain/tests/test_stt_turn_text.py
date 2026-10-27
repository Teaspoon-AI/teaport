#
# Unit test: the STT's sliced turn text (stt.py TURN TEXT, TEAPORT_STT_TURN_TEXT=slice):
# one continuous engine transcript, each turn the main words that END before its commit,
# pushed at the commit's preview, settled once the engine's bound passes the commit.
#
# What these pin, in order:
#
#   * the handshake: slice asks for the preview stream (and the progress report its bounds
#     ride on) before any audio; segment mode's handshake is exactly what it was. Slices
#     are taken only where session.created offers "transcription.preview": an engine
#     without it runs on segments, its dones as before;
#   * the slicing: words that end before the commit are its turn's, whenever they arrive;
#     one that arrives after the commit is never an interim (it would start a turn over
#     the reply to its own words); one that ends at or after the commit is the next
#     turn's, even ahead of the commit's preview;
#   * the provisional text (the words in hand at the preview, then the preview's text) is
#     the turn's final, stamped with the commit's VAD stop, as a done's would be;
#   * the final: settled once its preview has come AND end_through_ms has passed the
#     commit, oldest turn first; equal -> nothing goes down; different -> a
#     TurnTextFinalFrame naming what the final added ("adds": the trailing "stop" a
#     preview stopped short of), dropped, or changed;
#   * words are never lost: an empty provisional (a wordless close) whose final has words
#     pushes them as a final of their own, stamped NO_STOP; and so does a final the
#     corrector downstream could place nowhere (TurnTextUnplacedFrame, upstream);
#   * what slice mode turns off: the carry, and the wait for the caller's pause -- a
#     COMPLETE verdict commits at once over voice the analyzer has heard; what it keeps:
#     the hint, on every commit;
#   * the speculation's report rides on end_through_ms, not decoded_through_ms;
#   * a closed session takes its unsettled turns with it.
#
# Run: python test_stt_turn_text.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    NO_STOP,
    SegmentDoneFrame,
    SpeechDecodedFrame,
    TurnVerdictFrame,
)
from teaport_brain.stt import FinalTranscriptionFrame, TeaportSTTService  # noqa: E402
from teaport_brain.turn_text import TurnTextFinalFrame, TurnTextUnplacedFrame  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Service(TeaportSTTService):
    """The real service, built with the setting given, its wire recorded and its pushed
    frames kept."""

    def __init__(self, turn_text="slice", progress=False, **kwargs):
        was = stt_mod.TURN_TEXT
        stt_mod.TURN_TEXT = turn_text
        try:
            super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        finally:
            stt_mod.TURN_TEXT = was
        self._websocket = WireRecorder()
        self.pushed = []
        self.transcription_progress = progress

    async def push_frame(self, frame, direction=DOWN):
        self.pushed.append(frame)

    async def start_processing_metrics(self):
        pass

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()

    def of(self, kind):
        return [f for f in self.pushed if isinstance(f, kind)]

    def finals(self):
        return [(f.text, f.stop_n) for f in self.of(FinalTranscriptionFrame)]

    def interims(self):
        return [f.text for f in self.of(InterimTranscriptionFrame)]

    def corrections(self):
        return self.of(TurnTextFinalFrame)


async def created(s, preview=True):
    caps = ["transcription.progress"] + (["transcription.preview"] if preview else [])
    await s._handle_message({"type": "session.created", "id": "sess_1", "created": 1,
                             "capabilities": caps})


async def audio(s, ms):
    for _ in range(ms // CHUNK_MS):
        await s.process_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                 num_channels=1), DOWN)


async def vad_start(s):
    await s.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)


async def vad_stop(s, stop_secs=0.5):
    await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=stop_secs), UP)


async def complete(s):
    await s.process_frame(TurnVerdictFrame(complete=True, source="verdict",
                                           stop_n=s._vad_stops), UP)


async def turn(s, speech_ms=1000, quiet_ms=0):
    """An utterance and its stop, answered COMPLETE: the commit goes out."""
    await vad_start(s)
    await audio(s, speech_ms)
    await vad_stop(s)
    await audio(s, quiet_ms)
    await complete(s)


async def delta(s, text, end):
    await s._handle_message({"type": "transcription.delta", "delta": text,
                             "timestamp": 0.0, "audio_ms": max(0, end - 200),
                             "end_ms": end})


async def preview(s, through, text=""):
    await s._handle_message({"type": "transcription.preview", "through_ms": through,
                             "delta": text, "main_through_ms": 0, "credited_ms": 0})


async def progress(s, end_through, decoded=None):
    await s._handle_message({"type": "transcription.progress",
                             "decoded_through_ms": end_through if decoded is None else decoded,
                             "audio_through_ms": end_through,
                             "end_through_ms": end_through})


async def connected(s):
    """A connect as _connect_websocket makes it, onto a recorder instead of a socket."""
    async def connect(url, **kwargs):
        return WireRecorder()
    real, stt_mod.websockets.connect = stt_mod.websockets.connect, connect
    try:
        s._websocket = None
        await s._connect_websocket()
    finally:
        stt_mod.websockets.connect = real


def commits(s):
    return s._websocket.commits()


# ---- the handshake and the session's answer -------------------------------------------

async def test_slice_asks_for_the_preview_stream_and_segment_mode_asks_as_before():
    s = Service()
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model,
                                    "transcription_progress": True, "preview": True,
                                    "continuous": True}
    s = Service(turn_text="segment")
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model}
    s = Service(turn_text="segment", progress=True)
    await connected(s)
    assert s._websocket.sent[0] == {"type": "session.update", "model": s._model,
                                    "transcription_progress": True}
    # never to the streaming backend, which validates its session
    s = Service(streaming_backend=True)
    await connected(s)
    assert "preview" not in s._websocket.sent[0] and not s._slicing


async def test_an_engine_without_the_preview_runs_on_segments():
    s = Service()
    await connected(s)
    assert s._slicing, "taken at the connect, until the session says otherwise"
    await created(s, preview=False)
    assert not s._slicing
    await turn(s)
    await s._handle_message({"type": "transcription.delta", "delta": " hello"})
    await s._handle_message({"type": "transcription.done", "text": "hello",
                             "reason": "commit"})
    assert s.finals() == [("hello", 1)] and s.corrections() == []
    assert not s._turns


async def test_segment_mode_ignores_end_ms_and_closes_on_the_done():
    s = Service(turn_text="segment")
    await created(s)
    assert not s._slicing
    await turn(s)
    await delta(s, " what", 400)
    await delta(s, " time", 1200)
    assert s.interims() == [" what", " what time"]
    await s._handle_message({"type": "transcription.done", "text": "what time",
                             "reason": "commit"})
    assert s.finals() == [("what time", 1)]


# ---- slicing --------------------------------------------------------------------------

async def test_a_turn_is_the_words_that_end_before_its_commit():
    s = Service()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " what", 400)
    await delta(s, " time", 700)
    assert s.interims() == [" what", " what time"]
    await vad_stop(s)
    await complete(s)                      # the commit, at 1000 ms
    assert len(commits(s)) == 1 and s._turns[0].at_ms == 1000
    await delta(s, " is", 900)             # after the commit, ends before it: the turn's
    assert s.interims() == [" what", " what time"], "never an interim"
    await audio(s, 300)
    await delta(s, " it", 1100)            # ends after the commit: the next turn's
    assert s.interims()[-1] == " it"
    await preview(s, 1000, "")
    assert s.finals() == [("what time is", 1)]
    await progress(s, 1000)
    assert s.corrections() == [], "the final is what was asked"
    assert s._interim_buffer == " it" and [w for w, _ in s._open_words] == [" it"]


async def test_the_provisional_is_the_words_in_hand_then_the_preview():
    s = Service()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " I'd", 300)
    await delta(s, " like", 500)
    await vad_stop(s)
    await complete(s)
    await preview(s, 1000, " to know.")
    assert s.finals() == [("I'd like to know.", 1)]
    # the main transcript then sends the same words: nothing to correct, case and
    # punctuation aside
    await delta(s, " to", 700)
    await delta(s, " know", 950)
    await progress(s, 1200)
    assert s.corrections() == []
    assert s._turn_same == 1 and s._turn_differs == 0


async def test_the_final_waits_for_its_preview_and_for_the_bound():
    s = Service()
    await created(s)
    await turn(s)                           # commit at 1000
    await delta(s, " okay", 800)
    await progress(s, 900)                  # bound short of the commit
    await preview(s, 1000, "")
    assert s.finals() == [("okay", 1)] and s._turns, "not final yet"
    await delta(s, " stop", 990)
    await progress(s, 1000)
    [c] = s.corrections()
    assert (c.kind, c.added, c.dropped, c.asked, c.final, c.stop_n) == (
        "adds", ("stop",), (), "okay", "okay stop", 1)
    # a bound past the commit before its preview settles it at the preview
    s = Service()
    await created(s)
    await turn(s)
    await delta(s, " yes", 800)
    await progress(s, 1500)
    assert s._turns and not s.corrections()
    await preview(s, 1000, " please")
    assert s.finals() == [("yes please", 1)]
    [c] = s.corrections()
    assert (c.kind, c.dropped) == ("drops", ("please",))


async def test_what_the_final_changes_is_named():
    cases = [
        ("I like to know", "I'd like to know if", "changes", ("i'd", "if"), ("i",)),
        ("yeah let's go ahead and", "yeah let's go ahead", "drops", (), ("and",)),
        ("stop", "okay stop", "adds", ("okay",), ()),
        ("Paris.", "paris", "same", (), ()),
    ]
    for asked, final, kind, added, dropped in cases:
        assert stt_mod.compare_turn_text(asked, final) == (kind, added, dropped), asked


async def test_turns_settle_in_order_and_words_go_to_their_own():
    s = Service()
    await created(s)
    await turn(s)                           # commit 1 at 1000
    await turn(s)                           # commit 2 at 2000
    assert [t.at_ms for t in s._turns] == [1000, 2000]
    await delta(s, " one", 900)
    await delta(s, " two", 1900)
    await delta(s, " three", 2100)          # after both: open
    await preview(s, 1000, "")
    await preview(s, 2000, "")
    assert s.finals() == [("one", 1), ("two", 2)]
    assert s._interim_buffer == " three"
    await progress(s, 1500)
    assert len(s._turns) == 1 and s._turns[0].at_ms == 2000
    await progress(s, 2500)
    assert not s._turns and s.corrections() == []


async def test_a_word_below_a_settled_turn_is_kept_for_the_open_one():
    s = Service()
    await created(s)
    await turn(s)
    await preview(s, 1000, " hi")
    await progress(s, 1000)
    await delta(s, " late", 500)            # the engine's bound did not hold
    assert s._interim_buffer == " late" and s.interims()[-1] == " late"


# ---- words are never lost -------------------------------------------------------------

async def test_an_empty_provisional_whose_final_has_words_goes_on_as_new_words():
    s = Service()
    await created(s)
    await turn(s)
    await preview(s, 1000, "")
    assert s.finals() == [] and [f.stop_n for f in s.of(SegmentDoneFrame)] == [1]
    await delta(s, " okay", 600)
    await delta(s, " stop", 900)
    await progress(s, 1000)
    assert s.finals() == [("okay stop", NO_STOP)], "asked on the final, as words of their own"
    assert s.corrections() == []
    # and an empty one whose final is empty too is just a wordless close
    s = Service()
    await created(s)
    await turn(s)
    await preview(s, 1000, "")
    await progress(s, 1000)
    assert s.finals() == [] and s.corrections() == []


async def test_a_final_placed_nowhere_downstream_comes_back_as_its_own_final():
    s = Service()
    await created(s)
    await s.process_frame(TurnTextUnplacedFrame(stop_n=3, final="okay stop"), UP)
    assert s.finals() == [("okay stop", NO_STOP)]
    assert not any(isinstance(f, TurnTextUnplacedFrame) for f in s.pushed), "consumed"


async def test_the_wake_turn_is_corrected_on_what_follows_the_phrase():
    from teaport_brain import wake_gate
    from teaport_brain.wake_words import parse_phrases
    s = Service()
    await created(s)
    s.wake_gate = wake_gate.WakeGate(parse_phrases("hey teaport"),
                                     store=wake_gate.MicConversation())
    await turn(s)
    await delta(s, " Hey", 300)
    await delta(s, " Teaport.", 600)
    await preview(s, 1000, "")              # the phrase alone: the gate wakes, no words
    assert not s.wake_gate.asleep and s.finals() == []
    await delta(s, " What", 800)
    await delta(s, " time?", 990)
    await progress(s, 1000)
    assert s.finals() == [("What time?", NO_STOP)], s.finals()
    # asleep, no phrase anywhere: nothing goes on, and the final changes nothing
    s = Service()
    await created(s)
    s.wake_gate = wake_gate.WakeGate(parse_phrases("hey teaport"),
                                     store=wake_gate.MicConversation())
    await turn(s)
    await delta(s, " the", 300)
    await preview(s, 1000, " tv")
    await delta(s, " news", 990)
    await progress(s, 1000)
    assert s.finals() == [] and s.corrections() == [] and s.wake_gate.asleep


# ---- what slice mode turns off, and keeps ---------------------------------------------

async def test_no_carry_and_the_hint_stays():
    s = Service()
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s)
    await audio(s, 200)
    await complete(s)
    sent = s._websocket.sent
    at = max(i for i, m in enumerate(sent) if m["type"] == "input_audio_buffer.commit")
    assert sent[at].get("trailing_silence_ms"), "the hint shortens the preview's pad"
    assert at == len(sent) - 1, "nothing re-sent behind the commit"
    assert s._tail == bytearray()


class _Marks:
    """An analyzer whose marks say the caller's voice is back since the stop."""

    def __init__(self, s):
        self.s = s
        self.voice_heard = asyncio.Event()

    def voice_marks(self):
        p = self.s._passed_samples
        return SimpleNamespace(received=p, speech_end=p - 8000, stop_at=p - 1600,
                               onset_end=p, voice_end=p, paused=False, in_run=True)


async def test_a_complete_verdict_commits_at_once_over_the_callers_voice():
    for mode, held in (("segment", True), ("slice", False)):
        s = Service(turn_text=mode)
        await created(s)
        s.vad_analyzer = _Marks(s)
        await vad_start(s)
        await audio(s, 1000)
        await vad_stop(s)
        await audio(s, 100)
        await complete(s)
        assert (len(commits(s)) == 0) == held, mode
        await s._cancel_hold_expiry()


async def test_the_barge_in_commit_is_immediate_too():
    s = Service()
    await created(s)
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)
    assert [c.get("final") for c in commits(s)] == [True]
    assert s._turns[0].at_ms == 600


# ---- the report, and the session's end ------------------------------------------------

async def test_the_report_rides_on_end_through():
    stt_mod._REPORT_MARGIN_MS, was = 100, stt_mod._REPORT_MARGIN_MS
    try:
        s = Service(progress=True)
        await created(s)
        await vad_start(s)
        await audio(s, 1000)
        await vad_stop(s, stop_secs=0.5)     # speech end at 500 ms
        await progress(s, end_through=550, decoded=900)
        assert s.of(SpeechDecodedFrame) == []
        await progress(s, end_through=600, decoded=900)
        assert [f.stop_n for f in s.of(SpeechDecodedFrame)] == [1]
        # asked for by the slices alone (speculation not on the report): nothing pushed
        s = Service(progress=False)
        await created(s)
        await vad_start(s)
        await audio(s, 1000)
        await vad_stop(s, stop_secs=0.5)
        await progress(s, end_through=900)
        assert s.of(SpeechDecodedFrame) == []
    finally:
        stt_mod._REPORT_MARGIN_MS = was


async def test_a_closed_session_takes_its_unsettled_turns():
    s = Service()
    await created(s)
    await turn(s)
    await delta(s, " hi", 800)
    await preview(s, 1000, "")
    await audio(s, 200)
    await delta(s, " there", 1150)
    s._websocket = None                     # a socket already gone, as a reconnect finds it
    await s._disconnect_websocket()
    assert not s._turns and not s._open_words and s._interim_buffer == ""
    assert s.finals() == [("hi", 1)]


def main():
    stt_mod._HINT_MARGIN_MS = 100
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
