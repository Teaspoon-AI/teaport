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
# THE TOOL CONTRACT. Every tool is one `Tool` record in TOOLS (bottom of the schemas):
#   - its schema (what the model sees) and its handler (bound per session);
#   - an on/off switch, TEAPORT_TOOL_<NAME>, with a per-tool default (docs/CONFIG.md
#     "Tools"; all of today's tools default on, restart_session off);
#   - what it NEEDS to work: "agent" (the co-resident OpenClaw gateway,
#     TEAPORT_AGENT=openclaw, see agent_backend.py), "tts" (the session's voice),
#     "client:<feature>" (the connected client can do it, announced on /talk as
#     ?features=...), or "host:<part>" (something installed on this box: HOST_CHECKS);
#   - its call timeout and the phrase the system prompt names it with.
# active_tools() is the ONE place that decides which tools a session has: the schema
# the model is offered, the handlers registered on the LLM and the tools the system
# prompt names (persona.build_system_prompt) all come from it, so they cannot disagree.
# A tool switched off, or missing what it needs, is not offered, not registered and
# not named.
#
# Client tools (needs "client:<feature>") are performed by the client, not here: the
# handler sends {"type": "client_tool", "call_id", "name", "args"} and waits for the
# client's {"type": "tool_result", "call_id", "result"} (the same message, and the same
# consult_bridge registry, the plugin already answers consults with). Only a client
# that announced the feature is ever asked; the local audio bridge
# (local_audio.py) announces volume and restart.
#

import asyncio
import functools
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

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

from teaport_brain import consult_bridge
from teaport_brain import openclaw_client as oc
from teaport_brain.agent_backend import HAS_AGENT
from teaport_brain.env import env_flag
from teaport_brain.engine_tts import ENGINE_VOICES, LANG_NAMES

# Agent-first mode. Defined HERE and imported by gateway_server, not parsed separately in
# both: two copies of `os.getenv(...) in ("1","true")` could disagree if either was edited
# alone, leaving the mode half-enabled — the strict router directive installed while this
# module still spoke the "I'll work on that" ack the directive exists to suppress. It also
# only accepted 1/true, so `TEAPORT_AGENT_FIRST=on` silently meant off while docs/CONFIG.md
# promised otherwise; env_flag is the documented table.
AGENT_FIRST = env_flag("TEAPORT_AGENT_FIRST", False)
# Agent-first routes EVERY turn through ask_openclaw; without a gateway that tool is not
# registered, so the directive would send every turn to a tool that does not exist.
if AGENT_FIRST and not HAS_AGENT:
    logger.warning("TEAPORT_AGENT_FIRST is on but TEAPORT_AGENT=none — ignoring it "
                   "(agent-first needs a gateway to consult)")
    AGENT_FIRST = False

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

SET_VOLUME = FunctionSchema(
    name="set_volume",
    description=(
        "Change the volume of the speaker you talk through, when the user asks you to "
        "be louder, quieter, or to set a level. Give either direction or level. "
        "Returns the new level (percent) to mention in a few words."
    ),
    properties={
        "direction": {"type": "string", "enum": ["louder", "quieter"],
                      "description": "A step up or down"},
        "level": {"type": "integer", "minimum": 0, "maximum": 100,
                  "description": "An exact level in percent (0 is silent)"},
    },
    required=[],
)

RESTART_SESSION = FunctionSchema(
    name="restart_session",
    description=(
        "Start a fresh conversation: this session ends and a new one begins with an "
        "empty context and a greeting. Only when the user explicitly asks to restart, "
        "reset, or start over. Call it straight away, with no line before it; when it "
        "returns, say one short goodbye — the restart happens once that has played."
    ),
    properties={},
    required=[],
)

