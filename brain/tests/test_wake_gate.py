#
# The mic path's wake words on the brain side (wake_gate.py, wake_words.py): matching in
# any script, the privacy guarantee (nothing the room said before a wake word becomes a
# frame, a context message or a log line), the cut at the wake phrase, the mic
# conversation kept across short sleeps, the quiet STT failure, the phone call's yield
# of the engine, and the end_conversation tool's gating.
#
# Run: python test_wake_gate.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    InterimTranscriptionFrame,
    LLMRunFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from teaport_brain import wake_gate  # noqa: E402
from teaport_brain.endpointing import SegmentDoneFrame  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402
from teaport_brain.wake_words import find_wake, parse_phrases  # noqa: E402

ROOM = "the neighbours were arguing about money again"


class Recorder(TeaportSTTService):
    """Real _handle_message, frames captured instead of pushed."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _session(phrases="hey teaport, tea port", store=None, asleep=True):
    stt = Recorder()
    gate = wake_gate.WakeGate(parse_phrases(phrases), asleep=asleep, greeting="(greet me)",
                              store=store or wake_gate.MicConversation())
    gate.context = LLMContext([{"role": "system", "content": "persona"}])
    stt.wake_gate = gate
    return stt, gate


async def _say(stt, text, final=None):
    """The engine streams `text` word by word, then closes the segment with `final`."""
    for word in text.split():
        await stt._handle_message({"type": "transcription.delta", "delta": word + " "})
    await stt._handle_message({"type": "transcription.done",
                               "text": text if final is None else final})


def _texts(frames):
    return [f.text for f in frames if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))]


def _messages(frames):
    return [f.message for f in frames if isinstance(f, OutputTransportMessageUrgentFrame)]


# ---------------------------------------------------------------- matching

def test_matching_any_script_whole_words_no_fuzz():
    ph = parse_phrases("hey teaport, tea port，привет чайник、你好茶壶; Straße")
    assert ph == ["hey teaport", "tea port", "привет чайник", "你好茶壶", "strasse"]
    assert find_wake("So anyway. Hey Teaport, what's the weather?", ph) == \
        ("hey teaport", "what's the weather?")
    assert find_wake("Привет, чайник! Какая погода?", ph) == ("привет чайник", "Какая погода?")
    assert find_wake("嗯你好茶壶今天天气怎么样", ph) == ("你好茶壶", "今天天气怎么样")
    assert find_wake("hey tea-port: set a timer", ph) == ("tea port", "set a timer")
    assert find_wake("ＨＥＹ　ＴＥＡＰＯＲＴ", ph) == ("hey teaport", "")      # NFKC
    for miss in ("the teapot is hot", "teaports", "hey tea", "a heytea portrait"):
        assert find_wake(miss, ph) is None, miss                            # no fuzz
    assert parse_phrases(" , ;") == []


# ---------------------------------------------------------------- privacy

async def test_nothing_before_a_wake_word_becomes_a_frame_a_message_or_a_log():
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="TRACE")
    try:
        stt, gate = _session()
        before = list(gate.context.get_messages())
        await _say(stt, ROOM)
        await _say(stt, "", final=None)                      # a wordless close too
        await stt._handle_message({"type": "transcription.delta", "delta": "secret "})
        await stt._handle_message({"type": "transcription.done", "text": ""})  # interim fallback
    finally:
        logger.remove(sink)
    assert gate.asleep
    assert _texts(stt.pushed) == []                          # no interim, no final
    assert all(isinstance(f, SegmentDoneFrame) for f in stt.pushed), stt.pushed
    assert gate.context.get_messages() == before             # the LLM context untouched
    assert not any("neighbours" in ln or "secret" in ln for ln in lines), lines


async def test_the_wake_phrase_cuts_the_final_and_what_follows_is_the_first_turn():
    stt, gate = _session()
    await _say(stt, "money again. Hey Teaport, what's the weather")
    assert not gate.asleep
    assert _texts(stt.pushed) == ["what's the weather"]      # the room's half is dropped
    wake = _messages(stt.pushed)[0]
    assert wake["type"] == "wake" and wake["phrase"] == "hey teaport" and wake["greeted"]
    # A new conversation is greeted: the cue goes in before the turn; the turn runs it.
    assert gate.context.get_messages()[-1] == {"role": "user", "content": "(greet me)"}
    assert not any(isinstance(f, LLMRunFrame) for f in stt.pushed)
    # Awake, every word goes through, interims included.
    stt.pushed.clear()
    await _say(stt, "and tomorrow")
    assert _texts(stt.pushed)[-1] == "and tomorrow" and len(_texts(stt.pushed)) == 3


async def test_a_wake_word_alone_greets_with_a_run_of_its_own():
    stt, gate = _session()
    await _say(stt, "Hey teaport.")
    assert _texts(stt.pushed) == []
    assert any(isinstance(f, SegmentDoneFrame) for f in stt.pushed)
    assert any(isinstance(f, LLMRunFrame) for f in stt.pushed)


async def test_a_non_latin_wake_word_works():
    stt, gate = _session("привет чайник")
    await _say(stt, "ну и вот. Привет, чайник! Какая завтра погода?")
    assert not gate.asleep
    assert _texts(stt.pushed) == ["Какая завтра погода?"]


async def test_a_wake_word_said_again_while_awake_restarts_the_bridges_cap():
    stt, gate = _session(asleep=False)
    await _say(stt, "tea port, one more thing")
    assert _texts(stt.pushed)[-1] == "tea port, one more thing"   # nothing cut when awake
    assert _messages(stt.pushed) == [{"type": "wake", "phrase": "tea port", "again": True}]


# ---------------------------------------------------------------- the mic conversation

async def test_a_wake_within_the_window_continues_the_conversation_ungreeted():
    clock = Clock()
    store = wake_gate.MicConversation(clock=clock)
    stt, gate = _session(store=store)
    await _say(stt, "hey teaport what's the weather today")
    gate.context.add_message({"role": "user", "content": "what's the weather today"})
    gate.context.add_message({"role": "assistant", "content": "Sunny, 21 degrees."})
    gate.ended()                                    # the keep-alive ran out
    clock.t += 180                                  # three minutes asleep
    stt2, gate2 = _session(store=store)
    gate2.conversation_secs = 7200
    await _say(stt2, "hey teaport what about tomorrow")
    msgs = gate2.context.get_messages()
    assert msgs[0]["role"] == "system"              # its own system prompt, not the old one
    assert {"role": "assistant", "content": "Sunny, 21 degrees."} in msgs
    assert msgs.count({"role": "user", "content": "(greet me)"}) == 1   # the first time only
    wake = _messages(stt2.pushed)[0]
    assert wake["greeted"] is False and wake["resumed"] is True
    assert _texts(stt2.pushed) == ["what about tomorrow"]


async def test_a_wake_after_the_window_starts_fresh_and_greets():
    clock = Clock()
    store = wake_gate.MicConversation(clock=clock)
    stt, gate = _session(store=store)
    await _say(stt, "hey teaport hi")
    gate.context.add_message({"role": "assistant", "content": "an old answer"})
    gate.ended()
    clock.t += 7201
    stt2, gate2 = _session(store=store)
    await _say(stt2, "hey teaport hello")
    msgs = gate2.context.get_messages()
    assert {"role": "assistant", "content": "an old answer"} not in msgs
    assert msgs[-1] == {"role": "user", "content": "(greet me)"}
    assert _messages(stt2.pushed)[0]["greeted"] is True
    assert store.take(7200) is None                 # forgotten, not kept for later


async def test_a_session_that_never_woke_saves_nothing_and_the_store_is_capped():
    store = wake_gate.MicConversation()
    stt, gate = _session(store=store)
    gate.ended()
    assert store.take(7200) is None
    many = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 100}
            for i in range(200)]
    store.save([{"role": "system", "content": "persona"}, *many])
    kept = store.take(7200)
    assert len(kept) <= wake_gate.KEEP_MESSAGES and kept[0]["role"] == "user"
    assert all(m["role"] != "system" for m in kept)


def test_only_the_box_s_own_mic_shares_the_store():
    """gateway_server: ?features=local keeps the mic conversation; any other client gets
    a store of its own, so no client is ever handed another's context."""
    import inspect
    from teaport_brain import gateway_server
    src = inspect.getsource(gateway_server.run_relay_bot)
    assert 'wake_gate.MIC if "local" in features else wake_gate.MicConversation()' in src


