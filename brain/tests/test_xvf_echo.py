#
# Unit test: the XVF3800's echo suppression set by how far the user's voice stands above
# the bot's echo at the mic (teaport_brain.xvf_echo), and the bridge's taps for it
# (local_audio: the playback level as heard and as a caption re-marks it, the capture
# clock, Card._meter, which channel gets it). The card opening and the conversation
# flag, which need the fake card processes, are in test_local_audio.py.
#
# The chip: its AGC off at a fixed gain; the suppressor off, or its own values as read
# from it (factory if found off); each set written whole and read back, not twice; a
# replugged chip written again, and one reset under the same node found out when it is
# read back (the card opening, the periodic check); ECHO_CONTROL off or no chip: no USB
# at all; a failing chip logged once, never raising.
#
# The meter: a user near the box stays heard over the bot; one whose voice is barely
# above the echo turns the suppressor on; talking over the bot does not count as echo;
# a volume change moves the verdict; it does not flap; while the suppressor is on, or
# the chip does not hold it off, or for audio captured before it let go, it does not
# measure the suppressor as the echo; no verdict without enough to go on.
#
# The device is a fake with the PP registers behind the same control-transfer call the
# usbfs transport makes (resource 17, command | 0x80 to read, the status byte). The live
# check is in the PR.
#
# Run: python test_xvf_echo.py
#
import asyncio
import math
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
from loguru import logger  # noqa: E402

from teaport_brain import local_audio, xvf_echo, xvf_led  # noqa: E402

E = xvf_echo
FLOATS = {E.PP_AGCGAIN}


class FakePP:
    """The XVF3800's PP registers behind control(). `retries` reads answer "retry"
    first; `stuck` ignores writes (a chip that will not take them)."""

    def __init__(self, retries=0, stuck=False, fail=None, own=E.FACTORY):
        self.regs = {cmd: struct.pack("<i", v) for cmd, v in own}
        self.regs[E.PP_AGCONOFF] = struct.pack("<i", 1)
        self.regs[E.PP_AGCGAIN] = struct.pack("<f", 3.2)
        self.retries, self.stuck, self.fail = retries, stuck, fail
        self.node, self.devnum = "/dev/bus/usb/001/004", 4
        self.writes, self.opened = [], 0

    def replug(self):
        self.__init__()
        self.devnum += 1
        self.node = f"/dev/bus/usb/001/{self.devnum:03d}"

    def find(self):
        return self.node

    def open(self, node):
        assert node == self.node, (node, self.node)
        if self.fail is not None:
            raise self.fail
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def control(self, request_type, value, index, data):
        assert index == E.PP_RESID, index
        if request_type == xvf_led.VENDOR_OUT:
            self.writes.append((value, struct.unpack("<f" if value in FLOATS else "<i", data)[0]))
            if not self.stuck:
                self.regs[value] = data
            return b""
        assert request_type == xvf_led.VENDOR_IN and value & 0x80, (request_type, value)
        if self.retries:
            self.retries -= 1
            return bytes([xvf_led.STATUS_RETRY]) + b"\0" * (data - 1)
        out = bytes([xvf_led.STATUS_OK]) + self.regs[value & 0x7F]
        assert len(out) == data, (value, len(out), data)
        return out

    def i32(self, cmd):
        return struct.unpack("<i", self.regs[cmd])[0]

    def state(self):
        return tuple((cmd, self.i32(cmd)) for cmd, _ in E.FACTORY)

    def agc(self):
        return self.i32(E.PP_AGCONOFF), round(struct.unpack("<f", self.regs[E.PP_AGCGAIN])[0], 3)


def _echo(dev, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("gain_db", 16.0)
    kw.setdefault("clock", lambda: 0.0)
    return E.EchoSuppression(find=dev.find, opener=dev.open, **kw)


class _Warnings:
    def __enter__(self):
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level="WARNING")
        return self.lines

    def __exit__(self, *exc):
        logger.remove(self.sink)


# ---------------------------------------------------------------- the chip