WIFI_SETUP = FunctionSchema(
    name="wifi_setup",
    description=(
        "Connect this box to a different Wi-Fi network when the user asks to set up, "
        "change, or switch its Wi-Fi (for example to a phone hotspot). It takes over "
        "the conversation, asks the user to confirm and talks them through it on their "
        "phone; say nothing yourself."
    ),
    properties={},
    required=[],
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
#
# ONE budget for every lane, sized to the slowest one we cannot change. A Talk consult
# runs in OpenClaw's Control UI, which gives up at a hard-coded 120 s and submits "OpenClaw
# tool call timed out" (ui/src/pages/chat/talk/shared.ts, 2026.9.7) — nothing can
# arrive after that, so the old 180 s only meant a minute of waiting on a result that
# was never coming. 130 s = that 120 s plus room for its submission to land. The same
# budget now goes to the gateway/CLI lane (SIP, or a relay that never acks), which used
# to get CONSULT_TIMEOUT's 45 s: 11 of 13 SIP consults on 2026-09-30..10-02 died there,
# on the same kinds of request (local search, news) that took 45-100 s on Talk (#80).
_ASYNC_CONSULT_TIMEOUT = float(os.getenv("TEAPORT_ASYNC_CONSULT_TIMEOUT", "130"))


# What the model is told while a consult is still running. The placeholder stays in
# the context, as this tool's result, on EVERY turn until the follow-up rewrites it --
# not just the turn that started the consult -- so it has to cover the turns the caller
# takes in the meantime. "Do not respond now" alone covered only the first one: asked
# "did you get any news on that?" mid-consult, the model filled the gap from its own
# knowledge and presented it as the finding. Live twice: 2026-10-03 04:33, an invented
# RX 580 framework list ("here's what I found: ROCm 5.7 with vLLM..."); 19:08, a 60 s
# "rundown" of French rioting news quoting a prime minister out of office since 2024,
# while that consult was still running and later timed out (#80). Replaying the 04:33
# contexts against the live model: 5/12 invented results with the old wording, 0/12
# with this. It does not forbid general knowledge -- only passing it off as results.
_PENDING_RULE = (
    "Its outcome will arrive later as a separate notice. Until it does, you have NO "
    "results for this request: if the user asks about it, say you're still waiting on "
    "it, and never present your own knowledge as something you found.")


def _consult_outcome(result) -> oc.ConsultOutcome:
    """The outcome of a resolved native consult (see consult_bridge.classify)."""
    from teaport_brain import consult_bridge

    return oc.ConsultOutcome(*consult_bridge.classify(result))


def _describe(outcome: oc.ConsultOutcome) -> str:
    """One log phrase for an outcome."""
    if outcome.text:
        return f"answered ({len(outcome.text)} chars)"
    return f"{outcome.failure}: {outcome.detail}" if outcome.detail else str(outcome.failure)


# How long past each line's moment the narrator will wait for a conversational gap
# before giving up on that line. Short: a status update is worth saying in a lull,
# never worth cutting in for -- the answer itself arrives as the follow-up regardless.
_PROGRESS_GAP_WAIT = 6.0
# WHEN each line is due, in seconds from the start of the consult. Wall-clock
# instants, not countdowns between lines: a gap wait for one line counts against the
# next line's moment instead of pushing it out, so a conversation with no gaps (every
# line skipped) still ends the narration on time rather than 2 x 6s late. Named so
# tests can shorten it.
_PROGRESS_SCHEDULE = (9.0, 22.0)
# Caps on the topic echo: words, and characters (a request is often one long line).
_PROGRESS_TOPIC_WORDS = 9
_PROGRESS_TOPIC_CHARS = 60
# A cut must not leave the echo hanging on a function word ("...pastry shop in.").
_PROGRESS_TOPIC_TRAILING = frozenset(
    "a an the in on at of to for with and or but by from near into onto about that this "
    "these those my your our their its is are was were be if as than then".split())


def _topic_phrase(request):
    """A short echo of the request, for the progress line: its opening phrase, cut at
    the first clause boundary or the caps, and never left hanging on a function word.

    It is the MODEL's restatement of what the user asked -- ask_openclaw's `request`
    argument, which the tool schema asks for as "the full request, self-contained" --
    not the user's transcript, so it can misname the topic the way any paraphrase can.
    But it is the same text the agent is answering, which is what a status line should
    name. Anything that is not prose (a URL, JSON, code) yields "" and the generic
    line is used instead: nine tokens of that read aloud is worse than "still working
    on it"."""
    text = " ".join((request or "").split())
    if not text or "://" in text or any(c in text for c in "{}[]<>=`"):
        return ""
    words = []
    for w in text.split():
        words.append(w)
        if len(" ".join(words)) > _PROGRESS_TOPIC_CHARS:
            words.pop()
            break
        if w[-1] in ",;:.!?" and len(words) >= 3:  # a clause ended: the natural cut
            break
        if len(words) >= _PROGRESS_TOPIC_WORDS:
            break
    while words and words[-1].rstrip(".,:;!?").lower() in _PROGRESS_TOPIC_TRAILING:
        words.pop()
    phrase = " ".join(words).rstrip(" .,:;!?\u2014-")
    return phrase if any(c.isalpha() for c in phrase) else ""


def _progress_line(request, n):
    """The nth (0-based) progress line, naming the topic when the request gives one.
    'that <topic>' keeps it tied to the request, so a status update heard a minute
    later still has a referent — the confusion was 'still working on it' landing
    after unrelated turns with no 'it' in sight."""
    topic = _topic_phrase(request)
    # The topic rides as a dash appositive after a complete clause, never inside a
    # grammatical slot: the request is often a verb phrase ("find good pastry shops"),
    # and "almost there on the find good pastry shops" is broken where
    # "almost there — find good pastry shops" reads fine spoken.
    if n == 0:
        return f"Still working on that — {topic}." if topic else "Still working on it."
    return f"Almost there — {topic}." if topic else "Almost there — hang tight."


async def _consult_progress(llm, request=None, gate=None):
    """'Still alive' narration for the silent background stretch. The ack ends within
    ~2s but the CLI consult takes 15-30s, and dead air reads as a hang.

    Two things make it read like a person rather than a countdown clock. It NAMES
    what it's working on (the request's opening phrase — see _topic_phrase), so a
    late line still has a referent. And when a `gate` is given it waits for a
    conversational gap before speaking, up to _PROGRESS_GAP_WAIT past the line's
    moment: if the user is mid-conversation it stays quiet and skips that line rather
    than talking over them — under- is better than over-communicating here, because
    the answer lands as the follow-up either way. That means a conversation with no
    gap at all hears NO lines (every session built by build_agent_session, SIP
    included, passes a gate); only a caller that passes gate=None keeps the
    unconditional schedule. The lines are due at wall-clock instants from the
    consult's start (_PROGRESS_SCHEDULE), so a gap wait never delays the next line.

    The graceful COMPLETION ('...and by the way, that's done, reattached to what you
    asked') is not here: it is the follow-up injector, which the LLM writes grounded
    in the real answer, so it already fits task/research/action without hardcoding.

    Singleton per session: overlapping consults share ONE narrator — two narrators
    doubled every line audibly (observed live)."""
    if getattr(llm, "_teaport_progress_active", False):
        return
    llm._teaport_progress_active = True
    try:
        t0 = time.monotonic()
        for n, due in enumerate(_PROGRESS_SCHEDULE):
            await asyncio.sleep(max(0.0, t0 + due - time.monotonic()))
            # Fit it into a lull the way a person waits for a gap. wait_until_idle
            # returns False if no gap opened within the window — then skip this line
            # rather than force it over whoever is talking.
            if gate is not None and not await gate.wait_until_idle(max_wait=_PROGRESS_GAP_WAIT):
                continue
            # append_to_context=False: a filler is audio-only UX. Committed to the
            # LLM context it becomes the tail assistant message, which is exactly
            # where HeardContextCorrector's positional anchor looks for a cut
            # reply — a barge-in then deleted or rewrote the filler line instead
            # of the reply. The flag also marks the TTS context as a filler for
            # TranscriptLedger (stamped onto its TTSStartedFrame by tts_service).
            await llm.push_frame(TTSSpeakFrame(_progress_line(request, n),
                                               append_to_context=False))
    except asyncio.CancelledError:
        pass
    finally:
        llm._teaport_progress_active = False


async def _consult_and_followup(call_id, fut, request, followup, tool_call_id, llm=None,
                                gate=None):
    """Background waiter for the ASYNC ask_openclaw path. The turn already ended, so
    there's no tight voice deadline: wait out the consult, then hand the answer to
    the follow-up injector, which runs a fresh LLM turn so the bot SPEAKS it. Runs as
    a session-lifecycle task (params.llm.create_task) — a barge-in or the user moving
    on to another topic does NOT cancel the in-flight consult. tool_call_id lets the
    injector rewrite the placeholder tool result once the real outcome is known."""
    from teaport_brain import consult_bridge

    progress = (asyncio.create_task(_consult_progress(llm, request=request, gate=gate))
                if llm is not None else None)

    async def deliver(outcome: oc.ConsultOutcome):
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
        await followup(request, outcome.text, tool_call_id,
                       failure=outcome.failure, detail=outcome.detail)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ASYNC_CONSULT_TIMEOUT
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(fut),
                                            timeout=_NATIVE_CONSULT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            if not getattr(fut, "working", False):
                # Never acked — the relay didn't take it; run the CLI agent instead
                # (still async w.r.t. the turn, which already ended).
                #
                # Logged on both sides because this branch used to return in silence.
                # Every other outcome below says what happened; a consult that took
                # THIS one left no trace at all. Live 2026-09-10: two consults produced
                # zero ask_openclaw(async) lines while the caller heard "I haven't heard
                # back yet", and the real cause — DuckDuckGo serving a bot-detection
                # challenge to the box, so every web_search 500'd and the consult burned
                # its budget retrying — took a dig through the gateway journal to find.
                # One line here would have pointed straight at it.
                logger.info(
                    f"ask_openclaw(async): relay never acked in "
                    f"{_NATIVE_CONSULT_ACK_TIMEOUT:.0f}s; falling back to the CLI agent")
                outcome = await oc.agent_consult(request, timeout=deadline - loop.time())
                (logger.info if outcome.text else logger.warning)(
                    f"ask_openclaw(async): CLI consult {_describe(outcome)}")
                await deliver(outcome)
                return
            result = await asyncio.wait_for(fut, timeout=deadline - loop.time())
        outcome = _consult_outcome(result)
        if outcome.text:
            logger.info(f"ask_openclaw(async): follow-up ready ({len(outcome.text)} chars)")
        else:
            logger.warning(f"ask_openclaw(async): consult {_describe(outcome)}")
        await deliver(outcome)
    except asyncio.TimeoutError:
        logger.warning(f"ask_openclaw(async): consult unfinished after "
                       f"{_ASYNC_CONSULT_TIMEOUT:.0f}s")
        await deliver(oc.ConsultOutcome(
            None, "timeout", f"no result within {_ASYNC_CONSULT_TIMEOUT:.0f}s"))
    except asyncio.CancelledError:
        raise  # session teardown
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ask_openclaw(async) failed: {e!r}")
        try:
            await deliver(oc.ConsultOutcome(None, "error", repr(e)))
        except Exception:  # noqa: BLE001
            pass
    finally:
        if progress is not None:
            progress.cancel()
        consult_bridge.cancel(call_id)


async def _ask_openclaw(params: FunctionCallParams, followup=None, gate=None):
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
                 "instruction": ("This exact request is already in progress. Do not "
                                 "respond now. " + _PENDING_RULE)},
                properties=no_inference())
            return

    call_id = f"{consult_bridge.CALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"
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
            call_id, fut, request, followup, params.tool_call_id, llm=params.llm,
            gate=gate))
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
            "instruction": ("The task is running in the background and nothing has "
                            "come back yet. Do not respond now. " + _PENDING_RULE)},
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
                outcome = await oc.agent_consult(request)
                await params.result_callback(
                    {"answer": outcome.text} if outcome.text
                    else {"error": "the desktop agent did not answer in time"
                          if outcome.failure == "timeout"
                          else "the desktop agent returned no answer"})
                return
            try:
                result = await asyncio.wait_for(fut, timeout=_NATIVE_CONSULT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"ask_openclaw: acked consult unfinished after "
                               f"{_NATIVE_CONSULT_TIMEOUT:.0f}s")
                await params.result_callback(
                    {"error": "the desktop agent is taking too long — try a narrower request"})
                return
        outcome = _consult_outcome(result)
        if outcome.text:
            logger.info(f"ask_openclaw: native consult ok ({len(outcome.text)} chars)")
            await params.result_callback({"answer": outcome.text})
        elif outcome.failure == "empty":
            await params.result_callback({"error": "the desktop agent returned no answer"})
        else:
            logger.warning(f"ask_openclaw: native consult {_describe(outcome)}")
            await params.result_callback({"error": outcome.detail or "the desktop agent failed"})
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


