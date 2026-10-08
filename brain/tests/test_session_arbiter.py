#
# The session arbiter (session_arbiter.py, issue #58): one conversation at a time, and
# every pair of front-ends gets the policy, never a silent eviction.
#
#   Talk vs Talk            another client: refused, told busy; the same client: replaces
#   Talk vs room awake      refused, told busy; the room conversation goes on
#   Talk vs room asleep     the sleeping mic yields (4001), the Talk session runs
#   call vs room asleep     the sleeping mic yields (4002), the call gets the engine
#   call vs Talk            asked (#111: test_call_prompt.py); one that cannot be asked
#                           (answer_phone_call off) is told, then ended (4005)
#   Talk during a call      refused, told the agent is on a call
#   call vs room awake      asked (#111); one that cannot be asked keeps the box, and the
#                           caller hears busy
#   room vs a live session  refused: quietly while asleep, told when it asked to talk
#
# The Talk and room front-ends are driven through gateway_server.run_relay_bot with the
# pipeline stubbed; calls through the SIP front-end's CallClaim, and end to end through
# sip_server's connection loop over a real SEQPACKET socket (test_sip_call_lifecycle's
# fake gateway).
#
# Run: python test_session_arbiter.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)
os.environ.pop("GATEWAY_TOKEN", None)

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    InterruptionWorkerFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.agent_session import AgentSession, InputMute  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402

from teaport_brain import gateway_server as gs  # noqa: E402
from teaport_brain import session_arbiter as arb  # noqa: E402

LIVE = ("talk", "room awake", "call")


def _claim(name, client=None):
    kind = {"talk": arb.TALK, "room awake": arb.ROOM, "room asleep": arb.ROOM,
            "call": arb.CALL}[name]
    return arb.Claim(kind, client=client, asleep=lambda: name == "room asleep")


# ---------------------------------------------------------------- the policy itself

def test_the_policy_table():
    d = lambda holder, new, **kw: arb.decide(_claim(holder, kw.get("hc")), _claim(new, kw.get("nc")))
    assert arb.decide(None, _claim("talk")) is None
    assert d("talk", "talk", hc="a", nc="b") == arb.REFUSED
    assert d("talk", "talk", hc="a", nc="a") == arb.REPLACED
    assert d("talk", "talk") == arb.REFUSED                  # no id: never "the same"
    assert d("room awake", "talk") == arb.REFUSED
    assert d("room asleep", "talk") == arb.TAKEN
    assert d("room asleep", "call") == arb.CALL_IN
    assert d("talk", "call") == arb.CALL_IN
    assert d("call", "talk") == arb.REFUSED
    assert d("room awake", "call") == arb.REFUSED            # one that cannot be asked
    assert d("call", "room asleep") == arb.REFUSED
    assert d("talk", "room asleep") == arb.REFUSED
    assert d("room asleep", "room asleep", hc="local-audio", nc="local-audio") == arb.REPLACED
    assert d("call", "call", hc="sip", nc="sip") == arb.REPLACED
    # The same id under another kind is not the same client.
    assert d("talk", "room awake", hc="x", nc="x") == arb.REFUSED


def test_no_live_conversation_ever_loses_the_engine_without_being_told():
    """Every newcomer against every live holder: the holder keeps the engine, or it is a
    Talk session a call ends -- the one end its front-end speaks a line for."""
    for holder in LIVE:
        for new in (*LIVE, "room asleep"):
            why = arb.decide(_claim(holder, "h"), _claim(new, "n"))
            assert why == arb.REFUSED or (holder, new, why) == ("talk", "call", arb.CALL_IN), \
                (holder, new, why)


# ---------------------------------------------------------------- the front-ends

class _WS:
    """A /talk socket: what the brain sends, and how it closed it."""

    def __init__(self, query):
        self.query_params = query
        self.sent = []
        self.closed = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=None):
        self.closed.append((code, reason))


def _gate():
    """A real FollowupGate, unwired (push_frame and the interruption bookkeeping stubbed,
    as test_followup_gate.py does)."""
    gate = FollowupGate(quiet_secs=0.01)

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    async def no_bookkeeping(*a, **k):
        pass
    gate.push_frame = push
    gate._start_interruption = no_bookkeeping
    return gate


async def _feed(gate, *frames):
    for f in frames:
        await gate.process_frame(f, FrameDirection.DOWNSTREAM)


