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
# or a remembered network gets its chance first -- and said twice, CONFIRM_SECS apart,
# each check given CHECK_SECS: taking an online box off its network for a slow answer or
# a moment of "limited" is the worst mistake here. A check that cannot answer is unknown,
# never offline. A first-boot setup that finds the box online meanwhile (Ethernet plugged
# in) leaves setup mode by itself (wifi_voice).
#
# No nagging. Each boot tries at most once: the boot's id goes in setup-tried as any
# setup starts (the owner's own, by voice, counts), held in the process too, so a brain
# restart or the next room session does not start it again; a cancel or a timeout leaves
# the box offline, says how to start it again ('say "set up Wi-Fi"'), and waits for the
# next boot -- while the box is still new.
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

from teaport_brain import wifi_voice
from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_SETUP_ON_FIRST_BOOT")
# Since boot: how long Ethernet or a remembered network has to come up before the box
# counts as offline.
GRACE_SECS = 60.0
# Each connectivity check's time (the phrase's own, wifi_voice.ONLINE_CHECK_SECS, is
# short: someone is waiting on it), the gap between the two readings that make offline,
# and how many readings before giving up on an answer.
CHECK_SECS = 10.0
CONFIRM_SECS = 10.0
READINGS = 6
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


# Tried in this process. setup-tried carries the try across brain restarts; this holds it
# whatever became of that file (not writable, no boot id to compare), so a wake-word room
# that opens a session after every sleep is never set up again on a loop.
_tried_here = False


def tried_this_boot() -> bool:
    if _tried_here:
        return True
    bid = boot_id()
    return bool(bid) and _read(_path("setup-tried")) == bid


def _mark_tried() -> None:
    global _tried_here
    _tried_here = True
    try:
        _write("setup-tried", boot_id() + "\n")
    except OSError as e:
        logger.warning(f"first boot: could not write {_path('setup-tried')}: {e} — "
                       "tried once in this process all the same")


def setup_started() -> None:
    """A Wi-Fi setup is starting, however it began (the phrase or the tool too): this
    boot's try is spent, so a setup the owner started and cancelled during the grace
    period is not followed by one nobody asked for."""
    if ENABLED and is_new() and not tried_this_boot():
        _mark_tried()


def uptime() -> float:
    try:
        return float(_read(_UPTIME).split()[0])
    except (IndexError, ValueError):
        return GRACE_SECS  # cannot tell: no wait


def could_be_due() -> bool:
    """Cheap, no waiting: on, new, and not tried this boot."""
    return ENABLED and is_new() and not tried_this_boot()




async def _check() -> str | None:
    """NetworkManager's connectivity, given longer than the phrase's check: a first look
    that is merely slow must not read as offline (wifi_voice.connectivity)."""
    return await wifi_voice.connectivity(timeout=CHECK_SECS)


async def _reading(connectivity, sleep, still=lambda: True) -> str | None:
    """What the box is, once the boot's grace period is over: "full"; "offline" -- two
    readings that say so (not full, and an answer), CONFIRM_SECS apart, so a moment of
    "limited" while a network comes up is not enough; None -- no connectivity check
    configured; "unknown" -- no two such readings within READINGS tries (a check that
    timed out, "", is read again, never taken for offline); "gone" -- `still()` turned
    False meanwhile."""
    left = GRACE_SECS - uptime()
    if left > 0:
        await sleep(left)
    offline = 0
    for _ in range(READINGS):
        if not still():
            return "gone"
        state = await connectivity()
        if state is None or state == "full":
            return state
        if state:
            offline += 1
            if offline >= 2:
                return "offline"
        await sleep(CONFIRM_SECS)
    return "unknown"


async def due(connectivity=None, sleep=asyncio.sleep, still=lambda: True) -> bool:
    """Should the box go into setup mode by itself now? On, new, not tried this boot, and
    offline (_reading) once the grace period is over. Online: the marker is written
    instead. Due: this boot is marked tried before anything is said, so nothing starts it
    twice."""
    if not could_be_due():
        return False
    state = await _reading(connectivity or _check, sleep, still)
    if state == "full":
        reached_online("online at boot")
        return False
    if state is None:
        logger.warning("first boot: NetworkManager has no connectivity check configured, "
                       "so offline cannot be told from online — not going into setup mode")
        return False
    if state != "offline":
        logger.info(f"first boot: connectivity {state} — not going into setup mode")
        return False
    if not could_be_due() or not still():  # set up, or tried, while it waited
        return False
    _mark_tried()
    logger.info(f"first boot: a new box, still offline {GRACE_SECS:g} s after boot — "
                "going into setup mode")
    return True


async def run(session) -> bool:
    """For a room session (its WifiSetupVoice: session.wifi): when due, wake it if it
    sleeps (WakeGate.wake, as a wake word would, without the greeting: the welcome is
    it) and go into setup mode with the welcome. True when it started. Runs as a task
    nobody awaits, so whatever goes wrong is logged here."""
    try:
        wifi = getattr(session, "wifi", None)
        if wifi is None or not await due(still=lambda: not _finished(session)):
            return False
        gate = getattr(session, "wake_gate", None)
        if gate is not None and gate.asleep:
            async def push(frame):
                await session.task.queue_frames([frame])
            await gate.wake(push, None, why="first-boot setup", greet=False)
        return await wifi.begin_first_boot()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a task nobody awaits: say what went wrong
        logger.exception("first boot: could not go into setup mode")
        return False


def _finished(session) -> bool:
    """The session ended while the grace period ran out: its try is left to the next."""
    return bool(getattr(session.task, "has_finished", lambda: False)())


async def watch(connectivity=None, sleep=asyncio.sleep) -> None:
    """While the box has never been online: look after the grace period and every
    WATCH_SECS, and write the marker the first time it is. Covers an install or a boot
    that is online, and Ethernet plugged in later, with no room session involved. Gives
    up when no connectivity check is configured (nobody can tell)."""
    if not ENABLED:
        return
    check = connectivity or _check
    while is_new():
        state = await _reading(check, sleep)
        if state == "full":
            reached_online("online")
            return
        if state is None:
            return
        await sleep(WATCH_SECS)
