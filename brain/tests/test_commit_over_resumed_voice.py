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
# What these pin, in order:
#
#   * the ceiling's commit no longer claims voice heard during the hold: with the
#     voice still going it waits, and the VAD start then keeps the segment whole; with
#     a blip long over, it commits at once and counts the hint from the blip's end;
#   * a COMPLETE verdict waits for the analyzer to hear the inference's backlog, and
#     voice in it holds the boundary, so the resumed sentence is one segment and one
#     commit, stamped with the stop that closes it;
#   * a blip that is not followed by a VAD start costs a pause of the stop's own window,
#     no more, and the segment line says what the boundary was held for;
#   * the bounds: voice that never pauses commits at the session's ceiling, an
#     analyzer that never catches up is passed over after _VOICE_CATCHUP_SECS, each
#     with a warning, and counts that disagree turn the marks off for the session;
#   * with no voice since the stop nothing changes: a current analyzer commits at the
#     verdict itself with the same hint as before, and a lagging one only for as long
#     as its backlog takes;
#   * and the real onset mixin, driven in lockstep with the service as the aggregator
#     drives it, keeps marks on the same count the service keeps, so the positions the
#     two compare are the same audio.
#
# Run: python test_commit_over_resumed_voice.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams, VADState  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from stt_harness import WireRecorder  # noqa: E402

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.endpointing import TurnVerdictFrame  # noqa: E402
from teaport_brain.speech_onset import SpeechOnsetMixin, VoiceMarks  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

SAMPLE_RATE = 16000
CHUNK_MS = 20
FRAME = int(SAMPLE_RATE * CHUNK_MS / 1000)          # samples per 20 ms frame
CHUNK = b"\x00\x02" * FRAME
STOP_SECS = 0.2                                     # Talk's VAD stop
MARGIN = 100
CEILING = 0.15                                      # the session's ceiling, shortened
CATCHUP = 0.05                                      # _VOICE_CATCHUP_SECS, shortened

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


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


class Analyzer:
    """The session's VAD analyzer as the STT reads it: VoiceMarks on the count of audio
    it has been handed. Moved by the test as the user aggregator would move it, frame
    by frame behind the STT -- or not, for the audio queued behind Smart Turn's
    inference (`behind`), until `catch_up`."""

    def __init__(self):
        self.received = 0
        self.voice_end = None
        self.stop_at = None
        self.backlog = []

    def voice_marks(self):
        return VoiceMarks(self.received, self.received, self.voice_end, self.stop_at)

    def hear(self, voiced):
        self.received += FRAME
        if voiced:
            self.voice_end = self.received

    def trip(self):
        self.stop_at = self.received

    def catch_up(self):
        for voiced in self.backlog:
            self.hear(voiced)
        self.backlog = []


def service(with_analyzer):
    s = Recorder()
    vad = Analyzer()
    if with_analyzer:
        s.vad_analyzer = vad
    return s, vad


async def audio(s, vad, ms, voiced=False, behind=False):
    """`ms` of audio through the STT, then to the analyzer unless it is `behind`."""
    for _ in range(ms // CHUNK_MS):
        await s.process_frame(InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE,
                                                 num_channels=1), DOWN)
        if behind:
            vad.backlog.append(voiced)
        else:
            vad.hear(voiced)


async def stop(s, vad):
    vad.trip()
    await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS), UP)


async def start(s):
    await s.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)


async def verdict(s, complete, source="verdict"):
    await s.process_frame(TurnVerdictFrame(complete=complete, source=source), UP)


def commits(s):
    return s._websocket.commits()


def hints(s):
    return [m.get("trailing_silence_ms") for m in commits(s)]


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

async def the_ceiling_with_the_caller_back(with_analyzer):
    """An INCOMPLETE stop; during the hold the caller starts again (300 ms of voice the
    VAD has not yet started on) and the ceiling's verdict lands over it."""
    s, vad = service(with_analyzer)
    await start(s)
    await audio(s, vad, 600, voiced=True)
    await audio(s, vad, 200)                       # the stop's window
    await stop(s, vad)
    await verdict(s, False)
    await audio(s, vad, 300)
    await audio(s, vad, 300, voiced=True)          # "a fusion" ...
    await verdict(s, True, source="ceiling")
    return s, vad


