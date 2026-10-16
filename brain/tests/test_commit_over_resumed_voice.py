#
# Unit test: a commit is never drawn over the caller's resumed words (issue #140).
#
# On a 3.5-minute Talk session (2026-10-10) the brain lost words in 4 of ~24 utterances,
# all spoken in the clear, every one where the caller started again just as the STT
# committed a segment. Two mechanisms, both reproduced here against the service as it
# was (no VAD analyzer to read) and then shown fixed (the session's analyzer wired in):
#
#   * The hint over-claims. The commit counted everything since the stop's speech end
#     as silence, the caller's resumed voice included, and the engine skipped decoding
#     that much of the tail. Worst on the ceiling's commit, which claims the whole hold.
#   * The boundary falls mid-word. A COMPLETE verdict committed while the analyzer was
#     still behind on the audio that came in during Smart Turn's inference, and the
#     caller's next word had begun in it.
#
# Everything runs on the REAL analyzer the session builds -- pipecat's VAD state machine
# under the speech-onset mixin, with Silero's confidence scripted per 32 ms chunk -- in
# lockstep with the real service, frame by frame, the way the user aggregator drives it
# behind the STT: its VAD starts and stops are what reach the service, and the audio
# that came in during an inference waits in a backlog until the test lets it through.
# 0.9 is speech by both tests, 0.65 is voice the onset test hears and the VAD does not,
# 0.1 is quiet.
#
# What these pin, in order:
#
#   * the ceiling's commit no longer claims voice heard during the hold: with the
#     voice still going it waits, and the VAD start then keeps the segment whole; with
#     a cough long over, it commits at once and counts the hint from the cough's end;
#   * a COMPLETE verdict waits for the analyzer to hear the inference's backlog, and a
#     word starting in it holds the boundary, so the resumed sentence is one segment and
#     one commit, stamped with the stop that closes it;
#   * a cough shorter than an onset costs nothing past its own end; an onset the VAD
#     never starts on costs the stop's own window of quiet, no more, and the segment
#     line says what the boundary was held for;
#   * voice inside the stop's own window is not voice since the stop: the verdict
#     commits at once (the stop's position, and the filter on it, are what decide);
#   * with no voice since the stop, a lagging analyzer costs only its backlog: the
#     commit follows its catch-up at once, woken by it, with the hint placed on the
#     analyzer's own speech end;
#   * the bounds: voice that never pauses commits at the session's ceiling, and the
#     third such commit RUNNING stands the wait down -- any other commit breaks the
#     run, a pause's or a clean turn's, and so does a VAD start during a wait; an
#     analyzer that misses the catch-up bound is passed over, and is switched off only
#     if it has still not got there by the next verdict (slowness is logged once, not
#     punished), as are counts that disagree outright and marks that cannot be read;
#     and audio passed down unsent (a dead socket) is not a disagreement;
#   * a stop pipecat forces, which the analyzer never trips, does not take the last
#     utterance's speech end;
#   * a second verdict during the wait changes nothing, the bound included; and a wait
#     that fails still commits the segment.
#
# Run as a script: python test_commit_over_resumed_voice.py (or via test_suite.py).
# `pytest` on this file directly does not run its async tests.
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams, VADState  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain import speech_onset  # noqa: E402
from teaport_brain.endpointing import TurnVerdictFrame  # noqa: E402
from teaport_brain.speech_onset import SpeechOnsetMixin  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SAMPLE_RATE = 16000
CHUNK = 512                                         # one Silero chunk, 32 ms
FRAME = b"\x00\x02" * CHUNK
STOP_SECS = 0.2                                     # Talk's VAD stop: 6 chunks to trip
PAUSE = 7                                           # chunks of quiet the onset's offset needs
MARGIN = 100
CEILING = 0.15                                      # the session's ceiling, shortened
CATCHUP = 0.05                                      # _VOICE_CATCHUP_SECS, shortened

SPEECH, VOICE, QUIET = 0.9, 0.65, 0.1

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


