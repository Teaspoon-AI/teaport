# SPDX-License-Identifier: MIT
#
# playout_pause.py — the output transports' playout pause, for the barge-in pauser
# (barge_pause.py): stop writing the bot's audio, keep every sample of it, and go on
# from the same sample.
#
# One mechanism for every front end. Both of the brain's output transports keep a
# REAL-TIME send clock: the SIP transport sends one 20 ms datagram per 20 ms slot
# (sip_transport.SipGatewayOutputTransport._write_audio_sleep), and pipecat's FastAPI
# websocket transport -- the /talk path: the OpenClaw plugin, the dashboard, the Discord
# bridge, the box's own mic -- one audio block per block's worth of time (fastapi.py
# _write_audio_sleep; the "/ 2" there is bytes to samples, not a burst). Both clocks
# send the first two frames back to back and then one per slot, so the listener's end
# holds one frame beyond the one playing, and no more. So what has not been played yet
# is still in the BRAIN, queued in pipecat's MediaSender ahead of the transport, and a
# client holds no more than that frame and its own jitter margin: the room bridge's
# 120 ms lead ahead of its card, a browser its playout buffer.
# A transport that stops writing therefore stops the voice within that margin and loses
# nothing, and the client needs no pause control: it plays what it has, then whatever
# it plays when nothing comes (the bridge pads with silence; a browser idles), and plays
# on when the audio does. Only "clear" (the barge-in that cancels the reply) is ever a
# client's business.
#
# The host transport keeps the send clock; this mixin keeps the pause. What it relies on:
#   * `_next_send_time`: when the next write's slot begins, 0.0 to restart the clock at
#     the next write -- pipecat's websocket transports and ours share that spelling. A
#     resume restarts the clock rather than "catch up" by bursting the frames the pause
#     skipped.
#   * every write of bot audio awaits _wait_if_paused() first, at a frame boundary, and
#     drops what it was about to write when that returns False (an interruption dropped
#     the pause: the audio it held is the cut reply's); and reports each frame as it
#     sends it with _wrote(secs). That is the listener's playout, as the listener's end
#     plays it (back to back, from when each frame arrives): a pause's lag is how much of
#     what was sent is still to play (the ledger credits playout up to there), and
#     playout_tail_secs() what is left of the reply.
#
# The pause is announced both ways (PlayoutPauseFrame): the TranscriptLedger, the TTS's
# playout model, the start strategy and the captions all key off it (barge_pause.py,
# BOOKKEEPING).
#
import asyncio
import time

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from teaport_brain.barge_pause import PlayoutPauseFrame

# A playout pause ends on its own after this long. It blocks a write that MediaSender
# bounds at audio_out_write_timeout_secs (10 s) and treats running past it as the
# transport being dead for the rest of the session, so this stays under it whatever the
# pauser asks for.
_PAUSE_GUARD_S = 8.0


