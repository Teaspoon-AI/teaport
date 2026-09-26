#
# Unit test: client context notes (issue #71) — a Talk client's text note reaches the
# LLM context at a turn boundary, never mid-turn, and is never spoken or captioned.
#
# What is asserted, and why each one matters live:
#
#   * The wire: {"type":"context"} parses to a ClientContextFrame and every note is
#     answered with a context_result, so the plugin's RPC never hangs on a refusal.
#   * Limits refuse rather than queue: empty, too long, a reaction inside the respond
#     interval, a burst past the allowance.
#   * A note that arrives while a turn is in flight is inserted just BEFORE the message
#     that turn answers. Appended after it, the note would become the tail and the model
#     would answer the note instead of the user.
#   * A reaction is its own turn only at a quiet moment; its "react now" line is retired
#     at the completion that reads it (FollowupTrigger), and a reaction still waiting
#     when the user takes a turn is answered by that turn instead of by a second reply.
#   * The retention cap removes the oldest notes WITHOUT moving HeardContextCorrector's
#     window: a removal below its mark would push a cut reply out of the window and
#     leave the unheard words in the context.
#   * build_agent_session places ClientNotes between the corrector and the LLM, and a
#     ClientContextFrame gets there through every processor above it.
#
# Run: python test_client_notes.py   (hermetic)
#
import asyncio
import json
import os
import sys
from types import SimpleNamespace

# Before teaport_brain.services is imported anywhere (see test_service_unusable).
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMContextFrame,
    LLMRunFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from teaport_brain.client_notes import ClientContextFrame, ClientNotes  # noqa: E402
from teaport_brain.followup_gate import FollowupGate, FollowupTrigger  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.transcript_ledger import Utterance  # noqa: E402

TAG = "[app event, not spoken by the user]"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


def _harness(msgs=None, **kw):
    """ClientNotes over a real context, gate and trigger, with push_frame recorded and
    tasks run on the test loop (there is no pipeline TaskManager here)."""
    context = LLMContext(msgs if msgs is not None else
                         [{"role": "system", "content": "persona"}])
    gate = FollowupGate(quiet_secs=0.02, max_wait=0.3)
    trigger = FollowupTrigger()
    clock = _Clock()
    notes = ClientNotes(context, gate, trigger, clock=clock, **kw)
    notes.task = _Task()
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    async def ignore(frame, direction=FrameDirection.DOWNSTREAM):
        pass

    notes.push_frame = push
    gate.push_frame = ignore
    trigger.push_frame = ignore
    notes.create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    return SimpleNamespace(notes=notes, context=context, gate=gate, trigger=trigger,
                           clock=clock, pushed=pushed, task=notes.task)


async def _send(h, text, respond=False, rid="c1", kind=None):
    await h.notes.process_frame(
        ClientContextFrame(text=text, respond=respond, kind=kind, request_id=rid),
        FrameDirection.DOWNSTREAM)


def _results(h):
    return [f.message for f in h.pushed if isinstance(f, OutputTransportMessageUrgentFrame)]


async def _settle(h):
    """Let the drainer find its quiet moment (quiet_secs is 0.02 here)."""
    for _ in range(20):
        await asyncio.sleep(0.02)
        if not h.notes._pending and (h.notes._drainer is None or h.notes._drainer.done()):
            return


def _notes_in(context):
    return [m for m in context.get_messages() if str(m.get("content", "")).startswith(TAG)]


# ---- the wire ------------------------------------------------------------------

async def test_the_serializer_turns_a_context_message_into_a_frame():
    s = TeaportGatewaySerializer()
    f = await s.deserialize(json.dumps({"type": "context", "id": "c7", "text": "tap",
                                        "respond": True, "kind": "ui-event"}))
    assert isinstance(f, ClientContextFrame)
    assert (f.text, f.respond, f.kind, f.request_id) == ("tap", True, "ui-event", "c7")
    # Loosely typed on purpose: ClientNotes judges it and answers, so nothing is
    # dropped here where the plugin would wait out its ack timeout.
    f = await s.deserialize(json.dumps({"type": "context", "id": 3, "text": 5,
                                        "respond": "yes"}))
    assert isinstance(f, ClientContextFrame)
    assert (f.text, f.respond, f.request_id) == ("", False, "3")


