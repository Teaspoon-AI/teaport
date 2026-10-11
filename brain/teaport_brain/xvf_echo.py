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
# bot; above it the echo is too loud to leave in and the chip's own settings go back:
# the three values as the bridge first read them from that chip (FACTORY if it read
# DUPLEX -- a bridge that stopped with the suppressor off). The brain's echo guard
# (echo_guard.py) is the net for what leaks either way. DUPLEX_MAX_VOLUME 0: the chip is
# never touched.
#
# Written at run time only (never SAVE_CONFIGURATION): a replug or a chip reset brings
# its saved values back, and the bridge writes again whenever it opens the card or the
# volume changes -- always for the volume as it is when the write runs, so changes made
# in quick succession cannot land out of order. Each set is written whole, so the chip
# is never left half-way, and read back. The transport is xvf_led's (USB vendor control transfers on endpoint 0;
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

# (command, value) in write order; read back the same. FACTORY: firmware 2.0.6 as shipped.
FACTORY = ((PP_ECHOONOFF, 1), (PP_NLATTENONOFF, 1), (PP_DTSENSITIVE, 0))
DUPLEX = ((PP_ECHOONOFF, 0), (PP_NLATTENONOFF, 0), (PP_DTSENSITIVE, 5))

# The highest speaker volume (percent, the bridge's set_volume level) at which the chip's
# echo suppressor is off. 0: the chip is left alone.
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
        self.own = None        # that chip's own values, read when first seen there
        self.shown = None      # the set it holds as last written or read; None = not known
        self._failing = False
        # apply() runs on worker threads: a volume change racing the card's open must
        # not interleave its writes with the other's.
        self._lock = threading.Lock()

    def duplex(self, percent: int) -> bool:
        return 0 < percent <= self.max_volume

    def apply(self, percent) -> bool:
        """Set the chip for the volume: `percent`, or a function returning it, read once
        the lock is held (the bridge's: the level as it is now, not as it was when this
        was queued). True once the chip holds it (or there is no chip, or the bridge is
        to leave it alone); False on an error (logged once). Blocking: a few ms of USB."""
        if self.max_volume <= 0:
            return True
        with self._lock:
            return self._apply(percent() if callable(percent) else percent)

    def _apply(self, percent: int) -> bool:
        duplex = self.duplex(percent)
        name = "off (the user is heard over the bot)" if duplex else "the chip's own"
        try:
            node = self._find()
            if node != self.node:  # plugged in, out, or reset: its own values, read anew
                self.node, self.own, self.shown, self._failing = node, None, None, False
            if node is None:
                return True
            with self._opener(node) as dev:
                if self.own is None:
                    found = tuple((cmd, read_i32(dev, cmd)) for cmd, _ in FACTORY)
                    self.own = FACTORY if found == DUPLEX else found
                    self.shown = found
                want = DUPLEX if duplex else self.own
                if want != self.shown:
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
