#
# test_failure_judge_eval.py -- is a reply a telling of a consult's FAILURE, and did the
# caller hear it (#380)? OPT-IN: a live LLM, many calls. Skipped unless
# TEAPORT_EVAL_FAILURE_JUDGE is set and the model's endpoint is reachable (appliance.py),
# so the suite reports it skipped.
#
#   cd ~/<checkout>/brain && TEAPORT_EVAL_FAILURE_JUDGE=1 \
#       TEAPORT_EVAL_ENV_FILE=/etc/teaport/brain.env \
#       PYTHONPATH=$PWD /opt/teaport/venv/bin/python tests/test_failure_judge_eval.py
#
# [TEAPORT_EVAL_TRIALS=3] [TEAPORT_EVAL_CONCURRENCY=8] [TEAPORT_EVAL_PLAYED_ONLY=1: no cuts]
# [TEAPORT_EVAL_FAILURE_REPLIES=x.jsonl: more played-through tellings, kept out of the repo]
#
# Before #380 a reply was ABOUT a failure record by its words: a term shared with the
# request or the caller's question, or one of four English words that name what failed
# (owed._FAILURE_TERMS: billing, credit, error, empty). About it and played to its end, it
# closed the record; cut, it was judged and counted as a telling. The release speaks
# English, Spanish and Italian, and a telling in Spanish or Italian (or in other English
# words: "it timed out") shares nothing with the list -- while "I don't have the bookshop
# results yet", which names the request, closed a timed-out record as heard (#387).
#
# Now the failure judge decides (owed.failure_cut_judge_messages): shown what they asked,
# what happened (owed.failure_core) and what they heard. A reply heard to its end goes to
# owed.FAILURE_TOLD_JUDGE_PROMPT -- does it tell them it failed? -- and closes the record
# on HEARD; a cut one's whole text is asked the same (is it a telling of the failure at
# all? -- then it counts as a cut telling, and its words are read for a decline), and its
# heard prefix and rest go to owed.FAILURE_CUT_JUDGE_PROMPT (the 11-08 measured prompt),
# which closes it on HEARD. The record's own run (FAILURE_RUN_LINE) closes on no verdict.
#
# Every case is synthetic, in the shapes the box's tellings had (no conversation from a
# box goes into the repo): three failures (a timed-out lookup, an error, out of credit),
# each asked in English, Spanish and Italian, each with replies that tell it (labelled
# "told": it was not done, and why) and replies that do not ("owed": "not yet", "no
# results", "still waiting", a question back, another subject, a line about the request
# that says nothing of the failure). A told reply that leaves out why is "?" -- reported,
# not scored. Each told reply is also cut after every word: "told" once its marker (the
# words that say what happened) has been heard.
#
# It reports, for the rule before and the judge now: false closes (a record closed on a
# reply that did not tell it), false owes (a telling that leaves it owed), and for cuts the
# tellings counted wrongly. It fails if the judge does worse than the rule before, either
# way, played or cut, or closes a fifth of the played replies that told nothing.
#
# Measured 2026-11-09 (gpt-oss-120b on Groq, majority of 3; the cases here, plus 135
# tellings the model gave in test_owed_note_eval.py's cases, before and after #387, kept
# out of the repo and labelled by that eval's classes: 179 played, 102 told, 61 owed, 16
# arguable; 483 cuts):
#                     the words (before)      the judge
#   played, false closes   33/61                 9/61    ("non ho ricevuto una risposta":
#                                                         "I didn't get an answer")
#   played, false owes     41/102                0/102
#   cut, false closes      47/331               35/331   (most: a told reply cut after "I
#                                                         couldn't" and before why)
#   cut, false owes        71/152               24/152
#   cuts counted that told nothing  122 -> 0; tellings not counted 83 -> 0.
# On the hand-written cases alone (played): 17/24 -> 0/24 false closes, 5/18 -> 0/18 false
# owes. The cut prompt asked of whole replies instead (one prompt for both) closed 29/61;
# framed on "it failed" for cuts too, the existing cut sets (test_cut_judge_eval.py,
# failures / multi) went from 1/43 and 4/66 false closes to 21/43 and 35/66: a cut needs
# the reason heard, a whole reply that it failed.
#
import asyncio
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_FAILURE_JUDGE",
                      "an opted-in live evaluation of the failure judge")
