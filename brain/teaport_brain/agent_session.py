# SPDX-License-Identifier: MIT
#
# teaport — shared agent-session factory.
#
# The teaport brain has two front-ends — gateway_server.py (OpenClaw WebSocket
# relay) and sip_server.py (SIP over an AF_UNIX socket) — that used to build the
# SAME Pipecat pipeline separately, and drifted. This module is the single source
# of truth for the agent: build_agent_session() constructs the whole brain
# (VAD + engine STT -> LLM -> engine TTS, the heard-grounding TranscriptLedger +
# HeardContextCorrector, memory recall/reclaim, captions, turn-timing taps, the
# degenerate-text guards, tools + the async ask_openclaw follow-up) and returns an
# AgentSession the front-end drives. A front-end supplies only its transport and,
# optionally, its own input-side processors (SIP's HalfDuplexInputGate).
#
# Extracted from gateway_server.py (the more-complete of the two) so the OpenClaw
# path is byte-for-byte the same processor sequence it was before; the SIP path
# GAINS the full stack by adopting the factory.
#
import asyncio
import json

from loguru import logger

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from teaport_brain.captions import (
    CaptionTap,
    UserTranscriptEmitter,
    VoiceActivity,
)
from teaport_brain.endpointing import (
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_COMPLETE_THRESHOLD,
    SMARTTURN_STOP_SECS,
    VAD_CONFIDENCE,
    VAD_MIN_VOLUME,
    EagerSmartTurnAnalyzer,
)
from teaport_brain import endpoint_debug
from teaport_brain import llm_error_speaker
from teaport_brain import llm_text_guard
from teaport_brain import raw_llm_capture
from teaport_brain import thinking_sound
from teaport_brain.engine_tts import LANG_NAMES
from teaport_brain.followup_gate import FollowupGate, FollowupTrigger
from teaport_brain.heard_context import HeardContextCorrector
from teaport_brain.llm_error_speaker import LLMErrorSpeaker
from teaport_brain.llm_text_guard import LLMTextGuard
from teaport_brain.memory_hygiene import MemoryReclaim
from teaport_brain.memory_recall import MemoryRecall
from teaport_brain.persona import build_system_prompt, load_persona
from teaport_brain.raw_llm_capture import RawLLMCapture
# Built from the shared service factories (services.py) — no transport/demo imports,
# so the module stays cheap to import.
from teaport_brain.services import make_llm, make_stt, make_tts
from teaport_brain.thinking_sound import ThinkingSound
from teaport_brain.tools import (
    AGENT_FIRST,
    build_tools_schema,
    register_tools,
)
from teaport_brain.transcript_ledger import TranscriptLedger
from teaport_brain.tts_text import strip_urls_for_speech
from teaport_brain.turn_timing import TurnTimer


def _vad_cls():
    # Opt-in (TEAPORT_ENDPOINT_DEBUG=1): instrumented VAD logs volume/confidence
    # per state transition; otherwise the stock analyzer.
    return (endpoint_debug.InstrumentedSileroVAD if endpoint_debug.ENABLED
            else SileroVADAnalyzer)


# Agent-first experiment (TEAPORT_AGENT_FIRST=1): the sandboxed OpenClaw agent owns
# every conversational turn — ask_openclaw runs SYNCHRONOUSLY (no follow-up
# injector, so tools.py takes its sync path) and a strict system directive makes
# the brain LLM a thin router/phraser instead of the mind. Bridge-is-brain is the
# default; set TEAPORT_AGENT_FIRST=1 to enable. (AGENT_FIRST is defined once, in
# tools.py — imported here so both front-ends share the same switch.)
AGENT_FIRST_DIRECTIVE = (
    "AGENT-FIRST MODE — this overrides earlier tool guidance. For EVERY user "
    "message, immediately call ask_openclaw with the user's request as one "
    "self-contained sentence (add brief context from the conversation when "
    "needed). Call it SILENTLY: no preamble sentence, no other tools, and never "
    "answer from your own knowledge. When the result arrives, speak its answer "
    "naturally in one or two short spoken sentences. Only list_voices and "
    "switch_voice may be called directly."
)

