#
# first_boot.py — a new, offline box goes into setup mode by itself (#278).
#
# An appliance is unboxed by someone with no SSH and no terminal. A box that is NEW (it
# has never been online) and OFFLINE at boot puts itself into setup mode and greets them:
# "Hi, I'm Teaport. Let's get me online. ..." then the usual setup-mode instructions --
# without a wake word and without the "do you want to set up Wi-Fi?" question (it is
# offline already: there is nothing to lose). Opt-in, TEAPORT_SETUP_ON_FIRST_BOOT (off in
# the open install; appliance provisioning turns it on). Inert without the room mic (the
# local audio bridge) and the setup unit: it runs only through a room session's spoken
# Wi-Fi setup (wifi_voice.WifiSetupVoice), and only where NetworkManager's connectivity
# check is configured (wifi_voice.connectivity: without it nobody can tell offline from
# online).
#
# NEW is a marker in the brain's state directory (~/.local/state/teaport, the XDG
# default for the run user): setup-done, written the first time the box is seen with full
# connectivity -- by the watcher below (an install or boot that is online, Ethernet
# plugged in later), or by a setup that connects. Never removed by the brain. An image
# that ships must not carry it.
#
# OFFLINE is connectivity not "full" once GRACE_SECS have passed since boot, so Ethernet
# or a remembered network gets its chance first.
#
# No nagging. Each boot tries at most once: the boot's id goes in setup-tried as the
# setup starts, so a brain restart does not start it again, and a cancel or a timeout
# leaves the box offline, says how to start it again ('say "set up Wi-Fi"'), and waits for
# the next boot -- while the box is still new.
#
# WHERE IT LIVES. In the brain, as one more way into the room session's Wi-Fi setup
# (beside the phrase and the wifi_setup tool), started when the room's /talk session
# comes up (gateway_server). Not a systemd unit: a unit could start the setup network but
# could not speak, and the spoken lines are the room session's (the voice follows only a
# setup it started). Not the local audio bridge: it is an audio pipe with no setup logic,
# and the session it opens is already where wake words, setup and the face meet. The
# marker is a plain file in the run user's state directory, the brain's own, so the
# installer and the units need nothing new.
#
import asyncio
import os
import time

from loguru import logger
from pipecat.frames.frames import OutputTransportMessageUrgentFrame

from teaport_brain import wifi_voice
from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_SETUP_ON_FIRST_BOOT")
# Since boot: how long Ethernet or a remembered network has to come up before the box
# counts as offline.
GRACE_SECS = 60.0
# How often the watcher looks again while the box has never been online.
WATCH_SECS = 30.0
_BOOT_ID = "/proc/sys/kernel/random/boot_id"
_UPTIME = "/proc/uptime"


def state_dir() -> str:
    # The XDG state directory's default place (the unit sets no XDG_STATE_HOME).
    base = os.path.expanduser("~/.local/state")
    return os.path.join(base, "teaport")


def _path(name: str) -> str:
    return os.path.join(state_dir(), name)


def is_new() -> bool:
    """Never seen online (no setup-done marker)."""
    return not os.path.exists(_path("setup-done"))


def _write(name: str, text: str) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, _path(name))


def reached_online(why: str) -> None:
    """The box is online, for the first time or not: it is not new any more. Only with
    the setting on (otherwise nothing reads the marker, and nothing is written)."""
    if not ENABLED or not is_new():
        return
    try:
        _write("setup-done", time.strftime("%Y-%m-%dT%H:%M:%S%z") + f" {why}\n")
        logger.info(f"first boot: online ({why}) — this box is set up; it will not go "
                    "into setup mode by itself again")
    except OSError as e:
        logger.warning(f"first boot: could not write {_path('setup-done')}: {e}")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def boot_id() -> str:
    return _read(_BOOT_ID)


def tried_this_boot() -> bool:
    bid = boot_id()
    return bool(bid) and _read(_path("setup-tried")) == bid


def _mark_tried() -> None:
    try:
        _write("setup-tried", boot_id() + "\n")
    except OSError as e:
        logger.warning(f"first boot: could not write {_path('setup-tried')}: {e}")


def uptime() -> float:
    try:
        return float(_read(_UPTIME).split()[0])
    except (IndexError, ValueError):
        return GRACE_SECS  # cannot tell: no wait


def could_be_due() -> bool:
    """Cheap, no waiting: on, new, and not tried this boot."""
    return ENABLED and is_new() and not tried_this_boot()


async def _settled(connectivity, sleep) -> str | None:
    """Connectivity once the boot's grace period is over."""
    left = GRACE_SECS - uptime()
    if left > 0:
        await sleep(left)
    return await connectivity()


async def due(connectivity=None, sleep=asyncio.sleep, still=lambda: True) -> bool:
    """Should the box go into setup mode by itself now? On, new, not tried this boot, and
    offline once the grace period is over. Online: the marker is written instead. Due:
    this boot is marked tried before anything is said, so nothing starts it twice."""
    if not could_be_due():
        return False
    state = await _settled(connectivity or wifi_voice.connectivity, sleep)
    if state == "full":
        reached_online("online at boot")
        return False
    if state is None:
        logger.warning("first boot: NetworkManager has no connectivity check configured, "
                       "so offline cannot be told from online — not going into setup mode")
        return False
    if not could_be_due() or not still():  # set up, tried, or ended while it waited
        return False
    _mark_tried()
    logger.info(f"first boot: a new box, offline {GRACE_SECS:g} s after boot (connectivity "
                f"{state or 'unknown'}) — going into setup mode")
    return True


async def run(session) -> bool:
    """For a room session (its WifiSetupVoice: session.wifi): when due, wake it if it
    sleeps (no wake word needed: the bridge is told as for one) and go into setup mode
    with the welcome. True when it started."""
    wifi = getattr(session, "wifi", None)
    if wifi is None or not await due(still=lambda: not _finished(session)):
        return False
    gate = getattr(session, "wake_gate", None)
    if gate is not None and gate.asleep:
        gate.asleep, gate.woke = False, True
        await session.task.queue_frames([OutputTransportMessageUrgentFrame(
            message={"type": "wake", "phrase": "(first boot)", "greeted": False,
                     "resumed": False})])
    return await wifi.begin_first_boot()


def _finished(session) -> bool:
    """The session ended while the grace period ran out: its try is left to the next."""
    return bool(getattr(session.task, "has_finished", lambda: False)())


async def watch(connectivity=None, sleep=asyncio.sleep) -> None:
    """While the box has never been online: look after the grace period and every
    WATCH_SECS, and write the marker the first time it is. Covers an install or a boot
    that is online, and Ethernet plugged in later, with no room session involved."""
    if not ENABLED:
        return
    check = connectivity or wifi_voice.connectivity
    while is_new():
        if await _settled(check, sleep) == "full":
            reached_online("online")
            return
        await sleep(WATCH_SECS)
