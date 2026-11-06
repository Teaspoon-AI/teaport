#
# Barge-in pause (barge_pause.py, teaport#34): the bot stops talking the moment the
# caller does, and picks up from the same sample if no words follow.
#
# Without it, the bot goes on talking from the caller's onset until the end of what
# they say -- typically most of a second -- because barge-in waits for a transcript and
# the STT returns none for speech over the bot until the caller stops.
#
# The playout tests drive the REAL SipGatewayOutputTransport over a socketpair (the
# gateway end drained as it arrives, as test_sip_output_framing does): what the
# gateway receives is what the caller hears, so "paused at once", "nothing lost or
# repeated" and "cancelled" are read off the wire, byte for byte. The TranscriptLedger
# rides along as the pipeline's observer, so the heard accounting through a pause is
# checked against the same run.
#
# All utterances are synthetic.
#
# Run: python test_barge_pause.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import numpy as np  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from teaport_brain.barge_pause import (  # noqa: E402
    BargeInPauser,
    PauseAwareMinWordsStrategy,
    PlayoutPauseFrame,
)
from teaport_brain.sip_serializer import BYTES_PER_FRAME, PIPELINE_SAMPLE_RATE  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402
from teaport_brain.tts_text import CAPTION_LEAD_SECS  # noqa: E402

from test_sip_output_framing import _Wire  # noqa: E402

SR = PIPELINE_SAMPLE_RATE          # 16 kHz in and out: no resampling, so bytes compare
CHUNK_S = 0.2
N_CHUNKS = 10                      # a 2 s reply
WORDS = "one two three four five six seven eight nine ten".split()   # one per 0.2 s
RESUME_S = 0.3
STARTUP_S = 0.5


def _pcm(i):
    """Chunk i of the reply: a ramp no other chunk repeats, so a lost or repeated
    frame shows in the byte comparison."""
    n = int(SR * CHUNK_S)
    return (np.arange(i * n, (i + 1) * n) % 30000 + 1).astype(np.int16).tobytes()


SPEECH = b"".join(_pcm(i) for i in range(N_CHUNKS))


class _TimedWire(_Wire):
    """The gateway end, with when each datagram arrived."""

    def __init__(self):
        super().__init__()
        self.times = []

    async def _drain_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.sock_recv(self.gw_sock, 4096)
            if not data:
                return
            self.datagrams.append(data)
            self.times.append(time.monotonic())

    def payload(self):
        return b"".join(d[1:] for d in self.datagrams)

    def gaps(self, at_least=0.1):
        return [(self.times[i - 1], self.times[i]) for i in range(1, len(self.times))
                if self.times[i] - self.times[i - 1] >= at_least]


class Tap(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append((time.monotonic(), direction, frame))
        await self.push_frame(frame, direction)


class Rig:
    """top -> [pauser] -> real SIP output transport -> bottom, ledger observing."""

    def __init__(self, with_pauser=True):
        self.wire = _TimedWire()
        self.out = self.wire.build_output(end_silence_secs=0)
        self.pauser = BargeInPauser(self.out, resume_secs=RESUME_S, max_secs=3.0) \
            if with_pauser else None
        self.top, self.bottom = Tap(), Tap()
        self.ledger = TranscriptLedger(output=self.out)
        self.task = PipelineTask(
            Pipeline([p for p in [self.top, self.pauser, self.out, self.bottom] if p]),
            observers=[self.ledger], cancel_on_idle_timeout=False)

    async def __aenter__(self):
        self.wire.start()
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.sleep(STARTUP_S)
        return self

    async def __aexit__(self, *exc):
        await self.task.cancel()
        await self._run
        await self.wire.stop()

    async def speak(self):
        """One reply as the engine TTS hands it on: the LLM stream, a context, the audio,
        and word frames stamped with their playout pts -- measured from when the audio
        really started on the wire, and early by the caption lead, as _place_words
        stamps them. Returns once the first frame is out; the schedule (_at) counts
        from there."""
        ctx = "r1"
        frames = [LLMFullResponseStartFrame(), LLMTextFrame(" ".join(WORDS)),
                  LLMFullResponseEndFrame(), TTSStartedFrame(context_id=ctx)]
        frames += [TTSAudioRawFrame(_pcm(i), SR, 1, context_id=ctx) for i in range(N_CHUNKS)]
        frames.append(TTSStoppedFrame(context_id=ctx))
        await self.task.queue_frames(frames)
        while not self.wire.times:
            await asyncio.sleep(0.002)
        self.t0 = self.wire.times[0]
        base = self.out.get_clock().get_time() - int((time.monotonic() - self.t0) * 1e9)
        lead = int(CAPTION_LEAD_SECS * 1e9)
        words = []
        for i, w in enumerate(WORDS):
            f = TTSTextFrame(w, aggregated_by="word", context_id=ctx)
            f.pts = base + int(i * CHUNK_S * 1e9) - lead
            words.append(f)
        await self.task.queue_frames(words)

    def pause_frames(self, direction):
        return [f.paused for _, d, f in self.bottom.frames + self.top.frames
                if isinstance(f, PlayoutPauseFrame) and d == direction]

    def assistant(self):
        return [u for u in self.ledger.events if u.speaker == "assistant"]


async def _at(rig, secs):
    await asyncio.sleep(max(0.0, rig.t0 + secs - time.monotonic()))


# --- The voice clock: a paused reply's words wait out the pause (playout_pause.py) ---

def _words_out(rig):
    """(arrival, word) of each TTSTextFrame the transport released downstream."""
    return [(t, f.text) for t, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, TTSTextFrame)]


def _pauses_out(rig):
    """(arrival, frame) of each PlayoutPauseFrame the transport said downstream."""
    return [(t, f) for t, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, PlayoutPauseFrame)]


async def check_the_words_keep_the_voices_time_through_a_pause(rig):
    # The words are stamped on the voice clock (here: before the pause, where it is the
    # pipeline clock). Paused, nothing plays: no word is released; resumed, each word
    # of the reply comes when its audio plays -- the pause's length later than its pts
    # on the pipeline clock, less the lag that played on -- not all at once, nor early.
    await rig.speak()
    await _at(rig, 0.6)
    await rig.pauser.on_onset(True)
    await _at(rig, 1.0)
    await rig.pauser.on_onset(False)
    await _at(rig, 3.4)
    (t_pause, pause), (t_resume, _) = _pauses_out(rig)
    stop = t_pause + pause.lag_secs              # what was sent before the pause played on
    held = t_resume - stop
    stood = (rig.out.get_clock().get_time() - rig.out.voice_clock.now_ns()) / 1e9
    assert abs(stood - held) <= 0.01, (
        f"the voice clock stood {stood:.3f}s; the voice stood {held:.3f}s, from the end "
        f"of the pause's {pause.lag_secs:.3f}s lag to the resume")
    words = _words_out(rig)
    assert [w for _, w in words] == WORDS, words
    assert not [w for t, w in words if stop + 0.03 < t < t_resume - 0.01], (
        f"words released while the voice stood still: {words}")
    for i, (t, w) in enumerate(words):
        due = rig.t0 + i * CHUNK_S - CAPTION_LEAD_SECS   # its pts: the lead ahead of its audio
        if due + CAPTION_LEAD_SECS < stop - 0.03:
            # Played before the pause. (The rig queues the words once the audio is out,
            # so the first ones go at once, their pts already past.)
            assert t <= due + CAPTION_LEAD_SECS + 0.06, (
                f"{w!r} came {t - due:.2f}s after its pts, after its audio")
        elif due < stop - 0.03:
            # Its audio after the stop, its pts before it (the seam, review 1): held to
            # the resume, nobody having heard it before then -- unless it was out before
            # the pause began (the echo guard dates it by its audio: test_echo_guard).
            assert t < t_pause + 0.005 or t_resume - 0.01 <= t <= t_resume + 0.06, (
                f"{w!r}, heard only after the resume, came {t - t_resume:+.2f}s from it")
        elif due > stop + 0.03:
            assert abs(t - (due + held)) <= 0.06, (
                f"{w!r} came {t - due:.2f}s after its pts; the voice stood {held:.2f}s")