appliance.load_env_file()                  # before anything reads a setting

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain import owed, services  # noqa: E402
from teaport_brain.owed import failure_core, failure_outcome, terms_of  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402
from teaport_brain.transcript_ledger import _unheard  # noqa: E402

appliance.require_reachable(setting("LLM_BASE_URL") or "", "the language model's endpoint")
TRIALS = int(os.getenv("TEAPORT_EVAL_TRIALS") or "3")

# ---- the failures: (kind, detail, {lang: (the caller's words, the model's request)}) ----
FAILURES = {
    "timeout": ("timeout", "", {
        "en": ("Ask the agent to find bookshops on Elm Street in Riverton.",
               "Find bookshops on Elm Street in Riverton."),
        "es": ("Pídele al agente que busque librerías en la calle Olmo de Riverton.",
               "Find bookshops on Calle Olmo in Riverton."),
        "it": ("Chiedi all'agente di trovare librerie in via degli Olmi a Riverton.",
               "Find bookshops on Via degli Olmi in Riverton."),
    }),
    "error": ("error", "gateway unreachable (connection refused)", {
        "en": ("Can the agent check when the next train to Millbrook leaves?",
               "When does the next train to Millbrook leave?"),
        "es": ("¿Puede el agente mirar cuándo sale el próximo tren a Millbrook?",
               "When does the next train to Millbrook leave?"),
        "it": ("L'agente può controllare quando parte il prossimo treno per Millbrook?",
               "When does the next train to Millbrook leave?"),
    }),
    "pharmacy": ("timeout", "", {
        "en": ("Ask the agent to find pharmacies open late near Oak Avenue.",
               "Find pharmacies near Oak Avenue that are open late tonight."),
        "es": ("Pídele al agente que busque farmacias abiertas hasta tarde cerca de Oak "
               "Avenue.", "Find pharmacies near Oak Avenue that are open late tonight."),
        "it": ("Chiedi all'agente di trovare farmacie aperte fino a tardi vicino a Oak "
               "Avenue.", "Find pharmacies near Oak Avenue that are open late tonight."),
    }),
    "credit": ("error", "HTTP 402: insufficient credits on this account", {
        "en": ("Ask the agent what's on my calendar tomorrow.",
               "What's on the user's calendar tomorrow?"),
        "es": ("Pregúntale al agente qué tengo mañana en el calendario.",
               "What's on the user's calendar tomorrow?"),
        "it": ("Chiedi all'agente cosa ho in calendario domani.",
               "What's on the user's calendar tomorrow?"),
    }),
}