# Non-English TTS languages → the name to steer the LLM to reply in, so a selected
# Spanish/Italian/… voice speaks coherent text rather than mispronounced English.
# Derived from the single voice-inventory source (engine_tts.LANG_NAMES); English
# needs no directive — the shared persona is already English.
_TTS_LANG_NAMES = {k: v for k, v in LANG_NAMES.items() if not k.startswith("en-")}


# --- Single-slot arbiter (WITHIN ONE PROCESS) -----------------------------------
# The engine STT serves a single session, so only one pipeline may hold it at a time.
# Track the live (task, done-event) so a new connection can evict a stale one (e.g. a
# frozen client whose disconnect was never detected, leaving the slot held).
#
# SCOPE, because the name oversells it: these are module globals, so this arbitrates
# only among pipelines in the SAME PROCESS — i.e. among OpenClaw /talk connections.
# teaport-brain.service and teaport-sip-brain.service are two separate units running
# two separate processes, so acquire_slot() cannot evict a SIP call and slot_active()
# cannot see one (nor the reverse). The SIP path contends for the same engine slot on
# every call — it has since 2612baf made it per-call — it just cannot register here.
#
# The real cross-front-end contract is therefore NOT handoff but: FIRST CONNECT WINS,
# and the loser hears the busy line. That is enforced by the engine returning 503, not
# by any code here. stt.py retries a 503 briefly so a slot still settling from the
# previous session's close is not mistaken for one genuinely in use; see
# brain/formal/SttSlot.tla, which models both front-ends against the one engine slot
# and the close-latency window between them.
_active_session = None
_session_lock = asyncio.Lock()


def slot_active() -> bool:
    """True while some pipeline holds the single STT slot. gateway_server reads this
    in its outer finally to decide whether a session-end memory reclaim is safe (a
    replacement session that already evicted us must run its own reclaim)."""
    return _active_session is not None


async def acquire_slot(task):
    """Claim the single STT slot for `task`, evicting whatever held it before.

    Cancels the previous pipeline and waits for its teardown (STT _disconnect closes
    the engine socket) so this task's STT can claim the slot; pairs with the
    STT-unavailable greeting warning as a backstop if a race slips. Returns
    (done_event, release): await release() in a finally to relinquish the slot."""
    global _active_session
    my_done = asyncio.Event()
    async with _session_lock:
        prev = _active_session
        if prev is not None:
            prev_task, prev_done = prev
            logger.info("new client — evicting the previous pipeline to free the STT slot")
            try:
                await prev_task.cancel()
                await asyncio.wait_for(prev_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("evict: previous pipeline did not finish within 5s")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"evict: error cancelling previous pipeline: {e!r}")
            await asyncio.sleep(0.3)  # let the engine process the close + free the slot
        _active_session = (task, my_done)

    async def release():
        global _active_session
        my_done.set()
        async with _session_lock:
            if _active_session is not None and _active_session[0] is task:
                _active_session = None

    return my_done, release


# How many quiet windows to try before giving up on delivering a consult answer. A
# barge-in between queueing the follow-up turn and the model reading it flushes the
# queued LLMRunFrame, so the attempt has to be repeatable.
# How long greet() waits for the STT tri-state to resolve before treating it as
# can't-hear. The loopback engine answers in well under a second and a 503 now
# resolves within the connect retry budget, but an unreachable HOST (SYN blackhole —
# box off) takes the websockets ~10s open timeout to fail, so the window has to
# outlast that. Named so tests can shorten it instead of sitting through it.
_STT_RESOLVE_POLLS = 120
_STT_RESOLVE_INTERVAL_S = 0.1

_DELIVERY_ATTEMPTS = 3
# How long one attempt waits for the model to start answering from the trigger before
# treating the turn as flushed. Generous: this covers the queue plus a cold completion.
_DELIVERY_START_TIMEOUT = 20.0