class PausablePlayout:
    """Playout pause for a BaseOutputTransport with a zero-lead real-time send clock.
    Mix in ahead of the transport class; see the module header for the contract."""

    # A BargeInPauser may be put in front of this transport (agent_session.py).
    supports_playout_pause = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set = writing; cleared while playout is paused. A write held here stops between
        # two frames, so what has not been written stays where it is -- in MediaSender's
        # queue, and any remainder the transport buffers -- and plays from that sample on
        # resume.
        self._playing = asyncio.Event()
        self._playing.set()
        self._paused_at = None
        # Bumped whenever a pause is DROPPED (an interruption, the end): a write that was
        # waiting on it learns that its audio belongs to a reply that is gone.
        self._pause_epoch = 0
        # Audio handed to this transport and not yet written (seconds), and whether the
        # context it belongs to has finished synthesizing: playout_tail_secs().
        self._unplayed_s = 0.0
        # When the audio sent so far finishes playing at the listener's end (_wrote).
        self._heard_until = 0.0
        self._synth_done = False

    async def set_playout_paused(self, paused: bool):
        """Stop writing the bot's audio, or start again, at the next frame boundary, and
        say so both ways (PlayoutPauseFrame) for the ledger, the TTS, the start strategy
        and the captions."""
        if paused == (not self._playing.is_set()):
            return
        lag = 0.0
        if paused:
            self._playing.clear()
            self._paused_at = time.monotonic()
            # What was sent before the pause still plays out: the listener stops hearing
            # the reply that much later (the frame playing and the one queued behind it).
            lag = max(0.0, self._heard_until - self._paused_at)
        else:
            self._playing.set()
            self._paused_at = None
            self._next_send_time = 0.0
        tail = self.playout_tail_secs() if paused else None
        await self._announce_pause(lag=lag, tail=-1.0 if tail is None else tail,
                                   paused=paused)

    async def _announce_pause(self, *, lag: float, tail: float, paused: bool = True):
        down = PlayoutPauseFrame(paused=paused, lag_secs=lag, tail_secs=tail)
        up = PlayoutPauseFrame(paused=paused, lag_secs=lag, tail_secs=tail)
        down.broadcast_sibling_id, up.broadcast_sibling_id = up.id, down.id
        await self.push_frame(down)
        await self.push_frame(up, FrameDirection.UPSTREAM)

    async def _wait_if_paused(self) -> bool:
        """Hold a write while playout is paused. True: write on. False: the pause was
        dropped under it (an interruption or the end), and what it holds is not to be
        written."""
        if self._playing.is_set():
            return True
        epoch = self._pause_epoch
        try:
            await asyncio.wait_for(self._playing.wait(), timeout=_PAUSE_GUARD_S)
        except asyncio.TimeoutError:
            logger.warning(f"{self}: playout pause still held after {_PAUSE_GUARD_S:.0f}s "
                           "-- resuming before the write deadline costs the session its "
                           "voice")
            await self.set_playout_paused(False)
        return epoch == self._pause_epoch

    def _drop_pause(self):
        """An interruption or the end: the queued audio is gone (or going), so there is
        nothing to resume -- just stop blocking."""
        if not self._playing.is_set():
            self._pause_epoch += 1
        self._playing.set()
        self._paused_at = None

    def playout_tail_secs(self):
        """How much of the reply is left to play, if it is all here -- its synthesis is
        over (the TTSStoppedFrame has reached this transport) -- else None. Counted from
        what was handed to this transport less what has been written since; the
        barge-in pauser does not pause a reply this close to its end."""
        return self._unplayed_s if self._synth_done else None

    def _wrote(self, secs: float):
        """`secs` of the bot's audio went to the listener just now."""
        self._heard_until = max(self._heard_until, time.monotonic()) + secs
        self._unplayed_s = max(0.0, self._unplayed_s - secs)

    def _track_tail(self, frame: Frame, direction: FrameDirection):
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, OutputAudioRawFrame) and frame.sample_rate:
            self._unplayed_s += frame.num_frames / frame.sample_rate
        elif isinstance(frame, TTSStartedFrame):
            self._synth_done = False
        elif isinstance(frame, TTSStoppedFrame):
            self._synth_done = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        self._track_tail(frame, direction)
        if (isinstance(frame, TTSStoppedFrame) and direction == FrameDirection.DOWNSTREAM
                and not self._playing.is_set()):
            # The reply's synthesis completed while playout is paused: its tail is known
            # only now. Say the pause again, with it, so the start strategy can tell when
            # the reply would have ended without the pause (barge_pause.py).
            await self._announce_pause(lag=0.0, tail=self._unplayed_s)
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # The barge-in cancelled the reply (MediaSender has dropped its queued audio):
            # nothing to resume, and the next reply must not wait on this pause.
            self._drop_pause()
            self._unplayed_s, self._synth_done = 0.0, False
            # The client is told to clear what it holds ("clear" on /talk).
            self._heard_until = 0.0

    async def stop(self, frame: EndFrame):
        self._drop_pause()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._drop_pause()
        await super().cancel(frame)
