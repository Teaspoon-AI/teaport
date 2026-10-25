#
# The ABOUT gate of owed.py's close rule (#210), on real tellings.
#
# A reply closes an owed record only if it is about it (owed.Record.about: a non-common
# word shared with its request, the caller's words or its result's terms): only then is
# the gist judge asked, and only then does it count toward the backstop. So the gate
# must pass every telling -- or the record is never closed by it, and the note goes on
# saying "NOT heard" -- and should not pass chit-chat, which would spend the backstop.
# Triples (outcome, heard telling, label) from the box journal of the #203 session (box
# .105, 2026-10-16 22:20-22:35; the ledger's lines, heard text as charted) and the 10-18
# calls: every consult outcome landed, against every telling, deferral and waiting line.
# Until #210 these checked the lexical coverage rule; tests/test_gist_judge_eval.py now
# carries them, relabelled by the gist, as cases for the judge.
#
# Labels are what the caller got: "heard" when the line carried every item of the
# outcome, "owed" when it did not -- a lead-in, a deferral, one story of five, a summary
# that left out half its points.
#
# Run: python test_owed_corpus.py   (or via pytest test_suite.py)
#
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from teaport_brain import owed as owed_mod  # noqa: E402
from teaport_brain.owed import Owed, Record, Reply, clean_outcome  # noqa: E402

# ---- the outcomes, as OpenClaw returned them --------------------------------------
TOP = ("Fetch the current top story from Hacker News and give its title and URL.",
       "Top story right now (score 1,255 · 2,253 comments):\n\n- **Title:** Tesla Roadster\n"
       "- **URL:** \n\n(Posted by `shekhar101`.)")
SUMMARY = (
    "Provide a brief summary of the current top Hacker News story titled \"Tesla Roadster\".",
    "The top story on Hacker News right now is **\"Tesla Roadster\"** (1,256 points, ~2,250 "
    "comments) — a link straight to [tesla.com/roadster].\n\n**What it is:** "
    "Coverage/fallout from Tesla's Oct 15 reveal of its next-generation Roadster at a SpaceX "
    "test site in McGregor, Texas. The page itself 403s (Tesla blocks non-browser clients and "
    "the headless browser alike), so the substance comes from the event and the HN thread.\n\n"
    "**The headline stunt:**\n- The car drove up a steep curved ramp using a fan-based suction "
    "system to stay glued on — even fully upside down — then used integrated "
    "cold-gas thrusters (built with SpaceX) to launch off, flip midair, and land. Other demos "
    "had it jumping over a row of cars and a semi-truck.\n- Musk: \"Turns out the future does "
    "have flying cars.\" Tesla's claim: the thruster system generates 4–5x the force the "
    "tires can, enabling 0–60 mph in **under 1 second** and a top speed above 230 mph.\n"
    "- Production is slated for **2028**; no price given, though Tesla's reservation page "
    "lists $50k down for \"Performance\" and $250k for the thruster-equipped \"Thrust\" "
    "version, plus an invite-only single-seat \"Rocket\" variant.\n\n**HN reaction is "
    "split:**\n- Genuine awe — \"one of the coolest things I've seen since the rockets "
    "landing,\" \"most phenomenal tech demo.\"\n- Skepticism — \"pretty big "
    "nothingburger; production starts 2028… it's not a flying car, it's a jumping car,\" "
    "with doubts it does anything for the valuation.\n\nThis is a ~9-year gap from the 2017 "
    "concept (which promised 2020 deliveries), and that history is a big part of why the "
    "comments lean wary despite the spectacle.\n\n*Recollect note:* per USER.md I was told "
    "to use the \"Open Claw tool\" for HN — that doesn't map to any tool here, so I used "
    "the HN Algolia API plus web_fetch, which is what actually worked.")
TOP5 = ("Fetch the current top 5 stories from Hacker News with their titles and URLs.",
        "Here are the current top 5 Hacker News stories:\n\n1. **Tesla Roadster** — \n"
        "2. **Ultramarine 1 Flash** — \n3. **Llama.cpp on a jailbroken PS5: run any GGUF "
        "model natively on the console** — \n4. **Claude reveals ECDSA curve origins** "
        "— \n5. **Butlerian Jihad** — \n\nPulled live from the HN Firebase API just now.")