def test_the_agc_goes_to_a_fixed_gain_and_the_suppressor_off():
    dev = FakePP()
    assert _echo(dev).apply(True)
    assert dev.agc() == (0, round(10 ** (16 / 20), 3))
    assert dev.state() == E.DUPLEX


def test_the_measured_settings():
    assert dict(E.DUPLEX) == {E.PP_ECHOONOFF: 0, E.PP_NLATTENONOFF: 0, E.PP_DTSENSITIVE: 5}
    assert dict(E.FACTORY) == {E.PP_ECHOONOFF: 1, E.PP_NLATTENONOFF: 1, E.PP_DTSENSITIVE: 0}


def test_on_means_the_chips_own_saved_values():
    tuned = ((E.PP_ECHOONOFF, 1), (E.PP_NLATTENONOFF, 1), (E.PP_DTSENSITIVE, 3))
    dev = FakePP(own=tuned)                    # someone saved their own tuning to it
    echo = _echo(dev)
    assert echo.apply(False) and dev.state() == tuned
    assert all(cmd in (E.PP_AGCONOFF, E.PP_AGCGAIN) for cmd, _ in dev.writes)  # theirs kept
    assert echo.apply(True) and dev.state() == E.DUPLEX
    assert echo.apply(False) and dev.state() == tuned


def test_a_chip_left_with_the_suppressor_off_gets_the_factory_values_back():
    dev = FakePP(own=E.DUPLEX)                 # a bridge that stopped with it off
    assert _echo(dev).apply(False) and dev.state() == E.FACTORY


def test_switched_off_or_no_chip_means_no_usb_at_all():
    dev = FakePP()
    echo = _echo(dev, enabled=False)
    assert echo.apply(True) and echo.apply(False)
    assert dev.opened == 0 and dev.writes == []
    dev = FakePP()
    dev.node = None
    assert _echo(dev).apply(True)
    assert dev.opened == 0 and dev.writes == []


def test_the_same_setting_is_not_written_twice():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply(True)
    n = len(dev.writes)
    assert echo.apply(True) and echo.apply(lambda: True)
    assert len(dev.writes) == n


def test_the_verdict_is_read_when_the_write_runs():
    dev = FakePP()
    echo = _echo(dev)
    verdict = [True]
    queued = [lambda: verdict[0]] * 2          # two switches queued...
    verdict[0] = False                         # ...and the meter says "on" by the time
    for get in queued:                         # they run
        echo.apply(get)
    assert dev.state() == E.FACTORY


def test_a_replugged_chip_is_written_again():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply(True)
    dev.replug()                               # its own settings, AGC on, new node
    assert echo.apply(True)
    assert dev.state() == E.DUPLEX and dev.agc()[0] == 0


def test_a_chip_reset_under_the_same_node_is_found_out_when_the_card_opens():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(True)
    node = dev.node
    dev.replug()                               # its saved settings back, AGC on...
    dev.node = node                            # ...without re-enumerating
    assert echo.apply(True) and dev.state() == E.FACTORY   # the cache alone misses it
    assert echo.holds(True)
    assert echo.apply(True, verify=True)       # the card opening reads it back
    assert dev.state() == E.DUPLEX and dev.agc() == (0, round(10 ** (16 / 20), 3))
    n = len(dev.writes)
    assert echo.apply(True, verify=True)       # read back as it should be: no writes
    assert len(dev.writes) == n


def test_a_read_that_says_retry_is_asked_again():
    dev = FakePP(retries=2)
    assert _echo(dev).apply(True) and dev.state() == E.DUPLEX


def test_a_chip_that_does_not_take_the_write_is_a_failure_and_tried_again():
    dev = FakePP(stuck=True)
    echo = _echo(dev)
    with _Warnings() as lines:
        assert not echo.apply(True)
        assert not echo.apply(True)            # tried again, not logged again
    assert len(lines) == 1 and "read back" in lines[0], lines
    dev.stuck = False
    assert echo.apply(True) and dev.state() == E.DUPLEX and dev.agc()[0] == 0


def test_an_unusable_chip_never_raises_and_says_why_once():
    dev = FakePP(fail=PermissionError(13, "Permission denied"))
    echo = _echo(dev)
    with _Warnings() as lines:
        assert not echo.apply(True)
        assert not echo.apply(False)
    assert len(lines) == 1 and "udev rule" in lines[0], lines


