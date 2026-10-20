#
# owed.py (#203): the record, the note, the evidence -- unit tests, and the ledger's
# settle feed into it.
#
# brain/formal/Owed.tla leads the code; the rows these stand in for are named on each.
# The texts are pinned to what the replay rounds measured (the #203 design notes,
# harness spec_round3..5): the judge replays through owed.render, so a reworded note
# here is a different experiment.
#
# Run: python test_owed.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.utils.text.base_text_aggregator import AggregationType  # noqa: E402

from teaport_brain import owed  # noqa: E402
from teaport_brain import wake_gate  # noqa: E402
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG  # noqa: E402
from teaport_brain.owed import (  # noqa: E402
    CLOSED, DROPPED, OWED, Owed, Record, Reply, clean_outcome, item_heard, render,
    spoken_tokens, split_items, terms_of,
)
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

# ---- the #203 session (box .105, 2026-10-16 22:27; ctx-3263 in the design notes) ----
HN_RAW = ("Top story right now (score 1,255 · 2,253 comments):\n\n- **Title:** Tesla "
          "Roadster\n- **URL:** \n\n(Posted by `shekhar101`.)")
HN_REQ = "Fetch the current top story from Hacker News and give its title and URL."
# spec_round3.clean(): what every replay round handed the model as the result.
HN_CLEAN = ("Top story right now (score 1,255 · 2,253 comments):\n\n- Title: Tesla "
            "Roadster\n\n(Posted by shekhar101.)")
LEAD_IN = "The top story on Hacker News right now"      # what the caller heard (#203)
TOP5_RAW = ("Here are the current top 5 Hacker News stories:\n\n1. **Tesla Roadster** — \n"
            "2. **Ultramarine 1 Flash** — \n3. **Llama.cpp on a jailbroken PS5: run any "
            "GGUF model natively on the console** — \n4. **Claude reveals ECDSA curve "
            "origins** — \n5. **Butlerian Jihad** — \n\nPulled live from the HN "
            "Firebase API just now.")
TOP5_REQ = "Fetch the current top 5 stories from Hacker News with their titles and URLs."

# ---- the measured texts (spec_round3.final_note, round 4 S3, round 5 FIRST_C / RE_S) --
MEASURED_FOOTER = (
    "Say only what a result says. If they want something it doesn't include (a link, more "
    "detail, other items), say you don't have that and offer to look it up; never fill it "
    "in yourself, and never read out web addresses. Don't look up again what a result "
    "already answers. If they're busy with something else or asked you to wait, don't push "
    "it.")
MEASURED_FIRST = ("Nothing else is going on: tell them now in one or two short spoken sentences. "
                  "Lead with the answer, then say what it answers (\"<answer> is …\"), not "
                  "the other way round.")
MEASURED_REOFFER = (
    "They haven't heard this yet and the conversation has gone quiet. If their last words asked "
    "you to wait and they haven't come back to you, call stay_silent. Otherwise mention it once "
    "in passing, in one short sentence that says what it's about and gives the answer (\"By the "
    "way, that ... is ...\").")


def measured_note(status="They have NOT heard this yet.", run=None, run_first=False):
    """spec_round3.final_note + spec_round4.note_with, verbatim."""
    t = (SYSTEM_NOTICE_TAG + "\nResults you have for the user. Each is everything that result "
         f"contains.\n1. They asked: \"{HN_REQ}\"\n   Result: {HN_CLEAN}\n   {status}\n"
         + MEASURED_FOOTER)
    if run and run_first:
        head, rest = t.split("\n", 1)
        t = head + "\n" + run + "\n" + rest
    elif run:
        t += "\n" + run
    return t


def hn_record(**kw):
    return Record.make("call-hn", HN_REQ, clean_outcome(HN_RAW), **kw)


def tool_round(key="call-hn", request=HN_REQ, placeholder=None):
    return [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "Can you look up what the top story on Hacker News is?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": key, "type": "function",
             "function": {"name": "ask_openclaw", "arguments": json.dumps({"request": request})}}]},
        {"role": "tool", "tool_call_id": key,
         "content": placeholder or json.dumps({"status": "working_in_background"})},
    ]


# =============================================================== the outcome, cleaned

def test_the_203_answer_cleans_to_what_the_replays_measured():
    assert clean_outcome(HN_RAW) == HN_CLEAN, repr(clean_outcome(HN_RAW))


def test_a_numbered_list_becomes_items_and_loses_its_dangling_dashes():
    out = clean_outcome(TOP5_RAW)
    assert "—" not in out and "**" not in out, out
    assert "1." not in out.split("\n")[2], "the list numbering would clash with the note's"
    items = split_items(out)
    assert items == ["Tesla Roadster", "Ultramarine 1 Flash",
                     "Llama.cpp on a jailbroken PS5: run any GGUF model natively on the console",
                     "Claude reveals ECDSA curve origins", "Butlerian Jihad"], items


def test_openclaw_meta_notes_links_and_addresses_go():
    raw = ("The top story is **\"Tesla Roadster\"** — a link straight to [tesla.com/roadster]. "
           "See [the thread](https://news.ycombinator.com/item?id=1) or https://x.y/z.\n\n"
           "<details><summary>Centonote</summary>Firebase API</details>\n\n"
           "*Recollect note:* per USER.md I was told to use the \"Open Claw tool\" for HN.")
    out = clean_outcome(raw)
    for gone in ("tesla.com", "http", "ycombinator", "Recollect", "Centonote", "Firebase",
                 "USER.md", "**"):
        assert gone not in out, (gone, out)
    assert "the thread" in out and "Tesla Roadster" in out, out


def test_a_plain_answer_is_one_item():
    assert split_items("It's sunny and 75 in Austin tomorrow.") == [
        "It's sunny and 75 in Austin tomorrow."]
    assert split_items(HN_CLEAN) == [HN_CLEAN], "one bullet is not a list"


# =============================================================== the spoken form, terms

def test_a_number_is_one_token_however_it_is_written():
    assert spoken_tokens("1,255") == spoken_tokens("twelve hundred fifty-five") == ["1255"]
    assert spoken_tokens("2,253") == spoken_tokens("two thousand two hundred fifty three")
    assert spoken_tokens("the one I like")[:2] == ["the", "one"], "a lone 'one' is a word"


def test_the_203_terms_are_the_answer_not_the_question():
    """DESIGN §5: terms = {tesla, roadster, shekhar101, 1255, 2253}. "top", "story",
    "hacker", "news", "title" are the request's; "score", "comments", "posted" common."""
    rec = hn_record()
    assert rec.items[0][1] == {"tesla", "roadster", "shekhar101", "1255", "2253"}, rec.items


def test_a_contraction_is_no_term_and_a_possessive_is_its_word():
    """The #204 review: split on the apostrophe, "they're" and "you'll" left the terms
    "re" and "ll", so "Sorry, they're still checking -- you'll hear back" closed "They're
    open until nine tonight, and you'll find them ...", and any "I'll" closed "Got it,
    I'll remember that ...". A clitic contraction is one token and common; "Joy's" is
    "joy"; a name with an apostrophe stays whole."""
    toks = spoken_tokens("They're open, you'll see; I've, I'd, I'm, don't, Toy Joy's, O'Brien")
    assert toks == ["they're", "open", "you'll", "see", "i've", "i'd", "i'm", "don't", "toy",
                    "joy", "o'brien"], toks
    rec = Record.make("k", "Is the pharmacy on Lamar open tonight?",
                      "They're open until nine tonight, and you'll find them next to the H-E-B.")
    assert not rec.closes_on("Sorry, they're still checking -- you'll hear back in a moment.",
                             run=False)
    assert terms_of("Got it, I'll remember that Ann is your wife.",
                    "Remember that my wife's name is Ann.") == frozenset()


def test_a_script_without_spaces_is_one_token_per_character():
    assert spoken_tokens("東京タワー") == ["東", "京", "タ", "ワ", "ー"]


# =============================================================== the close rule

def test_the_203_lead_in_covers_nothing_and_the_answer_does():
    """Owed.tla ow_fraction (#203): the lead-in that restates the question names none of
    the answer's terms, the answer names two. (Coverage: the text as heard in full. A
    cut is the judge's, test_a_cut_is_closed_by_the_judge_alone.)"""
    rec = hn_record()
    assert not rec.closes_on(LEAD_IN, run=False)
    assert rec.closes_on("Tesla Roadster is the top", run=False)


def test_a_deferral_closes_nothing():
    """ow_singleWord: "Sure, take your time." against an outcome that has "time" in it."""
    rec = Record.make("k", "When is my dentist appointment?",
                      "Your dentist appointment is at 3 pm on Thursday; arrive in good time.")
    assert "time" not in rec.items[0][1] and "sure" not in rec.items[0][1]
    for line in ("Sure, take your time.", "No rush, just let me know when you're ready.",
                 "Okay, I'll wait."):
        assert not rec.closes_on(line, run=False), line


def test_present_does_not_contain_sent():
    assert not item_heard(frozenset({"sent", "rome"}), set(spoken_tokens("I'll present it in Rome")))
    assert item_heard(frozenset({"sent", "rome"}), set(spoken_tokens("It was sent to Rome")))


