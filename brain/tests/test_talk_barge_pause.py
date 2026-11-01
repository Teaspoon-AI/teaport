#
# The barge-in pause on /talk (talk_transport.py, playout_pause.py): the same pause the
# phone has, on pipecat's websocket transport -- the transport every /talk client is fed
# by (the OpenClaw plugin, the dashboard, Discord, the box's own mic).
#
# The playout tests drive the REAL TalkOutputTransport over a fake websocket that keeps
# what was sent and when: what the client receives is what it plays, a jitter margin
# later, so "paused at once", "nothing lost or repeated" and "cancelled" are read off
# the socket byte for byte, as test_barge_pause reads the SIP gateway's end. The rig, the
# reply and the ledger checks are test_barge_pause's.
#
# All utterances are synthetic.
#
# Run: python test_talk_barge_pause.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

# The session test builds a whole session: a dummy LLM endpoint and engine, never dialled.
for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from starlette.websockets import WebSocketState  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams  # noqa: E402

from teaport_brain.barge_pause import BargeInPauser, PlayoutPauseFrame  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.talk_transport import TalkOutputTransport, TalkWebsocketTransport  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

from test_barge_pause import (  # noqa: E402
    CHUNK_S,
    RESUME_S,
    SPEECH,
    SR,
    Rig,
    Tap,
    _at,
    _pcm,
)

# One audio block: pipecat's audio_out_10ms_chunks (4) x 10 ms. The pause takes effect
# at the next block boundary.
BLOCK_S = 0.04


class _Socket:
    """The client's end of /talk: what the brain sent, and when."""

    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.datagrams = []   # binary sends: the voice
        self.times = []
        self.texts = []       # text sends: control

    async def send_bytes(self, data):
        self.datagrams.append(bytes(data))
        self.times.append(time.monotonic())

    async def send_text(self, data):
        self.texts.append(json.loads(data))

    async def close(self, *a, **k):
        self.client_state = self.application_state = WebSocketState.DISCONNECTED

    def iter_bytes(self):
        return self._never()

    async def _never(self):
        await asyncio.Event().wait()
        yield b""

    def start(self):
        pass

    async def stop(self):
        pass

    def payload(self):
        return b"".join(self.datagrams)

    def gaps(self, at_least=0.1):
        return [(self.times[i - 1], self.times[i]) for i in range(1, len(self.times))
                if self.times[i] - self.times[i - 1] >= at_least]


def _talk_transport(sock):
    return TalkWebsocketTransport(websocket=sock, params=FastAPIWebsocketParams(
        audio_in_enabled=False, audio_out_enabled=True, audio_out_sample_rate=SR,
        audio_out_end_silence_secs=0, add_wav_header=False,
        serializer=TeaportGatewaySerializer()))


class TalkRig(Rig):
    """test_barge_pause.Rig with the /talk output transport in place of the SIP one."""

    def __init__(self, with_pauser=True):
        self.wire = _Socket()
        self.out = _talk_transport(self.wire).output()
        self.pauser = BargeInPauser(self.out, resume_secs=RESUME_S, max_secs=3.0) \
            if with_pauser else None
        self.top, self.bottom = Tap(), Tap()
        self.ledger = TranscriptLedger(output=self.out)
        self.task = PipelineTask(
            Pipeline([p for p in [self.top, self.pauser, self.out, self.bottom] if p]),
            observers=[self.ledger], cancel_on_idle_timeout=False)


async def test_the_talk_transport_is_the_pausable_one():
    out = _talk_transport(_Socket()).output()
    assert isinstance(out, TalkOutputTransport) and out.supports_playout_pause


