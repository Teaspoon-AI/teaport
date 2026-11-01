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
#     pushes them as a final of their own, stamped NO_STOP, through the echo guard and
#     the wake gate; over the bot an empty preview closes nothing, and the turn closes on
#     its final, stamped with its stop;
#   * a word ending just past the cut is the turn's when its preview vouched for it;
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
    SpeechPreviewFrame,
    SttProgressFrame,
    TurnVerdictFrame,
)
from teaport_brain.stt import FinalTranscriptionFrame, TeaportSTTService  # noqa: E402
from teaport_brain.endpointing import TurnTextFinalFrame  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Service(TeaportSTTService):
    """The real service, built with the setting given, its wire recorded and its pushed
    frames kept."""

    def __init__(self, turn_text="slice", progress=False, peeks=False, **kwargs):
        was = stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP
        stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP = turn_text, peeks
        try:
            super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        finally:
            stt_mod.TURN_TEXT, stt_mod.PREVIEW_AT_STOP = was
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


async def delta(s, text, end, start=None):
    await s._handle_message({"type": "transcription.delta", "delta": text,
                             "timestamp": 0.0,
                             "audio_ms": max(0, end - 200) if start is None else start,
                             "end_ms": end})


async def preview(s, through, text=""):
    await s._handle_message({"type": "transcription.preview", "through_ms": through,
                             "delta": text, "main_through_ms": 0, "credited_ms": 0})


async def progress(s, end_through, decoded=None, audio=None):
    await s._handle_message({"type": "transcription.progress",
                             "decoded_through_ms": end_through if decoded is None else decoded,
                             "audio_through_ms": end_through if audio is None else audio,
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
    assert s._interim_buffer == " it" and [w for w, *_ in s._open_words] == [" it"]


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
    await progress(s, 1000 + stt_mod._VOUCH_MS)    # " hi" never came: final at the margin
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


# ---- the peek: the preview asked at the VAD stop ----------------------------------------

async def verdict(s, complete=True, source="verdict"):
    await s.process_frame(TurnVerdictFrame(complete=complete, source=source,
                                           stop_n=s._vad_stops), UP)


async def test_a_peek_asks_at_the_stop_and_the_verdict_adopts_it():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " okay", 500)
    await vad_stop(s)                          # the peek, at the stop: 1000 ms
    [c] = commits(s)
    assert c.get("trailing_silence_ms"), "the hint at the stop: the stop's window"
    assert s._peek is not None and s._peek.at_ms == 1000 and not s._turns
    await audio(s, 200)
    await delta(s, " stop", 950)               # no turn is closed: still an interim
    assert s.interims()[-1] == " okay stop"
    await preview(s, 1000, "")
    assert [(p.stop_n, p.text) for p in s.of(SpeechPreviewFrame)] == [(1, "okay stop")]
    assert s.finals() == [], "nothing is closed on a peek"
    await verdict(s)                           # adopted: no second commit
    assert len(commits(s)) == 1 and s.finals() == [("okay stop", 1)]
    await progress(s, 1000)                    # final once the bound passes the STOP
    assert s.corrections() == [] and not s._turns


async def test_a_verdict_before_the_preview_pushes_the_turn_when_it_lands():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " yes", 700)
    await vad_stop(s)
    await verdict(s)
    assert len(commits(s)) == 1 and s.finals() == [] and s._turns
    await delta(s, " please", 990)             # the adopted turn's: never an interim
    assert s.interims()[-1] == " yes"
    await preview(s, 1000, "")
    assert s.finals() == [("yes please", 1)] and s.of(SpeechPreviewFrame) == []


async def test_a_peek_then_incomplete_then_the_caller_resuming_is_one_turn():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " so", 400)
    await vad_stop(s)                          # peek 1, at 1000
    await verdict(s, complete=False)
    await vad_start(s)                         # they go on: peek 1 is no one's
    await preview(s, 1000, " I was")
    assert s.of(SpeechPreviewFrame) == [] and s.finals() == []
    await audio(s, 1000)
    for w, e in ((" I", 1100), (" was", 1300), (" thinking", 1700)):
        await delta(s, w, e)
    await vad_stop(s)                          # peek 2, at 2000
    assert len(commits(s)) == 2 and s._peek.at_ms == 2000
    await preview(s, 2000, "")
    await verdict(s)
    assert s.finals() == [("so I was thinking", 2)], "one turn, every word once"
    await progress(s, 2000)
    assert s.corrections() == []


async def test_an_incomplete_verdict_then_the_ceiling_reuses_the_peek():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " well", 600)
    await vad_stop(s)
    await preview(s, 1000, " maybe")
    assert [p.text for p in s.of(SpeechPreviewFrame)] == ["well maybe"]
    await verdict(s, complete=False)
    await audio(s, 800)                        # the ceiling's silence, no VAD start
    await verdict(s, complete=True, source="ceiling")
    assert len(commits(s)) == 1, "the ceiling closes the turn on the peek: no commit"
    assert s.finals() == [("well maybe", 1)]
    await delta(s, " maybe", 980)              # the main transcript's own: the turn's
    await progress(s, 1000)
    assert s.corrections() == [] and s.interims() == [" well"]