class _Task:
    """A pipeline task that plays what it is given through the session's real gate: an
    interruption reaches it as an InterruptionFrame, a spoken line as the transport's
    BotStarted/BotStoppedSpeaking (after `play_secs`)."""

    def __init__(self, ws, on_cancel=None, gate=None, play_secs=0.05):
        self.ws, self.on_cancel, self.gate, self.play_secs = ws, on_cancel, gate, play_secs
        self.queued, self.cancelled = [], False
        self.played = asyncio.Event()

    async def queue_frames(self, frames):
        self.queued += frames
        for f in frames:
            if self.gate is None:
                continue
            if isinstance(f, InterruptionWorkerFrame):
                await _feed(self.gate, InterruptionFrame())
            elif isinstance(f, TTSSpeakFrame):
                asyncio.get_running_loop().create_task(self._play())

    async def _play(self):
        await _feed(self.gate, BotStartedSpeakingFrame())
        await asyncio.sleep(self.play_secs)
        await _feed(self.gate, BotStoppedSpeakingFrame())
        self.played.set()

    async def cancel(self):
        self.cancelled = True
        await self.ws.close()                          # what pipecat does as it goes
        if self.on_cancel:
            self.on_cancel()


class _TTS:
    espeak_language = "en-us"

    def __init__(self, voice=None, language=None):
        if voice and voice.startswith("e"):
            self.espeak_language = "es"
        self.said = []

    async def synthesize(self, text):
        self.said.append(text)
        return b"\0\0" * 1200                          # 50 ms


def _fresh():
    arb.ARBITER = arb.SessionArbiter()
    arb._BUSY_PCM.clear()


async def _call(call_id="c1"):
    """A call asking the arbiter, as the SIP front-end does before its STT connects:
    returns (its CallClaim, the refusal's holder kind or None)."""
    from teaport_brain import sip_server
    claim = sip_server.CallClaim()
    refusal = await claim.start(call_id)
    return claim, None if refusal is None else refusal.holder


def _session(ws, gate=None, play_secs=0.05):
    """An AgentSession with its real say_last_line, real FollowupGate and InputMute, and
    a task that plays through them."""
    gate = gate or _gate()
    task = _Task(ws, gate=gate, play_secs=play_secs)
    return AgentSession(task=task, context=LLMContext([]), stt=None, tts=_TTS(), llm=None,
                        ledger=None, followup_gate=gate, input_mute=InputMute())


async def _holding(name, client=None, gone=None, play_secs=0.05):
    """A session of kind `name` holding the engine, ended the way its front-end ends it."""
    ws = _WS({})
    session = _session(ws, play_secs=play_secs)
    room = name.startswith("room")
    claim = arb.Claim(arb.ROOM if room else arb.TALK, client=client, label=name,
                      asleep=lambda: name == "room asleep", gone=gone,
                      end=lambda why: gs._end_for(ws, session, why, room=room,
                                                  asleep=name == "room asleep"))
    session.task.on_cancel = lambda: arb.ARBITER.release(claim)
    assert await arb.ARBITER.acquire(claim) is None
    return ws, session, claim


async def _dial(query):
    """One /talk connection through run_relay_bot, the pipeline stubbed: returns its
    socket and whether its pipeline ran."""
    ran, built = [], []

    class Transport:
        def __init__(self, **kw):
            pass

        def event_handler(self, name):
            return lambda fn: fn

    class Runner:
        def __init__(self, **kw):
            pass

        async def run(self, task):
            ran.append(arb.ARBITER.holder)

    def build(transport, **kw):
        built.append(SimpleNamespace(task=None, tts=_TTS(), followup_gate=_gate(),
                                     client_notes=SimpleNamespace(limits=lambda: {})))
        return built[-1]

    saved = (gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner, arb.tts_for)
    gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner = Transport, build, Runner
    arb.tts_for = _TTS
    ws = _WS(query)
    try:
        await gs.run_relay_bot(ws)
    finally:
        gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner, arb.tts_for = saved
    return ws, bool(ran)


def _spoken(ws):
    return [m["text"] for m in ws.sent if isinstance(m, dict) and m.get("type") == "transcript"]


ROOM_ASLEEP = {"wake": "hey teaport", "features": "volume,restart,local,sleep", "client": "local-audio"}
ROOM_AWAKE = {"features": "volume,restart,local", "client": "local-audio"}  # no wake words


