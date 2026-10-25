#
# test_gist_judge_eval.py -- the GIST judge for a reply heard to its end (#210). OPT-IN: a
# live LLM, many calls. Skipped unless TEAPORT_EVAL_GIST_JUDGE is set and the model's
# endpoint is reachable (appliance.py), so the suite reports it skipped.
#
#   TEAPORT_EVAL_GIST_JUDGE=1 [TEAPORT_EVAL_TRIALS=5] [TEAPORT_EVAL_CONCURRENCY=8]
#       [TEAPORT_EVAL_GIST_CASES=/path/to/cases.json] python test_gist_judge_eval.py
#
# The question #210 left open: a reply played through closed an owed record by the
# lexical coverage rule (min(2, |terms|) of each item's terms, gone since), which closes
# a multi-fact answer told in part and never closes a one-fact answer whose request
# names everything but the fact (a one-fact answer told in the caller's words stayed
# owed and was re-told five times, 2026-10-23 14:06). The standard here is the GIST: a
# record is heard once a reply gave the caller the answer to what THEY asked -- the
# names, numbers, dates, places or items that answer it -- not every fact of the
# result. Details nobody asked for stay in the context.
#
# Cases, each (record, the reply heard in full, label), written here in the shapes the
# live sessions had (invented content: no conversation from a box goes into the repo):
# a one-fact answer, the answer without its details, one item of a list asked for, a
# list without the details nobody asked for, "it doesn't exist" as the answer, a
# summary, a two-part question told half, an offer or a deferral about the answer, a
# wrong fact, and a question asked over two turns. TEAPORT_EVAL_GIST_CASES adds cases
# kept out of the repo (a JSON list of {src, question, request, outcome, reply, label};
# "?" = arguable, reported, not scored).
#
# Scored on the replies the judge is asked about: those ABOUT the record
# (owed.Record.about); an unrelated reply never closes anything and is not judged. Each
# is judged through the session's own client (services.make_llm().judge_gist, the
# shipped prompt owed.GIST_JUDGE_PROMPT) by majority of TEAPORT_EVAL_TRIALS. It reports;
# it fails if the false closes reach a tenth of the owed cases.
#
# The prompt was chosen on 58 cases from the box's journals (2026-10-18 and 10-22/23,
# kept out of the repo; gpt-oss-120b; 25 owed, 29 heard, 4
# arguable):
#   lexical rule                         false closes 2/25, false owes 11/29
#   v1, caller's words + result, 5 tr.   false closes 0/25, false owes  2/29
#   v1, caller's words + result, 10 tr.  false closes 0/25, false owes  4/29
#   v2 / v3 (longer rules), same form    false closes 1/25, false owes  2/29
#   + the model's request ("both")       false owes 9-11/29: it asks for more than the
#                                        caller did (a title and a link)
#   the request alone                    false owes 11-12/29
#   no result shown                      false closes 1-3/25 (a wrong year closed 5/5)
# Its false owes: an answer told without one of its details, "it doesn't exist", a
# remember confirmation, and a re-asked lookup whose caller's words carried no question.
# Latency one request at a time: p50 0.56 s, p90 1.15 s, max 3.05 s; at 8 in flight p90
# 4-5 s.
#
import asyncio
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_GIST_JUDGE", "an opted-in live evaluation of the gist judge")

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain import services  # noqa: E402
from teaport_brain.owed import Record  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402

appliance.require_reachable(setting("LLM_BASE_URL") or "", "the language model's endpoint")
TRIALS = int(os.getenv("TEAPORT_EVAL_TRIALS") or "5")