async def test_an_empty_peek_then_complete_asks_on_the_final_and_loses_nothing():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await vad_stop(s)
    await preview(s, 1000, "")
    assert s.of(SpeechPreviewFrame) == [], "nothing to speculate on"
    await verdict(s)
    assert s.finals() == [] and [f.stop_n for f in s.of(SegmentDoneFrame)] == [1]
    await delta(s, " yes", 900)
    await progress(s, 1000)
    assert s.finals() == [("yes", NO_STOP)], "asked on the final, as words of their own"


async def test_one_commit_in_flight_a_flapping_stop_waits_for_the_answer():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)                          # peek 1, at 600, unanswered
    await vad_start(s)
    await audio(s, 300)
    await vad_stop(s)                          # waits: an answer is outstanding
    assert len(commits(s)) == 1 and s._peek is None
    await preview(s, 600, "")                  # peek 1's (no one's); now peek 2 goes
    assert len(commits(s)) == 2 and s._peek.at_ms == 900 and s._peek.n == 2
    # and a stop the caller resumed after before the answer came asks nothing
    await vad_start(s)
    await audio(s, 200)
    await vad_stop(s)
    await vad_start(s)
    await preview(s, 900, "")
    assert len(commits(s)) == 2 and s._peek is None


async def test_the_stop_over_the_bot_commits_a_turn_not_a_peek():
    s = Service(peeks=True)
    await created(s)
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)
    assert s._peek is None and s._turns and s._turns[0].at_ms == 600


async def test_the_strategy_is_told_the_session_peeks():
    for peeks in (True, False):
        s = Service(peeks=peeks)
        await created(s)
        [f] = s.of(SttProgressFrame)
        assert f.peeks is peeks
    s = Service(peeks=True)
    await created(s, preview=False)
    assert s.of(SttProgressFrame)[0].peeks is False, "an engine without the preview"


# ---- the 2026-10-27 review --------------------------------------------------------------

async def test_a_word_the_preview_vouches_for_stays_in_its_turn():
    # The peek slices at the stop; the main stream's last word ends a little past it (an
    # end estimate late by tens of ms). The preview has the word: it is the turn's, not
    # the open turn's -- no interim, no correction dropping it, no backstop committing it
    # as a turn of its own.
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " what's", 300)
    await delta(s, " the", 600)
    await vad_stop(s, stop_secs=0.2)
    at = s._peek.at_ms
    await preview(s, at, " weather")
    await complete(s)
    assert s.finals() == [("what's the weather", 1)]
    interims = list(s.interims())
    await delta(s, " weather", at + 10)
    await audio(s, 200)
    assert s.interims() == interims, "the turn's own word announced as an interim"
    await progress(s, at + 300)
    assert s.corrections() == [] and not s._open_words and not s._turns
    await asyncio.sleep(stt_mod._STRANDED_INTERIM_SECS + 0.1)
    assert len(commits(s)) == 1, "a backstop committed the turn's own word"


async def test_the_cut_s_margin_and_the_correction_hold_in_any_script():
    # Cyrillic, as the Latin test above: the word the preview vouched for stays in its
    # turn; and a final that differs is seen to (Cyrillic, Devanagari with its vowel
    # signs, a run of Han characters).
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " какая", 300)
    await delta(s, " сегодня", 600)
    await vad_stop(s, stop_secs=0.2)
    at = s._peek.at_ms
    await preview(s, at, " погода")
    await complete(s)
    await delta(s, " погода", at + 10)
    await progress(s, at + 300)
    assert s.finals() == [("какая сегодня погода", 1)]
    assert s.corrections() == [] and not s._open_words, s._open_words
    assert stt_mod.compare_turn_text("Привет друг", "Привет") == ("drops", (), ("друг",))
    assert stt_mod.compare_turn_text("नमस्ते दुनिया", "नमस्ते") == ("drops", (), ("दुनिया",))
    assert stt_mod.compare_turn_text("你好世界", "你好")[0] == "changes"
    assert stt_mod.compare_turn_text("Привет друг", "привет, друг!")[0] == "same"
    assert stt_mod.compare_turn_text("ＳＴＯＰ， ｐｌｅａｓｅ", "stop please")[0] == "same"


async def test_a_barge_in_in_another_script_goes_on_at_its_preview():
    s = Service()
    await created(s)
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)
    await preview(s, 600, " Стоп.")
    assert s.finals() == [("Стоп.", 1)], "a barge-in with words, deferred as if it had none"


