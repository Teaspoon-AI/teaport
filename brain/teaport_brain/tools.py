#
# teaport — voice-agent tools
#
# A small set of fast, REAL tools (the old "Special Pesto" / Hacker-News demo tools
# are gone). Two read the box directly (host status, time); three call the co-resident
# OpenClaw gateway over loopback (openclaw_client) so the voice agent shares the
# OpenClaw agent's actual capabilities — web search, web fetch, and the shared
# long-term memory — instead of canned demo data.
#
# Handlers are async and strictly best-effort: a failure returns a small payload for
# the model to speak around, never an exception into the realtime pipeline. The
# gateway calls take ~0.5-2s; that's a brief, acceptable pause on a voice turn.
#

import asyncio
import functools
import os
import re
import time

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams

from teaport_brain import openclaw_client as oc
from teaport_brain.env import env_flag
from teaport_brain.engine_tts import ENGINE_VOICES, LANG_NAMES

# Agent-first mode. Defined HERE and imported by gateway_server, not parsed separately in
# both: two copies of `os.getenv(...) in ("1","true")` could disagree if either was edited
# alone, leaving the mode half-enabled — the strict router directive installed while this
# module still spoke the "I'll work on that" ack the directive exists to suppress. It also
# only accepted 1/true, so `TEAPORT_AGENT_FIRST=on` silently meant off while docs/CONFIG.md
# promised otherwise; env_flag is the documented table.
AGENT_FIRST = env_flag("TEAPORT_AGENT_FIRST", False)

# The engine's serve log (decode ms/step lives here). Override per host.
ENGINE_LOG = os.getenv("ENGINE_LOG", os.path.expanduser("~/teaport-engine.log"))

HOST_STATUS = FunctionSchema(
    name="get_host_status",
    description=(
        "Get the live status of the machine this assistant runs on (an NVIDIA "
        "Jetson Orin Nano): free memory, CPU load, and the speech engine's "
        "current decode speed. Use it whenever the user asks how you're doing, "
        "how much memory or compute you have, or how fast you're running."
    ),
    properties={},
    required=[],
)

CURRENT_TIME = FunctionSchema(
    name="get_current_time",
    description="Get the current local date and time.",
    properties={},
    required=[],
)

WEB_SEARCH = FunctionSchema(
    name="web_search",
    description=(
        "Search the web for current or factual information you don't already know — "
        "news, weather, sports, prices, recent events, definitions, or anything you're "
        "unsure about. Use it instead of guessing. Returns a few top results to "
        "summarize aloud in one or two sentences."
    ),
    properties={"query": {"type": "string", "description": "What to search for"}},
    required=["query"],
)

WEB_FETCH = FunctionSchema(
    name="web_fetch",
    description=(
        "Fetch and read the contents of a specific web page. Use when the user gives "
        "you a URL, or to read more detail from one of your web_search results."
    ),
    properties={"url": {"type": "string", "description": "The full URL to fetch"}},
    required=["url"],
)

SEARCH_MEMORY = FunctionSchema(
    name="search_memory",
    description=(
        "Search your shared long-term memory — things the user told you earlier, by "
        "voice or by text. Use it when they refer back to something they told you, or "
        "ask what you know or remember about them."
    ),
    properties={"query": {"type": "string", "description": "What to recall"}},
    required=["query"],
)

REMEMBER = FunctionSchema(
    name="remember",
    description=(
        "Save a fact to your shared long-term memory when the user asks you to remember, "
        "note, or save something about them — a preference, relationship, plan, or life "
        "detail (e.g. 'remember that my dog is Biscuit', 'note that I prefer tea'). It "
        "becomes recallable later, by voice or by text. Don't use it for passing chit-chat "
        "or things you'd just look up."
    ),
    properties={"fact": {
        "type": "string",
        "description": "The fact to remember, as one clear standalone sentence about the user",
    }},
    required=["fact"],
)