async def test_reproduced_the_ceilings_commit_claimed_the_resumed_voice_as_silence():
    s, _ = await the_ceiling_with_the_caller_back(with_analyzer=False)
    assert hints(s) == [200 + 600 - MARGIN], (
        "without the analyzer the hint counts the whole hold, voice included: the "
        f"engine skips decoding the caller's resumed words; got {hints(s)}")


async def test_the_ceiling_waits_over_current_voice_and_the_start_keeps_the_segment():
    s, vad = await the_ceiling_with_the_caller_back(with_analyzer=True)
    await asyncio.sleep(0.03)
    assert commits(s) == [], "the ceiling's commit must not be drawn over current voice"
    await audio(s, vad, 100, voiced=True)
    await start(s)                                 # ... and the VAD admits it
    await audio(s, vad, 600, voiced=True)          # "... reaction that generates light"
    await audio(s, vad, 200)
    await stop(s, vad)
    await verdict(s, True)
    assert len(commits(s)) == 1, f"one segment for the whole sentence; got {commits(s)}"
    assert s._commits[0].n == 2, "the commit answers the stop that closed the sentence"
    assert hints(s) == [200 - MARGIN], "the hint counts from the second stop only"
    await asyncio.sleep(CEILING + 0.05)
    assert len(commits(s)) == 1, "the abandoned wait must not commit later"


async def test_a_blip_long_over_commits_at_the_ceiling_with_the_hint_from_its_end():
    s, vad = service(with_analyzer=True)
    await start(s)
    await audio(s, vad, 600, voiced=True)
    await audio(s, vad, 200)
    await stop(s, vad)
    await verdict(s, False)
    await audio(s, vad, 300)
    await audio(s, vad, 60, voiced=True)           # a cough, no VAD start
    await audio(s, vad, 300)
    await verdict(s, True, source="ceiling")
    assert hints(s) == [300 - MARGIN], (
        "the commit goes out at once (the caller has been quiet a stop's window since), "
        f"claiming only the silence after the cough, not {200 + 660 - MARGIN}; "
        f"got {hints(s)}")


# ---- mechanism 2: the boundary mid-word -------------------------------------------------

async def the_verdict_over_a_resumed_word(with_analyzer):
    """A short in-sentence pause draws a COMPLETE verdict; while Smart Turn ran, the
    caller started the next word, in audio the analyzer has not heard yet."""
    s, vad = service(with_analyzer)
    await start(s)
    await audio(s, vad, 1000, voiced=True)         # "For some reason you"
    await audio(s, vad, 200)
    await stop(s, vad)
    await audio(s, vad, 80, behind=True)
    await audio(s, vad, 80, voiced=True, behind=True)   # "con-"
    await verdict(s, True)
    return s, vad


async def test_reproduced_a_complete_verdict_committed_inside_the_next_word():
    s, vad = await the_verdict_over_a_resumed_word(with_analyzer=False)
    assert len(commits(s)) == 1, "without the analyzer the verdict commits at once"
    vad.catch_up()
    await audio(s, vad, 200, voiced=True)          # "-nected"
    await start(s)
    await audio(s, vad, 400, voiced=True)
    await audio(s, vad, 200)
    await stop(s, vad)
    await verdict(s, True)
    assert len(commits(s)) == 2, "the word straddles two segments"


async def test_the_verdict_waits_for_the_backlog_and_the_resumed_word_holds_it():
    s, vad = await the_verdict_over_a_resumed_word(with_analyzer=True)
    assert commits(s) == [], "the analyzer has not heard the inference's backlog yet"
    vad.catch_up()
    await asyncio.sleep(0.03)
    assert commits(s) == [], "voice in the backlog: the boundary must wait"
    await audio(s, vad, 200, voiced=True)
    await start(s)
    await audio(s, vad, 400, voiced=True)          # "to my Bluetooth speaker"
    await audio(s, vad, 200)
    await stop(s, vad)
    await verdict(s, True)
    assert len(commits(s)) == 1, f"one segment, one commit; got {commits(s)}"
    assert s._commits[0].n == 2


