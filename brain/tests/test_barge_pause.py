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
        assert lags and 0.0 < lags[0] <= 0.021, f"pause lag {lags}: the rest of one 20 ms slot"
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

    async def set_playout_paused(self, paused):
        self.calls.append((time.monotonic(), paused))


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

        assert not await said("Stop."), "while audible, one word stays under the guard"
        await s.process_frame(PlayoutPauseFrame(paused=True))
        assert await said("Stop."), "a lone stop word must end a paused reply"
        assert await said("wait", interim=False)
        assert not await said("Yeah."), "a backchannel must not cancel: the pause resumes"
        assert await said("Hold on."), "a stop phrase is a stop"
        assert await said("Shhh!"), "any shh spelling is shh"
        assert not await said("No."), "'no' is deliberately not a stop word"
        assert await said("hang on please"), "two words still interrupt, as before"
        await s.process_frame(PlayoutPauseFrame(paused=False))
        assert not await said("Stop.")
        await s.process_frame(PlayoutPauseFrame(paused=True))
        await s.process_frame(BotStoppedSpeakingFrame())
        assert await said("Stop."), "bot silent: one word is a turn anyway"

    asyncio.run(run())


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

        async def final_without_turn():
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


def test_a_pause_ending_inside_a_hold_does_not_unfreeze_the_tts():
    # Review S6: the hold and the pause share the TTS's playout model. One freeze per
    # source: the pause lifting while the hold is still on must leave it frozen.
    from teaport_brain.engine_tts import EngineTTSService
    from teaport_brain.reply_hold import PlayoutHoldFrame

    async def run():
        tts = EngineTTSService(voice="af_heart")

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            pass

        tts.push_frame = push
        tts._play_end = time.monotonic() + 1.0
        await tts.process_frame(PlayoutHoldFrame(active=True), FrameDirection.UPSTREAM)
        await tts.process_frame(PlayoutPauseFrame(paused=True), FrameDirection.UPSTREAM)
        await tts.process_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.2)
        assert tts._lead_secs() >= 0.98, "the pause ending unfroze a model the hold still froze"
        await tts.process_frame(PlayoutHoldFrame(active=False), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.2)
        assert 0.75 <= tts._lead_secs() <= 0.82, tts._lead_secs()

    asyncio.run(run())


def test_the_tts_freezes_its_playout_model_and_passes_the_pause_on():
    from teaport_brain.engine_tts import EngineTTSService

    async def run():
        tts = EngineTTSService(voice="af_heart")
        pushed = []

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append((frame, direction))

        tts.push_frame = push
        tts._play_end = time.monotonic() + 1.0
        await tts.process_frame(PlayoutPauseFrame(paused=True), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.25)
        assert 0.95 <= tts._lead_secs() <= 1.01
        await tts.process_frame(PlayoutPauseFrame(paused=False), FrameDirection.UPSTREAM)
        assert 0.95 <= tts._lead_secs() <= 1.01, "nothing played during the pause"
        assert [type(f).__name__ for f, _ in pushed] == ["PlayoutPauseFrame"] * 2, (
            "the strategy above the TTS reads the pause frames too")

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
    async with Rig() as rig:
        await rig.task.queue_frames([TTSAudioRawFrame(_pcm(i), SR, 1) for i in range(5)])
        while not rig.wire.times:
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.2)
        await rig.pauser.on_onset(True)
        await asyncio.sleep(0.1)
        assert not rig.pauser.paused, "pausing the bed only delays the answer behind it"


async def test_after_two_no_words_pauses_only_the_vad_pauses():
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
        assert rig.pauser.skipped_onset == 1, "the third onset-only pause must be skipped"
        await rig.task.queue_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        assert rig.pauser.paused, "the VAD's start (volume-gated) still pauses"
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
    from teaport_brain import sip_transport
    guard, sip_transport._PAUSE_GUARD_S = sip_transport._PAUSE_GUARD_S, 0.3
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
        sip_transport._PAUSE_GUARD_S = guard


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
    await asyncio.wait_for(writer, 1.0)
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


async def test_the_echo_like_budget_is_per_bot_speaking_window():
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
        assert p.pauses == 2 and p.skipped_onset == 1
        await task.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await window()                                    # the next reply
        await blip()
        assert p.pauses == 3, "the budget is per window: a new reply starts at zero"
    finally:
        await task.cancel()
        await run


async def test_only_echo_like_pauses_count_against_the_budget():
    # A cough that lasts, a backchannel whose final came back, a capped pause: none of
    # them is echo, and none may disarm the onset test for the rest of the reply.
    out, p, task, run = await _pauser_rig(max_secs=0.3)
    try:
        p._resume_secs = 0.1
        await task.queue_frames([TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r")])
        await task.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        for _ in range(2):                                # 0.35 s of speech, no words
            await p.on_onset(True)
            await asyncio.sleep(0.35)
            await p.on_onset(False)
            await asyncio.sleep(0.25)
        await p.on_onset(True)                            # "yeah": its final comes back
        await asyncio.sleep(0.3)
        await p.on_onset(False)
        await p.on_final_without_turn()
        await asyncio.sleep(0.25)
        assert p.pauses == 3 and p.resumes == 3 and p._noword == 0
        await p.on_onset(True)
        await asyncio.sleep(0.05)
        assert p.paused, "the onset test must still pause"
    finally:
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

        async def final_without_turn():
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


async def test_a_reply_whose_audio_starts_after_the_pause_is_not_shifted_twice():
    # Reply B's context opened before the pause but its first audio came after: the
    # TTS anchored it on the moved clock, so the ledger must not shift it again.
    from test_ledger_playout import OUT, audio as l_audio, feed, ledger, response, started, at
    L = ledger()
    a_frames, _ = response("one two three four", 0.0)
    b_frames, _ = response("five six seven eight", 0.4)
    await feed(L, a_frames + [
        (started("a"), 0.3, None), at(l_audio(1.0, "a"), 0.3),
        (BotStartedSpeakingFrame(), 1.0, OUT),
    ] + b_frames + [
        (started("b"), 1.2, None),                       # B's context, no audio yet
        (PlayoutPauseFrame(paused=True), 1.5, OUT),
        (PlayoutPauseFrame(paused=False), 2.5, OUT),
    ])
    shifts = {t["ctx"]: t["pts_shift"] for t in L._turns}
    assert shifts == {"a": 1.0, "b": 0.0}, shifts


async def test_the_session_builds_the_pauser_only_where_playout_can_pause():
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params
    from test_service_unusable import _Transport

    wire = _Wire()
    session = build_agent_session(SipGatewayTransport(wire.connection, make_sip_params()))
    out = session.ledger._output
    pauser = out.previous
    assert isinstance(pauser, BargeInPauser), f"above the SIP transport: {pauser!r}"
    p = session.tts
    while not isinstance(p, LLMUserAggregator):
        p = p.previous
    assert pauser.on_onset in p._params.vad_analyzer.onset_listeners
    starts = p._params.user_turn_strategies.start
    assert [type(s) for s in starts] == [PauseAwareMinWordsStrategy]
    wire.connection.close()
    wire.gw_sock.close()

    talk = build_agent_session(_Transport())          # no supports_playout_pause
    assert not isinstance(talk.ledger._output.previous, BargeInPauser)


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
