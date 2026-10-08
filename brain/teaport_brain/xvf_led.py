#
# xvf_led.py — the ReSpeaker XVF3800's LED ring as a busy lamp (BLF) for phone calls.
#
# busy(True) turns the ring solid red while the box is on a call (breathing red with
# ringing=True, for a call not yet answered); busy(False) puts back what the ring showed
# before, which is normally the firmware's own listening effect (DoA: the direction of
# the voice it hears). gateway_server drives it from the session arbiter's call claim
# (_busy_lamp).
#
# The XVF3800 takes its settings as USB vendor control transfers on endpoint 0 (Seeed's
# python_control/xvf_host.py, XMOS's host-control protocol): the resource id in wIndex,
# the command id in wValue (| 0x80 to read it), the values little-endian in the data
# stage; a read answers a status byte first (0 ok, 64 "retry") and the values after it.
# The LED commands are resource 20 (GPO servicer):
#   LED_EFFECT 12, uint8   0 off, 1 breath, 2 rainbow, 3 single colour, 4 DoA, 5 ring
#   LED_COLOR  16, uint32  0xRRGGBB, the colour of breath and single colour
# Both read back, so the ring is restored to what it was, not to a guess. Endpoint 0
# is not the audio interfaces' endpoints: this runs beside the ALSA streams of the
# local audio bridge without touching them, and claims no interface.
#
# Not pyusb or libusb: one control transfer is one ioctl on the device's usbfs node
# (USBDEVFS_CONTROL, what libusb does underneath on Linux), found by VID:PID in sysfs.
# The node is root's unless a udev rule hands it to the teaport-hw group, which the
# run user is in (install.sh, install_busy_lamp). No XVF3800, or no Linux: nothing to
# show, nothing done. A device that re-enumerates (a replug, a firmware reset) comes
# back at its own default look under a new node: the lamp sees the new node and lights
# it again.
#
# The look to put back is also kept in a marker file (MARKER) from just before the ring
# is lit until it has been restored, so a brain that dies mid-call leaves it behind and
# the next one restores it at start-up. It lives on /run (tmpfs): a crash keeps it, a
# reboot -- which restarts the XVF3800 at its own default too -- clears it. Its directory
# comes from install.sh's tmpfiles.d line, so it is there from boot and needs no unit
# change; the brain unit's own RuntimeDirectory= would be removed with every stop.
#
# It never raises: a lamp is not worth a call. A failure is logged once (until the
# lamp works again) and busy() returns False so the caller can try again later.
#
import ctypes
import fcntl
import glob
import json
import os
import struct
import threading
import time

from loguru import logger

from teaport_brain.env import env_choice

VID, PID = 0x2886, 0x001A
LED_RESID = 20
LED_EFFECT, LED_COLOR = 12, 16
EFFECT_BREATH, EFFECT_SINGLE, EFFECT_DOA = 1, 3, 4
# The firmware's own idle look (rainbow at boot, then DoA; LED_COLOR read 0x002040 on
# the appliance), put back when a brain died with the ring red and left no marker.
DEFAULT_EFFECT, DEFAULT_COLOR = EFFECT_DOA, 0x002040
MARKER = "/run/teaport-busy-lamp/saved.json"
STATUS_OK, STATUS_RETRY = 0, 64
TIMEOUT_MS = 500
READ_ATTEMPTS = 20

ENABLED = env_choice("TEAPORT_BUSY_LAMP", "auto", ("auto", "off")) == "auto"


def _color(raw: str, default: int = 0xFF0000) -> int:
    try:
        value = int(raw.strip().removeprefix("#").removeprefix("0x") or "x", 16)
    except ValueError:
        value = -1
    if not 0 <= value <= 0xFFFFFF:
        logger.warning(f"TEAPORT_BUSY_LAMP_COLOR={raw!r} is not an RRGGBB colour; using red")
        return default
    return value


COLOR = _color(os.getenv("TEAPORT_BUSY_LAMP_COLOR") or "ff0000")


# --- the transport: one vendor control transfer on the device's usbfs node ---

