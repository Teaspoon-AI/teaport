#
# test_owed_note_eval.py -- how a landed consult is told (#387): the note owed.py renders,
# replayed against the live model. OPT-IN: a live LLM, many calls. Skipped unless
# TEAPORT_EVAL_OWED_NOTE is set and the model's endpoint is reachable (appliance.py), so
# the suite reports it skipped.
#
#   cd ~/<checkout>/brain && TEAPORT_EVAL_OWED_NOTE=1 \
#       TEAPORT_EVAL_ENV_FILE=/etc/teaport/brain.env \
#       PYTHONPATH=$PWD /opt/teaport/venv/bin/python tests/test_owed_note_eval.py
#
# [TEAPORT_EVAL_TRIALS=10] [TEAPORT_EVAL_CASES=name,name] [TEAPORT_EVAL_SYSTEM_FILE=x.json]
#
# Each case is a room conversation in the shape the box's contexts have (invented content:
# no conversation from a box goes into the repo): the greeting, the caller's question, the
# consult's call, its tool result -- and then the request the brain makes. That request is
# built by the code itself: a real owed.Owed books the consult (dispatch), lands it (land:
# the tool result rewritten, failure_outcome's wording) and renders the request (view: the
# note, at the tail with the run line for the landing run, before the caller's words for
# their turn). The system prompt is the brain's own persona and tools paragraph for the
# room (build_system_prompt), or TEAPORT_EVAL_SYSTEM_FILE: a JSON list of the system
# messages a live context had (the host's instructions; kept out of the repo). The tools
# are the room's. Each request goes to the brain's model, routing and key
# (services.make_llm()'s client) TEAPORT_EVAL_TRIALS times.
#
# What #387 found on the box (2026-10-30..11-09):
#   - the answer run line's "then say what it answers (\"<answer> is …\")" was read out:
#     "... This answers your request for ..." about 50 times in ten days;
#   - a failure has no answer to lead with, and failure_outcome's hedge ("ask them to check
#     rather than saying it failed") rode on every failure, lookups included: 5 of 7 timed
#     out lookups were told as "I don't have the results (yet)" or "No results were
#     returned" -- that nothing exists, or that it is still running.
#
# Each reply is classified by its words (test-only regular expressions, English, Spanish
# and Italian):
#   answer cases  -- `lead` (the answer in the first sentence), `instruction` (the run
#                    line's shape spoken: "this answers your request"), `other`;
#   failure cases -- `told` (it did not get done, and why: the reason the outcome gives),
#                    `no reason` (that it failed, not why), `nothing exists` ("no results",
#                    "I couldn't find any"), `still running` ("yet", "still waiting":
#                    contradicts the outcome), `lookup` (a new consult), `other`.
# It reports per case, with samples; it fails if any answer case speaks the instruction,
# or a failure run tells it other than `told` in more than a tenth of the trials.
#
# Measured 2026-11-09 (gpt-oss-120b on Groq, the box's live system prompt, 20 trials;
# the texts before #387 -> the texts it shipped; owed.py's RUN_LINE note has the rest):
#   run_answer_list         instruction 7/20 -> 0/20     (lead 20/20 both, fact too)
#   run_timeout_lookup      told 3/20 -> 20/20           (before: "no results", "not yet")
#   run_error_lookup        told 0/20 -> 20/20
#   run_credit              told 20/20 -> 20/20
#   run_timeout_action      told 8/20 -> 20/20
#   ask_timeout_lookup      told 1/20 -> 15/20           (before: 14/20 "yet")
#   ask_status_two_failed   told 0/20 -> 20/20           (before: 20/20 "still waiting")
#   reoffer_timeout_lookup  told 9/20 -> 18/20
#   run_timeout_lookup_es   told 1/20 -> 20/20
#   run_timeout_lookup_it   told 2/20 -> 20/20
# Candidates dropped on the way (10 trials): the hedge reworded alone (lookups 2-4/10
# told); the new run lines without the line under a failure's result (the status question
# still 9/10 "still waiting"); a failure run line with a quoted example ("Sorry, I couldn't
# get that: it ran out of time."), copied word for word, an error told as a timeout 9/10.
#
import asyncio
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_OWED_NOTE", "an opted-in live evaluation of the owed note")
appliance.load_env_file()                  # before anything reads a setting

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from teaport_brain import owed, services, tools  # noqa: E402
from teaport_brain.persona import build_system_prompt, load_persona  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402

