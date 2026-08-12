#
# teaport — OpenClaw gateway-relay voice server
#
# The Pipecat voice brain (engine STT -> LLM -> engine TTS, with the heard-grounding
# TranscriptLedger + HeardContextCorrector) behind a plain WebSocket that the
# OpenClaw `teaport` realtime-voice provider plugin connects to.
#
#   OpenClaw Talk client --talk.session.appendAudio--> gateway --RealtimeVoiceBridge-->
#       teaport plugin --WS /talk--> THIS server (STT/LLM/TTS + barge-in grounding)
#       --WS /talk--> plugin.onAudio --talk.event--> Talk client
#
# OpenClaw drives the plugin as a bridge-only provider over transport
# "gateway-relay": it pumps the user's PCM16/24k mic audio in via bridge.sendAudio()
# and relays our audio/clear/transcript back out. Pipecat owns the whole brain
# (STT+LLM+TTS+tools) and, crucially, the heard-grounded barge-in this project is
# built around; OpenClaw is just the multi-surface front-end.
#
# Audio is PCM16 mono 24 kHz both ways (the relay fixes this format); the STT
# service resamples to the STT's 16 kHz. the engine TTS provides per-word
# playout timestamps, the sharpest heard-grounding.
#
# Usage:  teaport-brain [--host 0.0.0.0] [--port 7861]
#   Requires the engine reachable at TEAPORT_URL (default ws://127.0.0.1:8000).
#
import argparse
import asyncio
import json
import os

# Cache-only HF hub, read at huggingface_hub import time — set before any imports.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from loguru import logger  # noqa: E402

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain.captions import (  # noqa: E402
    CaptionTap,
    UserTranscriptEmitter,
    VoiceActivity,
)
from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_COMPLETE_THRESHOLD,
    VAD_CONFIDENCE,
    VAD_MIN_VOLUME,
    EagerSmartTurnAnalyzer,
)
from teaport_brain.gateway_serializer import (  # noqa: E402
    PIPELINE_SAMPLE_RATE,
    RELAY_SAMPLE_RATE,
    TeaportGatewaySerializer,
)
from teaport_brain import llm_text_guard  # noqa: E402
from teaport_brain import raw_llm_capture  # noqa: E402
from teaport_brain.llm_text_guard import LLMTextGuard  # noqa: E402
from teaport_brain.memory_hygiene import MemoryReclaim, turn_reclaim  # noqa: E402
from teaport_brain.memory_recall import MemoryRecall  # noqa: E402
from teaport_brain.raw_llm_capture import RawLLMCapture  # noqa: E402
from teaport_brain.turn_timing import TurnTimer  # noqa: E402
from teaport_brain import endpoint_debug  # noqa: E402
from teaport_brain import thinking_sound  # noqa: E402
from teaport_brain.thinking_sound import ThinkingSound  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402


def _vad_cls():
    # Opt-in (TEAPORT_ENDPOINT_DEBUG=1): instrumented VAD logs volume/confidence
    # per state transition; otherwise the stock analyzer.
    return (endpoint_debug.InstrumentedSileroVAD if endpoint_debug.ENABLED
            else SileroVADAnalyzer)
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.engine_tts import LANG_NAMES  # noqa: E402
from teaport_brain.persona import build_system_prompt, load_persona  # noqa: E402
# Built from the shared service factories (services.py) — no transport/demo imports,
# so the module stays cheap to import.
from teaport_brain.services import make_llm, make_stt, make_tts  # noqa: E402
from teaport_brain.tools import build_tools_schema, register_tools  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

LISTEN_PORT = int(os.getenv("GATEWAY_PORT", "7861"))
# Shared secret for /talk. When set, a client must present it as ?token=<value> on
# the WebSocket URL (the teaport-realtime plugin sends its TEAPORT_GATEWAY_TOKEN
# env / provider-config token); a missing or wrong token is rejected BEFORE the
# pipeline — and before the single-slot eviction — runs. When unset, anyone who can
# reach this port gets a full agent session (memory read/write tools included) and
# can evict the live call, so we log a loud warning at startup.
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")

# Agent-first experiment (TEAPORT_AGENT_FIRST=1): the sandboxed OpenClaw agent owns
# every conversational turn — ask_openclaw runs SYNCHRONOUSLY (no follow-up
# injector, so tools.py takes its sync path) and a strict system directive makes
# the brain LLM a thin router/phraser instead of the mind. Bridge-is-brain is the
# default; set TEAPORT_AGENT_FIRST=1 to enable (the launcher can source it from a
# ~/.config/teaport/agent_first file so it toggles without editing the unit).
AGENT_FIRST = os.getenv("TEAPORT_AGENT_FIRST", "").strip().lower() in ("1", "true")

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


