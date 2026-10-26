#
# teaport — OpenClaw gateway-relay wire serializer
#
# Wire format between the OpenClaw `teaport` realtime-voice provider plugin
# (integrations/openclaw/teaport-realtime) and this Pipecat server. The plugin is
# the WS *client*; this Pipecat process is the WS *server*. OpenClaw's
# gateway-relay path carries PCM16 mono 24 kHz audio (base64 inside its own
# JSON-RPC); the plugin decodes that to raw bytes and speaks this simpler framing:
#
#   plugin -> us (deserialize):
#       binary frame              raw PCM16 24 kHz mono  -> InputAudioRawFrame
#       text {"type":"close"}                            -> EndFrame
#       text {"type":"barge_in"}                         -> None (VAD owns barge-in)
#       text {"type":"tool_result",...}                  -> None (consult_bridge: a
#                                                           consult's answer, or a
#                                                           client tool's result)
#       text {"type":"context","id":...,"text":...,"respond":bool[,"kind":...]
#             [,"source":"host"]}                       -> ClientContextFrame
#                                                           (client_notes.py; "host":
#                                                           OpenClaw's own message for
#                                                           the model, which only the
#                                                           plugin sends: believed only
#                                                           with ?run_control=1)
#       text {"type":"run_control_result","id":...,"claimed":bool}
#                                                        -> RunControlResultFrame
#                                                           (run_control.py: did OpenClaw's
#                                                           run control take the words)
#
#   us -> plugin (serialize):
#       OutputAudioRawFrame       -> binary raw PCM16 24 kHz  (bot speech)
#       {"type":"hello","features":{"context":{limits}}[,"face_mood":tag|text|off]}
#                                                           (face_mood: only to a client that
#                                                           announced ?features=mood)
#                                                           (on accept, written straight
#                                                           to the socket by gateway_server)
#       {"type":"response","state":"start"|"done"}  (frames an utterance said outside
#                                                     the pipeline: the busy line to a
#                                                     refused client, written straight
#                                                     to the socket by gateway_server)
#       OutputTransportMessage[Urgent]Frame -> JSON text, the .message dict:
#           {"type":"ready"}                              (notes may be sent: the pipeline
#                                                           runs and the STT works)
#           {"type":"clear"}                              (barge-in: flush playback)
#           {"type":"transcript","role":...,"text":...,"final":bool[,"utterance":id]}
#             text is always the FULL text so far, never a delta; the plugin
#             adapts it to how the OpenClaw version merges transcripts.
#             Assistant captions carry "utterance" (their TTS context id).
#           {"type":"mood","mood":name,"starts":[w1..w4]}  (the model tagged this reply
#                                                           with a mood, mood_tag.py; starts =
#                                                           its first words, to tell which reply; sent
#                                                           only to a client that announced
#                                                           ?features=mood)
#           {"type":"context_result","id":...,"ok":bool,...}  (answers a context note)
#           {"type":"run_control","id":...,"text":...}     (is this final OpenClaw's? sent
#                                                           only to a plugin that announced
#                                                           ?run_control=1; run_control.py)
#           {"type":"client_tool","call_id":...,"name":...,"args":{...}}
#                                                         (a tool the client performs —
#                                                           set_volume, restart_session —
#                                                           sent only to a client that
#                                                           announced it in ?features=;
#                                                           answered with a tool_result)
#           {"type":"tool_call","call_id":...,"name":...,"args":{...}}
#                                                         (a tool call for the relay's
#                                                           onToolCall: a host-run tool,
#                                                           openclaw_agent_consult or, on
#                                                           Talk, openclaw_agent_control,
#                                                           answered with a tool_result;
#                                                           or a display-only card)
#           {"type":"tool_cancel","call_id":...}           (the brain stopped waiting on a
#                                                           consult the relay acknowledged:
#                                                           the relay is to stop it too,
#                                                           tools._cancel_at_relay)
#
# Pipecat serializes audio (write_audio_frame) and OutputTransportMessage frames
# (send_message); it never serializes transcripts itself, so the emitters in
# gateway_server.py (UserTranscriptEmitter for user transcripts, AssistantFinal
# for the bot's final reply, CaptionTap for played-caption partials) turn those
# pipeline events into OutputTransportMessage frames that land here.
#
import json
import time

from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from teaport_brain.client_notes import ClientContextFrame

# OpenClaw gateway-relay fixes audio at PCM16 / 24 kHz / mono both ways
# (REALTIME_VOICE_AUDIO_FORMAT_PCM16_24KHZ). Silero VAD and the engine STT want 16 kHz,
# and the input transport sets the VAD rate from the frame but does NOT resample, so
# we downsample inbound audio here. Outbound TTS is already at the relay rate (the
# output transport resamples to audio_out_sample_rate), so it passes straight through.
RELAY_SAMPLE_RATE = 24000
PIPELINE_SAMPLE_RATE = 16000


