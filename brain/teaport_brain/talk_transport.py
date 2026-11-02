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


    async def write_audio_frame(self, frame) -> bool:
        # One audio block per call (MediaSender's audio_chunk_size, 40 ms at pipecat's
        # default audio_out_10ms_chunks) -- or a whole buffer on the base class's direct
        # paths (the end's silence), which plays whole once it starts. A block the pause
        # held for a reply an interruption then cancelled is not written -- unless
        # pipecat marked it uninterruptible, which it keeps through an interruption.
        if not await self._wait_if_paused() and frame.interruptible:
            return False
        return await super().write_audio_frame(frame)

    async def _write_audio_sleep(self):
        # Counted as heard once it is on the wire: the base sends a block, then sleeps out
        # its slot here -- reached only after a send that went out. One block per slot,
        # as SIP counts its datagrams.
        self._wrote(self._send_interval)
        await super()._write_audio_sleep()


class TalkWebsocketTransport(FastAPIWebsocketTransport):
    """FastAPIWebsocketTransport whose output can pause (TalkOutputTransport) when
    `pausable` (barge_pause.TALK_ENABLED); otherwise pipecat's own, untouched."""

    def __init__(self, *args, pausable: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if pausable:
            # The base builds its output in __init__ with no way to pass a class:
            # replaced before anything has used it (pipecat builds the processors' tasks
            # at setup). Pinned by test_pipecat_premises.
            self._output = TalkOutputTransport(self, self._client, self._params,
                                               name=self._output_name)