# The engine STT serves a single session, so only one relay pipeline may hold it at a
# time. Track the live (task, done-event) so a new connection can evict a stale one
# (e.g. a frozen client whose disconnect was never detected, leaving the slot held).
_active_session = None
_session_lock = asyncio.Lock()


def _make_consult_followup(task, context, gate):
    """Follow-up injector for the ASYNC ask_openclaw path. When a background consult
    finishes, append its answer to the context and run the LLM so the bot SPEAKS it
    as an unprompted turn, reattached to what the user asked. A failed/empty consult
    yields a brief 'couldn't get it' turn instead of silence. Bound per-session to
    this task + context; `gate` holds the turn until neither side is mid-speech."""
    async def speak_followup(request, text, tool_call_id=None):
        # Wait for a clear moment: don't step on the user mid-utterance OR the
        # assistant mid-answer about something else. (Gives up after max_wait so a
        # relentlessly chatty conversation can't strand the answer.)
        await gate.wait_until_idle()
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
            content = (
                f"[background task complete] The desktop agent you delegated to has "
                f"finished this earlier request: \"{request}\".\n\nIts answer:\n{text}\n\n"
                "Tell the user now, in one or two short spoken sentences, briefly "
                "reattaching it to what they asked (e.g. \"About that forecast you "
                "wanted — …\"). Speak naturally; don't mention tools, agents, or that "
                "it was delayed.")
        else:
            content = (
                f"[background task: no confirmation] The desktop agent did not report "
                f"back on this earlier request: \"{request}\". It may or may not have "
                "completed. In one short spoken sentence, tell the user you didn't get "
                "confirmation — and if the request was something visible (a message, "
                "poll, or post), ask them to check whether it appeared. Do NOT state "
                "that it definitely failed.")
        context.add_message({"role": "system", "content": content})
        logger.info(f"consult follow-up: delivering ({'answer' if text else 'failure'}; "
                    f"tool result {'rewritten' if rewrote else 'not found'})")
        await task.queue_frames([LLMRunFrame()])
    return speak_followup


