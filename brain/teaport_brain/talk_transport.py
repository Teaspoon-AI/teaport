# SPDX-License-Identifier: MIT
#
# talk_transport.py — the /talk websocket transport: pipecat's FastAPI websocket
# transport, with the playout pause the barge-in pauser needs (playout_pause.py).
#
# Every /talk client -- the OpenClaw plugin, the dashboard, the Discord bridge, the box's
# own mic (local_audio.py) -- is fed by the same output transport, which sends one audio
# block per block's worth of time (one block ahead of playout at most), so what the
# listener has not heard yet is still here and a pause in the writes is a pause in the
# voice, a block and a client's jitter margin later. The client plays what it has and
# then whatever it plays when no audio comes; only one that asks (?features=pause) is
# told of a pause (pause_notice).
#
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketOutputTransport,
    FastAPIWebsocketTransport,
)

from pipecat.frames.frames import OutputTransportMessageUrgentFrame

from teaport_brain.playout_pause import PausablePlayout


class TalkOutputTransport(PausablePlayout, FastAPIWebsocketOutputTransport):
    """pipecat's websocket output, held at a block boundary while playout is paused."""

    # Set by build_agent_session for a client that asked (?features=pause: the room
    # bridge): it is told {"type": "pause", "on": ...} as playout pauses and goes on, so
    # it does not take the quiet speaker for the reply's end (#255). Sent here, on the
    # transport's own socket as the pause is set, so it comes in order with the "clear" of
    # the barge-in that drops the pause, which ends it on the client too; a pause said
    # while that clear was being written is said over after it (_drop_pause).
    pause_notice = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._told_paused = False
        self._unsay = False

    async def _announce_pause(self, *, lag: float, tail: float, paused: bool = True,
                              upstream: bool = True):
        if self.pause_notice and paused != self._told_paused:
            self._told_paused = paused
            await self.send_message(OutputTransportMessageUrgentFrame(
                message={"type": "pause", "on": paused}))
        await super()._announce_pause(lag=lag, tail=tail, paused=paused, upstream=upstream)

    def _drop_pause(self):
        super()._drop_pause()
        # The "clear" with it ends it on the client -- but a pause said while that clear
        # was being written would come after it: say it is over (process_frame).
        self._unsay = self._told_paused
        self._told_paused = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self._unsay:
            self._unsay = False
            await self.send_message(OutputTransportMessageUrgentFrame(
                message={"type": "pause", "on": False}))

    async def write_audio_frame(self, frame) -> bool:
        # One audio block per call (MediaSender's audio_chunk_size, 40 ms at pipecat's
        # default audio_out_10ms_chunks) -- or a whole buffer on the base class's direct
        # paths (the end's silence), which plays whole once it starts. A block the pause
        # held for a reply an interruption then cancelled is not written -- unless
        # pipecat marked it uninterruptible, which it keeps through an interruption.
        # A client gone is not waited on: the base write fails at once for it.
        if self._client.is_closing or not self._client.is_connected:
            return False
        if not await self._wait_if_paused() and frame.interruptible:
            return False
        # Counted as heard as it goes on the wire -- the base sends it first thing, then
        # sleeps out its slot -- by its own length: a block, or a whole buffer the base
        # writes directly (the end's silence).
        if self.sample_rate:
            self._wrote(len(frame.audio) / 2 / max(1, self._params.audio_out_channels)
                        / self.sample_rate, frame)
        return await super().write_audio_frame(frame)


class TalkWebsocketTransport(FastAPIWebsocketTransport):
    """FastAPIWebsocketTransport whose output can pause (TalkOutputTransport) when
    `pausable` (barge_pause.talk_pauses); otherwise pipecat's own, untouched."""

    def __init__(self, *args, pausable: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if pausable:
            # The base builds its output in __init__ with no way to pass a class:
            # replaced before anything has used it (pipecat builds the processors' tasks
            # at setup). Pinned by test_pipecat_premises.
            self._output = TalkOutputTransport(self, self._client, self._params,
                                               name=self._output_name)