# ---------------------------------------------------------------- the meter

class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class Room:
    """A room around an EchoMeter, in 20 ms chunks: the bot talks (playback at `ref`
    dBFS as synthesized, echoed at `coupling` dB through the volume), the user talks (at
    `voice` dBFS at the mic), or nothing (the floor). Levels jitter a few dB, as speech
    does."""

    def __init__(self, coupling=-10.0, voice=-12.0, ref=-20.0, floor=-50.0, gain_db=0.0,
                 margin=15.0):
        self.coupling, self.voice, self.ref, self.floor = coupling, voice, ref, floor
        self.gain_db = gain_db
        self.clock = Clock()
        self.meter = E.EchoMeter(gain_db=lambda: self.gain_db, margin_db=margin,
                                 clock=self.clock)
        self.rng = random.Random(1)
        self.changes = []
        self.suppressed = False  # the chip's own suppression on: the echo mostly gone

    def _j(self, db):
        return db + self.rng.uniform(-4, 4)

    def _feed(self, mic, ref, quiet):
        if self.meter.feed(mic, ref, quiet, suppressor_off=not self.suppressed):
            self.changes.append((round(self.clock.t, 1), self.meter.duplex))
            self.suppressed = not self.meter.duplex
        self.clock.t += 0.02

    def bot(self, secs, user_over=0.0):
        """The bot talks; the user talks over it for `user_over` of the time."""
        for i in range(int(secs * 50)):
            ref = self._j(self.ref)
            echo = ref + self.gain_db + self.coupling - (25 if self.suppressed else 0)
            mic = echo
            if (i // 25) % 10 < user_over * 10:  # that share of every 5 s
                mic = 10 * math.log10(10 ** (echo / 10) + 10 ** (self._j(self.voice) / 10))
            self._feed(mic, ref, False)

    def user(self, secs):
        for _ in range(int(secs * 50)):
            self._feed(self._j(self.voice), None, True)

    def quiet(self, secs):
        for _ in range(int(secs * 50)):
            self._feed(self._j(self.floor), None, True)

    def conversation(self, rounds=6, user_over=0.0):
        for _ in range(rounds):
            self.quiet(1)
            self.user(2)
            self.quiet(0.5)
            self.bot(5, user_over)


def test_a_user_well_above_the_echo_is_heard_over_the_bot():
    room = Room(coupling=-10, voice=-12)       # echo ~ -26 at its loud: ~14+ dB down...
    room.coupling = -14                        # ...here ~18 dB down: the near take
    room.conversation()
    assert room.meter.gap is not None and room.meter.gap > 15, room.meter.gap
    assert room.meter.duplex and room.changes == []


def test_a_voice_barely_above_the_echo_turns_the_suppressor_on():
    room = Room(coupling=0, voice=-12)         # echo as loud as the voice
    room.conversation()
    assert room.meter.gap < 15, room.meter.gap
    assert not room.meter.duplex and room.changes[0][1] is False


def test_talking_over_the_bot_is_not_counted_as_echo():
    quiet = Room(coupling=-14, voice=-12)
    quiet.conversation()
    busy = Room(coupling=-14, voice=-12)
    busy.conversation(user_over=0.4)           # the user talks over 40 % of the bot
    assert busy.meter.duplex and busy.changes == []
    assert abs(busy.meter.gap - quiet.meter.gap) < 6, (busy.meter.gap, quiet.meter.gap)


def test_turning_the_volume_down_moves_the_verdict_at_once():
    room = Room(coupling=0, voice=-12)
    room.conversation()
    assert not room.meter.duplex
    room.gain_db = -20.0                       # a volume 50 points lower
    room.clock.t += E.DWELL_SECS
    room.conversation(rounds=1)
    assert room.meter.duplex, (room.meter.gap, room.changes)


def test_the_suppressor_on_is_not_mistaken_for_a_quiet_echo():
    room = Room(coupling=0, voice=-12)
    room.conversation(rounds=12)               # on, and the chip now hides the echo
    assert not room.meter.duplex
    assert [d for _, d in room.changes] == [False], room.changes
    room.clock.t += E.RECHECK_SECS             # the recheck finds the echo still there
    room.conversation(rounds=6)
    assert not room.meter.duplex, room.changes


def test_a_suppressor_held_on_is_rechecked():
    room = Room(coupling=-14, voice=-12)
    room.conversation(user_over=0.8)           # the user talks over nearly all of it
    assert not room.meter.duplex               # thrown: on, though the echo is low
    room.clock.t += E.RECHECK_SECS
    room.conversation(rounds=8)                # a normal conversation: measured again
    assert room.meter.duplex, (room.meter.gap, room.changes)
    assert any(d for _, d in room.changes[1:])


def test_it_does_not_flap():
    room = Room(coupling=-12, voice=-12, margin=15)   # right at the margin
    room.conversation(rounds=20)
    times = [t for t, _ in room.changes]
    assert all(b - a >= E.DWELL_SECS for a, b in zip(times, times[1:])), room.changes
    assert len(room.changes) <= 2, room.changes


def test_no_verdict_without_enough_to_go_on():
    room = Room(coupling=0, voice=-12)
    room.bot(3)                                # the bot only: no voice heard yet
    room.quiet(1)
    assert room.meter.gap is None and room.meter.duplex


def test_a_silent_speaker_decides_nothing():
    room = Room(coupling=0, voice=-12)
    room.gain_db = -math.inf                   # volume 0
    room.conversation()
    assert room.meter.duplex and room.changes == []


# ---------------------------------------------------------------- the bridge's taps

def _tone(secs=1.0):
    return (np.sin(np.arange(int(24000 * secs)) / 10) * 10000).astype("<i2").tobytes()


def test_the_playback_level_is_looked_up_by_when_it_was_heard_and_before_the_volume():
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock, gain=lambda: 0.01)
    play.push(_tone(), voiced=True)
    play.pump()                                # LEAD_SECS handed to the card, from t=0
    db, voiced = play.heard_between(0.0, 0.02)
    assert voiced and -20 < db < -6, db        # the tone's own level, not 40 dB less
    assert play.heard_between(1.0, 1.02) == (None, False)   # nothing played then