def ms(chunks):
    return chunks * CHUNK * 1000 // SAMPLE_RATE


class Recorder(TeaportSTTService):
    """The real service, its wire recorded; audio driven through the real frame path."""

    def __init__(self, **kwargs):
        super().__init__(url="ws://127.0.0.1:1/none", **kwargs)
        self._websocket = WireRecorder()
        self.smartturn_stop_secs = CEILING
        self.pushed = []

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


class Scripted(VADAnalyzer):
    """pipecat's VAD state machine with Silero's confidence scripted, volume gate open."""

    def __init__(self, stop_secs=STOP_SECS):
        super().__init__(sample_rate=SAMPLE_RATE,
                         params=VADParams(stop_secs=stop_secs, start_secs=0.2,
                                          min_volume=0.0))
        self.conf = 0.0
        self.fail_next = False

    def num_frames_required(self):
        return CHUNK

    def voice_confidence(self, buffer):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("onnx: transient failure")
        return self.conf


class Line:
    """The service and the session's analyzer, driven as the pipeline drives them."""

    def __init__(self, wired=True, stop_secs=STOP_SECS):
        self.stt = Recorder()
        self.stop_secs = stop_secs
        self.vad = type("Onset", (SpeechOnsetMixin, Scripted), {})(stop_secs=stop_secs)
        self.vad.set_sample_rate(SAMPLE_RATE)
        if wired:
            self.stt.vad_analyzer = self.vad
        self.backlog = []
        self.speaking = False

    async def feed(self, chunks, conf, behind=False):
        """`chunks` frames through the service, then to the analyzer unless it is
        `behind` (queued behind an inference until catch_up)."""
        for _ in range(chunks):
            f = InputAudioRawFrame(audio=FRAME, sample_rate=SAMPLE_RATE, num_channels=1)
            await self.stt.process_frame(f, DOWN)
            if behind:
                self.backlog.append((f.audio, conf))
            else:
                await self._analyze(f.audio, conf)

    async def _analyze(self, buf, conf):
        self.vad.conf = conf
        try:
            state = await self.vad.analyze_audio(buf)
        except Exception:  # noqa: BLE001 -- pipecat's aggregator swallows it too
            return
        if state == VADState.SPEAKING and not self.speaking:
            self.speaking = True
            await self.stt.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)
        elif state == VADState.QUIET and self.speaking:
            self.speaking = False
            await self.stt.process_frame(
                VADUserStoppedSpeakingFrame(stop_secs=self.stop_secs), UP)

    async def catch_up(self):
        backlog, self.backlog = self.backlog, []
        for buf, conf in backlog:
            await self._analyze(buf, conf)

    async def utterance(self, chunks=20):
        """Speech, then the VAD's stop."""
        await self.feed(chunks, SPEECH)
        assert self.speaking, "the VAD started on the speech"
        await self.feed(self.vad._vad_stop_frames, QUIET)
        assert not self.speaking, "and stopped on the quiet"

    async def verdict(self, complete, source="verdict"):
        await self.stt.process_frame(TurnVerdictFrame(complete=complete, source=source,
                                                      stop_n=self.stt._vad_stops), UP)

    def commits(self):
        return self.stt._websocket.commits()

    def hints(self):
        return [m.get("trailing_silence_ms") for m in self.commits()]


async def settle():
    """Let the service's wait task run on what the analyzer has just heard."""
    for _ in range(3):
        await asyncio.sleep(0)


class Journal:
    def __init__(self, level="DEBUG"):
        self.lines = []
        self._level = level

    def __enter__(self):
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]),
                              level=self._level)
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)

    def having(self, text):
        return [x for x in self.lines if text in x]


# ---- mechanism 1: the hint over-claims -------------------------------------------------

async def the_ceiling_with_the_caller_back(wired):
    """An INCOMPLETE stop; during the hold the caller starts again with voice the VAD
    does not start on, and the ceiling's verdict lands over it."""
    line = Line(wired)
    await line.utterance()
    await line.verdict(False)
    await line.feed(9, QUIET)
    await line.feed(9, VOICE)                      # "a fusion" ...
    await line.verdict(True, source="ceiling")
    await settle()
    return line