ASK_OPENCLAW = FunctionSchema(
    name="ask_openclaw",
    description=(
        "Hand a request to your full desktop agent (OpenClaw) — every tool, deeper "
        "thinking — for multi-step or open-ended work your quick tools can't do. It "
        "also acts on this Discord server for you: post or announce to a channel, run "
        "a poll, pin a message, read or search recent messages, and check who's in a "
        "voice channel or what events are on the calendar. It can't change the server "
        "itself by voice — creating or deleting channels, roles, kicks or bans, or new "
        "scheduled events — so if the user asks for one of those, say it's a text or "
        "desktop task. Give a self-contained request, naming the channel in plain "
        "words (the agent finds the right one). Takes a few seconds; returns the "
        "agent's answer to summarize aloud."
    ),
    properties={"request": {
        "type": "string",
        "description": "The full request, self-contained, with any context the agent needs",
    }},
    required=["request"],
)

LIST_VOICES = FunctionSchema(
    name="list_voices",
    description=(
        "List the speaking voices available to you, grouped by language, plus your "
        "current voice. Use it before switching if you're unsure of voice names."
    ),
    properties={},
    required=[],
)

# Every id the engine will accept, flattened from the same table
# _switch_voice checks against.
_ALL_VOICES = sorted({v for vs in ENGINE_VOICES.values() for v in vs})

SWITCH_VOICE = FunctionSchema(
    name="switch_voice",
    description=(
        "Switch your speaking voice. The voice's language becomes your speaking "
        "language, so use this when the user starts speaking a different language "
        "(pick a voice for that language, then reply in it) or when they ask for a "
        "different voice."
    ),
    properties={"voice": {
        "type": "string",
        # The valid set, not three examples. Given only examples the model has to either
        # guess the id or spend a turn on list_voices to find it, and both go wrong:
        # measured 2026-08-23, gemma-4-31b-it guessed 'nova', 'Liam' and 'en_gb_emma' for
        # three ordinary requests -- all rejected by _switch_voice, so the user heard "I
        # couldn't find a voice called Liam" -- while gpt-oss-120b and qwen3.8-27b avoided
        # guessing only by calling list_voices first, which costs a whole extra round trip
        # before anything is spoken. An enum removes the choice: providers constrain the
        # argument to it, so an invalid id cannot be produced and no lookup is needed.
        #
        # Built from ENGINE_VOICES, which is what _switch_voice validates against, so the
        # advertised set and the accepted set cannot drift apart.
        "enum": _ALL_VOICES,
        "description": "Exact voice id. The first letter is the language (a=US English, "
                       "b=British, e=Spanish, f=French, h=Hindi, i=Italian, j=Japanese, "
                       "p=Portuguese, z=Mandarin) and the second is the gender (f/m), so "
                       "the Nova voice is 'af_nova' and Liam is 'am_liam'.",
    }},
    required=["voice"],
)

_BG: set = set()  # keep refs to background reindex tasks so they aren't GC'd mid-flight

# Return a tool result WITHOUT triggering another inference pass. Pipecat runs the
# LLM after every function-call result by default, which is right when the result
# carries an answer and wrong when it carries a placeholder: the model has nothing
# to say, so it invents something. Used by the async ask_openclaw path, whose real
# answer is spoken later by the follow-up injector (it rewrites the tool result and
# queues an LLMRunFrame, so inference happens once, on real data).
#
# A FUNCTION, not a module-level constant. FunctionCallResultProperties is a plain
# mutable dataclass carrying an on_context_updated callback field; one shared instance
# handed to every result path in every concurrent session is a single assignment away
# from leaking one turn's callback into an unrelated turn.
def no_inference() -> FunctionCallResultProperties:
    return FunctionCallResultProperties(run_llm=False)