async def test_talk_vs_talk_another_client_is_told_busy_and_the_first_goes_on():
    _fresh()
    holder_ws, holder, _ = await _holding("talk", client="openclaw:a")
    ws, ran = await _dial({"client": "openclaw:b"})
    assert not ran and not holder.task.cancelled and holder_ws.closed == []
    assert _spoken(ws) == [arb.busy_line(arb.TALK, "en-us")]
    assert any(isinstance(m, bytes) for m in ws.sent)            # the line, as audio
    assert ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: another conversation")]
    ws, ran = await _dial({})                                     # no id: refused alike
    assert not ran and ws.closed[-1][0] == gs.BUSY_CLOSE_CODE


async def test_talk_vs_talk_the_same_client_replaces_its_own_session():
    _fresh()
    holder_ws, holder, _ = await _holding("talk", client="openclaw:a")
    ws, ran = await _dial({"client": "openclaw:a"})
    assert ran and holder.task.cancelled
    assert holder_ws.closed == [(gs.TAKEN_CLOSE_CODE, gs.REPLACED_CLOSE_REASON)]
    assert ws.closed == [] and not arb.ARBITER.held()              # released after its run


async def test_talk_vs_room_awake_is_told_busy_and_the_room_goes_on():
    _fresh()
    room_ws, room, _ = await _holding("room awake", client="local-audio")
    ws, ran = await _dial({"client": "discord"})
    assert not ran and not room.task.cancelled and room_ws.closed == []
    assert _spoken(ws) == [arb.busy_line(arb.ROOM, "en-us")]
    assert ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: another conversation")]


async def test_talk_vs_room_asleep_takes_the_engine():
    _fresh()
    room_ws, room, _ = await _holding("room asleep", client="local-audio")
    ws, ran = await _dial({"client": "openclaw:a"})
    assert ran and room.task.cancelled
    assert room_ws.closed == [(gs.TAKEN_CLOSE_CODE, gs.TAKEN_CLOSE_REASON)]
    assert room.task.queued == []                                 # a sleeping room: no word


async def test_call_vs_room_asleep_takes_the_engine():
    _fresh()
    room_ws, room, _ = await _holding("room asleep", client="local-audio")
    call, busy = await _call()
    assert busy is None
    assert room_ws.closed == [(gs.YIELD_CLOSE_CODE, gs.YIELD_CLOSE_REASON)]
    assert gs.call_live()
    call.end()
    assert not arb.ARBITER.held()


async def test_call_vs_talk_the_talk_user_is_told_then_the_call_has_the_engine():
    _fresh()
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a")
    _, busy = await _call()
    assert busy is None
    # Whatever it was saying is cut, the line said, and only then the close.
    assert isinstance(talk.task.queued[0], InterruptionWorkerFrame)
    line = talk.task.queued[1]
    assert isinstance(line, TTSSpeakFrame) and line.text == arb.call_line("en-us")
    assert line.append_to_context is False
    assert talk_ws.closed == [(gs.CALL_CLOSE_CODE, gs.CALL_CLOSE_REASON)]
    assert gs.call_live()
    _fresh()


async def test_talk_during_a_call_is_told_the_agent_is_on_a_call():
    _fresh()
    assert (await _call())[1] is None
    ws, ran = await _dial({"client": "openclaw:a", "voice": "ef_dora"})
    assert not ran
    assert _spoken(ws) == [arb.busy_line(arb.CALL, "es")]          # in the client's language
    assert _spoken(ws)[0] != arb.busy_line(arb.CALL, "en-us")
    assert ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: a phone call")]
    assert gs.call_live()
    _fresh()


async def test_call_vs_room_awake_that_cannot_be_asked_is_refused():
    _fresh()
    room_ws, room, _ = await _holding("room awake", client="local-audio")
    assert (await _call())[1] == "room"
    assert not room.task.cancelled and not gs.call_live()
    _fresh()


async def test_the_room_against_a_live_session_is_refused_quietly_only_while_asleep():
    _fresh()
    await _holding("talk", client="openclaw:a")
    ws, ran = await _dial(ROOM_ASLEEP)
    assert not ran and _spoken(ws) == [] and ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: another conversation")]
    ws, ran = await _dial(ROOM_AWAKE)                              # someone in the room spoke
    assert not ran and _spoken(ws) == [arb.busy_line(arb.TALK, "en-us")]
    assert ws.closed[-1][0] == gs.BUSY_CLOSE_CODE
    _fresh()
    await _call()
    ws, _ = await _dial(ROOM_AWAKE)                                # during a call: wait it out
    assert _spoken(ws) == [arb.busy_line(arb.CALL, "en-us")]
    assert ws.closed[-1] == (gs.YIELD_CLOSE_CODE, gs.YIELD_CLOSE_REASON)
    _fresh()