async def test_reproduced_the_ceilings_commit_claimed_the_resumed_voice_as_silence():
    line = await the_ceiling_with_the_caller_back(wired=False)
    assert line.hints() == [200 + ms(18) - MARGIN], (
        "without the analyzer the hint counts the whole hold, voice included: the "
        f"engine skips decoding the caller's resumed words; got {line.hints()}")


async def test_the_ceiling_waits_over_current_voice_and_the_start_keeps_the_segment():
    line = await the_ceiling_with_the_caller_back(wired=True)
    assert line.commits() == [], "the ceiling's commit must not be drawn over current voice"
    await line.feed(10, SPEECH)                    # ... and the VAD admits it
    assert line.speaking
    await line.feed(10, SPEECH)                    # "... reaction that generates light"
    await line.feed(6, QUIET)
    await line.verdict(True)
    assert len(line.commits()) == 1, f"one segment for the sentence; got {line.commits()}"
    assert line.stt._commits[0].n == 2, "the commit answers the stop that closed it"
    assert line.hints() == [max(ms(6) - MARGIN, stt_mod._REPRIME_MS)], (
        "the hint counts from the second stop's own speech end, as the analyzer placed "
        f"it (the margin giving way at the re-prime floor); got {line.hints()}")
    await asyncio.sleep(CEILING + 0.05)
    assert len(line.commits()) == 1, "the abandoned wait must not commit later"


async def test_a_cough_long_over_commits_at_the_ceiling_with_the_hint_from_its_end():
    line = Line()
    await line.utterance()
    await line.verdict(False)
    await line.feed(9, QUIET)
    await line.feed(2, VOICE)                      # a cough, no VAD start
    await line.feed(9, QUIET)
    await line.verdict(True, source="ceiling")
    assert line.hints() == [ms(9) - MARGIN], (
        "committed at once, claiming only the silence after the cough, not "
        f"{ms(6) + ms(20) - MARGIN}; got {line.hints()}")


# ---- mechanism 2: the boundary mid-word ------------------------------------------------

async def the_verdict_over_a_resumed_word(wired):
    """A short in-sentence pause draws a COMPLETE verdict; while Smart Turn ran, the
    caller started the next word, in audio the analyzer has not heard yet."""
    line = Line(wired)
    await line.utterance()                         # "For some reason you"
    await line.feed(2, QUIET, behind=True)
    await line.feed(3, SPEECH, behind=True)        # "con-"
    await line.verdict(True)
    return line


async def test_reproduced_a_complete_verdict_committed_inside_the_next_word():
    line = await the_verdict_over_a_resumed_word(wired=False)
    assert len(line.commits()) == 1, "without the analyzer the verdict commits at once"
    await line.catch_up()
    await line.feed(10, SPEECH)                    # "-nected to my"
    await line.feed(6, QUIET)
    await line.verdict(True)
    assert len(line.commits()) == 2, "the word straddles two segments"


async def test_the_verdict_waits_for_the_backlog_and_the_resumed_word_holds_it():
    line = await the_verdict_over_a_resumed_word(wired=True)
    assert line.commits() == [], "the analyzer has not heard the inference's backlog yet"
    await line.catch_up()
    await settle()
    assert line.commits() == [], "a run of voice in the backlog: the boundary waits"
    await line.feed(10, SPEECH)                    # "-nected to my Bluetooth speaker"
    assert line.speaking
    await line.feed(6, QUIET)
    await line.verdict(True)
    assert len(line.commits()) == 1, f"one segment, one commit; got {line.commits()}"
    assert line.stt._commits[0].n == 2


