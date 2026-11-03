#
# xvf_echo.py — the XVF3800's mic tuning: a fixed gain in place of its AGC, and echo
# suppression that takes the bot's echo but lets the user's voice through over it.
#
# The room mic's channel 0 (LOCAL_AUDIO_CAPTURE_CHANNEL) is the chip's conversation mix:
# echo-cancelled, then post-processed. As shipped (firmware 2.0.6) the post-processing
# suppresses residual echo hard (FACTORY: PP_ECHOONOFF and PP_NLATTENONOFF on,
# PP_DTSENSITIVE 0) -- and while the bot speaks it suppresses the USER with it -- and
# its AGC (up to +36 dB) lifts whatever is left, echo included. Controlled takes on the
# appliance, 2026-10-10/11 (a story through the speaker, five questions asked over it,
# scored against Whisper and Deepgram):
#
#   setting                              where   engine heard   echo as speech
#   factory, volume 30 or 80             near    1 of 5         none
#   suppressor off, AGC on, volume 50    near    5 of 5         none
#   suppressor off, AGC off, volume 80   near    4 of 5         one word
#   suppressor off, AGC on, volume 65    near    5 of 5         words prefixed
#   suppressor off, AGC off, volume 80   3-4 m   3 of 5         a word, 2 onsets
#
# From volume 50 to 65 the echo on the AGC-free channel rose 2 dB and on channel 0 14:
# the AGC, not the speaker, made the leak. So the AGC is off, at a fixed gain
# (MIC_GAIN_DB): nothing lifts the echo in a pause.
#
# From that table the suppressor was turned off outright, and back to the factory values
# only when a meter found the user's voice too close above the echo. Measured again
# 2026-11-02 (a 20 s bot clip through the speaker, nobody talking; channel 0 in 100 ms
# blocks, dBFS; the room's floor about -41; then a line spoken over the clip, which in
# silence reads about -21):
#
#   ECHOONOFF NLATTENONOFF DTSENSITIVE   volume  echo p90            the user over it
#   0 0 5 (suppressor off)               70      -19, peaks -11      --
#   0 0 5 (suppressor off)               50      -29                 --
#   1 0 5                                70      below the floor     cut 10-15 dB
#   1 0 15 (DUPLEX)                      70      -43, below floor    -19..-24: about whole
#   1 1 0 (FACTORY)                      70      below the floor     cut almost entirely
#
# Off at volume 70, the echo stood 22 dB over the floor, as loud as a user 3-4 m
# away: the speech engine transcribed it and the bot interrupted itself. The meter,
# there to catch just that, put the voice 19-22 dB above that echo, past its 15 dB
# margin, and kept the suppressor off: the echo it measured was not the echo the
# engine heard. The suppressor on with the non-linear attenuation off and the
# double-talk detector at 15 takes the echo and keeps the user: in live conversations
# held at it, no echo was transcribed, the bot never interrupted itself, and every
# interruption and backchannel was heard. One set does both, so the bridge holds that
# one (DUPLEX) and measures nothing. The brain's echo guard (echo_guard.py) still takes
# whatever echo leaks.
#
# Written at run time only (never SAVE_CONFIGURATION): a replug or a chip reset brings
# its saved values back. So the bridge reads the chip's AGC and suppressor back when it
# opens the card and every ECHO_CHECK_SECS while it is open (local_audio), and writes
# again whatever differs, a new node or not; a chip found reset under the same node is
# logged. Each set is written whole and read back. The transport and its upkeep are
# xvf_led's (USB vendor control transfers on endpoint 0, the PP resource 17;
# xvf_led.ChipSetting): no device, no Linux, nothing done; never raises; a failure is
# logged once until it works again. ECHO_CONTROL off: the chip is not touched.
#
import os
import struct
import time

from loguru import logger

from teaport_brain import xvf_led
from teaport_brain.settings import setting

PP_RESID = 17
PP_AGCONOFF, PP_AGCGAIN = 10, 13
PP_ECHOONOFF, PP_NLATTENONOFF, PP_DTSENSITIVE = 23, 27, 31

# (command, value) in write order; read back the same.
# FACTORY: firmware 2.0.6 as shipped (named when the chip is found at it).
FACTORY = ((PP_ECHOONOFF, 1), (PP_NLATTENONOFF, 1), (PP_DTSENSITIVE, 0))
# DUPLEX: what the bridge holds, the double-talk-friendly suppression -- the suppressor
# ON (off, the echo is heard as speech), the non-linear attenuation off and the
# double-talk detector at 15 (at 5, or with the attenuation on, the user over the bot was
# cut: the module's table).
DUPLEX = ((PP_ECHOONOFF, 1), (PP_NLATTENONOFF, 0), (PP_DTSENSITIVE, 15))

ECHO_CONTROL = setting("LOCAL_AUDIO_ECHO_CONTROL")
# Retired with the meter (settings.py, RETIRED): an env file that still sets it is told,
# not silently ignored.
if os.getenv("LOCAL_AUDIO_ECHO_MARGIN_DB") is not None:
    logger.warning("LOCAL_AUDIO_ECHO_MARGIN_DB is no longer read: the bridge holds one echo "
                   "suppression setting that lets your voice through; remove it from "
                   "local-audio.env")