async def test_a_blip_without_a_start_costs_one_stop_window_and_is_on_the_segment_line():
    with Journal() as j:
        s, vad = service(with_analyzer=True)
        await start(s)
        await audio(s, vad, 1000, voiced=True)
        await audio(s, vad, 200)
        await stop(s, vad)
        await audio(s, vad, 100, behind=True)
        await audio(s, vad, 60, voiced=True, behind=True)   # a cough under the inference
        await verdict(s, True)
        vad.catch_up()
        await audio(s, vad, 180)
        await asyncio.sleep(0.03)
        assert commits(s) == [], "180 ms of quiet is short of the stop's 200 ms window"
        await audio(s, vad, 20)
        await asyncio.sleep(0.03)
        assert len(commits(s)) == 1, "a stop's window of quiet after the cough commits"
        assert hints(s) == [200 - MARGIN], (
            f"the hint counts from the cough's end; got {hints(s)}")
        await s._handle_message({"type": "transcription.done", "text": "hello",
                                 "reason": "commit"})
        line = j.having("commit=verdict")
        assert len(line) == 1 and "for the caller to pause" in line[0], j.lines


# ---- the bounds -------------------------------------------------------------------------

async def test_voice_that_never_pauses_commits_at_the_ceiling_claiming_nothing():
    with Journal("WARNING") as j:
        s, vad = service(with_analyzer=True)
        await start(s)
        await audio(s, vad, 600, voiced=True)
        await audio(s, vad, 200)
        await stop(s, vad)
        await audio(s, vad, 40, voiced=True)       # steady noise at speech confidence
        await verdict(s, True)
        waited = 0.0
        while not commits(s) and waited < CEILING + 0.2:
            await audio(s, vad, 20, voiced=True)
            await asyncio.sleep(0.02)
            waited += 0.02
        assert len(commits(s)) == 1, "the wait must end at the ceiling"
        assert CEILING - 0.03 <= waited <= CEILING + 0.08, f"committed after {waited:.2f}s"
        assert hints(s) == [None], "voice is current: no silence to claim"
        assert j.having("has not paused"), j.lines


async def test_an_analyzer_that_never_catches_up_is_passed_over_with_a_warning():
    with Journal("WARNING") as j:
        s, vad = service(with_analyzer=True)
        await start(s)
        await audio(s, vad, 600, voiced=True)
        await audio(s, vad, 200)
        await stop(s, vad)
        await audio(s, vad, 100, behind=True)      # and it never hears it
        await verdict(s, True)
        await asyncio.sleep(CATCHUP + 0.04)
        assert len(commits(s)) == 1, "the boundary is drawn on what has been heard"
        assert j.having("short of the audio sent at the verdict"), j.lines


async def test_counts_that_disagree_turn_the_marks_off_for_the_session():
    with Journal("WARNING") as j:
        s, vad = service(with_analyzer=True)
        await start(s)
        await audio(s, vad, 600, voiced=True)
        vad.received += 10 * FRAME                 # heard audio this service never passed
        await audio(s, vad, 200)
        await stop(s, vad)
        await verdict(s, True)
        assert len(commits(s)) == 1, "with the marks off the verdict commits at once"
        assert s._marks_unaligned
        assert len(j.having("counts disagree")) == 1, j.lines


# ---- with no voice since the stop, nothing changes --------------------------------------

async def test_a_current_analyzer_commits_at_the_verdict_itself_with_the_same_hint():
    s, vad = service(with_analyzer=True)
    await start(s)
    await audio(s, vad, 600, voiced=True)
    await audio(s, vad, 200)
    await stop(s, vad)
    await audio(s, vad, 100)
    await verdict(s, True)
    assert hints(s) == [200 + 100 - MARGIN], "committed in the verdict's own call"


async def test_a_lagging_analyzer_costs_only_its_backlog():
    with Journal() as j:
        s, vad = service(with_analyzer=True)
        await start(s)
        await audio(s, vad, 600, voiced=True)
        await audio(s, vad, 200)
        await stop(s, vad)
        await audio(s, vad, 160, behind=True)
        await verdict(s, True)
        assert commits(s) == []
        vad.catch_up()
        await asyncio.sleep(0.03)
        assert hints(s) == [200 + 160 - MARGIN], hints(s)
        await s._handle_message({"type": "transcription.done", "text": "hi",
                                 "reason": "commit"})
        line = j.having("commit=verdict")
        assert len(line) == 1 and "for the VAD" in line[0], j.lines