async def test_a_vouched_word_in_before_the_verdict_is_taken_back_from_the_open_turn():
    # The same word, arriving between the peek's preview and the verdict that adopts it:
    # the open turn had it (as every word of a turn not yet closed), and the turn takes it
    # back -- it is in the provisional already, through the preview.
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " what's", 300)
    await delta(s, " the", 600)
    await vad_stop(s, stop_secs=0.2)
    at = s._peek.at_ms
    await preview(s, at, " weather")
    await delta(s, " weather", at + 10)
    await complete(s)
    assert s.finals() == [("what's the weather", 1)]
    assert not s._open_words and s._interim_buffer == "", s._open_words
    await progress(s, at + 300)
    assert s.corrections() == [] and not s._turns
    await asyncio.sleep(stt_mod._STRANDED_INTERIM_SECS + 0.1)
    assert len(commits(s)) == 1, "a backstop committed the turn's own word"


async def test_a_word_the_cut_falls_inside_is_the_turns_when_the_preview_has_it():
    # A commit with no pause behind it (the backstop) cuts inside a word: it starts before
    # the cut and ends well past it. The preview, decoded to the cut, has it whole. The
    # turn takes it -- and its later pieces, which share its positions -- and is not final
    # until no word starting before the cut can still come (audio_through_ms).
    s = Service()
    await created(s)
    await turn(s)                                  # at 1000
    await delta(s, " vertically", 700, start=300)
    await preview(s, 1000, " horizontally.")
    await progress(s, 1250, audio=900)
    assert s._turns, "final while a word starting before the cut could still come"
    await delta(s, " horizontally", 1400, start=800)
    await delta(s, ".", 1400, start=800)
    await progress(s, 1400, audio=1000)
    assert not s._turns and s.corrections() == [], s.corrections()
    assert not s._open_words and s._interim_buffer == ""


async def test_a_words_later_pieces_follow_it_into_its_turn():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " to", 700)
    await vad_stop(s, stop_secs=0.2)
    at = s._peek.at_ms
    await preview(s, at, " 1300.")
    await complete(s)
    for piece in (" 1", "3", "0", "0", "."):
        await delta(s, piece, at + 60, start=at - 300)
    await progress(s, at + 300)
    assert not s._turns and s.corrections() == [], s.corrections()
    assert not s._open_words


async def test_a_preview_soon_after_a_commit_does_not_ask_the_turn_befores_word_again():
    # A commit inside a word (the backstop), then the VAD stop's commit half a second
    # later: the second preview is answered with the word the first turn's preview
    # vouched for, which the main stream has not sent yet. It is the first turn's: the
    # second turn is not asked on it again.
    s = Service()
    await created(s)
    await turn(s)                                  # at 1000
    await turn(s)                                  # at 2000
    await preview(s, 1000, " further?")
    await preview(s, 2000, " further? Okay.")
    assert s.finals() == [("further?", 1), ("Okay.", 2)], s.finals()
    await delta(s, " further", 1150, start=800)
    await delta(s, "?", 1150, start=800)
    await delta(s, " Okay", 1800, start=1500)
    await delta(s, ".", 1800, start=1500)
    await progress(s, 2000)
    assert not s._turns and s.corrections() == [], s.corrections()


async def test_the_turn_before_s_words_are_trimmed_past_one_its_preview_missed():
    # The earlier preview missed its turn's first word but had its second; the later
    # preview has both. The second is the earlier turn's and is not asked again; the
    # first, which the earlier turn never vouched for, stays (its final adds it there).
    s = Service()
    await created(s)
    await turn(s)                                  # at 1000
    await delta(s, " so", 300, start=100)
    await turn(s)                                  # at 2000
    await preview(s, 1000, " true.")
    await preview(s, 2000, " very true. Thanks.")
    assert s.finals() == [("so true.", 1), ("very Thanks.", 2)], s.finals()


async def test_the_turn_before_s_words_are_trimmed_past_one_the_later_preview_lacks():
    # The earlier preview vouched for two words; the later preview lacks the first (a
    # lossy drain) and has the second: it is still the earlier turn's, not asked again.
    s = Service()
    await created(s)
    await turn(s)                                  # at 1000
    await turn(s)                                  # at 2000
    await preview(s, 1000, " very true.")
    await preview(s, 2000, " true. Thanks.")
    assert s.finals() == [("very true.", 1), ("Thanks.", 2)], s.finals()


async def test_a_reclaimed_word_s_later_pieces_follow_it_into_the_turn():
    # Its first pieces came in between the peek's preview and the verdict, and the
    # adopted turn took them back from the open turn; the rest come after: they follow.
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " to", 700)
    await vad_stop(s, stop_secs=0.2)
    at = s._peek.at_ms
    await preview(s, at, " 1300.")
    for piece in (" 1", "3"):
        await delta(s, piece, at + 60, start=at - 300)
    await complete(s)                              # adopts the peek, takes them back
    for piece in ("0", "0", "."):
        await delta(s, piece, at + 60, start=at - 300)
    await progress(s, at + 300)
    assert not s._open_words and s.corrections() == [], (s._open_words, s.corrections())


