#
# Unit test: the XVF3800's mic tuning (teaport_brain.xvf_echo) and the bridge's use of
# it (local_audio.echo_control, Card's periodic check). The card opening, which needs
# the fake card processes, is in test_local_audio.py.
#
# The chip: its AGC off at a fixed gain and its suppressor at DUPLEX (on, non-linear
# attenuation off, double-talk sensitivity 15), the set measured 2026-11-02, whatever it
# was found at (factory, someone's own tuning, an older bridge's suppressor off); the
# suppressor never written off; each set written whole and read back, not written when
# it already holds; a replugged chip written again, and one reset under the same node
# found out and logged when it is read back (the card opening, the periodic check);
# what was set logged once; ECHO_CONTROL off or no chip: no USB at all; a failing chip
# logged once, retried, never raising.
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
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# ---------------------------------------------------------------- the chip

GAIN = round(10 ** (16 / 20), 3)
# The set measured 2026-11-02 (xvf_echo's table), spelled out: the suppressor on, the
# non-linear attenuation off, the double-talk detector at 15.
MEASURED = ((E.PP_ECHOONOFF, 1), (E.PP_NLATTENONOFF, 0), (E.PP_DTSENSITIVE, 15))
# The suppressor fully off: what the bridge wrote before 2026-11-02, and what a chip
# left by such a bridge (or by hand) still holds.
OFF = ((E.PP_ECHOONOFF, 0), (E.PP_NLATTENONOFF, 0), (E.PP_DTSENSITIVE, 5))


class _Lines:
    def __init__(self, level="INFO"):
        self.level = level

    def __enter__(self):
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level=self.level)
        return self.lines

    def __exit__(self, *exc):
        logger.remove(self.sink)


def _reset_in_place(dev):
    """The chip back at its saved values, AGC on, without re-enumerating."""
    node = dev.node
    dev.replug()
    dev.node = node


def test_the_measured_settings():
    assert E.DUPLEX == MEASURED
    assert dict(E.FACTORY) == {E.PP_ECHOONOFF: 1, E.PP_NLATTENONOFF: 1, E.PP_DTSENSITIVE: 0}


def test_the_chip_ends_with_the_measured_set_and_a_fixed_gain_whatever_it_held():
    tuned = ((E.PP_ECHOONOFF, 1), (E.PP_NLATTENONOFF, 1), (E.PP_DTSENSITIVE, 3))
    for own in (E.FACTORY, OFF, tuned, MEASURED):
        dev = FakePP(own=own)
        echo = _echo(dev)
        assert echo.apply()
        assert dev.state() == MEASURED and dev.agc() == (0, GAIN), own
        assert echo.held


def test_the_suppressor_is_never_written_off():
    # Through a first sight, a chip an older bridge left off, a replug, a reset under the
    # same node, and the periodic checks after each.
    dev = FakePP(own=OFF)
    echo = _echo(dev)
    seen = []
    for step in ("first", "check", "replug", "check", "reset", "check"):
        if step == "replug":
            dev.replug()
        elif step == "reset":
            _reset_in_place(dev)
        seen += dev.writes
        dev.writes = []
        assert echo.apply()
        seen += dev.writes
    assert seen and (E.PP_ECHOONOFF, 0) not in seen, seen
    assert all((cmd, v) in MEASURED for cmd, v in seen if cmd not in FLOATS | {E.PP_AGCONOFF}), seen


def test_switched_off_or_no_chip_means_no_usb_at_all():
    dev = FakePP()
    echo = _echo(dev, enabled=False)
    assert echo.apply() and echo.apply()
    assert dev.opened == 0 and dev.writes == []
    dev = FakePP()
    dev.node = None
    assert _echo(dev).apply()
    assert dev.opened == 0 and dev.writes == []


def test_a_chip_that_holds_it_is_only_read():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply()
    n = len(dev.writes)
    assert echo.apply() and echo.apply()
    assert len(dev.writes) == n
    dev = FakePP(own=MEASURED)                 # a bridge restarted: the chip already set...
    dev.regs[E.PP_AGCONOFF] = struct.pack("<i", 0)
    dev.regs[E.PP_AGCGAIN] = struct.pack("<f", 10 ** (16 / 20))
    assert _echo(dev).apply() and dev.writes == []   # ...and left as it is


def test_what_was_set_is_logged_once():
    dev = FakePP()
    echo = _echo(dev)
    with _Lines() as lines:
        for _ in range(3):
            assert echo.apply()
    said = [ln for ln in lines if "lets your voice through" in ln]
    assert len(said) == 1 and "+16 dB" in said[0] and "factory" in said[0], lines


def test_a_replugged_chip_is_written_again():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply()
    dev.replug()                               # its own settings, AGC on, new node
    with _Lines("WARNING") as warnings:
        assert echo.apply()
    assert dev.state() == MEASURED and dev.agc() == (0, GAIN)
    assert warnings == []                      # a new chip, not a reset


