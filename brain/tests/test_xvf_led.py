#
# Unit test: the XVF3800 LED ring as the phone busy lamp (teaport_brain.xvf_led) and
# the gateway's one call site for it (gateway_server._busy_lamp, the session
# arbiter's call claim).
#
# The ring must turn red when a call starts and go back to what it showed before
# however the call ends -- hung up, torn down with the SIP front-end, the brain
# stopping, even mid-write -- and a brain that starts after one died mid-call must put back the look
# that brain saved (its marker), without mistaking a user's own red for the lamp. A
# ring that re-enumerates mid-call is lit again. A box without the device does
# nothing, and a device that errors is logged once and never raises into the call.
#
# The device is a fake with the firmware's register semantics behind the same
# control-transfer call the usbfs transport makes, so the wire format (resource 20,
# command | 0x80 to read, the status byte) is checked too; the transport's own struct
# and buffer handling is checked against a stand-in ioctl. The live check is in the PR.
#
# Run: python test_xvf_led.py
#
import asyncio
import atexit
import ctypes
import os
import shutil
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger  # noqa: E402

from teaport_brain import xvf_led  # noqa: E402

RED = 0xFF0000
ROOM = (xvf_led.EFFECT_DOA, 0x002040)  # what the appliance's ring read before a call
_TMP = tempfile.mkdtemp(prefix="test_xvf_led-")
atexit.register(shutil.rmtree, _TMP, True)


class FakeXvf:
    """The XVF3800's LED registers behind control(): what UsbfsDevice.control does on
    the real device. `retries` reads answer "retry" (status 64) first. `node` is where
    sysfs finds it (None: unplugged); replug() re-enumerates it at its default look."""

    def __init__(self, effect=ROOM[0], color=ROOM[1], retries=0, fail=None):
        self.regs = {}
        self._look(effect, color)
        self.retries, self.fail = retries, fail
        self.node, self.devnum = "/dev/bus/usb/001/004", 4
        self.writes = []
        self.opened = 0

    def _look(self, effect, color):
        self.regs[xvf_led.LED_EFFECT] = bytes([effect])
        self.regs[xvf_led.LED_COLOR] = struct.pack("<I", color)

    def replug(self):
        self.devnum += 1
        self.node = f"/dev/bus/usb/001/{self.devnum:03d}"
        self._look(xvf_led.DEFAULT_EFFECT, xvf_led.DEFAULT_COLOR)

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
        assert index == xvf_led.LED_RESID, index
        if request_type == xvf_led.VENDOR_OUT:
            self.regs[value] = data
            self.writes.append((value, data))
            return b""
        assert request_type == xvf_led.VENDOR_IN and value & 0x80, (request_type, value)
        if self.retries:
            self.retries -= 1
            return bytes([xvf_led.STATUS_RETRY]) + b"\0" * (data - 1)
        out = bytes([xvf_led.STATUS_OK]) + self.regs[value & 0x7F]
        assert len(out) == data, (value, len(out), data)
        return out

    def look(self):
        (color,) = struct.unpack("<I", self.regs[xvf_led.LED_COLOR])
        return self.regs[xvf_led.LED_EFFECT][0], color


def _marker():
    """A fresh marker path in a directory that exists, as install.sh's tmpfiles.d makes it."""
    return os.path.join(tempfile.mkdtemp(dir=_TMP), "saved.json")


def _lamp(dev, marker=None, **kw):
    return xvf_led.BusyLamp(find=dev.find, opener=dev.open, color=RED, enabled=True,
                            marker=marker or _marker(), **kw)


class _Warnings:
    def __enter__(self):
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level="WARNING")
        return self.lines

    def __exit__(self, *exc):
        logger.remove(self.sink)


# --- the lamp ---

def test_a_call_lights_it_red_and_the_end_puts_the_room_back():
    dev = FakeXvf()
    lamp = _lamp(dev)
    assert lamp.set(xvf_led.BUSY)
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    assert os.path.exists(lamp.marker)       # the look to restore, kept while it is lit
    assert lamp.set(xvf_led.IDLE)
    assert dev.look() == ROOM
    assert not os.path.exists(lamp.marker)


def test_ringing_breathes_and_then_the_answer_holds_it_solid():
    dev = FakeXvf(effect=2, color=0x123456)  # someone's own rainbow
    lamp = _lamp(dev)
    assert lamp.set(xvf_led.RINGING)
    assert dev.look() == (xvf_led.EFFECT_BREATH, RED)
    assert lamp.set(xvf_led.BUSY)
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    assert lamp.set(xvf_led.IDLE)
    assert dev.look() == (2, 0x123456)      # theirs, read back, not the default