async def test_a_cough_shorter_than_an_onset_costs_nothing_past_its_end():
    with Journal() as j:
        line = Line()
        await line.utterance()
        await line.feed(3, QUIET, behind=True)
        await line.feed(2, SPEECH, behind=True)    # a cough under the inference
        await line.verdict(True)
        await line.catch_up()
        await settle()
        assert line.commits() == [], "a run still going could be a word"
        await line.feed(1, QUIET)
        await settle()
        assert len(line.commits()) == 1, "the run broke short of an onset: commit"
        await line.stt._handle_message({"type": "transcription.done", "text": "hi",
                                        "reason": "commit"})
        assert "for the caller to pause" in j.having("commit=verdict")[0], j.lines


async def test_voice_the_vad_never_starts_on_costs_one_stop_window_of_quiet():
    with Journal() as j:
        line = Line()
        await line.utterance()
        await line.feed(1, QUIET, behind=True)
        await line.feed(5, VOICE, behind=True)     # an onset by the onset test only
        await line.verdict(True)
        await line.catch_up()
        await line.feed(PAUSE - 1, QUIET)
        await settle()
        assert line.commits() == [], "short of the onset's own offset"
        await line.feed(1, QUIET)
        await settle()
        assert len(line.commits()) == 1, "the onset's offset point: commit"
        assert line.hints() == [ms(PAUSE) - MARGIN], (
            f"the hint counts from the voice's end; got {line.hints()}")
        await line.stt._handle_message({"type": "transcription.done", "text": "hi",
                                        "reason": "commit"})
        assert "for the caller to pause" in j.having("commit=verdict")[0], j.lines


async def test_voice_inside_the_stops_window_is_not_voice_since_the_stop():
    """The utterance's tail at 0.65 -- voice to the onset test, a run long enough to be
    an onset, but quiet to the VAD -- falls inside the stop's own window. It is the
    utterance's, not voice since the stop: the verdict commits at once. Placing the stop
    where the VAD began STOPPING, or dropping the filter on it, holds this commit."""
    line = Line()
    await line.feed(20, SPEECH)
    await line.feed(4, VOICE)
    await line.feed(2, QUIET)
    assert not line.speaking, "the VAD stopped: six chunks under its 0.7"
    await line.verdict(True)
    assert len(line.commits()) == 1, "committed in the verdict's own call"


# ---- with no voice since the stop -----------------------------------------------------

async def test_a_lagging_analyzer_costs_only_its_backlog():
    with Journal() as j:
        line = Line()
        await line.utterance()
        await line.feed(10, QUIET, behind=True)    # the inference's backlog, all quiet
        await line.verdict(True)
        assert line.commits() == [], "the analyzer has not heard the backlog yet"
        t0 = time.monotonic()
        await line.catch_up()
        await settle()
        took = time.monotonic() - t0
        assert len(line.commits()) == 1, "the catch-up wakes the commit"
        assert took < 0.01, f"woken by the analyzer, not a poll: {took * 1000:.1f} ms"
        assert line.hints() == [ms(6) + ms(10) - MARGIN], (
            f"the stop's speech end as the analyzer placed it, plus the backlog; "
            f"got {line.hints()}")
        await line.stt._handle_message({"type": "transcription.done", "text": "hi",
                                        "reason": "commit"})
        assert "for the VAD" in j.having("commit=verdict")[0], j.lines


async def test_a_vad_start_during_the_catch_up_keeps_the_segment_open():
    line = Line()
    await line.utterance()
    await line.feed(10, SPEECH, behind=True)
    await line.verdict(True)
    await line.catch_up()
    assert line.speaking, "the backlog held a VAD start"
    await asyncio.sleep(CEILING + 0.05)
    assert line.commits() == [], "the caller resumed: the next stop asks again"


# ---- the bounds -----------------------------------------------------------------------

async def steady_noise_turn(line):
    """A turn over steady voice at speech confidence the VAD never starts on."""
    await line.utterance()
    await line.feed(2, VOICE)
    before = len(line.commits())
    await line.verdict(True)
    waited = 0.0
    while len(line.commits()) == before and waited < CEILING + 0.2:
        await line.feed(1, VOICE)
        await asyncio.sleep(0.02)
        waited += 0.02
    return waited


