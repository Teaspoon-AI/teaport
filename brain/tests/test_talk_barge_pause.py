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
    check_a_cut_drops_the_words_a_pause_held,
    check_a_word_due_in_the_lag_waits_for_the_resume,
    check_a_reply_stamped_after_the_resume_is_on_time,
    check_the_words_keep_the_voices_time_through_a_pause,
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


def _talk_transport(sock, pausable=True):
    return TalkWebsocketTransport(websocket=sock, pausable=pausable, params=FastAPIWebsocketParams(
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


async def test_one_writes_waits_share_one_guard():
    # MediaSender gives a whole write 10 s before it writes the transport off for the
    # session. A write that waits out one pause and then a new one (a resume and a pause
    # in the same turn of the loop) must still be released inside the one guard.
    from teaport_brain import playout_pause
    guard, playout_pause._PAUSE_GUARD_S = playout_pause._PAUSE_GUARD_S, 0.4
    try:
        async with TalkRig(with_pauser=False) as rig:
            await rig.speak()
            await _at(rig, 0.4)
            t_paused = time.monotonic()
            await rig.out.set_playout_paused(True)
            await _at(rig, 0.7)                        # most of the guard
            rig.out._playing.set()
            rig.out._playing.clear()                   # a new pause at once
            await _at(rig, 1.6)
            gaps = rig.wire.gaps()
            assert len(gaps) == 1, gaps
            assert gaps[0][1] - t_paused <= 0.4 + 0.1, (
                f"the write was held {gaps[0][1] - t_paused:.2f}s: past its one guard")
    finally:
        playout_pause._PAUSE_GUARD_S = guard


async def test_a_quick_resume_keeps_the_send_clocks_one_block_of_lead():
    # A resume while what was sent before the pause still plays (a blip) must not start
    # the clock at once: two blocks would go out behind it and the client's lead would
    # grow by one for the rest of the reply, and every later pause stop that much later.
    async with TalkRig(with_pauser=False) as rig:
        await rig.speak()
        await _at(rig, 0.61)
        await rig.out.set_playout_paused(True)
        await asyncio.sleep(0.005)
        await rig.out.set_playout_paused(False)          # well inside the lag
        await _at(rig, 1.0)
        # The lead is a sawtooth (one to two blocks); its peak is what grows.
        lead = 0.0
        for _ in range(60):
            lead = max(lead, rig.out._heard_until - time.monotonic())
            await asyncio.sleep(0.005)
        assert lead <= 2 * BLOCK_S + 0.005, (
            f"{lead * 1000:.0f} ms queued at the client: the lead grew past one block")


async def test_a_resume_after_the_write_was_held_keeps_the_lead_too():
    # The pause holds the next write at its slot; the resume comes 20 ms later, while
    # what was sent before still plays. The woken write must wait for the slot the
    # resume set, not send at once with the next block straight behind it.
    async with TalkRig(with_pauser=False) as rig:
        await rig.speak()
        await _at(rig, 0.61)
        await rig.out.set_playout_paused(True)
        await asyncio.sleep(BLOCK_S + 0.02)            # the next write is held by now
        await rig.out.set_playout_paused(False)
        await asyncio.sleep(0.05)
        lead = 0.0
        for _ in range(60):
            lead = max(lead, rig.out._heard_until - time.monotonic())
            await asyncio.sleep(0.005)
        assert lead <= 2 * BLOCK_S + 0.005, (
            f"{lead * 1000:.0f} ms queued at the client: the lead grew past one block")


async def test_a_whole_buffer_write_counts_its_own_length():
    # The base's direct writes (the end's silence) send a whole buffer at once: heard
    # until its end, not one block's worth.
    sock = _Socket()
    out = _talk_transport(sock)
    out = out.output()
    out._sample_rate = SR
    t0 = time.monotonic()
    assert await out.write_audio_frame(OutputAudioRawFrame(b"\0\0" * int(0.5 * SR), SR, 1))
    assert abs(out._heard_until - t0 - 0.5) <= 0.05, out._heard_until - t0


async def test_a_gone_client_is_not_waited_on_through_a_pause():
    sock = _Socket()
    out = _talk_transport(sock).output()
    out._sample_rate = SR
    out._playing.clear()
    sock.client_state = WebSocketState.DISCONNECTED
    t0 = time.monotonic()
    assert await asyncio.wait_for(
        out.write_audio_frame(OutputAudioRawFrame(_pcm(0), SR, 1)), 1.0) is False
    assert time.monotonic() - t0 < 0.05, "a write to a gone client waited on the pause"


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

    from teaport_brain.echo_guard import PlayedWordsTap
    for front_end in ("talk", "room"):
        # Off by default on Talk and in the room (TEAPORT_BARGE_PAUSE_TALK and _ROOM):
        # /talk's transport is pipecat's own then, and nothing pauses.
        off_t = _talk_transport(_Socket(), pausable=False)
        assert not hasattr(off_t.output(), "supports_playout_pause")
        off = build_agent_session(off_t, front_end=front_end)
        assert not isinstance(off.ledger._output.previous, BargeInPauser)
        session = build_agent_session(_talk_transport(_Socket()), front_end=front_end)
        pauser = session.ledger._output.previous
        assert isinstance(pauser, BargeInPauser), f"{front_end}: above the transport: {pauser!r}"
        p = session.tts
        while not isinstance(p, LLMUserAggregator):
            p = p.previous
        assert pauser.on_onset in p._params.vad_analyzer.onset_listeners
        assert [type(s) for s in p._params.user_turn_strategies.start] == \
            [PauseAwareMinWordsStrategy]
    # The room's echo guard dates the words on the transport's voice clock (echo_guard.py).
    room = build_agent_session(_talk_transport(_Socket()), front_end="room",
                               client_features=frozenset({"local"}))
    from teaport_brain.playout_pause import PauseNotice

    def below(session):
        p, out = session.ledger._output.next, []
        while p is not None:
            out.append(p)
            p = p.next
        return out
    assert not any(isinstance(p, PauseNotice) for p in below(room)), "not asked for"
    told = build_agent_session(_talk_transport(_Socket()), front_end="room",
                               client_features=frozenset({"local", "pause"}))
    assert isinstance(told.ledger._output.next, PauseNotice), told.ledger._output.next
    unpaused = build_agent_session(_talk_transport(_Socket(), pausable=False),
                                   front_end="room", client_features=frozenset({"pause"}))
    assert not any(isinstance(p, PauseNotice) for p in below(unpaused)), "nothing pauses"
    out = room.ledger._output
    p, taps = out, []
    while p is not None:
        if isinstance(p, PlayedWordsTap):
            taps.append(p)
        p = p.next
    assert taps and all(t._voice is out.voice_clock for t in taps), taps


async def test_the_room_and_talk_pause_by_their_own_switches():
    """/talk's transport pauses by TEAPORT_BARGE_PAUSE_ROOM for the box's own mic
    (?features=local) and by TEAPORT_BARGE_PAUSE_TALK for every other client: the room
    can stay off while the app and browsers pause (its bridge's half of #255)."""
    from types import SimpleNamespace

    from teaport_brain import barge_pause as bp, gateway_server as gs

    class _Built(Exception):
        pass

    seen = []

    def make_transport(**kw):
        seen.append(kw["pausable"])
        raise _Built  # all this test needs is how the transport was asked for

    async def fetch(*a, **k):
        return None

    saved = (bp.ENABLED, bp.TALK_ENABLED, bp.ROOM_ENABLED)
    saved_gs = (gs.TalkWebsocketTransport, gs.host_context.fetch, gs.arb.ARBITER)
    gs.TalkWebsocketTransport, gs.host_context.fetch = make_transport, fetch
    try:
        for on, talk, room in [(True, True, False), (True, False, True), (True, True, True),
                               (True, False, False), (False, True, True)]:
            bp.ENABLED, bp.TALK_ENABLED, bp.ROOM_ENABLED = on, talk, room
            for query, want in (({"features": "local"}, on and room), ({}, on and talk),
                                ({"client": "discord"}, on and talk)):
                seen.clear()
                gs.arb.ARBITER = gs.arb.SessionArbiter()
                try:
                    await gs.run_relay_bot(SimpleNamespace(query_params=query,
                                                           send_text=lambda t: None))
                except _Built:
                    pass
                assert seen == [want], (on, talk, room, query, seen)
    finally:
        bp.ENABLED, bp.TALK_ENABLED, bp.ROOM_ENABLED = saved
        gs.TalkWebsocketTransport, gs.host_context.fetch, gs.arb.ARBITER = saved_gs


async def test_talk_words_keep_the_voices_time_through_a_pause():
    async with TalkRig() as rig:
        await check_the_words_keep_the_voices_time_through_a_pause(rig)


async def test_talk_a_word_due_in_the_lag_waits_for_the_resume():
    async with TalkRig(with_pauser=False) as rig:
        await check_a_word_due_in_the_lag_waits_for_the_resume(rig)


async def test_talk_a_cut_drops_the_words_a_pause_held():
    async with TalkRig() as rig:
        await check_a_cut_drops_the_words_a_pause_held(rig)


async def test_talk_a_reply_stamped_after_the_resume_is_on_time():
    async with TalkRig() as rig:
        await check_a_reply_stamped_after_the_resume_is_on_time(rig)


class NoticeRig(TalkRig):
    """TalkRig with playout_pause.PauseNotice below the transport, as a session builds it
    for a client that announced ?features=pause (the room bridge)."""

    def __init__(self):
        from teaport_brain.playout_pause import PauseNotice
        super().__init__()
        self.notice = PauseNotice()
        self.task = PipelineTask(
            Pipeline([self.top, self.pauser, self.out, self.notice, self.bottom]),
            observers=[self.ledger], cancel_on_idle_timeout=False)


async def test_a_client_that_asked_is_told_the_pause_and_its_end():
    # #255 part 2: the room bridge reads a quiet speaker as the reply's end (a goodbye
    # heard out, its idle timer); told of the pause, it waits it out.
    async with NoticeRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.0)
        await rig.pauser.on_onset(False)
        await _at(rig, 3.4)
        said = [m for m in rig.wire.texts if m.get("type") == "pause"]
        assert said == [{"type": "pause", "on": True}, {"type": "pause", "on": False}], said


async def test_a_cut_pause_says_nothing_more_the_clear_ends_it():
    async with NoticeRig() as rig:
        await rig.speak()
        await _at(rig, 0.6)
        await rig.pauser.on_onset(True)
        await _at(rig, 1.0)
        await rig.task.queue_frame(InterruptionFrame())
        await _at(rig, 1.6)
        types = [m.get("type") for m in rig.wire.texts]
        assert types.count("pause") == 1 and "clear" in types, rig.wire.texts
        assert types.index("pause") < types.index("clear")


def _pause_times(rig):
    return [(t, f.paused) for t, d, f in rig.bottom.frames
            if d == FrameDirection.DOWNSTREAM and isinstance(f, PlayoutPauseFrame)]


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