async def test_a_word_past_the_cut_the_preview_did_not_have_is_the_next_turns():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await delta(s, " last", 400)
    await delta(s, " week", 700)
    await vad_stop(s)
    at = s._peek.at_ms
    await preview(s, at, "")
    await complete(s)
    await delta(s, " I", at + 50)              # the next utterance's first word
    assert s.interims()[-1] == " I" and [w for w, *_ in s._open_words] == [" I"]
    await progress(s, at + 300)
    assert s.corrections() == []


async def test_a_barge_in_with_an_empty_preview_closes_on_its_final():
    # Over the bot the preview can be empty while the main stream has the words: the
    # turn closes on the final, stamped with its stop, so the words are decided as the
    # barge-in they are (the pause still holding the reply) -- not a wordless close now
    # and the words later, unpaused, as words of no stop.
    s = Service(peeks=True)
    await created(s)
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)
    await preview(s, 600, "")
    assert s.finals() == [] and s.of(SegmentDoneFrame) == [], "closed on the empty preview"
    await delta(s, " Stop.", 450)
    await progress(s, 600)
    assert s.finals() == [("Stop.", 1)]


async def test_a_barge_in_heard_only_as_the_bots_own_words_closes_on_its_final():
    # The room mic over the bot: the provisional is the bot's reply heard back, which the
    # echo guard takes off whole. As with an empty preview, the turn closes on its final,
    # stamped with its stop -- the caller's "stop" is the barge-in, not new words after it.
    from teaport_brain import echo_guard
    class Clock:
        t = 100.0

        def __call__(self):
            return self.t
    clock = Clock()
    s = Service()
    await created(s)
    s.echo_guard = echo_guard.EchoGuard(clock=clock)
    for w in "the weather today is sunny and warm".split():
        s.echo_guard.bot.played(w)              # a word every 0.3 s
        clock.t += 0.3
    clock.t = 100.5                             # its echo starts arriving 0.5 s in
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 1000)
    for w, e in ((" the", 200), (" weather", 400), (" today", 600), (" is", 700)):
        await delta(s, w, e)
    await vad_stop(s)
    await preview(s, 1000, " sunny")
    assert s.finals() == [] and s.of(SegmentDoneFrame) == [], "closed on the bot's words"
    await delta(s, " sunny", 800)
    await delta(s, " stop", 950)
    await progress(s, 1000)
    assert s.finals() == [("stop", 1)], s.finals()