def _make_consult_followup(task, context, gate, retirer):
    """Follow-up injector for the ASYNC ask_openclaw path. When a background consult
    finishes, append its answer to the context and run the LLM so the bot SPEAKS it
    as an unprompted turn, reattached to what the user asked. A failed/empty consult
    yields a brief 'couldn't get it' turn instead of silence. Bound per-session to
    this task + context; `gate` holds the turn until neither side is mid-speech and
    `retirer` (FollowupTrigger) takes the one-shot trigger back out of the context
    once the model has actually answered from it."""
    async def speak_followup(request, text, tool_call_id=None):
        # Wait for a clear moment: don't step on the user mid-utterance OR the
        # assistant mid-answer about something else. (Gives up after max_wait so a
        # relentlessly chatty conversation can't strand the answer.)
        await gate.wait_until_idle(turn_free=True)
        # Rewrite the placeholder tool result to the real outcome. The placeholder's
        # own instruction ("add nothing more... do not invent an answer now") stays
        # authoritative if left in the context — observed live: the model obeyed IT
        # over the delivery request below and answered with another waiting phrase,
        # so the answer never reached the user. With the tool result rewritten, the
        # context reads like any normally-completed tool call.
        rewrote = False
        for m in context.get_messages():
            if (isinstance(m, dict) and m.get("role") == "tool"
                    and m.get("tool_call_id") == tool_call_id):
                m["content"] = json.dumps(
                    {"status": "complete", "answer": text} if text
                    # "unknown", not "failed": the consult can die on teardown
                    # AFTER the action landed (observed live — message posted,
                    # then rc=1), so asserting failure can be a lie.
                    else {"status": "unknown",
                          "error": "the desktop agent did not report back; the "
                                   "action may or may not have completed"})
                rewrote = True
                break
        if text:
            # The addresses stay in the rewritten tool result above (the record the
            # user can ask about); they leave the delivery text. Spoken, each one is
            # "nvd dot nist dot gov slash vuln slash …" — live 2026-09-04 21:10 a
            # five-item delivery read five of them in ONE sentence, ~30 s of audio
            # whose first chunk alone took 6.6 s to synthesize: 8 s of silence the
            # user took for "done" and talked over, so the delivery was retired at
            # 10% heard. The persona now names sources instead of reading them.
            content = (
                f"[background task complete] The desktop agent you delegated to has "
                f"finished this earlier request: \"{request}\".\n\nIts answer:\n"
                f"{strip_urls_for_speech(text)}\n\n"
                "Tell the user now, in one or two short spoken sentences, briefly "
                "reattaching it to what they asked (e.g. \"About that forecast you "
                "wanted — …\"). Speak naturally; don't mention tools, agents, or that "
                "it was delayed, and don't read out web addresses — name the source.")
        else:
            content = (
                f"[background task: no confirmation] The desktop agent did not report "
                f"back on this earlier request: \"{request}\". It may or may not have "
                "completed. In one short spoken sentence, tell the user you didn't get "
                "confirmation — and if the request was something visible (a message, "
                "poll, or post), ask them to check whether it appeared. Do NOT state "
                "that it definitely failed.")
        # As a USER message, not a system one. A trailing system message is not a turn
        # the model answers: it re-answers the last real user question instead and the
        # delivery never happens. Measured against the live model on the exact context
        # from the 2026-08-26 07:59 failure — system delivered the answer 0/4 times
        # with an unrelated exchange in between (it repeated its own espresso answer,
        # which is what the user saw) and only 1/4 even when nothing intervened, while
        # leaking "background task"/"agent" wording 2/4. As a user message it delivered
        # 8/8 across both shapes and leaked nothing.
        #
        # The tag matters because this message OUTLIVES the turn: without it the rest
        # of the session reads a request the user never made, and the model starts
        # attributing it to them.
        trigger = {
            "role": "user",
            "content": f"[automated system notice, not spoken by the user]\n{content}"}
        context.add_message(trigger)
        logger.info(f"consult follow-up: delivering ({'answer' if text else 'failure'}; "
                    f"tool result {'rewritten' if rewrote else 'not found'})")

        # Retire the trigger once the model has ANSWERED FROM IT. It is a one-shot:
        # "Tell the user now..." left in the context is a standing order, and the model
        # re-executes it the next time a turn gives it nothing else to do (observed live
        # 2026-08-26 08:17 — the shop list was delivered, the user said "That's useful."
        # and the bot recited the whole list again).
        #
        # Neutralised in place rather than removed: the messages list may be a copy, but
        # the dicts in it are live (that is how the tool result above is rewritten).
        # Nothing is lost, because the answer itself stays in the rewritten tool result.
        #
        # This used to key on gate.wait_until_delivered(), and that is unsound: _busy is
        # set by ANY activity, so the user speaking right after the consult landed
        # satisfied it and the trigger was retired having never been read — the answer
        # then never reached the user at all. TLC finds it in 8 steps
        # (brain/formal/Followup.tla, MODE = "asWritten"). Keying on our OWN completion
        # instead is not enough either: a barge-in between the read and the retirement
        # leaves the trigger live for the next turn to recite a second time (MODE =
        # "gateOnOwn" violates NoRepeatRecital). Only retiring AT the read satisfies
        # both, which is what FollowupTrigger does.
        def _retire():
            trigger["content"] = ("[automated system notice, not spoken by the user]\n"
                                  "An earlier background task finished and its outcome "
                                  "was already given to the user. Nothing further is "
                                  "needed.")

        for attempt in range(_DELIVERY_ATTEMPTS):
            # Arm BEFORE queueing: the completion can start while queue_frames is still
            # awaiting, and an unarmed trigger read is exactly the double-recital case.
            shot = retirer.arm(_retire)
            await task.queue_frames([LLMRunFrame()])
            try:
                await asyncio.wait_for(shot.fired.wait(),
                                       timeout=_DELIVERY_START_TIMEOUT)
                return
            except asyncio.TimeoutError:
                # Nothing answered from the trigger — a barge-in flushed the queued
                # LLMRunFrame. The trigger is still live and still accurate, so wait for
                # the next clear moment and queue the turn again.
                retirer.disarm(shot)
                if attempt + 1 < _DELIVERY_ATTEMPTS:
                    logger.info("consult follow-up: turn was flushed before the model "
                                f"read it — retrying ({attempt + 2}/{_DELIVERY_ATTEMPTS})")
                    await gate.wait_until_idle(turn_free=True)
        # Out of attempts. Retire it anyway: a live "tell the user now" is a standing
        # order the next unrelated turn would execute, which is worse than a lost answer
        # (the answer itself survives in the rewritten tool result, so "what did the
        # agent say?" still works).
        logger.warning(f"consult follow-up: not delivered after {_DELIVERY_ATTEMPTS} "
                       "attempts — retiring the trigger unread")
        _retire()
    return speak_followup