# ---------------------------------------------------------------- failing closed

class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued += frames


class _Stt:
    stt_available = False
    slot_busy = True


async def test_a_wake_session_whose_stt_is_down_ends_quietly_and_never_greets():
    from teaport_brain.agent_session import AgentSession
    s = AgentSession(task=_Task(), context=LLMContext([]), stt=_Stt(), tts=None, llm=None,
                     ledger=None, followup_gate=None)
    await s.greet(speak=False, hello=False)
    assert s.should_end and s.end_reason == "busy"
    assert not any(isinstance(f, TTSSpeakFrame) for f in s.task.queued)   # the room hears nothing
    s2 = AgentSession(task=_Task(), context=LLMContext([]), stt=_Stt(), tts=None, llm=None,
                      ledger=None, followup_gate=None)
    await s2.greet()                                     # every other client: as before
    assert any(isinstance(f, TTSSpeakFrame) for f in s2.task.queued)


# ---------------------------------------------------------------- one conversation at a time

class _WS:
    def __init__(self):
        self.closed = []

    async def close(self, code=1000, reason=None):
        self.closed.append((code, reason))


class _PipelineTask:
    def __init__(self, ws, on_cancel=None):
        self.ws = ws
        self.cancelled = False
        self.on_cancel = on_cancel

    async def cancel(self):
        self.cancelled = True
        await self.ws.close()                          # what pipecat does as it goes
        if self.on_cancel:
            self.on_cancel()                           # its runner's finally: release


