#
# test_cut_judge_eval.py -- the cut judge (owed.Owed._judge, #204) against live tellings,
# OPT-IN: a live LLM, many calls. Skipped unless TEAPORT_EVAL_CUT_JUDGE is set and the
# model's endpoint is reachable (appliance.py), so the suite reports it skipped.
#
#   TEAPORT_EVAL_CUT_JUDGE=1 [TEAPORT_EVAL_TRIALS=3] python test_cut_judge_eval.py
#
# Every cut of 12 live tellings (the box journal, 2026-10-18/19), after every word, is
# labelled "heard" once every fact that answers the question has been spoken, else
# "owed". Each cut is judged through the session's own client (services.make_llm()
# .judge_cut, the brain's model, routing and key) by majority of TEAPORT_EVAL_TRIALS,
# beside the lexical rule on the heard prefix it replaced. Measured 2026-10-20
# (gpt-oss-120b, 10 trials): judge 1/145 false closes, 14/96 false owes; lexical 89/145,
# 0/96. It reports; it fails only if the judge's false closes reach the lexical rule's.
#
import asyncio
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_CUT_JUDGE", "an opted-in live evaluation of the cut judge")

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain import services  # noqa: E402
from teaport_brain.owed import Record, _rest, clean_outcome  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402

appliance.require_reachable(setting("LLM_BASE_URL") or "", "the language model's endpoint")
TRIALS = int(os.getenv("TEAPORT_EVAL_TRIALS") or "3")

# (name, the caller's question, the whole telling, the facts that answer it): live
# tellings, verbatim; tails added where noted.
TELLINGS = [
    ("austin", "Can you tell me what the weather will be like in Austin tomorrow?",
     "Tomorrow in Austin looks partly sunny with a high around seventy-five degrees and a low "
     "near fifty-five, with only a slight chance of rain.",
     ["partly sunny", "seventy-five", "fifty-five", "slight chance"]),
    ("boston", "Can you tell me what the weather will be like in Boston tomorrow?",
     "Tomorrow in Boston looks cloudy with a high near sixty-four degrees and a low around "
     "forty-nine. That's a cloudy day, high about sixty-four, low near forty-nine.",
     ["cloudy", "sixty-four", "forty-nine"]),
    ("amazon", "Can you look up the price of an NVIDIA Jetson Oren Nano on Amazon?",
     "Amazon lists the Orin Nano developer kit at two hundred forty-nine dollars, matching the "
     "official MSRP. The Amazon price for the NVIDIA Jetson Orin Nano developer kit is two "
     "hundred forty-nine dollars.",
     ["two hundred forty-nine"]),
    ("bakery", "Can you tell me if there are any pastry shops? On the same road.",
     "Upper Crust Bakery at 4508 Burnet Road offers fresh pastries and baked desserts daily. "
     "Yes—Upper Crust Bakery on Burnet Road serves a full range of pastries and daily baked "
     "desserts.",
     ["Upper Crust Bakery", "4508 Burnet Road"]),
    ("toyjoy", "Can you look if there are any toy stores on Burnett Road in Austin, Texas?",
     "There's a Toy Joy store at five-five-zero-one Burnet Road in Austin. Let me know if you "
     "need anything else.",                                                  # tail added
     ["Toy Joy", "five-five-zero-one Burnet"]),
    ("gens", "The different generations, like Boomer, Gen X, Gen Y, Gen Z, Millennial.",
     "Boomers are people born roughly 1946-1964, Gen X covers 1965-1980, Millennials (Gen Y) "
     "are 1981-1996, and Gen Z runs from about 1997 onward.",
     ["1946-1964", "1965-1980", "1981-1996", "1997"]),
    ("ugm", "Can you look up what days the Nets sapiens UGM conference in Austin, Texas is?",
     "The NetSapiens User Group Meeting in Austin runs from October twenty-sixth through "
     "October twenty-ninth, 2026.",
     ["October twenty-sixth", "October twenty-ninth"]),
    ("alpha", "So we wanted to know what years were Gen A, Gen Alpha born and what's the oldest "
              "age of a current gen alpha in 2026?",
     "Gen Alpha generally covers kids born from about 2010 through the mid-2020s, so the oldest "
     "members—those born in 2010—will be sixteen years old in 2026.",
     ["2010", "mid-2020s", "sixteen"]),
    ("days", "Can you tell me what days October 26th and October 22nd, what days of the week they are.",
     "October twenty-second falls on a Thursday, and October twenty-sixth lands on a Monday.",
     ["Thursday", "Monday"]),
    ("time", "What time is it right now?",
     "It's eleven forty-three PM Central time. Anything else I can help with?",  # tail added
     ["eleven forty-three"]),
    ("model", "Can you ask it which model it is, if it sees the configuration?",
     "The desktop agent says it's using the openrouter deepseek-v4.1-flash model, and no "
     "override is active.",
     ["deepseek-v4.1-flash"]),
    ("hn", "What's the top story on Hacker News right now?",
     "The top story on Hacker News right now is Tesla Roadster. Want me to look up more about "
     "it?",
     ["Tesla Roadster"]),
]


def cases():
    out = []
    for name, question, text, facts in TELLINGS:
        words = text.split()
        for n in range(1, len(words)):                     # every cut that leaves something
            heard = " ".join(words[:n])
            label = "heard" if all(f.lower() in heard.lower() for f in facts) else "owed"
            out.append((name, question, text, heard, label))
    return out


def lexical(question, text, heard):
    rec = Record.make("k", question, clean_outcome(text))
    return "heard" if rec.closes_on(heard, run=False) else "owed"


async def main():
    llm = services.make_llm()
    sem = asyncio.Semaphore(8)
    lat = []

    async def ask(question, heard, rest):
        async with sem:
            started = time.monotonic()
            try:
                verdict = await asyncio.wait_for(llm.judge_cut(question, heard, rest), 30)
            except Exception:  # noqa: BLE001 -- an error is no verdict, as in owed.py
                verdict = None
            lat.append(time.monotonic() - started)
            return verdict

    all_cases = cases()
    votes = await asyncio.gather(*(
        asyncio.gather(*(ask(q, h, _rest(t, h)) for _ in range(TRIALS)))
        for _, q, t, h, _ in all_cases))
    tally = collections.Counter()
    for (name, question, text, heard, label), vs in zip(all_cases, votes):
        judge = "heard" if sum(v is True for v in vs) * 2 > TRIALS else "owed"
        tally[f"n_{label}"] += 1
        tally["unparsed"] += sum(v is None for v in vs)
        for who, said in (("judge", judge), ("lexical", lexical(question, text, heard))):
            if said != label:
                tally[f"{who}_false_{'close' if label == 'owed' else 'owe'}"] += 1
    lat.sort()
    print(f"model={setting('LLM_MODEL')} trials={TRIALS} cuts={len(all_cases)} "
          f"({tally['n_owed']} owed, {tally['n_heard']} heard)")
    for who in ("judge", "lexical"):
        print(f"{who:8}: false closes {tally[who + '_false_close']}/{tally['n_owed']}, "
              f"false owes {tally[who + '_false_owe']}/{tally['n_heard']}")
    print(f"no verdict: {tally['unparsed']}; latency p50 {lat[len(lat) // 2]:.2f}s "
          f"p90 {lat[int(len(lat) * .9)]:.2f}s max {lat[-1]:.2f}s")
    assert tally["judge_false_close"] < tally["lexical_false_close"], tally
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