def test_a_chip_reset_under_the_same_node_is_set_again_and_logged():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply()
    _reset_in_place(dev)
    with _Lines("WARNING") as lines:
        assert echo.apply()                    # the card opening, or the periodic check
        assert dev.state() == MEASURED and dev.agc() == (0, GAIN)
        n = len(dev.writes)
        assert echo.apply()                    # read back as it should be: no writes
        assert len(dev.writes) == n
    assert len(lines) == 1 and "reset" in lines[0] and "setting it again" in lines[0], lines


def test_the_agc_alone_reset_is_set_again():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply()
    dev.regs[E.PP_AGCONOFF] = struct.pack("<i", 1)   # the AGC back on, the suppressor not
    dev.writes = []
    assert echo.apply()
    assert dev.agc() == (0, GAIN) and dev.state() == MEASURED
    assert {cmd for cmd, _ in dev.writes} == {E.PP_AGCONOFF, E.PP_AGCGAIN}, dev.writes


def test_a_read_that_says_retry_is_asked_again():
    dev = FakePP(retries=2)
    assert _echo(dev).apply() and dev.state() == MEASURED


def test_a_chip_that_does_not_take_the_write_is_logged_once_and_tried_again():
    dev = FakePP(stuck=True)
    echo = _echo(dev)
    with _Lines() as lines:
        assert not echo.apply()
        assert not echo.apply()                # tried again, not logged again
        assert not echo.held
        dev.stuck = False
        assert echo.apply() and dev.state() == MEASURED and dev.agc() == (0, GAIN)
    failed = [ln for ln in lines if "could not" in ln]
    assert len(failed) == 1 and "read back" in failed[0], lines
    assert any("working again" in ln for ln in lines), lines


def test_an_unusable_chip_never_raises_and_says_why_once():
    dev = FakePP(fail=PermissionError(13, "Permission denied"))
    echo = _echo(dev)
    with _Lines("WARNING") as lines:
        assert not echo.apply()
        assert not echo.apply()
    assert len(lines) == 1 and "udev rule" in lines[0], lines


# ---------------------------------------------------------------- the bridge

class Clock:
    t = 0.0

    def __call__(self):
        return self.t


async def _capture_chunks(card, chunks):
    """Card._capture over `chunks` 20 ms chunks of silence from a fake arecord."""
    class Arecord:
        class stdout:
            left = chunks

            @classmethod
            async def readexactly(cls, n):
                if cls.left == 0:
                    raise asyncio.IncompleteReadError(b"", n)
                cls.left -= 1
                return b"\0" * n
    card.play = local_audio.Playback(lambda b: None, clock=Clock())

    async def stderr_of(p):
        return "ended"
    card._stderr_of = stderr_of
    try:
        await card._capture(Arecord, b"")
    except local_audio.CardError:
        pass


async def _until(cond, secs=1.0):
    for _ in range(int(secs * 100)):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def _checked_while_open(dev, echo, due):
    card = local_audio.Card(None, volume=object(), echo=echo)
    card._echo_check = time.monotonic() + (0 if due else 1e9)
    await _capture_chunks(card, 3)
    return await _until(lambda: dev.state() == MEASURED and dev.agc() == (0, GAIN))


def test_a_chip_reset_under_an_open_card_is_set_again_by_the_periodic_check():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply()
    _reset_in_place(dev)
    assert not asyncio.run(_checked_while_open(dev, echo, due=False))   # not yet due
    assert asyncio.run(_checked_while_open(dev, echo, due=True))


def test_a_chip_write_that_failed_is_tried_again_by_the_periodic_check():
    dev = FakePP(fail=OSError("usb gone"))
    echo = _echo(dev)
    assert not echo.apply()                    # the open's write failed
    dev.fail = None
    assert asyncio.run(_checked_while_open(dev, echo, due=True))


def test_the_bridge_tunes_channel_0_only():
    saved_channel = local_audio.CAPTURE_CHANNEL
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        local_audio.CAPTURE_CHANNEL = 0
        assert isinstance(local_audio.echo_control(), E.EchoSuppression)
        local_audio.CAPTURE_CHANNEL = 1        # the ASR beam: the suppressor never acts on it
        assert local_audio.echo_control() is None
        assert any("channel 1" in ln and "left as it is" in ln for ln in lines), lines
    finally:
        local_audio.CAPTURE_CHANNEL = saved_channel
        logger.remove(sink)


def test_echo_control_off_leaves_the_chip_to_itself():
    class Off(E.EchoSuppression):              # LOCAL_AUDIO_ECHO_CONTROL off
        def __init__(self):
            super().__init__(enabled=False)
    saved = E.EchoSuppression
    E.EchoSuppression = Off
    try:
        assert local_audio.echo_control() is None
    finally:
        E.EchoSuppression = saved


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
