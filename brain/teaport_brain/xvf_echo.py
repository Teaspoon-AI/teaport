#
# xvf_echo.py — the XVF3800's echo suppression, set by how loud the bot's echo is
# against the user's voice, as measured at the mic.
#
# The room mic's channel 0 (LOCAL_AUDIO_CAPTURE_CHANNEL) is the chip's conversation mix:
# echo-cancelled, then post-processed. As shipped (firmware 2.0.6) the post-processing
# suppresses residual echo hard (PP_ECHOONOFF and PP_NLATTENONOFF on, PP_DTSENSITIVE 0)
# -- and while the bot speaks it suppresses the USER with it -- and its AGC (up to +36
# dB) lifts whatever is left, echo included. Controlled takes on the appliance,
# 2026-10-10/11 (a story through the speaker, five questions asked over it, scored
# against Whisper and Deepgram; "gap" is EchoMeter's, below):
#
#   setting                              where   engine heard   echo as speech    gap
#   factory, volume 30 or 80             near    1 of 5         none               --
#   suppressor off, AGC on, volume 50    near    5 of 5         none              30 dB
#   suppressor off, AGC off, volume 80   near    4 of 5         one word          27 dB
#   suppressor off, AGC on, volume 65    near    5 of 5         words prefixed    20 dB
#   suppressor off, AGC off, volume 80   3-4 m   3 of 5         a word, 2 onsets  18 dB
#
# From volume 50 to 65 the echo on the AGC-free channel rose 2 dB and on channel 0 14:
# the AGC, not the speaker, made the leak. And the same setting is clean near the box
# and leaks from across the room: what decides it is how far the user's voice stands
# above the echo, which no volume threshold can know. So:
#
#   * The chip's AGC is off, at a fixed gain (MIC_GAIN_DB): nothing lifts the echo in a
#     pause, and the levels below are on one scale.
#   * The suppressor is off (DUPLEX: the user is heard over the bot) unless EchoMeter
#     finds the user's voice less than MARGIN_DB above the echo; then the chip's own
#     suppression (its values as first read from it; FACTORY if it was found in DUPLEX,
#     a bridge that stopped with it off) is on until the gap is back over MARGIN_DB plus
#     HYSTERESIS_DB. The brain's echo guard (echo_guard.py) takes what leaks either way.
#
# Written at run time only (never SAVE_CONFIGURATION): a replug or a chip reset brings
# its saved values back, and the bridge writes again when it opens the card. Each set is
# written whole and read back. The transport is xvf_led's (USB vendor control transfers
# on endpoint 0, the PP resource 17); no device, no Linux, nothing done; never raises; a
# failure is logged once until it works again. ECHO_CONTROL off: the chip is not touched.
#
import collections
import math
import struct
import threading
import time

from loguru import logger

from teaport_brain import xvf_led
from teaport_brain.settings import setting

PP_RESID = 17
PP_AGCONOFF, PP_AGCGAIN = 10, 13
PP_ECHOONOFF, PP_NLATTENONOFF, PP_DTSENSITIVE = 23, 27, 31

# (command, value) in write order; read back the same. FACTORY: firmware 2.0.6 as shipped.
FACTORY = ((PP_ECHOONOFF, 1), (PP_NLATTENONOFF, 1), (PP_DTSENSITIVE, 0))
DUPLEX = ((PP_ECHOONOFF, 0), (PP_NLATTENONOFF, 0), (PP_DTSENSITIVE, 5))

ECHO_CONTROL = setting("LOCAL_AUDIO_ECHO_CONTROL")
MIC_GAIN_DB = setting("LOCAL_AUDIO_MIC_GAIN_DB")
MARGIN_DB = setting("LOCAL_AUDIO_ECHO_MARGIN_DB")
HYSTERESIS_DB = 3.0


def write_i32(dev, cmd: int, value: int) -> None:
    dev.control(xvf_led.VENDOR_OUT, cmd, PP_RESID, struct.pack("<i", value))