def test_one_story_of_five_does_not_close_the_list():
    """ow_oneItem: every item, not one."""
    rec = Record.make("k", TOP5_REQ, clean_outcome(TOP5_RAW))
    assert len(rec.items) == 5
    assert not rec.closes_on("The top story is Tesla Roadster.", run=False)
    assert not rec.closes_on("The top story is Tesla Roadster.", run=True), (
        "an uncut run that named one item closed five (ow_asWritten_closed)")
    told = ("Tesla Roadster, then Ultramarine One Flash, then Llama dot cpp on a jailbroken "
            "PS5, then Claude reveals ECDSA curve origins, and Butlerian Jihad.")
    assert rec.closes_on(told, run=False), [sorted(t) for _, t in rec.items]


def test_an_item_with_one_term_needs_that_term():
    rec = Record.make("k", "Is the pharmacy open?", "Yes.")
    assert rec.items[0][1] == {"yes"}
    assert rec.closes_on("Yes, it is.", run=False)
    assert not rec.closes_on("Let me know when you're ready.", run=False)


def test_a_failure_closes_when_a_reply_about_it_is_heard_to_its_end():
    """A failure is one item with no terms (Owed.tla OUTCOME = "failure"). It closes on a
    run heard to its end, or -- RUNCLOSE = "about", Fable's final review -- on any noted
    reply heard to its end that is about the record: the caller's own turn told it, and
    the lull must not tell it again (ow_failure_runOnly). An unrelated reply is about
    nothing the request named and closes nothing. (A cut is the judge's: Owed.settle.)"""
    status, outcome = owed.failure_outcome("timeout")
    rec = Record.make("k", HN_REQ, outcome, failure=status)
    assert rec.items[0][1] == frozenset()
    told = "Sorry, that Hacker News lookup ran out of time before it finished."
    assert rec.closes_on(told, run=False), "the caller's turn told it in full"
    assert not rec.closes_on("It's eleven twenty-two at night.", run=False), (
        "an unrelated reply closed the failure")
    assert rec.closes_on("Sorry, that one ran out of time.", run=True)


def test_no_pts_prefix_closes_nothing_with_terms():
    """No text closes nothing (DESIGN v3 #4: a cut reply whose words carried no pts is
    never judged either, Owed.settle)."""
    assert not hn_record().closes_on(None, run=False)


# =============================================================== the note

def test_a_caller_turn_carries_the_measured_note_before_the_last_caller_message():
    rec = hn_record()
    msgs = tool_round() + [
        {"role": "assistant", "content": LEAD_IN},
        {"role": "user", "content": "Oh, wow, I just saw something cool. Hold on."},
        {"role": "assistant", "content": "No rush—just let me know when you're ready."},
        {"role": "user", "content": "I'm ready."}]
    before = [dict(m) for m in msgs]
    out = render(msgs, [rec], "caller")
    assert msgs == before, "render modified the context"
    assert out[-1] == msgs[-1] and out[-2]["content"] == measured_note(), out[-2]["content"]
    assert len(out) == len(msgs) + 1


def test_a_partial_hearing_is_quoted():
    rec = hn_record(heard_quote="It's the Tesla")
    note = render([{"role": "user", "content": "I'm ready."}], [rec], "caller")[0]["content"]
    assert note == measured_note(
        status="They heard only \"It's the Tesla\" before they spoke, which is not the "
               "whole answer."), note


def test_the_note_never_goes_before_a_notice_or_a_wake_cue():
    rec = hn_record()
    msgs = [{"role": "user", "content": "what's the weather?"},
            {"role": "assistant", "content": "Sunny."},
            {"role": "user", "content": f"{SYSTEM_NOTICE_TAG}\nReact now ..."},
            {"role": "user", "content": "(The call/session just connected. Greet me warmly "
                                        "in one short sentence.)"},
            {"role": "user", "content": "[app event, not spoken by the user] \"tap\""}]
    out = render(msgs, [rec], "caller")
    at = next(i for i, m in enumerate(out) if m["content"].startswith(SYSTEM_NOTICE_TAG + "\nResults"))
    assert out[at + 1]["content"] == "what's the weather?", out


def test_the_greeting_and_resume_cues_are_not_caller_words():
    from teaport_brain.agent_session import AgentSession
    from teaport_brain.client_notes import _TAG
    for cue in (AgentSession._GREETING, AgentSession._RESUME_GREETING, f"{_TAG} \"x\"",
                f"{SYSTEM_NOTICE_TAG}\nx"):
        assert not owed.is_caller_message({"role": "user", "content": cue}), cue
    assert owed.is_caller_message({"role": "user", "content": "I'm ready."})


def test_a_run_renders_at_the_tail_with_the_run_line_first():
    out = render(tool_round(), [hn_record()], "run")
    assert out[-1]["content"] == measured_note(run=MEASURED_FIRST, run_first=True)


def test_the_reoffer_line_goes_where_it_was_measured():
    """Round 4 and 5 appended it (spec_round4.note_with(run=RE_S)): 18-20/20."""
    out = render(tool_round(), [hn_record()], "reoffer")
    assert out[-1]["content"] == measured_note(run=MEASURED_REOFFER)


def test_the_reoffer_says_not_heard_whatever_was_quoted():
    """The confirmation round (RESULTS-confirm): after "hold on" the re-offer stayed
    silent 9/40 with the lead-in quoted, 18/40 with round 5's "They have NOT heard this
    yet.". A caller's turn keeps the quote (round 4 S3, 20/20)."""
    rec = hn_record(heard_quote=LEAD_IN)
    out = render(tool_round(), [rec], "reoffer")
    assert out[-1]["content"] == measured_note(run=MEASURED_REOFFER), out[-1]["content"]
    caller = render([{"role": "user", "content": "I'm ready."}], [rec], "caller")[0]["content"]
    assert f"They heard only \"{LEAD_IN}\"" in caller


def test_no_records_no_note():
    msgs = tool_round()
    assert render(msgs, [], "caller") == msgs and render(msgs, [], "run") == msgs


def test_several_records_are_numbered_oldest_first():
    a, b = hn_record(), Record.make("k2", TOP5_REQ, clean_outcome(TOP5_RAW))
    note = render([{"role": "user", "content": "hi"}], [a, b], "caller")[0]["content"]
    assert note.index(f"1. They asked: \"{HN_REQ}\"") < note.index(f"2. They asked: \"{TOP5_REQ}\"")


# =============================================================== the book

class FakeGate:
    def __init__(self, pending=False):
        self.caller_request_pending = pending
        self.asked = []
        self.quiet = 0.0

    def ask(self, keys, purpose, context):
        self.asked.append((list(keys), purpose))

    def quiet_for(self):
        return self.quiet

    def release_claim(self):
        pass


def book(pending=False, msgs=None, **kw):
    ctx = LLMContext(msgs if msgs is not None else tool_round())
    gate = FakeGate(pending)
    return Owed(ctx, gate=gate, **kw), ctx, gate


class FakeJudge:
    """The cut judge (Owed.judge), scripted: a verdict (True HEARD, False OWED, None),
    or an exception; held until `release` is set, when given."""

    def __init__(self, verdict=True, error=None, release=None):
        self.verdict, self.error, self.release = verdict, error, release
        self.calls = []

    async def __call__(self, question, heard, rest):
        self.calls.append((question, heard, rest))
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.verdict


async def judged(b):
    """Until every verdict the book is waiting for has landed."""
    while b._judging:
        await asyncio.gather(*list(b._judging), return_exceptions=True)


def noted_reply(b, purpose, keys=("call-hn",)):
    r = Reply(purpose, keys if purpose in owed.RUNS else ())
    b.noted(r, keys)
    return r


async def test_landing_rewrites_the_placeholder_once_and_asks_the_run():
    b, ctx, gate = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    tool = ctx.get_messages()[-1]
    assert json.loads(tool["content"]) == {"status": "complete", "answer": HN_CLEAN}
    assert b.record("call-hn").state == OWED and gate.asked == [(["call-hn"], "run")]


async def test_a_landing_with_a_caller_request_to_render_asks_no_run():
    """Owed.tla Land: their request carries the note; the re-offer is armed instead."""
    b, _, gate = book(pending=True)
    b.land(HN_REQ, HN_RAW, "call-hn")
    assert gate.asked == [] and b.record("call-hn").missed


async def test_a_failure_lands_as_its_record():
    b, ctx, _ = book()
    b.land(HN_REQ, None, "call-hn", failure="error", detail="HTTP 402 Payment Required")
    tool = json.loads(ctx.get_messages()[-1]["content"])
    assert tool["status"] == "error" and "billing" in tool["error"]
    b2, ctx2, _ = book()
    b2.land(HN_REQ, None, "call-hn", failure="error", detail="req-140223 failed")
    assert "billing" not in json.loads(ctx2.get_messages()[-1]["content"])["error"]
    b3, ctx3, _ = book()
    b3.land(HN_REQ, "", "call-hn")
    assert json.loads(ctx3.get_messages()[-1]["content"])["status"] == "empty"


async def test_a_heard_reply_closes_and_an_unheard_one_does_not():
    b, _, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    r = noted_reply(b, "caller")
    b.settle(r, LEAD_IN, cut=True, audio=True)
    rec = b.record("call-hn")
    assert rec.state == OWED and rec.heard_quote == LEAD_IN, "a cut lead-in is quoted"
    assert rec.missed, "an audible cut arms the re-offer"
    r2 = noted_reply(b, "caller")
    b.settle(r2, "Tesla Roadster is the top story on Hacker News right now.", cut=False, audio=True)
    assert rec.state == CLOSED


