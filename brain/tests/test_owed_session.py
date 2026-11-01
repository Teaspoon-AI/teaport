#
# #203 replayed through a whole session: owed until heard, end to end.
#
# The rig is the session build_agent_session makes for Talk, run from the user aggregator
# down (as test_post_tool_under_speech.py runs it): the real corrector, TurnCommitMark,
# BoundedOpenAILLMService with its render hook, the guard, EngineTTSService, a real
# output transport paced at real time, FollowupGate (the run's door), the assistant
# aggregator, under the session's TranscriptLedger (the settle feed) -- and the session's
# own owed records. Scripted: the provider (a fake openai client: the request it records
# is what the model would have seen), the engine's synthesis, the stop strategy's verdict,
# the caller.
#
# The #203 session, 2026-10-16 22:27 (box .105), and what it does now:
#   :29.8  the consult lands                -> owed; one run, the note at the tail
#   :30.5  the run's reply opens with a lead-in restating the question
#   :32.4  the caller cuts in 1.1 s into it -> heard no answer: still owed, the lead-in
#                                              quoted, the re-offer armed
#          "Hold on."                       -> the note rides before their words; the
#                                              model defers
#          a long lull                      -> the one re-offer, which may stay silent
#          "I'm ready."                     -> the note; the answer, heard: closed
#          later                            -> no note
#
# Run: python test_owed_session.py   (or via the suite)
#
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")
# The speculative reply ON: "I'm ready." goes through an adopted speculation, the path
# the note's keys must survive (Owed.tla adoptDropsKeys).
os.environ["TEAPORT_SPECULATIVE_REPLY"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from types import SimpleNamespace as NS  # noqa: E402

from openai.types.chat import ChatCompletionChunk  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain import owed  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG, FollowupGate  # noqa: E402
from test_post_tool_under_speech import ScriptedStop, TalkTransport, _segments  # noqa: E402

STARTUP_TIMEOUT_S = 10.0
STEP_TIMEOUT_S = 10.0
REQ = "Fetch the current top story from Hacker News and give its title and URL."
HN_RAW = ("Top story right now (score 1,255 · 2,253 comments):\n\n- **Title:** Tesla "
          "Roadster\n- **URL:** \n\n(Posted by `shekhar101`.)")
LEAD_IN_FIRST = ("The top story on Hacker News right now is the Tesla Roadster, posted by "
                 "shekhar101, with twelve hundred fifty five points.")
ANSWER_FIRST = "Tesla Roadster is the top story on Hacker News right now."


def _chunk(text=None, tool=None):
    delta = {"role": "assistant"}
    if text is not None:
        delta["content"] = text
    if tool is not None:
        delta["tool_calls"] = [{"index": 0, "id": f"call-{tool}", "type": "function",
                                "function": {"name": tool, "arguments": "{}"}}]
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c

    async def close(self):
        pass


class Provider:
    """The openai client's chat.completions: records each request; `say` is the next
    reply (text, or ("call", name))."""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list = []
        self.asked = asyncio.Event()

    async def create(self, **params):
        self.requests.append(params)
        self.asked.set()
        reply = self.replies.pop(0) if self.replies else "Okay."
        if isinstance(reply, tuple):
            return _Stream([_chunk(tool=reply[1])])
        return _Stream([_chunk(text=reply)])


def _note(params):
    notes = [m["content"] for m in params["messages"] if m.get("role") == "user"
             and isinstance(m.get("content"), str) and m["content"].startswith(SYSTEM_NOTICE_TAG)]
    return notes[0] if notes else None


class Rig:
    def __init__(self):
        transport = TalkTransport()
        session = build_agent_session(transport, front_end="talk")
        self.session, self.owed = session, session.owed
        assert self.owed.judge == session.llm.judge_cut, "the session's cut judge"
        assert self.owed.gist == session.llm.judge_gist, "the session's gist judge"
        assert self.owed.decline == session.llm.judge_decline, "the session's decline check"
        # The scripted provider answers the turns; nothing is judged here: a cut errs owed,
        # a reply about a record played to its end closes it (no verdict), and the words
        # that cut a telling leave it owed (no verdict).
        self.owed.judge = self.owed.gist = self.owed.decline = None
        inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
        procs = inner.processors
        first = next(i for i, p in enumerate(procs) if isinstance(p, LLMUserAggregator))
        self.procs = [p for p in procs[first:] if not type(p).__name__.startswith("Pipeline")]
        self.user = procs[first]
        self.assistant = next(p for p in self.procs if isinstance(p, LLMAssistantAggregator))
        self.gate = next(p for p in self.procs if isinstance(p, FollowupGate))
        controller = self.user._user_turn_controller
        self.stop = ScriptedStop()
        controller._user_turn_strategies = UserTurnStrategies(
            start=controller._user_turn_strategies.start, stop=[self.stop])
        session.tts._synth_text = _segments
        self.provider = Provider()
        llm = session.llm
        llm._client = NS(chat=NS(completions=self.provider))
        llm._warm_closed = True
        self.played: list[str] = []
        self.bot_stops = 0
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            if isinstance(frame, TTSTextFrame):
                self.played.append(frame.text)
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.bot_stops += 1
        self.assistant.process_frame = recording
        self.task = PipelineTask(Pipeline(self.procs), observers=[session.ledger],
                                 cancel_on_idle_timeout=False)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STARTUP_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc):
        await self.task.cancel()
        await self._run

    def seed(self):
        """The turn that asked: the caller's question, the call, its placeholder."""
        self.session.context.add_messages([
            {"role": "user", "content": "Can you look up what the top story on Hacker News is right now?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-hn", "type": "function",
                 "function": {"name": "openclaw_agent_consult", "arguments": json.dumps({"question": REQ})}}]},
            {"role": "tool", "tool_call_id": "call-hn",
             "content": json.dumps({"status": "working_in_background"})}])
        self.owed.dispatch("call-hn", REQ)

    async def requests(self, n):
        await until(lambda: len(self.provider.requests) >= n)
        return self.provider.requests[n - 1]

    async def say(self, text, speculate=False):
        """The caller's turn: speech, its words, the verdict -- after a speculative
        reply on those words has come back, when `speculate`."""
        stops = self.bot_stops
        await self.task.queue_frames([VADUserStartedSpeakingFrame()])
        f = TranscriptionFrame(text, "caller", "t")
        f.finalized = True
        await self.task.queue_frames([f, VADUserStoppedSpeakingFrame()])
        await until(lambda: self.user._user_turn_controller._user_turn)
        if speculate:
            spec = self.session.llm.speculator
            assert await spec.start(text, "test"), "no speculation started"
            await until(lambda: spec._current is not None and spec._current.done)
        await self.stop.commit()
        return stops

    async def played_out(self, since):
        await until(lambda: self.bot_stops > since)
        await until(self.owed.is_settled)