def _mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _load_1min() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _decode_ms_per_step() -> float | None:
    """Latest 'NN.N ms/step' from the engine serve log, if present."""
    try:
        with open(ENGINE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    matches = re.findall(r"([\d.]+)\s*ms/step", tail)
    return float(matches[-1]) if matches else None


async def _get_host_status(params: FunctionCallParams):
    status = {
        "device": "NVIDIA Jetson Orin Nano 8GB",
        "memory_available_mb": _mem_available_mb(),
        "cpu_load_1min": _load_1min(),
        "decode_ms_per_step": _decode_ms_per_step(),
    }
    logger.debug(f"get_host_status -> {status}")
    await params.result_callback(status)


async def _get_current_time(params: FunctionCallParams):
    await params.result_callback({"local_time": time.strftime("%A %B %d, %Y %I:%M %p")})


async def _web_search(params: FunctionCallParams):
    query = (params.arguments or {}).get("query", "")
    results = await oc.web_search(query)
    if results is None:
        # Backend FAILURE (provider down / bot-blocked / timeout) — distinct from a
        # real empty result. Report it as an error so the LLM says "I can't search
        # right now" instead of a false "no results" (which reads as silence, or
        # tempts it to answer from memory / hallucinate). Not a retry hint.
        logger.debug(f"web_search({query!r}) -> FAILED (search backend unavailable)")
        await params.result_callback(
            {"error": "web search is unavailable right now — the search service did "
                      "not respond. Do not retry; tell the user you can't search at "
                      "the moment."})
        return
    logger.debug(f"web_search({query!r}) -> {len(results)} result(s)")
    await params.result_callback(
        {"results": results} if results else {"results": [], "note": "no results found"}
    )


async def _web_fetch(params: FunctionCallParams):
    url = (params.arguments or {}).get("url", "")
    text = await oc.web_fetch(url)
    if text is None:
        logger.debug(f"web_fetch({url!r}) -> FAILED (unreachable/blocked)")
        await params.result_callback(
            {"error": "couldn't reach that page — it may be blocking automated access"})
        return
    logger.debug(f"web_fetch({url!r}) -> {len(text)} chars")
    await params.result_callback(
        {"content": text} if text else {"error": "that page had no readable content"}
    )


async def _search_memory(params: FunctionCallParams):
    query = (params.arguments or {}).get("query", "")
    hits = await oc.memory_search(query, max_results=3)
    logger.debug(f"search_memory({query!r}) -> {len(hits)} hit(s)")
    await params.result_callback(
        {"memories": hits} if hits else {"memories": [], "note": "nothing relevant remembered"}
    )


# Native consults ride the relay's in-process agent machinery: we emit an
# openclaw_agent_consult tool_call over /talk, the relay runs the agent turn in
# the gateway process (no CLI/node startup — the ~5-8s tax of the fallback) and
# returns the result via the plugin's submitToolResult -> consult_bridge. The
# timeout covers the case where no relay-side runner completes the consult
# (config-dependent) — then we degrade to the proven CLI path. Two phases: the
# relay acks a live consult with a "working" notice within moments, so no ack
# inside ACK_TIMEOUT means no runner — fall back fast instead of burning the
# full window.
# How long to wait on an ACKED native consult before giving up. The useful
# consults return in ~15-30s; past ~45s the voice wait degrades faster than the
# answer improves (and the slow tail is usually an impossible query making the
# agent churn), so we cut it with an honest "taking too long" rather than dead air.
_NATIVE_CONSULT_TIMEOUT = float(os.getenv("TEAPORT_NATIVE_CONSULT_TIMEOUT", "45"))
# 1.5s, not 5: a live relay acks a native consult within moments, while the
# Discord bridge (a plain /talk client) never acks at all — so on that path the
# old 5s was pure dead time added to EVERY delegated action before the CLI
# fallback even started.
_NATIVE_CONSULT_ACK_TIMEOUT = float(os.getenv("TEAPORT_NATIVE_CONSULT_ACK_TIMEOUT", "1.5"))
# The pipecat function-call timeout for ask_openclaw MUST exceed the handler's own
# worst case (ACK 5s + native 45s = 50s) or pipecat abandons the call and drops the
# late-arriving answer. Kept as one knob so the two can't drift. (Only bounds the
# SYNC path; the ASYNC path returns in <1s — its wait is off the turn.)
_ASK_OPENCLAW_TIMEOUT = float(os.getenv("TEAPORT_ASK_OPENCLAW_TIMEOUT", "55"))
# ASYNC path: the consult runs off the turn as a background task, so it can wait far
# longer than a voice turn ever could — the answer is spoken as an unprompted
# follow-up whenever it lands (or an honest "couldn't get it" past this ceiling).
_ASYNC_CONSULT_TIMEOUT = float(os.getenv("TEAPORT_ASYNC_CONSULT_TIMEOUT", "180"))


def _consult_outcome(result):
    """(text, error) from a resolved consult result — text is None if empty."""
    from teaport_brain import consult_bridge

    if isinstance(result, dict) and result.get("error") \
            and not any(result.get(k) for k in ("text", "result", "output")):
        return None, str(result["error"])
    return (consult_bridge.extract_text(result) or None), None


async def _consult_progress(llm):
    """Deterministic 'still alive' narration for the silent background stretch.
    The ack sentence ends within ~2s but the CLI consult takes 15-30s, and dead
    air reads as a hang — the user has no idea anything is happening. Spoken
    lines, not the thinking bed: the bed is keyed to the in-turn function call,
    which the ASYNC path resolves immediately (hence 'pushed 0 chunks').
    Singleton per session: overlapping consults share ONE narrator — two
    narrators doubled every line audibly (observed live)."""
    if getattr(llm, "_teaport_progress_active", False):
        return
    llm._teaport_progress_active = True
    try:
        await asyncio.sleep(9)
        await llm.push_frame(TTSSpeakFrame("Still working on it."))
        await asyncio.sleep(13)
        await llm.push_frame(TTSSpeakFrame("Almost there — hang tight."))
    except asyncio.CancelledError:
        pass
    finally:
        llm._teaport_progress_active = False


async def _consult_and_followup(call_id, fut, request, followup, tool_call_id, llm=None):
    """Background waiter for the ASYNC ask_openclaw path. The turn already ended, so
    there's no tight voice deadline: wait out the consult, then hand the answer to
    the follow-up injector, which runs a fresh LLM turn so the bot SPEAKS it. Runs as
    a session-lifecycle task (params.llm.create_task) — a barge-in or the user moving
    on to another topic does NOT cancel the in-flight consult. tool_call_id lets the
    injector rewrite the placeholder tool result once the real outcome is known."""
    from teaport_brain import consult_bridge

    progress = asyncio.create_task(_consult_progress(llm)) if llm is not None else None

    async def deliver(answer):
        """Hand the outcome to the injector, narrator first."""
        # Stop the narrator BEFORE the answer is spoken, not in the finally below. It
        # is a countdown against dead air, and there is no dead air once the outcome is
        # known. Cancelling it afterwards used to be harmless because the injector
        # returned as soon as it had queued the LLM run; it now waits for the delivered
        # turn to finish speaking so it can retire its one-shot trigger, which left the
        # narrator running for the whole delivery. Observed live 2026-08-26 09:44: the
        # shop list was spoken at :25.0 and "Almost there — hang tight." landed at :37.3.
        if progress is not None:
            progress.cancel()
        await followup(request, answer, tool_call_id)

    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(fut),
                                            timeout=_NATIVE_CONSULT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            if not getattr(fut, "working", False):
                # Never acked — the relay didn't take it; run the CLI agent instead
                # (still async w.r.t. the turn, which already ended).
                reply = await oc.agent_consult(request)
                await deliver(reply or None)
                return
            result = await asyncio.wait_for(fut, timeout=_ASYNC_CONSULT_TIMEOUT)
        text, err = _consult_outcome(result)
        if err:
            logger.warning(f"ask_openclaw(async): consult errored: {err!r}")
        logger.info(f"ask_openclaw(async): follow-up ready ({len(text or '')} chars)")
        await deliver(text)
    except asyncio.TimeoutError:
        logger.warning(f"ask_openclaw(async): consult unfinished after "
                       f"{_ASYNC_CONSULT_TIMEOUT:.0f}s")
        await deliver(None)
    except asyncio.CancelledError:
        raise  # session teardown
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ask_openclaw(async) failed: {e!r}")
        try:
            await deliver(None)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if progress is not None:
            progress.cancel()
        consult_bridge.cancel(call_id)


async def _ask_openclaw(params: FunctionCallParams, followup=None):
    import uuid

    from teaport_brain import consult_bridge

    request = (params.arguments or {}).get("request", "").strip()
    if not request:
        await params.result_callback({"error": "empty request"})
        return

    # Fold duplicate dispatches BEFORE creating any consult machinery: gpt-oss
    # sometimes emits the same ask_openclaw twice a few seconds apart (observed
    # live), which doubled every ack, progress line, and follow-up. One
    # in-flight consult per exact request text; the duplicate resolves silently
    # and the original's follow-up reports for both.
    if followup is not None:
        inflight = getattr(params.llm, "_teaport_consults", None)
        if inflight is None:
            inflight = params.llm._teaport_consults = {}
        prior = inflight.get(request)
        if prior is not None and not prior.done():
            # No inference on a placeholder result — see no_inference() below.
            await params.result_callback(
                {"status": "duplicate",
                 "instruction": ("This exact request is already in progress; "
                                 "its outcome will arrive. Do not respond.")},
                properties=no_inference())
            return

    call_id = f"teaport-consult-{uuid.uuid4().hex[:12]}"
    fut = consult_bridge.create(call_id)
    await params.llm.push_frame(OutputTransportMessageUrgentFrame(
        message={"type": "tool_call", "call_id": call_id,
                 "name": "openclaw_agent_consult",
                 "args": {"question": request}}))

    # ASYNC path (a follow-up injector is wired — the OpenClaw relay). A full agent
    # turn can take 30-60s+, which can't block a voice turn (that was the whole
    # timeout-race that dropped answers). So hand the consult to a background waiter,
    # acknowledge NOW so the turn ends and the user can keep talking, and speak the
    # answer as an unprompted follow-up when it lands. This is the pattern mature
    # voice platforms use for slow sub-agent delegation.
    if followup is not None:
        task = params.llm.create_task(_consult_and_followup(
            call_id, fut, request, followup, params.tool_call_id, llm=params.llm))
        inflight[request] = task
        # Identity-checked pop: a same-text consult started AFTER this one
        # finished must not be evicted by this one's completion callback.
        task.add_done_callback(
            lambda t, r=request: inflight.pop(r, None) if inflight.get(r) is t else None)
        # Run NO inference on this placeholder (no_inference()). There is nothing for
        # the model to say: the user already heard the deterministic "I'll work on
        # that" ack, and the real outcome arrives later via the follow-up injector,
        # which rewrites this tool result and runs the LLM then. Asking the model to
        # respond here and discarding what it says is what produced fabricated
        # answers — prompt-level "don't claim it's done" failed three times live
        # (it announced "posted!" ~4s in), and a speech mute over the discarded
        # completion failed too (2026-08-12: a fully invented Hacker News headline
        # and item id reached the speaker).
        await params.result_callback({
            "status": "working_in_background",
            "instruction": (
                "The task is running in the background; the outcome will arrive "
                "later. Do not respond now.")},
            properties=no_inference())
        return

    # SYNC path (no follow-up injector — e.g. the WebRTC dev client): ack-gated wait,
    # CLI fallback only if the consult is never acked.
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(fut),
                                            timeout=_NATIVE_CONSULT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            if not getattr(fut, "working", False):
                logger.warning("ask_openclaw: native consult not acked; using CLI")
                reply = await oc.agent_consult(request)
                await params.result_callback(
                    {"answer": reply} if reply
                    else {"error": "the desktop agent did not answer in time"})
                return
            try:
                result = await asyncio.wait_for(fut, timeout=_NATIVE_CONSULT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"ask_openclaw: acked consult unfinished after "
                               f"{_NATIVE_CONSULT_TIMEOUT:.0f}s")
                await params.result_callback(
                    {"error": "the desktop agent is taking too long — try a narrower request"})
                return
        text, err = _consult_outcome(result)
        if err:
            logger.warning(f"ask_openclaw: native consult errored: {err!r}")
            await params.result_callback({"error": err})
        elif text:
            logger.info(f"ask_openclaw: native consult ok ({len(text)} chars)")
            await params.result_callback({"answer": text})
        else:
            await params.result_callback({"error": "the desktop agent returned no answer"})
    except asyncio.CancelledError:
        consult_bridge.cancel(call_id)
        raise  # barge-in: propagate so the task actually cancels
    finally:
        consult_bridge.cancel(call_id)


async def _remember(params: FunctionCallParams):
    fact = (params.arguments or {}).get("fact", "").strip()
    if not fact:
        await params.result_callback({"saved": False, "note": "nothing to remember"})
        return
    # Direct/sidecar write: append the fact to the shared daily memory note — instant and
    # deterministic, so it's saved (and keyword-recallable) before we even reply. Then
    # reindex in the background so it's *semantically* recallable too (~7 s, off the turn).
    # Zero-egress; no LLM/consult call. The `saved` flag reflects the real write result.
    saved = oc.remember_note(fact)
    if saved:
        task = asyncio.create_task(oc.reindex_memory())
        _BG.add(task)
        task.add_done_callback(_BG.discard)
        logger.info(f"remember: saved + reindexing in background: {fact[:60]!r}")
        await params.result_callback({"saved": True, "fact": fact})
    else:
        logger.warning(f"remember: write failed for: {fact[:60]!r}")
        await params.result_callback({"saved": False, "note": "couldn't save that"})


async def _list_voices(params: FunctionCallParams, tts=None):
    await params.result_callback({
        "current_voice": getattr(tts, "_voice", None),
        "current_language": LANG_NAMES.get(
            getattr(tts, "espeak_language", ""), getattr(tts, "espeak_language", "?")),
        "voices_by_language": {LANG_NAMES.get(k, k): v
                               for k, v in ENGINE_VOICES.items()},
        "note": "Japanese and Mandarin voices use a lower-quality phonemizer.",
    })


async def _switch_voice(params: FunctionCallParams, tts=None):
    voice = str((params.arguments or {}).get("voice", "")).strip()
    lang = next((k for k, vs in ENGINE_VOICES.items() if voice in vs), None)
    if lang is None or tts is None:
        await params.result_callback(
            {"ok": False, "error": f"unknown voice {voice!r}",
             "hint": "call list_voices for the available names"})
        return
    result = tts.set_voice(voice)
    if result.get("ok"):
        result["note"] = (f"You now speak with {voice}. From now on reply only in "
                          f"{result['language_name']}.")
        logger.info(f"switch_voice: {voice} ({result['language_name']})")
    await params.result_callback(result)


def build_tools_schema() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[HOST_STATUS, CURRENT_TIME, WEB_SEARCH, WEB_FETCH,
                        SEARCH_MEMORY, REMEMBER, ASK_OPENCLAW,
                        LIST_VOICES, SWITCH_VOICE]
    )