def _fresh_arbiter():
    """A clean arbiter for gateway_server; returns (gs, arb)."""
    from teaport_brain import gateway_server as gs
    from teaport_brain import session_arbiter as arb
    arb.ARBITER = arb.SessionArbiter()
    return gs, arb


async def _room_session(gs, arb, asleep: bool):
    """A room mic session holding the arbiter, as run_relay_bot registers one."""
    from types import SimpleNamespace
    ws = _WS()
    session = SimpleNamespace(task=None)
    claim = arb.Claim(arb.ROOM, client="local-audio", asleep=lambda: asleep,
                      end=lambda why: gs._end_for(ws, session, why, room=True, asleep=asleep))
    session.task = _PipelineTask(ws, on_cancel=lambda: arb.ARBITER.release(claim))
    assert await arb.ARBITER.acquire(claim) is None
    return ws, session


async def test_a_phone_call_closes_the_asleep_mic_session_and_holds_the_room_off():
    from teaport_brain import sip_server
    gs, arb = _fresh_arbiter()
    asleep_ws, asleep = await _room_session(gs, arb, asleep=True)
    try:
        call = sip_server.CallClaim()
        assert await call.start("c1") is None
        assert asleep.task.cancelled and asleep_ws.closed == [(gs.YIELD_CLOSE_CODE, gs.YIELD_CLOSE_REASON)]
        assert gs.call_live()
        status = await gs.talk_status(type("R", (), {"query_params": {}, "headers": {}})())
        assert status["call"] is True and status["holder"] == "call"
        call.end()
        call.end()                                     # twice: harmless
        assert not gs.call_live() and not arb.ARBITER.held()
    finally:
        _fresh_arbiter()


async def test_an_awake_room_conversation_keeps_the_box_and_the_call_is_refused():
    """For now: #58's take-the-call prompt replaces this refusal."""
    from teaport_brain import sip_server
    gs, arb = _fresh_arbiter()
    awake_ws, awake = await _room_session(gs, arb, asleep=False)
    try:
        call = sip_server.CallClaim()
        assert await call.start("c1") == "room"
        assert not awake.task.cancelled and awake_ws.closed == []
        assert not gs.call_live()
        call.end()                                     # nothing held: harmless
        assert arb.ARBITER.holder is not None and arb.ARBITER.holder.kind == arb.ROOM
    finally:
        _fresh_arbiter()


async def test_an_asleep_session_is_refused_while_a_call_is_live():
    gs, arb = _fresh_arbiter()

    class WS(_WS):
        query_params = {"wake": "hey teaport", "features": "local,sleep"}
    ws = WS()
    assert await arb.ARBITER.acquire(arb.Claim(arb.CALL, client="sip")) is None
    try:
        await gs.run_relay_bot(ws)
    finally:
        _fresh_arbiter()
    assert ws.closed == [(gs.YIELD_CLOSE_CODE, gs.YIELD_CLOSE_REASON)]


# ---------------------------------------------------------------- end_conversation

# ---------------------------------------------------------------- review fixes (#105)

class TimedRecorder(Recorder):
    """A Recorder whose timers run (the stranded-segment backstop needs them)."""

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    async def cancel_task(self, task, timeout=None):
        task.cancel()