async def test_only_a_reply_that_carried_the_note_counts():
    """A reply rendered before the answer landed names "Tesla" from the model's own
    knowledge: not evidence (redteam-gist #4)."""
    b, _, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.settle(Reply("caller"), "Tesla Roadster, by shekhar101.", cut=False, audio=True)
    assert b.record("call-hn").state == OWED


async def test_a_failure_the_callers_turn_told_is_not_reoffered():
    """Fable's final review (Owed.tla ow_failure_runOnly): the consult fails while the
    caller is mid-turn, so no landing run; their turn tells it in full. Closed, and the
    lull has nothing to say."""
    b, _, gate = book(pending=True, reoffer_quiet_s=0.05)
    b.land(HN_REQ, None, "call-hn", failure="timeout")
    b.settle(noted_reply(b, "caller"), "Sorry, that Hacker News lookup ran out of time, so "
             "I don't have an answer.", cut=False, audio=True)
    assert b.record("call-hn").state == CLOSED
    gate.quiet = 1.0
    await asyncio.sleep(0.3)
    assert gate.asked == [], gate.asked


async def test_a_run_that_ends_without_closing_arms_the_reoffer():
    """REOFFER = "anyMiss" (ow_asWritten_live): a run with no audio."""
    b, _, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.settle(noted_reply(b, "run"), None, cut=False, audio=False)
    assert b.record("call-hn").state == OWED and b.record("call-hn").missed


async def test_the_lull_reoffers_once():
    """Owed.tla Lull, OneReoffer: once per record, however many cuts after."""
    b, _, gate = book(reoffer_quiet_s=0.05)
    b.land(HN_REQ, HN_RAW, "call-hn")
    gate.asked.clear()
    gate.quiet = 1.0
    b.caller_turn()
    await asyncio.sleep(0.4)
    assert gate.asked == [(["call-hn"], "reoffer")], gate.asked
    b.caller_turn()
    b.settle(noted_reply(b, "reoffer"), None, cut=False, audio=False)
    await asyncio.sleep(0.4)
    assert gate.asked == [(["call-hn"], "reoffer")], "a second re-offer"
    assert b.record("call-hn").reoffers == 1


async def test_the_lull_waits_for_quiet_and_for_the_settle():
    """ow_lagSettle: the lull reads the record only once every reply has settled."""
    b, _, gate = book(reoffer_quiet_s=0.05)
    b.land(HN_REQ, HN_RAW, "call-hn")
    gate.asked.clear()
    b.caller_turn()
    gate.quiet = 0.0
    await asyncio.sleep(0.3)
    assert gate.asked == [], "re-offered on a line that is not quiet"
    r = noted_reply(b, "caller")              # a telling, still to settle
    gate.quiet = 1.0
    await asyncio.sleep(0.3)
    assert gate.asked == [], "re-offered ahead of the settle"
    b.settle(r, "It's Tesla Roadster.", cut=False, audio=True)
    await asyncio.sleep(0.3)
    assert gate.asked == [] and b.record("call-hn").state == CLOSED


async def test_readers_wait_for_a_pending_settle():
    b, ctx, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    r = noted_reply(b, "caller")
    view = asyncio.ensure_future(b.view(ctx, "caller"))
    await asyncio.sleep(0.05)
    assert not view.done(), "rendered ahead of the reply's settle"
    b.settle(r, "Tesla Roadster.", cut=False, audio=True)
    out, keys = await view
    assert out is ctx and keys == (), "a closed record still rendered"


async def test_a_reply_that_never_settles_holds_one_render_not_every_one():
    """Past the bound the readers go on without the replies still pending: one lost
    reply must not make every later render wait. One the ledger never saw (unclaimed:
    never played) is settled as heard nothing; one it has still settles at its end,
    and its evidence still counts (it errs owed, never closed)."""
    b, _, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    seen, lost = noted_reply(b, "caller"), noted_reply(b, "caller")
    seen.claimed = True               # the ledger has it: its end is late
    await b.settled(timeout=0.05)
    assert lost.settled and not seen.settled and b.is_settled()
    assert b.record("call-hn").state == OWED
    t0 = asyncio.get_running_loop().time()
    await b.settled(timeout=2.0)
    assert asyncio.get_running_loop().time() - t0 < 0.05, "the next render waited again"
    b.settle(seen, "Tesla Roadster, by shekhar101.", cut=False, audio=True)
    assert b.record("call-hn").state == CLOSED, "the late settle's evidence was dropped"


async def test_a_run_for_a_heard_answer_renders_nothing():
    b, ctx, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.record("call-hn").state = CLOSED
    assert await b.view(ctx, "run", ("call-hn",)) == (None, ())


async def test_the_reoffer_view_offers_stay_silent():
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    b, _, _ = book()
    ctx = LLMContext(tool_round(), tools=ToolsSchema(standard_tools=[FunctionSchema(
        name="get_current_time", description="t", properties={}, required=[])]))
    b.context = ctx
    b.land(HN_REQ, HN_RAW, "call-hn")
    view, _ = await b.view(ctx, "reoffer", ("call-hn",))
    names = [t.name for t in view.tools.standard_tools]
    assert names == ["get_current_time", "stay_silent"], names
    assert [t.name for t in ctx.tools.standard_tools] == ["get_current_time"], "context touched"
    view, _ = await b.view(ctx, "run", ("call-hn",))
    assert "stay_silent" not in [t.name for t in view.tools.standard_tools]


async def test_at_most_three_render_and_the_oldest_is_dropped_not_closed():
    msgs = sum((tool_round(f"k{i}", f"request {i}") for i in range(4)), [])
    b, ctx, _ = book(msgs=msgs)
    for i in range(4):
        b.land(f"request {i}", f"answer {i} Zanzibar{i} Quokka{i}", f"k{i}")
    assert b.record("k0").state == DROPPED, "evicted unheard is DROPPED (DESIGN v3 #10)"
    assert [r.key for r in b.owed()] == ["k1", "k2", "k3"]
    assert "answer 0" in ctx.get_messages()[3]["content"], "its tool result is kept"


async def test_the_session_end_turns_a_running_consults_placeholder_into_its_failure():
    b, ctx, gate = book(msgs=tool_round(placeholder=json.dumps(
        {"status": "working_in_background", "instruction": "will arrive later"})))
    assert b.dispatch("call-hn", HN_REQ) is None
    b.close_session()
    tool = json.loads(ctx.get_messages()[-1]["content"])
    assert tool["status"] == "unknown" and "report back" in tool["error"], tool
    assert b.record("call-hn").state == OWED and gate.asked == [], "asked a run of a dead session"
    b.close_session()   # idempotent
    assert len(b.owed()) == 1


async def test_a_duplicate_dispatch_is_answered_by_the_original():
    msgs = tool_round("a") + tool_round("b")
    b, ctx, _ = book(msgs=msgs)
    assert b.dispatch("a", HN_REQ) is None
    assert b.dispatch("b", HN_REQ) == "a", "a duplicate was dispatched as a consult"
    b.land(HN_REQ, HN_RAW, "a")
    assert json.loads(ctx.get_messages()[3]["content"])["status"] == "complete"
    assert json.loads(ctx.get_messages()[7]["content"])["status"] == "duplicate"
    assert [r.key for r in b.owed()] == ["a"]


# =============================================================== a sync tool's answer (#204)

TOY = ("There's a Toy Joy store at five-five-zero-one Burnet Road in Austin. Toy Joy at "
       "five-five-zero-one Burnet Road, Austin, Texas.")
T_REQ = "Can you look if there are any toy stores on Burnett Road in Austin, Texas?"
# What web_search returned (the shape tools._web_search hands the model).
T_RESULT = json.dumps({"results": [
    {"title": "Toy Joy - Austin's Coolest Toy Store", "url": "https://toyjoy.com/",
     "snippet": "Toy Joy, 5501 Burnet Rd, Austin, TX 78756. Open daily 10am-9pm."}]})
# The 2026-10-18 17:25 weather search (ctx-172557, box journal), cut to its first result:
# titles and snippets, the untrusted-content wrapper -- and no forecast values at all.
W_REQ = "Hey, can you check what the weather is going to be in Austin tomorrow?"
W_RESULT = json.dumps({"results": [
    {"title": "Tomorrow's Weather in Austin - Hourly Forecast and Conditions",
     "snippet": "Get the latest hourly weather updates for Austin tomorrow. Detailed forecast "
                "including temperature, wind, rain, snow, and UV index. Stay informed about "
                "tomorrow's weather conditions in Austin.",
     "site": "\n<<<EXTERNAL_UNTRUSTED_CONTENT id=\"ec645b64e3d7bc48\">>>\nSource: Web Search\n"
             "---\nwww.easeweather.com\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\"ec645b64e3d7bc48\">>>"}]})
W_TOLD = ("Tomorrow in Austin looks warm, with a high around seventy-two and a low near "
          "fifty-five.")
ANSWERS = frozenset({"web_search", "web_fetch", "get_current_time"})


def round_msgs(request=T_REQ, result=T_RESULT, cid="call-ws", tool="web_search", text=None):
    """The context of a sync tool round: the words that asked, the call (after a line,
    with `text`), its result."""
    return [{"role": "user", "content": request},
            {"role": "assistant", "content": text, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": tool, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": cid, "content": result}]


