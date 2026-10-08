#
# The session arbiter (session_arbiter.py, issue #58): one conversation at a time, and
# every pair of front-ends gets the policy, never a silent eviction.
#
#   Talk vs Talk            another client: refused, told busy; the same client: replaces
#   Talk vs room awake      refused, told busy; the room conversation goes on
#   Talk vs room asleep     the sleeping mic yields (4001), the Talk session runs
#   call vs room asleep     the sleeping mic yields (4002), the call gets the engine
#   call vs Talk            the Talk user is told, then ended (4005); the call gets it
#   Talk during a call      refused, told the agent is on a call
#   call vs room awake      refused for now (the caller hears busy); #58's prompt is next
#   room vs a live session  refused: quietly while asleep, told when it asked to talk
#
# The Talk and room front-ends are driven through gateway_server.run_relay_bot with the
# pipeline stubbed; calls through /talk/call, as the SIP brain makes them.
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

from pipecat.frames.frames import InterruptionWorkerFrame, TTSSpeakFrame  # noqa: E402

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
    assert d("room awake", "call") == arb.REFUSED            # until #58's prompt
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


class _Task:
    def __init__(self, ws, on_cancel=None):
        self.ws, self.on_cancel = ws, on_cancel
        self.queued, self.cancelled = [], False

    async def queue_frames(self, frames):
        self.queued += frames

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
    gs._lease = None


async def _holding(name, client=None):
    """A session of kind `name` holding the engine, ended the way its front-end ends it."""
    ws = _WS({})
    session = SimpleNamespace(tts=_TTS(), followup_gate=SimpleNamespace(
        wait_until_delivered=lambda: asyncio.sleep(0)))
    room = name.startswith("room")
    claim = arb.Claim(arb.ROOM if room else arb.TALK, client=client, label=name,
                      asleep=lambda: name == "room asleep",
                      end=lambda why: gs._end_for(ws, session, why, room=room,
                                                  asleep=name == "room asleep"))
    session.task = _Task(ws, on_cancel=lambda: arb.ARBITER.release(claim))
    assert await arb.ARBITER.acquire(claim) is None
    return ws, session, claim


async def _dial(query):
    """One /talk connection through run_relay_bot, the pipeline stubbed: returns its
    socket and whether its pipeline ran."""
    ran = []

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
        return SimpleNamespace(task=None, tts=_TTS(), client_notes=SimpleNamespace(limits=lambda: {}))

    saved = (gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner, gs._tts_for)
    gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner = Transport, build, Runner
    gs._tts_for = _TTS
    ws = _WS(query)
    try:
        await gs.run_relay_bot(ws)
    finally:
        gs.FastAPIWebsocketTransport, gs.build_agent_session, gs.PipelineRunner, gs._tts_for = saved
    return ws, bool(ran)


def _request(state):
    class R:
        query_params, headers = {}, {}
        client = SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return {"state": state}
    return R()


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
    assert await gs.talk_call(_request("start")) == {"call": True, "yielded": 1}
    assert room_ws.closed == [(gs.YIELD_CLOSE_CODE, gs.YIELD_CLOSE_REASON)]
    assert gs.call_live()
    await gs.talk_call(_request("end"))
    assert not arb.ARBITER.held()


async def test_call_vs_talk_the_talk_user_is_told_then_the_call_has_the_engine():
    _fresh()
    talk_ws, talk, _ = await _holding("talk", client="openclaw:a")
    assert await gs.talk_call(_request("start")) == {"call": True, "yielded": 1}
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
    assert await gs.talk_call(_request("start")) == {"call": True, "yielded": 0}
    ws, ran = await _dial({"client": "openclaw:a", "voice": "ef_dora"})
    assert not ran
    assert _spoken(ws) == [arb.busy_line(arb.CALL, "es")]          # in the client's language
    assert _spoken(ws)[0] != arb.busy_line(arb.CALL, "en-us")
    assert ws.closed == [(gs.BUSY_CLOSE_CODE, "busy: a phone call")]
    assert gs.call_live()
    _fresh()


async def test_call_vs_room_awake_is_refused_for_now():
    _fresh()
    room_ws, room, _ = await _holding("room awake", client="local-audio")
    assert await gs.talk_call(_request("start")) == {"call": False, "busy": "room"}
    assert not room.task.cancelled and not gs.call_live() and gs._lease is None
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
    await gs.talk_call(_request("start"))
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
    saved = gs._tts_for
    gs._tts_for = Mute
    try:
        ws = _WS({"client": "openclaw:b"})
        await gs._refuse(ws, arb.ARBITER.would_refuse(arb.Claim(arb.TALK, client="b")),
                         speak=True, room=False)
    finally:
        gs._tts_for = saved
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

async def test_the_sip_brain_answers_a_refused_call_busy_and_holds_no_lease():
    from teaport_brain import sip_server
    told = []

    async def tell(state):
        told.append(state)
        return {"call": False, "busy": "room"}
    p = sip_server.CallPresence(tell=tell)
    assert await p.start() == "room"
    await p.end()                                    # nothing to end: never told "end"
    assert told == ["start"]


async def test_a_refused_session_says_busy_and_ends():
    from pipecat.processors.aggregators.llm_context import LLMContext
    from teaport_brain.agent_session import AgentSession
    task = _Task(_WS({}))
    s = AgentSession(task=task, context=LLMContext([]), stt=None, tts=_TTS(), llm=None,
                     ledger=None, followup_gate=None)
    await s.refuse("room")
    assert s.should_end and s.end_reason == "busy"
    assert [f.text for f in task.queued] == [arb.busy_line(arb.ROOM, "en-us")]


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