# ---- the records: (the caller's words, the model's request when it differs, the result)
RECORDS = {
    "lighthouse": (
        "Can you look up when the Harborview Lighthouse was first lit?",
        "What year was the Harborview Lighthouse first lit?",
        "The Harborview Lighthouse was first lit in 1871 — keeper Ada Marsh lit the lamp "
        "on May 3, 1871, in Port Ellery."),
    "forecast": (
        "What's the weather going to be in Millbrook tomorrow?", "",
        "Tomorrow in Millbrook looks mild, with highs near seventy-two degrees, lows around "
        "fifty-four, and only a slight chance of rain."),
    "stories": (
        "What are the top three stories on the Gazette site today?",
        "List the top three Gazette stories with a short summary of each.",
        "Top three on the Gazette right now:\n\n- Harbor bridge reopens after repairs\n"
        "   The east span is open again, two weeks early.\n- Library adds a night "
        "reading room\n   Open until midnight on weekdays.\n- Local robotics team "
        "wins regionals\n   They go to nationals in May."),
    "bakeries": (
        "Which bakeries are on Alder Street?", "",
        "Bakeries on Alder Street:\n\n- Crumb & Co. — 210 Alder St\n- Sunrise Loaves — "
        "455 Alder St\n- The Rolling Pin — 812 Alder St\n- Petit Four — 1020 Alder St"),
    "no_road": (
        "Can you find toy shops on Wexley Road?",
        "Find toy shops on Wexley Road and list their names and addresses.",
        "There's no toy shop list to give you, because Wexley Road doesn't exist in "
        "Millbrook. The closest real match is Wesley Road; say if you meant that one."),
    "comments": (
        "Can you sum up what people said in the comments on that first story?", "",
        "Commenters were split: most welcomed the early reopening, some worried the "
        "repairs were rushed, and a few asked for a public safety report."),
    "two_part": (
        "Which model and which provider does the voice loop use?", "",
        "The voice loop uses the model Atlas-40B through the Northwind provider, with no "
        "fallbacks."),
    "status": (
        "What's the status right now?", "",
        "Free memory is about three hundred twenty megabytes, and the CPU load is just "
        "over one percent. All good: plenty of memory left and the processor is barely "
        "busy."),
    "founders": (
        "Who founded Orchid Labs?", "",
        "Orchid Labs was founded by Mara Quist, Theo Brandt and Ines Kalu in 2019."),
    "follow_up": (
        "Who runs the Fernway museum? … Yeah, please go check.", "",
        "The Fernway museum's director is Priya Okafor, who took over in March."),
    "remember": (
        "Remember that my sister's name is Lena.", "",
        "Got it, I'll remember that Lena is your sister."),
}

CASES = [
    ("lighthouse", "1871 is the year the Harborview Lighthouse was first lit.", "heard"),
    ("lighthouse", "It was first lit in eighteen seventy-one.", "heard"),
    ("lighthouse", "The Harborview Lighthouse was first lit by Ada Marsh in Port Ellery.",
     "owed"),                                                             # no year
    ("lighthouse", "Do you want me to tell you the year the Harborview Lighthouse was "
                   "first lit?", "owed"),
    ("lighthouse", "I'm still waiting on the Harborview Lighthouse lookup.", "owed"),
    ("lighthouse", "The Harborview Lighthouse was first lit in 1902.", "owed"),  # not the result's
    ("forecast", "Tomorrow in Millbrook: highs near seventy-two and a slight chance of rain.",
     "heard"),                                                            # no low
    ("forecast", "I've got tomorrow's Millbrook forecast whenever you want it.", "owed"),
    ("stories", "The top Gazette story is the harbor bridge reopening.", "owed"),
    ("stories", "The harbor bridge reopened, the library added a night reading room, and "
                "the robotics team won regionals.", "heard"),             # no summaries
    ("bakeries", "There are four bakeries on Alder Street, including Crumb & Co.", "owed"),
    ("bakeries", "Crumb & Co., Sunrise Loaves, The Rolling Pin and Petit Four are all on "
                 "Alder Street.", "heard"),                               # no addresses
    ("no_road", "There's no Wexley Road in Millbrook, so there are no toy shops to list. "
                "Did you mean Wesley Road?", "heard"),
    ("no_road", "Did you mean Wesley Road?", "owed"),
    ("comments", "Most commenters liked the early reopening, though some thought the "
                 "repairs were rushed.", "heard"),
    ("comments", "Got it—the summary of the reopening comments is as provided.", "owed"),
    ("two_part", "It uses Atlas-40B.", "owed"),                           # no provider
    ("two_part", "Atlas-40B, through Northwind.", "heard"),
    ("status", "Everything looks fine: plenty of memory free and the processor's barely "
               "busy.", "heard"),
    ("status", "Let me check the status again.", "owed"),
    ("founders", "Orchid Labs was founded by Mara Quist.", "owed"),       # one of three
    ("founders", "Mara Quist, Theo Brandt and Ines Kalu founded Orchid Labs.", "heard"),
    ("follow_up", "Priya Okafor runs the Fernway museum.", "heard"),
    ("remember", "Got it, I'll remember that Lena is your sister.", "heard"),
]