async def check_a_cut_drops_the_words_a_pause_held(rig):
    await rig.speak()
    await _at(rig, 0.6)
    await rig.pauser.on_onset(True)
    await _at(rig, 1.1)
    await rig.task.queue_frame(InterruptionFrame())
    await _at(rig, 2.6)
    (t_pause, pause), = _pauses_out(rig)
    late = [w for t, w in _words_out(rig) if t > t_pause + pause.lag_secs + 0.06]
    assert not late, f"words of the cut reply released after the pause: {late}"
    # The cut ended the pause: the voice clock runs again, and the next reply's words
    # come on time (review 4: left standing, every later word waited for good).
    word = TTSTextFrame("next", aggregated_by="word", context_id="r2")
    word.pts = rig.out.voice_clock.now_ns() + int(0.3e9)
    t_queued = time.monotonic()
    await rig.task.queue_frames([TTSStartedFrame(context_id="r2"),
                                 TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r2"),
                                 word, TTSStoppedFrame(context_id="r2")])
    await asyncio.sleep(0.6)
    t_next = [t for t, w in _words_out(rig) if w == "next"]
    assert t_next and abs(t_next[0] - t_queued - 0.3) <= 0.06, (t_next, t_queued)


async def check_a_reply_stamped_after_the_resume_is_on_time(rig):
    # A reply placed after the resume is stamped on the voice clock as it reads then (the
    # TTS's baseline): its words are released on time, not moved again by the pause.
    await rig.speak()
    await _at(rig, 0.6)
    await rig.pauser.on_onset(True)
    await _at(rig, 1.0)
    await rig.pauser.on_onset(False)
    await _at(rig, 3.4)
    assert rig.out.voice_clock.now_ns() < rig.out.get_clock().get_time() - int(0.3e9), (
        "the voice clock stood still through the pause")
    word = TTSTextFrame("next", aggregated_by="word", context_id="r2")
    word.pts = rig.out.voice_clock.now_ns() + int(0.3e9)
    t_queued = time.monotonic()
    await rig.task.queue_frames([TTSStartedFrame(context_id="r2"),
                                 TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r2"),
                                 word, TTSStoppedFrame(context_id="r2")])
    await asyncio.sleep(0.6)
    t_next = [t for t, w in _words_out(rig) if w == "next"]
    assert t_next and abs(t_next[0] - t_queued - 0.3) <= 0.06, (t_next, t_queued)


async def check_a_word_due_in_the_lag_waits_for_the_resume(rig):
    # A word whose pts comes after the pause but inside its lag, while what was sent
    # still plays: its audio, the caption lead later, is past the stop, so it waits for
    # the resume -- released at its pts, it would be read as played while the voice
    # stood still. (No other word queued: the clock task would hold it behind one.)
    await rig.task.queue_frames(
        [TTSStartedFrame(context_id="r1")]
        + [TTSAudioRawFrame(_pcm(i), SR, 1, context_id="r1") for i in range(N_CHUNKS)]
        + [TTSStoppedFrame(context_id="r1")])
    while not rig.wire.times:
        await asyncio.sleep(0.002)
    rig.t0 = rig.wire.times[0]
    await _at(rig, 0.4)
    await rig.out.set_playout_paused(True)
    # The lag as frozen at the pause, not a later read: a loop stall after it would
    # leave none, and the word would test nothing (review round 3).
    assert rig.out._pause_lag > 0.005, f"no lag to test in: {rig.out._pause_lag:.3f}s"
    voice = rig.out.voice_clock
    seam = TTSTextFrame("seam", aggregated_by="word", context_id="r1")
    seam.pts = voice._stop_at - voice._stood - int(0.001e9)   # just before the stop
    await rig.task.queue_frames([seam])
    await _at(rig, 1.0)
    assert not [w for _, w in _words_out(rig) if w == "seam"], (
        "released before its audio, which plays only after the resume")
    t_resume = time.monotonic()
    await rig.out.set_playout_paused(False)
    await asyncio.sleep(0.2)
    t = [t for t, w in _words_out(rig) if w == "seam"]
    assert t and t[0] - t_resume <= 0.06, (t, t_resume)


async def test_a_word_due_in_the_lag_waits_for_the_resume():
    async with Rig(with_pauser=False) as rig:
        await check_a_word_due_in_the_lag_waits_for_the_resume(rig)


async def test_the_words_keep_the_voices_time_through_a_pause():
    async with Rig() as rig:
        await check_the_words_keep_the_voices_time_through_a_pause(rig)


async def test_a_cut_drops_the_words_a_pause_held():
    async with Rig() as rig:
        await check_a_cut_drops_the_words_a_pause_held(rig)


async def test_a_reply_stamped_after_the_resume_is_on_time():
    async with Rig() as rig:
        await check_a_reply_stamped_after_the_resume_is_on_time(rig)


async def test_the_thinking_sound_is_no_part_of_the_reply_s_end():
    # #255 comment: PausablePlayout's remaining playout counted every audio frame it was
    # handed, the thinking sound's typing too: the pauser's near-end rule and a pause's
    # tail then read the reply as longer than it is.
    async with Rig() as rig:
        await rig.task.queue_frames([OutputAudioRawFrame(_pcm(i), SR, 1) for i in range(5)])
        await asyncio.sleep(0.05)
        await rig.task.queue_frames([TTSStartedFrame(context_id="r1"),
                                     TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r1"),
                                     TTSStoppedFrame(context_id="r1")])
        await asyncio.sleep(0.05)
        tail = rig.out.playout_tail_secs()
        assert tail is not None and tail <= CHUNK_S + 0.05, (
            f"{tail}: the reply is one {CHUNK_S}s chunk; the typing ahead of it is not it")


async def test_speech_then_no_words_pauses_at_once_and_resumes_without_loss():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        t_onset = time.monotonic()
        await rig.pauser.on_onset(True)                 # the caller coughs over the bot
        await _at(rig, 1.0)
        assert rig.pauser.paused
        await rig.pauser.on_onset(False)                # ... and no words follow
        t_quiet = time.monotonic()
        await _at(rig, 3.4)
        gaps = rig.wire.gaps()
        assert len(gaps) == 1, f"expected one pause in the playout, got {gaps}"
        stopped, resumed = gaps[0]
        assert stopped - t_onset < 0.045, (
            f"the last frame before the pause left {1000*(stopped-t_onset):.0f} ms after "
            "the onset; the pause must take effect at the next 20 ms frame")
        assert RESUME_S - 0.05 <= resumed - t_quiet <= RESUME_S + 0.1, (
            f"resumed {resumed - t_quiet:.2f}s after the caller went quiet")
        assert rig.wire.payload() == SPEECH, (
            f"{len(rig.wire.payload())} of {len(SPEECH)} bytes reached the gateway, or not "
            "in order: a resumed reply must lose and repeat nothing")
        assert rig.pause_frames(FrameDirection.DOWNSTREAM) == [True, False]
        lags = [f.lag_secs for _, d, f in rig.bottom.frames
                if isinstance(f, PlayoutPauseFrame) and f.paused]
        # What was sent and is still to play: the frame playing and the one queued
        # behind it (the send clock runs one frame ahead).
        assert lags and 0.015 <= lags[0] <= 0.041, f"pause lag {lags}: one to two 20 ms frames"
        tails = [f.tail_secs for _, d, f in rig.bottom.frames
                 if isinstance(f, PlayoutPauseFrame) and f.paused]
        assert 1.2 <= tails[0] <= 1.5, f"tail at the pause {tails}: ~1.4 s of 2.0 s left"
        assert rig.pause_frames(FrameDirection.UPSTREAM) == [True, False]
        said = rig.assistant()
        assert said and not said[0].interrupted and said[0].heard_fraction == 1.0, said