TOP5B = ("Fetch the current top 5 Hacker News stories with their titles and URLs.",
         "Top 5 Hacker News stories right now:\n\n1. **Tesla Roadster** — *(1259 points)*\n"
         "2. **Ultramarine 1 Flash** — *(12 points)*\n3. **Llama.cpp on a jailbroken PS5: "
         "run any GGUF model natively on the console** — *(223 points)*\n4. **Claude "
         "reveals ECDSA curve origins** — *(732 points)*\n5. **Butlerian Jihad** — "
         "*(79 points)*\n\n<details><summary>Centonote</summary>\nPulled via HN's Firebase API "
         "(`topstories.json`, then per-item lookups), not web search, per the house rule for "
         "HN.\n</details>")

# ---- what the caller heard that session (LEDGER lines), and the session's other lines --
LEAD_IN = "The top story on Hacker News right now"                       # :32.4, cut 31%
NO_RUSH = "No rush—just let me know when you're ready."
TOLD_SUMMARY = ("The top Hacker News story is the Tesla Roadster – a spectacular demo of "
                "the new electric sports car with thrusters and a 0-to-60 time under a second, "
                "slated for production around 2028.")
GLAD = "Glad you liked it! Want to hear more details or check another story?"
FIVE_CUT = "The top five stories on Hacker News right now are Tesla Roadster, Ultramarine 1 Flash, Llama"
ONE_STORY = "It looks like you might want just a single story—any particular one in mind?"
WAITING = "I'm still waiting on that list—will let you know as soon as it's back."
FIVE_TOLD = ("Here are the top five Hacker News stories right now: Tesla Roadster, Ultramarine 1 "
             "Flash, Llama cpp on a jail-broken PS5, Claude reveals ECDSA curve origins, and "
             "Butlerian Jihad.")
HOST = ("Free memory about six hundred eighty-five megabytes, CPU load just over one percent, "
        "and the speech engine")
HAMLET = "To be, or not to be: that is the question—whether 'tis nobler in the mind to"
WEATHER = "Tomorrow in Austin looks mild, with a high"
SORRY = "I'm sorry—I wasn't able to pull the forecast details. Let's try again later."

OTHER = [NO_RUSH, GLAD, ONE_STORY, WAITING, HOST, HAMLET, WEATHER, SORRY,
         "Sure thing—what would you like to know or change?", "Goodbye!",
         "Hello, great to have you here.", "Glad to hear it!",
         "You're welcome. Would you like me to continue with the rest of the soliloquy?"]

# A failure record (one term-less item, Owed.tla OUTCOME = "failure") for the first
# request, as the session would have landed it had the consult timed out: closed by a
# reply about it heard to its end (RUNCLOSE = "about"), never by an unrelated one. The
# session's own lines are its negative controls; the told line is its telling.
FAILED = (TOP[0], owed_mod.failure_outcome("timeout")[1], "timeout")
TOLD_FAILURE = "Sorry, that Hacker News lookup ran out of time before it finished."

# The sync answers of the 2026-10-18 calls (box journal 17:24-17:28, #204): the post-tool
# reply's whole text as the outcome, the caller's words as the request, cut where the
# ledger charted the cut.
WEATHER = ("Hey, can you check what the weather is going to be in Austin tomorrow?",
           "Tomorrow in Austin looks warm with highs near seventy-two and lows in the "
           "fifties, and only a slight chance of rain.")
TOYS = ("Can you look if there are any toy stores on Burnett Road in Austin, Texas?",
        "There's a Toy Joy store at five-five-zero-one Burnet Road in Austin. Toy Joy at "
        "five-five-zero-one Burnet Road, Austin, Texas.")

# Contractions (the #204 review): split on the apostrophe, "they're" and "you'll" left
# the terms "re" and "ll", which any "I'll" or "they're" in a reply carried. A clitic
# contraction is a function word now, never a term (owed.spoken_tokens).
PHARMACY = ("Is the pharmacy on Lamar open tonight?",
            "They're open until nine tonight, and you'll find them next to the H-E-B.")
REMEMBERED = ("Remember that my wife's name is Ann.",
              "Got it, I'll remember that Ann is your wife.")