async def test_the_backstop_never_quotes_the_room_and_still_watches_an_asleep_segment():
    """A held segment the room resumes (VAD stop, then start) re-arms the backstop off
    the asleep deltas; when it fires it names a count, not the words."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection
    from teaport_brain import stt as stt_mod
    old = stt_mod._STRANDED_INTERIM_SECS
    stt_mod._STRANDED_INTERIM_SECS = 0.2
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="TRACE")
    try:
        stt = TimedRecorder()
        gate = wake_gate.WakeGate(parse_phrases("hey teaport"), store=wake_gate.MicConversation())
        gate.context = LLMContext([])
        stt.wake_gate = gate
        for w in ROOM.split():
            await stt._handle_message({"type": "transcription.delta", "delta": w + " "})
        stt._commit_pending = True                    # verdict mode: the stop's hold
        await stt.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for w in "and the rent".split():
            await stt._handle_message({"type": "transcription.delta", "delta": w + " "})
        await asyncio.sleep(0.4)
    finally:
        stt_mod._STRANDED_INTERIM_SECS = old
        logger.remove(sink)
    fired = [ln for ln in lines if "no final after" in ln]
    assert fired and "chars" in fired[0]               # it fired, counting
    assert not any("neighbours" in ln or "rent" in ln for ln in lines), fired
    assert _texts(stt.pushed) == []


async def test_the_wake_final_s_result_holds_only_what_is_kept():
    stt, gate = _session()
    await _say(stt, "money again. Hey Teaport, what's the weather")
    final = [f for f in stt.pushed if isinstance(f, TranscriptionFrame)][0]
    assert final.text == "what's the weather"
    assert final.result["text"] == "what's the weather" and "money" not in str(final.result)


async def test_a_wake_phrase_split_across_two_finals_still_wakes_and_cuts_clean():
    clock = Clock()
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="TRACE")
    try:
        stt, gate = _session()
        gate._clock = clock
        await _say(stt, "the money again. Hey.")          # the STT closed mid-phrase
        assert gate.asleep and _texts(stt.pushed) == []
        clock.t += 1.0
        await _say(stt, "Teaport, what's the weather")
    finally:
        logger.remove(sink)
    assert not gate.asleep
    assert _texts(stt.pushed) == ["what's the weather"]  # no "money", no "hey"
    assert not any("money" in ln for ln in lines)
    # Not across a pause: the carried words are gone after CARRY_SECS.
    stt, gate = _session()
    gate._clock = clock
    await _say(stt, "Hey.")
    clock.t += wake_gate.CARRY_SECS + 0.1
    await _say(stt, "Teaport, what's the weather")
    assert gate.asleep and _texts(stt.pushed) == []
    # A script without spaces, split mid-phrase.
    stt, gate = _session("你好茶壶")
    gate._clock = clock
    await _say(stt, "嗯你好")
    await _say(stt, "茶壶今天天气怎么样")
    assert _texts(stt.pushed) == ["今天天气怎么样"]
    # The carry is the phrase's own length at most: nothing more of the room is held.
    from teaport_brain.wake_words import carry_tail
    assert carry_tail("the neighbours were arguing hey", parse_phrases("hey teaport")) == "hey"
    assert carry_tail("anything at all", parse_phrases("computer")) == ""


async def test_a_call_that_lands_while_a_wake_session_sets_up_never_meets_its_stt():
    """The arbiter decides again after the session is built: a call that took the engine
    in between refuses it (4002) before its pipeline runs."""
    gs, arb = _fresh_arbiter()
    seen = {}

    class WS(_WS):
        query_params = {"wake": "hey teaport", "features": "local,sleep"}

        async def send_text(self, text):
            seen["hello"] = __import__("json").loads(text)
            arb.ARBITER._holder = arb.Claim(arb.CALL, client="sip")   # the call lands now

    class Runner:
        def __init__(self, **kw):
            pass

        async def run(self, task):
            raise AssertionError("the pipeline ran under a live call")

    saved = gs.PipelineRunner
    gs.PipelineRunner = Runner
    ws = WS()
    try:
        await gs.run_relay_bot(ws)
    finally:
        gs.PipelineRunner = saved
        _fresh_arbiter()
    assert seen["hello"]["wake"] == "asleep"            # the bridge may send the room now
    assert ws.closed[-1][0] == gs.YIELD_CLOSE_CODE


async def test_a_pending_consult_is_announced_to_the_client_until_it_is_delivered():
    from teaport_brain import tools
    pushed, delivered = [], []

    class LLM:
        async def push_frame(self, frame, direction=None):
            pushed.append(frame)

    async def followup(request, text, tool_call_id, failure=None, detail=None):
        delivered.append(text)
        assert [m["type"] for m in _messages(pushed)] == ["working"]   # still owed here

    fut = asyncio.get_running_loop().create_future()
    fut.set_result({"ok": True, "text": "the answer"})
    await tools._consult_and_followup("call-1", fut, "a long question", followup, "tc-1",
                                      llm=LLM())
    working = [m for m in _messages(pushed) if m["type"] == "working"]
    assert working[0]["call_id"] == "call-1" and working[0]["secs"] > tools._ASYNC_CONSULT_TIMEOUT
    assert working[-1] == {"type": "working", "call_id": "call-1", "done": True}
    assert delivered


def test_end_conversation_is_offered_only_to_a_client_that_can_sleep():
    from teaport_brain.tools import CLIENT_FEATURES, ToolContext, active_tools
    assert "sleep" in CLIENT_FEATURES
    names = lambda feats: {t.name for t in active_tools(ToolContext(client_features=frozenset(feats)))}
    assert "end_conversation" not in names({"volume", "restart", "local"})
    assert "end_conversation" in names({"volume", "restart", "local", "sleep"})


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