async def test_a_busy_line_the_engine_cannot_say_still_closes_busy():
    _fresh()
    await _holding("talk", client="openclaw:a")

    class Mute(_TTS):
        async def synthesize(self, text):
            raise OSError("engine down")
    saved = arb.tts_for
    arb.tts_for = Mute
    try:
        ws = _WS({"client": "openclaw:b"})
        await gs._refuse(ws, arb.ARBITER.would_refuse(arb.Claim(arb.TALK, client="b")),
                         speak=True, room=False)
    finally:
        arb.tts_for = saved
    assert not any(isinstance(m, bytes) for m in ws.sent)
    assert ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: another conversation")]
    _fresh()


async def test_a_holder_that_never_lets_go_does_not_wedge_the_arbiter():
    _fresh()
    old = arb.END_WAIT_SECS
    arb.END_WAIT_SECS = 0.1

    async def end(why):
        pass                                         # never releases
    stuck = arb.Claim(arb.ROOM, client="local-audio", asleep=lambda: True, end=end)
    try:
        assert await arb.ARBITER.acquire(stuck) is None
        new = arb.Claim(arb.TALK, client="x")
        assert await arb.ARBITER.acquire(new) is None
        assert arb.ARBITER.holder is new
    finally:
        arb.END_WAIT_SECS = old
        _fresh()


# ---------------------------------------------------------------- the SIP brain's side

class _SipRig:
    """test_sip_call_lifecycle's fake gateway: the real sip_server connection loop over a
    real SEQPACKET socket, the per-call pipeline stubbed."""

    def __init__(self):
        import test_sip_call_lifecycle as lc
        self.lc = lc
        self.h = lc._Harness()


async def test_sip_end_to_end_a_call_during_talk_tells_the_talk_user_and_takes_the_engine():
    _fresh()
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a")
    rig = _SipRig()
    try:
        await rig.h.start()
        await rig.h.call_state("A", "confirmed")
        assert await rig.lc.wait_until(lambda: "A" in rig.lc._FakeSession.greeted, 5)
        assert talk_ws.closed == [(gs.CALL_CLOSE_CODE, gs.CALL_CLOSE_REASON)]
        assert talk.task.queued[1].text == arb.call_line("en-us")
        assert arb.ARBITER.holder.kind == arb.CALL
        await rig.h.call_state("A", "disconnected")
        assert await rig.lc.wait_until(lambda: not arb.ARBITER.held(), 5)
        ws, ran = await _dial({"client": "openclaw:a"})          # the box is free again
        assert ran
    finally:
        await rig.h.stop()
        _fresh()


async def test_sip_end_to_end_a_call_during_an_awake_room_hears_busy_and_is_hung_up():
    """Refused: no session is built (no models, no STT on the shared loop); the cached
    busy line goes out to the gateway in 20 ms frames, then the hangup."""
    from teaport_brain import sip_server
    from teaport_brain.sip_serializer import BYTES_PER_FRAME, MSG_AUDIO_OUT
    _fresh()
    room_ws, room, _ = await _holding("room awake", client="local-audio")
    rig = _SipRig()
    saved = (arb.tts_for, sip_server.SipConnection.send_control)
    arb.tts_for = _TTS
    sent = []

    async def send_control(self, msg):
        sent.append(msg)
    sip_server.SipConnection.send_control = send_control
    try:
        await rig.h.start()
        await rig.h.call_state("A", "confirmed")
        assert await rig.lc.wait_until(lambda: {"type": "call.hangup", "call_id": "A"} in sent, 5)
        assert rig.lc._FakeSession.built == []                    # nothing built for it
        audio = []
        rig.h.peer.setblocking(False)
        try:
            while True:
                audio.append(rig.h.peer.recv(4096))
        except BlockingIOError:
            pass
        frames = [d for d in audio if d[:1] == bytes([MSG_AUDIO_OUT])]
        assert frames and all(len(d) == 1 + BYTES_PER_FRAME for d in frames)
        assert not room.task.cancelled and arb.ARBITER.holder.kind == arb.ROOM
    finally:
        arb.tts_for, sip_server.SipConnection.send_control = saved
        await rig.h.stop()
        _fresh()