def sync_book(**kw):
    return book(msgs=[], answer_tools=ANSWERS, **kw)


def answer_reply(b, msgs, kind=owed.POST):
    """A reply rendered from `msgs`, as the LLM service registers it at its render."""
    r = Reply(kind)
    b.noted(r, (), msgs)
    return r


async def test_a_sync_answer_cut_before_it_was_heard_is_owed():
    """OwedAnswers.tla (call 3, 2026-10-18 17:27): cut at "There's a Toy Joy store at" by
    "Hold on, hold on.". The whole text is owed to the caller's words, keyed by the
    round's call id, the cut quoted, no run asked, the re-offer armed."""
    b, _, gate = sync_book(reoffer_quiet_s=0.05)
    r = answer_reply(b, round_msgs())
    assert r.fresh == ("call-ws",)
    assert not b.is_settled(), "the next render would not wait for the cut's record"
    b.settle(r, "There's a Toy Joy store at", cut=True, audio=True, text=TOY)
    rec = b.record("call-ws")
    assert rec is not None and rec.state == OWED and rec.outcome == clean_outcome(TOY)
    assert rec.ids == ("call-ws",) and rec.request == T_REQ
    assert rec.heard_quote == "There's a Toy Joy store at" and rec.missed
    note = render([{"role": "user", "content": "Hold on, hold on."}], b.owed(), "caller")
    assert f"They asked: \"{T_REQ}\"" in note[0]["content"] and note[1]["content"] == "Hold on, hold on."
    gate.quiet = 1.0
    await asyncio.sleep(0.3)
    assert gate.asked == [(["call-ws"], "reoffer")], "no landing run, one re-offer"


async def test_the_answers_first_reply_settles_it_by_its_position():
    """The second review's finding 1 (repro_fresh_rule (a)): the 10-18 weather search held
    no forecast values, so no reading of the reply against it could find the telling.
    By position, whatever its words: the paraphrase, a one-term telling, a clarifying
    question -- each, cut, is the record."""
    for text in (W_TOLD, "It'll be sunny.", "Which Austin -- Texas or Minnesota?"):
        b, _, _ = sync_book()
        r = answer_reply(b, round_msgs(W_REQ, W_RESULT))
        b.settle(r, text.split()[0], cut=True, audio=True, text=text)
        assert [(x.outcome, x.request) for x in b.owed()] == [(text, W_REQ)], text


async def test_a_told_answer_is_settled_and_a_later_reply_carries_nothing():
    """The second review's finding 2 (repro_fresh_rule (b)): heard in full, the answer is
    settled in the book, and a later unrelated reply, cut, is no record of it."""
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT)
    b.settle(answer_reply(b, msgs), W_TOLD, cut=False, audio=True, text=W_TOLD)
    later = msgs + [{"role": "assistant", "content": W_TOLD},
                    {"role": "user", "content": "Thanks. Unrelated: can you set a timer later?"}]
    c = answer_reply(b, later, owed.COMMIT)
    assert c.fresh == () and b.is_settled()
    b.settle(c, "Sure, I can", cut=True, audio=True,
             text="Sure, I can do that from the search page content once you're ready.")
    assert b.owed() == []