# ---------------------------------------------------------------- client tools
# How long a client gets to answer a client_tool request. They are local and instant
# (a volume change, arming a restart); a client that does not answer in this time is
# treated as not having done it, and the model is told so.
CLIENT_TOOL_TIMEOUT_S = 3.0
# Every client_tool call_id starts with this (consult_bridge routes tool_results by id).
CLIENT_CALL_PREFIX = "teaport-client-"


def _client_tool(name: str):
    """The handler for a tool the client performs: ask it, return what it answers.
    The model gets exactly one result for every call that is not itself cancelled."""
    async def handler(params: FunctionCallParams):
        call_id = CLIENT_CALL_PREFIX + uuid.uuid4().hex[:12]
        fut = consult_bridge.create(call_id)
        try:
            await params.llm.push_frame(OutputTransportMessageUrgentFrame(message={
                "type": "client_tool", "call_id": call_id, "name": name,
                "args": params.arguments or {}}))
            result = await asyncio.wait_for(fut, CLIENT_TOOL_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"{name}: the client did not answer within {CLIENT_TOOL_TIMEOUT_S:g}s")
            result = {"ok": False, "error": "the device did not respond"}
        except Exception as e:  # noqa: BLE001 — the request never reached the client
            logger.warning(f"{name}: could not ask the client ({e!r})")
            result = {"ok": False, "error": "the device could not be reached"}
        finally:
            # Every way out, a cancel (the call itself was cancelled: no result) among
            # them. A no-op once the client answered: resolve already took the entry.
            consult_bridge.cancel(call_id)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        logger.info(f"{name}: {result}")
        await params.result_callback(result)
    return handler