async def test_the_gateway_going_away_mid_call_gives_the_engine_back():
    _fresh()
    rig = _SipRig()
    try:
        await rig.h.start()
        await rig.h.call_state("A", "confirmed")
        assert await rig.lc.wait_until(lambda: arb.ARBITER.call_live(), 5)
        rig.h.peer.close()                                       # the gateway restarts
        rig.h.peer = None
        assert await rig.lc.wait_until(lambda: not arb.ARBITER.held(), 5)
    finally:
        await rig.h.stop()
        _fresh()


# ---------------------------------------------------------------- review fixes (#106)

async def test_the_call_line_is_heard_in_full_even_over_a_user_mid_sentence():
    """The user is talking when the call lands. The line waits for the interruption,
    is watched to its own end (not to the end of the user's speech), and nothing the
    user says can barge in on it: their audio and transcripts are dropped from then on."""
    _fresh()
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a", play_secs=0.4)
    await _feed(talk.followup_gate, UserStartedSpeakingFrame())   # mid-sentence
    started = asyncio.get_running_loop().time()
    call = asyncio.create_task(_call())
    await asyncio.sleep(0.1)
    await _feed(talk.followup_gate, UserStoppedSpeakingFrame())   # they stop: not the line's end
    assert (await call)[1] is None
    assert talk.task.played.is_set()                               # the line played out...
    assert asyncio.get_running_loop().time() - started >= 0.4 + 0.2   # ...to its end
    assert talk_ws.closed == [(gs.CALL_CLOSE_CODE, gs.CALL_CLOSE_REASON)]
    assert talk.input_mute.muted
    mute, below = talk.input_mute, InputMute(follow=talk.input_mute, kinds=(TranscriptionFrame,))
    passed = []
    for m in (mute, below):
        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            passed.append(frame)
        m.push_frame = push
    await mute.process_frame(InputAudioRawFrame(b"\0\0" * 160, 16000, 1), FrameDirection.DOWNSTREAM)
    await below.process_frame(TranscriptionFrame("stop", "u", "t"), FrameDirection.DOWNSTREAM)
    assert passed == []                                           # no barge-in from here
    _fresh()


async def test_a_call_that_ends_during_its_start_leaves_no_call_behind():
    """The call ends (its claim's end()) while its start waits in the arbiter (the Talk
    user is being told): nothing is left holding the box. And a bring-up cancelled
    mid-start (cancel_setup) leaves nothing either."""
    from teaport_brain import sip_server
    for how in ("end", "cancel"):
        _fresh()
        await _holding("talk", client="openclaw:a", play_secs=0.5)
        claim = sip_server.CallClaim()
        start = asyncio.create_task(claim.start("c1"))
        await asyncio.sleep(0.2)
        if how == "end":
            claim.end()
        else:
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        await asyncio.sleep(0.6)                                   # the Talk end finishes
        claim.end()                                                # disconnected: harmless
        assert not arb.ARBITER.held() and not gs.call_live(), how
    _fresh()


async def test_a_newcomer_that_gives_up_still_finishes_ending_the_holder():
    """The call is cancelled while the Talk user is being told: the Talk session is
    still closed and ended, never left told-but-open."""
    _fresh()
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a", play_secs=0.4)
    call = asyncio.create_task(arb.ARBITER.acquire(arb.Claim(arb.CALL, client="sip")))
    await asyncio.sleep(0.25)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    for _ in range(100):
        if talk.task.cancelled:
            break
        await asyncio.sleep(0.02)
    assert talk.task.cancelled and talk_ws.closed == [(gs.CALL_CLOSE_CODE, gs.CALL_CLOSE_REASON)]
    assert not arb.ARBITER.held()
    _fresh()


async def test_a_session_whose_client_is_gone_is_reaped_not_a_reason_to_refuse():
    _fresh()
    gone = {"v": False}
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a", gone=lambda: gone["v"])
    ws, ran = await _dial({"client": "openclaw:b"})
    assert not ran and ws.closed[-1][0] == gs.BUSY_CLOSE_CODE      # alive: refused
    gone["v"] = True                                                # its socket went silent
    ws, ran = await _dial({"client": "openclaw:b"})
    assert ran and talk_ws.closed == [(gs.TAKEN_CLOSE_CODE, gs.REAPED_CLOSE_REASON)]
    _fresh()