_HANDLERS = {
    "get_host_status": _get_host_status,
    "get_current_time": _get_current_time,
    "web_search": _web_search,
    "web_fetch": _web_fetch,
    "search_memory": _search_memory,
    "remember": _remember,
    "ask_openclaw": _ask_openclaw,
}

# "Working on it" speech is primarily the MODEL's job: VOICE_OVERLAY tells it to say
# one short, request-specific line before a web search/fetch, so the wording varies
# naturally with context. But gpt-oss often goes straight to the tool call with no
# text (observed live) — so a deterministic NET below speaks a contextual line built
# from the tool's own arguments whenever the model stayed silent. Each completion in
# a tool CHAIN resets the tracker, so a silent multi-step chain produces audible
# progress ("Looking up X." ... "Opening that page.") instead of a minute of dead air.


def _args_summary(args: dict, cap: int = 60) -> str:
    """One short human-readable value (query, url, fact, ...)."""
    for v in (args or {}).values():
        s = str(v).strip()
        if s:
            return s if len(s) <= cap else s[: cap - 3] + "..."
    return ""


def _fallback_line(name: str, args: dict, lang: str) -> str | None:
    """Deterministic-but-contextual 'working on it' line, per tool and language."""
    if name == "web_search":
        q = _args_summary(args, cap=40)
        return {"es": f"Buscando {q}.", "it": f"Cerco {q}.",
                "cmn": f"我来查一下{q}。"}.get(lang, f"Looking up {q}.") if q else None
    if name == "web_fetch":
        return {"es": "Abriendo la página.", "it": "Apro la pagina.",
                "cmn": "我打开那个页面。"}.get(lang, "Opening that page.")
    if name == "ask_openclaw":
        # Agent-first: every turn is a consult — a stock ack per turn would be
        # noise (ThinkingSound covers the wait) and would corrupt TURN-TIMING's
        # tts_first_audio, which must mark the ANSWER in this mode.
        if AGENT_FIRST:
            return None
        return {"es": "Voy a trabajar en eso, un momento.",
                "it": "Ci lavoro subito, un attimo.",
                "cmn": "我来处理，请稍等。"}.get(
                    lang, "I'll work on that — give me a moment.")
    return None  # instant tools: speaking would take longer than the call