def test_it_is_idempotent():
    dev = FakeXvf()
    lamp = _lamp(dev)
    lamp.set(xvf_led.BUSY)
    n = len(dev.writes)
    assert lamp.set(xvf_led.BUSY) and lamp.set(xvf_led.BUSY)
    assert len(dev.writes) == n and dev.opened == 1


def test_a_read_that_says_retry_is_asked_again():
    dev = FakeXvf(retries=3)
    assert _lamp(dev).set(xvf_led.BUSY)
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED)


def test_a_restarted_brain_puts_back_what_the_dead_one_saved():
    dev = FakeXvf(effect=2, color=0x123456)  # someone's rainbow
    marker = _marker()
    _lamp(dev, marker).set(xvf_led.BUSY)     # ... and that brain is SIGKILLed mid-call
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    assert _lamp(dev, marker).set(xvf_led.IDLE)  # the next brain's first pass
    assert dev.look() == (2, 0x123456)
    assert not os.path.exists(marker)


def test_a_restarted_brain_still_on_the_call_restores_the_room_not_the_red():
    dev = FakeXvf(effect=2, color=0x123456)
    marker = _marker()
    _lamp(dev, marker).set(xvf_led.BUSY)     # dies; the gateway replays the call to its
    lamp = _lamp(dev, marker)                # successor before that one's lamp first looks
    assert lamp.set(xvf_led.BUSY) and dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    assert lamp.set(xvf_led.IDLE)
    assert dev.look() == (2, 0x123456)


def test_a_users_own_red_is_left_alone_at_start_up():
    dev = FakeXvf(effect=xvf_led.EFFECT_SINGLE, color=RED)  # the user's own solid red
    assert _lamp(dev).set(xvf_led.IDLE)      # no marker: no call was cut short
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED) and not dev.writes


def test_without_a_marker_directory_the_lamps_own_look_is_still_recognised():
    gone = os.path.join(_TMP, "no-such-dir", "saved.json")
    dev = FakeXvf(effect=xvf_led.EFFECT_SINGLE, color=RED)  # a brain died mid-call
    assert _lamp(dev, gone).set(xvf_led.IDLE)
    assert dev.look() == (xvf_led.DEFAULT_EFFECT, xvf_led.DEFAULT_COLOR)
    dev = FakeXvf(effect=xvf_led.EFFECT_SINGLE, color=0x00FF00)  # someone's green
    assert _lamp(dev, gone).set(xvf_led.IDLE)
    assert dev.look() == (xvf_led.EFFECT_SINGLE, 0x00FF00) and not dev.writes
    dev = FakeXvf()                          # and a call still lights and restores
    lamp = _lamp(dev, gone)
    assert lamp.set(xvf_led.BUSY) and lamp.set(xvf_led.IDLE) and dev.look() == ROOM


def test_a_ring_that_re_enumerates_mid_call_is_lit_again():
    dev = FakeXvf(effect=2, color=0x123456)
    lamp = _lamp(dev)
    assert lamp.set(xvf_led.BUSY)
    dev.replug()                             # back at its own default, on a new node
    assert dev.look() == (xvf_led.DEFAULT_EFFECT, xvf_led.DEFAULT_COLOR)
    assert lamp.set(xvf_led.BUSY)            # the next poll pass of the call
    assert dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    assert lamp.set(xvf_led.IDLE)
    assert dev.look() == (2, 0x123456)       # what was there before the call
    dev.node = None                          # unplugged: nothing to do, no error
    assert lamp.set(xvf_led.BUSY) and lamp.set(xvf_led.IDLE)


def test_no_device_is_a_no_op():
    lamp = xvf_led.BusyLamp(find=lambda: None, opener=None, color=RED, enabled=True,
                            marker=_marker())
    with _Warnings() as lines:
        assert lamp.set(xvf_led.BUSY) and lamp.set(xvf_led.IDLE)
    assert not lines


def test_off_never_touches_the_ring():
    dev = FakeXvf()
    lamp = xvf_led.BusyLamp(find=dev.find, opener=dev.open, color=RED, enabled=False,
                            marker=_marker())
    assert lamp.set(xvf_led.BUSY)
    assert dev.opened == 0 and dev.look() == ROOM


def test_an_error_is_logged_once_and_never_raised():
    dev = FakeXvf(fail=PermissionError(13, "Permission denied"))
    lamp = _lamp(dev)
    with _Warnings() as lines:
        for _ in range(5):
            assert lamp.set(xvf_led.BUSY) is False
            assert lamp.set(xvf_led.IDLE) is False
    assert len(lines) == 1 and "teaport-hw" in lines[0], lines
    dev.fail = None                         # the rule landed: it works, and says so
    assert lamp.set(xvf_led.BUSY) and dev.look() == (xvf_led.EFFECT_SINGLE, RED)
    dev.fail = OSError(5, "Input/output error")
    with _Warnings() as lines:
        lamp.set(xvf_led.IDLE)
        lamp.set(xvf_led.IDLE)
    assert len(lines) == 1, lines           # a new outage is logged again, once