def test_gone_is_a_closed_socket_or_one_that_has_said_nothing_for_a_while():
    from starlette.websockets import WebSocketState
    ser = SimpleNamespace(last_rx=__import__("time").monotonic())
    open_ws = SimpleNamespace(client_state=WebSocketState.CONNECTED,
                              application_state=WebSocketState.CONNECTED)
    assert not gs._client_gone(open_ws, ser)
    assert gs._client_gone(SimpleNamespace(client_state=WebSocketState.DISCONNECTED), ser)
    ser.last_rx -= arb.STALE_SECS + 1
    assert gs._client_gone(open_ws, ser)


async def test_a_room_without_wake_words_is_asleep_until_someone_speaks_and_after_a_lull():
    """The bridge greets an empty room at start: that must not keep a Talk client out for
    the idle timeout. Asleep until the first user turn, awake while a conversation goes
    on, asleep again after the keep-alive with nobody speaking."""
    gate = _gate()
    assert gs.room_idle(gate, 0.2)                                 # greeted, nobody spoke
    await _feed(gate, UserStartedSpeakingFrame())
    assert not gs.room_idle(gate, 0.2)
    await _feed(gate, UserStoppedSpeakingFrame())
    assert not gs.room_idle(gate, 0.2)                             # just spoke
    await asyncio.sleep(0.25)
    assert gs.room_idle(gate, 0.2)                                 # the lull
    # With the arbiter: a room nobody has spoken to yields to Talk quietly (4001); one
    # in conversation refuses it.
    for spoken in (False, True):
        _fresh()
        ws = _WS({})
        session = _session(ws)
        claim = arb.Claim(arb.ROOM, client="local-audio",
                          asleep=lambda s=session: gs.room_idle(s.followup_gate, 45),
                          end=lambda why, s=session, w=ws: gs._end_for(
                              w, s, why, room=True, asleep=gs.room_idle(s.followup_gate, 45)))
        session.task.on_cancel = lambda c=claim: arb.ARBITER.release(c)
        assert await arb.ARBITER.acquire(claim) is None
        if spoken:
            await _feed(session.followup_gate, UserStartedSpeakingFrame())
        talk_ws, ran = await _dial({"client": "openclaw:a"})
        if spoken:
            assert not ran and ws.closed == [] and talk_ws.closed[-1][0] == gs.BUSY_CLOSE_CODE
        else:
            assert ran and ws.closed == [(gs.TAKEN_CLOSE_CODE, gs.TAKEN_CLOSE_REASON)]
            assert session.task.queued == []                       # no word to an empty room
    _fresh()


async def test_replacing_a_live_session_of_the_same_id_is_logged_as_a_warning():
    from loguru import logger
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        _fresh()
        await _holding("talk", client="openclaw:a")
        await _dial({"client": "openclaw:a"})
    finally:
        logger.remove(sink)
        _fresh()
    assert any("replaces a LIVE session" in ln for ln in lines)


async def test_the_busy_line_is_captioned_first_and_synthesized_once():
    _fresh()
    await _holding("talk", client="openclaw:a")
    events = []

    class TTS(_TTS):
        async def synthesize(self, text):
            events.append("synth")
            return await super().synthesize(text)
    saved = arb.tts_for
    arb.tts_for = TTS
    try:
        for _ in range(2):
            ws = _WS({"client": "openclaw:b"})
            orig = ws.send_text

            async def send_text(text, orig=orig):
                if json.loads(text)["type"] == "transcript":
                    events.append("caption")
                await orig(text)
            ws.send_text = send_text
            await gs._refuse(ws, arb.ARBITER.would_refuse(arb.Claim(arb.TALK, client="b")),
                             speak=True, room=False)
            assert any(isinstance(m, bytes) for m in ws.sent)
    finally:
        arb.tts_for = saved
        _fresh()
    assert events == ["caption", "synth", "caption"]


async def test_the_arbiter_says_when_nothing_is_live_anywhere():
    """For the face ("a sleeping face means the voice loop is not active anywhere"): a
    sleeping room is nothing live; a call or a Talk session is; listeners hear changes."""
    _fresh()
    seen = []
    arb.ARBITER.listeners.append(lambda: seen.append(arb.ARBITER.anything_live()))
    await _holding("room asleep", client="local-audio")
    assert not arb.ARBITER.anything_live() and arb.ARBITER.status()["live"] is False
    call = arb.Claim(arb.CALL, client="sip")
    assert await arb.ARBITER.acquire(call) is None and arb.ARBITER.anything_live()
    arb.ARBITER.release(call)
    assert not arb.ARBITER.anything_live()
    assert seen[:1] == [False] and True in seen and seen[-1] is False
    _fresh()