class _CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer (linux/usbdevice_fs.h)."""
    _fields_ = [("bRequestType", ctypes.c_uint8), ("bRequest", ctypes.c_uint8),
                ("wValue", ctypes.c_uint16), ("wIndex", ctypes.c_uint16),
                ("wLength", ctypes.c_uint16), ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


# _IOWR('U', 0, struct usbdevfs_ctrltransfer)
USBDEVFS_CONTROL = (3 << 30) | (ctypes.sizeof(_CtrlTransfer) << 16) | (ord("U") << 8) | 0
VENDOR_OUT, VENDOR_IN = 0x40, 0xC0  # vendor request to the device, host-to-device / back


def find_node(sysfs: str = "/sys/bus/usb/devices") -> str | None:
    """The usbfs node of the first XVF3800 plugged in, or None."""
    for d in sorted(glob.glob(os.path.join(sysfs, "*"))):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if int(f.read(), 16) != VID:
                    continue
            with open(os.path.join(d, "idProduct")) as f:
                if int(f.read(), 16) != PID:
                    continue
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                dev = int(f.read())
        except (OSError, ValueError):
            continue
        return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    return None


class UsbfsDevice:
    """The XVF3800's control endpoint, open for one exchange (a `with` block): a device
    that re-enumerates between calls is simply found again."""

    def __init__(self, node: str):
        self.fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)

    def control(self, request_type: int, value: int, index: int, data: bytes | int) -> bytes:
        """One control transfer: `data` bytes out, or an int byte count in."""
        n = data if isinstance(data, int) else len(data)
        buf = ctypes.create_string_buffer(data if isinstance(data, bytes) else b"", max(n, 1))
        ctrl = _CtrlTransfer(request_type, 0, value, index, n, TIMEOUT_MS,
                             ctypes.cast(buf, ctypes.c_void_p))
        got = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, ctrl)
        return buf.raw[:got] if isinstance(data, int) else b""


# --- the commands ---

def write_u32(dev, cmd: int, *values: int) -> None:
    dev.control(VENDOR_OUT, cmd, LED_RESID, struct.pack(f"<{len(values)}I", *values))


def write_u8(dev, cmd: int, value: int) -> None:
    dev.control(VENDOR_OUT, cmd, LED_RESID, bytes([value]))


def read(dev, cmd: int, size: int) -> bytes:
    """The value of a read command (`size` bytes, the status byte stripped)."""
    for _ in range(READ_ATTEMPTS):
        got = dev.control(VENDOR_IN, 0x80 | cmd, LED_RESID, size + 1)
        if got and got[0] == STATUS_OK and len(got) == size + 1:
            return got[1:]
        if not got or got[0] != STATUS_RETRY:
            raise OSError(f"XVF3800 read {cmd}: status {got[:1].hex() or 'none'}")
        time.sleep(0.01)
    raise OSError(f"XVF3800 read {cmd}: still busy after {READ_ATTEMPTS} tries")


def read_look(dev) -> tuple[int, int]:
    """(effect, colour) the ring shows now."""
    effect = read(dev, LED_EFFECT, 1)[0]
    (color,) = struct.unpack("<I", read(dev, LED_COLOR, 4))
    return effect, color


def show(dev, effect: int, color: int) -> None:
    write_u32(dev, LED_COLOR, color)  # colour first: the effect then starts in it
    write_u8(dev, LED_EFFECT, effect)


# --- the lamp ---

IDLE, BUSY, RINGING = "idle", "busy", "ringing"


class BusyLamp:
    def __init__(self, find=find_node, opener=UsbfsDevice, color: int = COLOR,
                 enabled: bool = ENABLED, marker: str = MARKER):
        self._find, self._opener = find, opener
        self.color = color
        self.enabled = enabled
        self.marker = marker
        self.node = None       # the device's usbfs node when last looked
        self.shown = None      # the state the ring was last set to; None = not known
        self.saved = None      # (effect, colour) from before the call, to put back
        self._failing = False  # an error was logged and the lamp has not worked since
        # set() runs on worker threads: one cancelled mid-write must finish before the
        # next one looks at `shown`, or a stop's restore could land before its red.
        self._lock = threading.Lock()

    def set(self, state: str) -> bool:
        """Show `state` (IDLE, BUSY or RINGING). True once the ring shows it, or when
        there is no ring to show it on; False on an error (logged once)."""
        if not self.enabled:
            return True
        with self._lock:
            return self._set(state)

    def _set(self, state: str) -> bool:
        try:
            node = self._find()
            if node != self.node:  # plugged in, out, or re-enumerated: its own look again
                self.node, self.shown = node, None
            if state == self.shown:
                return True
            if node is None:
                self.shown = state  # no XVF3800 here: nothing to show
                return True
            with self._opener(node) as dev:
                self._apply(dev, state)
        except Exception as e:  # noqa: BLE001 — a lamp must never fail a call
            if not self._failing:
                logger.warning(f"busy lamp: could not set the XVF3800 ring {state}: {e!r}"
                               + (" (needs the installer's udev rule and the teaport-hw group)"
                                  if isinstance(e, PermissionError) else ""))
                self._failing = True
            return False
        if self._failing:
            logger.info(f"busy lamp: working again ({state})")
            self._failing = False
        self.shown = state
        return True

    def _apply(self, dev, state: str) -> None:
        if state == IDLE:
            saved = self.saved or self._load_marker()
            if saved is not None:
                show(dev, *saved)
            elif not os.path.isdir(os.path.dirname(self.marker)):
                # No marker directory (a box the installer has not set up for it): fall
                # back to recognising our own look, which a dead brain may have left lit.
                effect, color = read_look(dev)
                if effect in (EFFECT_SINGLE, EFFECT_BREATH) and color == self.color:
                    show(dev, DEFAULT_EFFECT, DEFAULT_COLOR)
            self.saved = None
            self._drop_marker()
            return
        if self.saved is None:
            # A marker here is from a brain that died mid-call: its look is the room's,
            # what the ring shows now is that call's red.
            self.saved = self._load_marker() or read_look(dev)
            self._save_marker(self.saved)
        show(dev, EFFECT_BREATH if state == RINGING else EFFECT_SINGLE, self.color)

    def _load_marker(self):
        try:
            with open(self.marker) as f:
                d = json.load(f)
            return int(d["effect"]), int(d["color"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"busy lamp: ignoring {self.marker}: {e!r}")
            return None

    def _save_marker(self, look) -> None:
        tmp = self.marker + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"effect": look[0], "color": look[1]}, f)
            os.replace(tmp, self.marker)
        except OSError:
            pass  # no marker directory: a crash mid-call falls back to the look check

    def _drop_marker(self) -> None:
        try:
            os.unlink(self.marker)
        except OSError:
            pass


_lamp = BusyLamp()


def busy(on: bool, ringing: bool = False) -> bool:
    """The ring red while the box is on a call (`ringing`: breathing, for a call not yet
    answered), the room's own effect otherwise. Idempotent -- cheap enough to repeat
    every second of a call, which is how a replugged ring is lit again -- blocking (a
    few ms of USB; up to TIMEOUT_MS on a wedged device), never raises. False: try
    again later."""
    return _lamp.set(IDLE if not on else RINGING if ringing else BUSY)
