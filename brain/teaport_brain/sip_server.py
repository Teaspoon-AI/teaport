# SPDX-License-Identifier: MIT
#
# teaport — SIP brain client (M2): the real agent behind the teaport-sip gateway.
#
# The SIP-over-UNIX-socket sibling of gateway_server.py (the OpenClaw WebSocket
# relay). Same brain, same factories (services.make_stt/make_llm/make_tts), same
# persona + VAD + smart-turn endpointing + heard-grounding ledger — but the
# transport is SipGatewayTransport over the teaport-sip AF_UNIX SOCK_SEQPACKET
# socket (sip_transport.py) instead of a FastAPI WebSocket. A phone call at the
# gateway now reaches Voxtral STT -> LLM -> Kokoro TTS instead of a stub echo.
#
# Lifecycle (M2): the UDS connection = the pipeline's lifetime (one persistent
# pipeline, single active call in protocol v0). We GREET on call.state=confirmed
# and RESET the LLM context on call.state=disconnected so the next caller starts
# fresh WITHOUT tearing the pipeline down. If the gateway socket closes, we stop.
#
# Usage:  python -m teaport_brain.sip_server [--socket /tmp/teaport-sip.sock]
#   Requires the engine at TEAPORT_URL (STT/TTS) and an LLM at LLM_BASE_URL.
#
# NOTE: parallel to gateway_server.py, not a replacement — the OpenClaw path is
# untouched. The LLM TOOLS are now wired in exactly like the OpenClaw path
# (build_tools_schema -> LLMContext; register_tools after the PipelineTask; the
# async ask_openclaw follow-up rides a FollowupGate + _make_consult_followup, with
# a ThinkingSound bed over the consult wait) so a caller gets REAL tool results
# instead of hallucinated ones. On SIP there is no OpenClaw plugin to service the
# native openclaw_agent_consult round-trip (the sip_serializer drops it, being a
# non-protocol control), so ask_openclaw degrades to the CLI agent_consult path;
# the fast tools (host status, time, web search/fetch, memory) run unchanged.
# Still deferred vs. the OpenClaw pipeline (documented in RUNLOG): memory recall +
# reclaim, captions/transcript emitters (no SIP control type carries them),
# turn-timing taps.

import argparse
import asyncio
import json
import os

# Cache-only HF hub, read at huggingface_hub import time — set before any imports.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from loguru import logger  # noqa: E402

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    LLMRunFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from teaport_brain import endpoint_debug  # noqa: E402
from teaport_brain import thinking_sound  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_COMPLETE_THRESHOLD,
    VAD_CONFIDENCE,
    VAD_MIN_VOLUME,
    EagerSmartTurnAnalyzer,
)
from teaport_brain.engine_tts import LANG_NAMES  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402
from teaport_brain import llm_error_speaker  # noqa: E402
from teaport_brain import llm_text_guard  # noqa: E402
from teaport_brain import raw_llm_capture  # noqa: E402
from teaport_brain.llm_error_speaker import LLMErrorSpeaker  # noqa: E402
from teaport_brain.llm_text_guard import LLMTextGuard  # noqa: E402
from teaport_brain.raw_llm_capture import RawLLMCapture  # noqa: E402
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.persona import build_system_prompt, load_persona  # noqa: E402
from teaport_brain.services import make_llm, make_stt, make_tts  # noqa: E402
from teaport_brain.sip_serializer import SipProtocolSerializer  # noqa: E402
from teaport_brain.sip_transport import (  # noqa: E402
    DEFAULT_UDS_PATH,
    SipGatewayTransport,
    connect_seqpacket,
    make_sip_params,
)
from teaport_brain.thinking_sound import ThinkingSound  # noqa: E402
from teaport_brain.tools import build_tools_schema, register_tools  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger  # noqa: E402

# Non-English TTS languages → the name to steer the LLM to reply in (mirrors
# gateway_server); English needs no directive (the persona is already English).
_TTS_LANG_NAMES = {k: v for k, v in LANG_NAMES.items() if not k.startswith("en-")}


def _vad_cls():
    return (endpoint_debug.InstrumentedSileroVAD if endpoint_debug.ENABLED
            else SileroVADAnalyzer)


