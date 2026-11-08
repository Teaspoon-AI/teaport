#
# test_cut_judge_eval.py -- the cut judges (owed.Owed._cut, #204, #312) against tellings,
# OPT-IN: a live LLM, many calls. Skipped unless TEAPORT_EVAL_CUT_JUDGE is set and the
# model's endpoint is reachable (appliance.py), so the suite reports it skipped.
#
# On the appliance, from a checkout's brain/ (the env file is read as systemd reads it --
# appliance.load_env_file; never `source` it: the shell strips LLM_EXTRA_BODY's JSON
# quotes and runs a value with spaces as a command):
#
#   cd ~/<checkout>/brain && TEAPORT_EVAL_CUT_JUDGE=1 \
#       TEAPORT_EVAL_ENV_FILE=/etc/teaport/brain.env TEAPORT_EVAL_CUT_SET=failures \
#       PYTHONPATH=$PWD /opt/teaport/venv/bin/python tests/test_cut_judge_eval.py
#
# [TEAPORT_EVAL_TRIALS=3] [TEAPORT_EVAL_CUT_SET=live|failures|multi] (live by default).
#
# TEAPORT_EVAL_CUT_SET=failures judges synthetic tellings of a consult's FAILURE instead
# (owed.failure_outcome's kinds, the formal audit's C1, 2026-11-05), through the FAILURE
# judge (services.judge_failure_cut, owed.failure_cut_judge_messages) that Owed._cut asks
# for a failure record, shown what happened: the core of its kind's outcome
# (owed.failure_core). The cut judge's prompt asks for a name, number, date... and on these
# answered HEARD 40-41/43 owed cuts (2026-11-08). =multi: tellings in other words and in
# other languages, the same judge. Labelled "heard" once what happened has been said
# (lexical labels: test-only ground truth); an offer or a question after it is the rest.
#
# Every cut of 12 live tellings (the box journal, 2026-10-18/19), after every word, is
# labelled "heard" once every fact that answers the question has been spoken, else
# "owed". Each cut is judged through the session's own client (services.make_llm()
# .judge_cut, the brain's model, routing and key) by majority of TEAPORT_EVAL_TRIALS.
# Measured 2026-10-20 (gpt-oss-120b, 10 trials): judge 1/145 false closes, 14/96 false
# owes; the lexical rule on the heard prefix it replaced (gone since #210) 89/145, 0/96.
# The failure judge, 2026-11-08 (same model, majority of 10): failures 1/43 false closes,
# 11/56 false owes; multi 4/66 and 9/65 (owed.FAILURE_CUT_JUDGE_PROMPT has the rest).
# It reports false closes and false owes; it fails if the false closes reach a tenth of
# the owed cuts.
#
import asyncio
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_CUT_JUDGE", "an opted-in live evaluation of the cut judge")
appliance.load_env_file()                  # before anything reads a setting

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain import owed, services  # noqa: E402
from teaport_brain.transcript_ledger import _unheard  # noqa: E402
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

# What each failure kind's record says happened (owed.failure_outcome), as the failure judge
# is shown it (owed.failure_core): the record a telling of that kind was made from.
FAILURE_CORES = {
    kind: owed.failure_core(owed.failure_outcome(*how, settings=True)[1])
    for kind, how in {"billing": ("error", "HTTP 402"), "credit": ("error", "out of credit"),
                      "timeout": ("timeout",), "error": ("error",),
                      "unknown": ("unknown",)}.items()}

# Synthetic tellings of each failure kind, in the model's own words (no request words
# needed): what happened is the fact; the offer after it is not. (name, question, telling,
# facts, kind)
FAILURES = [
    ("billing", "Can you check my calendar for Friday afternoon?",
     "I couldn't check that because of a billing problem with the AI service. Want me to "
     "open your settings so you can fix it?",
     ["billing problem"], "billing"),
    ("credit", "Can you look up the weather for tomorrow?",
     "Sorry, I can't do that right now. Your AI account is out of credit. Should I open your "
     "settings?",
     ["out of credit"], "credit"),
    ("timeout", "What's the top story on Hacker News right now?",
     "Sorry, that lookup ran out of time before it finished, so I don't have an answer. Want "
     "me to try again?",
     ["ran out of time"], "timeout"),
    ("error", "Find me a pizza place near the office.",
     "That search hit an error and couldn't be done. Do you want me to try it again?",
     ["hit an error"], "error"),
    ("unknown", "Did my message to Sam go out?",
     "I'm not sure. It didn't report back, so it may or may not have been sent. Could you "
     "check your messages to see?",
     ["may or may not"], "unknown"),
]

