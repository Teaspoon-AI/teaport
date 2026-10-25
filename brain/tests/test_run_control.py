#
# Unit test: utterances OpenClaw's spoken run control takes (#230, run_control.py).
#
# While a consult runs on a Talk session, OpenClaw's relay acts on "cancel that", "how's
# it going?" and the like itself, and answers them through the provider. The brain's own
# model must not answer the same words too. What is asserted, and why each one matters:
#
#   * Nothing is held without a consult running, nor an empty or already-taken final:
#     every other turn ends exactly as before, with no wait.
#   * With one running, the final waits for the plugin's answer, and so does every STT
#     frame behind it, in order: a segment close that overtook its words would end the
#     turn without them.
#   * Claimed, the final goes on taken (emptied and marked), so TakenTurnCloser ends the
#     turn with nothing to answer, and the words reach the context as a notice.
#     Not claimed, or no answer in time, the frames go on unchanged.
#   * A stale answer changes nothing; the session's end lets what is held go first.
#   * A second final held behind the first is asked about in its turn, not let through
#     with the first's answer: the relay reads every final.
#   * The brain waits longer than the plugin's own window for the relay to act.
#   * The model's openclaw_agent_control (tools.py): the call goes out as the relay's
#     tool_call with OpenClaw's arguments, its result comes back for its own call id and
#     is said in plain words, never read as a consult's; no answer in time is an honest
#     "couldn't be confirmed" (the steer may have acted, its result held), and no
#     display card runs it a second time. The typing bed is not heard while it waits.
#   * A consult the brain gives up on is cancelled at the relay (tool_cancel), and a
#     cancelled agent-first consult is not answered a second time.
#   * The wire: the question goes out as {"type":"run_control"}, the answer comes back as
#     a RunControlResultFrame; a session built with run_control places it directly below
#     the transcript emitter with the closer in its stop strategies, and /talk builds it
#     only for a plugin that announced ?run_control=1.
#
# Run: python test_run_control.py   (hermetic)
#
import asyncio
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    EndFrame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import consult_bridge, run_control, tools  # noqa: E402
from teaport_brain.captions import UserTranscriptEmitter  # noqa: E402
from teaport_brain.endpointing import SegmentDoneFrame  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.run_control import RunControlClaims, RunControlResultFrame  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402
from teaport_brain.taken_turn import TakenTurnCloser, is_taken, taken  # noqa: E402


def _final(text, stop_n=1):
    return FinalTranscriptionFrame(text, "u", "t", None, finalized=True, stop_n=stop_n)


class _Notes:
    def __init__(self):
        self.notices = []

    def add_notice(self, text):
        self.notices.append(text)


def _harness(consults=1, wait_secs=0.3):
    running = {"n": consults}
    notes = _Notes()
    claims = RunControlClaims(lambda: running["n"], notes=notes, wait_secs=wait_secs)
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    claims.push_frame = push
    claims.create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    return SimpleNamespace(claims=claims, pushed=pushed, notes=notes, running=running)


async def _in(h, frame):
    await h.claims.process_frame(frame, FrameDirection.DOWNSTREAM)


def _questions(h):
    return [f.message for f in h.pushed if isinstance(f, OutputTransportMessageUrgentFrame)]


def _passed(h):
    return [f for f in h.pushed if not isinstance(f, OutputTransportMessageUrgentFrame)]


async def test_nothing_is_held_without_a_consult_running():
    h = _harness(consults=0)
    final = _final("cancel that")
    await _in(h, final)
    assert h.pushed == [final] and _questions(h) == []


async def test_an_empty_or_taken_final_is_not_asked_about():
    h = _harness()
    for frame in (_final("  "), taken(_final("set up wifi"))):
        await _in(h, frame)
    assert _questions(h) == [] and len(h.pushed) == 2