async def test_talk_speech_then_no_words_pauses_at_once_and_resumes_without_loss():
    async with TalkRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        t_onset = time.monotonic()
        await rig.pauser.on_onset(True)                 # the user coughs over the bot
        await _at(rig, 1.0)
        assert rig.pauser.paused
        await rig.pauser.on_onset(False)                # ... and no words follow
        t_quiet = time.monotonic()
        await _at(rig, 3.4)
        gaps = rig.wire.gaps()
        assert len(gaps) == 1, f"expected one pause in the playout, got {gaps}"
        stopped, resumed = gaps[0]
        assert stopped - t_onset < BLOCK_S + 0.025, (
            f"the last block before the pause left {1000*(stopped-t_onset):.0f} ms after "
            "the onset; the pause must take effect at the next block")
        assert RESUME_S - 0.05 <= resumed - t_quiet <= RESUME_S + 0.1, (
            f"resumed {resumed - t_quiet:.2f}s after the user went quiet")
        assert rig.wire.payload() == SPEECH, (
            f"{len(rig.wire.payload())} of {len(SPEECH)} bytes reached the client, or not "
            "in order: a resumed reply must lose and repeat nothing")
        assert not rig.wire.texts, f"a pause puts nothing on the wire: {rig.wire.texts}"
        assert rig.pause_frames(FrameDirection.DOWNSTREAM) == [True, False]
        assert rig.pause_frames(FrameDirection.UPSTREAM) == [True, False]
        pauses = [f for _, d, f in rig.bottom.frames
                  if isinstance(f, PlayoutPauseFrame) and f.paused]
        # What was sent and is still to play: the block playing and the one queued
        # behind it (the send clock runs one block ahead).
        assert BLOCK_S - 0.005 <= pauses[0].lag_secs <= 2 * BLOCK_S + 0.001, (
            f"pause lag {pauses[0].lag_secs}: one to two blocks")
        assert 1.2 <= pauses[0].tail_secs <= 1.5, (
            f"tail at the pause {pauses[0].tail_secs}: ~1.4 s of 2.0 s left")
        sent = sum(len(d) for d, t in zip(rig.wire.datagrams, rig.wire.times)
                   if t <= stopped) / 2 / SR
        # What was not sent, plus what was sent and still playing (the lag).
        assert abs(pauses[0].tail_secs - (2.0 - sent + pauses[0].lag_secs)) <= 0.01, (
            f"tail {pauses[0].tail_secs:.3f}: what was not sent, plus the lag still playing")
        said = rig.assistant()
        assert said and not said[0].interrupted and said[0].heard_fraction == 1.0, said


async def test_talk_speech_then_words_pauses_at_once_then_the_barge_in_clears():
    async with TalkRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        t_onset = time.monotonic()
        await rig.pauser.on_onset(True)
        await _at(rig, 1.1)                              # the words arrive: a turn starts
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.6)
        played = len(rig.wire.payload()) / 2 / SR
        assert rig.wire.times[-1] - t_onset < BLOCK_S + 0.025, "audio kept going after the pause"
        assert 0.5 <= played <= 0.7, f"{played:.2f}s of the reply reached the client"
        assert {"type": "clear"} in rig.wire.texts, "the barge-in must still clear the client"
        cut = rig.assistant()
        assert cut and cut[0].interrupted, cut
        # Heard: what played before the pause (~0.6 s of 2 s), not up to the cut (1.1 s).
        assert 0.22 <= cut[0].heard_fraction <= 0.4, cut[0].heard_fraction
        # The pause went with the cancelled reply: the next one plays at once.
        n_before = len(rig.wire.datagrams)
        t_next = time.monotonic()
        await rig.task.queue_frames([TTSStartedFrame(context_id="r2"),
                                     TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r2"),
                                     TTSStoppedFrame(context_id="r2")])
        await asyncio.sleep(0.4)
        assert len(rig.wire.datagrams) > n_before, (
            "the next reply did not play: the cancelled pause is still blocking the writes")
        assert rig.wire.times[n_before] - t_next < 0.1


async def test_talk_without_the_pauser_the_bot_talks_until_the_words_arrive():
    # /talk as it was: the same script plays on to the interruption.
    async with TalkRig(with_pauser=False) as rig:
        await rig.speak()
        await _at(rig, 1.1)
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.0)
        played = len(rig.wire.payload()) / 2 / SR
        assert played >= 1.0, f"only {played:.2f}s played; the comparison above is moot"


async def test_a_block_held_by_a_pause_an_interruption_dropped_is_not_written():
    # pipecat keeps the audio task running through an interruption when uninterruptible
    # frames are queued, so a write held at the pause is woken rather than cancelled: the
    # block it holds is the cut reply's and must not play.
    sock = _Socket()
    out = _talk_transport(sock).output()
    out._playing.clear()
    writer = asyncio.create_task(out.write_audio_frame(OutputAudioRawFrame(_pcm(0), SR, 1)))
    await asyncio.sleep(0.05)
    assert not writer.done(), "the write should be holding at the pause"
    out._drop_pause()
    assert await asyncio.wait_for(writer, 1.0) is False
    assert not sock.datagrams, "the held block was written after its reply was cut"


async def test_a_write_woken_by_a_resume_the_next_pause_undoes_stays_held():
    # A resume and a new pause in the same turn of the loop: the waiting write wakes with
    # playout paused again, and must not send its block into the new pause.
    sock = _Socket()
    out = _talk_transport(sock).output()
    out._sample_rate = SR
    out._playing.clear()
    writer = asyncio.create_task(out.write_audio_frame(OutputAudioRawFrame(_pcm(0), SR, 1)))
    await asyncio.sleep(0.05)
    out._playing.set()
    out._playing.clear()                                  # paused again before it ran
    await asyncio.sleep(0.05)
    assert not writer.done() and not sock.datagrams, "a block went out into the new pause"
    out._playing.set()
    assert await asyncio.wait_for(writer, 1.0) is True and sock.payload() == _pcm(0)