def test_a_render_files_every_result_it_has_not_seen_wherever_it_lies():
    """A result is new to the book until a render files it, wherever pipecat put the
    line before its call (the third review: that line's message can land after the
    tool-call message). A dict lookup per message, nothing tokenised (the second review's
    per-render cost). A conversation continued after the room's sleep files its old
    results as history."""
    msgs = round_msgs(W_REQ, W_RESULT) + [{"role": "assistant", "content": W_TOLD}]
    late_line = [{"role": "user", "content": "What time is it?"},
                 {"role": "assistant", "content": None, "tool_calls": [
                     {"id": "t", "type": "function",
                      "function": {"name": "get_current_time", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "t", "content": '{"local_time": "3:45 PM"}'},
                 {"role": "assistant", "content": "Let me double-check that for you."}]
    assert owed.new_results(msgs + late_line, {"call-ws": None}) == [
        ("t", "get_current_time", "What time is it?")]
    b, ctx, _ = book(msgs=msgs, answer_tools=ANSWERS)
    b.restore([])
    assert answer_reply(b, msgs + [{"role": "user", "content": "Hi again."}], owed.COMMIT).fresh == ()


async def test_a_sync_answer_cut_after_it_was_heard_makes_no_record():
    """The judge's HEARD: the rest held nothing the caller needed. Its question is the
    caller's words, its heard the pts prefix, its rest the words after it."""
    judge = FakeJudge(True)
    b, _, _ = sync_book(judge=judge)
    heard = "There's a Toy Joy store at five-five-zero-one Burnet Road"
    b.settle(answer_reply(b, round_msgs()), heard, cut=True, audio=True, text=TOY)
    await judged(b)
    assert judge.calls == [(T_REQ, heard, TOY[len(heard):].strip())], judge.calls
    assert b.owed() == [], "a cut after the answer was heard made a record"
    assert answer_reply(b, round_msgs()).fresh == (), "an answer heard by the cut stayed open"


async def test_the_amazon_cut_before_the_price_stays_owed():
    """2026-10-19 23:56:08 (box journal): "Amazon lists the Orin Nano developer kit at two
    hundred" -- the lexical rule on the heard prefix called it heard ("Orin", "developer",
    "kit"; the caller's "Oren" left "orin" a term), and the price, 249, was never told.
    Judged: OWED, the record. The lexical rule on that prefix still reads it as heard --
    which is why a cut is never read by it (formal/Owed.tla prefixLexical)."""
    amazon = "Can you look up the price of an NVIDIA Jetson Oren Nano on Amazon?"
    told = ("Amazon lists the Orin Nano developer kit at two hundred forty-nine dollars, "
            "matching the official MSRP.")
    heard = "Amazon lists the Orin Nano developer kit at two hundred"
    judge = FakeJudge(False)
    b, _, _ = sync_book(judge=judge)
    b.settle(answer_reply(b, round_msgs(amazon)), heard, cut=True, audio=True, text=told)
    await judged(b)
    assert [r.outcome for r in b.owed()] == [told]
    assert judge.calls[0][2].startswith("forty-nine dollars")
    assert Record.make("k", amazon, clean_outcome(told)).closes_on(heard, run=False)


async def test_a_cut_is_closed_by_the_judge_alone():
    """Coverage is lexical, a cut is judged (formal/Owed.tla Closes, Verdicts): a cut
    reply that carried the note closes the record only if its whole text covers it and
    the judge answers HEARD. OWED, no verdict (None, an error, a timeout) err owed; no
    whole text (a cut mid-stream), no heard prefix, or a reply that does not cover the
    record (a deferral) are never judged. The reply stays pending until the verdict."""
    async def cut(judge, heard="Tesla Roadster is the top", text=TELLING):
        b, _, _ = book(judge=judge)
        b.land(HN_REQ, HN_RAW, "call-hn")
        r = noted_reply(b, "caller")
        b.settle(r, heard, cut=True, audio=True, text=text)
        pending = not b.is_settled()
        await judged(b)
        assert b.is_settled()
        return b.record("call-hn").state, pending

    assert await cut(FakeJudge(True)) == (CLOSED, True)
    assert await cut(FakeJudge(False)) == (OWED, True)
    assert await cut(FakeJudge(None)) == (OWED, True)
    assert await cut(FakeJudge(error=RuntimeError("503"))) == (OWED, True)
    assert await cut(None) == (OWED, False), "no judge: errs owed at once"
    for kw in ({"text": None}, {"heard": None}, {"text": "Sure, take your time."}):
        judge = FakeJudge(True)
        assert await cut(judge, **kw) == (OWED, False), kw
        assert judge.calls == [], kw
    # A timeout: the bound is SETTLE_WAIT_S.
    old, owed.SETTLE_WAIT_S = owed.SETTLE_WAIT_S, 0.05
    try:
        assert await cut(FakeJudge(True, release=asyncio.Event())) == (OWED, True)
    finally:
        owed.SETTLE_WAIT_S = old
    # Played through: the lexical rule, no judge.
    judge = FakeJudge(False)
    b, _, _ = book(judge=judge)
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.settle(noted_reply(b, "caller"), TELLING, cut=False, audio=True, text=TELLING)
    assert b.record("call-hn").state == CLOSED and judge.calls == []


async def test_a_late_verdict_reads_the_live_book():
    """The verdict lands after the readers' wait ran out (formal/OwedAnswers.tla LATE):
    a reply rendered meanwhile with the same open answer told it in full -- the late
    OWED makes no record; or the consult record a late HEARD would close was closed by a
    reply played through meanwhile -- it stays closed, nothing reopens or re-offers."""
    release = asyncio.Event()
    b, _, gate = sync_book(judge=FakeJudge(False, release=release), reoffer_quiet_s=0.05)
    gate.quiet = 99.0
    p = answer_reply(b, round_msgs())
    b.settle(p, "There's a", cut=True, audio=True, text=TOY)
    await b.settled(timeout=0.05)                    # the readers go on
    c = answer_reply(b, round_msgs() + [{"role": "user", "content": "Sorry, go on?"}],
                     owed.COMMIT)
    assert c.fresh == ("call-ws",)
    b.settle(c, TOY, cut=False, audio=True, text=TOY)
    release.set()
    await judged(b)
    await asyncio.sleep(0.15)
    assert b.owed() == [] and gate.asked == [], "a late OWED recorded a told answer"
    release = asyncio.Event()
    b, _, gate = book(judge=FakeJudge(True, release=release))
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.settle(noted_reply(b, "caller"), "Tesla Roadster is the top", cut=True, audio=True,
             text=TELLING)
    await b.settled(timeout=0.05)
    b.settle(noted_reply(b, "caller"), TELLING, cut=False, audio=True, text=TELLING)
    assert b.record("call-hn").state == CLOSED
    release.set()
    await judged(b)
    assert b.record("call-hn").state == CLOSED and b.is_settled()
    # ...or dropped meanwhile (three newer answers): a late HEARD does not revive it.
    release = asyncio.Event()
    b, _, gate = book(judge=FakeJudge(True, release=release))
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.settle(noted_reply(b, "caller"), "Tesla Roadster is the top", cut=True, audio=True,
             text=TELLING)
    await b.settled(timeout=0.05)
    for i in range(3):
        b.land(f"Question {i}?", f"Answer {i}: Lisbon {i}.", f"call-{i}")
    assert b.record("call-hn").state == DROPPED
    release.set()
    await judged(b)
    assert b.record("call-hn").state == DROPPED


async def test_a_reply_that_does_not_settle_it_leaves_one_more_then_it_is_history():
    """OwedAnswers.tla partialRecord, noSecondChance: a telling cut before its stream ended is
    a fragment (no record, logged: the STREAMCUT residual), and one cut before its audio
    is heard by nobody; either leaves the answer ONE more reply, which settles it. Two
    replies that did not settle it, and it is history, as before #204."""
    from loguru import logger
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        b, _, _ = sync_book()
        b.settle(answer_reply(b, round_msgs()), "Tomorrow in", cut=True, audio=True, text=None)
    finally:
        logger.remove(sink)
    assert b.owed() == []
    assert any("cut before its stream ended" in x and "STREAMCUT" in x for x in lines), lines
    nxt = answer_reply(b, round_msgs() + [{"role": "user", "content": "Sorry, go on."}],
                       owed.COMMIT)
    assert nxt.fresh == ("call-ws",)
    b.settle(nxt, "There's a", cut=True, audio=True, text=TOY)
    assert [r.key for r in b.owed()] == ["call-ws"]
    b, _, _ = sync_book()
    for _ in range(2):
        b.settle(answer_reply(b, round_msgs()), None, cut=True, audio=False, text=TOY)
    late = answer_reply(b, round_msgs() + [{"role": "user", "content": "Sorry, go on."}],
                        owed.COMMIT)
    assert late.fresh == ()
    b.settle(late, "There's a", cut=True, audio=True, text=TOY)
    assert b.owed() == [], "a third reply settled it"


async def test_an_action_tools_cut_confirmation_is_never_owed():
    """oa_actionOwed (round 1, the reviewer's repro): set_volume, remember and
    switch_voice are no answer tools (tools.Tool.answers), so their confirmation, cut by
    "perfect, thanks", is no record and nothing is re-offered."""
    from teaport_brain.tools import ANSWER_TOOLS
    assert not ANSWER_TOOLS & {"set_volume", "remember", "switch_voice", "end_conversation",
                               "restart_session", "answer_phone_call", "wifi_setup"}
    assert {"web_search", "web_fetch", "search_memory", "get_current_time"} <= ANSWER_TOOLS
    for req, tool, result, text, heard in [
            ("Turn it down to five.", "set_volume", '{"ok": true, "level": 5}',
             "Okay, I've turned the volume down to five.", "Okay, I've"),
            ("Remember that my wife's name is Ann.", "remember",
             '{"saved": true, "fact": "The user\'s wife is named Ann."}',
             "Got it, I'll remember that Ann is your wife.", "Got it,"),
            ("Switch to the British voice.", "switch_voice", '{"ok": true, "voice": "bf_emma"}',
             "Sure, switching to Emma now.", "Sure,")]:
        b, _, gate = book(msgs=[], answer_tools=ANSWER_TOOLS, reoffer_quiet_s=0.05)
        r = answer_reply(b, round_msgs(req, result, tool=tool))
        assert r.fresh == () and b.is_settled(), tool
        b.settle(r, heard, cut=True, audio=True, text=text)
        gate.quiet = 1.0
        await asyncio.sleep(0.15)
        assert b.owed() == [] and gate.asked == [], (tool, b.owed(), gate.asked)


async def test_the_callers_turn_that_reads_a_fresh_result_and_is_cut_is_owed():
    """oa_postOnly (round 1's finding 4): the result landed under the caller's speech,
    their turn dropped the held post-tool request, and the turn's own reply (a COMMIT) is
    the answer's first: its cut is owed like the post-tool reply's."""
    b, _, _ = sync_book()
    msgs = round_msgs() + [{"role": "user", "content": "Oh, and are they open today?"}]
    r = answer_reply(b, msgs, owed.COMMIT)
    assert r.fresh == ("call-ws",)
    b.settle(r, "There's a Toy Joy", cut=True, audio=True,
             text="There's a Toy Joy at 5501 Burnet Road, open today until nine.")
    rec = b.record("call-ws")
    assert rec is not None and rec.state == OWED and rec.request == T_REQ, "who asked is the round's caller"


async def test_a_reply_with_nothing_of_its_own_settles_nothing():
    """The fifth review's J1 (OwedAnswers.tla fillerSettles): the post-tool request
    dropped by "Hold on.", the turn's reply defers. A reply with no terms of its own
    settles nothing -- cut, no record of "Sure, take your time."; heard, the answer is
    not told -- and the reply after it tells the answer: cut, its record. ctx-172525
    (2026-10-18 17:25): "Understood -- let me know when you'd like me to continue.",
    then "Oh, continue." got the telling."""
    for defer, cut in [("Sure, take your time.", True),
                       ("Understood—let me know when you'd like me to continue.", False)]:
        b, _, gate = sync_book(reoffer_quiet_s=0.05)
        gate.quiet = 99.0
        msgs = round_msgs() + [{"role": "user", "content": "Hold on."}]
        b.settle(answer_reply(b, msgs, owed.COMMIT), defer.split()[0] if cut else defer,
                 cut=cut, audio=True, text=defer)
        await asyncio.sleep(0.15)
        assert b.owed() == [] and gate.asked == [], (defer, b.owed())
        msgs += [{"role": "assistant", "content": defer},
                 {"role": "user", "content": "Oh, continue."}]
        told = answer_reply(b, msgs, owed.COMMIT)
        assert told.fresh == ("call-ws",), defer
        b.settle(told, "There's a Toy Joy", cut=True, audio=True, text=TOY)
        assert [r.key for r in b.owed()] == ["call-ws"], defer


async def test_a_caller_turn_in_words_of_their_own_moves_on():
    """The fifth review's J2 (OwedAnswers.tla ownWordsKeep): the weather's post-tool
    reply went unheard; "Never mind. What's the capital of Australia?" moves on. The
    answer to it, cut or heard, is no record of the weather, nor its telling."""
    for cut in (True, False):
        b, _, _ = sync_book()
        msgs = round_msgs(W_REQ, W_RESULT)
        b.settle(answer_reply(b, msgs), None, cut=True, audio=False, text=None)
        msgs += [{"role": "user", "content": "Never mind. What's the capital of Australia?"}]
        c = answer_reply(b, msgs, owed.COMMIT)
        assert c.fresh == ()
        text = "The capital of Australia is Canberra, not Sydney."
        b.settle(c, "The capital" if cut else text, cut=cut, audio=True, text=text)
        assert b.owed() == []


async def test_restating_the_question_does_not_move_on():
    """The final review: words of their own are judged against the open answer's own
    question. Restating it ("Wait, Austin tomorrow?") is no new question, so the answer
    stays open and the reply that tells it, cut, is its record; "Hold on." and "Hello?"
    carry nothing at all (the controls)."""
    for words in ["The weather in Austin, go on.", "Sorry, what about tomorrow's weather?",
                  "Wait, Austin tomorrow?", "Hold on.", "Hello?"]:
        b, _, _ = sync_book()
        msgs = round_msgs(W_REQ, W_RESULT)
        b.settle(answer_reply(b, msgs), None, cut=True, audio=False, text=None)
        c = answer_reply(b, msgs + [{"role": "user", "content": words}], owed.COMMIT)
        assert c.fresh == ("call-ws",), words
        b.settle(c, "Tomorrow in Austin", cut=True, audio=True, text=W_TOLD)
        assert [r.key for r in b.owed()] == ["call-ws"], words


async def test_what_is_still_misread():
    """The residuals left (formal/README.md, OwedAnswers.tla ASIDE), pinned so a change
    is seen: a deferral that says something of its own is taken for the telling (heard:
    told, and the telling after it is no record); a caller turn in words of their own
    that dropped the post-tool request, answered with something else and cut, is the
    dropped answer's record."""
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT) + [{"role": "user", "content": "Hold on."}]
    defer = "Got it, I'll keep the Austin forecast handy for when you're ready."
    b.settle(answer_reply(b, msgs, owed.COMMIT), defer, cut=False, audio=True, text=defer)
    msgs += [{"role": "assistant", "content": defer}, {"role": "user", "content": "Go on."}]
    told = answer_reply(b, msgs, owed.COMMIT)
    assert told.fresh == ()
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT) + [{"role": "user", "content": "What time is it?"}]
    c = answer_reply(b, msgs, owed.COMMIT)
    assert c.fresh == ("call-ws",)
    b.settle(c, "It's three", cut=True, audio=True, text="It's three fifteen in the afternoon.")
    assert [r.outcome for r in b.owed()] == ["It's three fifteen in the afternoon."]


async def test_a_later_rounds_reply_never_settles_an_older_answer():
    """The third review's cases A and C (OwedAnswers.tla keepOpen): a new round ends an
    older open answer at once. The weather's post-tool reply went unheard (one reply
    spent); the caller asks for the volume -- set_volume's round -- and its
    confirmation, cut, is no record of the weather. Or the caller asks about toy stores
    over the weather's dropped post-tool request: the toy answer's cut is the toy
    record, the weather's none."""
    b, _, gate = sync_book(reoffer_quiet_s=0.05)
    gate.quiet = 99.0
    msgs = round_msgs(W_REQ, W_RESULT, cid="w1")
    b.settle(answer_reply(b, msgs), None, cut=True, audio=False, text=None)
    msgs += round_msgs("Turn it down to five.", '{"ok": true, "volume": 5}', cid="v1",
                       tool="set_volume")
    p = answer_reply(b, msgs)
    assert p.fresh == ()
    b.settle(p, "Done, the", cut=True, audio=True, text="Done, the volume is at five now.")
    await asyncio.sleep(0.2)
    assert b.owed() == [] and gate.asked == [], "(A) a confirmation recorded as the weather"
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT, cid="w1") + round_msgs(cid="t1")
    p = answer_reply(b, msgs)
    assert p.fresh == ("t1",)
    b.settle(p, "There's a", cut=True, audio=True, text=TOY)
    assert [(r.key, r.request) for r in b.owed()] == [("t1", T_REQ)]