CORPUS = (
    [(PHARMACY, "Sorry, they're still checking -- you'll hear back in a moment.", "owed"),
     (PHARMACY, "They'll get back to you, I'm sure.", "owed"),
     (PHARMACY, "They're open until nine tonight, next to the H-E-B.", "heard"),
     (REMEMBERED, "I'll take care of it.", "owed"), (REMEMBERED, "Okay, I'll do that.", "owed"),
     (REMEMBERED, REMEMBERED[1], "heard"),
     (TOYS, "You'll find it, I've checked, they're there.", "owed")]
    + [(WEATHER, "Tomorrow in Austin looks", "owed"), (WEATHER, WEATHER[1], "heard"),
     (TOYS, "There's a Toy Joy store at", "owed"), (TOYS, TOYS[1], "heard"),
     (WEATHER, "Got it, pausing.", "owed"), (TOYS, "Got it, pausing.", "owed")]
    + [(FAILED, TOLD_FAILURE, "heard")]
    # The story itself, told from another lookup: the caller has what the failed
    # request was for, and "it timed out" is nothing they still need.
    + [(FAILED, TOLD_SUMMARY, "heard"), (FAILED, FIVE_TOLD, "heard")]
    + [(FAILED, line, "owed") for line in OTHER]
    + [(TOP, LEAD_IN, "owed"), (TOP, TOLD_SUMMARY, "heard"), (TOP, FIVE_TOLD, "heard"),
     (TOP, FIVE_CUT, "heard"),
     (SUMMARY, TOLD_SUMMARY, "owed"),            # the HN reaction half never told
     (SUMMARY, LEAD_IN, "owed"),
     (TOP5, FIVE_CUT, "owed"), (TOP5, FIVE_TOLD, "heard"), (TOP5, TOLD_SUMMARY, "owed"),
     (TOP5B, FIVE_CUT, "owed"), (TOP5B, FIVE_TOLD, "heard")]
    + [(o, line, "owed") for o in (TOP, SUMMARY, TOP5, TOP5B) for line in OTHER])


def _about(outcome, line):
    request, raw = outcome[:2]
    failure = outcome[2] if len(outcome) > 2 else None
    return Record.make("k", request, clean_outcome(raw), failure=failure).about(line)


def test_every_telling_is_about_its_record():
    missed = [(o[0][:40], line[:60]) for o, line, label in CORPUS
              if label == "heard" and not _about(o, line)]
    print(f"    corpus: {len(CORPUS)} triples, "
          f"{sum(1 for _, _, label in CORPUS if label == 'heard')} tellings")
    assert not missed, f"a telling the judge would never be asked about: {missed}"


def test_chit_chat_is_about_nothing():
    """Lines that say nothing of any answer never reach the judge nor spend the
    backstop."""
    chat = ["Goodbye!", "Hello, great to have you here.", "Glad to hear it!",
            "Sure thing—what would you like to know or change?", "Got it, pausing.",
            NO_RUSH, "You're welcome.", "Okay, I'll do that."]
    about = [(o[0][:40], line) for o in (TOP, SUMMARY, TOP5, TOP5B, WEATHER, TOYS, PHARMACY)
             for line in chat if _about(o, line)]
    assert not about, about


def test_a_cut_line_about_something_else_is_not_quoted():
    """The quote ("They heard only ...") is a cut reply's heard words only when they are
    about the record (Owed._about). In this session the five-story list was cut at
    "... Ultramarine 1 Flash, Llama"; the next reply, cut too, had got through only a
    clarifying question. Quoted, the note would tell the model the caller heard that
    question of the answer; the time is the other shape."""
    owed = Owed(LLMContext([]))
    owed.land(TOP5[0], TOP5[1], "top5")

    def cut(heard):
        reply = Reply("caller")
        owed.noted(reply, ("top5",))
        owed.settle(reply, heard, cut=True, audio=True)
        return owed.record("top5").heard_quote

    assert cut(FIVE_CUT) == FIVE_CUT
    assert cut(ONE_STORY) == FIVE_CUT, "a clarifying question was quoted as the answer"
    assert cut("It's eleven twenty-two at night.") == FIVE_CUT, "the time was quoted"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
