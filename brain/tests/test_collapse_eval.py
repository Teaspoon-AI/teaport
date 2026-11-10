#
# test_collapse_eval.py -- the reply after a tool result against the collapse (#384),
# OPT-IN: a live LLM, hundreds of calls. Skipped unless TEAPORT_EVAL_COLLAPSE is set and
# the model's endpoint is reachable (appliance.py), so the suite reports it skipped.
#
# From a checkout's brain/ (the env file is read as systemd reads it --
# appliance.load_env_file; never `source` it):
#
#   TEAPORT_EVAL_COLLAPSE=1 TEAPORT_EVAL_ENV_FILE=/etc/teaport/brain.env \
#       PYTHONPATH=$PWD /opt/teaport/venv/bin/python tests/test_collapse_eval.py
#
# [TEAPORT_EVAL_TRIALS=8] (requests per scenario and shape: 96 a shape, 288 in all)
# [TEAPORT_EVAL_SHAPES=lead,leadmem,long]
#
# A PROVIDER CHECK. gpt-oss-120b, on some of its hosts (Groq, Cerebras), now and then
# breaks down mid-reply into no-break spaces and "…" -- the guard trips and speaks its
# recovery line -- most often in the answer to a tool result. Point the env file's
# LLM_EXTRA_BODY at the provider to check. Each context here is a synthetic post-tool turn
# (en/es/it: the time, the device's memory, a web search, a consult's answer, a memory
# saved, a voice or volume change, goodbye) behind the room's own starting context, built
# from brain code (persona, tools, mood paragraph, the app-event line), in three shapes:
# "lead" (the starting context as is), "leadmem" (a memory note before the caller's last
# words) and "long" (that, after up to three earlier answered tool rounds in the turn's
# language). A completion is a collapse when the guard's own oracle says so
# (llm_text_guard.is_degenerate, on the raw text). Each request goes out as the session
# sends it: the service's own request builder, model, routing and key.
#
# Measured 2026-11-09 (reasoning low), collapses / requests: OpenRouter -> Groq, lead 3/96,
# leadmem 1/96, long 7/96 (11/288). OpenRouter -> BaseTen served the same synthetic turns
# behind the box's own host prompt with no collapse in 288 (#384's spike; Groq there: 17/288).
#
# THE VERDICT, over the requests the provider answered (a refused one has no reply to
# judge): it fails at 6 collapses or more in 288 answered. That tells a provider that
# collapses about 4% of the time (Groq, measured 3.8%: fails 96% of runs; at 4%, 98%)
# from a clean one (0%: never fails); at a true 2% it fails about half the runs, at 1%
# 7%. More than 5% of the requests refused (a bad key, a spent limit, a routing the
# provider rejects) fails too, loudly: the rate would mean nothing.
#
# Tried and rejected (#384, PR #401): rendering each request with one system message (the
# starting run joined, later notes as tagged user messages). It took "lead" to 0/96 but
# not the rest (leadmem 2/96, long 8/96), and owed notes were told less (57/96 -> 43/96
# pooled over four runs). Joining the later notes into the prompt too made the model look
# a memory note's fact up instead of using it (7/24 against 24/24).
#
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402

appliance.require_env("TEAPORT_EVAL_COLLAPSE", "an opted-in live evaluation of the collapse")
appliance.load_env_file()                  # before anything reads a setting

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from teaport_brain import mood_tag, services, tools  # noqa: E402
from teaport_brain.agent_session import _build_initial_messages  # noqa: E402
from teaport_brain.client_notes import SYSTEM_LINE  # noqa: E402
from teaport_brain.llm_text_guard import is_degenerate  # noqa: E402
from teaport_brain.persona import build_system_prompt, load_persona  # noqa: E402
from teaport_brain.settings import setting  # noqa: E402

appliance.require_reachable(setting("LLM_BASE_URL") or "", "the language model's endpoint")
TRIALS = int(os.getenv("TEAPORT_EVAL_TRIALS") or "8")      # per scenario and shape
SHAPES = (os.getenv("TEAPORT_EVAL_SHAPES") or "lead,leadmem,long").split(",")
CONCURRENCY = 12

# The room's session: its tools and its starting context, as agent_session builds them.
CTX = tools.ToolContext(has_tts=True, front_end="room", client_features=tools.parse_client_features(
    "volume,restart,local,phonemes,mood,typing,pause,sleep"))
SESSION_TOOLS = tools.active_tools(CTX)
PROMPT = build_system_prompt(load_persona(), tools=SESSION_TOOLS) + " " + mood_tag.PARAGRAPH
START = _build_initial_messages(PROMPT, None, context_notes=True)
assert sum(m["role"] == "system" for m in START) >= 2 and START[-1]["content"] == SYSTEM_LINE

MEMORY = ("You remember these things about the user from earlier chats (by text or by voice). "
          "Use them if relevant; don't mention that you looked them up:\n"
          "- The user likes their coffee black.\n- The user's dog is called Biscuit.")