async def test_a_new_question_asked_while_an_answer_is_owed_is_its_own():
    """The third review's case C (OwedAnswers.tla relookAll): the weather record is owed
    and every request carries its note; the caller asks about toy stores, a new question.
    Its round is its own, and its cut telling a record of its own."""
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT, cid="w1")
    b.settle(answer_reply(b, msgs), "Tomorrow in Austin looks", cut=True, audio=True, text=W_TOLD)
    t_req = "Can you look if there are any toy stores on Burnett Road?"
    msgs += [{"role": "assistant", "content": "Tomorrow in Austin looks"}]
    msgs += round_msgs(t_req, '{"results": [{"title": "Toy Joy - 5501 Burnet Rd"}]}', cid="t1")
    p = Reply(owed.POST)
    b.noted(p, ("w1",), msgs)
    assert p.fresh == ("t1",)
    b.settle(p, "There's a Toy", cut=True, audio=True,
             text="There's a Toy Joy store at fifty-five oh one Burnet Road.")
    assert [(r.key, r.request) for r in b.owed()] == [("w1", W_REQ), ("t1", t_req)]


async def _owed_weather(b):
    """The weather telling cut ("Tomorrow in Austin looks"): its record owed."""
    msgs = round_msgs(W_REQ, W_RESULT, cid="w1")
    b.settle(answer_reply(b, msgs), "Tomorrow in Austin looks", cut=True, audio=True, text=W_TOLD)
    return msgs + [{"role": "assistant", "content": "Tomorrow in Austin looks"}]


def _commit_calls(b, msgs, words, cid):
    """The caller's turn `words` whose reply calls a tool (`cid`), as the LLM service
    registers it: its render, its call."""
    msgs = msgs + [{"role": "user", "content": words}]
    c = Reply(owed.COMMIT)
    b.noted(c, tuple(r.key for r in b.owed()), msgs)
    c.calls = 1
    b.called(c, [cid])
    b.settle(c, None, cut=False, audio=False, text="")
    return msgs


async def test_short_follow_ups_while_a_record_is_owed_are_their_own_questions():
    """The fourth review's N1 (OwedAnswers.tla vagueConsent): while the weather record is
    owed, the caller asks about Toy Joy, the bot offers to check, and the caller answers
    in a few words -- consent, or a short question. Each round is its own: its cut
    telling is a record of its own, for the rest of the session."""
    for words in ["Yes, please.", "Sure, go ahead.", "Please do.", "When do they close?",
                  "What time is it?", "And the day after?"]:
        b, _, _ = sync_book()
        msgs = await _owed_weather(b)
        msgs += [{"role": "user", "content": "Also, is Toy Joy on Burnet open today?"}]
        ask = Reply(owed.COMMIT)
        b.noted(ask, ("w1",), msgs)
        offer = "I'm not sure of their hours. Want me to check?"
        b.settle(ask, offer, cut=False, audio=True, text=offer)
        msgs = _commit_calls(b, msgs + [{"role": "assistant", "content": offer}], words, "h1")
        msgs += round_msgs(words, '{"results": [{"snippet": "Toy Joy hours 10-7"}]}', cid="h1")[1:]
        p = Reply(owed.POST)
        b.noted(p, ("w1",), msgs)
        b.settle(p, "So,", cut=True, audio=True, text="So, Toy Joy is open today from ten to seven.")
        assert sorted(r.key for r in b.owed()) == ["h1", "w1"], words


async def test_a_re_lookup_is_the_turn_right_after_the_records_cut():
    """The second review's finding 5 (ctx-172607): the weather telling was cut by "Hold
    on, hold on.", and the reply to that very turn called web_fetch; the fetched
    answer's telling was cut too. A turn right after the cut, in words with no terms of
    their own: the round is the weather record's re-lookup -- no second record with
    "Hold on, hold on." as its question, its cut evidence for the first. Under two owed
    records, of the one cut last. One turn only: the same words a turn later are their
    own question."""
    b, _, _ = sync_book()
    msgs = await _owed_weather(b)
    msgs = _commit_calls(b, msgs, "Hold on, hold on.", "call-wf")
    msgs += round_msgs(result='{"content": "Tomorrow: high 72."}', cid="call-wf",
                       tool="web_fetch", text="I'm still pulling the details.")[1:]
    told = Reply(owed.POST)
    b.noted(told, ("w1",), msgs)
    assert told.fresh == (), "the re-lookup's result was an answer of its own"
    b.settle(told, "Tomorrow in Austin should be warm and", cut=True, audio=True,
             text="Tomorrow in Austin should be warm and dry, high near seventy-two.")
    assert [(r.key, r.request) for r in b.owed()] == [("w1", W_REQ)]
    # Two owed records: the turn right after the second's cut re-looks it up.
    b, _, _ = sync_book()
    msgs = await _owed_weather(b)
    msgs += round_msgs(cid="t1")
    b.settle(answer_reply(b, msgs), "There's a", cut=True, audio=True, text=TOY)
    msgs = _commit_calls(b, msgs + [{"role": "assistant", "content": "There's a"}], "Hold on.", "f1")
    msgs += round_msgs(result='{"content": "Toy Joy, 5501 Burnet."}', cid="f1", tool="web_fetch")[1:]
    p = Reply(owed.POST)
    b.noted(p, ("w1", "t1"), msgs)
    assert p.fresh == () and [r.key for r in b.owed()] == ["w1", "t1"]
    # Right after the cut, words of their own are a question of their own (case C).
    b, _, _ = sync_book()
    msgs = await _owed_weather(b)
    msgs = _commit_calls(b, msgs, T_REQ, "t1")
    msgs += round_msgs(T_REQ, T_RESULT, cid="t1")[1:]
    p = Reply(owed.POST)
    b.noted(p, ("w1",), msgs)
    assert p.fresh == ("t1",)
    # A turn later the same words are a question of their own.
    b, _, _ = sync_book()
    msgs = await _owed_weather(b)
    msgs += [{"role": "user", "content": "Thanks."}]
    thanks = Reply(owed.COMMIT)
    b.noted(thanks, ("w1",), msgs)
    b.settle(thanks, "Sure!", cut=False, audio=True, text="Sure!")
    msgs = _commit_calls(b, msgs + [{"role": "assistant", "content": "Sure!"}], "Hold on.", "f1")
    msgs += round_msgs(result='{"content": "Toy Joy, 5501 Burnet."}', cid="f1", tool="web_fetch")[1:]
    p = Reply(owed.POST)
    b.noted(p, ("w1",), msgs)
    assert p.fresh == ("f1",)


async def test_a_re_lookups_chain_stays_the_re_lookup():
    """The fifth review's J3: "Hold on, hold on." right after the weather's cut starts a
    re-lookup (web_search), whose reply calls web_fetch. The re-lookup is passed on to
    the call its reply chains: the fetch is no answer to "Hold on, hold on.", and its
    telling, cut, is evidence for the weather record, not a second record."""
    b, _, _ = sync_book()
    msgs = await _owed_weather(b)
    msgs = _commit_calls(b, msgs, "Hold on, hold on.", "s2")
    msgs += round_msgs(result='{"results": []}', cid="s2")[1:]
    p2 = Reply(owed.POST)
    b.noted(p2, ("w1",), msgs)
    p2.calls = 1
    b.called(p2, ["f3"])
    b.settle(p2, None, cut=False, audio=False, text="")
    msgs += round_msgs(result='{"content": "Tomorrow: high 74."}', cid="f3", tool="web_fetch")[1:]
    p3 = Reply(owed.POST)
    b.noted(p3, ("w1",), msgs)
    assert p3.fresh == ()
    b.settle(p3, "Tomorrow in", cut=True, audio=True,
             text="Tomorrow in Austin should be warm and dry, with a high near seventy-four.")
    assert [(r.key, r.request) for r in b.owed()] == [("w1", W_REQ)]


