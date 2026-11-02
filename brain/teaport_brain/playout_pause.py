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
#   * every write of bot audio awaits _wait_if_paused(deadline) first, at a frame
#     boundary, with one deadline for the whole write (write_deadline()), and
#     drops what it was about to write when that returns False (an interruption dropped
#     the pause: the audio it held is the cut reply's); and reports each frame as it
#     sends it with _wrote(secs). That is the listener's playout, as the listener's end
#     plays it (back to back, from when each frame arrives): a pause's lag is how much of
#     what was sent is still to play (the ledger credits playout up to there), and
#     playout_tail_secs() what is left of the reply.
#
# The pause is announced both ways (PlayoutPauseFrame): the TranscriptLedger, the TTS's
# playout model and the start strategy key off it (barge_pause.py, BOOKKEEPING).
#
# Not here: pipecat's clock releases each word (TTSTextFrame, its caption progress, a
# reply's End) at its pts on the wall clock, pause or not -- a known limit (barge_pause.py,
# Known limits), kept for one design of the playout timing across a pause.
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

# The longest one write may wait on pauses -- all of them, however many come and go
# while it waits -- before the pause ends on its own. MediaSender bounds the whole write
# at audio_out_write_timeout_secs (10 s) and treats running past it as the transport
# being dead for the rest of the session, so this stays under it whatever the pauser
# asks for.
_PAUSE_GUARD_S = 8.0


class PausablePlayout:
    """Playout pause for a BaseOutputTransport with a real-time send clock. Mix in ahead
    of the transport class; see the module header for the contract."""

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
        self._pause_lag = 0.0
        # Bumped when an interruption drops a pause: a write that was waiting on it
        # learns that its audio belongs to a reply that is gone.
        self._pause_epoch = 0
        # Audio handed to this transport and not yet written (seconds), and whether the
        # context it belongs to has finished synthesizing: playout_tail_secs().
        self._unplayed_s = 0.0
        # When the audio sent so far finishes playing at the listener's end (_wrote).
        self._heard_until = 0.0
        self._synth_done = False

    async def set_playout_paused(self, paused: bool, *, upstream: bool = True):
        """Stop writing the bot's audio, or start again, at the next frame boundary, and
        say so both ways (PlayoutPauseFrame) for the ledger, the TTS and the start
        strategy -- downstream only, not `upstream`, at the end of the session (the
        processors above have had their EndFrame)."""
        if paused == (not self._playing.is_set()):
            return
        lag = 0.0
        if paused:
            self._playing.clear()
            self._paused_at = time.monotonic()
            # What was sent before the pause still plays out: the listener stops hearing
            # the reply that much later (the frame playing and the one queued behind it).
            lag = max(0.0, self._heard_until - self._paused_at)
            self._pause_lag = lag
        else:
            self._playing.set()
            self._paused_at = None
            self._next_send_time = 0.0
        tail = self.playout_tail_secs() if paused else None
        await self._announce_pause(lag=lag, tail=-1.0 if tail is None else tail,
                                   paused=paused, upstream=upstream)

    async def _announce_pause(self, *, lag: float, tail: float, paused: bool = True,
                              upstream: bool = True):
        fields = dict(paused=paused, lag_secs=lag, tail_secs=tail)
        if upstream:
            await self.broadcast_frame(PlayoutPauseFrame, **fields)
        else:
            await self.push_frame(PlayoutPauseFrame(**fields))

    @staticmethod
    def write_deadline() -> float:
        """The deadline one write's waits on pauses share (_wait_if_paused)."""
        return time.monotonic() + _PAUSE_GUARD_S

    async def _wait_if_paused(self, deadline: float = None) -> bool:
        """Hold a write while playout is paused, until `deadline` (time.monotonic(); the
        write's own, from write_deadline(): one bound for all its waits, under
        MediaSender's). True: write on. False: an interruption dropped the pause under
        it, and what it holds is the cut reply's, not to be written."""
        if self._playing.is_set():
            return True
        epoch = self._pause_epoch
        deadline = self.write_deadline() if deadline is None else deadline
        # Until it is really playing: a resume and a new pause in the same turn of the
        # loop wake this waiter with playout paused again.
        while not self._playing.is_set():
            try:
                await asyncio.wait_for(self._playing.wait(),
                                       timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                logger.warning(f"{self}: a write held by playout pauses for "
                               f"{_PAUSE_GUARD_S:.0f}s -- resuming before the write "
                               "deadline costs the session its voice")
                await self.set_playout_paused(False)
        return epoch == self._pause_epoch

    def _drop_pause(self):
        """An interruption or a cancel: the queued audio is the cut reply's, gone --
        nothing to resume, just stop blocking. (The end resumes instead: what is queued
        plays out.)"""
        if not self._playing.is_set():
            self._pause_epoch += 1
        self._playing.set()
        self._paused_at = None

    def playout_tail_secs(self):
        """How much of the reply is left to play, if it is all here -- its synthesis is
        over (the TTSStoppedFrame has reached this transport) -- else None: what was
        handed to this transport and not written yet, and what was written and is still
        playing (the lag a pause has). The barge-in pauser does not pause a reply this
        close to its end, and a pause's tail_secs is this."""
        if not self._synth_done:
            return None
        if self._paused_at is not None:
            return self._unplayed_s + self._pause_lag
        return self._unplayed_s + max(0.0, self._heard_until - time.monotonic())

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
            await self._announce_pause(lag=0.0, tail=self.playout_tail_secs())
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # The barge-in cancelled the reply (MediaSender has dropped its queued audio):
            # nothing to resume, and the next reply must not wait on this pause.
            self._drop_pause()
            self._unplayed_s, self._synth_done = 0.0, False
            # The client is told to clear what it holds ("clear" on /talk).
            self._heard_until = 0.0

    async def stop(self, frame: EndFrame):
        # The end plays out what is queued: the pause is over, and the held write sends
        # its frame. Said downstream only (the ledger observes it there); the processors
        # above have had their EndFrame.
        if not self._playing.is_set():
            await self.set_playout_paused(False, upstream=False)
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._drop_pause()
        await super().cancel(frame)