async def until(predicate, timeout=STEP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.01)


async def test_an_answer_heard_as_it_lands_is_closed_and_not_rendered_again():
    async with Rig() as rig:
        rig.seed()
        rig.provider.replies = [ANSWER_FIRST, "You're welcome!"]
        rig.owed.land(REQ, HN_RAW, "call-hn")
        run = await rig.requests(1)
        tail = run["messages"][-1]
        assert tail["role"] == "user" and tail["content"].startswith(
            SYSTEM_NOTICE_TAG + "\n" + owed.RUN_LINE), tail
        assert "Tesla Roadster" in tail["content"] and "They have NOT heard this yet." in tail["content"]
        await rig.played_out(0)
        assert rig.owed.record("call-hn").state == owed.CLOSED, "heard in full, still owed"
        stored = [m for m in rig.session.context.get_messages()
                  if isinstance(m.get("content"), str) and m["content"].startswith(SYSTEM_NOTICE_TAG)]
        assert not stored, "the note was stored in the context"
        tool = next(m for m in rig.session.context.get_messages() if m.get("role") == "tool")
        assert "Tesla Roadster" in json.loads(tool["content"])["answer"]
        await rig.say("Thanks, that's cool.")
        later = await rig.requests(2)
        assert _note(later) is None, "a heard answer was rendered again"