def test_the_thinking_sound_is_not_the_bot_talking():
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    play.push(_tone(0.5), voiced=False)        # uncaptioned audio: the typing bed
    play.pump()
    db, voiced = play.heard_between(0.0, 0.1)
    assert db is not None and not voiced


def test_a_caption_vouches_for_the_playback_levels_already_handed_over():
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    play.push(_tone(0.5), voiced=False)        # pumped before its caption arrived
    play.pump()
    assert play.heard_between(0.0, 0.1)[1] is False
    play.mark_voiced()                         # the caption: it was the reply
    db, voiced = play.heard_between(0.0, 0.1)
    assert voiced and db is not None


def test_playback_levels_are_not_kept_for_ever():
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    for _ in range(500):                       # 10 s of silence fed, nobody asking
        clock.t += 0.02
        play.pump()
    assert len(play._heard) <= local_audio.HEARD_KEEP_SECS * 50 + 10


class _CardLike:
    """What Card._meter uses of a Card."""
    _meter = local_audio.Card._meter
    _log_gap = local_audio.Card._log_gap
    _held = local_audio.Card._held

    def __init__(self, play, meter, echo=None):
        self.play, self.meter, self.echo = play, meter, echo
        self._cap_clock, self._echo_check = local_audio.CaptureClock(), 0.0
        self._gap_logged, self._gap_log_at = None, 0.0
        self.conversing = True


class _Holds:
    """An echo suppression whose chip holds `held` (True: the suppressor off), since
    for ever."""
    shown_at, resets = -math.inf, 0

    def __init__(self, held=True):
        self.held = held

    def holds(self, duplex):
        return duplex == self.held

    def apply(self, duplex, verify=False):
        return True