async def run_relay_bot(websocket: WebSocket):
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            audio_out_sample_rate=RELAY_SAMPLE_RATE,
            add_wav_header=False,
            serializer=TeaportGatewaySerializer(),
        ),
    )
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
    # OpenClaw selects the TTS voice/language per session: the teaport provider forwards
    # talk.realtime.providers.teaport.{voice,language} as WS URL query params. A
    # voice's prefix implies its language (ef_*→Spanish, …), so `voice` alone is enough;
    # `language` can override the phonemizer. Missing/unknown → defaults (af_heart/en-us).
    qp = websocket.query_params
    tts = make_tts(voice=qp.get("voice"), language=qp.get("language"))

    # Phase 1: identity comes from the shared persona so voice and text feel like one
    # agent; build_system_prompt falls back to the baked-in persona if the shared
    # source is unreachable, so the loop never hard-fails on a persona lookup.
    persona = load_persona()
    logger.info(f"Phase 1: loaded shared persona ({len(persona)} chars)")
    context = LLMContext(
        [{"role": "system", "content": build_system_prompt(persona)}],
        tools=build_tools_schema(),
    )
    if AGENT_FIRST:
        context.add_message({"role": "system", "content": AGENT_FIRST_DIRECTIVE})
        logger.info("AGENT-FIRST mode active: every turn delegates to the OpenClaw agent")
    # If OpenClaw selected a non-English voice/language, tell the LLM to reply in it too
    # (the voice only changes pronunciation; the words still come from the LLM).
    lang_name = _TTS_LANG_NAMES.get(getattr(tts, "espeak_language", "en-us"))
    if lang_name:
        context.add_message(
            {"role": "system", "content": f"Always reply to the user in {lang_name}."}
        )
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
                            params=SmartTurnParams(stop_secs=ENDPOINT_STOP_SECS),
                        )
                    )
                ]
            ),
        ),
    )

    ledger = TranscriptLedger()
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

    pipeline = Pipeline([p for p in [
        transport.input(),
        ep_in,  # tap (debug): VAD-stop / turn-commit bubbles
        stt,
        TurnTimer(turn_marks),  # tap: user-stopped + stt-final
        UserTranscriptEmitter(activity),
        MemoryRecall(context),  # fire memory_search on interim, inject before the LLM
        context_aggregator.user(),
        heard_corrector,
        llm,
        # tap (debug): log the raw completion verbatim when it degenerates into
        # ellipsis/markdown runs. Must sit HERE — upstream of the guard and the TTS
        # aggregator, where the model's own bytes are still visible.
        RawLLMCapture() if raw_llm_capture.ENABLED else None,
        # fold no-break/zero-width unicode out of the deltas and cut a runaway
        # ellipsis collapse — the TTS and the committed context both read what
        # this forwards, so it cleans speech and history in one place.
        LLMTextGuard() if llm_text_guard.ENABLED else None,
        TurnTimer(turn_marks),  # tap: llm-start + llm-first-token
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

    task = PipelineTask(
        pipeline,
        # barge-in is governed by the turn-start strategy's enable_interruptions (default
        # True on MinWordsUserTurnStartStrategy); pipecat 1.0 removed the
        # PipelineParams.allow_interruptions global switch.
        observers=[ledger],
    )

    # Now that the task exists, wire the tools — ask_openclaw goes ASYNC: it hands
    # the consult to a background waiter and this injector speaks the answer as an
    # unprompted follow-up turn when it lands.
    # Agent-first runs consults SYNCHRONOUSLY on the turn (no follow-up injector);
    # the ThinkingSound bed covers the wait and TURN-TIMING measures to the answer.
    register_tools(llm, lang=getattr(tts, "espeak_language", "en-us"), tts=tts,
                   followup=None if AGENT_FIRST
                   else _make_consult_followup(task, context, followup_gate))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        logger.info("OpenClaw relay client connected — greeting")
        # If STT didn't connect (e.g. the single-session engine is busy → 503), warn
        # the user instead of greeting — otherwise the bot 'talks but can't hear'.
        # _connect fires on StartFrame; the loopback engine resolves the tri-state in
        # well under a second, but an unreachable engine HOST (SYN blackhole — box
        # off) takes the websockets ~10s open timeout to fail, so wait out the full
        # resolution window. The healthy path still resolves fast, so this adds no
        # latency when things work; and still-None after the window is treated as
        # can't-hear too, not greeted through.
        for _ in range(120):
            if stt.stt_available is not None:
                break
            await asyncio.sleep(0.1)
        if stt.stt_available is not True:
            logger.warning(
                "STT engine unavailable at connect — warning the user instead of greeting"
            )
            await task.queue_frames([TTSSpeakFrame(
                "Sorry, I can't hear you right now — my speech recognition isn't "
                "available. Please hang up and reconnect in a moment."
            )])
            return
        context.add_message(
            {"role": "system", "content": "Greet the user in one short sentence."}
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info("OpenClaw relay client disconnected — stopping")
        await task.cancel()

    # --- Single-slot eviction -----------------------------------------------
    # A frozen/abandoned client leaves its pipeline running (on_client_disconnected
    # never fires), holding the single STT slot until a ~5-min idle timeout. So
    # before starting ours, evict the previous pipeline: cancel it and wait for its
    # teardown (STT _disconnect closes the engine socket) so our STT can claim the
    # slot. Pairs with the STT-unavailable warning as a backstop if a race slips.
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

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        my_done.set()
        async with _session_lock:
            if _active_session is not None and _active_session[0] is task:
                _active_session = None


app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True, "tts": "engine"}


@app.websocket("/talk")
async def talk(websocket: WebSocket):
    # Auth BEFORE anything else: run_relay_bot evicts the live pipeline for every new
    # connection, so an unauthenticated socket must never get that far — otherwise any
    # host that can reach this port can kill the owner's call and use the agent (and
    # its memory read/write tools).
    if GATEWAY_TOKEN and websocket.query_params.get("token") != GATEWAY_TOKEN:
        logger.warning("rejected /talk client: missing or bad token")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        await run_relay_bot(websocket)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"relay session error: {e}")
    finally:
        # Skip the reclaim when a replacement session evicted us: it is already
        # mid-greeting on this same event loop, and gc+malloc_trim+empty_cache here
        # would stall its audio — and empty_cache can contend the CUDA allocator
        # lock against its in-flight synth (the hazard MemoryReclaim's docstring
        # documents). The replacement's own session-end reclaim covers the memory.
        if _active_session is None:
            turn_reclaim()
        else:
            logger.info("session-end reclaim skipped — a replacement session is active")


def main():
    parser = argparse.ArgumentParser(
        description="teaport OpenClaw gateway-relay voice server"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args()
    if not GATEWAY_TOKEN:
        logger.warning(
            "GATEWAY_TOKEN is not set — /talk is UNAUTHENTICATED: anyone who can "
            "reach this port can use the agent (and its memory tools) and evict "
            "the live call. Set GATEWAY_TOKEN (server) + TEAPORT_GATEWAY_TOKEN "
            "(plugin) except on a trusted network."
        )
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup (G2P/synthesis are engine-side)
    logger.info(f"teaport OpenClaw relay server on ws://{args.host}:{args.port}/talk")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