async def test_203_a_lead_in_cut_stays_owed_until_the_caller_hears_it():
    async with Rig() as rig:
        rig.seed()
        rig.owed._reoffer_quiet_s = 0.6
        rig.provider.replies = [LEAD_IN_FIRST, "No rush, just let me know when you're ready.",
                                ("call", owed.STAY_SILENT), ANSWER_FIRST]
        rig.owed.land(REQ, HN_RAW, "call-hn")
        await rig.requests(1)
        # The caller cuts in ~1.1 s into the lead-in: "The top story on ..." (the fake
        # engine plays 0.3 s a word).
        await until(lambda: len(rig.played) >= 4)       # "The top story on" has played
        await rig.say("Oh, wow, I just saw something cool. Hold on.")
        hold_on = await rig.requests(2)
        rec = rig.owed.record("call-hn")
        assert rec.state == owed.OWED, "a lead-in that named nothing closed the record (#203)"
        assert rec.heard_quote and "Roadster" not in rec.heard_quote, rec.heard_quote
        msgs = hold_on["messages"]
        assert msgs[-1]["content"].endswith("Hold on."), msgs[-1]
        assert msgs[-2]["content"].startswith(SYSTEM_NOTICE_TAG + "\nResults you have"), msgs[-2]
        assert f"They heard only \"{rec.heard_quote}\"" in msgs[-2]["content"]
        # The deferral plays out, then the line is quiet: the one re-offer.
        reoffer = await rig.requests(3)
        names = [t["function"]["name"] for t in reoffer.get("tools") or []]
        assert owed.STAY_SILENT in names, names
        assert reoffer["messages"][-1]["content"].endswith(owed.REOFFER_LINE)
        assert owed.NOT_HEARD in reoffer["messages"][-1]["content"], (
            "the re-offer quoted the lead-in (RESULTS-confirm: 9/40 stay silent, 18/40 not)")
        n = len(rig.session.context.get_messages())
        try:
            await until(lambda: rig.gate.is_clear())
        except AssertionError:
            g = rig.gate
            raise AssertionError(f"gate not clear: user={g._user} bot={g._bot} llm={g._llm} "
                                 f"turn={g._turn} claimed={g._claimed} held={g.held}")
        await asyncio.sleep(1.5)
        assert len(rig.provider.requests) == 3, "a second re-offer (OneReoffer)"
        assert len(rig.session.context.get_messages()) == n, "stay_silent entered the context"
        assert rec.state == owed.OWED and rec.reoffers == 1
        # "I'm ready."
        stops = await rig.say("I'm ready.", speculate=True)
        ready = await rig.requests(4)
        assert "Tesla Roadster" in (_note(ready) or ""), "the caller's turn went without the note"
        await rig.played_out(stops)
        assert rig.session.llm.speculator.hits == 1, "the commit did not adopt the speculation"
        assert len(rig.provider.requests) == 4, "the commit asked again"
        assert rec.state == owed.CLOSED, "a full telling through an adopted stream left it owed"


TOY = ("There's a Toy Joy store at five-five-zero-one Burnet Road in Austin. Toy Joy at "
       "five-five-zero-one Burnet Road, Austin, Texas.")
T_REQ = "Can you look if there are any toy stores on Burnett Road in Austin, Texas?"
T_RESULTS = [{"title": "Toy Joy - Austin's Coolest Toy Store",
              "snippet": "Toy Joy, 5501 Burnet Rd, Austin, TX 78756. Open daily 10am-9pm."}]


async def test_204_a_sync_answer_cut_at_hold_on_is_owed_until_heard():
    """Call 3 of 2026-10-18 (17:27): a sync tool's answer, cut at "There's a Toy Joy
    store at" by "Hold on, hold on.". Live: "Got it, pausing." and 64 s of silence. Now:
    the cut answer is owed with its whole text, the caller's turn carries the note
    before their words, the lull's one re-offer goes, and its full telling closes it."""
    async with Rig() as rig:
        async def web_search(params):                        # the box's: no agent here
            await params.result_callback({"results": T_RESULTS})
        rig.session.llm.register_function("web_search", web_search)
        rig.owed._reoffer_quiet_s = 0.6
        rig.provider.replies = [("call", "web_search"), TOY, "Got it, pausing.", TOY]
        await rig.say(T_REQ)
        await rig.requests(2)                                # the post-tool answer
        await until(lambda: len(rig.played) >= 6)            # "There's a Toy Joy store at"
        await rig.say("Hold on, hold on.")
        hold_on = await rig.requests(3)
        rec = rig.owed.record("call-web_search")
        assert rec is not None and rec.state == owed.OWED, "the cut answer was owed to nobody"
        assert rec.outcome == owed.clean_outcome(TOY) and rec.request == T_REQ
        assert rec.heard_quote.startswith("There's a Toy Joy"), rec.heard_quote
        msgs = hold_on["messages"]
        assert msgs[-1]["content"] == "Hold on, hold on.", msgs[-1]
        assert msgs[-2]["content"].startswith(SYSTEM_NOTICE_TAG + "\nResults you have")
        assert f'They asked: "{T_REQ}"' in msgs[-2]["content"]
        reoffer = await rig.requests(4)                      # the lull, after "pausing"
        assert reoffer["messages"][-1]["content"].endswith(owed.REOFFER_LINE)
        stops = rig.bot_stops
        await rig.played_out(stops)
        assert rec.state == owed.CLOSED, "the re-offer's full telling left it owed"


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