def test_a_burst_of_capture_is_placed_by_when_it_was_captured():
    # arecord hands over ~125 ms at once: the chunks of a burst were captured one after
    # another, not all in the instant they were read.
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    seen = []

    class Meter:
        duplex, gap = True, None
        resets_seen = 0

        def feed(self, mic, ref, quiet, conversing, suppressor_off):
            seen.append(ref)
            return False
    card = _CardLike(play, Meter())
    real = local_audio.time.monotonic
    local_audio.time.monotonic = clock
    try:
        chunk = np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2")
        clock.t = 0.125                        # the first burst lands
        for _ in range(6):
            card._meter(chunk)
        play.push(_tone(0.08), voiced=True)    # the bot starts at 0.125...
        play.pump()
        clock.t = 0.25                         # ...and the next burst arrives
        for _ in range(6):
            card._meter(chunk)
    finally:
        local_audio.time.monotonic = real
    # The first burst's chunks were captured before the bot spoke; of the second, the
    # ones captured while the tone played see it, and the rest see silence again.
    assert all(r is None for r in seen[:6]), seen
    heard = [i for i, r in enumerate(seen) if r is not None]
    assert heard and heard[0] == 6 and 2 <= len(heard) <= 4, seen
    assert heard == list(range(6, 6 + len(heard))) and seen[-1] is None, seen


def test_the_capture_clock_recovers_from_lost_frames():
    # Frames come in 125 ms bursts, read up to 30 ms late; then the loop stalls for a
    # second and arecord's overrun loses 0.8 s of them. Every frame after that lands
    # 0.8 s later than the frame count says; the clock follows within its window.
    rate, burst = local_audio.DEVICE_RATE, local_audio.DEVICE_RATE // 8
    clock = local_audio.CaptureClock()
    rng = random.Random(3)
    captured = 100.0                           # when the frame just read was captured
    errors = []
    for i in range(400):                       # 50 s
        if i == 200:
            captured += 0.8                    # lost: never read
        captured += burst / rate
        late = 0.002 + rng.uniform(0, 0.03)
        if i == 200:
            late += 1.0                        # the stall that lost them
        errors.append(clock.read(burst, captured + late) - captured)
    before, after = errors[20:200], errors[200:]
    assert all(0 <= e < 0.01 for e in before), max(before)
    # Right after the loss the old placement holds (0.8 s early) for at most the window...
    assert min(after) > -0.81
    settled = int(local_audio.CAPTURE_CLOCK_SECS * 8) + 1
    assert all(0 <= e < 0.01 for e in after[settled:]), (settled, after[:settled + 3])


def test_the_capture_clock_follows_a_card_clock_that_drifts():
    rate, burst = local_audio.DEVICE_RATE, local_audio.DEVICE_RATE // 8
    clock = local_audio.CaptureClock()
    captured, errors = 0.0, []
    for i in range(8 * 600):                   # ten minutes, the card 100 ppm slow
        captured += burst / rate * 1.0001
        errors.append(clock.read(burst, captured + 0.002 + (i % 5) * 0.006) - captured)
    assert all(abs(e) < 0.01 for e in errors[100:]), max(map(abs, errors[100:]))


def test_a_verdict_that_holds_still_logs_how_far_the_voice_was_above_the_echo():
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)

    class Meter:
        duplex, margin, gap = True, 15.0, None
        resets_seen = 0

        def feed(self, mic, ref, quiet, conversing, suppressor_off):
            return False
    meter = Meter()
    card = _CardLike(play, meter, _Holds())
    card._echo_check = 1e9                     # the periodic check is another test's
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    real = local_audio.time.monotonic
    local_audio.time.monotonic = clock
    chunk = np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2")
    try:
        card._meter(chunk)                           # nothing measured yet: nothing said
        for clock.t, meter.gap in ((1, 24.0), (2, 25.0), (3, 30.0), (40, 30.0), (41, 34.0)):
            card._meter(chunk)
    finally:
        local_audio.time.monotonic = real
        logger.remove(sink)
    said = [ln for ln in lines if "mic echo" in ln]
    # The first measure; a 1 dB move is nothing; 6 dB inside 30 s waits, then shows.
    assert len(said) == 2 and "24.0 dB" in said[0] and "still off" in said[0], said
    assert "30.0 dB" in said[1], said
    # A chip that does not hold the suppressor off is not said to.
    card.echo.held = False
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    local_audio.time.monotonic = clock
    try:
        clock.t, meter.gap = 80, 20.0
        card._meter(chunk)
    finally:
        local_audio.time.monotonic = real
        logger.remove(sink)
    said = [ln for ln in lines if "mic echo" in ln]
    assert len(said) == 3 and "20.0 dB" in said[2] and "still" not in said[2], said
    assert "does not hold" in said[2], said