async def test_a_claimed_final_ends_its_turn_unanswered_and_leaves_a_notice():
    h = _harness()
    final, close = _final(" cancel that", stop_n=4), SegmentDoneFrame(stop_n=5)
    interim = InterimTranscriptionFrame("and", "u", "t")
    await _in(h, final)
    await _in(h, close)
    await _in(h, interim)
    assert _questions(h) == [{"type": "run_control", "id": "r1", "text": "cancel that"}]
    assert _passed(h) == [], "a frame went on while its final was being asked about"
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    out = _passed(h)
    assert len(out) == 3 and out[1:] == [close, interim], out
    assert is_taken(out[0]) and out[0].text == "" and out[0].stop_n == 4, out[0]
    assert type(out[0]) is FinalTranscriptionFrame, "the final lost its uninterruptible class"
    assert h.notes.notices == [
        'The user said "cancel that" about the request running in the background, and '
        "OpenClaw took it: it acts on it and answers them itself."], h.notes.notices
    assert h.claims.claimed == 1
    # The next final is asked about afresh.
    await _in(h, _final("what's the weather"))
    assert _questions(h)[-1]["id"] == "r2"
    await _in(h, RunControlResultFrame(request_id="r2", claimed=False))


async def test_a_final_not_claimed_goes_on_unchanged():
    h = _harness()
    final, close = _final("what's the weather tomorrow?"), SegmentDoneFrame(stop_n=2)
    await _in(h, final)
    await _in(h, close)
    await _in(h, RunControlResultFrame(request_id="r1", claimed=False))
    assert _passed(h) == [final, close] and h.notes.notices == []
    assert h.claims.passed == 1


async def test_no_answer_in_time_lets_the_turn_go_on():
    h = _harness(wait_secs=0.05)
    final = _final("cancel that")
    await _in(h, final)
    await asyncio.sleep(0.15)
    assert _passed(h) == [final] and h.claims.unanswered == 1
    # A late answer to it changes nothing.
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    assert _passed(h) == [final] and h.notes.notices == []


async def test_a_stale_answer_is_ignored():
    h = _harness()
    final = _final("never mind")
    await _in(h, final)
    await _in(h, RunControlResultFrame(request_id="r9", claimed=True))
    assert _passed(h) == []
    await _in(h, RunControlResultFrame(request_id="r1", claimed=False))
    assert _passed(h) == [final]


async def test_the_sessions_end_lets_what_is_held_go_first():
    h = _harness()
    final = _final("how's it going")
    await _in(h, final)
    end = EndFrame()
    await _in(h, end)
    assert _passed(h) == [final, end]


async def test_the_serializer_turns_the_answer_into_a_frame():
    s = TeaportGatewaySerializer()
    f = await s.deserialize(json.dumps({"type": "run_control_result", "id": "r3",
                                        "claimed": True}))
    assert isinstance(f, RunControlResultFrame) and (f.request_id, f.claimed) == ("r3", True)
    f = await s.deserialize(json.dumps({"type": "run_control_result", "id": 4,
                                        "claimed": "yes"}))
    assert (f.request_id, f.claimed) == ("4", False), "only a true claims"


async def test_the_session_places_it_below_the_emitter_with_the_closer():
    from teaport_brain.agent_session import build_agent_session
    from test_client_notes import _Transport

    def inner(session):
        return list(next(p for p in session.task.pipeline.processors
                         if isinstance(p, Pipeline)).processors)

    def stops(procs):
        agg = next(p for p in procs if isinstance(p, LLMUserAggregator))
        return agg._user_turn_controller.user_turn_strategies.stop

    plain = inner(build_agent_session(_Transport(), context_notes=True))
    assert not any(isinstance(p, RunControlClaims) for p in plain)
    assert not any(isinstance(s, TakenTurnCloser) for s in stops(plain))

    session = build_agent_session(_Transport(), context_notes=True, run_control=True)
    procs = inner(session)
    at = next(i for i, p in enumerate(procs) if isinstance(p, RunControlClaims))
    assert isinstance(procs[at - 1], UserTranscriptEmitter), "not directly below the emitter"
    agg = next(i for i, p in enumerate(procs) if isinstance(p, LLMUserAggregator))
    assert at < agg
    assert procs[at].notes is session.client_notes is not None
    assert isinstance(stops(procs)[0], TakenTurnCloser), "the closer must come first"
    # It counts the consults the session's tools count (tools._consult_and_land).
    session.followup_gate.consults_running = 1
    assert procs[at]._consults() == 1


async def test_talk_builds_it_only_for_a_plugin_that_answers():
    from test_client_notes import _run_talk

    _, built = await _run_talk(query={"run_control": "1"})
    assert built.get("run_control") is True
    _, built = await _run_talk()
    assert built.get("run_control") is False