async def test_voice_that_never_pauses_commits_at_the_ceiling_and_three_stand_it_down():
    with Journal("WARNING") as j:
        line = Line()
        waited = await steady_noise_turn(line)
        assert len(line.commits()) == 1, "the wait must end at the ceiling"
        assert CEILING - 0.03 <= waited <= CEILING + 0.08, f"committed after {waited:.2f}s"
        # Voice is current, and the commit was held for it: its segment keeps that
        # voice, so there is no silence to claim and nothing to carry, whatever the
        # carry's bound (CARRY THE TAIL, test_commit_carries_the_tail.py).
        assert line.hints() == [None], "voice is current: no silence to claim"
        assert line.stt._commits[0].for_voice and line.stt._commits[0].carry_ms == 0, (
            "a commit held for the caller's voice carries none of it")
        assert j.having("has not paused"), j.lines
        await steady_noise_turn(line)
        await steady_noise_turn(line)
        assert len(j.having("commits stop waiting")) == 1, j.lines
        assert line.stt._pause_off
        await line.utterance()
        await line.feed(2, VOICE)
        await line.verdict(True)
        assert len(line.commits()) == 4, "stood down: the fourth commits at the verdict"


async def clean_turn(line):
    """A turn the verdict commits at once: no voice since the stop."""
    before = len(line.commits())
    await line.utterance()
    await line.verdict(True)
    assert len(line.commits()) == before + 1


async def test_a_start_during_a_wait_breaks_the_run_of_capped_commits():
    line = Line()
    await steady_noise_turn(line)
    await steady_noise_turn(line)
    await line.utterance()
    await line.feed(2, SPEECH)
    await line.verdict(True)
    await line.feed(10, SPEECH)                    # the caller resumes: VAD start
    assert line.speaking and line.stt._pause_capouts == 0, "the start broke the run"
    await line.feed(6, QUIET)
    await line.verdict(True)
    await steady_noise_turn(line)
    assert not line.stt._pause_off


async def test_a_wait_ended_by_a_pause_breaks_the_run_of_capped_commits():
    line = Line()
    await steady_noise_turn(line)
    await steady_noise_turn(line)
    await line.utterance()
    await line.feed(5, VOICE)                      # an onset the VAD never starts on
    await line.verdict(True)
    await line.feed(PAUSE, QUIET)                  # ... that pauses
    await settle()
    assert len(line.commits()) == 3 and line.stt._pause_capouts == 0, \
        "the pause's commit broke the run"
    await steady_noise_turn(line)
    await steady_noise_turn(line)
    assert not line.stt._pause_off, "two capped commits since: not a run of three"


async def test_clean_turns_break_the_run_of_capped_commits():
    """Three capped commits spread over a long session, clean turns between them, are
    not steady noise: the wait stays on."""
    line = Line()
    for _ in range(3):
        await steady_noise_turn(line)
        await clean_turn(line)
        await clean_turn(line)
    assert not line.stt._pause_off and line.stt._pause_capouts == 0


async def test_a_forced_stop_does_not_take_the_last_utterances_speech_end():
    """The stop pipecat forces when the client stops sending audio mid-speech is one
    the analyzer never trips: its speech end is the previous utterance's. Placed from
    it, the hint would claim this whole utterance as silence."""
    line = Line()
    await clean_turn(line)
    sent_before = line.stt._sent_samples
    await line.feed(20, SPEECH)
    assert line.speaking
    line.speaking = False                          # the forced stop, no trip
    await line.stt.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS), UP)
    assert line.stt._speech_end > sent_before, (
        "the speech end must not be placed before this utterance began: "
        f"{line.stt._speech_end} <= {sent_before}")