_ids = iter(range(1, 10**6))


def _call(name, args):
    cid = f"call_{next(_ids):05d}"
    return ({"role": "assistant", "tool_calls": [{"id": cid, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}, cid)


def _round(ask, name, args, result, before=()):
    """(the caller's words and what came before them, the call, its result)."""
    call, cid = _call(name, args)
    return [*before, {"role": "user", "content": ask}, call,
            {"role": "tool", "content": json.dumps(result), "tool_call_id": cid}]


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


SEARCH = {"results": [
    {"title": "Riverside Public Library - Hours", "snippet": "The main branch is open Monday to "
     "Saturday from 9 a.m. to 8 p.m. and Sunday from noon to 5 p.m.", "site": "library.example.org"},
    {"title": "Riverside library reopens", "snippet": "The main branch reopened on Tuesday with a "
     "new children's wing.", "site": "news.example.com"}]}
SEARCH_ES = {"results": [
    {"title": "Farmacia Central - Horario", "snippet": "Abierta de lunes a sábado de 9:00 a 21:00.",
     "site": "farmacia.example.es"}]}
SEARCH_IT = {"results": [
    {"title": "Meteo Bologna domani", "snippet": "Cielo nuvoloso al mattino, schiarite nel "
     "pomeriggio. Temperature tra 9 e 16 gradi.", "site": "meteo.example.it"}]}

# (name, the turn: the caller's words, the call and its result, with what came first, and
# what the model answered, for the earlier rounds of the "long" shape)
SCENARIOS = [
    ("time", _round("What time is it?", "get_current_time", {},
                    {"local_time": "Monday November 09, 2026 03:11 PM"},
                    [_u("Hey, are you there?"), _a("I'm here. What can I do for you?")]),
     "It's three eleven in the afternoon."),
    ("memory", _round("How much memory is free on you right now?", "get_host_status", {},
                      {"device": "NVIDIA Jetson Orin Nano 8GB", "memory_available_mb": 680,
                       "cpu_load_1min": 1.2, "decode_ms_per_step": None},
                      [_u("Good morning."), _a("Good morning! How can I help?")]),
     "About six hundred eighty megabytes are free."),
    ("search", _round("When is the Riverside library open?", "web_search",
                      {"query": "Riverside library hours"}, SEARCH,
                      [_u("Can you look something up for me?"), _a("Sure, what should I look up?")]),
     "The main branch is open Monday to Saturday from nine to eight."),
    ("voice", _round("Bien. ¿Puedes cambiar a inglés, por favor?", "switch_voice",
                     {"voice": "af_nova"},
                     {"ok": True, "voice": "af_nova", "language": "en-us",
                      "language_name": "English (US)",
                      "note": "You now speak with af_nova. From now on reply only in English (US)."},
                     [_u("Hola, ¿qué tal?"), _a("¡Hola! Muy bien, ¿y tú?")]),
     "Sure, I'm speaking English now."),
    ("remember", _round("Remember that my sister Anna's birthday is on March third.", "remember",
                        {"text": "The user's sister Anna's birthday is March 3."},
                        {"ok": True, "saved": "The user's sister Anna's birthday is March 3."}),
     "Got it, I'll remember that."),
    ("consult", _round("What do I have tomorrow?", "openclaw_agent_consult",
                       {"request": "What is on my calendar tomorrow?"},
                       {"status": "done", "answer": "Tomorrow: dentist at 10:30, lunch with Marco "
                        "at 1 p.m."}),
     "Tomorrow you have the dentist at ten thirty and lunch with Marco at one."),
    ("time_es", _round("¿Qué hora es?", "get_current_time", {},
                       {"local_time": "Monday November 09, 2026 09:42 AM"}),
     "Son las nueve y cuarenta y dos de la mañana."),
    ("search_es", _round("¿A qué hora abre la Farmacia Central mañana?", "web_search",
                         {"query": "Farmacia Central horario"}, SEARCH_ES,
                         [_u("Buenas tardes."), _a("¡Buenas tardes! ¿En qué te ayudo?")]),
     "Mañana abre a las nueve."),
    ("search_it", _round("Che tempo farà domani a Bologna?", "web_search",
                         {"query": "meteo Bologna domani"}, SEARCH_IT),
     "Domani a Bologna sarà nuvoloso al mattino, poi schiarite."),
    ("memory_it", _round("Quanta memoria hai libera?", "get_host_status", {},
                         {"device": "NVIDIA Jetson Orin Nano 8GB", "memory_available_mb": 1210,
                          "cpu_load_1min": 0.8, "decode_ms_per_step": None},
                         [_u("Ciao!"), _a("Ciao! Come posso aiutarti?")]),
     "Ho circa milleduecento megabyte liberi."),
    ("volume", _round("Turn it up a bit.", "set_volume", {"level": 60}, {"ok": True, "volume": 60}),
     "Done, it's at sixty percent now."),
    ("end", _round("Thanks, that's all for now. Bye!", "end_conversation", {}, {"ok": True}),
     "Bye!"),
]


# The earlier rounds of the "long" shape, by the language of the turn: a session keeps to
# its language (a voice change or a goodbye is never an earlier round).
_EARLIER = {"en": ("time", "memory", "search", "remember", "consult", "volume"),
            "es": ("time_es", "search_es"), "it": ("search_it", "memory_it")}


def _lang(name: str) -> str:
    return name.rsplit("_", 1)[1] if name.endswith(("_es", "_it")) else "en"


def shaped(k: int, shape: str) -> list:
    """Scenario k's turn behind the starting context, in `shape` (the module comment)."""
    name, turn, _ = SCENARIOS[k % len(SCENARIOS)]
    turn = json.loads(json.dumps(turn))
    if shape in ("leadmem", "long"):
        last_user = max(i for i, m in enumerate(turn) if m["role"] == "user")
        turn.insert(last_user, {"role": "system", "content": MEMORY})
    history = []
    if shape == "long":
        pool = [n for n in _EARLIER[_lang(name)] if n != name]
        by_name = {n: (t, a) for n, t, a in SCENARIOS}
        for j in range(min(3, len(pool))):
            earlier, answer = by_name[pool[(k + j) % len(pool)]]
            history += json.loads(json.dumps(earlier)) + [_a(answer)]
    return [dict(m) for m in START] + history + turn


# ------------------------------------------------------------------------- the requests

LLM = services.make_llm()
TOOLS_SCHEMA = tools.build_tools_schema(CTX)
ADAPTER = OpenAILLMAdapter()
_sem = asyncio.Semaphore(CONCURRENCY)


async def complete(messages: list) -> str:
    """The completion's text (a tool call in it as "<call NAME>"), the request built as
    the session builds it."""
    params = ADAPTER.get_llm_invocation_params(LLMContext(messages, tools=TOOLS_SCHEMA),
                                               convert_developer_to_user=False)
    body = LLM.build_chat_completion_params(params)
    async with _sem:
        for attempt in range(3):
            try:
                stream = await LLM._client.chat.completions.create(**body)
                text = ""
                async for chunk in stream:
                    for c in chunk.choices or []:
                        if c.delta and c.delta.tool_calls:
                            text += "".join(f"<call {t.function.name}>" for t in c.delta.tool_calls
                                            if t.function and t.function.name)
                        text += (c.delta.content or "") if c.delta else ""
                return text
            except Exception as e:  # noqa: BLE001 -- a refused or dropped request is retried
                if attempt == 2:
                    # (a provider's refusal of the model's own tool call, say: no reply to
                    # judge, counted apart)
                    return f"<error {type(e).__name__}>"
                await asyncio.sleep(2)


REFUSED_MAX = 0.05     # of the requests: more, and the run measured nothing
COLLAPSE_MAX = 0.02    # of the answered: at 288, 6 collapses or more fail


def verdict(texts: list) -> tuple[int, str]:
    """(exit code, why) for the completions of a run (complete()'s texts): the collapse
    rate over the answered ones, unless too many were refused to judge."""
    refused = sum(t.startswith("<error") for t in texts)
    answered = len(texts) - refused
    bad = sum(is_degenerate(t) for t in texts if not t.startswith("<error"))
    if texts and refused > REFUSED_MAX * len(texts):
        return 1, (f"FAIL: the provider refused {refused} of {len(texts)} requests -- check "
                   "the key, its credit limit and LLM_EXTRA_BODY's routing; no collapse rate "
                   "can be read from this run")
    if answered and bad >= COLLAPSE_MAX * answered:
        return 1, (f"FAIL: {bad} collapses in {answered} answered: this provider collapses "
                   "too often for gpt-oss-120b")
    return 0, f"ALL PASS ({bad} collapses in {answered} answered, {refused} refused)"


async def main() -> int:
    every = []
    for shape in SHAPES:
        n = TRIALS * len(SCENARIOS)
        texts = await asyncio.gather(*(complete(shaped(k, shape)) for k in range(n)))
        bad = [t for t in texts if is_degenerate(t)]
        errors = sum(t.startswith("<error") for t in texts)
        print(f"collapse {shape:8s} {len(bad)}/{n - errors}"
              + (f" ({errors} refused by the provider)" if errors else ""))
        for t in bad[:2]:
            print(f"    e.g. {t[:60]!r}")
        by = {}
        for k, t in enumerate(texts):
            if is_degenerate(t):
                name = SCENARIOS[k % len(SCENARIOS)][0]
                by[name] = by.get(name, 0) + 1
        if by:
            print(f"    by scenario: {by}")
        every += texts
    code, why = verdict(every)
    print(why)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