async def test_a_second_final_held_behind_the_first_is_asked_about_in_its_turn():
    h = _harness()
    first, close = _final("cancel that", stop_n=1), SegmentDoneFrame(stop_n=2)
    second, after = _final("how much longer", stop_n=3), SegmentDoneFrame(stop_n=4)
    for f in (first, close, second, after):
        await _in(h, f)
    assert [q["id"] for q in _questions(h)] == ["r1"], "only the first is asked so far"
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    # The first goes on taken, with what came before the second; the second is asked.
    out = _passed(h)
    assert len(out) == 2 and is_taken(out[0]) and out[1] is close, out
    assert _questions(h)[-1] == {"type": "run_control", "id": "r2", "text": "how much longer"}
    await _in(h, RunControlResultFrame(request_id="r2", claimed=False))
    assert _passed(h)[2:] == [second, after]
    assert (h.claims.claimed, h.claims.passed) == (1, 1)


async def test_a_second_final_goes_on_unasked_once_no_consult_runs():
    h = _harness()
    first, second = _final("cancel that"), _final("thanks")
    await _in(h, first)
    await _in(h, second)
    h.running["n"] = 0  # the cancel closed the consult before the answer came
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    out = _passed(h)
    assert len(out) == 2 and is_taken(out[0]) and out[1] is second
    assert len(_questions(h)) == 1


async def test_at_the_sessions_end_a_held_second_final_goes_unasked():
    h = _harness()
    first, second, end = _final("cancel that"), _final("stop"), EndFrame()
    for f in (first, second, end):
        await _in(h, f)
    assert _passed(h) == [first, second, end] and len(_questions(h)) == 1