async def test_speech_then_words_pauses_at_once_then_the_barge_in_cancels():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        t_onset = time.monotonic()
        await rig.pauser.on_onset(True)
        await _at(rig, 1.1)                              # the words arrive: a turn starts
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.6)
        played = len(rig.wire.payload()) / 2 / SR
        last = rig.wire.times[-1]
        assert last - t_onset < 0.045, "audio kept going after the pause"
        assert 0.5 <= played <= 0.68, f"{played:.2f}s of the reply reached the caller"
        assert rig.pauser.resumes == 0
        cut = rig.assistant()
        assert cut and cut[0].interrupted, cut
        # Heard: what played before the pause (~0.6 s of 2 s), not up to the cut (1.1 s).
        assert 0.22 <= cut[0].heard_fraction <= 0.38, cut[0].heard_fraction
        assert cut[0].heard_text.split() == WORDS[:len(cut[0].heard_text.split())]
        assert 2 <= len(cut[0].heard_text.split()) <= 4, cut[0].heard_text
        # The pause went with the cancelled reply: the next one plays at once.
        n_before = len(rig.wire.datagrams)
        t_next = time.monotonic()
        await rig.task.queue_frames([TTSStartedFrame(context_id="r2"),
                                     TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r2"),
                                     TTSStoppedFrame(context_id="r2")])
        await asyncio.sleep(0.4)
        assert len(rig.wire.datagrams) - n_before == int(SR * CHUNK_S * 2 / BYTES_PER_FRAME), (
            "the next reply did not play: the cancelled pause is still blocking the writes")
        assert rig.wire.times[n_before] - t_next < 0.1


async def test_without_the_pauser_the_bot_talks_until_the_words_arrive():
    # The brain as it was: the same script plays on to the interruption.
    async with Rig(with_pauser=False) as rig:
        await rig.speak()
        await _at(rig, 1.1)
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.0)
        played = len(rig.wire.payload()) / 2 / SR
        assert played >= 1.0, f"only {played:.2f}s played; the comparison above is moot"


async def test_a_cut_after_a_resume_charts_what_really_played():
    # Paused 0.6 -> resumed ~1.3, cut at 1.7: 0.6 + 0.4 = ~1.0 s of the 2 s played. A
    # ledger that ignored the pause would chart ~1.5 s and three words too many.
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.0)
        await rig.pauser.on_onset(False)
        await _at(rig, 1.7)
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.4)
        played = len(rig.wire.payload()) / 2 / SR
        cut = rig.assistant()
        assert cut and cut[0].interrupted
        assert abs(cut[0].heard_fraction - played / 2.0) <= 0.08, (
            f"charted heard~{cut[0].heard_fraction:.2f}, the wire says {played / 2.0:.2f}")
        # One word per 0.2 s: the words charted heard are the ones whose audio played.
        n = len(cut[0].heard_text.split())
        assert abs(n - played / CHUNK_S) <= 1.01, (
            f"{n} words charted heard for {played:.2f}s of audio: {cut[0].heard_text!r}")


async def test_the_ledger_credits_playout_up_to_where_the_audio_really_stopped():
    # Hermetic, on the ledger alone (the shapes test_ledger_playout feeds it). The pause
    # frame says how long after it the audio stops (the written frame's slot); the
    # ledger credits that much, freezes, and re-anchors at the resume.
    from test_ledger_playout import OUT, TTS, audio as l_audio, feed, ledger, response, started, at
    L = ledger()
    frames, end = response("one two three four five six seven eight nine ten", 0.0)
    pz = PlayoutPauseFrame(paused=True, lag_secs=0.2)
    rz = PlayoutPauseFrame(paused=False)
    await feed(L, frames + [
        (started("c"), 0.3, None), at(l_audio(2.0, "c"), 0.3),
        (TTSStoppedFrame(context_id="c"), 0.35, None),   # all 2.0 s synthesized
        (BotStartedSpeakingFrame(), 1.0, OUT),
        (pz, 1.5, OUT),                       # the audio stops at 1.7
        (rz, 2.5, OUT),                       # ... and goes on from 2.5
        (InterruptionFrame(), 3.0, None),     # 0.7 + 0.5 = 1.2 s of 2.0 played
    ])
    cut = [u for u in L.events if u.speaker == "assistant"]
    assert cut and cut[0].interrupted
    assert abs(cut[0].heard_fraction - 0.6) < 0.02, cut[0].heard_fraction


class _FakeOut:
    def __init__(self):
        self.calls = []
        self.playout_paused = False

    async def set_playout_paused(self, paused):
        self.calls.append((time.monotonic(), paused))
        self.playout_paused = paused


async def _pauser_rig(**kw):
    out = _FakeOut()
    p = BargeInPauser(out, resume_secs=RESUME_S, **kw)
    task = PipelineTask(Pipeline([p]), cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)
    return out, p, task, run


async def test_no_pause_while_the_bot_is_silent_and_a_cap_on_a_long_one():
    out, p, task, run = await _pauser_rig(max_secs=0.8)
    try:
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert not out.calls, "paused with the bot silent: that is the reply hold's job"
        await p.on_onset(False)
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)                          # never stops talking
        await asyncio.sleep(1.0)
        assert [x[1] for x in out.calls] == [True, False], out.calls
        assert 0.75 <= out.calls[1][0] - out.calls[0][0] <= 0.9
    finally:
        await task.cancel()
        await run


async def test_a_bot_stopped_under_a_pause_leaves_it_to_its_own_end():
    # The pause landed in a gap the TTS had not filled: the transport's queue ran dry and
    # it said the bot stopped (after 0.35 s, or pipecat's 3 s fallback). The pause is not
    # lifted for it: a caller's lone word may still be on its way to the judge, and the
    # next sentence must not start over it. Its quiet window ends it, as any pause's.
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        await asyncio.sleep(0.3)
        await p.on_onset(False)                          # quiet: the window starts
        await task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        assert p.paused and [x[1] for x in out.calls] == [True], out.calls
        await asyncio.sleep(RESUME_S + 0.1)
        assert not p.paused and [x[1] for x in out.calls] == [True, False], out.calls
    finally:
        await task.cancel()
        await run


async def test_a_resume_the_transport_made_on_its_own_ends_the_pause_here_too():
    # The transport's write guard resumes playout itself: the pauser must not go on
    # counting the bot paused (it would refuse the next pause).
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert p.paused
        out.playout_paused = False                       # the guard resumed it
        await task.queue_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        assert not p.paused, "the pauser still counts the bot paused"
    finally:
        await task.cancel()
        await run


async def test_a_stale_resume_notice_does_not_end_a_newer_pause():
    # The pauser resumed, then paused again before its own resume's notice came back up:
    # that notice must not end the newer pause (the transport is paused again).
    out = _FakeOut()
    p = BargeInPauser(out, resume_secs=RESUME_S, max_secs=3.0)
    task = PipelineTask(Pipeline([p]), cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(STARTUP_S)
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        await task.queue_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        assert p.paused, "an old resume notice ended a pause the transport still holds"
    finally:
        await task.cancel()
        await run


async def test_an_echo_like_burst_does_not_pause():
    # Residual echo shows up as a short run of confident frames. The onset test wants
    # TEAPORT_ONSET_MIN_MS (128 ms) of it; 96 ms is not enough, 160 ms is.
    from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams
    from teaport_brain.speech_onset import SpeechOnsetMixin

    class Scripted(VADAnalyzer):
        def __init__(self, confs):
            super().__init__(sample_rate=16000, params=VADParams(stop_secs=0.2))
            self.confs = list(confs)

        def num_frames_required(self):
            return 512

        def voice_confidence(self, buffer):
            return self.confs.pop(0) if self.confs else 0.0

    for burst, want in ((3, 0), (5, 1)):
        out, p, task, run = await _pauser_rig()
        try:
            await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
            await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            await asyncio.sleep(0.05)
            confs = [0.1] * 3 + [0.9] * burst + [0.1] * 12
            vad = type("Onset", (SpeechOnsetMixin, Scripted), {})(confs)
            vad.set_sample_rate(16000)
            vad.onset_listeners.append(p.on_onset)
            for _ in confs:
                await vad.analyze_audio(b"\x00\x00" * 512)
            assert p.pauses == want, f"{burst} x 32 ms burst: {p.pauses} pauses, want {want}"
        finally:
            await task.cancel()
            await run


def test_a_stop_word_ends_a_paused_reply_and_a_backchannel_does_not():
    async def run():
        started = []
        s = PauseAwareMinWordsStrategy(min_words=2)

        async def on_started(_s, *a, **k):
            started.append(True)

        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())

        async def said(text, interim=True):
            started.clear()
            cls = InterimTranscriptionFrame if interim else TranscriptionFrame
            await s.process_frame(cls(text, "u", "t"))
            return bool(started)

        await s.process_frame(PlayoutPauseFrame(paused=True))
        assert await said("Stop."), "a lone stop word must end a paused reply"
        assert await said("wait", interim=False)
        assert not await said("Yeah."), "a backchannel must not cancel: the pause resumes"
        assert await said("Hold on."), "a stop phrase is a stop"
        assert await said("Shhh!"), "any shh spelling is shh"
        assert not await said("No."), "'no' is deliberately not a stop word"
        assert await said("hang on please"), "two words still interrupt, as before"
        await s.process_frame(PlayoutPauseFrame(paused=False))
        assert await said("Stop."), "resumed: a stop word still ends it (#216)"
        await s.process_frame(PlayoutPauseFrame(paused=True))
        await s.process_frame(BotStoppedSpeakingFrame())
        assert await said("Stop."), "bot silent: one word is a turn anyway"

    asyncio.run(run())