async def test_a_pause_shifts_only_the_replies_still_to_play():
    # r1 has played out when r2 is paused: only r2's words (and the ledger's cut for it)
    # move. A finished reply has nothing left to move, and keeps no shift.
    async with TalkRig() as rig:
        await rig.speak()
        await _at(rig, 2.3)                               # r1's 2 s are over
        await rig.task.queue_frames([TTSStartedFrame(context_id="r2")]
                                    + [TTSAudioRawFrame(_pcm(i), SR, 1, context_id="r2")
                                       for i in range(5)]
                                    + [TTSStoppedFrame(context_id="r2")])
        await _at(rig, 2.6)
        await rig.out.set_playout_paused(True)
        await _at(rig, 3.0)
        await rig.out.set_playout_paused(False)
        await asyncio.sleep(0.05)
        resume = [f for _, d, f in rig.bottom.frames if d == FrameDirection.DOWNSTREAM
                  and isinstance(f, PlayoutPauseFrame) and not f.paused][0]
        assert resume.shifted == ("r2",), resume.shifted
        assert "r1" not in rig.out._shift_ns, rig.out._shift_ns


def test_the_timed_frames_awaited_are_bounded():
    from pipecat.frames.frames import TextFrame
    from teaport_brain.playout_pause import _MAX_TIMED
    out = _talk_transport(_Socket()).output()
    for i in range(_MAX_TIMED + 50):
        f = TextFrame("w")
        f.pts = i + 1
        out._track_tail(f, FrameDirection.DOWNSTREAM)
    assert len(out._timed_ids) == _MAX_TIMED


async def test_a_talk_pause_nobody_resumes_ends_before_the_write_deadline():
    from teaport_brain import playout_pause
    guard, playout_pause._PAUSE_GUARD_S = playout_pause._PAUSE_GUARD_S, 0.3
    try:
        async with TalkRig(with_pauser=False) as rig:
            await rig.speak()
            await _at(rig, 0.4)
            await rig.out.set_playout_paused(True)       # nobody ever resumes it
            await _at(rig, 1.2)
            gaps = rig.wire.gaps()
            assert len(gaps) == 1 and 0.25 <= gaps[0][1] - gaps[0][0] <= 0.45, gaps
            assert rig.pause_frames(FrameDirection.DOWNSTREAM) == [True, False]
    finally:
        playout_pause._PAUSE_GUARD_S = guard


async def test_a_talk_session_builds_the_pauser():
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
    from teaport_brain.agent_session import build_agent_session
    from teaport_brain.barge_pause import PauseAwareMinWordsStrategy

    for front_end in ("talk", "room"):
        session = build_agent_session(_talk_transport(_Socket()), front_end=front_end)
        pauser = session.ledger._output.previous
        assert isinstance(pauser, BargeInPauser), f"{front_end}: above the transport: {pauser!r}"
        p = session.tts
        while not isinstance(p, LLMUserAggregator):
            p = p.previous
        assert pauser.on_onset in p._params.vad_analyzer.onset_listeners
        assert [type(s) for s in p._params.user_turn_strategies.start] == \
            [PauseAwareMinWordsStrategy]


# --- The words' clock through a pause (playout_pause.py, THE WORDS' CLOCK) ----------

def _words_out(rig):
    """(arrival, word) of each TTSTextFrame the transport released downstream."""
    from pipecat.frames.frames import TTSTextFrame
    return [(t, f.text) for t, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, TTSTextFrame)]


def _pause_times(rig):
    return [(t, f.paused) for t, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, PlayoutPauseFrame)]


async def test_the_words_keep_the_voices_time_through_a_pause():
    # pipecat's clock releases each word at its pts on the wall clock. Paused, nothing
    # plays: no word is released; resumed, each word of the reply comes the pause's length
    # later than scheduled -- when its audio plays -- not all at once, nor early.
    from test_barge_pause import WORDS
    from teaport_brain.tts_text import CAPTION_LEAD_SECS
    async with TalkRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.0)
        await rig.pauser.on_onset(False)
        await _at(rig, 3.4)
        (t_pause, _), (t_resume, _) = _pause_times(rig)
        lag = [f for _, d, f in rig.bottom.frames if d == FrameDirection.DOWNSTREAM
               and isinstance(f, PlayoutPauseFrame) and f.paused][0].lag_secs
        held = t_resume - t_pause - lag        # what was sent before the pause played on
        words = _words_out(rig)
        assert [w for _, w in words] == WORDS, words
        assert not [w for t, w in words if t_pause + 0.06 < t < t_resume - 0.01], (
            f"words released while paused: {words}")
        for i, (t, w) in enumerate(words):
            due = rig.t0 + i * CHUNK_S - CAPTION_LEAD_SECS
            if due > t_pause + 0.06:
                assert abs(t - (due + held)) <= 0.06, (
                    f"{w!r} came {t - due:.2f}s after its pts; the pause held {held:.2f}s")