async def test_a_late_settle_opens_no_re_lookup_turn():
    """The fifth review's J8: the post-tool reply's settle is late, and the caller's turn
    right after its cut ("Hold on.") rendered before it. The window opens only if no
    caller turn has rendered since the cut, so the NEXT turn's round ("What time is
    it?" -- or any words) is its own question."""
    b, _, _ = sync_book()
    msgs = round_msgs(W_REQ, W_RESULT, cid="w1")
    p = answer_reply(b, msgs)
    p.claimed = True
    await b.settled(timeout=0.05)
    msgs += [{"role": "assistant", "content": "Tomorrow in"}, {"role": "user", "content": "Hold on."}]
    c = answer_reply(b, msgs, owed.COMMIT)
    b.settle(c, None, cut=False, audio=False, text="")
    b.settle(p, "Tomorrow in", cut=True, audio=True, text=W_TOLD)
    assert [r.key for r in b.owed()] == ["w1"]
    msgs += [{"role": "assistant", "content": "Sure."}, {"role": "user", "content": "Okay."}]
    c2 = Reply(owed.COMMIT)
    b.noted(c2, ("w1",), msgs)
    assert c2.relook is None, "a late settle opened the re-lookup turn a turn late"


async def test_a_telling_that_calls_again_is_no_record_and_its_chain_is_one_answer():
    """Position only (formal/README.md, the accepted degradations): "There's a Toy Joy at
    5501 Burnet. Let me get their hours." + web_fetch calls again, so it settles
    nothing -- cut, no record; the answer's next reply settles it. "Let me get their
    hours." + web_fetch heard: the chain's result comes next, and the reply after it
    settles both results as one answer."""
    b, _, _ = sync_book()
    told = answer_reply(b, round_msgs())
    told.calls = 1
    b.settle(told, "There's a", cut=True, audio=True,
             text="There's a Toy Joy at 5501 Burnet Road. Let me get their hours.")
    assert b.owed() == []
    again = answer_reply(b, round_msgs() + [{"role": "user", "content": "Sorry, go on."}],
                         owed.COMMIT)
    b.settle(again, "There's a", cut=True, audio=True, text=TOY)
    assert [r.key for r in b.owed()] == ["call-ws"]
    b, _, _ = sync_book()
    msgs = round_msgs()
    line = answer_reply(b, msgs)
    line.calls = 1
    b.called(line, ["call-wf"])
    b.settle(line, "Let me get their hours.", cut=False, audio=True, text="Let me get their hours.")
    assert b.owed() == []
    msgs += round_msgs(result='{"content": "Hours: 10am to 9pm daily."}', cid="call-wf",
                       tool="web_fetch", text="Let me get their hours.")[1:]
    both = answer_reply(b, msgs)
    assert both.fresh == ("call-ws", "call-wf"), both.fresh
    b.settle(both, "Toy Joy", cut=True, audio=True,
             text="Toy Joy on Burnet Road is open from ten to nine every day.")
    rec = b.owed()[0]
    assert rec.ids == ("call-ws", "call-wf") and rec.key == "call-ws"


async def test_two_sync_answers_asked_with_the_same_words_are_two_records():
    """Round 1's finding 5 (repro case C): keyed by the round's call ids, not the words --
    two answers to "Yes, please." were one record, the second silently replacing the
    first."""
    b, _, _ = sync_book()
    for cid, result, text, heard in [
            ("call-1", '{"results": [{"snippet": "The pharmacy on Lamar closes at 9pm."}]}',
             "The pharmacy on Lamar closes at nine tonight.", "The pharmacy"),
            ("call-2", '{"results": [{"snippet": "Austin tomorrow: warm, high 72, low 55."}]}',
             "Tomorrow looks warm with highs near seventy-two.", "Tomorrow")]:
        r = answer_reply(b, round_msgs("Yes, please.", result, cid=cid))
        b.settle(r, heard, cut=True, audio=True, text=text)
    assert [r.key for r in b.owed()] == ["call-1", "call-2"], [r.key for r in b.owed()]


async def test_a_round_nobody_asked_for_is_no_record_of_theirs():
    """Round 1's finding 6: the request is the latest user message of the round, and ""
    when the brain's own notice or a wake cue started it (a client note's reaction, the
    greeting) -- never an older, unrelated caller line. A caller's round after it ends
    it, and its own cut is the caller's record (the third review's finding 4)."""
    notice = ([{"role": "user", "content": "What's the weather like?"},
               {"role": "assistant", "content": "It's sunny and warm."},
               {"role": "user", "content": f"{SYSTEM_NOTICE_TAG}\nThe user opened the map."}]
              + round_msgs()[1:])
    assert owed.new_results(notice, {}) == [("call-ws", "web_search", "")]
    b, _, _ = sync_book()
    b.settle(answer_reply(b, notice), "There's a", cut=True, audio=True, text=TOY)
    assert b.owed() == []
    b, _, _ = sync_book()
    b.settle(answer_reply(b, notice), None, cut=True, audio=False, text=None)
    msgs = notice + round_msgs(W_REQ, W_RESULT, cid="w1")
    p = answer_reply(b, msgs)
    assert p.fresh == ("w1",)
    b.settle(p, "Tomorrow in Austin looks", cut=True, audio=True, text=W_TOLD)
    assert [(r.key, r.request) for r in b.owed()] == [("w1", W_REQ)]


async def test_a_late_settle_reads_the_live_book():
    """The second review's finding 4 (repro_late_settle; OwedAnswers.tla lateReplace,
    lateNoEvidence): the post-tool reply P is cut, the readers' wait runs out, and the
    caller's turn C renders with the same open answer. (a) C tells it in full: P's late
    cut makes no record. (b) C is cut too and makes the record, its re-offer spent: P's
    late cut neither replaces it nor resets its re-offer. (c) P's late settle makes the
    record while C plays: C's full telling closes it."""
    async def scenario(c_end, p_first=False):
        b, _, gate = sync_book(reoffer_quiet_s=0.05)
        b.gate.quiet = 99.0
        p = answer_reply(b, round_msgs())
        p.claimed = True
        await b.settled(timeout=0.05)
        ctx2 = round_msgs() + [{"role": "assistant", "content": "There's a"},
                               {"role": "user", "content": "Sorry, go on?"}]
        c = answer_reply(b, ctx2, owed.COMMIT)
        assert c.fresh == ("call-ws",)
        if p_first:
            b.settle(p, "There's a", cut=True, audio=True, text=TOY)
        c_end(b, c)
        await asyncio.sleep(0.4)
        if not p_first:
            b.settle(p, "There's a", cut=True, audio=True, text=TOY)
            await asyncio.sleep(0.4)
        return b, gate

    b, gate = await scenario(lambda b, c: b.settle(c, TOY, cut=False, audio=True, text=TOY))
    assert b.owed() == [] and gate.asked == [], "(a) a told answer was recorded late"
    b, gate = await scenario(lambda b, c: b.settle(c, "There's a Toy", cut=True, audio=True,
                                                   text=TOY))
    assert [(r.key, r.reoffers) for r in b.owed()] == [("call-ws", 1)]
    assert gate.asked == [(["call-ws"], "reoffer")], "(b) a second re-offer"
    b, gate = await scenario(lambda b, c: b.settle(c, TOY, cut=False, audio=True, text=TOY),
                             p_first=True)
    assert b.owed() == [] and b.record("call-ws").state == CLOSED and gate.asked == [], "(c)"


async def test_a_settle_timeout_still_records_a_cut_sync_answer():
    """Round 1's finding 7 (repro case D): past the bound the readers go on, and the sync
    reply the ledger has still settles at its end -- its cut is still a record (errs
    owed). Before, the timeout settled it as heard nothing and its answer was lost."""
    b, _, _ = sync_book()
    r = answer_reply(b, round_msgs())
    r.claimed = True
    await b.settled(timeout=0.05)
    assert b.is_settled() and not r.settled and b.owed() == []
    b.settle(r, "There's a Toy Joy store at", cut=True, audio=True, text=TOY)
    assert [x.key for x in b.owed()] == ["call-ws"]


async def test_a_reply_that_put_a_call_out_holds_no_render():
    """Round 1's finding 9 (repro case B): the next round's request waited up to
    SETTLE_WAIT_S for a reply that called again, though its round's answer comes after
    it and a cut of it cancels the call. One whose request carried the note is still
    waited for: its telling is evidence."""
    b, ctx, _ = sync_book()
    r = answer_reply(b, round_msgs())
    r.calls = 1
    b.called(r, ["call-wf"])
    assert b.is_settled(), "the next round's render would wait for the line"
    b2, ctx2, _ = book()
    b2.land(HN_REQ, HN_RAW, "call-hn")
    noted = noted_reply(b2, "caller")
    noted.calls = 1
    b2.called(noted, ["call-x"])
    assert not b2.is_settled(), "a reply carrying the note stopped being evidence"


