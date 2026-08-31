#
# teaport — Pipecat STT service
#
# A Pipecat streaming STT service that uses a teaport engine
# (the engine's realtime STT) as the transcriber, over the engine's
# vLLM /v1/realtime-compatible WebSocket.
#
# Structure mirrors pipecat's GladiaSTTService (the canonical WebsocketSTTService
# subclass that uses raw `websockets`); frame/metric conventions reconciled
# against DeepgramSTTService.
#
# Protocol (verified against the engine websocket implementation):
#   endpoint:  ws://<host>:<port>/v1/realtime   (no auth)
#   we send:   {"type":"session.update","model":"..."}                      (handshake; ack-only)
#              {"type":"input_audio_buffer.append","audio":"<base64 PCM16>"} (mono, 16 kHz)
#              {"type":"input_audio_buffer.commit","final":true}            (forces a final)
#   we recv:   {"type":"session.created","id":"sess_...","created":N}
#              {"type":"transcription.delta","delta":"<piece>","timestamp":1.2}  (append-only)
#              {"type":"transcription.done","text":"<full utterance>","usage":{...}}
#              {"type":"error","error":"...","code":"..."}
#
# Key semantics:
#   - Deltas are APPEND-ONLY token pieces (not revisable). We keep a running
#     buffer and emit it as the cumulative InterimTranscriptionFrame each time.
#   - A final (transcription.done) is gated on an explicit commit. We send the
#     commit on VADUserStoppedSpeakingFrame (Pipecat VAD endpointing) by default.
#     The engine resets its text after each done, so done.text is per-utterance
#     and we reset our buffer on it.
#

import asyncio
import base64
import json
from typing import AsyncGenerator, Optional

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    UninterruptibleFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

# P99 latency from speech end to final transcript (broadcast to downstream turn
# strategies). The engine's realtime STT targets <500 ms delta delay; ~0.8 s end-to-end p99
# is a conservative default — override per deployment.
TEAPORT_TTFS_P99 = 0.8

# How many consecutive word-less finals before the brain says it cannot hear. Low enough
# to catch a dead microphone inside one exchange, high enough that ordinary VAD triggers
# on room noise never reach it.
_EMPTY_FINAL_RUN = 5

# Retry budget for a connect the engine REJECTS because its single STT slot is still
# held (see _connect_websocket). Sized to cover the hand-off window both front-ends
# currently paper over with `await asyncio.sleep(0.3)`, while staying well inside
# greet()'s 12s tri-state resolution window so a genuinely busy engine still gets the
# spoken warning promptly.
_CONNECT_ATTEMPTS = 4
_CONNECT_RETRY_S = 0.4


try:
    import websockets
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"{e}\nInstall with: pip install websockets")
    raise