# ---------------------------------------------------------------- the contract
@dataclass(frozen=True)
class ToolContext:
    """What a session can offer its tools. The availability of a tool depends only on
    tts and client_features, so a context built before the PipelineTask exists (for
    the schema and the prompt) and the one register_tools builds later agree."""
    tts: object = None
    has_tts: bool = False
    client_features: frozenset = frozenset()
    followup: Callable | None = None
    gate: object = None
    # The session's WifiSetupVoice (wifi_voice.py), which the wifi_setup tool hands to.
    wifi_voice: object = None


@dataclass(frozen=True)
class Tool:
    schema: FunctionSchema
    # ctx -> the async handler(params) for this session.
    bind: Callable
    # The TEAPORT_TOOL_<NAME> switch, read at import (a literal env_flag per tool, so
    # the config-schema drift test sees every one).
    enabled: bool
    needs: frozenset = frozenset()
    timeout_secs: float | None = None
    # How the system prompt names the tool when it composes the tools paragraph
    # (persona.build_system_prompt). The default sets keep their tuned paragraphs.
    hint: str = ""

    @property
    def name(self) -> str:
        return self.schema.name

    @property
    def client(self) -> bool:
        """Performed by the client (the prompt names these in their own sentence)."""
        return any(n.startswith("client:") for n in self.needs)

    @property
    def direct(self) -> bool:
        """One of the session's own controls — its voice, or the device it speaks
        through — not a source of answers: agent-first mode lets the model call these
        itself rather than through ask_openclaw (agent_session.agent_first_directive)."""
        return self.client or "tts" in self.needs