def cases():
    out = [("written", *RECORDS[name], reply, label) for name, reply, label in CASES]
    path = os.getenv("TEAPORT_EVAL_GIST_CASES")
    if path:
        out += [(c.get("src", "local"), c["question"], c.get("request") or "", c["outcome"],
                 c["reply"], c["label"]) for c in json.load(open(path))]
    return out


def about(question, request, outcome, reply):
    """Whether the judge is asked: the record as owed.py makes it (a consult's request
    is the model's; a sync record's, the caller's words)."""
    return Record.make("k", request or question, outcome, question=question).about(reply)


async def main():
    llm = services.make_llm()
    sem = asyncio.Semaphore(int(os.getenv("TEAPORT_EVAL_CONCURRENCY") or "8"))
    lat = []

    async def ask(question, outcome, reply):
        async with sem:
            started = time.monotonic()
            try:
                verdict = await asyncio.wait_for(llm.judge_gist(question, outcome, reply), 30)
            except Exception:  # noqa: BLE001 -- an error is no verdict, as in owed.py
                verdict = None
            lat.append(time.monotonic() - started)
            return verdict

    judged = [c for c in cases() if about(c[1], c[2], c[3], c[4])]
    print(f"model={setting('LLM_MODEL')} trials={TRIALS} judged={len(judged)}")
    for src in sorted({c[0] for c in judged}):
        n = [c for c in judged if c[0] == src]
        print(f"  {src:6}: {len(n)} judged, {sum(c[5] == 'heard' for c in n)} heard, "
              f"{sum(c[5] == 'owed' for c in n)} owed, {sum(c[5] == '?' for c in n)} arguable")
    votes = await asyncio.gather(*(
        asyncio.gather(*(ask(q, o, rep) for _ in range(TRIALS)))
        for _, q, _, o, rep, _ in judged))
    tally, wrong, arguable = collections.Counter(), [], []
    for (src, q, r, o, rep, lab), vs in zip(judged, votes):
        heard = sum(v is True for v in vs)
        judge = "heard" if heard * 2 > TRIALS else "owed"
        tally["unparsed"] += sum(v is None for v in vs)
        if lab == "?":
            arguable.append((judge, heard, q[:40], rep[:60]))
            continue
        tally[f"n_{lab}"] += 1
        if judge != lab:
            tally[f"false_{'close' if lab == 'owed' else 'owe'}"] += 1
            wrong.append((src, lab, heard, q[:40], rep[:70]))
    print(f"judge: false closes {tally['false_close']}/{tally['n_owed']}, false owes "
          f"{tally['false_owe']}/{tally['n_heard']}, no verdict {tally['unparsed']}")
    for src, lab, heard, q, rep in wrong:
        print(f"    wrong: {src:6} label {lab:5} heard-votes {heard}/{TRIALS} | {q!r} | {rep!r}")
    for judge, heard, q, rep in arguable:
        print(f"    arguable -> {judge} ({heard}/{TRIALS}) | {q!r} | {rep!r}")
    lat.sort()
    print(f"latency p50 {lat[len(lat) // 2]:.2f}s p90 {lat[int(len(lat) * .9)]:.2f}s "
          f"max {lat[-1]:.2f}s")
    assert tally["false_close"] * 10 < tally["n_owed"], tally
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
