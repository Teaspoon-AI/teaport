#
# xvf_echo.py — the XVF3800's echo suppression, set by the speaker's volume.
#
# The room mic's channel 0 (LOCAL_AUDIO_CAPTURE_CHANNEL) is the chip's conversation mix:
# echo-cancelled, then post-processed. As shipped (firmware 2.0.6), the post-processing
# suppresses residual echo hard (PP_ECHOONOFF on, PP_NLATTENONOFF on, PP_DTSENSITIVE 0,
# its most suppressive double-talk setting) -- and while the bot speaks it suppresses the
# USER with it. Controlled takes on the appliance, 2026-10-10 (a story played through the
# speaker at the bridge's real level, five questions asked over it, scored against
# Whisper and Deepgram):
#
#   volume  post-processing                 engine heard   echo heard as speech
#   30      factory                         1 of 5         none
#   30      ES off, NL off, DT 5            5 of 5         none
#   50      ES off, NL off, DT 5            5 of 5         none (brain VAD: 0 onsets)
#   65      ES off, NL off, DT 5            5 of 5         yes: bot words prefixed to 2
#   80      DT 5 (ES, NL on)                1 of 5         none
#   80      ES off, NL off (live session)   --             the bot interrupted itself
#
# So up to DUPLEX_MAX_VOLUME the suppressor is turned off and the user is heard over the
# bot; above it the echo is too loud to leave in and the chip's own settings go back.
# The brain's echo guard (echo_guard.py) is the net for what leaks either way.
#
# Written at run time only (never SAVE_CONFIGURATION): a replug or a chip reset brings
# the factory values back, and the bridge writes again whenever it opens the card or the
# volume changes. Both sets are written whole, so the chip is never left half-way, and
# read back. The transport is xvf_led's (USB vendor control transfers on endpoint 0;
# the PP resource is 17, each value an int32), and the same rule holds: no device, no
# Linux, nothing done; never raises; a failure is logged once until it works again.
#
import struct
import threading

from loguru import logger

from teaport_brain import xvf_led
from teaport_brain.settings import setting

PP_RESID = 17
PP_ECHOONOFF, PP_NLATTENONOFF, PP_DTSENSITIVE = 23, 27, 31

# (command, value) in write order; read back the same.
FACTORY = ((PP_ECHOONOFF, 1), (PP_NLATTENONOFF, 1), (PP_DTSENSITIVE, 0))
DUPLEX = ((PP_ECHOONOFF, 0), (PP_NLATTENONOFF, 0), (PP_DTSENSITIVE, 5))

# The highest speaker volume (percent, the bridge's set_volume level) at which the chip's
# echo suppressor is off. 0: never off, the chip's own settings at every volume.
DUPLEX_MAX_VOLUME = setting("LOCAL_AUDIO_DUPLEX_MAX_VOLUME")


def write_i32(dev, cmd: int, value: int) -> None:
    dev.control(xvf_led.VENDOR_OUT, cmd, PP_RESID, struct.pack("<i", value))


def read_i32(dev, cmd: int) -> int:
    (value,) = struct.unpack("<i", xvf_led.read(dev, cmd, 4, resid=PP_RESID))
    return value


class EchoSuppression:
    """The chip's post-processing, kept in step with the volume: apply(percent)."""

    def __init__(self, find=xvf_led.find_node, opener=xvf_led.UsbfsDevice,
                 max_volume: int = DUPLEX_MAX_VOLUME):
        self._find, self._opener = find, opener
        self.max_volume = max_volume
        self.node = None       # the device's usbfs node when last written
        self.shown = None      # FACTORY or DUPLEX as last written there; None = not known
        self._failing = False
        # apply() runs on worker threads: a volume change racing the card's open must
        # not interleave its writes with the other's.
        self._lock = threading.Lock()

    def wanted(self, percent: int):
        return DUPLEX if 0 < percent <= self.max_volume else FACTORY

    def apply(self, percent: int) -> bool:
        """Set the chip for `percent`. True once it holds it (or there is no chip);
        False on an error (logged once). Blocking: a few ms of USB."""
        with self._lock:
            return self._apply(percent)

    def _apply(self, percent: int) -> bool:
        want = self.wanted(percent)
        name = "off (the user is heard over the bot)" if want is DUPLEX else "the chip's own"
        try:
            node = self._find()
            if node != self.node:  # plugged in, out, or reset: back to its own values
                self.node, self.shown = node, None
            if node is None or want is self.shown:
                return True
            with self._opener(node) as dev:
                for cmd, value in want:
                    write_i32(dev, cmd, value)
                got = tuple((cmd, read_i32(dev, cmd)) for cmd, _ in want)
            if got != want:
                raise OSError(f"read back {got}, wrote {want}")
        except Exception as e:  # noqa: BLE001 — the mic must keep working regardless
            self.shown = None
            if not self._failing:
                logger.warning(f"mic echo suppression: could not set it {name} at volume "
                               f"{percent}%: {e!r}"
                               + (" (needs the installer's udev rule and the teaport-hw group)"
                                  if isinstance(e, PermissionError) else ""))
                self._failing = True
            return False
        self._failing = False
        self.shown = want
        logger.info(f"mic echo suppression: {name} at volume {percent}% "
                    f"(off up to {self.max_volume}%)")
        return True