async def test_a_commit_that_answers_no_verdict_neither_counts_nor_breaks_the_run():
    """With a TV in the room every verdict's commit caps. A stop over the bot's reply
    between them commits at once, answering no verdict: it says nothing about the
    noise, and the third capped verdict commit still stands the wait down."""
    line = Line()
    await steady_noise_turn(line)
    await steady_noise_turn(line)
    await line.stt.process_frame(BotStartedSpeakingFrame(), UP)
    before = len(line.commits())
    await line.utterance()                         # the flush over the bot's reply
    assert len(line.commits()) == before + 1
    await line.stt.process_frame(BotStoppedSpeakingFrame(), UP)
    assert line.stt._pause_capouts == 2, "the bot-speaking commit left the run alone"
    await steady_noise_turn(line)
    assert line.stt._pause_off, "three capped verdict commits running"


async def test_one_failed_analysis_leaves_no_shortfall():
    """A transient model error in the VAD, which the aggregator swallows: the frame was
    still handed over, and the chunk is still behind the analyzer. The counts stay in
    line, and the next turn's verdict commits at once rather than waiting out the
    catch-up bound on every turn for the rest of the session."""
    line = Line()
    await line.feed(5, QUIET)
    line.vad.fail_next = True
    await line.feed(1, QUIET)                      # the model raises on this chunk
    marks = line.vad.voice_marks()
    assert marks.received == line.stt._passed_samples, marks
    assert marks.analyzed == marks.received, f"positions drifted: {marks}"
    await line.utterance()
    await line.verdict(True)
    assert len(line.commits()) == 1, "committed in the verdict's own call"


async def test_a_slow_analyzer_is_not_switched_off_and_is_logged_once():
    """A busy box misses the catch-up bound on commit after commit, and the analyzer
    gets there each time a little later. That is slowness: commits are drawn on what
    has been heard, the marks stay on, and one line says so."""
    with Journal("WARNING") as j:
        line = Line()
        for turn in range(3):
            await line.utterance()
            await line.feed(5, QUIET, behind=True)
            await line.verdict(True)
            await asyncio.sleep(CATCHUP + 0.03)
            assert len(line.commits()) == turn + 1, "drawn on what has been heard"
            await line.catch_up()                  # ... and the analyzer gets there
        await clean_turn(line)
        assert not line.stt._marks_unaligned, j.lines
        assert line.stt._behind is None
        assert len(j.having("is still")) == 1, j.lines


async def test_an_analyzer_that_never_gets_there_is_switched_off_at_the_next_verdict():
    """An analyzer that stops hearing the frames this service passes it: the audio a
    catch-up fell short of is still unheard at the next verdict."""
    with Journal("WARNING") as j:
        line = Line()
        await line.utterance()
        await line.feed(5, QUIET, behind=True)
        await line.verdict(True)
        await asyncio.sleep(CATCHUP + 0.03)
        assert len(line.commits()) == 1
        assert not line.stt._marks_unaligned, "one miss is slowness until shown otherwise"
        line.backlog = []                          # it never hears them; nor anything after
        await line.stt.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)
        await line.feed(20, SPEECH, behind=True)
        await line.stt.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS), UP)
        await line.verdict(True)
        assert len(line.commits()) == 2, "with the marks off the verdict commits at once"
        assert line.stt._marks_unaligned
        assert len(j.having("counts disagree")) == 1, j.lines


async def test_marks_that_cannot_be_read_turn_off_and_the_wait_still_commits():
    """A broken analyzer must not hold a segment: the wait, the hint and the speech end
    all read the marks through one place, which turns them off and falls back."""
    with Journal("WARNING") as j:
        line = Line()
        await line.utterance()
        await line.feed(5, VOICE)
        await line.verdict(True)
        assert line.commits() == [], "waiting on the voice"

        def broken():
            raise RuntimeError("marks gone")

        line.vad.voice_marks = broken
        await line.feed(1, VOICE)                  # wakes the wait
        await settle()
        assert len(line.commits()) == 1 and not line.stt._commit_pending
        assert line.hints() == [ms(6) + ms(6) - MARGIN], (
            "with no marks the hint counts from the stop's speech end, as it did "
            f"before #140; got {line.hints()}")
        assert len(j.having("reading the VAD analyzer's marks failed")) == 1, j.lines

    line = Line()
    line.vad.voice_marks = broken                  # broken from the start
    await line.utterance()                         # the stop places its speech end
    await line.verdict(True)
    assert line.hints() == [200 - MARGIN], "the stop window, as without the marks"