def test_a_bad_status_is_an_error_not_a_hang():
    dev = FakeXvf()
    dev.control = lambda *a: bytes([1, 0])
    assert _lamp(dev).set(xvf_led.BUSY) is False


def test_busy_maps_to_the_three_states():
    dev = FakeXvf()
    saved = xvf_led._lamp
    xvf_led._lamp = _lamp(dev)
    try:
        assert xvf_led.busy(True, ringing=True) and dev.look()[0] == xvf_led.EFFECT_BREATH
        assert xvf_led.busy(True) and dev.look()[0] == xvf_led.EFFECT_SINGLE
        assert xvf_led.busy(False) and dev.look() == ROOM
    finally:
        xvf_led._lamp = saved


def test_the_colour_setting():
    assert xvf_led._color("ff0000") == 0xFF0000
    assert xvf_led._color("#00ff88") == 0x00FF88
    assert xvf_led._color("0x0000FF") == 0x0000FF
    with _Warnings() as lines:
        assert xvf_led._color("red") == 0xFF0000
        assert xvf_led._color("1000000") == 0xFF0000
    assert len(lines) == 2


# --- the transport ---

def test_the_device_is_found_by_vid_pid_in_sysfs():
    with tempfile.TemporaryDirectory() as d:
        def dev(name, vid, pid, bus, num):
            os.mkdir(os.path.join(d, name))
            for k, v in (("idVendor", vid), ("idProduct", pid), ("busnum", bus), ("devnum", num)):
                with open(os.path.join(d, name, k), "w") as f:
                    f.write(v + "\n")
        os.mkdir(os.path.join(d, "1-0:1.0"))  # an interface: no idVendor
        dev("1-1", "0bda", "5411", "1", "2")
        assert xvf_led.find_node(d) is None
        dev("1-2.1", "2886", "001a", "1", "4")
        assert xvf_led.find_node(d) == "/dev/bus/usb/001/004"


def test_the_ioctl_is_usbdevfs_control():
    # _IOWR('U', 0, struct usbdevfs_ctrltransfer): 24 bytes on a 64-bit box.
    import ctypes
    assert ctypes.sizeof(xvf_led._CtrlTransfer) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert xvf_led.USBDEVFS_CONTROL == 0xC0185500


def test_a_control_transfer_is_one_ioctl_with_the_struct_filled_in():
    calls = []

    def ioctl(fd, request, ctrl):
        assert request == xvf_led.USBDEVFS_CONTROL
        calls.append((ctrl.bRequestType, ctrl.bRequest, ctrl.wValue, ctrl.wIndex,
                      ctrl.wLength, ctrl.timeout))
        if ctrl.bRequestType == xvf_led.VENDOR_OUT:
            calls.append(ctypes.string_at(ctrl.data, ctrl.wLength))
            return ctrl.wLength
        ctypes.memmove(ctrl.data, b"\x00\x04\x99", 3)
        return 2                             # a short read: only what came back counts

    real = xvf_led.fcntl.ioctl
    xvf_led.fcntl.ioctl = ioctl
    try:
        with tempfile.NamedTemporaryFile(dir=_TMP) as node, xvf_led.UsbfsDevice(node.name) as d:
            assert d.control(xvf_led.VENDOR_OUT, 16, 20, struct.pack("<I", RED)) == b""
            got = d.control(xvf_led.VENDOR_IN, 0x80 | 12, 20, 3)
    finally:
        xvf_led.fcntl.ioctl = real
    assert calls[0] == (0x40, 0, 16, 20, 4, xvf_led.TIMEOUT_MS)
    assert calls[1] == struct.pack("<I", RED)
    assert calls[2] == (0xC0, 0, 0x8C, 20, 3, xvf_led.TIMEOUT_MS)
    assert got == b"\x00\x04"


# --- the session arbiter's call claim drives it (gateway_server) ---

async def _until(cond, secs=2.0):
    deadline = time.monotonic() + secs
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _gateway(dev):
    """gateway_server with a fresh arbiter and this fake behind the lamp."""
    from teaport_brain import gateway_server as gs
    from teaport_brain import session_arbiter as arb
    gs.LAMP_POLL_SECS = 0.05
    gs.LAMP_RETRY_SECS = 15.0
    arb.ARBITER = arb.SessionArbiter()
    xvf_led._lamp = _lamp(dev)
    return gs, arb