appliance.require_reachable(setting("LLM_BASE_URL") or "", "the language model's endpoint")
TRIALS = int(os.getenv("TEAPORT_EVAL_TRIALS") or "10")

ROOM = tools.ToolContext(has_tts=True, tts=object(), front_end="room", client_features=frozenset(
    "volume,restart,local,phonemes,mood,typing,sleep".split(",")))
TOOLS = OpenAILLMAdapter().to_provider_tools_format(tools.build_tools_schema(ROOM))
CONSULT = tools.OPENCLAW_AGENT_CONSULT.name
GREETING = [{"role": "user", "content": "(The call/session just connected. Greet me warmly "
                                        "in one short sentence.)"},
            {"role": "assistant", "content": "Hi there! What can I do for you?"}]
LANGS = {"es": "Spanish", "it": "Italian"}


def system(lang=None) -> list:
    path = os.getenv("TEAPORT_EVAL_SYSTEM_FILE")
    msgs = (json.load(open(path)) if path else
            [{"role": "system", "content": build_system_prompt(
                load_persona(), tools=tools.active_tools(ROOM))}])
    if lang:
        msgs = msgs + [{"role": "system", "content": f"Always reply to the user in {LANGS[lang]}."}]
    return msgs


# ---- the consults: (the caller's words, the model's request, the outcome) -------------
# outcome: the agent's answer text, or ("failure", kind, detail) as the consult reports it.
CONSULTS = {
    "hours": ("Can you ask the agent when the Alder Street library opens on Saturday?",
              "When does the Alder Street library open on Saturdays?",
              "The Alder Street Library opens at 9 a.m. on Saturdays and closes at 5 p.m."),
    "shops": ("Ask the desktop agent to find bookshops on Elm Street in Riverton.",
              "Find bookshops on Elm Street in Riverton.",
              "Bookshops on Elm Street, Riverton:\n\n- **Paper Lantern Books** — 14 Elm St\n"
              "- **The Quiet Shelf** — 230 Elm St\n- **Elm Street Comics & Books** — 402 Elm St"),
    "shops_timeout": ("Ask the desktop agent to find bookshops on Elm Street in Riverton.",
                      "Find bookshops on Elm Street in Riverton.",
                      ("failure", "timeout", "no result within 130s")),
    "train_error": ("Can you have the agent check when the next train to Millbrook leaves?",
                    "When does the next train to Millbrook leave?",
                    ("failure", "error", "gateway unreachable (connection refused)")),
    "credit": ("Ask the agent what's on my calendar tomorrow.",
               "What's on the user's calendar tomorrow?",
               ("failure", "error", "HTTP 402: insufficient credits on this account")),
    "message_timeout": ("Tell the agent to send Sam a message on Discord saying I'll be late.",
                        "Send Sam a Discord message: I'll be about twenty minutes late.",
                        ("failure", "timeout", "no result within 130s")),
    "pharmacy_timeout": ("Ask the agent to find pharmacies open late near Oak Avenue.",
                         "Find pharmacies near Oak Avenue that are open late tonight.",
                         ("failure", "timeout", "no result within 130s")),
    "cafes_timeout": ("Can the agent find cafés on Birch Lane?",
                      "Find cafés on Birch Lane.",
                      ("failure", "timeout", "no result within 130s")),
}


def _key(name):
    return f"call-{name}"


async def build(consults, landed, *, after=(), purpose=owed.RUN, lang=None, said=()):
    """The request the brain makes: `consults` asked in turn (each its own call, with its
    placeholder), `landed` of them landed (in order), then the caller's words `after`, and
    the render for `purpose`: the landing run for the last landed one, or the caller's
    turn. Returns the messages, as the model would get them."""
    ctx = LLMContext(system(lang) + [dict(m) for m in GREETING])
    book = owed.Owed(ctx, gate=None, settings=True)
    for name in consults:
        words, request, _ = CONSULTS[name]
        ctx.add_messages([
            {"role": "user", "content": words},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": _key(name), "type": "function",
                "function": {"name": CONSULT, "arguments": json.dumps({"question": request})}}]},
            {"role": "tool", "tool_call_id": _key(name), "content": json.dumps({
                "status": "working_in_background",
                "instruction": ("The task is running in the background and nothing has "
                                "come back yet. Do not respond now. " + tools._PENDING_RULE)})}])
        book.dispatch(_key(name), request)
    for name in landed:
        _, request, outcome = CONSULTS[name]
        if isinstance(outcome, tuple):
            book.land(request, None, _key(name), failure=outcome[1], detail=outcome[2])
        else:
            book.land(request, outcome, _key(name))
    for role, words in said:                 # what was said since, as (role, words)
        ctx.add_message({"role": role, "content": words})
    for words in after:
        ctx.add_message({"role": "user", "content": words})
    keys = (_key(landed[-1]),) if purpose in owed.RUNS else ()
    view, _ = await book.view(ctx, purpose, keys)
    return view.get_messages()


