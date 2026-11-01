#
# Unit test: a sliced turn's final words, where they differ from those it was asked on
# (turn_text.TurnTextCorrector; stt.py TURN TEXT).
#
# What these pin, in order:
#
#   * amended: words still in the aggregator (the turn not committed) are replaced, and
#     the commit -- through CutReplyFence, as agent_session wires it -- writes the final;
#   * corrected: words committed into a user message are rewritten IN PLACE, the reply
#     kept: no message added, removed or moved; the newest message or one a few back;
#     never an injected system notice;
#   * unplaced: words found nowhere are logged only, never pushed again
#     (test_turn_text_taken.py has why);
#   * the rewrite trips nothing into a spurious miss, re-ask or double count:
#       - the speculative reply: a live snapshot holding the rewritten message is
#         rewritten with it, so the next commit is a hit (and without that, the
#         ctx-changed miss this exists to avoid);
#       - owed.py (#203): nothing renders, so its turn count and the gate's renders do
#         not move, no request goes out, and the next request reads the final words;
#       - over a real pipeline -- the aggregator, the stop strategy, CutReplyFence (#164),
#         the ledger, HeardContextCorrector, the reply hold and the assistant aggregator --
#         a correction while the reply plays asks nothing, cuts nothing, merges nothing
#         and waits on nothing; the reply plays out and is committed whole.
#
# All utterances are synthetic.
#
# Run: python test_turn_text_correction.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

import test_barge_commit_order as barge  # noqa: E402
import test_owed_render as render  # noqa: E402
import test_speculative_reply as specs  # noqa: E402

from teaport_brain import owed, services  # noqa: E402
from teaport_brain.agent_session import user_aggregator_params  # noqa: E402
from teaport_brain.barge_pause import PauseAwareMinWordsStrategy  # noqa: E402
from teaport_brain.endpointing import CutReplyFence, SegmentDoneSink  # noqa: E402
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG  # noqa: E402
from teaport_brain.turn_text import TurnTextCorrector, TurnTextFinalFrame  # noqa: E402

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


class Corrector(TurnTextCorrector):
    """The real corrector, its pushes kept."""

    def __init__(self, aggregator, **kwargs):
        super().__init__(aggregator, **kwargs)
        self.pushed = []

    async def push_frame(self, frame, direction=DOWN):
        self.pushed.append((frame, direction))


def final(asked, words, kind="adds", added=(), dropped=(), stop_n=1, pushed_at=0.0):
    return TurnTextFinalFrame(stop_n=stop_n, asked=asked, final=words, kind=kind,
                              added=tuple(added), dropped=tuple(dropped), pushed_at=pushed_at)


def _user():
    pair = LLMContextAggregatorPair(
        LLMContext([{"role": "system", "content": "sys"}]),
        user_params=user_aggregator_params(user_turn_strategies=UserTurnStrategies(
            start=[PauseAwareMinWordsStrategy(min_words=2)], stop=[])))
    return pair.user()


# ---- amended, corrected, unplaced ------------------------------------------------------

async def test_words_not_yet_committed_are_amended_and_the_commit_writes_the_final():
    user = _user()
    fence = CutReplyFence()
    fence.hold_commits(user)
    c = Corrector(user)
    await user._handle_transcription(TranscriptionFrame("so I was", "u", "t"))
    await user._handle_transcription(TranscriptionFrame("thinking about", "u", "t"))
    await c._apply(final("thinking about", "thinking about tea", added=("tea",)))
    assert c.amended == 1 and c.pushed == []
    assert user.aggregation_string() == "so I was thinking about tea"
    pushed = []
    user.push_frame = lambda f, d=DOWN: pushed.append(f) or asyncio.sleep(0)
    await user._push_aggregation()
    assert user.context.get_messages()[-1] == {"role": "user",
                                               "content": "so I was thinking about tea"}
    assert fence.waits == 0 and not any(isinstance(f, InterruptionFrame) for f in pushed)