# --- DIAGNOSTIC: half-duplex input gate (echo-hypothesis test) ------------------
# Telephony has no client-side echo cancellation (unlike the OpenClaw Talk client
# the WS path relies on), so the bot's own audio echoes back down the line and
# retriggers the VAD/STT (the bot fights itself → rough conversation). This gate
# DROPS caller audio while the bot is speaking, plus a tail covering the gateway's
# bounded (~1 s) playout backlog. It is NOT the real fix — that's an echo canceller
# in the teaport-sip bridge (pjmedia AEC), keeping the brain transport-agnostic —
# but it isolates whether echo is the cause. Kill switch: SIP_HALF_DUPLEX=0.
HALF_DUPLEX = os.getenv("SIP_HALF_DUPLEX", "1").strip().lower() not in ("0", "false", "no")
_HD_TAIL_S = float(os.getenv("SIP_HALF_DUPLEX_TAIL_S", "0.8"))


class HalfDuplexInputGate(FrameProcessor):
    """Swallow caller InputAudioRawFrames while the bot is speaking (+ a tail), so
    un-cancelled line echo can't retrigger the VAD. Half-duplex: no barge-in."""

    def __init__(self, tail_s: float = _HD_TAIL_S):
        super().__init__()
        self._muted = False
        self._tail_s = tail_s
        self._unmute_task = None

    async def _unmute_after_tail(self):
        await asyncio.sleep(self._tail_s)
        self._muted = False
        logger.debug("half-duplex: unmuted (tail elapsed)")

    async def _cancel_pending(self):
        if self._unmute_task is not None:
            t, self._unmute_task = self._unmute_task, None
            await self.cancel_task(t)

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            await self._cancel_pending()
            if not self._muted:
                logger.debug("half-duplex: muted (bot speaking)")
            self._muted = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._cancel_pending()
            self._unmute_task = self.create_task(self._unmute_after_tail())
        # Drop the caller's mic while muted; pass everything else through.
        if (self._muted and direction == FrameDirection.DOWNSTREAM
                and isinstance(frame, InputAudioRawFrame)):
            return
        await self.push_frame(frame, direction)


def _initial_messages(system_prompt: str, lang_name: str | None) -> list:
    msgs = [{"role": "system", "content": system_prompt}]
    if lang_name:
        msgs.append({"role": "system", "content": f"Always reply to the user in {lang_name}."})
    return msgs