def _is_slot_busy(exc: Exception) -> bool:
    """True for an engine REJECTION (HTTP 503 / 4xx-5xx handshake failure), as opposed
    to an unreachable host.

    The distinction decides whether retrying is worth anything: a rejection means the
    engine is up and its slot is momentarily taken, an unreachable host means each
    further attempt burns the websockets open timeout for nothing.

    Duck-typed on purpose. websockets is lock-pinned but bounded per-major and bumped
    deliberately (see brain/pyproject.toml — the range is OURS to own; pipecat's base
    dep is unbounded), and the rejection carries its status as `.response.status_code` on
    >=14 but `.status_code` on older releases. Matching the shape rather than the class
    keeps this working across that range; an unrecognised exception is treated as NOT
    retryable, so the worst case is the behaviour we had before.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return isinstance(status, int)


class FinalTranscriptionFrame(TranscriptionFrame, UninterruptibleFrame):
    """A final transcript that an interruption cannot flush.

    TranscriptionFrame is a plain data frame. On InterruptionFrame every processor
    flushes its queued data frames (FrameProcessor._start_interruption ->
    FrameQueue.reset), and the LLM user aggregator flushes its own queue the moment
    it broadcasts one (FrameProcessor.broadcast_interruption). The aggregator
    broadcasts exactly that when MinWordsUserTurnStartStrategy starts a user turn
    from an INTERIM — so when the engine closes a segment on its own, the final
    rides a few frames behind the interim that starts the turn, and the turn's own
    start flushes the turn's own words out of the pipeline. The turn is then empty:
    TurnAnalyzerUserTurnStopStrategy cannot fire without transcript text, the 5 s
    user_turn_stop_timeout force-stops the turn, push_aggregation() has nothing to
    push, and the user gets silence. Live 2026-08-25 19:05 on jetson01: "Can you
    recite Hamlet's soliloquy for me?" — charted by the ledger at the STT's push,
    never seen by the model (SILENT TURN reached=['nothing']).

    UninterruptibleFrame is pipecat's designed escape hatch for exactly this:
    FrameQueue.reset() keeps such frames, and a task processing one is not
    cancelled (same pattern as pipecat's own FunctionCallResultFrame). Interims
    stay interruptible on purpose — they are disposable hypotheses.
    """


class TeaportSTTService(WebsocketSTTService):
    """Streams audio to a teaport engine and yields interim + final transcripts.

    Args:
        url: engine /v1/realtime WebSocket URL (e.g. ws://jetson.local:8000/v1/realtime).
        model: model name sent in the session.update handshake. the engine is a
            single-model server and only acknowledges this, so any value works.
        sample_rate: audio sample rate fed to the engine. the engine expects 16 kHz
            mono PCM16; Pipecat resamples to this for us.
        language: language tag attached to emitted frames.
        commit_on_user_stopped_speaking: if True, send input_audio_buffer.commit
            when Pipecat emits VADUserStoppedSpeakingFrame, forcing a per-utterance
            final. Set False if the engine runs its own VAD and auto-commits.
    """

    def __init__(
        self,
        *,
        url: str = "ws://127.0.0.1:8000/v1/realtime",
        model: str = "voxtral-mini-realtime",
        sample_rate: int = 16000,
        language: Language = Language.EN,
        commit_on_user_stopped_speaking: bool = True,
        ttfs_p99_latency: float = TEAPORT_TTFS_P99,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=model, language=language),
            ttfs_p99_latency=ttfs_p99_latency,
            **kwargs,
        )
        self._url = url
        self._model = model
        self._language = language
        self._commit_on_stop = commit_on_user_stopped_speaking

        # Running transcript for the current utterance (deltas are append-only).
        self._interim_buffer: str = ""
        # Consecutive finals that carried no words at all — see _handle_message.
        self._empty_finals = 0
        # _receive_task is created only on a SUCCESSFUL connect; initialize it here
        # so a failed/503 connect doesn't AttributeError during _disconnect teardown.
        self._receive_task = None
        # STT engine reachability: None = not yet attempted, True = session open,
        # False = unavailable (e.g. the single-session engine returned 503). The
        # brain reads stt_available to warn the user it cannot hear.
        self._stt_available = None
        # WHY the last connect failed, when it did: True = the engine REJECTED us (its
        # one session is taken), False = we could not reach it at all. None until a
        # failure resolves it. The brain speaks a different line for each — telling a
        # caller the assistant is "busy with another session" when the engine is simply
        # down sends them to the wrong remedy entirely.
        self._slot_busy = None

    @property
    def stt_available(self):
        """None until a connect is attempted; True if the engine session is open;
        False if the engine was unavailable (no free session / 503 / refused)."""
        return self._stt_available

    @property
    def slot_busy(self):
        """True if the last failure was the engine REJECTING us (503 — its single
        session is held), False if the engine was unreachable, None if no connect has
        failed. Only meaningful when stt_available is False."""
        return self._slot_busy

    def can_generate_metrics(self) -> bool:
        """True: we start processing metrics in run_stt and stop them on the
        final transcript (mirrors GladiaSTTService)."""
        return True

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    # ---- audio in ----------------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Optional[Frame], None]:
        """Forward one chunk of PCM16 audio to the engine. Transcripts arrive
        asynchronously via _receive_messages, so we just yield None here.

        Metrics: processing metrics are started here per chunk (each call
        restarts the clock, so the reported span is last audio chunk -> final
        transcript) and stopped on the final (mirrors GladiaSTTService)."""
        await self.start_processing_metrics()
        if self._websocket:
            payload = base64.b64encode(audio).decode("ascii")
            try:
                await self._websocket.send(
                    json.dumps({"type": "input_audio_buffer.append", "audio": payload})
                )
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                # Connection died under us — drop the chunk. The receive task
                # owns reconnection; raising here would kill the pipeline task.
                logger.warning(f"{self}: audio chunk dropped, send failed: {e}")
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Commit (force a final) when the VAD detects end-of-speech — NOT on
        # UserStoppedSpeakingFrame. With the Smart Turn turn strategy that turn-
        # stop frame only fires AFTER the turn is judged complete, which itself
        # waits for the final transcript — a deadlock that left finalization to
        # the engine's ~15s buffer auto-commit (multi-second endpointing lag).
        # VADUserStoppedSpeakingFrame fires promptly at ~stop_secs of silence, so
        # the engine finalizes right away and Smart Turn decides on a fresh final
        # (fragments from mid-utterance pauses are re-aggregated by the LLM
        # aggregator, exactly as the strategy expects).
        if self._commit_on_stop and isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._send_commit(final=True)

    async def _send_commit(self, final: bool = True):
        if self._websocket:
            try:
                await self._websocket.send(
                    json.dumps({"type": "input_audio_buffer.commit", "final": final})
                )
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                # Lost commit = lost turn (no final transcript), but raising here
                # would kill the whole pipeline. The receive task owns reconnection.
                logger.warning(f"{self}: commit dropped, send failed: {e}")

    # ---- connection (WebsocketSTTService contract) -------------------------

    async def _connect(self):
        await super()._connect()  # base resets the _disconnecting flag
        await self._connect_websocket()
        # Only run the receiver if the websocket actually connected (a 503/refused
        # connect leaves _websocket None — see _connect_websocket).
        if self._websocket:
            # Base class wraps _receive_messages with error reporting + reconnect.
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()  # base sets _disconnecting (suppresses reconnect)
        try:
            if self._receive_task:
                await self.cancel_task(self._receive_task)
                self._receive_task = None
        finally:
            # The engine is single-session: its slot is only freed by our close
            # reaching it, so the close must run even if task-cancel raises.
            await self._disconnect_websocket()

    async def _connect_websocket(self):
        if self._websocket:
            return
        logger.debug(f"{self}: connecting to the engine at {self._url}")
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                self._websocket = await websockets.connect(self._url)
                # Handshake: the engine is single-model and just acknowledges this.
                await self._websocket.send(
                    json.dumps({"type": "session.update", "model": self._model})
                )
                self._stt_available = True
                if attempt:
                    logger.info(f"{self}: STT slot acquired on attempt {attempt + 1} "
                                "(the previous session's close was still settling)")
                return
            except Exception as e:  # 503 (engine session busy), refused, timeout, ...
                # If connect succeeded but the handshake send failed, an OPEN socket
                # holds the engine's single session slot — abandoning the reference
                # leaks the slot until TCP notices ("talks but can't hear" for every
                # later session). Close it before dropping it.
                ws, self._websocket = self._websocket, None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
                # A 503 means the engine is up and its single slot is taken — which,
                # right after an eviction, usually means "taken by the close we are
                # still waiting on". Both front-ends hand that over to a bare
                # `await asyncio.sleep(0.3)` and then connect once; if the engine has
                # not finished processing the close by then, this session declares
                # itself deaf and the user is told the assistant is busy with another
                # session when nothing is using it at all (SIP also hangs the caller
                # up). TLC finds the interleaving with one OpenClaw session and one
                # SIP call — brain/formal/SttSlot.tla, MODE = "fixedSettle",
                # invariant NoFalseBusy — because nothing anywhere observes that the
                # engine has actually freed the slot.
                #
                # So retry a REJECTION briefly. Only a rejection: a refused or
                # blackholed host is not going to fix itself in a second, and each
                # such attempt costs the websockets open timeout (~10s), which would
                # blow through greet()'s resolution window and leave the caller in
                # silence instead of hearing the warning.
                if attempt + 1 < _CONNECT_ATTEMPTS and _is_slot_busy(e):
                    logger.debug(f"{self}: engine slot busy ({type(e).__name__}) — "
                                 f"retrying in {_CONNECT_RETRY_S}s "
                                 f"({attempt + 2}/{_CONNECT_ATTEMPTS})")
                    await asyncio.sleep(_CONNECT_RETRY_S)
                    continue
                self._stt_available = False
                self._slot_busy = _is_slot_busy(e)
                logger.error(
                    f"{self}: STT engine unavailable ({type(e).__name__}: {e}) — the "
                    f"agent cannot hear; the brain will warn the user "
                    f"({'engine slot held' if self._slot_busy else 'engine unreachable'})."
                )
                # Do NOT re-raise: let the pipeline start so the brain can speak a
                # pre-computed warning instead of silently 'talking but not hearing'.
                return

    async def _disconnect_websocket(self):
        try:
            if self._websocket and self._websocket.state is not State.CLOSED:
                # Close in ANY live state, not just OPEN: a CONNECTING/CLOSING or
                # half-open socket dropped by reference never finishes the close
                # handshake from our side, and the single-session engine's slot
                # stays held until TCP times it out. close() is idempotent.
                logger.debug(f"{self}: disconnecting from the engine")
                await self._websocket.close()
        finally:
            self._websocket = None
            self._interim_buffer = ""
            # Per-session, like the buffer beside it: a run of 4 empty finals before a
            # reconnect plus 1 after is not 5 finals of the same microphone.
            self._empty_finals = 0

    # ---- transcripts out ---------------------------------------------------

    async def _receive_messages(self):
        # Iterate the base class's websocket directly (WebsocketSTTService owns it).
        # Returning normally on graceful close is expected; the base
        # _receive_task_handler handles reconnect/cleanup.
        if not self._websocket:
            return
        async for message in self._websocket:
            try:
                msg = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"{self}: non-JSON message: {message!r}")
                continue
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict):
        mtype = msg.get("type")

        if mtype == "transcription.delta":
            # Append-only token piece → grow the running utterance buffer and
            # emit it as the cumulative interim hypothesis.
            self._interim_buffer += msg.get("delta", "")
            await self.push_frame(
                InterimTranscriptionFrame(
                    self._interim_buffer,
                    self._user_id,
                    time_now_iso8601(),
                    self._language,
                    result=msg,
                )
            )

        elif mtype == "transcription.done":
            # Authoritative per-utterance text; engine resets after this. Mark it
            # finalized=True: this IS the definitive final, so the turn-stop
            # strategy triggers the turn the instant it arrives (once Smart Turn
            # says complete) instead of waiting out its STT p99-latency fallback
            # timeout — that fallback is for STTs that never signal a final, and
            # eating it added ~(ttfs_p99 - stop_secs) of dead hang after every
            # finished utterance.
            # `or`, not get()'s default: the engine sends "text": "" (key PRESENT,
            # value empty) for an utterance it could not finalize, so the default is
            # never reached and the interim fallback was dead code for the one case it
            # was written for. Verified against the engine on 2026-08-20 -- 1.5s of
            # silence returns {"type": "transcription.done", "text": "", ...}.
            #
            # Pushing nothing wedges the turn. MinWordsUserTurnStartStrategy starts the
            # user turn from an INTERIM, but TurnAnalyzerUserTurnStopStrategy only ever
            # sets its _text from a final TranscriptionFrame, and
            # _maybe_trigger_user_turn_stopped() returns early while that is empty. So a
            # turn that started on interims and got an empty final can never stop: it
            # waits out the aggregator's 5s user_turn_stop_timeout, and since any VAD or
            # transcription frame re-arms that timeout, a noisy mic starves it and the
            # turn never ends at all. Falling back to the hypothesis we already streamed
            # both recovers the words and lets the turn close.
            # The ENGINE's own verdict, kept separate from what we go on to push. The
            # empty-final run below has to be counted on this and not on the fallback:
            # `or self._interim_buffer` made `text` truthy whenever the engine had
            # streamed so much as one delta before deciding the audio held no speech, so
            # _empty_finals was reset instead of incremented and the "microphone muted"
            # warning could never reach its run — the two halves of this hunk cancelled
            # out. .strip() because a final of " " is truthy but wordless, and pushing it
            # commits a user turn whose content is whitespace.
            engine_text = (msg.get("text") or "").strip()
            text = engine_text or self._interim_buffer.strip()
            if text and not engine_text:
                logger.debug(f"{self}: empty final — falling back to the interim "
                             f"hypothesis {text[:60]!r} to close the turn")
            self._interim_buffer = ""
            # A run of finals with nothing in them means the engine is being handed audio
            # it can find no words in. Both are silent from the pipeline's side — no
            # transcript, so no turn, so no reply — and that is indistinguishable from the
            # pipeline bugs that produce the same silence, which is why it has to say so.
            #
            # Live 2026-08-21: 77 seconds of it. The engine ran 11 segments and returned 0
            # characters for every one while the brain's own VAD stayed QUIET, so the room
            # was making noise and the user's voice was not reaching the microphone. The
            # engine was healthy throughout — fed a synthesized sentence it transcribed it
            # perfectly — and nothing anywhere said "I cannot hear you".
            #
            # One line per run, not per final: an isolated empty final is ordinary (the VAD
            # fires on a cough or a door), and warning on each would bury the real case.
            if not engine_text:
                self._empty_finals += 1
                if self._empty_finals == _EMPTY_FINAL_RUN:
                    logger.warning(
                        f"{self}: {self._empty_finals} finals in a row with no words — "
                        "the engine is hearing audio but no speech (microphone muted or "
                        "routed away, or the room is louder than the speaker)"
                    )
            else:
                if self._empty_finals >= _EMPTY_FINAL_RUN:
                    logger.info(f"{self}: hearing speech again after "
                                f"{self._empty_finals} empty finals")
                self._empty_finals = 0
            if text:
                await self.push_frame(
                    FinalTranscriptionFrame(
                        text,
                        self._user_id,
                        time_now_iso8601(),
                        self._language,
                        result=msg,
                        finalized=True,
                    )
                )
            await self.stop_processing_metrics()

        elif mtype == "session.created":
            logger.debug(f"{self}: session created {msg.get('id')}")

        elif mtype == "error":
            logger.error(
                f"{self}: engine error: {msg.get('error')} ({msg.get('code')})"
            )

        else:
            logger.trace(f"{self}: unhandled message type {mtype!r}")