def test_a_stop_word_ends_a_reply_that_never_paused():
    # #216: the pause fails most on short unvoiced words, and "stop" is one -- a lone
    # "Stop." over a reply still playing must end it, on every front end (the strategy
    # is the session's, paused or not, pause on or off). Everything else keeps
    # TEAPORT_INTERRUPT_MIN_WORDS.
    async def run(min_words):
        started = []
        s = PauseAwareMinWordsStrategy(min_words=min_words)

        async def on_started(_s, *a, **k):
            started.append(True)

        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())

        async def said(text, interim=False):
            started.clear()
            cls = InterimTranscriptionFrame if interim else TranscriptionFrame
            await s.process_frame(cls(text, "u", "t"))
            return bool(started)

        assert await said("Stop."), "a lone stop word over a playing reply"
        assert await said("Wait!", interim=True), "its interim too, as while paused"
        assert await said("Hold on."), "a stop phrase -- even as a backchannel"
        assert await said("Shhh"), "any shh spelling"
        assert await said("Wait, wait!")
        assert not await said("Yeah."), "a backchannel stays under the guard"
        assert not await said("No."), "'no' is deliberately not a stop word"
        return s

    asyncio.run(run(2))
    s = asyncio.run(run(3))
    # min_words=3: two words that are not all stop words stay under it.
    async def two_words():
        started = []

        async def on_started(_s, *a, **k):
            started.append(True)

        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(TranscriptionFrame("stop by", "u", "t"))
        assert not started, "'stop by' is not a stop, and two words are under 3"
        await s.process_frame(TranscriptionFrame("hold it", "u", "t"))
        assert not started, "'hold it' is not all stop words"
        await s.process_frame(TranscriptionFrame("please hold on", "u", "t"))
        assert started, "three words: the guard's own count"

    asyncio.run(two_words())


def test_a_stop_word_counts_only_when_the_whole_utterance_is_one():
    from teaport_brain.barge_pause import is_stop_utterance

    async def run():
        started = []
        s = PauseAwareMinWordsStrategy(min_words=3)

        async def on_started(_s, *a, **k):
            started.append(True)

        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(PlayoutPauseFrame(paused=True))
        await s.process_frame(TranscriptionFrame("don't stop", "u", "t"))
        assert not started, "'don't stop' is not a stop, and two words are under 3"
        await s.process_frame(TranscriptionFrame("Wait, wait!", "u", "t"))
        assert started

    asyncio.run(run())
    assert is_stop_utterance("Stop.") and is_stop_utterance("sshh")
    assert not is_stop_utterance("stop by the shop") and not is_stop_utterance("")


def test_a_one_word_answer_after_the_reply_would_have_ended_is_a_turn():
    # Review must-fix 1: "...shall I book it?" -- "Yes." over the last syllable. The
    # pause held back the reply's end, so the bot still counted as speaking when the
    # final landed and the 2-word guard dropped it. Once the held-back audio would
    # have finished anyway, one word is a turn, as it was without the pause.
    async def run(tail, wait):
        started, finals = [], []
        s = PauseAwareMinWordsStrategy(min_words=2)

        async def on_started(_s, *a, **k):
            started.append(True)

        async def final_without_turn(words=False):
            finals.append(True)

        s._on_final_without_turn = final_without_turn
        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(PlayoutPauseFrame(paused=True, tail_secs=tail))
        await asyncio.sleep(wait)
        await s.process_frame(TranscriptionFrame("Yes.", "u", "t"))
        return bool(started), bool(finals)

    assert asyncio.run(run(0.2, 0.3)) == (True, False), "the answer was lost"
    assert asyncio.run(run(5.0, 0.3)) == (False, True), (
        "mid-reply, a one-word final stays a backchannel -- and tells the pauser")
    assert asyncio.run(run(-1.0, 0.3)) == (False, True), "synthesis not over: no tail"


class _PipelineClock:
    """A pipeline clock the test moves by hand (ns)."""
    t = 10_000_000_000

    def get_time(self):
        return self.t


def _tts_on_a_voice_clock():
    """An EngineTTSService on a VoiceClock that runs on a hand-moved pipeline clock."""
    from teaport_brain.engine_tts import EngineTTSService
    from teaport_brain.playout_pause import VoiceClock

    tts, base, voice = EngineTTSService(voice="af_heart"), _PipelineClock(), VoiceClock()
    voice.attach(base)
    tts.voice_clock = voice
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    tts.push_frame = push
    return tts, base, voice, pushed


def test_a_pause_during_a_hold_leaves_the_hold_s_freeze_alone():
    # The hold freezes the TTS's playout model; a pause in the middle of it (a filler
    # playing over the held reply) stands the voice clock still, and the model is on that
    # clock: the hold still freezes it after the pause, and moves it at its end by the
    # voice clock's time since -- the pause's own time is not in it (formal/VoiceClock.tla,
    # vc_holdWall).
    from teaport_brain.reply_hold import PlayoutHoldFrame

    async def run():
        tts, base, voice, _ = _tts_on_a_voice_clock()
        tts._play_end = tts._now() + 1.0
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        voice.stop()
        base.t += 200_000_000
        voice.start()
        base.t += 200_000_000
        assert abs(tts._lead_secs() - 1.0) < 1e-6, "the pause ending unfroze what the hold froze"
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        assert abs(tts._lead_secs() - 1.0) < 1e-6, "nothing played through the hold"
        base.t += 250_000_000
        assert abs(tts._lead_secs() - 0.75) < 1e-6, tts._lead_secs()

    asyncio.run(run())


def test_the_tts_leaves_a_pause_to_the_voice_clock_and_passes_it_on():
    async def run():
        tts, base, voice, pushed = _tts_on_a_voice_clock()
        tts._play_end = tts._now() + 1.0
        await tts.process_frame(PlayoutPauseFrame(paused=True), FrameDirection.UPSTREAM)
        voice.stop()
        base.t += 250_000_000
        assert abs(tts._lead_secs() - 1.0) < 1e-6, "nothing plays during a pause"
        voice.start()
        await tts.process_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)
        assert abs(tts._lead_secs() - 1.0) < 1e-6, "the pause moved the model by itself too"
        assert not tts._freezes, "a pause is no freeze of the TTS's own"
        assert [type(f).__name__ for f, _ in pushed] == ["PlayoutPauseFrame"] * 2, (
            "the strategy above the TTS reads the pause frames too")

    asyncio.run(run())