async def test_only_an_open_answer_is_owed():
    """A reply with no open answer in the book (a commit's own exchange) is no record;
    one that played through is none either."""
    b, _, _ = sync_book()
    commit = answer_reply(b, [{"role": "user", "content": "Tell me about Toy Joy."}], owed.COMMIT)
    b.settle(commit, "There's a", cut=True, audio=True, text=TOY)
    assert b.owed() == []
    played = answer_reply(b, round_msgs())
    b.settle(played, TOY, cut=False, audio=True, text=TOY)
    assert b.owed() == [], "an answer that played through was made a record"


# =============================================================== the room's sleep

async def test_the_records_sleep_and_wake_with_the_conversation():
    store = wake_gate.MicConversation()
    b, ctx, _ = book()
    b.land(HN_REQ, HN_RAW, "call-hn")
    b.record("call-hn").heard_quote = LEAD_IN
    g = wake_gate.WakeGate(["hey teaport"], asleep=False, store=store)
    g.context, g.owed = ctx, b
    g.ended()
    assert store.records and store.records[0]["key"] == "call-hn"

    b2, ctx2, _ = book(msgs=[{"role": "system", "content": "persona"}])
    g2 = wake_gate.WakeGate(["hey teaport"], asleep=True, store=store)
    g2.context, g2.owed = ctx2, b2
    pushed = []

    async def push(f):
        pushed.append(f)
    await g2.final("hey teaport, I'm ready", push)
    rec = b2.record("call-hn")
    assert rec is not None and rec.state == OWED and rec.heard_quote == LEAD_IN
    note = render(ctx2.get_messages() + [{"role": "user", "content": "I'm ready"}],
                  b2.owed(), "caller")
    assert any("Tesla Roadster" in (m.get("content") or "") for m in note
               if isinstance(m.get("content"), str) and m["content"].startswith(SYSTEM_NOTICE_TAG))


# =============================================================== the settle feed

def _audio(secs, sr=24000):
    return TTSAudioRawFrame(b"\x00\x00" * int(secs * sr), sr, 1)


def _word(s, pts_s):
    f = TTSTextFrame(s, aggregated_by=AggregationType.WORD)
    f.pts = int(pts_s * 1e9)
    return f


TTS = object()   # the session's TTS, as the ledger is built with it: frames sighted there


async def _ledger(seq, reply, tts=None):
    """A ledger fed `seq`; with `tts`, built as the session builds it and every frame
    sighted at the TTS (a reply's synthesis is over only at its own TTSStoppedFrame)."""
    ledger = TranscriptLedger(tts=tts)
    got = []
    ledger.on_reply_end = lambda r, heard, *, cut, audio, text: got.append((r, heard, cut, audio, text))
    start = LLMFullResponseStartFrame()
    start.owed_reply = reply
    ledger.feed = lambda more: _feed_ledger(ledger, more, tts)
    await ledger.feed([(start, 0.0)] + seq)
    return ledger, got


async def _feed_ledger(ledger, seq, tts):
    for f, t in seq:
        await ledger.on_process_frame(SimpleNamespace(frame=f, timestamp=int(t * 1e9),
                                                      processor=tts))


TELLING = "Tesla Roadster is the top story on Hacker News right now, posted by shekhar101."


async def test_the_feed_reads_the_pts_prefix_not_the_ledgers_heard_text():
    """ow_ledgerMax: the ledger's heard_text is the LONGER of the pts prefix and an
    audio-fraction estimate; the record must read the pts prefix only."""
    reply = Reply("caller")
    ledger, got = await _ledger([
        (LLMTextFrame(TELLING), 0.0), (LLMFullResponseEndFrame(), 0.0),
        (TTSStartedFrame(), 1.0), (_audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.0),
        (_word("Tesla", 1.1), 1.05), (_word("Roadster", 1.6), 1.05), (_word("is", 1.9), 1.05),
        (InterruptionFrame(), 1.7)], reply)
    u = [e for e in ledger.events if e.speaker == "assistant"][-1]
    assert "Roadster" in u.heard_text, f"the control: the estimate reads more ({u.heard_text!r})"
    assert got == [(reply, "Tesla", True, True, TELLING)], got
    assert not hn_record().closes_on(got[0][1], run=False)
    assert hn_record().closes_on(u.heard_text, run=False), "the control would close it"


async def test_a_reply_that_played_through_settles_at_its_own_window_close():
    """ow_lagSettle: at the BotStopped its own audio played out in, not at the next bot
    activity (the 2026-09-11 14:23 loss)."""
    reply = Reply("caller")
    _, got = await _ledger([
        (LLMTextFrame(TELLING), 0.0), (LLMFullResponseEndFrame(), 0.0),
        (TTSStartedFrame(), 1.0), (_audio(3.0), 1.0), (TTSStoppedFrame(), 1.0),
        (BotStartedSpeakingFrame(), 1.0), (BotStoppedSpeakingFrame(), 4.0)], reply)
    assert got == [(reply, TELLING, False, True, TELLING)], got


def _ctx(frame, ctx):
    frame.context_id = ctx
    return frame


async def test_a_bot_stop_mid_reply_settles_nothing():
    """The transport's 3 s idle fallback sends BotStoppedSpeaking when the TTS stalls
    mid-reply: the reply is not over until its synthesis is done and all of it played.
    Settled there, it would close on words not yet spoken (ClosedOnEvidence)."""
    reply = Reply("caller")
    ledger, got = await _ledger([
        (LLMTextFrame(TELLING), 0.0), (LLMFullResponseEndFrame(), 0.0),
        (TTSStartedFrame(context_id="r"), 1.0), (_ctx(_audio(1.0), "r"), 1.0),
        (BotStartedSpeakingFrame(), 1.0), (BotStoppedSpeakingFrame(), 5.0)], reply, TTS)
    assert got == [], f"settled on a stall: {got}"
    await ledger.feed([(_ctx(_audio(1.0), "r"), 6.0), (TTSStoppedFrame(context_id="r"), 6.0),
                       (BotStartedSpeakingFrame(), 6.0), (BotStoppedSpeakingFrame(), 7.0)])
    assert got == [(reply, TELLING, False, True, TELLING)], got


async def test_a_filler_lines_end_settles_nothing():
    """A tool-ack (append_to_context=False) plays first, the reply queued behind it: the
    transport's stop at the filler's end is not the reply's (the gate's one-context
    watch this replaced took whatever context came last)."""
    reply = Reply("caller")
    ledger, got = await _ledger([
        (LLMTextFrame(TELLING), 0.0), (LLMFullResponseEndFrame(), 0.0),
        (TTSStartedFrame(context_id="f", append_to_context=False), 1.0),
        (_ctx(_audio(1.0), "f"), 1.0), (TTSStoppedFrame(context_id="f"), 1.0),
        (TTSStartedFrame(context_id="r"), 1.0), (_ctx(_audio(1.0), "r"), 1.0),
        (BotStartedSpeakingFrame(), 1.0), (BotStoppedSpeakingFrame(), 2.0)], reply, TTS)
    assert got == [], f"the filler's end settled the reply: {got}"
    await ledger.feed([(TTSStoppedFrame(context_id="r"), 2.0),
                       (BotStoppedSpeakingFrame(), 3.0)])
    assert got == [(reply, TELLING, False, True, TELLING)], got


async def test_a_reply_cut_after_its_last_word_was_heard_to_its_end():
    """Every word's pts before the cut: the reply heard to its end (Owed.tla g = "all"
    for a cut), which closes a failure the way playing through does."""
    reply = Reply("run")
    _, got = await _ledger([
        (LLMTextFrame("It timed out."), 0.0), (LLMFullResponseEndFrame(), 0.0),
        (TTSStartedFrame(), 1.0), (_audio(3.0), 1.0), (BotStartedSpeakingFrame(), 1.0),
        (_word("It", 1.1), 1.05), (_word("timed", 1.3), 1.05), (_word("out.", 1.5), 1.05),
        (InterruptionFrame(), 2.5)], reply)
    assert got == [(reply, "It timed out.", False, True, "It timed out.")], got


async def test_a_reply_with_no_text_settles_as_no_audio():
    reply = Reply("run")
    _, got = await _ledger([(LLMFullResponseEndFrame(), 0.0)], reply)
    assert got == [(reply, None, False, False, "")]


async def test_a_reply_cut_before_its_tts_opened_was_heard_nothing():
    """#79: the cut came while the TTS was still gathering its first clause."""
    reply = Reply("run")
    _, got = await _ledger([(LLMTextFrame("Tesla Roadster is"), 0.0),
                            (InterruptionFrame(), 0.5)], reply)
    assert got == [(reply, None, True, False, None)], got


async def test_a_reply_cut_mid_stream_settles_its_heard_prefix_and_no_text():
    """The ledger's `text` is the reply's whole text once its stream ended, and None when
    it was cut before that, its audio already playing (Owed.tla STREAMCUT): the parts so
    far are a fragment, not the answer a cut telling is owed as (partialRecord)."""
    reply = Reply(owed.POST)
    _, got = await _ledger([
        (LLMTextFrame("Tomorrow in Austin looks warm "), 0.0),
        (TTSStartedFrame(), 0.5), (_audio(3.0), 0.5), (BotStartedSpeakingFrame(), 0.5),
        (_word("Tomorrow", 0.6), 0.55), (_word("in", 1.0), 0.55),
        (LLMTextFrame("with highs near"), 0.7), (InterruptionFrame(), 0.9)], reply)
    assert got == [(reply, "Tomorrow", True, True, None)], got


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")
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