# Synthetic failure tellings in other words and other languages: the judge reads the
# telling, not a list of words (the user's ruling, 2026-11-08).
MULTI = [
    ("es-billing", "¿Puedes revisar mi calendario para el viernes por la tarde?",
     "No pude revisarlo porque hay un problema de facturación con el servicio de IA. "
     "¿Quieres que abra tu configuración para arreglarlo?",
     ["problema de facturación"], "billing"),
    ("es-credit", "¿Qué tiempo hará mañana?",
     "Lo siento, ahora no puedo. Tu cuenta de IA se quedó sin crédito. ¿Abro tu configuración?",
     ["sin crédito"], "credit"),
    ("ko-credit", "내일 날씨 좀 알려줄래?",
     "죄송해요, 지금은 할 수 없어요. AI 계정의 크레딧이 모두 소진되었어요. 설정을 열어 드릴까요?",
     ["크레딧이 모두 소진"], "credit"),
    ("ko-timeout", "지금 해커 뉴스 톱 기사가 뭐야?",
     "죄송해요, 검색 시간이 초과되어서 답을 찾지 못했어요. 다시 시도할까요?",
     ["시간이 초과"], "timeout"),
    ("pt-error", "Encontra uma pizzaria perto do escritório.",
     "Desculpa, essa busca deu erro e não foi concluída. Quer que eu tente de novo?",
     ["deu erro"], "error"),
    ("de-unknown", "Ist meine Nachricht an Sam rausgegangen?",
     "Ich bin mir nicht sicher. Es kam keine Rückmeldung, also wurde sie vielleicht "
     "verschickt, vielleicht auch nicht. Kannst du in deinen Nachrichten nachsehen?",
     ["vielleicht verschickt, vielleicht auch nicht"], "unknown"),
    ("en-wrong", "Can you add milk to my shopping list?",
     "Hmm, something went wrong on my end and I couldn't add it. Want me to try once more?",
     ["went wrong"], "error"),
    ("en-credit-howto", "What's on my calendar tomorrow?",
     "Sorry, I can't answer: my AI account has run out of credit. Add credit on OpenRouter's "
     "website, or say open my settings for help.",
     ["run out of credit"], "credit"),
]

SETS = {"live": [t + (None,) for t in TELLINGS], "failures": FAILURES, "multi": MULTI}


def cases():
    """(name, question, telling, heard, label, core): every cut that leaves something;
    `core` is what happened for a failure's telling (the failure judge), None for an
    answer's (the cut judge)."""
    out = []
    chosen = SETS[os.getenv("TEAPORT_EVAL_CUT_SET") or "live"]
    for name, question, text, facts, kind in chosen:
        words = text.split()
        core = FAILURE_CORES[kind] if kind else None
        for n in range(1, len(words)):                     # every cut that leaves something
            heard = " ".join(words[:n])
            label = "heard" if all(f.lower() in heard.lower() for f in facts) else "owed"
            out.append((name, question, text, heard, label, core))
    return out


async def main():
    llm = services.make_llm()
    sem = asyncio.Semaphore(8)
    lat = []

    async def ask(question, heard, rest, core):
        # The judge Owed._cut asks: the failure judge for a failure, shown what happened.
        call = (llm.judge_failure_cut(question, core, heard, rest) if core is not None
                else llm.judge_cut(question, heard, rest))
        async with sem:
            started = time.monotonic()
            try:
                verdict = await asyncio.wait_for(call, 30)
            except Exception:  # noqa: BLE001 -- an error is no verdict, as in owed.py
                verdict = None
            lat.append(time.monotonic() - started)
            return verdict

    all_cases = cases()
    votes = await asyncio.gather(*(
        asyncio.gather(*(ask(q, h, _unheard(t, h), c) for _ in range(TRIALS)))
        for _, q, t, h, _, c in all_cases))
    tally = collections.Counter()
    for (name, question, text, heard, label, _), vs in zip(all_cases, votes):
        judge = "heard" if sum(v is True for v in vs) * 2 > TRIALS else "owed"
        tally[f"n_{label}"] += 1
        tally["unparsed"] += sum(v is None for v in vs)
        if judge != label:
            tally[f"judge_false_{'close' if label == 'owed' else 'owe'}"] += 1
    lat.sort()
    print(f"set={os.getenv('TEAPORT_EVAL_CUT_SET') or 'live'} "
          f"model={setting('LLM_MODEL')} trials={TRIALS} cuts={len(all_cases)} "
          f"({tally['n_owed']} owed, {tally['n_heard']} heard)")
    print(f"judge   : false closes {tally['judge_false_close']}/{tally['n_owed']}, "
          f"false owes {tally['judge_false_owe']}/{tally['n_heard']}")
    print(f"no verdict: {tally['unparsed']}; latency p50 {lat[len(lat) // 2]:.2f}s "
          f"p90 {lat[int(len(lat) * .9)]:.2f}s max {lat[-1]:.2f}s")
    assert tally["judge_false_close"] * 10 < tally["n_owed"], tally
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