async def _bot_talks_to(card, clock, secs=2.0):
    """The bot's tone played into `card`'s meter for `secs`, in 20 ms chunks of an
    echo at the mic, captured in step."""
    card.play.push(_tone(secs + 0.5), voiced=True)
    chunk = (np.ones(local_audio.CHUNK_FRAMES) * 3000).astype("<i2")
    real = local_audio.time.monotonic
    local_audio.time.monotonic = clock
    try:
        for _ in range(int(secs * 50)):
            card.play.pump()
            clock.t += 0.02
            card._meter(chunk)
    finally:
        local_audio.time.monotonic = real


async def _echo_measured(echo):
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    meter = E.EchoMeter(clock=clock)
    card = _CardLike(play, meter, echo)
    card._echo_check = 1e9                     # no retry: the chip as it is
    await _bot_talks_to(card, clock)
    return len(meter.coupling)


def test_the_echo_is_measured_only_while_the_chip_holds_the_suppressor_off():
    held = _echo(FakePP())
    assert held.apply(True)
    assert asyncio.run(_echo_measured(held)) > 0
    # The verdict is "off" (the meter's start), but the chip never took it: what the
    # mic hears is the suppressor's work, not the echo.
    stuck = _echo(FakePP(stuck=True))
    with _Warnings():
        assert not stuck.apply(True)
    assert asyncio.run(_echo_measured(stuck)) == 0
    assert asyncio.run(_echo_measured(None)) == 0          # no chip: nothing to decide
    assert asyncio.run(_echo_measured(_Holds(held=False))) == 0   # it holds it on


class _LateCapture:
    """A capture clock whose chunks reach the meter `lag` after they were captured."""

    def __init__(self, lag):
        self.lag = lag

    def read(self, frames, now):
        return now - self.lag


async def _measured_across_a_switch():
    # The chip held the suppressor on; at t=1 the meter's recheck turns it off. The chunks
    # read just after were captured before (arecord's period, the pipe): still the
    # suppressor's work, not the echo.
    clock = Clock()
    dev = FakePP()
    echo = _echo(dev, clock=clock)
    assert echo.apply(False)
    play = local_audio.Playback(lambda b: None, clock=clock)
    meter = E.EchoMeter(clock=clock)
    card = _CardLike(play, meter, echo)
    card._echo_check = 1e9
    card._cap_clock = _LateCapture(0.125)
    play.push(_tone(4.0), voiced=True)
    chunk = (np.ones(local_audio.CHUNK_FRAMES) * 3000).astype("<i2")
    real = local_audio.time.monotonic
    local_audio.time.monotonic = clock
    counts = {}
    try:
        while clock.t < 3.0:
            if abs(clock.t - 1.0) < 1e-9:
                assert echo.apply(True)        # lands at t=1
            play.pump()
            clock.t = round(clock.t + 0.02, 6)
            card._meter(chunk)
            counts[round(clock.t, 2)] = len(meter.coupling)
    finally:
        local_audio.time.monotonic = real
    return counts


def test_audio_captured_before_the_suppressor_let_go_is_not_the_echo():
    counts = asyncio.run(_measured_across_a_switch())
    # Every chunk read by then was captured before the switch (0.125 s late, 20 ms
    # long): nothing of them measured...
    before = 1.0 + 0.125 + 0.02
    assert all(n == 0 for t, n in counts.items() if t <= before), counts
    # ...and the bare echo after.
    assert counts[3.0] > 0, counts


