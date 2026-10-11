#
# Unit test: the XVF3800's echo suppression set by how far the user's voice stands above
# the bot's echo at the mic (teaport_brain.xvf_echo), and the bridge's taps for it
# (local_audio: the playback level as heard, the card opening).
#
# The chip: its AGC off at a fixed gain, once per chip; the suppressor off, or its own
# values as read from it (factory if found off); each set written whole and read back,
# not twice; a replugged chip written again; ECHO_CONTROL off or no chip: no USB at all;
# a failing chip logged once, never raising.
#
# The meter: a user near the box stays heard over the bot; one whose voice is barely
# above the echo turns the suppressor on; talking over the bot does not count as echo;
# a volume change moves the verdict; it does not flap; while the suppressor is on it
# does not measure the suppressor as the echo; no verdict without enough to go on.
#
# The device is a fake with the PP registers behind the same control-transfer call the
# usbfs transport makes (resource 17, command | 0x80 to read, the status byte). The live
# check is in the PR.
#
# Run: python test_xvf_echo.py
#
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
        if self.meter.feed(mic, ref, quiet):
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

def test_the_playback_level_is_given_as_it_is_heard_and_before_the_volume():
    clock = Clock()
    written = []
    play = local_audio.Playback(written.append, clock=clock, gain=lambda: 0.01)
    tone = (np.sin(np.arange(24000) / 10) * 10000).astype("<i2").tobytes()  # 1 s at 24 kHz
    play.push(tone, voiced=True)
    play.pump()                                # LEAD_SECS handed to the card
    assert play.heard_due(clock.t - 0.001) is None
    clock.t += 0.06
    got = play.heard_due(clock.t)
    assert got is not None and -20 < got < -6, got   # the tone's own level, not -40 dB less


def test_volume_gain_in_db():
    v = local_audio.Volume(os.devnull + ".none")
    v.percent = 100
    assert v.gain_db == 0
    v.percent = 50
    assert v.gain_db == -20
    v.percent = 0
    assert v.gain_db == -math.inf


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
