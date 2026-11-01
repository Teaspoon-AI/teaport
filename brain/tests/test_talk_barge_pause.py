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
    LLMFullResponseEndFrame,
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
from teaport_brain.captions import CaptionTap  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.talk_transport import TalkOutputTransport, TalkWebsocketTransport  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

from test_captions import END, P  # noqa: E402
from test_barge_pause import (  # noqa: E402
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


# --- The Talk view's captions through a pause (captions.CaptionTap, PAUSE-HOLD) --------

def _word(ctx, text, acc, pts=None):
    return P(ctx, 1, text, acc, pts)


class _Captions:
    """A CaptionTap whose sends are kept: the partials and finals the client gets."""

    def __init__(self):
        self.tap = CaptionTap(every_final=True)
        self.sent = []

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            msg = getattr(frame, "message", None)
            if isinstance(msg, dict) and msg.get("type") == "transcript":
                self.sent.append((msg["text"], msg["final"]))
        self.tap.push_frame = push

    async def feed(self, *frames):
        for f in frames:
            await self.tap.process_frame(f, FrameDirection.DOWNSTREAM)


async def test_captions_hold_through_a_pause_and_drop_it_on_a_cut():
    c = _Captions()
    s = "one two three four"
    await c.feed(_word("c", s, "one"), _word("c", s, "one two"),
                 PlayoutPauseFrame(paused=True),
                 _word("c", s, "one two three"))          # released, never heard
    assert c.sent == [("one", False), ("one two", False)], c.sent
    await c.tap._start_interruption()                    # the words take the turn
    await c.feed(_word("c", s, "one two three four"), LLMFullResponseEndFrame())
    assert c.sent == [("one", False), ("one two", False)], (
        f"the cut bubble must stay at what was heard: {c.sent}")


async def test_captions_go_on_at_the_resume():
    c = _Captions()
    s = "one two three"
    await c.feed(_word("c", s, "one", pts=1), PlayoutPauseFrame(paused=True),
                 _word("c", s, "one two", pts=2), END(3),
                 PlayoutPauseFrame(paused=True, tail_secs=0.5))   # re-announced: still held
    assert c.sent == [("one", False)], c.sent
    await c.feed(PlayoutPauseFrame(paused=False))
    assert c.sent == [("one", False), ("one two", False), ("one two three", True)], c.sent


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run_aio():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