MIC_GAIN_DB = setting("LOCAL_AUDIO_MIC_GAIN_DB")


_NAMES = {PP_ECHOONOFF: "suppressor", PP_NLATTENONOFF: "non-linear attenuation",
          PP_DTSENSITIVE: "double-talk sensitivity"}


def _words(held) -> str:
    """A suppressor set as a log line says it."""
    return ", ".join(f"{_NAMES[cmd]} {value}" for cmd, value in held)


def _get(dev, cmd: int, fmt: str = "<i"):
    (value,) = struct.unpack(fmt, xvf_led.read(dev, cmd, 4, resid=PP_RESID))
    return value


def _set(dev, cmd: int, value, fmt: str = "<i") -> None:
    xvf_led.write(dev, cmd, struct.pack(fmt, value), resid=PP_RESID)


class EchoSuppression(xvf_led.ChipSetting):
    """The chip's post-processing: its AGC off at a fixed gain, its suppressor at DUPLEX
    (apply)."""

    LOG = "mic echo suppression"

    def __init__(self, find=xvf_led.find_node, opener=xvf_led.UsbfsDevice,
                 enabled: bool = ECHO_CONTROL, gain_db: float = MIC_GAIN_DB,
                 clock=time.monotonic):
        super().__init__(find, opener, clock)
        self.enabled = enabled
        self.gain_db = gain_db
        self.gain = 10 ** (gain_db / 20)
        # The chip held DUPLEX and the fixed gain when last read or written: one found
        # otherwise under the same node was reset (logged), not first seen.
        self.held = False
        self._told = None  # the node whose tuning was logged: once per chip seen

    def apply(self) -> bool:
        """Read the chip back and write what differs from DUPLEX and the fixed gain, as
        the bridge does when it opens the card and every ECHO_CHECK_SECS while it is
        open: the chip may have reset to its saved values without re-enumerating. True
        once the chip holds them (or there is no chip, or the bridge is to leave it
        alone); False on an error (logged once). Blocking: a few ms of USB."""
        if not self.enabled:
            return True

        def change(node) -> bool:
            if node is None:
                return False
            with self._opener(node) as dev:
                self._change(dev)
            return True
        return self._run("set the mic's fixed gain and echo suppression", change)

    def _forget(self) -> None:
        self.held = False  # plugged in, out, or re-enumerated: a chip not yet set

    def _failed(self) -> None:
        # A part-write of ours cleared `held` before it began (_change), so it is not
        # taken for a reset. A failure before any write (a chip mid-reset failing a
        # read) leaves `held` as it was: the reset the next check finds is still a
        # reset, and logged as one.
        pass

    def _change(self, dev) -> None:
        found = tuple((cmd, _get(dev, cmd)) for cmd, _ in DUPLEX)
        agc = self._agc(dev)
        if found == DUPLEX and self._agc_fixed(agc):
            self._hold(found, agc)
            return
        if self.held:
            logger.warning(f"{self.LOG}: the chip no longer held what was set "
                           f"({_words(found)}, AGC on={agc[0]} gain={agc[1]:g}): "
                           "it was reset; setting it again")
        self.held = False  # part-written until it reads back whole
        if not self._agc_fixed(agc):
            _set(dev, PP_AGCONOFF, 0)
            _set(dev, PP_AGCGAIN, self.gain, "<f")
            on, gain = self._agc(dev)
            if not self._agc_fixed((on, gain)):
                raise OSError(f"AGC read back on={on} gain={gain:g}, "
                              f"wrote off at {self.gain:g}")
        if found != DUPLEX:
            for cmd, value in DUPLEX:
                _set(dev, cmd, value)
            got = tuple((cmd, _get(dev, cmd)) for cmd, _ in DUPLEX)
            if got != DUPLEX:
                raise OSError(f"read back {got}, wrote {DUPLEX}")
        self._hold(found, agc)

    def _hold(self, found, agc) -> None:
        self.held = True
        if self._told == self.node:
            return
        self._told = self.node
        if found == DUPLEX and self._agc_fixed(agc):
            was = "it already held them"
        elif found == FACTORY:
            was = "it was at its factory echo suppression"
        else:
            was = f"it was at {_words(found)}, AGC on={agc[0]} gain={agc[1]:g}"
        logger.info(f"mic: the XVF3800 set to a fixed {self.gain_db:+g} dB gain (its "
                    "automatic gain off) and to echo suppression that lets your voice "
                    f"through while the bot talks ({_words(DUPLEX)}); {was}")

    @staticmethod
    def _agc(dev) -> tuple[int, float]:
        return _get(dev, PP_AGCONOFF), _get(dev, PP_AGCGAIN, "<f")

    def _agc_fixed(self, agc: tuple[int, float]) -> bool:
        on, gain = agc
        return on == 0 and abs(gain - self.gain) <= 1e-3 * self.gain