def test_the_suppressor_is_given_time_to_let_go():
    # Each chunk's capture start against whether it counted as the bare echo, around a
    # switch to the suppressor off at t=1: the chip lets go of the echo a moment after
    # it reads back off, so a chunk captured just after is not the bare echo yet.
    clock = Clock()
    echo = _echo(FakePP(), clock=clock)
    assert echo.apply(False)
    play = local_audio.Playback(lambda b: None, clock=clock)
    seen = []

    class Meter:
        duplex, gap, coupling = True, None, []
        resets_seen = 0

        def feed(self, mic, ref, quiet, conversing, suppressor_off):
            seen.append((start[0], suppressor_off))
            return False
    card = _CardLike(play, Meter(), echo)
    card._echo_check = 1e9
    card._cap_clock = _LateCapture(0.0)
    chunk = np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2")
    start = [0.0]
    real = local_audio.time.monotonic
    local_audio.time.monotonic = clock
    try:
        for i in range(90, 140):               # chunks captured from t=0.88 on, 10 ms apart
            clock.t = i / 100
            if i == 100:
                assert echo.apply(True)        # reads back off at t=1
            start[0] = round(clock.t - 0.02, 3)
            card._meter(chunk)
    finally:
        local_audio.time.monotonic = real
    assert not any(bare for t, bare in seen if t <= 1.05), seen
    assert all(bare for t, bare in seen if t >= 1.2), seen


def test_the_agc_set_again_restarts_the_bare_echo():
    clock = Clock()
    dev = FakePP()
    echo = _echo(dev, clock=clock)
    assert echo.apply(True)
    dev.regs[E.PP_AGCONOFF] = struct.pack("<i", 1)   # the AGC back on, the suppressor not
    clock.t = 5.0
    assert echo.apply(True, verify=True)
    assert dev.agc()[0] == 0 and echo.shown_at == 5.0
    assert echo.resets == 1