async def test_counts_that_disagree_turn_the_marks_off_for_the_session():
    with Journal("WARNING") as j:
        line = Line()
        await line.feed(20, SPEECH)
        await line.vad.analyze_audio(FRAME)        # audio this service never passed
        await line.feed(6, QUIET)
        await line.verdict(True)
        assert len(line.commits()) == 1, "with the marks off the verdict commits at once"
        assert line.stt._marks_unaligned
        assert len(j.having("counts disagree")) == 1, j.lines


async def test_audio_passed_down_unsent_is_not_a_disagreement():
    """A dead socket: frames go down to the VAD unsent. The passed count is what the
    analyzer receives, sent or not, so the marks stay on and still hold a commit."""
    class Dead:
        async def send(self, raw):
            raise OSError("socket gone")

    with Journal("WARNING") as j:
        line = Line()
        live = line.stt._websocket
        line.stt._websocket = Dead()
        await line.feed(20, SPEECH)
        line.stt._websocket = live
        await line.feed(6, QUIET)
        await line.feed(5, SPEECH, behind=True)
        await line.verdict(True)
        await line.catch_up()
        await settle()
        assert not j.having("counts disagree"), j.lines
        assert not line.stt._marks_unaligned
        assert line.commits() == [], "the marks still see the caller's voice"


# ---- the wait itself ------------------------------------------------------------------

async def test_a_second_verdict_during_the_wait_changes_nothing():
    line = Line()
    await line.utterance()
    await line.feed(5, VOICE)
    t0 = time.monotonic()
    await line.verdict(True)
    await asyncio.sleep(CEILING * 0.6)
    await line.verdict(True, source="turn-stopped")
    assert line.commits() == [], "the second verdict must not commit"
    while not line.commits() and time.monotonic() - t0 < 3 * CEILING:
        await line.feed(1, VOICE)
        await asyncio.sleep(0.01)
    took = time.monotonic() - t0
    assert len(line.commits()) == 1
    assert took < CEILING + 0.06, f"the bound counts from the first verdict: {took:.2f}s"
    assert line.stt._commits[0].why == "verdict"


async def test_a_wait_that_fails_still_commits_the_segment():
    with Journal("WARNING") as j:
        line = Line()
        await line.utterance()
        await line.feed(5, VOICE)
        await line.verdict(True)
        assert line.commits() == []

        def broken(w, heard_through):
            raise RuntimeError("boom")

        line.stt._boundary_wait = broken
        await line.feed(1, VOICE)                  # wakes the wait
        await settle()
        assert len(line.commits()) == 1, "the held segment must still close"
        assert j.having("wait for the caller's pause failed"), j.lines


def test_the_onset_test_and_the_wait_share_one_offset():
    """The reply hold's offset event and the wait's pause are one decision: `paused`
    turns true on the chunk the offset fires on."""
    async def run():
        line = Line()
        events = []

        async def listener(started):
            events.append((started, line.vad.voice_marks().paused))

        line.vad.onset_listeners.append(listener)
        await line.feed(5, VOICE)
        await line.feed(PAUSE - 1, QUIET)
        assert events == [(True, False)] and not line.vad.voice_marks().paused
        await line.feed(1, QUIET)
        assert events == [(True, False), (False, True)], events

    asyncio.run(run())


def main():
    stt_mod._HINT_MARGIN_MS = MARGIN
    stt_mod.STT_SILENCE_HINT = True
    stt_mod._VOICE_CATCHUP_SECS = CATCHUP
    assert speech_onset.ONSET_MIN_MS == 128, "these scripts assume a 4-chunk onset"
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {fn.__name__}")

    asyncio.run(run())
    for fn in tests:
        if not asyncio.iscoroutinefunction(fn):
            fn()
            print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