async def test_every_refusal_is_answered():
    h = _harness(max_chars=10, respond_interval_s=15.0)
    await _send(h, "   ", rid="e")
    await _send(h, "x" * 11, rid="l")
    await _send(h, "tap", respond=True, rid="ok")
    h.clock.t += 5
    await _send(h, "tap again", respond=True, rid="soon")
    got = {r["id"]: r for r in _results(h)}
    assert got["e"]["ok"] is False and got["e"]["error"] == "empty"
    assert got["l"]["ok"] is False and got["l"]["error"] == "too_long"
    assert got["ok"]["ok"] is True
    assert got["soon"]["ok"] is False and got["soon"]["error"] == "rate_limited"
    assert 9_000 < got["soon"]["retry_after_ms"] <= 10_000, got["soon"]
    h.clock.t += 10
    await _send(h, "later", respond=True, rid="later")
    assert _results(h)[-1]["ok"] is True, "the interval ran out and it was still refused"
    await _settle(h)


async def test_a_burst_past_the_allowance_is_refused_not_queued():
    h = _harness(burst=3, refill_per_s=1.0)
    for i in range(5):
        await _send(h, f"event {i}", rid=str(i))
    oks = [r["ok"] for r in _results(h)]
    assert oks == [True, True, True, False, False], oks
    assert _results(h)[-1]["error"] == "rate_limited"
    await _settle(h)
    assert len(_notes_in(h.context)) == 3


async def test_a_note_is_never_captioned_or_forwarded():
    h = _harness()
    await _send(h, "The user tapped the character's left shoulder twice.")
    await _settle(h)
    assert not [f for f in h.pushed if isinstance(f, ClientContextFrame)], \
        "the note frame travelled on down the pipeline"
    kinds = {m.get("type") for m in _results(h)}
    assert kinds == {"context_result"}, f"something other than the ack went out: {kinds}"


# ---- when a note enters the context ----------------------------------------------

async def test_idle_context_note_is_appended_without_a_turn():
    h = _harness()
    await _send(h, "The user tapped the character's left shoulder twice.")
    assert _results(h)[-1] == {"type": "context_result", "id": "c1", "ok": True,
                               "status": "applied"}
    await _settle(h)
    tail = h.context.get_messages()[-1]
    assert tail["role"] == "user" and tail["content"].startswith(TAG)
    assert "left shoulder" in tail["content"]
    assert h.task.queued == [], "respond:false ran an LLM turn"


async def test_idle_respond_note_runs_one_reaction_and_retires_its_order():
    h = _harness()
    await _send(h, "The user tapped the character's nose.", respond=True)
    await _settle(h)
    assert [type(f) for f in h.task.queued] == [LLMRunFrame], h.task.queued
    tail = h.context.get_messages()[-1]
    assert "React to this now" in tail["content"]
    # The reaction's completion reads it: the order is retired, the event stays.
    await h.trigger.process_frame(LLMTextFrame("Hey, that tickles!"),
                                  FrameDirection.DOWNSTREAM)
    assert "React to this now" not in tail["content"], "a standing order was left live"
    assert "nose" in tail["content"]


async def test_a_note_during_a_reply_waits_and_keeps_arrival_order():
    h = _harness([{"role": "system", "content": "persona"},
                  {"role": "user", "content": "Tell me a story."}])
    await h.gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _send(h, "first event", rid="a")
    await _send(h, "second event", rid="b")
    assert [r["status"] for r in _results(h)] == ["queued", "queued"]
    await asyncio.sleep(0.05)
    assert _notes_in(h.context) == [], "a note went in while the bot was speaking"
    # The reply ends and is committed; then the conversation goes quiet.
    h.context.add_message({"role": "assistant", "content": "Once upon a time."})
    await h.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _settle(h)
    tail = [m["content"] for m in h.context.get_messages()[-3:]]
    assert tail == ["Once upon a time.", f"{TAG}\nfirst event", f"{TAG}\nsecond event"], tail


async def test_a_user_turn_takes_pending_notes_before_its_own_words():
    h = _harness()
    await h.gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _send(h, "The user tapped the character.", respond=True)
    assert _results(h)[-1]["status"] == "queued"
    # The user barges in; their turn commits and its completion is requested.
    h.context.add_message({"role": "user", "content": "What did I just do?"})
    await h.notes.process_frame(LLMContextFrame(context=h.context),
                                FrameDirection.DOWNSTREAM)
    msgs = h.context.get_messages()
    assert msgs[-1]["content"] == "What did I just do?", \
        "the note became the tail: the model would answer it instead of the user"
    assert msgs[-2]["content"].startswith(TAG) and "tapped" in msgs[-2]["content"]
    assert "React to this now" not in msgs[-2]["content"]
    # The frame still goes on to the LLM.
    assert any(isinstance(f, LLMContextFrame) for f in h.pushed)
    await h.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _settle(h)
    assert h.task.queued == [], "the user's turn answered the note and a second reply ran"