async def test_a_barge_in_waiting_on_its_final_holds_the_reply_s_pause():
    # The pause resumes TEAPORT_BARGE_PAUSE_RESUME_S after the caller stops; a barge-in
    # whose final comes later (the decoder behind, ~2 s on the box) holds it until its
    # close, so a lone "Stop." is decided with the reply paused -- the unpaused guard
    # would drop it as one word.
    import time
    import test_barge_pause as tbp
    from pipecat.frames.frames import (TTSAudioRawFrame, VADUserStartedSpeakingFrame,
                                       VADUserStoppedSpeakingFrame)
    out, p, task, run = await tbp._pauser_rig()           # resumes 0.3 s after a stop
    try:
        await task.queue_frames([TTSAudioRawFrame(tbp._pcm(0), tbp.SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)
        s = Service()
        await created(s)
        s.barge_hold = lambda on: p.hold_resume(on, who="the turn's final")
        await s.process_frame(BotStartedSpeakingFrame(), UP)
        await task.queue_frame(VADUserStartedSpeakingFrame())
        await vad_start(s)
        await audio(s, 600)
        await asyncio.sleep(0.4)                          # 0.4 s of speech: no blip
        await task.queue_frame(VADUserStoppedSpeakingFrame())
        await vad_stop(s)
        await preview(s, 600, "")                         # over the bot: deferred
        await asyncio.sleep(tbp.RESUME_S + 0.4)           # past the resume window
        assert [c[1] for c in out.calls] == [True], f"resumed under the deferral {out.calls}"
        await delta(s, " Stop.", 450)
        await progress(s, 600)
        assert s.finals() == [("Stop.", 1)]
        assert [c[1] for c in out.calls] == [True], "resumed before the close went out"
        # Its close out, the hold is let go: a quiet line resumes on the window.
        await asyncio.sleep(tbp.RESUME_S + 0.2)
        assert [c[1] for c in out.calls] == [True, False], out.calls
        # And the judge letting go does not let go a turn's hold, nor the other way.
        p._paused, p._holds = True, set()
        p.hold_resume(True, who="the turn's final")
        p.hold_resume(True)
        p.hold_resume(False)
        assert p._held and p._resume_task is None
    finally:
        await task.cancel()
        await run


async def test_the_pause_s_cap_closes_a_held_barge_in_before_it_resumes():
    # The decoder far behind, the end bound frozen: the barge-in's final never comes
    # before the pause's cap. At the cap it closes on the words in hand -- the reply still
    # paused -- and only then does the reply resume.
    import test_barge_pause as tbp
    from pipecat.frames.frames import (TTSAudioRawFrame, VADUserStartedSpeakingFrame,
                                       VADUserStoppedSpeakingFrame)
    out, p, task, run = await tbp._pauser_rig(max_secs=1.0)
    try:
        await task.queue_frames([TTSAudioRawFrame(tbp._pcm(0), tbp.SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), UP)
        await asyncio.sleep(0.05)
        s = Service()
        await created(s)
        s.barge_hold = lambda on: p.hold_resume(on, who="the turn's final")
        p.before_cap = s.close_held
        paused_at_final = []
        pushed = s.push_frame

        async def push(frame, direction=None):
            if isinstance(frame, FinalTranscriptionFrame):
                paused_at_final.append(p.paused)
            await pushed(frame) if direction is None else await pushed(frame, direction)
        s.push_frame = push
        await s.process_frame(BotStartedSpeakingFrame(), UP)
        await task.queue_frame(VADUserStartedSpeakingFrame())
        await vad_start(s)
        await audio(s, 600)
        await asyncio.sleep(0.4)
        await task.queue_frame(VADUserStoppedSpeakingFrame())
        await vad_stop(s)
        await preview(s, 600, "")                     # held
        await delta(s, " Stop.", 450)                 # in hand; the final never comes
        await asyncio.sleep(1.0)                      # past the cap
        assert s.finals() == [("Stop.", 1)], s.finals()
        assert paused_at_final == [True], "closed after the reply resumed"
        assert [c[1] for c in out.calls] == [True, False], out.calls
        assert not s._turns
    finally:
        await task.cancel()
        await run


async def test_two_held_barge_ins_hold_the_pause_until_the_last_one_closes():
    s = Service()
    await created(s)
    held = []
    s.barge_hold = held.append
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    for _ in range(2):
        await vad_start(s)
        await audio(s, 600)
        await vad_stop(s)
    await preview(s, 600, "")
    await preview(s, 1200, "")
    assert held == [True]
    await delta(s, " Stop.", 450)
    await progress(s, 600)                            # the first closes
    assert held == [True], f"the first of two let go the pause: {held}"
    await delta(s, " now.", 1000)
    await progress(s, 1200)
    assert held == [True, False] and s.finals() == [("Stop.", 1), ("now.", 2)], (
        held, s.finals())


async def test_a_lost_session_closes_a_deferred_turn_on_the_words_in_hand():
    from teaport_brain.endpointing import SegmentResetFrame
    s = Service()
    await created(s)
    held = []
    s.barge_hold = held.append
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    await vad_start(s)
    await audio(s, 600)
    await vad_stop(s)
    await preview(s, 600, "")                         # deferred, the pause held
    await delta(s, " Stop.", 450)                     # its word in hand, no final yet
    s._websocket = None                               # the session goes
    await s._disconnect_websocket()
    assert s.finals() == [("Stop.", 1)] and held == [True, False], (s.finals(), held)
    order = [type(f).__name__ for f in s.pushed
             if isinstance(f, (FinalTranscriptionFrame, SegmentResetFrame))]
    assert order == ["FinalTranscriptionFrame", "SegmentResetFrame"], order


async def test_a_turn_after_one_still_owing_its_words_waits_on_its_own_final():
    # The earlier turn put nothing with words on at its preview -- deferred over the bot,
    # cut short, or an empty provisional -- and the next commit comes before its final:
    # the next preview has its words too (unsent then). Each turn delivers its own words,
    # in order, once.
    s = Service()                                     # deferred over the bot
    await created(s)
    await s.process_frame(BotStartedSpeakingFrame(), UP)
    for _ in range(2):
        await vad_start(s)
        await audio(s, 600)
        await vad_stop(s)
    await preview(s, 600, "")
    await preview(s, 1200, " Stop. Go back.")
    assert s.finals() == [], s.finals()
    await delta(s, " Stop.", 450)
    await delta(s, " Go", 900)
    await delta(s, " back.", 1100)
    await progress(s, 1400)
    assert s.finals() == [("Stop.", 1), ("Go back.", 2)] and s.corrections() == []

    s = Service()                                     # cut short
    await created(s)
    await turn(s)
    await s._handle_message({"type": "transcription.preview", "through_ms": 1000,
                             "delta": " wea", "complete": False})
    await turn(s)
    await preview(s, 2000, " weather today?")
    await delta(s, " weather", 900)
    await delta(s, " today?", 1800)
    await progress(s, 2200)
    assert s.finals() == [("weather", 1), ("today?", 2)] and s.corrections() == []

    s = Service()                                     # an empty provisional
    await created(s)
    await turn(s)
    await preview(s, 1000, "")
    await turn(s)
    await preview(s, 2000, " Yes. And also tea.")
    await delta(s, " Yes.", 900)
    for w, e in ((" And", 1300), (" also", 1500), (" tea.", 1800)):
        await delta(s, w, e)
    await progress(s, 2200)
    assert s.finals() == [("Yes.", NO_STOP), ("And also tea.", 2)], s.finals()
    assert s.corrections() == []


async def test_a_peek_previewed_while_a_turn_owed_words_waits_even_once_that_settled():
    # The backstop's turn has an empty preview; the VAD stop's peek is answered with that
    # turn's unsent word; then that turn's final delivers it, and only then the verdict
    # adopts the peek. The peek's text was taken while the word was owed: the turn waits
    # on its own final and does not deliver the word again.
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await s._send_commit(final=True, why="backstop")        # at 1000, the VAD stuck
    await preview(s, 1000, "")
    await audio(s, 300)
    await vad_stop(s)                                         # the peek at 1300
    at = s._peek.at_ms
    await preview(s, at, " Yes. Please.")
    await delta(s, " Yes.", 900)
    await progress(s, 1000)                                   # the first turn's final
    assert s.finals() == [("Yes.", NO_STOP)], s.finals()
    await complete(s)                                         # adopts the peek
    await delta(s, " Please.", 1250)
    await progress(s, at)        # final at its position: "Yes.", the first turn's, is spent
    assert s.finals() == [("Yes.", NO_STOP), ("Please.", 1)], s.finals()


async def test_no_speculation_on_a_peek_while_an_earlier_turn_owes_words():
    s = Service(peeks=True)
    await created(s)
    await vad_start(s)
    await audio(s, 1000)
    await s._send_commit(final=True, why="backstop")        # at 1000: an empty preview
    await preview(s, 1000, "")
    await audio(s, 300)
    await vad_stop(s)
    await preview(s, s._peek.at_ms, " Yes. Please.")
    assert s.of(SpeechPreviewFrame) == [], "speculated on words an earlier turn owes"


async def test_a_turn_after_one_of_only_the_bot_s_words_waits_on_its_final():
    # Only the bot's own words heard back went on (a wordless close): that turn's final
    # may still deliver the caller's words, so the next turn goes after them.
    from teaport_brain import echo_guard

    class Clock:
        t = 100.0

        def __call__(self):
            return self.t
    clock = Clock()
    s = Service()
    await created(s)
    s.echo_guard = echo_guard.EchoGuard(clock=clock)
    for w in "the weather today is sunny and warm".split():
        s.echo_guard.bot.played(w)
        clock.t += 0.3
    clock.t = 100.5
    await vad_start(s)
    await audio(s, 1000)
    for w, e in ((" the", 200), (" weather", 400), (" today", 600), (" is", 700)):
        await delta(s, w, e)
    await vad_stop(s)
    await complete(s)                                         # at 1000, not over the bot
    await preview(s, 1000, " sunny")
    assert s.finals() == [] and [f.stop_n for f in s.of(SegmentDoneFrame)] == [1]
    await turn(s)                                             # at 2000
    await preview(s, 2000, " stop now.")
    assert s.finals() == [], "went on ahead of the turn before's words"


async def test_a_wake_turn_after_room_talk_the_gate_kept_does_not_wait():
    # The gate asleep keeps the room's words (nothing goes on, and the final is left as
    # it went): a turn after it owes it nothing and goes on at its preview.
    from teaport_brain import wake_gate
    from teaport_brain.wake_words import parse_phrases
    s = Service()
    await created(s)
    s.wake_gate = wake_gate.WakeGate(parse_phrases("hey teaport"),
                                     store=wake_gate.MicConversation())
    await turn(s)                                  # at 1000: the room talking
    await delta(s, " nice", 400)
    await preview(s, 1000, " weather")
    await turn(s)                                  # at 2000: the wake
    await preview(s, 2000, " Hey Teaport, what time?")
    assert not s.wake_gate.asleep and s.finals() == [("what time?", 2)], s.finals()


async def test_an_empty_provisionals_words_go_through_the_wake_gate():
    from teaport_brain import wake_gate
    from teaport_brain.wake_words import parse_phrases
    s = Service(peeks=True)
    await created(s)
    s.wake_gate = wake_gate.WakeGate(parse_phrases("hey teaport"),
                                     store=wake_gate.MicConversation())
    await turn(s)
    await preview(s, 1000, "")
    for w, e in ((" Hey", 300), (" Teaport,", 500), (" what", 700), (" time?", 900)):
        await delta(s, w, e)
    await progress(s, 1000)
    assert not s.wake_gate.asleep and s.finals() == [("what time?", NO_STOP)], s.finals()


async def test_an_engine_with_the_preview_but_no_progress_runs_on_segments():
    s = Service(peeks=True)
    await s._handle_message({"type": "session.created", "id": "s", "created": 1,
                             "capabilities": ["transcription.preview"]})
    assert not s._slicing


async def test_a_dropped_word_then_a_backstop_is_counted():
    s = Service()
    await created(s)
    lines = []
    sink = stt_mod.logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        await vad_start(s)
        await audio(s, 1000)
        await delta(s, " capital", 500)
        await vad_stop(s)
        await complete(s)
        await preview(s, 1000, " cities")
        await delta(s, " cities", 1500)        # past the cut by more than the margin
        await progress(s, 1000 + stt_mod._VOUCH_MS)
        assert [c.kind for c in s.corrections()] == ["drops"]
        await asyncio.sleep(stt_mod._STRANDED_INTERIM_SECS + 0.1)
        assert any("[TURN]" in m and "backstop" in m and "dropped" in m for m in lines), lines
    finally:
        stt_mod.logger.remove(sink)


# ---- nothing waits on a final (the 2026-10-27 engine review) ----------------------------

def _lines():
    lines = []
    return lines, stt_mod.logger.add(lambda m: lines.append(m.record["message"]), level="INFO")


async def test_a_final_that_never_comes_times_out_on_the_decoded_clock():
    # end_through_ms freezes through the pause (the engine's 03e4223 prototype): it settles
    # as its provisional text once the decoder is _FINAL_TIMEOUT_MS past its position (and
    # the margin, its preview vouching for a word still to come).
    s = Service()
    await created(s)
    lines, sink = _lines()
    due = 1000 + stt_mod._FINAL_TIMEOUT_MS + stt_mod._VOUCH_MS
    try:
        await turn(s)
        await delta(s, " hi", 500)
        await preview(s, 1000, " there")
        assert s.finals() == [("hi there", 1)]
        await progress(s, 520, decoded=due - 100)
        assert len(s._turns) == 1, "settled before its timeout"
        await progress(s, 520, decoded=due)
        assert not s._turns and s.corrections() == []
        assert any("[TURN]" in m and "final timeout" in m and "provisional" in m
                   for m in lines), lines
    finally:
        stt_mod.logger.remove(sink)


async def test_a_vouched_turn_s_timeout_allows_for_the_margin():
    # The engine sends a word before it has decoded 2 s past the word's start, and a word
    # the preview vouched for may start up to the margin past the cut.
    s = Service()
    await created(s)
    await turn(s)                                  # at 1000
    await preview(s, 1000, " there")               # vouches for a word still to come
    await progress(s, 1100, decoded=1000 + 2000 + stt_mod._VOUCH_MS - 1, audio=900)
    assert s._turns, "timed out inside the margin"
    await progress(s, 1100, decoded=1000 + 2000 + stt_mod._VOUCH_MS, audio=900)
    assert not s._turns


async def test_the_next_turn_is_not_held_behind_a_final_that_never_came():
    s = Service()
    await created(s)
    await turn(s)                           # at 1000
    await turn(s)                           # at 2000
    await delta(s, " one", 900)
    await preview(s, 1000, "")
    await preview(s, 2000, " two")
    await progress(s, 500, decoded=3000)    # the first's final is overdue, not the second's
    assert [t.at_ms for t in s._turns] == [2000]
    await delta(s, " two", 1900)
    await progress(s, 2000, decoded=3100)
    assert not s._turns and s.corrections() == []


async def test_a_decoder_behind_real_time_does_not_time_a_final_out():
    # Live 2026-10-31 on the Orin Nano: the end bound past the cut, the start bound not
    # yet (a word the preview vouched for, begun before the cut, still to come), and the
    # decoder 2 s behind real time. The turn waits for the word, on the decoder's clock.
    s = Service()
    await created(s)
    lines, sink = _lines()
    try:
        await turn(s)                                  # at 1000
        await delta(s, " in", 400, start=200)
        await delta(s, " the", 600, start=400)
        await preview(s, 1000, " kitchen.")
        await progress(s, 1250, decoded=1089, audio=700)
        await asyncio.sleep(2.2)                       # 2 s of wall clock past the commit
        assert s._turns, "timed out on the wall clock with the decoder behind"
        await delta(s, " kitchen", 1300, start=800)
        await delta(s, ".", 1300, start=800)
        await progress(s, 1400, decoded=1500, audio=1100)
        assert not s._turns and s.corrections() == [] and not s._open_words
        assert not any("final timeout" in m for m in lines), lines
    finally:
        stt_mod.logger.remove(sink)


async def test_a_timeout_says_what_it_waited_for_and_a_backstop_after_it_is_counted():
    s = Service()
    await created(s)
    lines, sink = _lines()
    try:
        await turn(s)                                  # at 1000
        await delta(s, " in", 400, start=200)
        await preview(s, 1000, " the kitchen.")
        due = 1000 + stt_mod._FINAL_TIMEOUT_MS + stt_mod._VOUCH_MS
        await progress(s, 1250, decoded=due, audio=700)
        [line] = [m for m in lines if "final timeout" in m]
        assert "audio_through_ms 700" in line and "2 its preview still vouches for" in line
        await delta(s, " kitchen", 3100, start=3000)   # late: the open turn's now
        await asyncio.sleep(stt_mod._STRANDED_INTERIM_SECS + 0.1)
        assert any("[TURN]" in m and "timed out without" in m and "backstop" in m
                   for m in lines), lines
    finally:
        stt_mod.logger.remove(sink)


async def test_a_timed_out_turn_s_late_word_goes_to_the_open_turn_not_the_next():
    # After the wall-clock backstop (an engine that stopped reporting), the timed-out
    # turn's words come at last, with the next turn not yet final: they are not that
    # turn's (they end before its stretch) and its correction must not add them.
    saved = stt_mod._FINAL_BACKSTOP_SECS
    stt_mod._FINAL_BACKSTOP_SECS = 0.3
    lines, sink = _lines()
    try:
        s = Service()
        await created(s)
        await turn(s)                              # at 1000
        await delta(s, " in", 400, start=200)
        await preview(s, 1000, " the kitchen.")
        await asyncio.sleep(0.2)
        await turn(s)                              # at 2000
        await preview(s, 2000, " Thanks.")
        await asyncio.sleep(0.15)                  # the first's final: the backstop
        assert [t.at_ms for t in s._turns] == [2000]
        await delta(s, " the", 600, start=450)
        await delta(s, " kitchen.", 900, start=650)
        await delta(s, " Thanks.", 1700, start=1500)
        await progress(s, 2000)
        assert s.corrections() == [], [(c.kind, c.final) for c in s.corrections()]
        assert [w for w, *_ in s._open_words] == [" the", " kitchen."], s._open_words
        assert any("was settled" in m for m in lines), lines
    finally:
        stt_mod._FINAL_BACKSTOP_SECS = saved
        stt_mod.logger.remove(sink)


async def test_a_final_times_out_on_the_wall_clock_too(monkeypatch=None):
    saved = stt_mod._FINAL_BACKSTOP_SECS
    stt_mod._FINAL_BACKSTOP_SECS = 0.2
    try:
        s = Service()
        await created(s)
        await turn(s)
        await preview(s, 1000, " hello")
        await asyncio.sleep(0.35)               # no progress at all: the engine stalled
        assert not s._turns and s.corrections() == []
        # And a barge-in waiting on its final closes on the words in hand, stamped.
        s = Service()
        await created(s)
        await s.process_frame(BotStartedSpeakingFrame(), UP)
        await vad_start(s)
        await audio(s, 600)
        await vad_stop(s)
        await preview(s, 600, "")
        await delta(s, " Stop.", 450)
        assert s.finals() == [] and s.of(SegmentDoneFrame) == []
        await asyncio.sleep(0.35)
        assert s.finals() == [("Stop.", 1)]
    finally:
        stt_mod._FINAL_BACKSTOP_SECS = saved


async def test_an_empty_peek_preview_says_audio_in_no_words_out():
    # The double-talk diagnostic: speech the VAD heard, and the preview at its stop has
    # no words. With the peek (slice mode's default) that commit is the peek's.
    s = Service(peeks=True)
    await created(s)
    lines, sink = _lines()
    try:
        await vad_start(s)
        await audio(s, 1000)
        await vad_stop(s)
        await preview(s, s._peek.at_ms, "")
        assert any("audio in, no words out" in m for m in lines), lines
    finally:
        stt_mod.logger.remove(sink)


async def test_a_cut_short_preview_is_no_preview_the_turn_closes_on_its_final():
    s = Service(peeks=True)
    await created(s)
    lines, sink = _lines()
    try:
        await vad_start(s)
        await audio(s, 1000)
        await delta(s, " what's", 300)
        await delta(s, " the", 600)
        await vad_stop(s, stop_secs=0.2)
        at = s._peek.at_ms
        await s._handle_message({"type": "transcription.preview", "through_ms": at,
                                 "delta": " wea", "complete": False, "main_through_ms": 0,
                                 "credited_ms": 0})
        assert s.of(SpeechPreviewFrame) == [], "speculated on a cut-short preview"
        await complete(s)
        assert s.finals() == [] and s.of(SegmentDoneFrame) == [], "closed on its pieces"
        await delta(s, " weather", at - 50)
        await progress(s, at)
        assert s.finals() == [("what's the weather", 1)] and s.corrections() == []
        assert any("cut short" in m for m in lines), lines
        # A turn's (the preview at the verdict) the same.
        s = Service()
        await created(s)
        await turn(s)
        await delta(s, " so", 400)
        await s._handle_message({"type": "transcription.preview", "through_ms": 1000,
                                 "delta": " ye", "complete": False, "main_through_ms": 0,
                                 "credited_ms": 0})
        assert s.finals() == []
        await delta(s, " yes", 900)
        await progress(s, 1000)
        assert s.finals() == [("so yes", 1)] and s.corrections() == []
    finally:
        stt_mod.logger.remove(sink)


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