def _build_initial_messages(system_prompt: str, lang_name: str | None) -> list:
    """The context's starting messages: the shared persona system prompt, the
    AGENT-FIRST directive when that mode is on, and a reply-language directive when a
    non-English TTS voice/language was selected. Used both at build time and for the
    SIP per-call reset so a reset restores the exact starting context."""
    msgs = [{"role": "system", "content": system_prompt}]
    if AGENT_FIRST:
        msgs.append({"role": "system", "content": AGENT_FIRST_DIRECTIVE})
    if lang_name:
        msgs.append({"role": "system", "content": f"Always reply to the user in {lang_name}."})
    return msgs


class AgentSession:
    """Everything a front-end drives after build_agent_session(): the PipelineTask to
    run, the LLMContext, the STT service (for the greeting's tri-state check), the
    TranscriptLedger observer, and the FollowupGate. greet() is the shared lifecycle
    helper both front-ends call."""

    # The robust greeting: a USER-role stage direction (NOT a system-only note). At
    # connect the context is otherwise system-only, and strict OpenAI-compatible
    # providers (observed: OpenRouter->qwen3) reject a system-only completion with
    # "No user query found in messages". The parenthetical reads as a call-connected
    # cue, so the persona still generates the wording.
    _GREETING = "(The call/session just connected. Greet me warmly in one short sentence.)"
    # Spoken when the engine's single STT slot is already held by another session (the
    # local OpenClaw brain and the SIP brain share ONE engine slot; whoever connects
    # first holds it, the second hears this). Unified wording for both front-ends: the
    # SIP side hangs the caller up after it plays, the OpenClaw side lets its own client
    # disconnect — so a neutral "busy, try again" reads right on a phone and in the app.
    _BUSY_MESSAGE = (
        "Sorry, the voice assistant is busy with another session right now — "
        "please try again in a moment."
    )
    # Spoken when STT is unavailable for any OTHER reason — engine service dead, box
    # off, wrong host. These are NOT the same failure and must not read the same:
    # "busy with another session" sends the user to the FAQ's "stop the local brain so
    # the line wins the slot" remedy for what is actually a down engine. The STT
    # service distinguishes them (stt.slot_busy) because a REJECTION is exactly what it
    # retries and an unreachable host is what it does not.
    _UNAVAILABLE_MESSAGE = (
        "Sorry, I can't hear you right now — my speech recognition isn't available. "
        "Please hang up and reconnect in a moment."
    )

    def __init__(self, *, task, context, stt, ledger, followup_gate):
        self.task = task
        self.context = context
        self.stt = stt
        self.ledger = ledger
        self.followup_gate = followup_gate
        # Set by greet() when the STT slot is busy: the front-end reads it to end the
        # session cleanly after the busy line plays (SIP hangs up; OpenClaw lets its
        # client disconnect). False on a normal greeting.
        self.should_end = False

    async def greet(self):
        """Greet the user with an LLM turn — but only through a working STT.

        If STT didn't connect (e.g. the single-session engine's STT slot is already
        held → 503), speak a busy message and flag the session to END instead of
        greeting, otherwise the bot 'talks but can't hear'. STT connect fires on
        StartFrame; the loopback engine resolves the tri-state in well under a second,
        but an unreachable engine HOST (SYN blackhole — box off) takes the websockets
        ~10s open timeout to fail, so wait out the full resolution window. The healthy
        path still resolves fast, so this adds no latency when things work; still-None
        after the window is treated as can't-hear too, not greeted through.

        On the busy path this sets self.should_end and queues the busy line but does NOT
        block: the front-end decides how to end (SIP waits for the line to play, then
        hangs up; OpenClaw leaves it to its existing client-disconnect path). The normal
        (STT-available) greeting is unchanged."""
        for _ in range(_STT_RESOLVE_POLLS):
            if self.stt.stt_available is not None:
                break
            await asyncio.sleep(_STT_RESOLVE_INTERVAL_S)
        if self.stt.stt_available is not True:
            # Say WHICH failure it was. slot_busy is True only when the engine actively
            # rejected us (503 — its one session is taken, after the connect retry gave
            # the previous session's close time to land); False for an unreachable host
            # and None when the tri-state never resolved at all, both of which mean the
            # engine, not another session, is the problem.
            busy = self.stt.slot_busy is True
            logger.warning(
                ("STT slot held by another session at connect — " if busy else
                 "STT engine unavailable at connect (not a busy slot) — ")
                + "speaking the warning and ending this session instead of greeting"
            )
            self.should_end = True
            # append_to_context=False: the pipeline talking, not the model -- never
            # an assistant message, and a marked filler for TranscriptLedger.
            await self.task.queue_frames([TTSSpeakFrame(
                self._BUSY_MESSAGE if busy else self._UNAVAILABLE_MESSAGE,
                append_to_context=False)])
            return
        self.context.add_message({"role": "user", "content": self._GREETING})
        await self.task.queue_frames([LLMRunFrame()])

    # There is no reset_context() here any more. It existed for the pre-2612baf
    # persistent SIP pipeline, which reset the context between calls; the per-call
    # design builds a fresh pipeline (and a fresh context) each time, so nothing
    # called it. It also reached into HeardContextCorrector._done, a private write
    # that would have broken silently on a rename. If a persistent pipeline ever
    # comes back, give HeardContextCorrector a public reset() rather than restoring
    # this.