def _make_consult_followup(task, context, gate):
    """Follow-up injector for the ASYNC ask_openclaw path (verbatim from
    gateway_server.py). When a background consult finishes, append its answer to the
    context and run the LLM so the bot SPEAKS it as an unprompted turn, reattached to
    what the user asked. A failed/empty consult yields a brief 'couldn't get it' turn
    instead of silence. Bound per-session to this task + context; `gate` holds the
    turn until neither side is mid-speech."""
    async def speak_followup(request, text, tool_call_id=None):
        # Wait for a clear moment: don't step on the user mid-utterance OR the
        # assistant mid-answer about something else. (Gives up after max_wait so a
        # relentlessly chatty conversation can't strand the answer.)
        await gate.wait_until_idle()
        # Rewrite the placeholder tool result to the real outcome so the context
        # reads like any normally-completed tool call (see gateway_server.py for the
        # live-observed reason the placeholder instruction must not survive).
        rewrote = False
        for m in context.get_messages():
            if (isinstance(m, dict) and m.get("role") == "tool"
                    and m.get("tool_call_id") == tool_call_id):
                m["content"] = json.dumps(
                    {"status": "complete", "answer": text} if text
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
        # Deliver as a USER message, not a system one: a trailing system message is not
        # a turn the model answers — it re-answers the last real user question instead
        # (measured live in the merged fix), which is the "recites the previous answer"
        # bug. The tag matters because this message OUTLIVES the turn.
        trigger = {
            "role": "user",
            "content": f"[automated system notice, not spoken by the user]\n{content}"}
        context.add_message(trigger)
        logger.info(f"consult follow-up: delivering ({'answer' if text else 'failure'}; "
                    f"tool result {'rewritten' if rewrote else 'not found'})")
        await task.queue_frames([LLMRunFrame()])
        # Retire the one-shot trigger once spoken, or the model re-executes the
        # "tell the user now" standing order on the next empty turn (recites again).
        await gate.wait_until_delivered()
        trigger["content"] = ("[automated system notice, not spoken by the user]\n"
                              "An earlier background task finished and its outcome was "
                              "already given to the user. Nothing further is needed.")
    return speak_followup


async def run(sock_path: str):
    logger.info(f"connecting to teaport-sip gateway at {sock_path}")
    sock = connect_seqpacket(sock_path)
    logger.info("connected — the brain is the socket client (gateway is the server)")

    transport = SipGatewayTransport(sock, make_sip_params(SipProtocolSerializer()))

    # pipecat 1.x drives VAD from the user aggregator (not the transport); it owns
    # endpointing + barge-in. Same policy as the OpenClaw path (endpointing.py).
    vad_analyzer = _vad_cls()(
        params=VADParams(confidence=VAD_CONFIDENCE, min_volume=VAD_MIN_VOLUME,
                         stop_secs=ENDPOINT_STOP_SECS)
    )

    stt = make_stt()
    llm = make_llm()
    tts = make_tts()

    persona = load_persona()
    logger.info(f"loaded shared persona ({len(persona)} chars)")
    system_prompt = build_system_prompt(persona)
    lang_name = _TTS_LANG_NAMES.get(getattr(tts, "espeak_language", "en-us"))
    # Tools wired in exactly like the OpenClaw path (gateway_server.py): the schema
    # goes onto the context now; register_tools is deferred until after the
    # PipelineTask exists (the async ask_openclaw follow-up needs a task-bound
    # injector). The disconnect reset uses set_messages, which leaves _tools intact.
    context = LLMContext(_initial_messages(system_prompt, lang_name),
                         tools=build_tools_schema())
    if lang_name:
        logger.info(f"TTS language {lang_name} → instructing the LLM to reply in it")

    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_mute_strategies=[],
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
                stop=[TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=EagerSmartTurnAnalyzer(
                        complete_threshold=SMARTTURN_COMPLETE_THRESHOLD,
                        params=SmartTurnParams(stop_secs=ENDPOINT_STOP_SECS),
                    )
                )],
            ),
        ),
    )

    ledger = TranscriptLedger()  # observer: logs the merged user/assistant transcript
    heard_corrector = HeardContextCorrector(ledger, context)
    # Gates the async ask_openclaw follow-up so its unprompted turn only speaks in a
    # clear moment (mirrors gateway_server.py; placed right after transport.output()).
    followup_gate = FollowupGate()

    logger.info(f"half-duplex input gate: {'ON' if HALF_DUPLEX else 'off'} "
                f"(tail {_HD_TAIL_S}s) — diagnostic; real fix is AEC in the bridge")
    pipeline = Pipeline([p for p in [
        transport.input(),
        HalfDuplexInputGate() if HALF_DUPLEX else None,
        stt,
        context_aggregator.user(),
        heard_corrector,
        # A failed completion must be HEARD, not just logged. ABOVE the LLM: ErrorFrames
        # travel UPSTREAM, so below the LLM it never sees an LLM error (mirrors gateway_server).
        LLMErrorSpeaker() if llm_error_speaker.ENABLED else None,
        llm,
        # Capture the raw completion verbatim when it degenerates (must be upstream of the
        # guard + TTS aggregator, where the model's own bytes are still visible).
        RawLLMCapture() if raw_llm_capture.ENABLED else None,
        # Fold no-break/zero-width unicode out of the deltas and cut a runaway ellipsis
        # collapse — cleans both the spoken audio and the committed context in one place.
        # This is the degenerate-text guard from the merged fix; without it the SIP path
        # spoke the garbage/doubled output we saw live.
        LLMTextGuard() if llm_text_guard.ENABLED else None,
        tts,
        # Fill the dead air of a long ask_openclaw consult with a soft typing bed;
        # stops the instant the reply's first audio arrives. No-op for fast tools.
        # (Sits before transport.output() so the output transport resamples its
        # 24 kHz frames down to the SIP wire's 16 kHz, same as the TTS frames.)
        ThinkingSound() if thinking_sound.ENABLED else None,
        transport.output(),
        followup_gate,  # track user/bot/LLM activity → hold async follow-ups for a clear moment
        context_aggregator.assistant(),
    ] if p is not None])

    # SIP path runs ONE persistent pipeline across calls (unlike the OpenClaw path's
    # per-connection pipeline), so pipecat's idle-timeout (default ~5 min) must NOT
    # cancel it between calls — otherwise the brain dies and the next caller gets
    # silence. Proper fix is a per-call pipeline + shared slot (the reusable-agent
    # refactor); this keeps the persistent pipeline alive for now.
    task = PipelineTask(pipeline, observers=[ledger], cancel_on_idle_timeout=False)

    # Now that the task exists, wire the tool handlers (mirrors gateway_server.py).
    # ask_openclaw goes ASYNC: it hands the consult to a background waiter and this
    # injector speaks the answer as an unprompted follow-up turn when it lands. On
    # SIP the native openclaw_agent_consult round-trip has no plugin to service it
    # (dropped by the serializer), so the waiter degrades to the CLI agent_consult
    # path — still off the voice turn, still delivered via the same follow-up.
    register_tools(llm, lang=getattr(tts, "espeak_language", "en-us"), tts=tts,
                   followup=_make_consult_followup(task, context, followup_gate))

    greeted = {"active": False}

    async def greet():
        # Don't greet through a deaf STT: if the single-session engine is busy the
        # STT connect returns 503 and stt_available resolves False — warn instead.
        for _ in range(120):
            if stt.stt_available is not None:
                break
            await asyncio.sleep(0.1)
        if stt.stt_available is not True:
            logger.warning("STT unavailable at confirmed — warning the caller instead of greeting")
            await task.queue_frames([TTSSpeakFrame(
                "Sorry, I can't hear you right now — my speech recognition isn't "
                "available. Please hang up and call back in a moment."
            )])
            return
        # Greet with an LLM turn. Seed it as a USER-role stage direction, not a
        # system note: at confirm the context is otherwise system-only, and strict
        # OpenAI-compatible providers (observed: OpenRouter->qwen3) reject a
        # completion with "No user query found in messages". The parenthetical
        # reads as a call-connected cue, so the persona still generates the wording.
        context.add_message({
            "role": "user",
            "content": "(The phone call just connected. Greet me warmly in one short sentence.)",
        })
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        logger.info("SIP gateway socket connected")

    @transport.event_handler("on_hello")
    async def on_hello(_transport, msg):
        logger.info(f"gateway hello: proto={msg.get('proto')} rate={msg.get('rate')} "
                    f"ch={msg.get('channels')} ptime={msg.get('ptime_ms')}ms")

    @transport.event_handler("on_call_incoming")
    async def on_call_incoming(_transport, msg):
        logger.info(f"call.incoming id={msg.get('call_id')} from={msg.get('from')} to={msg.get('to')}")

    @transport.event_handler("on_dtmf")
    async def on_dtmf(_transport, call_id, digit):
        logger.info(f"dtmf {digit!r} (call {call_id})")

    @transport.event_handler("on_call_state")
    async def on_call_state(_transport, call_id, state):
        logger.info(f"call.state={state} (call {call_id})")
        if state == "confirmed" and not greeted["active"]:
            greeted["active"] = True
            await greet()
        elif state == "disconnected":
            # Reset for the next caller WITHOUT tearing down the pipeline (M2).
            greeted["active"] = False
            context.set_messages(_initial_messages(system_prompt, lang_name))
            heard_corrector._done = len(ledger.events)
            logger.info("call disconnected — LLM context reset for the next caller")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        logger.info("SIP gateway hung up (socket EOF) — stopping the pipeline")
        await task.cancel()

    logger.info("SIP brain pipeline starting (STT -> LLM -> TTS over teaport-sip)")
    await PipelineRunner(handle_sigint=False).run(task)
    logger.info("SIP brain pipeline stopped")


def main():
    parser = argparse.ArgumentParser(description="teaport SIP brain client (M2)")
    parser.add_argument("--socket", default=os.getenv("TEAPORT_SIP_SOCKET", DEFAULT_UDS_PATH),
                        help="gateway UDS path (default: the live /tmp/teaport-sip.sock)")
    args = parser.parse_args()
    logger.info("Priming TTS service...")
    make_tts()  # warm the engine TTS client once at startup
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