def read_i32(dev, cmd: int) -> int:
    (value,) = struct.unpack("<i", xvf_led.read(dev, cmd, 4, resid=PP_RESID))
    return value


def write_f32(dev, cmd: int, value: float) -> None:
    dev.control(xvf_led.VENDOR_OUT, cmd, PP_RESID, struct.pack("<f", value))


def read_f32(dev, cmd: int) -> float:
    (value,) = struct.unpack("<f", xvf_led.read(dev, cmd, 4, resid=PP_RESID))
    return value


class EchoSuppression:
    """The chip's post-processing: its AGC fixed, its suppressor off or on (apply)."""

    def __init__(self, find=xvf_led.find_node, opener=xvf_led.UsbfsDevice,
                 enabled: bool = ECHO_CONTROL, gain_db: float = MIC_GAIN_DB):
        self._find, self._opener = find, opener
        self.enabled = enabled
        self.gain = 10 ** (gain_db / 20)
        self.node = None       # the device's usbfs node when last written
        self.own = None        # that chip's own suppressor values, read when first seen
        self.shown = None      # the suppressor set it holds as last written or read
        self._failing = False
        # apply() runs on worker threads: two must not interleave their writes.
        self._lock = threading.Lock()

    def apply(self, duplex) -> bool:
        """The suppressor off (`duplex`, or a function returning it, read once the lock
        is held: the meter's verdict as it is when the write runs, not as it was queued)
        or the chip's own. True once the chip holds it (or there is no chip, or the
        bridge is to leave it alone); False on an error (logged once). Blocking: a few
        ms of USB."""
        if not self.enabled:
            return True
        with self._lock:
            return self._apply(duplex() if callable(duplex) else duplex)

    def _apply(self, duplex: bool) -> bool:
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
                    # The AGC off at a fixed gain, once per chip (see the module).
                    write_i32(dev, PP_AGCONOFF, 0)
                    write_f32(dev, PP_AGCGAIN, self.gain)
                    agc, gain = read_i32(dev, PP_AGCONOFF), read_f32(dev, PP_AGCGAIN)
                    if agc != 0 or abs(gain - self.gain) > 1e-3 * self.gain:
                        raise OSError(f"AGC read back on={agc} gain={gain:g}, "
                                      f"wrote off at {self.gain:g}")
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
                logger.warning(f"mic echo suppression: could not set it {name}: {e!r}"
                               + (" (needs the installer's udev rule and the teaport-hw group)"
                                  if isinstance(e, PermissionError) else ""))
                self._failing = True
            return False
        self._failing = False
        self.shown = want
        return True


# --- the meter ---

BLOCK_CHUNKS = 5             # 20 ms chunks per measured block (100 ms)
REF_VOICED_DB = -70.0        # a block of playback this loud is the bot talking
VOICE_OVER_FLOOR_DB = 12.0   # a block with the bot quiet this far over the room's floor
                             # is someone talking
COUPLING_PCT, LOUD_PCT, FLOOR_PCT = 30, 90, 20
# Blocks kept, and the fewest a verdict needs: the echo's coupling over the last minute
# of the bot talking (5 s at least), the voice over the last 30 s of it (2 s at least).
COUPLING_KEEP, COUPLING_MIN = 600, 50
VOICE_KEEP, VOICE_MIN = 300, 20
REF_KEEP, FLOOR_KEEP = 300, 300
# The suppressor is not switched more often than this...
DWELL_SECS = 10.0
# ...and once on, it is turned off again after this to measure the echo anew: on, the
# coupling cannot be measured (it would measure the suppressor), so a reading thrown by
# a user who talked over most of the bot, or a speaker moved since, would hold it on.
RECHECK_SECS = 300.0


def _power_mean_db(dbs: list[float]) -> float:
    return 10 * math.log10(sum(10 ** (d / 10) for d in dbs) / len(dbs) + 1e-18)