async def _call(arb):
    """A phone call takes the engine, as sip_server's CallClaim does."""
    claim = arb.Claim(arb.CALL, label="test call")
    assert await arb.ARBITER.acquire(claim) is None
    return claim


async def _call_start_and_end():
    dev = FakeXvf()
    gs, arb = _gateway(dev)
    gs.LAMP_POLL_SECS = 5.0                    # so a change seen well inside it was nudged
    lamp = asyncio.create_task(gs._busy_lamp())
    try:
        await _until(lambda: gs._lamp_wake is not None)
        assert dev.look() == ROOM
        claim = await _call(arb)
        await _until(lambda: dev.look() == (xvf_led.EFFECT_SINGLE, RED), secs=1.0)
        arb.ARBITER.release(claim)              # hung up
        await _until(lambda: dev.look() == ROOM, secs=1.0)
        talk = arb.Claim(arb.TALK, label="talk")  # not a call: the ring stays the room's
        await arb.ARBITER.acquire(talk)
        await asyncio.sleep(0.1)
        assert dev.look() == ROOM
        arb.ARBITER.release(talk)
    finally:
        lamp.cancel()
        await asyncio.gather(lamp, return_exceptions=True)
    assert not arb.ARBITER.listeners           # its listener went with it


def test_the_call_claim_drives_the_lamp():
    asyncio.run(_call_start_and_end())


async def _stop_mid_call(release_first):
    dev = FakeXvf()
    gs, arb = _gateway(dev)
    async with gs._lifespan(gs.app):
        await _until(lambda: gs._lamp_wake is not None)
        claim = await _call(arb)
        await _until(lambda: dev.look()[1] == RED)
        if release_first:
            # _ReadyServer.shutdown tears the SIP front-end down (its call released)
            # before uvicorn runs the lifespan shutdown.
            arb.ARBITER.release(claim)
    # The brain stopped with the call live: the ring is the room's either way.
    assert dev.look() == ROOM
    arb.ARBITER.release(claim)


def test_a_brain_stopping_mid_call_puts_the_room_back():
    asyncio.run(_stop_mid_call(release_first=True))
    asyncio.run(_stop_mid_call(release_first=False))


async def _failing_device_is_retried():
    dev = FakeXvf(fail=PermissionError(13, "Permission denied"))
    gs, arb = _gateway(dev)
    gs.LAMP_RETRY_SECS = 0.1
    with _Warnings() as lines:
        lamp = asyncio.create_task(gs._busy_lamp())
        try:
            claim = await _call(arb)
            await asyncio.sleep(0.3)
            dev.fail = None                     # the group/udev rule landed (--only brain order)
            await _until(lambda: dev.look()[1] == RED)
        finally:
            lamp.cancel()
            await asyncio.gather(lamp, return_exceptions=True)
            arb.ARBITER.release(claim)
    assert len(lines) == 1, lines
    assert dev.look() == ROOM                   # and the stop put it back


def test_a_lamp_that_fails_is_retried_and_never_breaks_the_call():
    asyncio.run(_failing_device_is_retried())


class _SlowXvf(FakeXvf):
    def control(self, *a):
        time.sleep(0.02)  # a slow USB exchange (real: ~1 ms), to widen the window
        return super().control(*a)


async def _stop_while_lighting():
    dev = _SlowXvf()
    gs, arb = _gateway(dev)
    async with gs._lifespan(gs.app):
        await _until(lambda: gs._lamp_wake is not None)
        await asyncio.sleep(0.2)                 # the start-up pass is done
        claim = await _call(arb)
        await asyncio.sleep(0.03)                # busy(True) is mid-write on its thread
    # The stop's restore waited for that write instead of landing before it.
    await asyncio.sleep(0.3)
    assert dev.look() == ROOM, dev.look()
    arb.ARBITER.release(claim)


def test_a_stop_that_lands_mid_write_still_leaves_the_room():
    asyncio.run(_stop_while_lighting())


async def _replug_mid_call():
    dev = FakeXvf()
    gs, arb = _gateway(dev)
    lamp = asyncio.create_task(gs._busy_lamp())
    try:
        claim = await _call(arb)
        await _until(lambda: dev.look()[1] == RED)
        dev.replug()
        await _until(lambda: dev.look() == (xvf_led.EFFECT_SINGLE, RED))  # the next poll
        arb.ARBITER.release(claim)
        await _until(lambda: dev.look() == ROOM)
    finally:
        lamp.cancel()
        await asyncio.gather(lamp, return_exceptions=True)


def test_the_poll_relights_a_ring_replugged_mid_call():
    asyncio.run(_replug_mid_call())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