async def test_a_cut_drops_the_words_a_pause_held():
    async with TalkRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.1)
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 2.6)
        (t_pause, _), = _pause_times(rig)
        late = [w for t, w in _words_out(rig) if t > t_pause + 0.06]
        assert not late, f"words of the cut reply released after the pause: {late}"


async def test_a_reply_after_the_resume_is_on_time():
    # A reply whose audio comes after the resume is anchored by the TTS on its moved
    # clock: the transport must not shift its words again.
    from pipecat.frames.frames import TTSTextFrame
    async with TalkRig() as rig:
        # The thinking sound's plain audio first: no words, so no reply to shift.
        await rig.task.queue_frames([OutputAudioRawFrame(_pcm(0)[:640], SR, 1)])
        await asyncio.sleep(0.1)
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.0)
        await rig.pauser.on_onset(False)
        await _at(rig, 3.4)
        resume = [f for _, d, f in rig.bottom.frames if d == FrameDirection.DOWNSTREAM
                  and isinstance(f, PlayoutPauseFrame) and not f.paused][0]
        pause = [f for _, d, f in rig.bottom.frames if d == FrameDirection.DOWNSTREAM
                 and isinstance(f, PlayoutPauseFrame) and f.paused][0]
        (t_pause, _), (t_resume, _) = _pause_times(rig)
        assert resume.shifted == ("r1",), resume.shifted
        assert abs(resume.shift_secs - (t_resume - t_pause - pause.lag_secs)) <= 0.02, (
            resume.shift_secs, t_resume - t_pause, pause.lag_secs)
        word = TTSTextFrame("next", aggregated_by="word", context_id="r2")
        word.pts = rig.out.get_clock().get_time() + int(0.3e9)
        t_queued = time.monotonic()
        await rig.task.queue_frames([TTSStartedFrame(context_id="r2"),
                                     TTSAudioRawFrame(_pcm(0), SR, 1, context_id="r2"),
                                     word, TTSStoppedFrame(context_id="r2")])
        await asyncio.sleep(0.6)
        t_next = [t for t, w in _words_out(rig) if w == "next"]
        assert t_next and abs(t_next[0] - t_queued - 0.3) <= 0.06, (t_next, t_queued)


async def test_the_end_of_a_session_in_a_pause_says_the_pause_is_over():
    from pipecat.frames.frames import EndFrame
    rig = TalkRig(with_pauser=False)
    await rig.__aenter__()
    await rig.speak()
    await _at(rig, 0.4)
    await rig.out.set_playout_paused(True)
    await asyncio.sleep(0.1)
    sent_in_pause = len(rig.wire.payload())
    await rig.task.queue_frame(EndFrame())
    await asyncio.wait_for(rig._run, timeout=6.0)
    assert [p for _, p in _pause_times(rig)] == [True, False], _pause_times(rig)
    assert rig.pause_frames(FrameDirection.UPSTREAM) == [True], (
        "the end's resume reaches no processor above the transport: they had the EndFrame")
    assert rig.wire.payload() == SPEECH and len(SPEECH) > sent_in_pause, (
        "the end plays out what the pause held, the held block included")


async def test_an_uninterruptible_block_is_written_though_the_pause_was_dropped():
    sock = _Socket()
    out = _talk_transport(sock).output()
    out._playing.clear()
    block = OutputAudioRawFrame(_pcm(0), SR, 1)
    block.interruptible = False
    writer = asyncio.create_task(out.write_audio_frame(block))
    await asyncio.sleep(0.05)
    out._drop_pause()
    assert await asyncio.wait_for(writer, 1.0) is True
    assert sock.payload() == _pcm(0), "pipecat keeps an uninterruptible block: so must we"


async def test_a_block_that_was_not_sent_is_not_counted_as_heard():
    sock = _Socket()
    out = _talk_transport(sock).output()
    out._sample_rate = SR      # as its StartFrame sets it: otherwise nothing is counted
    sock.client_state = WebSocketState.DISCONNECTED
    assert await out.write_audio_frame(OutputAudioRawFrame(_pcm(0), SR, 1)) is False
    assert out._heard_until == 0.0
    sock.client_state = WebSocketState.CONNECTED            # the control: one that went out
    assert await out.write_audio_frame(OutputAudioRawFrame(_pcm(0), SR, 1)) is True
    assert out._heard_until > 0.0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run_aio():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