def test_the_tts_stamps_on_the_voice_clock():
    # A reply placed after a pause: its baseline (pipecat's start_word_timestamps) and a
    # spoken segment's announcement (tts_service.push_frame) are on the voice clock, as
    # the transport releases them -- not on the pipeline clock, which ran on through it.
    from pipecat.frames.frames import AggregatedTextFrame
    from pipecat.utils.text.base_text_aggregator import AggregationType

    async def run():
        tts, base, voice, pushed = _tts_on_a_voice_clock()
        voice.stop()
        base.t += 700_000_000
        voice.start()
        assert voice.now_ns() == base.t - 700_000_000
        await tts.start_word_timestamps()
        assert tts._initial_word_timestamp == voice.now_ns(), (
            tts._initial_word_timestamp, voice.now_ns(), base.t)
        del tts.push_frame                               # the service's own push
        seg = AggregatedTextFrame("Hello there.", AggregationType.SENTENCE)
        seg.will_be_spoken = True
        tts._word_last_pts = 0
        await tts.push_frame(seg)
        assert seg.pts == voice.now_ns(), (seg.pts, voice.now_ns(), base.t)
        # A reply hold holding anchored audio back: its words are on the frozen schedule
        # (the baseline pin), and so is its announcement, just ahead of them (review 5).
        tts._frozen_at, tts._frozen_from = tts._now(), voice.now_ns() - 400_000_000
        held = AggregatedTextFrame("Then this.", AggregationType.SENTENCE)
        held.will_be_spoken = True
        await tts.push_frame(held)
        assert held.pts == tts._frozen_from, (held.pts, tts._frozen_from)

    asyncio.run(run())


def test_a_pause_in_an_underrun_is_not_a_playout_gap():
    # formal/README, PlayoutPause pp_late_ahead_heard; VoiceClock.tla's late clause: the
    # first clause has played out and the next is not synthesized yet; the caller makes a
    # sound, the transport pauses with nothing to hold back, and resumes. The next clause
    # is placed right after the first on the voice clock, which stood still through the
    # pause: no gap, so its words land on its audio.
    async def run():
        tts, base, voice, _ = _tts_on_a_voice_clock()
        tts._ctx_audio_secs["c"] = 1.0           # the first clause's audio
        tts._play_end = tts._now()               # ... just played out: an underrun
        voice.stop()
        base.t += 300_000_000
        voice.start()
        tts._note_playout("c", 0.5, mid_sentence=False)   # the next clause, at once
        assert tts._ctx_audio_secs["c"] < 1.05, (
            f"the pause joined the playout gap: the next clause starts at "
            f"{tts._ctx_audio_secs['c']:.2f}s")

    asyncio.run(run())


async def test_no_pause_in_the_last_half_second_of_a_reply():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 1.7)                              # 0.3 s of 2.0 s left, all synthesized
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.05)
        assert not rig.pauser.paused and rig.pauser.skipped_near_end == 1
        await rig.pauser.on_onset(False)
        await _at(rig, 2.4)
        assert rig.wire.payload() == SPEECH and not rig.wire.gaps()


async def test_a_caller_already_talking_when_the_bot_starts_is_paused_at_once():
    async with Rig() as rig:
        await rig.pauser.on_onset(True)                  # talking over the reply's start
        await rig.speak()
        await asyncio.sleep(0.1)
        assert rig.pauser.paused, "no rising edge will come: the start itself must pause"
        assert len(rig.wire.datagrams) <= 2, f"{len(rig.wire.datagrams)} frames played"
        await rig.pauser.on_onset(False)
        await _at(rig, 3.2)
        assert rig.wire.payload() == SPEECH


async def test_the_thinking_bed_is_not_paused():
    # The typing is plain audio (thinking_sound.py), not bot speech: nothing to pause.
    async with Rig() as rig:
        await rig.task.queue_frames([OutputAudioRawFrame(_pcm(i), SR, 1) for i in range(5)])
        while not rig.wire.times:
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.2)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.1)
        assert not rig.pauser.paused, "pausing the bed only delays the answer behind it"


async def test_after_two_no_words_pauses_nothing_pauses_until_words_come():
    # 2026-11-06, the box: a ringtone near it read as speech, over and over, and the
    # box's own "Someone's calling me..." was paused each time. Two pauses with no words
    # in them, and nothing pauses -- the onset test nor the VAD -- until words come.
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
    async with Rig() as rig:
        rig.pauser._resume_secs = 0.15
        await rig.speak()
        for k in range(3):
            await _at(rig, 0.2 + 0.4 * k)
            await rig.pauser.on_onset(True)
            await asyncio.sleep(0.05)
            await rig.pauser.on_onset(False)
        await asyncio.sleep(0.4)
        assert rig.pauser.pauses == 2 and rig.pauser.resumes == 2
        assert rig.pauser.skipped_noisy == 1, "the third word-less pause must be skipped"
        await rig.task.queue_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        assert not rig.pauser.paused, "the VAD's start no more than the onset test"
        await rig.task.queue_frame(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.05)
        await rig.pauser.on_final_without_turn(words=True)   # "yeah": words came
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.05)
        assert rig.pauser.paused, "words re-arm it"
        await rig.task.queue_frame(VADUserStoppedSpeakingFrame())


async def test_a_blip_resumes_sooner_and_a_turnless_final_sooner_still():
    async with Rig() as rig:
        rig.pauser._resume_secs = 0.8                     # the default window
        await rig.speak()
        await _at(rig, 0.4)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.1)                          # a 0.1 s blip
        await rig.pauser.on_onset(False)
        t_quiet = time.monotonic()
        while rig.pauser.paused:
            await asyncio.sleep(0.005)
        assert time.monotonic() - t_quiet < 0.4, "a blip resumes after ~0.3 s, not RESUME_S"
        await _at(rig, 1.0)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.4)
        await rig.pauser.on_onset(False)
        await asyncio.sleep(0.02)
        t_final = time.monotonic()
        await rig.pauser.on_final_without_turn()         # "Yeah." closed: no turn
        while rig.pauser.paused:
            await asyncio.sleep(0.005)
        assert time.monotonic() - t_final < 0.25, "the STT's close says it was nothing"


async def test_renewed_speech_cancels_a_pending_resume():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.4)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.3)
        await rig.pauser.on_onset(False)
        await asyncio.sleep(RESUME_S / 2)
        await rig.pauser.on_onset(True)                  # they go on talking
        await asyncio.sleep(RESUME_S)
        assert rig.pauser.paused, "the resume scheduled at the first quiet must not fire"
        await rig.pauser.on_onset(False)


async def test_the_transport_resumes_on_its_own_before_the_write_deadline():
    from teaport_brain import playout_pause
    guard, playout_pause._PAUSE_GUARD_S = playout_pause._PAUSE_GUARD_S, 0.3
    try:
        async with Rig(with_pauser=False) as rig:
            await rig.speak()
            await _at(rig, 0.4)
            await rig.out.set_playout_paused(True)       # nobody ever resumes it
            await _at(rig, 1.2)
            gaps = rig.wire.gaps()
            assert len(gaps) == 1 and 0.25 <= gaps[0][1] - gaps[0][0] <= 0.4, gaps
            assert rig.pause_frames(FrameDirection.DOWNSTREAM) == [True, False]
    finally:
        playout_pause._PAUSE_GUARD_S = guard


async def test_one_sip_writes_waits_share_one_guard():
    # A SIP write of several datagrams (a direct write: the end's silence, a DTMF
    # tone) waits on the pause once per datagram. Its waits share the one guard:
    # MediaSender bounds the whole write at 10 s. (Reply audio comes one datagram per
    # write, audio_out_10ms_chunks=2, each with a budget of its own.)
    from pipecat.frames.frames import OutputAudioRawFrame
    from teaport_brain import playout_pause
    guard, playout_pause._PAUSE_GUARD_S = playout_pause._PAUSE_GUARD_S, 0.4
    try:
        async with Rig(with_pauser=False) as rig:
            await rig.out.set_playout_paused(True)
            t0 = time.monotonic()
            writer = asyncio.create_task(rig.out.write_audio_frame(
                OutputAudioRawFrame(b"\x01\x00" * BYTES_PER_FRAME * 2, SR, 1)))
            await asyncio.sleep(0.3)
            await rig.out.set_playout_paused(False)     # the first datagram goes
            await asyncio.sleep(0.005)
            await rig.out.set_playout_paused(True)      # the second waits: same write
            await asyncio.wait_for(writer, 2.0)
            held = time.monotonic() - t0
            assert held <= 0.4 + 0.1, f"the write was held {held:.2f}s: past its one guard"
            assert len(rig.wire.datagrams) == 4, "all of the write went out"
    finally:
        playout_pause._PAUSE_GUARD_S = guard