# ---- the replies: (failure, lang, text, label, marker) ----------------------------------
# label: "told" | "owed" | "?" (told without why: arguable). marker: for a told reply, the
# words after which a cut has told it (case-insensitive substring of the text).
REPLIES = [
    # --- the timed-out lookup
    ("timeout", "en", "Sorry, I couldn't get the bookshop list: the search timed out before "
     "it finished. Want me to try again?", "told", "timed out"),
    ("timeout", "en", "The search for bookshops on Elm Street ran out of time, so I don't "
     "have a list. I can try again if you like.", "told", "ran out of time"),
    ("timeout", "en", "That took too long and never finished, so there's nothing to tell "
     "you yet. Should I try again?", "told", "never finished"),
    ("timeout", "en", "I couldn't get that list. Want me to try again?", "?", None),
    ("timeout", "en", "I don't have the bookshop results yet. Want me to look them up?",
     "owed", None),
    ("timeout", "en", "No bookshops turned up on Elm Street in Riverton.", "owed", None),
    ("timeout", "en", "I'm still waiting on that bookshop search.", "owed", None),
    ("timeout", "en", "Elm Street is the one that runs past the park, right?", "owed", None),
    ("timeout", "en", "It's sunny and about seventy-two degrees right now.", "owed", None),
    ("timeout", "es", "Lo siento, no pude conseguir la lista de librerías: la búsqueda se "
     "quedó sin tiempo antes de terminar. ¿Quieres que lo intente de nuevo?", "told",
     "sin tiempo"),
    ("timeout", "es", "La búsqueda tardó demasiado y no terminó, así que no tengo la lista. "
     "¿Lo intento otra vez?", "told", "tardó demasiado"),
    ("timeout", "es", "Todavía no tengo los resultados de las librerías. ¿Quieres que los "
     "busque?", "owed", None),
    ("timeout", "es", "No encontré ninguna librería en la calle Olmo.", "owed", None),
    ("timeout", "es", "Sigo esperando la búsqueda de librerías.", "owed", None),
    ("timeout", "es", "Hoy hace sol y unos veinte grados.", "owed", None),
    ("timeout", "it", "Mi dispiace, non sono riuscito a trovare le librerie: la ricerca è "
     "scaduta prima di finire. Vuoi che riprovi?", "told", "scaduta"),
    ("timeout", "it", "La ricerca ci ha messo troppo e non è finita, quindi non ho l'elenco. "
     "Riprovo?", "told", "troppo"),
    ("timeout", "it", "Non ho ancora i risultati delle librerie. Vuoi che li cerchi?", "owed",
     None),
    ("timeout", "it", "Non ho trovato nessuna libreria in via degli Olmi.", "owed", None),
    ("timeout", "it", "Sto ancora aspettando la ricerca delle librerie.", "owed", None),
    ("timeout", "it", "Oggi c'è il sole e ci sono circa venti gradi.", "owed", None),
    # --- a status question ("how are those searches going?") answered about it
    ("pharmacy", "en", "The pharmacy search timed out before it finished, so I don't have "
     "that list. Want me to try again?", "told", "timed out"),
    ("pharmacy", "en", "I'm still waiting on the pharmacy search; nothing back yet.", "owed",
     None),
    ("pharmacy", "en", "Both of those searches are still running.", "owed", None),
    ("pharmacy", "es", "La búsqueda de farmacias se quedó sin tiempo y no terminó. ¿Quieres "
     "que la intente de nuevo?", "told", "sin tiempo"),
    ("pharmacy", "es", "La búsqueda de farmacias sigue en curso.", "owed", None),
    ("pharmacy", "it", "La ricerca delle farmacie è scaduta prima di finire. Vuoi che "
     "riprovi?", "told", "scaduta"),
    ("pharmacy", "it", "La ricerca delle farmacie è ancora in corso.", "owed", None),
    # --- the error
    ("error", "en", "Sorry, I couldn't check the train times: the request hit an error. Want "
     "me to try another way?", "told", "hit an error"),
    ("error", "en", "Something went wrong on my end, so I don't have the next train to "
     "Millbrook. Should I try again?", "told", "went wrong"),
    ("error", "en", "The next train to Millbrook should be on the station's board.", "owed",
     None),
    ("error", "en", "I'm still checking the train times for you.", "owed", None),
    ("error", "es", "Lo siento, no pude mirar los horarios: la consulta dio un error. ¿Lo "
     "intento de otra forma?", "told", "error"),
    ("error", "es", "Sigo mirando los horarios del tren a Millbrook.", "owed", None),
    ("error", "it", "Mi dispiace, non ho potuto controllare gli orari: la richiesta ha dato "
     "un errore. Provo in un altro modo?", "told", "errore"),
    ("error", "it", "Sto ancora controllando gli orari del treno per Millbrook.", "owed",
     None),
    # --- out of credit
    ("credit", "en", "I couldn't check your calendar because the AI account has run out of "
     "credit. Want me to open your settings?", "told", "out of credit"),
    ("credit", "en", "There's a billing problem with the AI service, so your calendar "
     "couldn't be read.", "told", "billing problem"),
    ("credit", "en", "I couldn't check your calendar right now. Want me to try again?", "?",
     None),
    ("credit", "en", "Your calendar tomorrow looks clear.", "owed", None),
    ("credit", "es", "No pude mirar tu calendario porque la cuenta de IA se quedó sin "
     "crédito. ¿Abro los ajustes?", "told", "sin crédito"),
    ("credit", "es", "Mañana tu calendario parece libre.", "owed", None),
    ("credit", "it", "Non ho potuto controllare il calendario perché l'account IA ha finito "
     "il credito. Apro le impostazioni?", "told", "finito il credito"),
    ("credit", "it", "Domani il calendario sembra libero.", "owed", None),
]

