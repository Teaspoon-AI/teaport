#
# The lexical premise of owed.py's close rule (#203, DESIGN §3), on real tellings.
#
# formal/Owed.tla takes it as given that "every item's terms in the pts-exact heard text"
# means the caller heard every item: the model can only ask whether the RULE closes on
# that. This is the premise's check, on (outcome, heard telling, label) triples from the
# box journal of the #203 session (box .105, 2026-10-16 22:20-22:35; the ledger's lines,
# heard text as charted): every consult outcome that session landed, against every
# telling, deferral and waiting line it had. The target is DESIGN §3's: no false close.
# A false "still owed" costs one mention (0/60 in the replays) and is counted, not failed.
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

from teaport_brain.owed import Record, clean_outcome  # noqa: E402

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

CORPUS = (
    [(TOP, LEAD_IN, "owed"), (TOP, TOLD_SUMMARY, "heard"), (TOP, FIVE_TOLD, "heard"),
     (TOP, FIVE_CUT, "heard"),
     (SUMMARY, TOLD_SUMMARY, "owed"),            # the HN reaction half never told
     (SUMMARY, LEAD_IN, "owed"),
     (TOP5, FIVE_CUT, "owed"), (TOP5, FIVE_TOLD, "heard"), (TOP5, TOLD_SUMMARY, "owed"),
     (TOP5B, FIVE_CUT, "owed"), (TOP5B, FIVE_TOLD, "heard")]
    + [(o, line, "owed") for o in (TOP, SUMMARY, TOP5, TOP5B) for line in OTHER])


def _closes(outcome, line):
    request, raw = outcome
    return Record.make("k", request, clean_outcome(raw)).closes_on(line, uncut_run=False)


def test_no_false_close():
    false = [(o[0][:40], line[:60]) for o, line, label in CORPUS
             if label == "owed" and _closes(o, line)]
    assert not false, f"closed on a line that did not carry every item: {false}"


def test_the_tellings_close():
    missed = [(o[0][:40], line[:60]) for o, line, label in CORPUS
              if label == "heard" and not _closes(o, line)]
    owed = sum(1 for _, _, label in CORPUS if label == "owed")
    heard = sum(1 for _, _, label in CORPUS if label == "heard")
    print(f"    corpus: {len(CORPUS)} triples, {owed} owed (0 false closes), {heard} heard "
          f"({len(missed)} left owed)")
    assert not missed, f"a full telling left the record owed: {missed}"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