def test_the_longest_cap_and_its_grace_end_before_the_transport_s_guard():
    # One guard, the transport's and the pauser's; the longest cap the setting allows,
    # with the grace after its close of held words, ends before it -- and a cap asked
    # for past it, or a resume window that would stretch the cap past it, is held to it.
    from teaport_brain import barge_pause as bp, playout_pause
    from teaport_brain.settings import ROWS
    from teaport_brain.sip_transport import SipGatewayOutputTransport
    from teaport_brain.talk_transport import TalkOutputTransport
    # The guard lives in barge_pause; playout_pause's wait, which both transports use,
    # takes it from there.
    assert playout_pause._PAUSE_GUARD_S is bp.PAUSE_GUARD_S
    for cls in (SipGatewayOutputTransport, TalkOutputTransport):
        assert cls._wait_if_paused is playout_pause.PausablePlayout._wait_if_paused, cls
    assert ROWS["TEAPORT_BARGE_PAUSE_MAX_S"]["max"] == bp.MAX_CAP_S
    assert ROWS["TEAPORT_BARGE_PAUSE_RESUME_S"]["max"] == bp.MAX_CAP_S
    assert bp.MAX_CAP_S + bp._FINAL_GRACE_S < bp.PAUSE_GUARD_S
    for kw in ({"max_secs": 8.0}, {"max_secs": 1.0, "resume_secs": 8.0}):
        p = BargeInPauser(_FakeOut(), **{"resume_secs": RESUME_S, **kw})
        assert p._max_secs == bp.MAX_CAP_S, kw
        assert p._resume_secs <= bp.MAX_CAP_S, kw


async def test_an_interruption_in_the_cap_s_grace_ends_the_grace_too():
    # The cap closed the held words and waits its grace; their interruption ends the
    # pause, and the caller pauses a new one at once: the old cap does not resume it.
    out, p, task, run = await _pauser_rig(max_secs=0.5)
    closed = []

    async def close():
        closed.append(True)
        return True
    p.before_cap = close
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        p.hold_resume(True, who="the turn's final")
        await asyncio.sleep(0.55)                       # the cap: closed, its grace on
        assert closed and p.paused
        await task.queue_frame(InterruptionFrame())     # the words take the turn
        await asyncio.sleep(0.03)
        assert not p.paused and p.cancels == 1
        await p.on_onset(True)                          # a new pause, inside the grace
        await asyncio.sleep(0.2)                        # the old grace long out
        assert p.paused, "the old cap's grace resumed the new pause"
        assert [c[1] for c in out.calls] == [True, True], out.calls
    finally:
        await task.cancel()
        await run


async def test_an_interruption_clears_the_pause_everywhere():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.4)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.05)
        await rig.task.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        assert not rig.pauser.paused and rig.pauser.cancels == 1
        assert rig.out._playing.is_set(), "the transport must not stay paused"
    s = PauseAwareMinWordsStrategy(min_words=2)
    await s.process_frame(BotStartedSpeakingFrame())
    await s.process_frame(PlayoutPauseFrame(paused=True))
    await s.process_frame(InterruptionFrame())
    assert not s._playout_paused


async def test_the_end_of_the_call_lifts_a_pause():
    from pipecat.frames.frames import EndFrame
    rig = Rig()
    await rig.__aenter__()
    await rig.speak()
    await _at(rig, 0.4)
    await rig.out.set_playout_paused(True)
    t0 = time.monotonic()
    await rig.task.queue_frame(EndFrame())
    await asyncio.wait_for(rig._run, timeout=6.0)
    await rig.wire.stop()
    assert time.monotonic() - t0 < 3.0, "the end waited on the pause guard"


async def test_a_write_woken_with_its_buffer_gone_sends_no_empty_datagram():
    # A write blocked on the pause, woken after an interruption cleared its buffer: it
    # must stop, not send a 1-byte datagram (an empty 20 ms frame to the gateway).
    from pipecat.frames.frames import OutputAudioRawFrame
    wire = _TimedWire()
    wire.start()
    out = wire.build_output(end_silence_secs=0)
    out._playing.clear()
    writer = asyncio.create_task(out.write_audio_frame(
        OutputAudioRawFrame(b"\x01\x00" * BYTES_PER_FRAME, SR, 1)))
    await asyncio.sleep(0.05)
    assert not writer.done(), "the write should be holding at the pause"
    out._audio_send_buffer.clear()
    out._drop_pause()
    assert await asyncio.wait_for(writer, 1.0) is False, "a cut frame was not written"
    await wire.stop()
    assert all(len(d) == 1 + BYTES_PER_FRAME for d in wire.datagrams), (
        [len(d) for d in wire.datagrams])


async def test_reply_audio_with_no_bot_speaking_does_not_pause():
    # The reply's audio passes the pauser BEFORE the transport says the bot started:
    # until then there is nothing audible to pause.
    out, p, task, run = await _pauser_rig()
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert not out.calls and p.pauses == 0
        await p.on_onset(False)
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        assert not p._reply_audio, "a finished window must not leave 'reply audio' behind"
    finally:
        await task.cancel()
        await run


async def test_words_being_judged_re_arm_the_no_words_limit():
    # A backchannel over the pause goes to the judge (BargeInJudgeGate holds the resume):
    # words came, and the line is not only noise.
    out, p, task, run = await _pauser_rig()
    try:
        p._resume_secs = 0.1
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        for _ in range(2):
            await p.on_onset(True)
            await asyncio.sleep(0.03)
            await p.on_onset(False)
            await asyncio.sleep(0.4)
        assert p._noisy, "two word-less pauses"
        p.hold_resume(True)                               # the judge, on words
        p.hold_resume(False)
        assert not p._noisy and not p._noword_at
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert p.paused
    finally:
        await task.cancel()
        await run


async def test_the_no_words_limit_ends_with_the_reply():
    out, p, task, run = await _pauser_rig()
    try:
        p._resume_secs = 0.1

        async def window():
            await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
            await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            await asyncio.sleep(0.05)

        async def blip():
            await p.on_onset(True)
            await asyncio.sleep(0.03)
            await p.on_onset(False)
            await asyncio.sleep(0.4)

        await window()
        for _ in range(3):
            await blip()
        assert p.pauses == 2 and p.skipped_noisy == 1
        await task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await window()                                    # the next reply
        await blip()
        assert p.pauses == 3, "the limit is the reply's: a new reply starts at zero"
    finally:
        await task.cancel()
        await run


async def test_what_counts_against_the_no_words_limit():
    # Any pause that resumes with no words in it counts (a ringtone, a cough that lasts);
    # a backchannel's words do not, and start the count over; a pause the cap ended does
    # not count; nor one longer ago than the window.
    from teaport_brain import barge_pause as bp
    out, p, task, run = await _pauser_rig(max_secs=0.3)
    window, bp._NOWORD_WINDOW_S = bp._NOWORD_WINDOW_S, 1.5
    try:
        p._resume_secs = 0.1
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)

        async def sound(secs=0.15):
            await p.on_onset(True)
            await asyncio.sleep(secs)
            await p.on_onset(False)
            await asyncio.sleep(0.25)

        await sound()                                     # no words: 1
        assert len(p._noword_at) == 1
        await p.on_onset(True)                            # "yeah": its final has words
        await asyncio.sleep(0.15)
        await p.on_onset(False)
        await p.on_final_without_turn(words=True)
        await asyncio.sleep(0.25)
        assert len(p._noword_at) == 0, "words start the count over"
        await sound(secs=0.4)                             # held to the cap: not counted
        assert len(p._noword_at) == 0, p._noword_at
        await sound()                                     # 1
        await asyncio.sleep(1.6)                          # out of the window
        await sound()                                     # 1 again, not 2
        assert len(p._noword_at) == 1 and not p._noisy
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert p.paused, "still pausing"
    finally:
        bp._NOWORD_WINDOW_S = window
        await task.cancel()
        await run