def _pct(values, q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q / 100
    lo = int(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


class EchoMeter:
    """How far the user's voice stands above the bot's echo at the mic (gap), and from
    it whether the suppressor should be off (duplex).

    Fed every capture chunk with the mic's level, the level of the playback heard at
    that moment (as synthesized, before the volume; None when nothing was due), and
    whether the bot is quiet; `gain_db` is the volume's gain now. In 100 ms blocks:

      * the bot talking: the echo's COUPLING, mic minus playback. Its 30th percentile:
        the user talking over the bot raises some blocks and not the low ones -- over a
        whole take, the user's lines included, it ranked the takes as their leaks did,
        and in simulation it holds while the user talks over less than about half of
        the bot. Only while the suppressor is off: on, it would measure the suppressor
        (so after RECHECK_SECS on, it is turned off to measure again).
      * the bot talking: the playback's loud level (90th percentile); the echo's loud
        level is that plus the coupling.
      * the bot quiet: the room's floor (20th percentile), and the user's loud level
        (90th percentile of the blocks VOICE_OVER_FLOOR_DB over it).

    gap = the voice's loud level - the echo's. The playback is kept as synthesized and
    the volume applied when the gap is taken, so a volume change moves the gap at once;
    a user across the room moves the voice's level, and so the gap, as they speak."""

    def __init__(self, gain_db=lambda: 0.0, margin_db: float = MARGIN_DB,
                 clock=time.monotonic):
        self.margin = margin_db
        self._gain_db = gain_db
        self._clock = clock
        self.duplex = True
        self._since = None
        self._mic, self._ref, self._quiet = [], [], True
        self.coupling = collections.deque(maxlen=COUPLING_KEEP)
        self.ref = collections.deque(maxlen=REF_KEEP)
        self.voice = collections.deque(maxlen=VOICE_KEEP)
        self.floor = collections.deque(maxlen=FLOOR_KEEP)
        self.gap: float | None = None
        self.recheck = False   # the last change was a recheck, not a measured verdict

    def feed(self, mic_db: float, ref_db: float | None, bot_quiet: bool) -> bool:
        """One capture chunk. True when the verdict (duplex) changed with it."""
        self._mic.append(mic_db)
        if ref_db is not None:
            self._ref.append(ref_db)
        self._quiet = self._quiet and bot_quiet
        if len(self._mic) < BLOCK_CHUNKS:
            return False
        mic = _power_mean_db(self._mic)
        ref = _power_mean_db(self._ref) if self._ref else None
        quiet = self._quiet
        self._mic, self._ref, self._quiet = [], [], True
        return self._block(mic, ref, quiet)

    def _block(self, mic: float, ref: float | None, quiet: bool) -> bool:
        gain = self._gain_db()
        if ref is not None and ref + gain > REF_VOICED_DB:
            self.ref.append(ref)
            if self.duplex:
                self.coupling.append(mic - (ref + gain))
        elif quiet:
            self.floor.append(mic)
            if len(self.floor) >= VOICE_MIN and mic > _pct(self.floor, FLOOR_PCT) + VOICE_OVER_FLOOR_DB:
                self.voice.append(mic)
        if (len(self.coupling) < COUPLING_MIN or len(self.voice) < VOICE_MIN
                or len(self.ref) < VOICE_MIN):
            return False
        if gain == -math.inf:
            return False  # silent: no echo, nothing to decide
        echo = _pct(self.ref, LOUD_PCT) + gain + _pct(self.coupling, COUPLING_PCT)
        self.gap = _pct(self.voice, LOUD_PCT) - echo
        now = self._clock()
        if not self.duplex and now - self._since >= RECHECK_SECS:
            self.duplex, self._since, self.recheck = True, now, True
            self.coupling.clear()
            return True
        if self._since is not None and now - self._since < DWELL_SECS:
            return False
        want = self.gap >= self.margin + (0 if self.duplex else HYSTERESIS_DB)
        if want == self.duplex:
            return False
        self.duplex, self._since, self.recheck = want, now, False
        return True