def _install_spoke_tracker(llm) -> None:
    """Track whether the CURRENT completion emitted any real text. Patched at the
    service level (not a pipeline processor) so the flag is guaranteed set before
    function handlers run — downstream processors race the handler, this doesn't."""
    if getattr(llm, "_teaport_spoke_patched", False):
        return
    llm._teaport_spoke_patched = True
    llm._teaport_spoke = False
    orig_push = llm.push_frame

    async def push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMFullResponseStartFrame):
            llm._teaport_spoke = False
        elif isinstance(frame, LLMTextFrame):
            if any(c.isalnum() for c in getattr(frame, "text", "")):
                llm._teaport_spoke = True
        return await orig_push(frame, direction)

    llm.push_frame = push_frame


async def _bubble(llm, text: str) -> None:
    """A closed, display-only assistant bubble in the Talk view (never spoken).
    The view renders markdown, so these are styled like the native chat's tool
    cards: blockquote + bold tool name + code-span argument."""
    await llm.push_frame(OutputTransportMessageUrgentFrame(
        message={"type": "transcript", "role": "assistant", "final": True,
                 "text": text}))


def _wrap(name, handler, lang_fn):
    async def wrapped(params: FunctionCallParams):
        # Best-effort UX around the call — a display/speech failure must never
        # break the tool itself.
        try:
            args = params.arguments or {}
            # Machine-readable event: the plugin forwards it to the relay's
            # onToolCall (tool.call Talk event). Today's control-ui doesn't render
            # those yet, so ALSO send the styled bubble the Talk view can render.
            await params.llm.push_frame(OutputTransportMessageUrgentFrame(
                message={"type": "tool_call", "call_id": params.tool_call_id,
                         "name": name, "args": args}))
            summary = _args_summary(args)
            await _bubble(params.llm,
                          f"> 🔧 **{name}**" + (f" · `{summary}`" if summary else ""))
            if not getattr(params.llm, "_teaport_spoke", True):
                line = _fallback_line(name, args, lang_fn())
                if line:
                    await params.llm.push_frame(TTSSpeakFrame(line))

            # Mirror the native card's status dimension: intercept the result to
            # post a failure bubble (with duration) when the tool errors. Success
            # stays silent — the spoken answer is the success signal, and a ✓ per
            # call would just clutter the transcript.
            orig_cb = params.result_callback
            t0 = time.monotonic()

            async def result_cb(result, **kwargs):
                try:
                    failed = isinstance(result, dict) and (
                        result.get("error") or result.get("ok") is False)
                    if failed:
                        detail = str(result.get("error") or "failed")
                        await _bubble(params.llm,
                                      f"> ⚠️ **{name}** — {detail} "
                                      f"· {time.monotonic() - t0:.1f}s")
                except Exception:  # noqa: BLE001
                    pass
                await orig_cb(result, **kwargs)

            params.result_callback = result_cb
        except Exception as e:  # noqa: BLE001
            logger.debug(f"tool-call display skipped for {name}: {e!r}")
        await handler(params)
    return wrapped