def _plain(handler):
    return lambda ctx: handler


async def _wifi_setup(params: FunctionCallParams, voice=None):
    if voice is None:
        await params.result_callback({"ok": False, "error": "Wi-Fi setup is not available here"})
        return
    await voice.begin(by_model=True)
    # The setup flow speaks from here on; a model turn would talk over it.
    await params.result_callback(
        {"ok": True, "note": "Wi-Fi setup has taken over and is asking the user to confirm. "
                             "It tells the user itself how it goes; you get a notice when it ends."},
        properties=no_inference())


def _bind_ask_openclaw(ctx: ToolContext):
    # ASYNC consult (answer spoken later as a follow-up) when the session gave us the
    # injector; agent-first passes none and consults synchronously on the turn.
    if ctx.followup is not None:
        return functools.partial(_ask_openclaw, followup=ctx.followup, gate=ctx.gate)
    return _ask_openclaw


# In the order the model is offered them.
TOOLS: tuple[Tool, ...] = (
    Tool(HOST_STATUS, _plain(_get_host_status),
         env_flag("TEAPORT_TOOL_GET_HOST_STATUS", True),
         hint="get_host_status (this machine's live free memory, CPU load, decode speed)"),
    Tool(CURRENT_TIME, _plain(_get_current_time),
         env_flag("TEAPORT_TOOL_GET_CURRENT_TIME", True),
         hint="get_current_time"),
    Tool(WEB_SEARCH, _plain(_web_search),
         env_flag("TEAPORT_TOOL_WEB_SEARCH", True), frozenset({"agent"}),
         hint="web_search (search the web for anything current, factual, or that you don't know)"),
    Tool(WEB_FETCH, _plain(_web_fetch),
         env_flag("TEAPORT_TOOL_WEB_FETCH", True), frozenset({"agent"}),
         hint="web_fetch (read a specific web page)"),
    Tool(SEARCH_MEMORY, _plain(_search_memory),
         env_flag("TEAPORT_TOOL_SEARCH_MEMORY", True), frozenset({"agent"}),
         hint="search_memory (recall what the user told you before, by voice or text)"),
    Tool(REMEMBER, _plain(_remember),
         env_flag("TEAPORT_TOOL_REMEMBER", True), frozenset({"agent"}),
         hint="remember (save a fact the user asks you to remember)"),
    # ask_openclaw runs a full agent turn (~15-35s); pipecat's default 10s
    # function-call timeout abandons it mid-flight and discards the answer that
    # arrives later (the "weather never came back" bug). Its ceiling sits above the
    # handler's own consult caps; the fast tools keep pipecat's 10s.
    Tool(ASK_OPENCLAW, _bind_ask_openclaw,
         env_flag("TEAPORT_TOOL_ASK_OPENCLAW", True), frozenset({"agent"}),
         timeout_secs=_ASK_OPENCLAW_TIMEOUT,
         hint="ask_openclaw (your full desktop agent — every tool, deeper thinking; for "
              "multi-step or open-ended requests your quick tools can't handle)"),
    Tool(LIST_VOICES, lambda ctx: functools.partial(_list_voices, tts=ctx.tts),
         env_flag("TEAPORT_TOOL_LIST_VOICES", True), frozenset({"tts"}),
         hint="list_voices (your speaking voices)"),
    Tool(SWITCH_VOICE, lambda ctx: functools.partial(_switch_voice, tts=ctx.tts),
         env_flag("TEAPORT_TOOL_SWITCH_VOICE", True), frozenset({"tts"}),
         hint="switch_voice (change your speaking voice; if the user starts speaking a "
              "different language, switch to a voice for that language and reply in it)"),
    Tool(WIFI_SETUP, lambda ctx: functools.partial(_wifi_setup, voice=ctx.wifi_voice),
         env_flag("TEAPORT_TOOL_WIFI_SETUP", True),
         frozenset({"client:local", "host:wifi_setup"}),
         hint="wifi_setup (connect this box to a different Wi-Fi network when the user asks)"),
    Tool(SET_VOLUME, _plain(_client_tool("set_volume")),
         env_flag("TEAPORT_TOOL_SET_VOLUME", True), frozenset({"client:volume"}),
         hint="set_volume (make your speaker louder or quieter when the user asks)"),
    # Off by default: a testing aid. On, the model can wipe a conversation on a request
    # it misheard.
    Tool(RESTART_SESSION, _plain(_client_tool("restart_session")),
         env_flag("TEAPORT_TOOL_RESTART_SESSION", False), frozenset({"client:restart"}),
         hint="restart_session (start a fresh conversation, only when the user explicitly "
              "asks to restart or start over; once it returns, say one short goodbye)"),
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
# The same guard for the switch: agent-first with ask_openclaw switched off would route
# every turn to a tool the model is not offered.
if AGENT_FIRST and not TOOLS_BY_NAME["ask_openclaw"].enabled:
    logger.warning("TEAPORT_AGENT_FIRST is on but TEAPORT_TOOL_ASK_OPENCLAW is off — "
                   "ignoring it (agent-first routes every turn through ask_openclaw)")
    AGENT_FIRST = False
# The client features a /talk client may announce (?features=): one per client tool.
CLIENT_FEATURES = frozenset(n.split(":", 1)[1] for t in TOOLS for n in t.needs
                            if n.startswith("client:"))


def parse_client_features(raw: str | None) -> frozenset:
    """?features=volume,restart -> the known ones (an unknown name is ignored)."""
    names = {f.strip().lower() for f in (raw or "").split(",") if f.strip()}
    unknown = names - CLIENT_FEATURES
    if unknown:
        logger.warning(f"client announced unknown features {sorted(unknown)} — ignored")
    return frozenset(names & CLIENT_FEATURES)


def _wifi_setup_installed() -> bool:
    from teaport_brain import wifi_voice
    return wifi_voice.available()


# "host:<part>" needs: is that part installed on this box. Checked per session.
HOST_CHECKS: dict[str, Callable[[], bool]] = {"wifi_setup": _wifi_setup_installed}


def _has(need: str, ctx: ToolContext) -> bool:
    if need.startswith("host:"):
        check = HOST_CHECKS.get(need.split(":", 1)[1])
        return bool(check and check())
    if need == "agent":
        return HAS_AGENT
    if need == "tts":
        return ctx.has_tts or ctx.tts is not None
    if need.startswith("client:"):
        return need.split(":", 1)[1] in ctx.client_features
    raise ValueError(f"unknown tool need {need!r}")


def active_tools(ctx: ToolContext | None = None) -> list[Tool]:
    """THE tools this session has: switched on, with everything they need. Without a
    context: no voice and a client that announced nothing — what register_tools(llm)
    registers when it is given no tts, so the bare defaults agree too. A session passes
    its own (agent_session: its tts and the client's features)."""
    ctx = ctx or ToolContext()
    return [t for t in TOOLS if t.enabled and all(_has(n, ctx) for n in t.needs)]


def build_tools_schema(ctx: ToolContext | None = None) -> ToolsSchema:
    """The schema the model is offered: active_tools(ctx)."""
    return ToolsSchema(standard_tools=[t.schema for t in active_tools(ctx)])


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
                    # append_to_context=False — same as the consult narrator's
                    # lines: audio-only, never an assistant message the heard
                    # corrector could misanchor on, and marked as a filler
                    # context for the ledger.
                    await params.llm.push_frame(
                        TTSSpeakFrame(line, append_to_context=False))

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


def register_tools(llm, lang: str = "en-us", tts=None, followup=None, gate=None,
                   client_features: frozenset = frozenset(), wifi_voice=None) -> None:
    """Wire the handlers of active_tools() onto `llm` — exactly the tools
    build_tools_schema offered for the same tts and client_features. `followup`, if
    given, is an async `(request, text|None) -> None` injector that speaks a background
    consult's answer as an unprompted turn; providing it switches ask_openclaw to the
    ASYNC path. `gate` (a FollowupGate), if given, lets the consult narrator wait for a
    conversational gap before speaking its progress lines."""
    _install_spoke_tracker(llm)
    if tts is not None:
        # Live language: read the TTS service at call time, so a mid-session
        # switch_voice also switches the fallback lines' language.
        def lang_fn():
            return getattr(tts, "espeak_language", lang).split("-")[0]
    else:
        def lang_fn():
            return (lang or "en-us").split("-")[0]
    ctx = ToolContext(tts=tts, client_features=frozenset(client_features),
                      followup=followup if HAS_AGENT else None, gate=gate,
                      wifi_voice=wifi_voice)
    for tool in active_tools(ctx):
        kw = {"timeout_secs": tool.timeout_secs} if tool.timeout_secs else {}
        llm.register_function(tool.name, _wrap(tool.name, tool.bind(ctx), lang_fn), **kw)
