# SPDX-License-Identifier: MIT
#
# talk_transport.py — the /talk websocket transport: pipecat's FastAPI websocket
# transport, with the playout pause the barge-in pauser needs (playout_pause.py).
#
# Every /talk client -- the OpenClaw plugin, the dashboard, the Discord bridge, the box's
# own mic (local_audio.py) -- is fed by the same output transport, which sends one audio
# block per block's worth of time (one block ahead of playout at most), so what the
# listener has not heard yet is still here and a pause in the writes is a pause in the
# voice, a block and a client's jitter margin later. Nothing about pausing goes on the
# wire: the client plays what it has and then whatever it plays when no audio comes.
#
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketOutputTransport,
    FastAPIWebsocketTransport,
)

from teaport_brain.playout_pause import PausablePlayout


class TalkOutputTransport(PausablePlayout, FastAPIWebsocketOutputTransport):
    """pipecat's websocket output, held at a block boundary while playout is paused."""

    _sending_secs = 0.0  # the block write_audio_frame is sending, until it is counted

    async def write_audio_frame(self, frame) -> bool:
        # One audio block per call (MediaSender's audio_chunk_size, 40 ms at pipecat's
        # default audio_out_10ms_chunks) -- or a whole buffer on the base class's direct
        # paths (the end's silence), which plays whole once it starts. A block the pause
        # held for a reply an interruption then cancelled is not written -- unless
        # pipecat marked it uninterruptible, which it keeps through an interruption.
        if not await self._wait_if_paused() and frame.interruptible:
            return False
        # Counted as heard once it is on the wire: the base sends it, then sleeps out its
        # slot (_write_audio_sleep, which it reaches only after a send that went out).
        self._sending_secs = (len(frame.audio) / 2 / max(1, self._params.audio_out_channels)
                              / self.sample_rate) if self.sample_rate else 0.0
        try:
            return await super().write_audio_frame(frame)
        finally:
            self._sending_secs = 0.0

    async def _write_audio_sleep(self):
        if self._sending_secs:
            self._wrote(self._sending_secs)
            self._sending_secs = 0.0
        await super()._write_audio_sleep()


class TalkWebsocketTransport(FastAPIWebsocketTransport):
    """FastAPIWebsocketTransport whose output can pause (TalkOutputTransport)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The base builds its output in __init__ with no way to pass a class: replaced
        # before anything has used it (pipecat builds the processors' tasks at setup).
        self._output = TalkOutputTransport(self, self._client, self._params,
                                           name=self._output_name)