def register_tools(llm, lang: str = "en-us", tts=None, followup=None) -> None:
    """Wire the tool handlers onto `llm`. `followup`, if given, is an async
    `(request, text|None) -> None` injector that speaks a background consult's answer
    as an unprompted turn; providing it switches ask_openclaw to the ASYNC path."""
    _install_spoke_tracker(llm)
    if tts is not None:
        # Live language: read the TTS service at call time, so a mid-session
        # switch_voice also switches the fallback lines' language.
        def lang_fn():
            return getattr(tts, "espeak_language", lang).split("-")[0]
    else:
        def lang_fn():
            return (lang or "en-us").split("-")[0]
    handlers = dict(_HANDLERS)
    if tts is not None:
        handlers["list_voices"] = functools.partial(_list_voices, tts=tts)
        handlers["switch_voice"] = functools.partial(_switch_voice, tts=tts)
    if followup is not None:
        handlers["ask_openclaw"] = functools.partial(_ask_openclaw, followup=followup)
    for name, handler in handlers.items():
        # ask_openclaw runs a full agent turn (~15-35s); pipecat's default 10s
        # function-call timeout abandons it mid-flight and discards the answer
        # that arrives later (the "weather never came back" bug). Give it a
        # ceiling above the handler's own consult caps; the fast tools keep 10s.
        kw = {"timeout_secs": _ASK_OPENCLAW_TIMEOUT} if name == "ask_openclaw" else {}
        llm.register_function(name, _wrap(name, handler, lang_fn), **kw)