async def _reset_found(meter):
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(True)
    clock = Clock()
    card = _CardLike(local_audio.Playback(lambda b: None, clock=clock), meter, echo)
    card._echo_check = 1e9
    node = dev.node
    dev.replug()                               # reset under the open card
    dev.node = node
    meter.coupling.extend([-30.0] * 60)        # measured since: the suppressor's work
    with _Warnings() as lines:
        assert echo.apply(True, verify=True)   # the periodic check
        assert echo.apply(True, verify=True)   # and the next one: holds again
    card._meter(np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2"))
    return echo, lines


def test_a_reset_found_by_a_check_is_logged_and_the_meter_forgets_its_echo():
    meter = E.EchoMeter(clock=Clock())
    echo, lines = asyncio.run(_reset_found(meter))
    assert echo.resets == 1
    assert len(lines) == 1 and "reset" in lines[0], lines
    assert len(meter.coupling) == 0


def test_a_reset_found_with_the_suppressor_on_still_lets_it_recheck():
    # The echo is dropped whatever the verdict; held on, the meter has none to measure
    # again, and the recheck must not wait for any.
    clock = Clock()
    meter = E.EchoMeter(clock=clock)
    meter.duplex, meter._since = False, 0.0
    asyncio.run(_reset_found(meter))
    assert len(meter.coupling) == 0
    clock.t = E.RECHECK_SECS + 1
    assert meter._block(-60.0, -20.0, False, True, False)
    assert meter.duplex and meter.recheck


def test_a_reset_found_between_two_cards_is_not_missed():
    # The count seen lives with the meter, which outlives the card.
    meter = E.EchoMeter(clock=Clock())
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(True)
    node = dev.node
    dev.replug()                               # reset while no card is open...
    dev.node = node
    assert echo.apply(True, verify=True)       # ...found by the old card's last check
    meter.coupling.extend([-30.0] * 60)
    asyncio.run(_open_card_and_meter(echo, meter))   # a real Card, built after the reset
    assert echo.resets == 1 and len(meter.coupling) == 0


async def _open_card_and_meter(echo, meter):
    card = local_audio.Card(None, volume=object(), echo=echo, meter=meter)
    card.play = local_audio.Playback(lambda b: None, clock=Clock())
    card._echo_check = 1e9
    card._meter(np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2"))


def test_a_failed_check_is_said_to_be_one():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(True)
    dev.fail = OSError(5, "Input/output error")
    with _Warnings() as lines:
        assert not echo.apply(True, verify=True)
    assert len(lines) == 1 and "could not check the chip holds the suppressor off" in lines[0], lines


async def _reset_under_an_open_card():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(True)
    node = dev.node
    dev.replug()                               # its saved settings back, AGC on...
    dev.node = node                            # ...under the same node: the cache holds
    assert echo.holds(True)
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    card = _CardLike(play, E.EchoMeter(clock=clock), echo)
    card._meter(np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2"))   # the periodic check
    for _ in range(50):
        await asyncio.sleep(0.01)
        if dev.state() == E.DUPLEX:
            break
    assert dev.state() == E.DUPLEX and dev.agc()[0] == 0


def test_a_chip_reset_under_an_open_card_is_found_by_the_periodic_check():
    asyncio.run(_reset_under_an_open_card())


def test_the_meter_steers_channel_0_only():
    saved_channel = local_audio.CAPTURE_CHANNEL
    volume = local_audio.Volume(os.devnull + ".none")
    volume.percent = 50
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        local_audio.CAPTURE_CHANNEL = 0
        echo, meter = local_audio.echo_control(volume)
        assert echo is not None and meter is not None
        assert meter._gain_db() == -20.0       # the volume's gain, not a meter's default
        local_audio.CAPTURE_CHANNEL = 1        # the ASR beam: the suppressor never acts on it
        assert local_audio.echo_control(volume) == (None, None)
        assert any("channel 1" in ln and "left as it is" in ln for ln in lines), lines
    finally:
        local_audio.CAPTURE_CHANNEL = saved_channel
        logger.remove(sink)


async def _retry():
    dev = FakePP(fail=OSError("usb gone"))
    echo = _echo(dev)
    assert not echo.apply(True)                # the open's write failed
    dev.fail = None
    clock = Clock()
    play = local_audio.Playback(lambda b: None, clock=clock)
    meter = E.EchoMeter(clock=clock)
    card = _CardLike(play, meter, echo)
    card._meter(np.zeros(local_audio.CHUNK_FRAMES, dtype="<i2"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if echo.holds(True):
            break
    assert echo.holds(True) and dev.state() == E.DUPLEX


def test_a_chip_write_that_did_not_take_is_tried_again():
    asyncio.run(_retry())


def test_a_muted_mic_is_not_measured():
    room = Room(coupling=-14, voice=-12)
    room.conversation()
    gap = room.meter.gap
    for _ in range(500):                       # 10 s of digital silence, bot talking
        room.meter.feed(-180.0, -20.0, False, suppressor_off=True)
    for _ in range(500):                       # and with the bot quiet
        room.meter.feed(-180.0, None, True, suppressor_off=True)
    assert room.meter.gap == gap and -60 < E._pct(room.meter.floor, E.FLOOR_PCT) < -40


def test_a_voice_between_conversations_is_not_the_users():
    room = Room(coupling=-14, voice=-12)
    room.conversation()
    gap = room.meter.gap
    for _ in range(1500):                      # 30 s of a loud TV, nobody conversing
        room.meter.feed(room._j(0.0), None, True, conversing=False, suppressor_off=True)
    assert room.meter.gap == gap


def test_levels_are_averaged_as_power():
    assert abs(E.power_mean_db([0.0, -180.0]) - 10 * math.log10(0.5)) < 1e-6   # -3.01
    assert abs(E.power_mean_db([-20.0] * 4) + 20.0) < 1e-6
    assert abs(E.power_mean_db([-10.0, -20.0]) - 10 * math.log10(0.055)) < 1e-6


def test_volume_gain_in_db():
    v = local_audio.Volume(os.devnull + ".none")
    v.percent = 100
    assert v.gain_db == 0 and v.gain == 1.0
    v.percent = 50
    assert v.gain_db == -20 and abs(v.gain - 0.1) < 1e-12
    v.percent = 0
    assert v.gain_db == -math.inf and v.gain == 0.0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