async def test_committed_words_are_rewritten_in_place_and_the_reply_kept():
    user = _user()
    ctx = user.context
    c = Corrector(user)
    for m in ({"role": "user", "content": "what's the capital of france"},
              {"role": "assistant", "content": "Paris."},
              {"role": "user", "content": "and its population okay"}):
        ctx.add_message(m)
        if m["role"] == "user":
            c.committed(m)
    before = list(ctx.get_messages())
    await c._apply(final("and its population okay", "and its population okay stop",
                         added=("stop",)))
    after = ctx.get_messages()
    assert [id(m) for m in after] == [id(m) for m in before], "nothing added, removed or moved"
    assert after[-1]["content"] == "and its population okay stop"
    assert after[2] == {"role": "assistant", "content": "Paris."}, "the reply is kept"
    assert c.corrected == 1 and c.pushed == []
    # one a turn back (the caller has spoken since): still corrected, in its own message
    # -- the first committed after the words were pushed
    await c._apply(final("of france", "of frances", kind="changes", added=("frances",),
                         dropped=("france",)))
    assert after[1]["content"] == "what's the capital of frances" and c.corrected == 2
    # but never in a message committed BEFORE the words were pushed
    pushed = time.monotonic()
    await c._apply(final("its", "it's", kind="changes", added=("it's",), dropped=("its",),
                         stop_n=3, pushed_at=pushed))
    assert c.corrected == 2 and after[3]["content"] == "and its population okay stop", after


async def test_a_final_with_no_words_takes_the_preview_only_word_out_but_never_empties_a_turn():
    user = _user()
    c = Corrector(user)
    await user._handle_transcription(TranscriptionFrame("yeah let's go ahead", "u", "t"))
    await user._handle_transcription(TranscriptionFrame("and", "u", "t"))
    await c._apply(final("and", "", kind="drops", dropped=("and",)))
    assert user.aggregation_string() == "yeah let's go ahead" and c.amended == 1
    ctx = user.context
    ctx.add_message({"role": "user", "content": "yeah let's go ahead and"})
    c.committed(ctx.get_messages()[-1])
    await c._apply(final("yeah let's go ahead and", "yeah let's go ahead", kind="drops",
                         dropped=("and",)))
    assert ctx.get_messages()[-1]["content"] == "yeah let's go ahead"
    ctx.add_message({"role": "user", "content": "uh"})
    c.committed(ctx.get_messages()[-1])
    await c._apply(final("uh", "", kind="drops", dropped=("uh",)))
    assert ctx.get_messages()[-1]["content"] == "uh", "a turn is never emptied"


async def test_a_turn_folded_into_the_one_before_is_corrected_there():
    user = _user()
    ctx = user.context
    c = Corrector(user)
    ctx.add_message({"role": "user", "content": "what's the weather"})
    c.committed(ctx.get_messages()[-1])
    pushed = time.monotonic()
    ctx.add_message({"role": "user", "content": "in paris"})
    c.committed(ctx.get_messages()[-1])
    # the reply hold dropped the unplayed reply; the corrector merged the two
    first, last = ctx.get_messages()[-2:]
    first["content"] = "what's the weather in paris"
    ctx.set_messages(ctx.get_messages()[:-1])
    await c._apply(final("in paris", "in paris tomorrow", added=("tomorrow",),
                         pushed_at=pushed))
    assert ctx.get_messages()[-1]["content"] == "what's the weather in paris tomorrow"
    assert c.corrected == 1 and c.pushed == []


async def test_only_whole_words_are_rewritten():
    user = _user()
    ctx = user.context
    c = Corrector(user)
    ctx.add_message({"role": "user", "content": "I know no"})
    c.committed(ctx.get_messages()[-1])
    await c._apply(final("no", "no thanks", added=("thanks",)))
    assert ctx.get_messages()[-1]["content"] == "I know no thanks"
    pushed = time.monotonic()
    ctx.add_message({"role": "user", "content": "I know"})
    c.committed(ctx.get_messages()[-1])
    await c._apply(final("no", "no way", added=("way",), pushed_at=pushed))
    assert ctx.get_messages()[-1]["content"] == "I know", "the end of a word is no turn"
    assert ctx.get_messages()[-2]["content"] == "I know no thanks", "nor an older turn"


async def test_an_injected_notice_is_never_taken_for_the_callers_words():
    user = _user()
    ctx = user.context
    c = Corrector(user)
    ctx.add_message({"role": "user", "content": "go ahead"})
    c.committed(ctx.get_messages()[-1])
    notice = f"{SYSTEM_NOTICE_TAG} the consult says go ahead"
    ctx.add_message({"role": "user", "content": notice})
    c.committed(ctx.get_messages()[-1])
    await c._apply(final("go ahead", "go ahead please", added=("please",)))
    assert ctx.get_messages()[-1]["content"] == notice
    assert ctx.get_messages()[-2]["content"] == "go ahead please"


async def test_words_placed_nowhere_are_logged_only():
    # Never pushed again, whatever the final adds (test_turn_text_taken.py has why).
    user = _user()
    c = Corrector(user)
    await c._apply(final("okay", "okay stop", added=("stop",), stop_n=4))
    assert c.pushed == [] and c.unplaced == 1 and c.last_unplaced is None
    await c._apply(final("go ahead and", "go ahead", kind="drops", dropped=("and",)))
    assert c.pushed == [] and c.unplaced == 2


