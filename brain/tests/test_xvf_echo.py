#
# Unit test: the XVF3800's echo suppression set by the speaker volume
# (teaport_brain.xvf_echo) and the bridge's two triggers for it (local_audio: the card
# opening, set_volume).
#
# Up to LOCAL_AUDIO_DUPLEX_MAX_VOLUME the chip's suppressor is off (the user is heard
# over the bot); above it, and at 0, the chip's own settings -- as read from it, not
# assumed. Each set is written whole and read back; the same set is not written twice;
# a replugged chip (back at its own settings) is written again; changes made in quick
# succession land for the volume as it is; a limit of 0 never touches the chip; no
# chip does nothing; a failing chip is logged once and never raises.
#
# The device is a fake with int32 registers behind the same control-transfer call the
# usbfs transport makes, so the wire format (resource 17, command | 0x80 to read, the
# status byte) is checked too. The live check is in the PR.
#
# Run: python test_xvf_echo.py
#
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

from teaport_brain import local_audio, xvf_echo, xvf_led  # noqa: E402

E = xvf_echo


class FakePP:
    """The XVF3800's PP registers (int32) behind control(). `retries` reads answer
    "retry" first; `stuck` ignores writes (a chip that will not take them)."""

    def __init__(self, retries=0, stuck=False, fail=None, own=E.FACTORY):
        self.regs = dict(own)
        self.opened = 0
        self.retries, self.stuck, self.fail = retries, stuck, fail
        self.node, self.devnum = "/dev/bus/usb/001/004", 4
        self.writes = []

    def replug(self):
        self.devnum += 1
        self.node = f"/dev/bus/usb/001/{self.devnum:03d}"
        self.regs = dict(E.FACTORY)

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
            (v,) = struct.unpack("<i", data)
            self.writes.append((value, v))
            if not self.stuck:
                self.regs[value] = v
            return b""
        assert request_type == xvf_led.VENDOR_IN and value & 0x80, (request_type, value)
        if self.retries:
            self.retries -= 1
            return bytes([xvf_led.STATUS_RETRY]) + b"\0" * (data - 1)
        out = bytes([xvf_led.STATUS_OK]) + struct.pack("<i", self.regs[value & 0x7F])
        assert len(out) == data, (value, len(out), data)
        return out

    def state(self):
        return tuple((cmd, self.regs[cmd]) for cmd, _ in E.FACTORY)


def _echo(dev, max_volume=50):
    return E.EchoSuppression(find=dev.find, opener=dev.open, max_volume=max_volume)


class _Warnings:
    def __enter__(self):
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level="WARNING")
        return self.lines

    def __exit__(self, *exc):
        logger.remove(self.sink)


def test_up_to_the_limit_the_suppressor_is_off_and_above_it_the_chip_is_its_own():
    dev = FakePP()
    echo = _echo(dev)
    assert echo.apply(30) and dev.state() == E.DUPLEX
    assert echo.apply(50) and dev.state() == E.DUPLEX
    assert echo.apply(60) and dev.state() == E.FACTORY
    assert echo.apply(10) and dev.state() == E.DUPLEX


def test_the_measured_settings():
    # What the controlled takes used (see the module): ES off, NL off, DT 5 -- and the
    # firmware's own values, so "factory" puts back exactly what a replug would.
    assert dict(E.DUPLEX) == {E.PP_ECHOONOFF: 0, E.PP_NLATTENONOFF: 0, E.PP_DTSENSITIVE: 5}
    assert dict(E.FACTORY) == {E.PP_ECHOONOFF: 1, E.PP_NLATTENONOFF: 1, E.PP_DTSENSITIVE: 0}


def test_silent_means_the_chips_own_and_a_limit_of_0_never_touches_it():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply(30)
    assert echo.apply(0) and dev.state() == E.FACTORY
    dev = FakePP()
    echo = _echo(dev, max_volume=0)
    assert echo.apply(20) and echo.apply(100)
    assert dev.opened == 0 and dev.writes == []


def test_the_chips_own_saved_values_are_what_comes_back():
    tuned = ((E.PP_ECHOONOFF, 1), (E.PP_NLATTENONOFF, 1), (E.PP_DTSENSITIVE, 3))
    dev = FakePP(own=tuned)                    # someone saved their own tuning to it
    echo = _echo(dev)
    assert echo.apply(80) and dev.writes == []  # already its own: nothing written
    assert echo.apply(40) and dev.state() == E.DUPLEX
    assert echo.apply(80) and dev.state() == tuned


def test_a_chip_left_with_the_suppressor_off_gets_the_factory_values_back():
    dev = FakePP(own=E.DUPLEX)                 # a bridge that stopped at volume 40
    echo = _echo(dev)
    assert echo.apply(80) and dev.state() == E.FACTORY


def test_changes_in_quick_succession_land_for_the_volume_as_it_is():
    dev = FakePP()
    echo = _echo(dev)
    level = [40]
    queued = [lambda: level[0], lambda: level[0]]  # 40 queued, then 80 queued...
    level[0] = 80                                  # ...and the level is 80 by the time
    for get in reversed(queued):                   # they run, in either order
        echo.apply(get)
    assert dev.state() == E.FACTORY


def test_the_same_setting_is_not_written_twice():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply(40)
    n = len(dev.writes)
    assert echo.apply(30) and echo.apply(50)
    assert len(dev.writes) == n


def test_a_replugged_chip_is_written_again():
    dev = FakePP()
    echo = _echo(dev)
    echo.apply(40)
    dev.replug()                       # back at its own settings, under a new node
    assert dev.state() == E.FACTORY
    assert echo.apply(40) and dev.state() == E.DUPLEX


def test_no_chip_does_nothing():
    dev = FakePP()
    dev.node = None
    assert _echo(dev).apply(40)
    assert dev.writes == [] and dev.opened == 0


def test_a_read_that_says_retry_is_asked_again():
    dev = FakePP(retries=2)
    assert _echo(dev).apply(40) and dev.state() == E.DUPLEX


def test_a_chip_that_does_not_take_the_write_is_a_failure_and_tried_again():
    dev = FakePP(stuck=True)
    echo = _echo(dev)
    with _Warnings() as lines:
        assert not echo.apply(40)
        assert not echo.apply(40)      # not cached as done: tried again, not logged again
    assert len(lines) == 1 and "read back" in lines[0], lines
    dev.stuck = False
    assert echo.apply(40) and dev.state() == E.DUPLEX


def test_an_unusable_chip_never_raises_and_says_why_once():
    dev = FakePP(fail=PermissionError(13, "Permission denied"))
    echo = _echo(dev)
    with _Warnings() as lines:
        assert not echo.apply(40)
        assert not echo.apply(80)
    assert len(lines) == 1 and "udev rule" in lines[0], lines


def test_set_volume_tells_the_bridge():
    seen = []
    with tempfile.TemporaryDirectory() as d:
        volume = local_audio.Volume(os.path.join(d, "v.json"))
        volume.on_change = seen.append
        volume.apply({"level": 70})
        volume.apply({"direction": "quieter"})
        volume.apply({"level": "loud"})          # refused: no change, no call
    assert seen == [70, 60], seen


def test_the_bridge_volume_drives_the_chip():
    dev = FakePP()
    echo = _echo(dev)
    with tempfile.TemporaryDirectory() as d:
        volume = local_audio.Volume(os.path.join(d, "v.json"))
        volume.on_change = lambda _: echo.apply(lambda: volume.percent)  # as run_bridge
        volume.apply({"level": 80})
        assert dev.state() == E.FACTORY
        volume.apply({"level": 50})
        assert dev.state() == E.DUPLEX


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