# ---- the cases: (name, kind, what it is about, how to build it) -----------------------
# kind: "answer" | a failure's reason ("timeout", "error", "credit") | "status".
CASES = [
    ("run_answer_fact", "answer", "hours", lambda: build(["hours"], ["hours"])),
    ("run_answer_list", "answer", "shops", lambda: build(["shops"], ["shops"])),
    ("run_timeout_lookup", "timeout", "shops_timeout",
     lambda: build(["shops_timeout"], ["shops_timeout"])),
    ("run_error_lookup", "error", "train_error", lambda: build(["train_error"], ["train_error"])),
    ("run_credit", "credit", "credit", lambda: build(["credit"], ["credit"])),
    ("run_timeout_action", "timeout", "message_timeout",
     lambda: build(["message_timeout"], ["message_timeout"])),
    ("ask_timeout_lookup", "timeout", "shops_timeout",
     lambda: build(["shops_timeout"], ["shops_timeout"], after=["What about the bookshops?"],
                   purpose="caller")),
    ("ask_status_two_failed", "status", "pharmacy_timeout",
     lambda: build(["pharmacy_timeout", "cafes_timeout", "hours"],
                   ["pharmacy_timeout", "cafes_timeout"],
                   after=["How are those searches going?"], purpose="caller")),
    # The lull's one re-offer, after the landing run was cut at its first words by a caller
    # who asked it to wait, came back and thanked it for something else.
    ("reoffer_timeout_lookup", "timeout", "shops_timeout",
     lambda: build(["shops_timeout"], ["shops_timeout"], purpose=owed.REOFFER, said=[
         ("assistant", "I wasn't able"), ("user", "Hold on a second."),
         ("assistant", "Sure, take your time."), ("user", "Okay, I'm back. Thanks."),
         ("assistant", "You're welcome!")])),
    ("run_timeout_lookup_es", "timeout", "shops_timeout",
     lambda: build(["shops_timeout"], ["shops_timeout"], lang="es")),
    ("run_timeout_lookup_it", "timeout", "shops_timeout",
     lambda: build(["shops_timeout"], ["shops_timeout"], lang="it")),
]

# ---- the classes ----------------------------------------------------------------------
INSTRUCTION = re.compile(
    r"\b(this|that|which) (answer|answers|reply|response)\b|\b(answers|addresses|answering|"
    r"addressing) (your|the) (request|question)\b|\banswer to your (request|question)\b|"
    r"\bresponde a tu (solicitud|pregunta)\b|\brisposta alla tua (richiesta|domanda)\b|"
    r"\brisponde alla tua (richiesta|domanda)\b|<answer>", re.I)
REASON = {
    "timeout": re.compile(r"tim(e[sd]?|ing) ?out|ran out of time|time ran out|too long|in time\b|out of time|"
                          r"tiempo|tard[óo] demasiado|se agot[óo]|expir|tempo|troppo|scadut", re.I),
    "error": re.compile(r"error|went wrong|problem|glitch|couldn't (reach|connect)|"
                        r"connect|technical|fall[óo]|errore|problema|guasto", re.I),
    "credit": re.compile(r"credit|cr[ée]dito|billing|account", re.I),
}
STILL = re.compile(r"\byet\b|still (waiting|working|looking|searching|checking|running|on)|"
                   r"in progress|on (its|the) way|any (moment|minute)|todav[ií]a|a[uú]n no|"
                   r"ancora", re.I)
NONE = re.compile(r"\bno (results?|matches|listings?|answer)\b|\bnone\b|(couldn't|could not|"
                  r"didn't|did not) find|nothing (came up|was found|turned up|came back)|"
                  r"don't have (any|that|the|a)|aren't any|no hay|no (se )?encontr|"
                  r"no tengo|nessun|non ho trovato|non ho", re.I)
