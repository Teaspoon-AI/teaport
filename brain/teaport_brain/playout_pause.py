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
# playout model and the start strategy key off it (barge_pause.py, BOOKKEEPING).
#
# THE WORDS' CLOCK. pipecat releases every timed frame -- each word's TTSTextFrame and
# AggregatedTextProgressFrame, a reply's LLMFullResponseEndFrame -- from MediaSender's
# clock task at its presentation time, through this transport's push_frame, on the wall
# clock: it knows nothing of a pause. Unchecked, the words of a paused reply are
# released while nobody hears them, and every later word of it is released the length
# of the pause before its audio plays. Whatever reads them as "played" downstream --
# the room's echo guard (its windows are 2-3 s), the Talk view's captions -- is then
# wrong by that much. So the transport keeps them on the voice's time, here, once:
# while paused it holds them, and after the resume it releases each word of a reply
# whose audio was still to play that much later (_shift_ns, by context). A reply
# whose audio comes after the resume needs none: the TTS anchors it on its moved clock
# (engine_tts._freeze_playout). How much later is the pause less its lag: what was sent
# before it played on that long. Both -- which replies, and how much -- are decided here
# only, and ride the resume's PlayoutPauseFrame (shifted, shift_secs): the ledger moves
# its pts cut by them rather than working them out again. A timed frame with no context
# (a reply's End) gets no shift of its own: the clock releases in pts order, so it comes
# after the shifted words before it.
#
import asyncio
import time
from collections import OrderedDict, deque

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    SystemFrame,
    InterruptionFrame,
    MixerControlFrame,
    OutputAudioRawFrame,
    OutputImageRawFrame,
    SpriteFrame,
    TTSAudioRawFrame,
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
# Timed frames handed to MediaSender's clock and not back yet, kept at most: one that
# never comes back (routed elsewhere) cannot grow the set without bound.
_MAX_TIMED = 4096
# The audio queue's entry for audio that is no reply (the thinking sound): it keeps the
# queue in step with what is written, and is never shifted.
_NOT_A_REPLY = object()
# A timed frame that has no context_id field (a reply's End).
_NO_CONTEXT = object()
# What never reaches MediaSender's clock queue, whatever its pts: a SystemFrame
# (BaseOutputTransport.process_frame pushes it at once), and what _handle_frame routes
# elsewhere. Every other downstream frame with a pts is timed. Pinned against pipecat by
# test_pipecat_premises (check_timed_frames_come_back_through_the_transports_push).
_UNTIMED = (SystemFrame, InterruptionFrame, OutputAudioRawFrame, OutputImageRawFrame,
            SpriteFrame, MixerControlFrame, TTSStoppedFrame)


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
        # Its inverse, for a word waiting out its shift: set while paused.
        self._pausing = asyncio.Event()
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
        # THE WORDS' CLOCK (module header): the timed frames handed to MediaSender's
        # clock, by id; contexts whose audio came here (None: untagged audio); how much
        # later each one's words are released (ns); when the pause stopped the audio, on
        # the pipeline clock (the pause plus its lag).
        self._timed_ids: OrderedDict = OrderedDict()   # frame id -> its context
        # The audio handed here and not written yet, as [context, seconds] in order
        # (written FIFO), and the context of the audio written last -- still playing
        # its lag at a pause.
        self._queued: deque = deque()
        self._last_written = _NOT_A_REPLY
        self._paused_ctxs: tuple = ()
        self._shift_ns: dict = {}
        self._pause_clock_t0 = None

    async def set_playout_paused(self, paused: bool, *, upstream: bool = True):
        """Stop writing the bot's audio, or start again, at the next frame boundary, and
        say so both ways (PlayoutPauseFrame) for the ledger, the TTS and the start
        strategy -- downstream only, not `upstream`, at the end of the session (the
        processors above have had their EndFrame)."""
        if paused == (not self._playing.is_set()):
            return
        lag, shift, shifted = 0.0, 0.0, ()
        if paused:
            self._playing.clear()
            self._pausing.set()
            self._paused_at = time.monotonic()
            # What was sent before the pause still plays out: the listener stops hearing
            # the reply that much later (the frame playing and the one queued behind it).
            lag = max(0.0, self._heard_until - self._paused_at)
            self._pause_lag = lag
            # The replies this pause stops: what is still to be written, and what was
            # written and still plays its lag. One that played out before is not moved.
            ctxs = [c for c, _ in self._queued]
            if lag > 0:
                ctxs.append(self._last_written)
            self._paused_ctxs = tuple(dict.fromkeys(
                c for c in ctxs if c is not _NOT_A_REPLY))
            self._pause_clock_t0 = self.get_clock().get_time() + round(lag * 1e9)
        else:
            self._playing.set()
            self._pausing.clear()
            self._paused_at = None
            self._next_send_time = 0.0
            shift, shifted = self._shift_words()
        tail = self.playout_tail_secs() if paused else None
        await self._announce_pause(lag=lag, tail=-1.0 if tail is None else tail,
                                   paused=paused, shift=shift, shifted=shifted,
                                   upstream=upstream)

    def _shift_words(self) -> tuple:
        """At a resume: every reply whose audio was here and still to play -- at the
        pause, or handed during it -- plays the pause's length less its lag later than
        scheduled. Returns (seconds, those contexts)."""
        if self._pause_clock_t0 is None:
            return 0.0, ()
        held = max(0, self.get_clock().get_time() - self._pause_clock_t0)
        self._pause_clock_t0 = None
        ctxs = tuple(dict.fromkeys(self._paused_ctxs + tuple(
            c for c, _ in self._queued if c is not _NOT_A_REPLY)))
        self._paused_ctxs = ()
        for ctx in ctxs:
            self._shift_ns[ctx] = self._shift_ns.get(ctx, 0) + held
        self._prune_shifts()
        return held / 1e9, ctxs

    def _prune_shifts(self):
        """Forget the shift of a reply with no audio left to play and no word left to
        release: nothing of it can be moved any more."""
        live = {c for c, _ in self._queued} | set(self._timed_ids.values())
        if self._paused_at is not None:
            live |= set(self._paused_ctxs)
        for ctx in [c for c in self._shift_ns if c not in live]:
            del self._shift_ns[ctx]

    async def _announce_pause(self, *, lag: float, tail: float, paused: bool = True,
                              shift: float = 0.0, shifted: tuple = (),
                              upstream: bool = True):
        fields = dict(paused=paused, lag_secs=lag, tail_secs=tail, shift_secs=shift,
                      shifted=shifted)
        if upstream:
            await self.broadcast_frame(PlayoutPauseFrame, **fields)
        else:
            await self.push_frame(PlayoutPauseFrame(**fields))

    async def _wait_if_paused(self) -> bool:
        """Hold a write while playout is paused. True: write on. False: an interruption
        dropped the pause under it, and what it holds is the cut reply's, not to be
        written."""
        if self._playing.is_set():
            return True
        epoch = self._pause_epoch
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PAUSE_GUARD_S
        # Until it is really playing: a resume and a new pause in the same turn of the
        # loop wake this waiter with playout paused again.
        while not self._playing.is_set():
            try:
                await asyncio.wait_for(self._playing.wait(),
                                       timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                logger.warning(f"{self}: playout pause still held after "
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
        self._pausing.clear()
        self._paused_at = None
        self._pause_clock_t0 = None

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
        left = secs
        while self._queued and left > 1e-9:
            entry = self._queued[0]
            self._last_written = entry[0]
            taken = min(entry[1], left)
            entry[1] -= taken
            left -= taken
            if entry[1] <= 1e-9:
                self._queued.popleft()

    def _track_tail(self, frame: Frame, direction: FrameDirection):
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, OutputAudioRawFrame) and frame.sample_rate:
            secs = frame.num_frames / frame.sample_rate
            self._unplayed_s += secs
            # A reply's audio: its context, or None for an untagged TTS (the ledger's
            # legacy turns). The thinking sound's plain audio has no words to move.
            ctx = frame.context_id if isinstance(frame, TTSAudioRawFrame) else _NOT_A_REPLY
            if self._queued and self._queued[-1][0] is ctx:
                self._queued[-1][1] += secs
            else:
                self._queued.append([ctx, secs])
        elif isinstance(frame, TTSStartedFrame):
            self._synth_done = False
        elif isinstance(frame, TTSStoppedFrame):
            self._synth_done = True
        if frame.pts and not isinstance(frame, _UNTIMED):
            self._timed_ids[frame.id] = getattr(frame, "context_id", None)
            while len(self._timed_ids) > _MAX_TIMED:
                self._timed_ids.popitem(last=False)

    async def push_frame(self, frame: Frame,
                         direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if direction == FrameDirection.DOWNSTREAM and frame.id in self._timed_ids:
            # Released by MediaSender's clock (the clock task awaits this, so a word
            # held here holds the ones behind it, in their order).
            self._timed_ids.pop(frame.id, None)
            await self._on_the_voices_time(frame)
            if self._shift_ns and self._paused_at is None:
                self._prune_shifts()
        await super().push_frame(frame, direction)

    async def _on_the_voices_time(self, frame: Frame):
        """Hold a word while playout is paused, then until its shifted time."""
        # A frame of no context at all (a reply's End: no context_id field) is not
        # shifted; a word of untagged audio (context_id None) is, as that audio was.
        ctx = getattr(frame, "context_id", _NO_CONTEXT)
        while True:
            if not self._playing.is_set():
                await self._wait_if_paused()
                continue
            shift = 0 if ctx is _NO_CONTEXT else self._shift_ns.get(ctx, 0)
            wait = (frame.pts + shift - self.get_clock().get_time()) / 1e9
            if wait <= 0:
                return
            try:
                # Until its time -- or a new pause, which holds it again.
                await asyncio.wait_for(self._pausing.wait(), timeout=wait)
            except asyncio.TimeoutError:
                return

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
            # The barge-in cancelled the reply (MediaSender has dropped its queued audio
            # and its clock's words): nothing to resume, and the next reply must not wait
            # on this pause, nor its words on the cut reply's shift.
            self._drop_pause()
            self._unplayed_s, self._synth_done = 0.0, False
            # The client is told to clear what it holds ("clear" on /talk).
            self._heard_until = 0.0
            self._timed_ids.clear()
            self._queued.clear()
            self._last_written = _NOT_A_REPLY
            self._paused_ctxs = ()
            self._shift_ns.clear()

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