async def test_a_holder_already_being_ended_is_ended_once_and_a_newcomer_waits_for_it():
    """The first call gives up while the Talk user is being told; the end runs on
    (shielded). A second call, or a Talk client, in that window waits for it -- one call
    line, one close -- and is granted the slot it frees, not refused."""
    for second in (arb.Claim(arb.CALL, client="sip", label="call2"),
                   arb.Claim(arb.TALK, client="openclaw:b", label="talk b")):
        _fresh()
        talk_ws, talk, _ = await _holding("talk", client="openclaw:a", play_secs=0.6)
        c1 = asyncio.create_task(arb.ARBITER.acquire(arb.Claim(arb.CALL, client="sip")))
        await asyncio.sleep(0.3)
        c1.cancel()
        await asyncio.gather(c1, return_exceptions=True)
        assert arb.ARBITER.would_refuse(second) is None             # going, not live
        assert await arb.ARBITER.acquire(second) is None
        lines = [f for f in talk.task.queued if isinstance(f, TTSSpeakFrame)]
        assert len(lines) == 1 and talk_ws.closed == [(gs.CALL_CLOSE_CODE, gs.CALL_CLOSE_REASON)]
        assert arb.ARBITER.holder is second
    _fresh()


async def test_the_busy_line_cache_is_keyed_on_the_voice_used_and_bounded():
    _fresh()
    synths = []

    class TTS(_TTS):
        def __init__(self, voice=None, language=None):
            super().__init__(voice, language)
            self._voice = "af_heart"                                # what the engine is asked for

        async def synthesize(self, text):
            synths.append(text)
            return b"\0\0"
    saved = arb.tts_for
    arb.tts_for = TTS
    try:
        for junk in range(40):                                     # 40 client strings, one voice
            await arb.busy_line_audio(arb.TALK, voice=f"nonsense-{junk}")
        assert len(synths) == 1
        arb.tts_for = lambda v, l: SimpleNamespace(_voice=v, espeak_language="en-us",
                                                   synthesize=TTS().synthesize)
        for v in range(40):                                        # 40 real voices: capped
            await arb.busy_line_audio(arb.TALK, voice=f"v{v}")
        assert len(arb._BUSY_PCM) <= arb._BUSY_PCM_MAX
    finally:
        arb.tts_for = saved
        _fresh()


def test_a_room_is_not_idle_while_a_consult_answer_is_owed():
    gate = _gate()
    gate.user_heard = True
    gate.last_active -= 1000                                       # a long lull...
    assert gs.room_idle(gate, 1.0)
    gate.owed = 1                                                  # ...but an answer is coming
    assert not gs.room_idle(gate, 1.0)


async def test_the_busy_line_is_one_framed_response_closed_only_after_it_played():
    """OpenClaw's relay plays assistant output only for a live response: the busy line
    comes framed (start, caption, audio, done), and the close only after the done -- so
    the line is heard, and the session ends cleanly instead of failing."""
    _fresh()
    await _holding("talk", client="openclaw:a")
    ws = _WS({"client": "openclaw:b", "voice": "ef_dora"})
    closed_at = {}
    orig_close = ws.close

    async def close(code=1000, reason=None):
        closed_at["after"] = len(ws.sent)
        await orig_close(code, reason)
    ws.close = close
    saved = arb.tts_for
    arb.tts_for = _TTS
    try:
        await gs._refuse(ws, arb.ARBITER.would_refuse(arb.Claim(arb.TALK, client="b")),
                         speak=True, room=False, voice="ef_dora")
    finally:
        arb.tts_for = saved
    kinds = [m["type"] + (":" + m["state"] if m["type"] == "response" else "")
             if isinstance(m, dict) else "audio" for m in ws.sent]
    assert kinds == ["response:start", "transcript", "audio", "response:done"], kinds
    assert closed_at["after"] == len(ws.sent)                      # nothing after the close
    assert ws.sent[1]["text"] == arb.busy_line(arb.TALK, "es")     # the client's language
    _fresh()


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