FAILED = re.compile(r"couldn't|could not|wasn't able|was not able|unable|didn't (finish|"
                    r"complete|go through|get)|failed|fail|no pude|no se pudo|no he podido|"
                    r"non sono riuscit|non è stato possibile|non ho potuto", re.I)
ANSWER = {"hours": re.compile(r"\b9\b|nine", re.I),
          "shops": re.compile(r"paper lantern|quiet shelf|elm street comics", re.I)}


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


def classify(kind: str, about: str, msg: dict) -> str:
    if msg.get("tool_calls"):
        names = [c["function"]["name"] for c in msg["tool_calls"]]
        return ("lookup" if CONSULT in names else "stay silent" if owed.STAY_SILENT in names
                else "call:" + ",".join(names))
    text = (msg.get("content") or "").strip().replace("\u2019", "'")
    if not text:
        return "empty"
    if INSTRUCTION.search(text):
        return "instruction"
    if kind == "answer":
        return "lead" if ANSWER[about].search(first_sentence(text)) else "other"
    if kind == "status":
        # The two that failed must be told as failed; the third may still be running.
        return ("told" if REASON["timeout"].search(text) and not re.search(
                    r"still (waiting|working) on (those|them|all|both|the searches)", text, re.I)
                else "still running" if STILL.search(text) else "no reason")
    if REASON[kind].search(text) and not STILL.search(text):
        return "told"
    if STILL.search(text):
        return "still running"
    if NONE.search(text):
        return "nothing exists"
    return "no reason" if FAILED.search(text) else "other"


async def run(cases=None, trials=TRIALS):
    """{case name: (Counter of classes, [(class, reply text)])}."""
    llm = services.make_llm()
    extra = services._llm_extra()
    cap = services._max_tokens()
    sem = asyncio.Semaphore(int(os.getenv("TEAPORT_EVAL_CONCURRENCY") or "8"))

    async def ask(messages, offered):
        async with sem:
            for attempt in range(3):
                try:
                    r = await asyncio.wait_for(llm._client.chat.completions.create(
                        model=setting("LLM_MODEL"), messages=messages, tools=offered,
                        **({"max_tokens": cap} if cap else {}),
                        **{k: v for k, v in extra.items() if k != "extra_body"},
                        extra_body=extra.get("extra_body")), 60)
                    return r.choices[0].message.model_dump()
                except Exception:  # noqa: BLE001 -- a provider hiccup: try again
                    if attempt == 2:
                        return {"content": ""}
                    await asyncio.sleep(2)

    out = {}
    chosen = [c for c in CASES if cases is None or c[0] in cases]
    reoffer_tools = TOOLS + OpenAILLMAdapter().to_provider_tools_format(
        owed.ToolsSchema(standard_tools=[owed.STAY_SILENT_SCHEMA]))
    for name, kind, about, make in chosen:
        messages = await make()
        offered = reoffer_tools if name.startswith("reoffer_") else TOOLS
        replies = await asyncio.gather(*(ask(messages, offered) for _ in range(trials)))
        got = [(classify(kind, about, m), (m.get("content") or "").strip().replace("\n", " ")
                if not m.get("tool_calls") else "CALL " + "; ".join(
                    c["function"]["name"] for c in m["tool_calls"])) for m in replies]
        out[name] = (collections.Counter(k for k, _ in got), got)
    return out


def report(results) -> int:
    """Prints the counts and a few samples; returns the failures (see the header)."""
    bad = 0
    for name, kind, *_ in CASES:
        if name not in results:
            continue
        counts, got = results[name]
        n = sum(counts.values())
        print(f"\n== {name} ({kind}): {dict(counts.most_common())}")
        seen = set()
        for k, text in got:
            if k not in seen or len(seen) < 3:
                print(f"    [{k}] {text[:220]}")
                seen.add(k)
        if counts["instruction"]:
            bad += 1
        if name.startswith("run_") and kind in REASON and (n - counts["told"]) * 10 > n:
            bad += 1
    return bad


async def main():
    only = os.getenv("TEAPORT_EVAL_CASES")
    results = await run(set(only.split(",")) if only else None)
    print(f"model={setting('LLM_MODEL')} trials={TRIALS}")
    bad = report(results)
    assert not bad, f"{bad} case(s) below the bar"
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