async def test_a_vad_start_during_the_catch_up_keeps_the_segment_open():
    s, vad = service(with_analyzer=True)
    await start(s)
    await audio(s, vad, 600, voiced=True)
    await audio(s, vad, 200)
    await stop(s, vad)
    await audio(s, vad, 160, voiced=True, behind=True)
    await verdict(s, True)
    await start(s)
    vad.catch_up()
    await asyncio.sleep(CEILING + 0.05)
    assert commits(s) == [], "the caller resumed: the next stop asks again"


# ---- the real mixin, in lockstep with the service ----------------------------------------

class Scripted(VADAnalyzer):
    """A VAD analyzer whose Silero confidences are scripted per 512-sample chunk, with
    the volume gate open, so the VAD's own state follows the script."""

    def __init__(self, confs):
        super().__init__(sample_rate=SAMPLE_RATE,
                         params=VADParams(stop_secs=STOP_SECS, start_secs=0.064,
                                          min_volume=0.0))
        self.confs = list(confs)

    def num_frames_required(self):
        return 512

    def voice_confidence(self, buffer):
        return self.confs.pop(0) if self.confs else 0.0


async def test_the_onset_mixins_marks_are_on_the_services_count():
    """Speech, a stop, then the caller back at 0.65 -- under the VAD's 0.7, so the VAD
    never starts on it -- in audio queued behind the inference. The service, reading the
    real mixin, holds the verdict's commit until the voice has paused, and claims only
    the silence after it: positions the two counted separately, frame by frame."""
    # Chunks of 32 ms. The stop trips on the 7th quiet chunk, so the voice starts in
    # the audio that arrives while Smart Turn runs.
    speech, quiet, back = 20, 7, 8
    confs = [0.9] * speech + [0.1] * quiet + [0.65] * back + [0.1] * 30
    vad = type("Onset", (SpeechOnsetMixin, Scripted), {})(confs)
    vad.set_sample_rate(SAMPLE_RATE)
    s = Recorder()
    s.vad_analyzer = vad
    s.smartturn_stop_secs = 1.0                    # this test paces in wall time
    tripped = [False]

    async def frame(analyze=True):
        f = InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1)
        await s.process_frame(f, DOWN)
        if analyze:
            await analyze_one(f.audio)
        return f.audio

    async def analyze_one(buf):
        state = await vad.analyze_audio(buf)
        if state == VADState.SPEAKING and not tripped[0]:
            await start(s)
            tripped[0] = None
        elif state == VADState.QUIET and tripped[0] is None:
            tripped[0] = True
            await s.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS), UP)

    while tripped[0] is not True:
        await frame()
    marks = vad.voice_marks()
    assert marks.stop_at is not None and marks.received == s._passed_samples, marks
    # Smart Turn runs: the next frames reach the service and wait for the analyzer.
    backlog = [await frame(analyze=False) for _ in range(10)]
    await verdict(s, True)
    for buf in backlog:
        await analyze_one(buf)
    await asyncio.sleep(0.03)
    marks = vad.voice_marks()
    assert marks.voice_end is not None and marks.voice_end > marks.stop_at, marks
    assert commits(s) == [], "the caller's voice in the backlog holds the boundary"
    while not commits(s):
        await frame()
        await asyncio.sleep(0.012)
    marks = vad.voice_marks()
    assert marks.received == s._passed_samples
    assert marks.analyzed - marks.voice_end >= round(STOP_SECS * SAMPLE_RATE), marks
    quiet_ms = (s._sent_samples - marks.voice_end) * 1000 // SAMPLE_RATE
    assert hints(s) == [quiet_ms - MARGIN], (hints(s), quiet_ms)
    assert vad._vad_state == VADState.QUIET, "the VAD itself never started on 0.65"


def main():
    stt_mod._HINT_MARGIN_MS = MARGIN
    stt_mod.STT_SILENCE_HINT = True
    stt_mod._VOICE_CATCHUP_SECS = CATCHUP
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