# ---- the rule before #380 (owed.py until this change), for the "before" counts ---------
_OLD_FAILURE_TERMS = frozenset({"billing", "credit", "error", "empty"})


def old_about(question, request, outcome, text) -> bool:
    """Record.about for a failure record before #380: a term shared with its four-word
    failure terms, its request or the caller's question."""
    about = ((terms_of(failure_core(outcome), request) & _OLD_FAILURE_TERMS)
             | terms_of(request, "") | terms_of(question, ""))
    return bool(terms_of(text, "") & about)


def records():
    out = {}
    for name, (kind, detail, langs) in FAILURES.items():
        _, outcome = failure_outcome(kind, detail)
        for lang, (question, request) in langs.items():
            out[(name, lang)] = (question, request, outcome)
    return out


def cuts(text, marker):
    """(heard, rest, label) after every word that leaves something."""
    words = text.split()
    for n in range(1, len(words)):
        heard = " ".join(words[:n])
        yield heard, _unheard(text, heard), (
            "told" if marker and marker.lower() in heard.lower() else "owed")


async def main():
    llm = services.make_llm()
    sem = asyncio.Semaphore(int(os.getenv("TEAPORT_EVAL_CONCURRENCY") or "8"))
    lat = []

    async def ask(question, core, heard, rest):
        async with sem:
            started = time.monotonic()
            try:
                verdict = await asyncio.wait_for(
                    llm.judge_failure_cut(question, core, heard, rest), 30)
            except Exception:  # noqa: BLE001 -- an error is no verdict, as in owed.py
                verdict = None
            lat.append(time.monotonic() - started)
            return verdict

    async def majority(question, core, heard, rest):
        votes = await asyncio.gather(*(ask(question, core, heard, rest)
                                       for _ in range(TRIALS)))
        return sum(v is True for v in votes) * 2 > TRIALS, sum(v is None for v in votes)

    recs = records()
    t = collections.Counter()
    wrong = []
    replies = list(REPLIES)
    extra = os.getenv("TEAPORT_EVAL_FAILURE_REPLIES")
    if extra:
        # Tellings kept out of the repo: a JSON line each, {failure, lang, text, label} --
        # played to their end only (they carry no cut markers).
        import json
        with open(extra, encoding="utf-8") as f:
            replies += [(d["failure"], d["lang"], d["text"], d["label"], None)
                        for d in map(json.loads, f) if d.get("text")]
    # Played to its end: the whole reply, "they heard all of it".
    played = []
    for name, lang, text, label, _ in replies:
        question, request, outcome = recs[(name, lang)]
        played.append((name, lang, text, label, question, request, outcome))
    verdicts = await asyncio.gather(*(majority(q, failure_core(o), x, None)
                                      for _, _, x, _, q, _, o in played))
    for (name, lang, text, label, q, r, o), (heard, none) in zip(played, verdicts):
        t["none"] += none
        before = old_about(q, r, o, text)
        t[f"played_{label}"] += 1
        if label == "?":
            t[f"played_?_judge_{'heard' if heard else 'owed'}"] += 1
            continue
        for rule, closed in (("before", before), ("judge", heard)):
            if closed and label == "owed":
                t[f"played_{rule}_false_close"] += 1
                wrong.append((rule, "played", lang, text))
            elif not closed and label == "told":
                t[f"played_{rule}_false_owe"] += 1
                wrong.append((rule, "played", lang, text))
    # Cut after every word: the whole text first (a telling of it at all? -- the gate,
    # the count), then the prefix and the rest.
    whole = {text: heard for (_, _, text, *_), (heard, _) in zip(played, verdicts)}
    jobs = []
    for name, lang, text, label, marker in REPLIES:
        if label == "?" or os.getenv("TEAPORT_EVAL_PLAYED_ONLY"):
            continue
        q, r, o = recs[(name, lang)]
        for heard, rest, cl in cuts(text, marker):
            jobs.append((lang, text, label, heard, rest, cl, q, r, o))
    cut_verdicts = await asyncio.gather(*(majority(q, failure_core(o), h, rest)
                                          for _, _, _, h, rest, _, q, _, o in jobs))
    for (lang, text, label, heard, rest, cl, q, r, o), (v, none) in zip(jobs, cut_verdicts):
        t["none"] += none
        t[f"cut_{cl}"] += 1
        gate_before = old_about(q, r, o, text)
        gate_judge = whole[text]
        for rule, gate in (("before", gate_before), ("judge", gate_judge)):
            closed = gate and v
            if closed and cl == "owed":
                t[f"cut_{rule}_false_close"] += 1
                if rule == "judge" and label == "told":
                    wrong.append((rule, "cut early", lang, heard))
            elif not closed and cl == "told":
                t[f"cut_{rule}_false_owe"] += 1
                if rule == "judge":
                    wrong.append((rule, "cut late", lang, heard))
            # Counted as a cut telling (a second one drops the record): a telling of it?
            if gate and label == "owed":
                t[f"count_{rule}_false"] += 1
            elif not gate and label == "told":
                t[f"count_{rule}_missed"] += 1
    lat.sort() if lat else lat.append(0.0)
    print(f"model={setting('LLM_MODEL')} trials={TRIALS} replies={len(played)} "
          f"({t['played_told']} told, {t['played_owed']} owed, {t['played_?']} arguable), "
          f"cuts={len(jobs)} ({t['cut_told']} told, {t['cut_owed']} owed)")
    for rule in ("before", "judge"):
        print(f"{rule:6}: played false closes {t[f'played_{rule}_false_close']}/"
              f"{t['played_owed']}, false owes {t[f'played_{rule}_false_owe']}/"
              f"{t['played_told']}; cut false closes {t[f'cut_{rule}_false_close']}/"
              f"{t['cut_owed']}, false owes {t[f'cut_{rule}_false_owe']}/{t['cut_told']}; "
              f"cuts counted that tell nothing {t[f'count_{rule}_false']}, tellings not "
              f"counted {t[f'count_{rule}_missed']}")
    print(f"arguable (told, no why): judge HEARD {t['played_?_judge_heard']}, OWED "
          f"{t['played_?_judge_owed']}")
    print(f"no verdict: {t['none']}; latency p50 {lat[len(lat) // 2]:.2f}s "
          f"p90 {lat[int(len(lat) * .9)]:.2f}s")
    for rule, where, lang, text in wrong:
        print(f"  [{rule} {where} {lang}] {text[:110]}")
    # The judge must do better than the words did, both ways, and close no more than a
    # fifth of the replies that told nothing.
    for where in ("played", "cut"):
        assert t[f"{where}_judge_false_close"] <= t[f"{where}_before_false_close"], t
        assert t[f"{where}_judge_false_owe"] <= t[f"{where}_before_false_owe"], t
    assert t["played_judge_false_close"] * 5 < max(1, t["played_owed"]), t
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