# ---- no spurious miss, re-ask or double count -----------------------------------------

async def test_the_live_speculation_is_corrected_with_the_context_and_hits():
    for wired in (True, False):
        ctx, agg, llm, spec, opened = specs._rig(parts=("what about tomorrow",))
        c = Corrector(agg, speculator=spec if wired else None)
        ctx.add_message({"role": "user", "content": "will it rain okay"})
        c.committed(ctx.get_messages()[-1])
        ctx.add_message({"role": "assistant", "content": "No rain today."})
        assert await spec.start(spec.candidate(), "test")
        await specs._settle()
        await c._apply(final("will it rain okay", "will it rain today", kind="changes",
                             added=("today",), dropped=("okay",)))
        ctx.add_message({"role": "user", "content": "what about tomorrow"})
        out = await llm.get_chat_completions(ctx)
        await out.close()
        await spec.close()
        if wired:
            assert (spec.hits, spec.misses, len(opened)) == (1, 0, 1), (
                "the correction made the next commit a miss")
        else:
            assert (spec.hits, spec.misses, len(opened)) == (0, 1, 2), (
                "control: without the snapshot's rewrite the commit misses")


async def test_owed_counts_nothing_twice_and_the_next_request_reads_the_final():
    provider = render._Provider()
    llm = render._llm(provider)
    ctx = render._context({"role": "assistant", "content": "The top story is out."},
                          {"role": "user", "content": "I'm ready"})
    llm.owed, gate = render._book(ctx)

    async def commit():
        token = services._REPLY.set(owed.Reply(owed.COMMIT))
        try:
            await llm.get_chat_completions(ctx)
        finally:
            services._REPLY.reset(token)

    await commit()
    turns, rendered = llm.owed._turn, gate.rendered
    agg = specs.FakeAggregator(ctx)
    c = Corrector(agg)
    c.committed(ctx.get_messages()[-1])            # the commit just asked
    await c._apply(final("I'm ready", "I'm ready now", added=("now",)))
    assert (llm.owed._turn, gate.rendered, len(provider.requests)) == (turns, rendered, 1), (
        "a correction rendered, counted a turn or asked")
    ctx.add_message({"role": "assistant", "content": "Here it is."})
    ctx.add_message({"role": "user", "content": "thanks"})
    await commit()
    assert llm.owed._turn == turns + 1 and gate.rendered == rendered + 1
    asked = [m.get("content") for m in provider.requests[1]["messages"]]
    assert "I'm ready now" in asked and "I'm ready" not in asked


async def test_over_the_real_pipeline_a_correction_while_the_reply_plays_asks_and_cuts_nothing():
    call = barge.Call(with_fence=True)
    user = call.pair.user()
    corrector = Corrector(user)
    pushed = corrector.pushed

    async def passing(frame, direction=DOWN):
        pushed.append((frame, direction))
        await TurnTextCorrector.push_frame(corrector, frame, direction)
    corrector.push_frame = passing
    call.task = PipelineTask(Pipeline([
        user, call.fence.head, corrector, SegmentDoneSink(), call.corrector,
        call.llm, call.tts, call.gate, barge.Playout(), call.pair.assistant(),
        call.fence.tail,
    ]), observers=[call.ledger], cancel_on_idle_timeout=False)
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(call.task))
    try:
        await asyncio.sleep(barge.STARTUP_S)
        asked = "Count from one to twenty"
        await call.utterance(asked, stop_n=1, late=False)
        await asyncio.sleep(1.0)                   # the count is playing
        waits, since = call.fence.waits, len(pushed)
        await call.task.queue_frame(final(asked, barge.REQUEST, kind="same"))
        await asyncio.sleep(0.3)
        msgs = call.context.get_messages()
        users = [m["content"] for m in msgs if m["role"] == "user"]
        assert users == [barge.REQUEST], users
        assert corrector.corrected == 1
        assert len(call.llm.contexts) == 1, "the correction asked the model again"
        assert call.fence.waits == waits and call.requested == 0 and not call.merge.pending
        assert not any(isinstance(f, (InterruptionFrame, TranscriptionFrame))
                       for f, _ in pushed[since:]), "the correction cut or re-sent something"
        assert not any(u.interrupted for u in call.ledger.events
                       if u.speaker == "assistant")
        await asyncio.sleep(barge.WORD_S * 22)    # the count plays out
    finally:
        await call.task.cancel()
        await run
    replies = [m["content"] for m in call.context.get_messages() if m["role"] == "assistant"]
    assert replies == [barge.COUNT], replies


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