async def test_a_completion_not_answering_the_user_leaves_notes_waiting():
    h = _harness([{"role": "system", "content": "persona"},
                  {"role": "user", "content": "What's the weather?"},
                  {"role": "tool", "tool_call_id": "c1", "content": "sunny"}])
    await h.gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _send(h, "The user opened the map.")
    await h.notes.process_frame(LLMContextFrame(context=h.context),
                                FrameDirection.DOWNSTREAM)
    assert h.context.get_messages()[-1]["role"] == "tool", \
        "a note became the tail of a completion answering a tool result"
    await h.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _settle(h)
    assert len(_notes_in(h.context)) == 1, "the held note never went in"


async def test_a_reaction_that_cannot_find_a_quiet_moment_stays_as_context():
    h = _harness(react_max_wait_s=0.1)
    await h.gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _send(h, "The user waved.", respond=True)
    await asyncio.sleep(0.2)  # the bot is still talking past the reaction's deadline
    assert _notes_in(h.context) == []
    await h.gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await _settle(h)
    assert len(_notes_in(h.context)) == 1
    assert "React to this now" not in _notes_in(h.context)[0]["content"]
    assert h.task.queued == [], "a stale reaction ran"


# ---- the retention cap -------------------------------------------------------------

async def test_the_cap_removes_the_oldest_notes():
    h = _harness(max_notes=2)
    for i in range(3):
        await _send(h, f"event {i}", rid=str(i))
        await _settle(h)
    texts = [m["content"].split("\n", 1)[1] for m in _notes_in(h.context)]
    assert texts == ["event 1", "event 2"], texts


def _corrector(context, ledger_events):
    c = HeardContextCorrector(SimpleNamespace(events=ledger_events), context,
                              mode="truncate")
    return c


async def test_a_removal_below_the_mark_keeps_the_cut_reply_in_the_window():
    """The regression the drop_messages hook exists for. A note removed from BELOW the
    corrector's mark shifts the cut reply left, out of the window, and its unheard words
    stay in the context for the model to build on."""
    old_note = {"role": "user", "content": f"{TAG}\nold event"}
    context = LLMContext([
        {"role": "system", "content": "persona"},
        old_note,
        {"role": "user", "content": "Tell me about Rome."},
    ])
    events = []
    corrector = _corrector(context, events)
    corrector._reconcile()  # the turn above is settled; mark = 3
    # The reply is cut after "Ancient Rome began as a city-state," …
    context.add_message({"role": "assistant",
                         "content": "Ancient Rome began as a city-state, grew into an empire."})
    events.append(Utterance(speaker="assistant", text="…", t_start=0.0, t_end=1.0,
                            interrupted=True, heard_fraction=0.4,
                            heard_text="Ancient Rome began as a city-state,"))
    # … a note is added at a quiet moment and the cap removes the old one …
    corrector.drop_messages([old_note])
    context.add_message({"role": "user", "content": f"{TAG}\nnew event"})
    # … and the user's next turn reconciles.
    context.add_message({"role": "user", "content": "Go on."})
    corrector._reconcile()
    spoken = [m["content"] for m in context.get_messages() if m["role"] == "assistant"]
    assert spoken == ["Ancient Rome began as a city-state"], spoken


# ---- in the real pipeline -------------------------------------------------------------

class _Stub(FrameProcessor):
    pass


class _Transport:
    def __init__(self):
        self._input = _Stub(name="stub-in")
        self._output = _Stub(name="stub-out")

    def input(self):
        return self._input

    def output(self):
        return self._output


async def test_the_session_places_it_and_a_note_gets_there():
    from teaport_brain.agent_session import build_agent_session

    session = build_agent_session(_Transport())
    # The task wraps our pipeline (source, RTVIProcessor, Pipeline, sink); look inside.
    inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
    procs = list(inner.processors)
    kinds = [type(p) for p in procs]
    at = kinds.index(ClientNotes)
    assert procs[at] is session.client_notes
    assert session.client_notes.task is session.task
    assert kinds.index(HeardContextCorrector) < at, "notes must sit below the corrector"
    llm_at = next(i for i, p in enumerate(procs) if p is session.llm)
    assert at < llm_at, "notes must reach the context before the LLM reads it"
    # Every processor from the transport input down to ClientNotes passes the frame on.
    frame = ClientContextFrame(text="tap", request_id="x")
    start = next(i for i, p in enumerate(procs) if p.name == "stub-in")
    for p in procs[start + 1:at]:
        seen = []

        async def push(f, direction=FrameDirection.DOWNSTREAM, _seen=seen):
            _seen.append(f)
        p.push_frame = push
        await p.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert frame in seen, f"{type(p).__name__} swallowed the note"


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