def test_the_brain_outwaits_the_plugins_window():
    """The plugin answers within CONFIRM_MS (2 s, plugin/provider.js) for words the relay
    might act on; the brain's own wait only bounds a plugin that is gone."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "plugin", "provider.js"), encoding="utf-8").read()
    confirm_ms = int(src.split("export const CONFIRM_MS = ", 1)[1].split(";", 1)[0])
    assert run_control.CLAIM_WAIT_S * 1000 > confirm_ms + 250, (run_control.CLAIM_WAIT_S,
                                                                 confirm_ms)


# ---------------------------------------------------------------- the model's control

class _LLM:
    """What a tool handler pushes, and the plugin answering on the wire: `answer(msg)`
    gives the tool_result for a tool_call (None: no answer)."""

    def __init__(self, answer=None):
        self.pushed = []
        self.answer = answer
        self.tasks = []

    def create_task(self, coro, *a, **k):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)
        msg = getattr(frame, "message", None)
        if self.answer and isinstance(msg, dict) and msg.get("type") == "tool_call":
            result = self.answer(msg)
            if result is not None:
                asyncio.get_running_loop().call_soon(consult_bridge.resolve, msg["call_id"],
                                                     result)

    def messages(self, kind):
        return [f.message for f in self.pushed
                if isinstance(getattr(f, "message", None), dict) and f.message.get("type") == kind]


def _params(llm, arguments, call_id="fc_1"):
    got = []

    async def result_callback(result, **kw):
        got.append((result, kw))

    return SimpleNamespace(llm=llm, arguments=arguments, tool_call_id=call_id,
                           result_callback=result_callback), got


# The Control UI's talk.session.steer result for a cancel (RealtimeVoiceAgentControlResult).
_CANCELLED = {"ok": True, "mode": "cancel", "sessionKey": "agent:main:talk-x", "sessionId": "s1",
              "active": True, "aborted": True, "message": "Cancelled the active OpenClaw run.",
              "speak": True, "show": True, "suppress": False,
              "providerResult": {"status": "cancelled",
                                 "message": "Cancelled the active OpenClaw run."}}


async def test_a_control_call_goes_to_the_relay_and_its_result_is_said_in_plain_words():
    llm = _LLM(answer=lambda msg: _CANCELLED)
    params, got = _params(llm, {"text": " scratch that ", "mode": "Cancel", "extra": 1})
    await tools._openclaw_agent_control(params)
    [call] = llm.messages("tool_call")
    assert call["name"] == "openclaw_agent_control"
    assert call["args"] == {"text": "scratch that", "mode": "cancel"}, "OpenClaw's arguments"
    assert call["call_id"].startswith(tools.CONTROL_CALL_PREFIX)
    [(result, kw)] = got
    assert kw == {}, "the model says it: inference runs"
    assert result["ok"] is True and result["mode"] == "cancel"
    assert result["update"] == "Cancelled the active OpenClaw run."
    assert "Never say OpenClaw, run, or the name of any tool" in result["instruction"]
    for leak in ("sessionKey", "sessionId", "providerResult"):
        assert leak not in result, leak
    assert call["call_id"] not in consult_bridge._pending, "the call's future is gone"


async def test_a_control_with_nothing_in_progress_leaves_a_new_request_to_be_done():
    """OpenClaw answers a control with no run active:false ("There is no active OpenClaw
    run to cancel."). The words may have been a new request ("cancel my two o'clock"): the
    model is told nothing changed and to do it, not to report "nothing to cancel"."""
    none = {"ok": False, "mode": "cancel", "sessionKey": "k", "active": False, "aborted": False,
            "reason": "no_active_run", "message": "There is no active OpenClaw run to cancel.",
            "speak": True, "show": True, "suppress": False}
    out = tools._control_result(none, None)
    assert out["nothing_in_progress"] is True and out["ok"] is False
    assert "call openclaw_agent_consult for it now" in out["instruction"]
    assert "never say it is done" in out["instruction"]
    assert "nothing_in_progress" not in tools._control_result(_CANCELLED, None)


async def test_a_control_result_never_resolves_a_consult():
    """The relay cancels a consult by submitting {"status": "cancelled"} for the consult's
    own call; the control's result comes for the control's call. Neither touches the other."""
    consult = consult_bridge.create(consult_bridge.CALL_ID_PREFIX + "c1")
    llm = _LLM(answer=lambda msg: _CANCELLED)
    params, got = _params(llm, {"text": "olvídalo", "mode": "cancel"})
    await tools._openclaw_agent_control(params)
    assert not consult.done(), "a control's result was taken for the consult's"
    assert got[0][0]["ok"] is True
    consult_bridge.resolve(consult_bridge.CALL_ID_PREFIX + "c1",
                           _CANCELLED["providerResult"])
    assert consult_bridge.classify(consult.result())[1] == "cancelled"


async def test_a_control_with_no_answer_says_it_could_not_be_confirmed():
    """No result in time: the Control UI runs the steer first and may hold its result
    while audio plays, so it may have gone through -- "couldn't be confirmed", never
    "nothing changed". A client that refused it did change nothing."""
    monkeypatch_wait = tools.CONTROL_TIMEOUT_S
    tools.CONTROL_TIMEOUT_S = 0.05
    try:
        for answer, said in ((lambda msg: None, "couldn't be confirmed whether that went"),
                             (lambda msg: {"error": "text required"}, "could not be reached")):
            llm = _LLM(answer=answer)
            params, got = _params(llm, {"text": "how much longer?", "mode": "status"})
            await tools._openclaw_agent_control(params)
            [(result, kw)] = got
            assert result["ok"] is False and said in result["update"], result
            assert "OpenClaw" not in result["update"]
        assert "nothing" not in tools._CONTROL_UNCONFIRMED
    finally:
        tools.CONTROL_TIMEOUT_S = monkeypatch_wait
    # No text: nothing is sent.
    llm = _LLM()
    params, got = _params(llm, {"mode": "status"})
    await tools._openclaw_agent_control(params)
    assert llm.pushed == [] and got[0][0] == {"ok": False, "error": "text required"}


async def test_a_control_cancelled_by_a_barge_in_still_tells_what_it_did():
    """The Control UI runs the steer first and the result comes later; a barge-in cancels
    the call meanwhile. The result is still waited for, off the turn, and said as the
    host's update when something changed; nothing is said when nothing did, or when no
    result comes in the call's time."""
    class Notes:
        def __init__(self):
            self.said = []

        def add_host_update(self, text):
            self.said.append(text)

    none = {"ok": False, "mode": "cancel", "active": False,
            "message": "There is no active OpenClaw run to cancel."}
    saved = tools.CONTROL_TIMEOUT_S
    tools.CONTROL_TIMEOUT_S = 0.3
    try:
        for late, said in ((_CANCELLED, [_CANCELLED["message"]]), (none, []), (None, [])):
            notes, llm = Notes(), _LLM()
            params, got = _params(llm, {"text": "scratch that", "mode": "cancel"})
            task = asyncio.ensure_future(tools._openclaw_agent_control(params, notes=notes))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            [call] = llm.messages("tool_call")
            assert got == [], "a cancelled call gets no result"
            if late is not None:
                consult_bridge.resolve(call["call_id"], late)
            await asyncio.wait_for(asyncio.gather(*llm.tasks), 2)
            assert notes.said == said, (late, notes.said)
            assert call["call_id"] not in consult_bridge._pending
    finally:
        tools.CONTROL_TIMEOUT_S = saved


async def test_no_display_card_runs_a_host_run_tool_a_second_time():
    """tools._wrap sends a display tool_call for every tool; the relay runs every consult
    and the Control UI every control call it is shown, so theirs must not go."""
    for name in tools.HOST_RUN_TOOLS:
        llm = _LLM()
        llm._teaport_spoke = True

        async def handler(params):
            await params.result_callback({"ok": True})

        params, _ = _params(llm, {"text": "x", "mode": "status"})
        await tools._wrap(name, handler, lambda: "en")(params)
        assert llm.messages("tool_call") == [], name
    llm = _LLM()
    llm._teaport_spoke = True
    params, _ = _params(llm, {})
    await tools._wrap("get_current_time", handler, lambda: "en")(params)
    assert len(llm.messages("tool_call")) == 1, "other tools keep their card"


async def test_control_is_offered_on_talk_only_and_only_with_the_consult():
    def names(**kw):
        return {t.name for t in tools.active_tools(tools.ToolContext(has_tts=True, **kw))}

    if not tools.HAS_AGENT:
        assert "openclaw_agent_control" not in names(run_control=True)
        return
    assert "openclaw_agent_control" in names(run_control=True)
    assert "openclaw_agent_control" not in names()
    import dataclasses
    saved = tools.TOOLS
    tools.TOOLS = tuple(dataclasses.replace(t, enabled=False) if t.name ==
                        "openclaw_agent_consult" else t for t in saved)
    try:
        assert "openclaw_agent_control" not in names(run_control=True)
    finally:
        tools.TOOLS = saved
    control = tools.TOOLS_BY_NAME["openclaw_agent_control"]
    assert control.direct, "agent-first lets the model call it itself"
    assert control.timeout_secs > tools.CONTROL_TIMEOUT_S


async def test_a_cancel_through_the_control_closes_the_consult_silently_end_to_end():
    """The model's control cancels a consult in flight. What the relay does then
    (2026.9.7): the Control UI runs talk.session.steer, which aborts the run and submits
    {"status": "cancelled"} as the consult's own final result
    (submitRelayAgentControlProviderResults), then submits the control's result for the
    control's call. On the brain: the owed record closes silently (no record, no run, its
    placeholder says it was stopped), the control's result is said, and nothing is
    cancelled at the relay a second time."""
    from test_owed import book, tool_round

    b, ctx, gate = book(msgs=tool_round("tc-a", request="flights to Paris on Friday"))
    consult_ids = []

    def relay(msg):
        if msg["name"] == "openclaw_agent_consult":
            consult_ids.append(msg["call_id"])
            consult_bridge.resolve(msg["call_id"], {"status": "working"}, will_continue=True)
            return None
        # The control: the consult's cancelled result first, then the control's own.
        for cid in consult_ids:
            consult_bridge.resolve(cid, _CANCELLED["providerResult"])
        return _CANCELLED

    llm = _LLM(answer=relay)
    params, got = _params(llm, {"question": "flights to Paris on Friday"}, call_id="tc-a")
    await tools._openclaw_agent_consult(params, owed=b)
    assert got[0][0]["status"] == "working_in_background"
    await asyncio.sleep(0)
    params, said = _params(llm, {"text": "scratch that", "mode": "cancel"}, call_id="tc-b")
    await tools._openclaw_agent_control(params)
    await asyncio.wait_for(asyncio.gather(*llm.tasks), 2)
    assert said[0][0]["ok"] is True and said[0][0]["mode"] == "cancel"
    assert b.owed() == [] and b.record("tc-a") is None and gate.asked == []
    tool = json.loads(ctx.get_messages()[-1]["content"])
    assert tool["status"] == "cancelled", tool
    assert llm.messages("tool_cancel") == [], "the relay already stopped it"
    # The client was told the answer is no longer owed, and that it got none.
    assert llm.messages("working")[-1]["done"] is True
    assert llm.messages("working")[-1]["answered"] is False


# ---------------------------------------------------------------- consults that end

async def test_a_consult_the_brain_gives_up_on_is_cancelled_at_the_relay():
    llm = _LLM()
    saved = (tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT)
    tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT = 0.02, 0.1
    landed = []

    class Owed:
        def land(self, request, text, key, *, failure=None, detail=""):
            landed.append(failure)

    try:
        fut = consult_bridge.create("teaport-consult-gu")
        fut.working = True  # the relay acknowledged it
        await tools._consult_and_land("teaport-consult-gu", fut, "q", Owed(), "tc", llm=llm)
        assert llm.messages("tool_cancel") == [{"type": "tool_cancel",
                                                "call_id": "teaport-consult-gu"}]
        assert landed == ["timeout"]
        # One the relay never acknowledged is the CLI lane's: nothing to cancel there.
        llm2 = _LLM()

        async def cli(message, timeout=None):
            from teaport_brain import openclaw_client as oc
            return oc.ConsultOutcome("cli answer")

        saved_cli = tools.oc.agent_consult
        tools.oc.agent_consult = cli
        try:
            fut = consult_bridge.create("teaport-consult-cl")
            await tools._consult_and_land("teaport-consult-cl", fut, "q", Owed(), "tc", llm=llm2)
        finally:
            tools.oc.agent_consult = saved_cli
        assert llm2.messages("tool_cancel") == []
    finally:
        tools._NATIVE_CONSULT_ACK_TIMEOUT, tools._ASYNC_CONSULT_TIMEOUT = saved


async def test_a_cancelled_agent_first_consult_is_not_answered_twice():
    """Sync (agent-first) consult: the cancellation is already said, by the relay's host
    note or the model's control call, so the cancelled result runs no inference."""
    def answer(msg):
        if msg["name"] != "openclaw_agent_consult":
            return None
        consult_bridge.resolve(msg["call_id"], {"status": "working"}, will_continue=True)
        return {"status": "cancelled", "message": "Cancelled the active OpenClaw run."}

    llm = _LLM(answer=answer)
    params, got = _params(llm, {"question": "flights to Paris"})
    await tools._openclaw_agent_consult(params)
    [(result, kw)] = got
    assert result["status"] == "cancelled"
    assert kw["properties"].run_llm is False



async def test_an_agent_first_consult_counts_as_running_while_the_turn_waits():
    """The relay acts on words about an agent-first consult as on an async one's, so the
    brain asks the plugin about finals while it runs (consults_running), and stops when
    it ends, however it ends. A barge-in that cancels the wait leaves the relay's run
    alone, as it leaves an async consult's."""
    gate = SimpleNamespace(consults_running=0)
    seen = []

    def answer(msg):
        if msg["name"] == "openclaw_agent_consult":
            # (read as the answer comes in, the turn waiting on it)
            asyncio.get_running_loop().call_soon(
                lambda: seen.append(gate.consults_running))
            return {"text": "It is sunny."}
        return None

    llm = _LLM(answer=answer)
    params, got = _params(llm, {"question": "weather in Rome"})
    await tools._bind_openclaw_agent_consult(tools.ToolContext(gate=gate))(params)
    assert seen == [1] and gate.consults_running == 0
    assert got[0][0] == {"answer": "It is sunny."}
    # A barge-in cancels the wait: the count goes back, and the relay is not told.
    llm = _LLM()
    params, got = _params(llm, {"question": "weather in Oslo"})
    task = asyncio.ensure_future(tools._openclaw_agent_consult(params, gate=gate))
    await asyncio.sleep(0.05)
    assert gate.consults_running == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    [call] = llm.messages("tool_call")
    assert gate.consults_running == 0 and got == []
    assert call and llm.messages("tool_cancel") == []

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