async def test_a_turnless_final_does_not_resume_over_a_caller_still_talking():
    async with Rig() as rig:
        await rig.speak()
        await _at(rig, 0.4)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.1)
        await rig.pauser.on_final_without_turn()          # an earlier final, landing now
        await asyncio.sleep(0.3)
        assert rig.pauser.paused, "the caller is still talking: no resume"
        await rig.pauser.on_onset(False)


def test_a_wordless_close_while_paused_tells_the_pauser():
    from teaport_brain.endpointing import SegmentDoneFrame

    async def run():
        told = []

        async def final_without_turn(words=False):
            told.append(True)

        s = PauseAwareMinWordsStrategy(min_words=2, on_final_without_turn=final_without_turn)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(SegmentDoneFrame(stop_n=1))
        assert not told, "not paused: nothing to resume"
        await s.process_frame(PlayoutPauseFrame(paused=True))
        await s.process_frame(SegmentDoneFrame(stop_n=2))
        assert told, "a wordless close while paused must let the pauser resume sooner"

    asyncio.run(run())


def test_a_tail_learned_during_the_pause_is_measured_from_its_start():
    # The reply's synthesis completed while it was paused: the transport re-announces
    # the pause with the tail it now knows. "Yes." after that tail (from the pause's
    # start) is a turn.
    async def run():
        started = []
        s = PauseAwareMinWordsStrategy(min_words=2)

        async def on_started(_s, *a, **k):
            started.append(True)

        s.add_event_handler("on_user_turn_started", on_started)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(PlayoutPauseFrame(paused=True, tail_secs=-1.0))
        await asyncio.sleep(0.2)
        await s.process_frame(PlayoutPauseFrame(paused=True, tail_secs=0.25))
        await asyncio.sleep(0.1)                          # 0.3 s after the pause began
        await s.process_frame(TranscriptionFrame("Yes.", "u", "t"))
        return bool(started)

    assert asyncio.run(run()), "the late tail must be anchored at the pause's start"


async def test_the_transport_announces_a_tail_learned_during_the_pause():
    async with Rig() as rig:
        frames = [TTSStartedFrame(context_id="r1")]
        frames += [TTSAudioRawFrame(_pcm(i), SR, 1, context_id="r1") for i in range(N_CHUNKS)]
        await rig.task.queue_frames(frames)               # no TTSStoppedFrame yet
        while not rig.wire.times:
            await asyncio.sleep(0.002)
        rig.t0 = rig.wire.times[0]
        await _at(rig, 0.4)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.1)
        await rig.task.queue_frame(TTSStoppedFrame(context_id="r1"))
        await asyncio.sleep(0.1)
        tails = [f.tail_secs for _, d, f in rig.bottom.frames
                 if isinstance(f, PlayoutPauseFrame) and f.paused
                 and d == FrameDirection.DOWNSTREAM]
        assert tails[0] == -1.0 and len(tails) == 2, tails
        assert 1.5 <= tails[1] <= 1.7, f"the held-back rest is ~1.6 s, got {tails[1]}"
        await rig.pauser.on_onset(False)


async def test_the_ledger_cuts_the_words_on_the_voice_clock():
    # The TTS stamps every word on the transport's voice clock, which stands still while
    # a pause holds the voice still. So the ledger reads its cut on that clock: reply A
    # (word k at 1.0 + 0.2 k on it) paused 1.0 s from 1.5, cut at 3.0 -- the voice clock
    # reads 2.0 there, and the words before it, less the caption lead, were heard. And
    # reply B, whose audio came after the pause, is anchored on the same clock: no
    # second move for it either (the old pts_shift bookkeeping, gone).
    from test_ledger_playout import (OUT, audio as l_audio, feed, ledger, response,
                                     started, at, word)
    L = ledger()
    text = "one two three four five six seven eight"
    a_frames, _ = response(text, 0.0)
    words = [(word(w, 1.0 + 0.2 * k, "a"), 0.3, None) for k, w in enumerate(text.split())]
    await feed(L, a_frames + [
        (started("a"), 0.3, None), at(l_audio(1.6, "a"), 0.3), *words,
        (BotStartedSpeakingFrame(), 1.0, OUT),
        (PlayoutPauseFrame(paused=True), 1.5, OUT),
        (PlayoutPauseFrame(paused=False), 2.5, OUT),
        (InterruptionFrame(), 3.0, None),
    ])
    cut = [u for u in L.events if u.speaker == "assistant"]
    assert cut and cut[0].interrupted
    want = sum(1 for k in range(8) if 1.0 + 0.2 * k <= 2.0 - CAPTION_LEAD_SECS + 1e-9)
    assert len(cut[0].heard_text.split()) == want, (cut[0].heard_text, want)


async def test_the_session_builds_the_pauser_only_where_playout_can_pause():
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params
    from test_service_unusable import _Transport

    wire = _Wire()
    session = build_agent_session(SipGatewayTransport(wire.connection, make_sip_params()),
                                  reply_hold_enabled=True)
    out = session.ledger._output
    pauser = out.previous
    assert isinstance(pauser, BargeInPauser), f"above the SIP transport: {pauser!r}"
    p = session.tts
    while not isinstance(p, LLMUserAggregator):
        p = p.previous
    assert pauser.on_onset in p._params.vad_analyzer.onset_listeners
    starts = p._params.user_turn_strategies.start
    assert [type(s) for s in starts] == [PauseAwareMinWordsStrategy]
    # One clock for when the words play: the transport's, handed to the TTS that stamps
    # on it and the reply hold that measures on it (the ledger reads it off the transport).
    from teaport_brain.reply_hold import ReplyHoldGate
    assert out.voice_clock is not None and session.tts.voice_clock is out.voice_clock
    p, gates = out, []
    while p is not None:
        if isinstance(p, ReplyHoldGate):
            gates.append(p)
        p = p.previous
    assert gates and all(g.voice_clock is out.voice_clock for g in gates), gates
    wire.connection.close()
    wire.gw_sock.close()

    talk = build_agent_session(_Transport())          # no supports_playout_pause
    assert not isinstance(talk.ledger._output.previous, BargeInPauser)
    assert talk.tts.voice_clock is None, "no pausable transport: the pipeline clock"


async def test_the_judge_s_session_asks_for_the_report_its_heard_out_wait_needs():
    # Audit A2: the pause's heard-out wait needs the engine's transcription report, which
    # only the speculative reply asked for: with the judge on and segment turn text, a
    # late word cut the playing reply unjudged. Asked for with the judge -- not without
    # it (the user, 2026-11-06: there the wait only holds a cough longer).
    from teaport_brain import barge_pause as bp, speculate
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params
    from test_service_unusable import _Transport

    saved = bp.JUDGE, speculate.ENABLED
    speculate.ENABLED = False
    try:
        for judge in (True, False):
            bp.JUDGE = judge
            wire = _Wire()
            try:
                session = build_agent_session(
                    SipGatewayTransport(wire.connection, make_sip_params()))
                assert isinstance(session.ledger._output.previous, BargeInPauser)
                assert session.stt.transcription_progress is judge, (
                    judge, session.stt.transcription_progress)
            finally:
                wire.connection.close()
                wire.gw_sock.close()
        bp.JUDGE = True
        talk = build_agent_session(_Transport())      # no pauser, so no judge
        assert not talk.stt.transcription_progress, "nothing asks: not asked"
    finally:
        bp.JUDGE, speculate.ENABLED = saved