def build_agent_session(transport, *, voice: str | None = None,
                        language: str | None = None,
                        input_processors: list | None = None,
                        cancel_on_idle_timeout: bool | None = None) -> AgentSession:
    """Build the shared teaport brain around `transport` and return an AgentSession.

    This is the single source of truth for the pipeline both front-ends run — the
    OpenClaw path gets exactly the sequence gateway_server built inline, and the SIP
    path gets the same full stack.

    - voice / language: TTS selection (OpenClaw forwards them as WS query params). A
      voice's prefix implies its language; language can override the phonemizer.
      Missing/unknown → engine defaults (af_heart/en-us).
    - input_processors: optional processors inserted right after transport.input()
      (before the debug ep_in tap) — the ONLY place a front-end injects its own
      processors. SIP passes [HalfDuplexInputGate()]; OpenClaw passes nothing.
    - cancel_on_idle_timeout: forwarded to PipelineTask only when set. Left unset for
      the OpenClaw per-connection pipeline (PipelineTask's default); the SIP path runs
      ONE persistent pipeline across calls and passes False so pipecat's idle-timeout
      can't cancel the brain between callers.
    """
    # pipecat 1.0 moved VAD off the transport onto the user aggregator
    # (LLMUserAggregatorParams.vad_analyzer); it drives endpointing + barge-in.
    vad_analyzer = _vad_cls()(
        params=VADParams(
            confidence=VAD_CONFIDENCE, min_volume=VAD_MIN_VOLUME,
            stop_secs=ENDPOINT_STOP_SECS,
        )
    )

    stt = make_stt()
    llm = make_llm()
    tts = make_tts(voice=voice, language=language)

    # Phase 1: identity comes from the shared persona so voice and text feel like one
    # agent; build_system_prompt falls back to the baked-in persona if the shared
    # source is unreachable, so the loop never hard-fails on a persona lookup.
    persona = load_persona()
    logger.info(f"Phase 1: loaded shared persona ({len(persona)} chars)")
    system_prompt = build_system_prompt(persona)
    # If a non-English voice/language was selected, tell the LLM to reply in it too
    # (the voice only changes pronunciation; the words still come from the LLM).
    lang_name = _TTS_LANG_NAMES.get(getattr(tts, "espeak_language", "en-us"))
    context = LLMContext(
        _build_initial_messages(system_prompt, lang_name),
        tools=build_tools_schema(),
    )
    if AGENT_FIRST:
        logger.info("AGENT-FIRST mode active: every turn delegates to the OpenClaw agent")
    if lang_name:
        logger.info(f"TTS language selected → instructing the LLM to reply in {lang_name}")
    # register_tools is deferred until after the PipelineTask exists: the async
    # ask_openclaw path needs a follow-up injector bound to THIS task + context.

    # Barge-in is the whole point of this surface (the OpenClaw Talk client does its
    # own echo cancellation), so we do NOT mute the user while the bot speaks —
    # smart-turn endpointing handles turn-taking instead.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                # Gate interruptions on a min word count so short STT garble can't
                # cut the bot mid-reply (self-relaxes to 1 word when the bot is
                # silent, so normal turns are unaffected). Replaces the default
                # [VAD, Transcription] start — VAD-start would interrupt on any
                # sound, defeating the guard.
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=EagerSmartTurnAnalyzer(
                            complete_threshold=SMARTTURN_COMPLETE_THRESHOLD,
                            # The CEILING on an INCOMPLETE verdict, not the VAD's
                            # floor -- see endpointing.py.
                            params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS),
                        )
                    )
                ]
            ),
        ),
    )

    # The ledger reads the LLM stream at the TTS's sighting (the text the TTS is
    # actually handed, after the guard) and tells the thinking bed's audio, pushed
    # into the transport, from the transport's untagged copies of what it played.
    ledger = TranscriptLedger(tts=tts, output=transport.output())
    heard_corrector = HeardContextCorrector(ledger, context)
    activity = VoiceActivity()  # shared: user-interim stamps gate assistant partials (captions.py)
    turn_marks: dict = {}  # shared by the three TurnTimer taps (per-session, see TurnTimer)
    # Opt-in live endpointing probe (TEAPORT_ENDPOINT_DEBUG=1): two taps sharing
    # one dict emit real-time bubbles for the VAD-stop → turn-commit → first-audio
    # cascade; no-ops when disabled.
    ep_marks: dict = {}
    ep_in = endpoint_debug.EndpointDebug(ep_marks, "in") if endpoint_debug.ENABLED else None
    ep_out = endpoint_debug.EndpointDebug(ep_marks, "out") if endpoint_debug.ENABLED else None
    # Gates the async ask_openclaw follow-up so it only speaks in a clear moment.
    followup_gate = FollowupGate()
    # Retires the follow-up's one-shot trigger at the completion that reads it. Must
    # sit directly below the LLM — see FollowupTrigger's placement note.
    followup_trigger = FollowupTrigger()

    pipeline = Pipeline([p for p in [
        transport.input(),
        # Front-end input-side processors (SIP's HalfDuplexInputGate); none for OpenClaw.
        *(input_processors or []),
        ep_in,  # tap (debug): VAD-stop / turn-commit bubbles
        stt,
        # This tap owns the silent-turn watchdog (see TurnTimer): it is the first to
        # see the turn begin, and only one of the three may arm it.
        TurnTimer(turn_marks, watchdog=True),  # tap: user-stopped + stt-final
        UserTranscriptEmitter(activity),
        MemoryRecall(context),  # fire memory_search on interim, inject before the LLM
        context_aggregator.user(),
        heard_corrector,
        # A failed completion must be HEARD, not just logged (see the module). ABOVE the
        # LLM on purpose: ErrorFrames travel UPSTREAM, so below the LLM this never saw a
        # single LLM error and only ever caught the TTS's, which it then blamed on the
        # model.
        LLMErrorSpeaker() if llm_error_speaker.ENABLED else None,
        llm,
        # Retires the consult follow-up's one-shot trigger the moment the model starts
        # answering from it. HERE and nowhere else: LLMTextFrame is consumed by the TTS
        # (push_text_frames=False), so no position below it ever sees one — and
        # LLMFullResponseStartFrame is pushed BEFORE the context is serialized, so it
        # cannot be the signal. See FollowupTrigger.
        followup_trigger,
        # tap: llm-start + llm-first-token. Must sit ABOVE the guard: downstream of it
        # this would time the first token that SURVIVES the guard, and a completion the
        # guard trips on immediately would set no llm_first_token mark at all — which
        # turn_timing drops silently from the TURN-TIMING line, making a swallowed turn
        # look like a missing log field.
        TurnTimer(turn_marks),
        # tap (debug): log the raw completion verbatim when it degenerates into
        # ellipsis/markdown runs. Must sit HERE — upstream of the guard and the TTS
        # aggregator, where the model's own bytes are still visible.
        RawLLMCapture() if raw_llm_capture.ENABLED else None,
        # fold no-break/zero-width unicode out of the deltas and cut a runaway
        # ellipsis collapse — the TTS and the committed context both read what
        # this forwards, so it cleans speech and history in one place.
        LLMTextGuard() if llm_text_guard.ENABLED else None,
        tts,
        ep_out,  # tap (debug): first-audio bubble
        TurnTimer(turn_marks),  # tap: tts-first-audio (logs the turn line)
        # Fill the dead air of a long ask_openclaw consult with a soft typing bed;
        # stops the instant the reply's first audio arrives. No-op for fast tools.
        ThinkingSound() if thinking_sound.ENABLED else None,
        transport.output(),
        followup_gate,  # track user/bot/LLM activity → hold async follow-ups for a clear moment
        CaptionTap(activity),  # AFTER the transport: playout-paced partials + per-utterance finals
        MemoryReclaim(),  # per-turn: hand glibc arena pages back to the OS (CUDA at session end)
        context_aggregator.assistant(),
    ] if p is not None])

    task_kwargs = {"observers": [ledger]}
    if cancel_on_idle_timeout is not None:
        task_kwargs["cancel_on_idle_timeout"] = cancel_on_idle_timeout
    task = PipelineTask(
        pipeline,
        # barge-in is governed by the turn-start strategy's enable_interruptions (default
        # True on MinWordsUserTurnStartStrategy); pipecat 1.0 removed the
        # PipelineParams.allow_interruptions global switch.
        **task_kwargs,
    )

    # Now that the task exists, wire the tools — ask_openclaw goes ASYNC: it hands
    # the consult to a background waiter and this injector speaks the answer as an
    # unprompted follow-up turn when it lands.
    # Agent-first runs consults SYNCHRONOUSLY on the turn (no follow-up injector);
    # the ThinkingSound bed covers the wait and TURN-TIMING measures to the answer.
    register_tools(llm, lang=getattr(tts, "espeak_language", "en-us"), tts=tts,
                   followup=None if AGENT_FIRST
                   else _make_consult_followup(task, context, followup_gate,
                                               followup_trigger),
                   # Lets the consult narrator fit its progress lines into a gap
                   # rather than talking over the user (see _consult_progress).
                   gate=followup_gate)

    return AgentSession(
        task=task,
        context=context,
        stt=stt,
        ledger=ledger,
        followup_gate=followup_gate,
    )