class TeaportGatewaySerializer(FrameSerializer):
    """Binary PCM16 audio + JSON text control between the plugin and the pipeline.

    Inbound relay audio (24 kHz) is resampled to the pipeline rate (16 kHz) so Silero
    VAD accepts it; outbound TTS audio is already at the relay rate, so it passes
    straight through.
    """

    def __init__(self, relay_rate: int = RELAY_SAMPLE_RATE,
                 pipeline_rate: int = PIPELINE_SAMPLE_RATE, *, host_notes: bool = False):
        super().__init__()
        self._relay_rate = relay_rate
        self._pipeline_rate = pipeline_rate
        # Whether a context note's "source": "host" is believed: only from the teaport
        # plugin, which announces ?run_control=1 (gateway_server). Any other /talk client
        # could otherwise pass its text off as OpenClaw's word, an order the model
        # follows (client_notes.py, HOST NOTES); from one, it is an app-event note.
        self._host_notes = host_notes
        # soxr streaming resampler: stateful across calls for this session, the same
        # role the audioop.ratecv running-state served (native pipecat; drops audioop-lts).
        self._in_resampler = create_stream_resampler()
        # When the client last sent anything (time.monotonic()): every /talk client streams
        # its microphone, so a long silence here means the client is gone (the session
        # arbiter reaps such a session rather than refuse a newcomer for it).
        self.last_rx = time.monotonic()

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            # Already at audio_out_sample_rate (= relay rate) by now.
            return bytes(frame.audio)
        if isinstance(frame, InterruptionFrame):
            # Barge-in. The interruption is broadcast out-of-band (it never reaches a
            # mid-pipeline processor), but the output transport routes it through the
            # serializer here — same hook Pipecat's Twilio serializer uses for clear.
            logger.debug("serializer: sending {'type':'clear'} (barge-in flush)")
            return json.dumps({"type": "clear"})
        if isinstance(frame, (OutputTransportMessageUrgentFrame, OutputTransportMessageFrame)):
            if self.should_ignore_frame(frame):  # drop RTVI protocol messages
                return None
            try:
                return json.dumps(frame.message)
            except (TypeError, ValueError):
                return None
        return None

    async def deserialize(self, data):
        self.last_rx = time.monotonic()
        # One malformed message from the network must never kill the session:
        # anything we can't parse is logged and dropped, never raised.
        if isinstance(data, (bytes, bytearray)):
            audio = bytes(data)
            if self._relay_rate != self._pipeline_rate:
                try:
                    audio = await self._in_resampler.resample(
                        audio, self._relay_rate, self._pipeline_rate
                    )
                except Exception as e:  # noqa: BLE001 — network-boundary audio: never kill the session
                    logger.warning(
                        f"gateway serializer: dropping unresamplable audio frame "
                        f"({len(audio)} bytes): {e}"
                    )
                    return None
            return InputAudioRawFrame(
                audio=audio, sample_rate=self._pipeline_rate, num_channels=1
            )
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(msg, dict):
            logger.warning(f"gateway serializer: ignoring non-object control message: {msg!r}")
            return None
        if msg.get("type") == "close":
            return EndFrame()
        if msg.get("type") == "tool_result":
            # Relay -> brain: the result of a call the client runs (a consult, a
            # control call, a client tool; see consult_bridge). Resolved here directly —
            # no pipeline frame; the awaiting tool handler picks it up off its future.
            from teaport_brain import consult_bridge
            consult_bridge.resolve(
                str(msg.get("call_id") or ""), msg.get("result"),
                will_continue=bool(msg.get("will_continue")),
            )
            return None
        if msg.get("type") == "context":
            # A client context note (teaport.talk.context). Typed loosely here and
            # judged by ClientNotes, which answers every note with a context_result --
            # a note refused here would leave the plugin waiting on its ack.
            text, kind, req = msg.get("text"), msg.get("kind"), msg.get("id")
            return ClientContextFrame(
                text=text if isinstance(text, str) else "",
                respond=msg.get("respond") is True,
                kind=kind if isinstance(kind, str) and kind else None,
                request_id=str(req) if req is not None else None,
                host=self._host_notes and msg.get("source") == "host",
            )
        if msg.get("type") == "run_control_result":
            from teaport_brain.run_control import RunControlResultFrame
            req = msg.get("id")
            return RunControlResultFrame(request_id=str(req) if req is not None else "",
                                         claimed=msg.get("claimed") is True)
        # barge_in: the pipeline VAD already interrupts from the forwarded mic
        # audio, so an explicit signal needs no separate injection here.
        return None