async def test_the_sip_session_wires_a_sliced_barge_in_s_hold_and_close_to_the_pauser():
    # The S1 fix's production wiring (stt.py TURN TEXT): on SIP, where the pauser is, a
    # sliced barge-in waiting on its final holds the pause through stt.barge_hold, and
    # the pause's cap closes it first through before_cap.
    from teaport_brain import stt as stt_mod
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params

    wire = _Wire()
    saved = stt_mod.TURN_TEXT
    stt_mod.TURN_TEXT = "slice"
    try:
        session = build_agent_session(SipGatewayTransport(wire.connection, make_sip_params()))
    finally:
        stt_mod.TURN_TEXT = saved
    try:
        pauser = session.ledger._output.previous
        assert isinstance(pauser, BargeInPauser)
        assert session.stt.slices_asked
        session.stt.barge_hold(True)
        assert pauser._holds == {"the turn's final"}, pauser._holds
        session.stt.barge_hold(False)
        assert pauser._holds == set()
        assert pauser.before_cap == session.stt.close_held
        # A quiet line waits for the STT to have heard the speech out (#275), and the
        # start strategy hands the STT's notice back to this pauser.
        assert pauser.watch_heard == session.stt.watch_heard
        from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
        agg = session.tts
        while not isinstance(agg, LLMUserAggregator):
            agg = agg.previous
        start = agg._params.user_turn_strategies.start[0]
        await start._on_speech_heard(5)
        assert pauser._watch is None and not pauser._heard, "not this pauser's watch"
        pauser._watch = 5
        await start._on_speech_heard(5)
        assert pauser._heard, "the strategy's notice reaches the session's pauser"
    finally:
        wire.connection.close()
        wire.gw_sock.close()


# ---- HEARD OUT (#275): a quiet line waits for the STT to have heard the speech out --

_FINAL_GRACE = 0.15


class _Watch:
    """The STT's watch_heard: each watch asked, and whether a notice will come."""

    def __init__(self, reports=True):
        self.reports, self.asked = reports, []

    async def __call__(self, watch):
        self.asked.append(watch)
        return self.reports


async def _paused_over_a_reply(p, task):
    await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
    await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await asyncio.sleep(0.05)
    await p.on_onset(True)
    await asyncio.sleep(0.3)                            # longer than a blip
    assert p.paused


async def _resumed_within(p, secs):
    t = time.monotonic()
    while p.paused and time.monotonic() - t < secs + 0.5:
        await asyncio.sleep(0.005)
    return not p.paused and time.monotonic() - t < secs


async def test_a_quiet_line_waits_past_the_window_for_the_stt_to_hear_it_out():
    # The live shape (2026-11-02): a short word over the reply trips only the onset
    # test, and its first interim comes 20 ms after the quiet window resumed the reply.
    # Where the STT will report, the window no longer resumes: the STT's notice does.
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch()
    try:
        await _paused_over_a_reply(p, task)
        await p.on_onset(False)
        assert p.watch_heard.asked == [p._watch] and p._watch is not None
        await asyncio.sleep(RESUME_S + 0.2)
        assert p.paused, "resumed on the window with the speech not yet heard out"
        await p.on_speech_heard(p._watch)
        assert await _resumed_within(p, 0.25), "heard out, no turn: resume after the grace"
        assert p.resumes == 1 and len(p._noword_at) == 1, "a word-less pause counts"
    finally:
        await task.cancel()
        await run


async def test_with_no_report_the_window_resumes_and_a_report_that_never_comes_is_bounded():
    from teaport_brain import barge_pause as bp
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch(reports=False)
    try:
        await _paused_over_a_reply(p, task)
        await p.on_onset(False)
        assert p._watch is None, "no notice will come: nothing to wait for"
        assert await _resumed_within(p, RESUME_S + 0.1), "the window, as before"
    finally:
        await task.cancel()
        await run
    saved, bp._HEARD_WAIT_S = bp._HEARD_WAIT_S, 0.6
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch()
    try:
        await _paused_over_a_reply(p, task)
        await p.on_onset(False)
        t = time.monotonic()
        await asyncio.sleep(0.5)
        assert p.paused
        assert await _resumed_within(p, 0.25), "the report never came: its bound resumes"
        assert 0.55 <= time.monotonic() - t <= 0.8
    finally:
        bp._HEARD_WAIT_S = saved
        await task.cancel()
        await run


async def test_a_notice_for_earlier_speech_does_not_resume_later_speech():
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch()
    try:
        await _paused_over_a_reply(p, task)
        await p.on_onset(False)
        first = p._watch
        await p.on_onset(True)                          # they go on talking
        await p.on_speech_heard(first)                  # the first speech's notice
        await asyncio.sleep(0.05)
        await p.on_onset(False)
        second = p._watch
        assert second != first and p.watch_heard.asked == [first, second]
        await p.on_speech_heard(first)                  # late: overtaken by the second
        await asyncio.sleep(RESUME_S + 0.2)
        assert p.paused, "an earlier speech's notice resumed over words still to come"
        await p.on_speech_heard(second)
        assert await _resumed_within(p, 0.25)
    finally:
        await task.cancel()
        await run


async def test_a_released_hold_waits_for_the_notice_and_then_for_what_it_decided():
    # The judge carried on over the first words: more of the speech may still come, so
    # the release waits on the STT as a quiet line does. And a release never resumes on
    # the grace (review F2): what the hold decided -- the judge's words, the STT's final
    # -- is only now on its way down, and its own arrival, if it starts no turn, resumes.
    for notice_while_held in (False, True):
        out, p, task, run = await _pauser_rig(max_secs=3.0)
        p.watch_heard = _Watch()
        try:
            await _paused_over_a_reply(p, task)
            p.hold_resume(True)
            await p.on_onset(False)
            if notice_while_held:
                await p.on_speech_heard(p._watch)
                await asyncio.sleep(0.25)
                assert p.paused, "held: only the cap resumes"
                p._resume_secs = 0.6                      # the window well clear of the grace
                p.hold_resume(False)
                await asyncio.sleep(_FINAL_GRACE + 0.05)
                assert p.paused, "the release resumed on the grace, ahead of its close"
                await p.on_final_without_turn()           # the close arrives: no turn
            else:
                p.hold_resume(False)
                await asyncio.sleep(RESUME_S + 0.2)
                assert p.paused, "released onto the window, the speech not heard out"
                await p.on_speech_heard(p._watch)
            assert await _resumed_within(p, 0.25), notice_while_held
        finally:
            await task.cancel()
            await run


async def test_a_close_of_earlier_speech_waits_for_the_notice_of_the_latest():
    # "Yeah" closed with no turn (its VAD stop committed it), then a short word the VAD
    # never heard: the close comes back after the line went quiet, but the later word
    # may still be on its way. The notice, not the close, says all of it is in.
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch()
    try:
        await _paused_over_a_reply(p, task)
        await p.on_onset(False)
        await p.on_final_without_turn()                 # an earlier segment's close
        await asyncio.sleep(RESUME_S + 0.2)
        assert p.paused, "a close of earlier speech resumed over words still to come"
        await p.on_speech_heard(p._watch)
        assert await _resumed_within(p, 0.25)
    finally:
        await task.cancel()
        await run


async def test_a_blip_waits_for_the_stt_too_and_still_counts_as_echo_like():
    # On Talk's 0.2 s stop a short "yeah" is a blip: it must not resume past its words.
    out, p, task, run = await _pauser_rig(max_secs=3.0)
    p.watch_heard = _Watch()
    try:
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        await p.on_onset(False)                         # a blip
        await asyncio.sleep(RESUME_S + 0.2)
        assert p.paused, "a blip resumed on its window over words still to come"
        await p.on_speech_heard(p._watch)
        assert await _resumed_within(p, 0.25) and len(p._noword_at) == 1, (
            "heard out with no words, a blip's resume counts against the no-words limit")
    finally:
        await task.cancel()
        await run


def test_the_strategy_hands_the_stt_s_notice_to_the_pauser_while_paused():
    from teaport_brain.endpointing import SpeechHeardFrame

    async def run():
        told = []

        async def speech_heard(watch):
            told.append(watch)

        s = PauseAwareMinWordsStrategy(min_words=2, on_speech_heard=speech_heard)
        await s.process_frame(BotStartedSpeakingFrame())
        await s.process_frame(SpeechHeardFrame(watch=1))
        assert not told, "not paused: nothing to resume"
        await s.process_frame(PlayoutPauseFrame(paused=True))
        await s.process_frame(SpeechHeardFrame(watch=2))
        assert told == [2]

    asyncio.run(run())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        if not asyncio.iscoroutinefunction(fn):
            fn()
            print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
