#
# first_boot.py — a new, offline box goes into setup mode by itself (#278).
#
# An appliance is unboxed by someone with no SSH and no terminal. A box that is NEW (it
# has never been online) and OFFLINE at boot puts itself into setup mode and greets them:
# "Hi, I'm Teaport. Let's get me online. ..." then the usual setup-mode instructions --
# without a wake word and without the "do you want to set up Wi-Fi?" question (it is
# offline already: there is nothing to lose). Opt-in, TEAPORT_SETUP_ON_FIRST_BOOT (off in
# the open install; appliance provisioning turns it on). Inert without the setup unit,
# and where NetworkManager's connectivity check is not configured (wifi_voice.connectivity:
# without it nobody can tell offline from online). With the room mic (the local audio
# bridge) it runs as a room session's spoken Wi-Fi setup (wifi_voice.WifiSetupVoice);
# without one, the setup network starts with no voice and its screen shows the way (#457).
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
# WHERE IT LIVES. In the brain: drive(), its own task from start-up (gateway_server),
# decides, then hands the setup to the room's session -- one more way into its Wi-Fi
# setup, beside the phrase and the tool -- asking the bridge for one if none is up (#457).
# Not a systemd unit: a unit could start the setup network but could not speak, and the
# spoken lines are the room session's (the voice follows only a setup it started). Not the
# local audio bridge: an audio pipe with no setup logic, which dials when it is asked. The
# marker is a plain file in the run user's state directory, the brain's own, so the
# installer and the units need nothing new.
#
import asyncio
import os
import time

from loguru import logger

from teaport_brain import wifi_voice
from teaport_brain import settings
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
    return settings.state_dir()  # ~/.local/state/teaport


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
    """For a room session past its STT check (its WifiSetupVoice: session.wifi): it is
    where first boot's setup can go (_room), until drive() hands it over -- then wake it
    if it sleeps (WakeGate.wake, as a wake word would, without the greeting: the welcome
    is it) and go into setup mode with the welcome. True when it started. Runs as a task
    nobody awaits, cancelled with the session, so whatever goes wrong is logged here;
    drive() hears how it went (_Room.answer) whatever happens."""
    wifi = getattr(session, "wifi", None)
    if wifi is None:
        return False
    room = _Room(session)
    _offer(room)
    began = False
    try:
        await room.turn
        if _finished(session):
            return False
        gate = getattr(session, "wake_gate", None)
        if gate is not None and gate.asleep:
            async def push(frame):
                await session.task.queue_frames([frame])
            await gate.wake(push, None, why="first-boot setup", greet=False)
        # False: the owner's own setup is asking or running here: drive() leaves it to
        # them (BUSY), the boot's try spent as by any setup they start.
        began = True
        started = await wifi.begin_first_boot()
        room.answered(STARTED if started else BUSY)
        return started
    except asyncio.CancelledError:
        # The session ended mid-start: the start runs on (WifiSetupVoice._shielded), so
        # the unit is on its way up -- not a session that went before it began.
        if began:
            room.answered(STARTED)
        raise
    except Exception:  # noqa: BLE001 — a task nobody awaits: say what went wrong
        logger.exception("first boot: could not go into setup mode")
        room.answered(FAILED)
        return False
    finally:
        room.answered(GONE)  # a no-op once answered
        _withdraw(room)


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


# The brain asking for the room (#457). Without wake words the local audio bridge
# dials once at start and then only when it hears a voice, and nobody talks to a box
# fresh out of its packaging -- so a first session that ended early (the engine still
# loading at boot) left it waiting, and setup mode never began. While this is set, the
# brain wants the bridge to open a session by itself: /talk/status carries it as
# "wanted" ({"why": ..., "id": ...}) while nothing holds the engine, and the bridge,
# waiting for a voice, dials once per id (local_audio.Asks), with no voice. Its rules:
#   * Each want_room() is a new ask, dialled once more: call it once per attempt, not
#     on every tick of a loop (each would be a dial).
#   * The bridge takes an ask as it dials. A dial the brain refused (a Talk session or a
#     call took the box meanwhile), or a session that ended before it began (its STT
#     was not up), spends it: the brain asks again (a new id) if it still wants the
#     room. Only a dial that never reached the brain is retried by the bridge itself.
#   * room_answered() once a room session has really started (past its STT check), or
#     the brain no longer wants one; a brain restart starts from none.
_wanted: dict | None = None


def want_room(why: str) -> str:
    """Ask the room's bridge to open a session (a new ask, dialled once); returns the
    ask's id."""
    import uuid
    global _wanted
    _wanted = {"why": why, "id": uuid.uuid4().hex}
    return _wanted["id"]


def room_answered() -> None:
    """A room session came, or nobody needs one any more: the ask is over."""
    global _wanted
    _wanted = None


def room_wanted() -> dict | None:
    """The brain's ask for the room (/talk/status "wanted"), or None."""
    return dict(_wanted) if _wanted else None


# Who starts first boot (#457). drive() -- a task of the brain's own from start-up
# (gateway_server's lifespan) -- is the only one that decides (due()). Once it is due, it
# waits for the engine (a room session needs its STT: the bridge's first session at boot
# ended for want of it), then hands the setup to the room's session: the one up already
# (a wake-word room asleep, a room nobody has spoken to yet), or one it asks the bridge
# for (want_room), up to ROOM_ASKS asks of ROOM_WAIT_SECS each. The session takes it in
# its own task (run: the setup's lines are that session's), so drive() only hands over.
# No room session took it: the box has no room mic that answers (no card, no bridge, a
# room without spoken setup), and the setup network starts without a voice
# (_start_headless): its screen shows the network's name, password and code
# (wifi_setup.py), and its own time limit puts things back -- unless a conversation or a
# call holds the box at that moment (a LAN with no internet reads as offline too), which
# it would cut off its network unannounced: then not before the brain starts again. Still
# one try a boot: due() marked this one tried before
# any of this. drive() looks once per start of the brain; a look that cannot tell
# ("unknown": every check timed out) waits for the next.
ENGINE_WAIT_SECS = 300.0
ENGINE_POLL_SECS = 5.0
ROOM_WAIT_SECS = 45.0
ROOM_ASKS = 3
# How long a room session may take to say the welcome and start the unit
# (begin_first_boot) before drive() stops waiting -- and stops: it may have started.
HANDOVER_SECS = 120.0
# How long drive() waits out the slot changing hands before it looks (_settled): an end
# takes up to session_arbiter.END_WAIT_SECS, a resume its "where were we?".
SETTLE_WAIT_SECS = 30.0
SETTLE_POLL_SECS = 0.25
# How a room session answered the hand-over (_Room.answer).
STARTED, BUSY, GONE, FAILED = "started", "busy", "gone", "failed"


class _Room:
    """A room session first boot's setup can go to (run), and the hand-over: drive()
    resolves `turn`, the session `answer`."""

    def __init__(self, session):
        loop = asyncio.get_running_loop()
        self.session = session
        self.turn: asyncio.Future = loop.create_future()
        self.answer: asyncio.Future = loop.create_future()

    def live(self) -> bool:
        return not self.answer.done() and not _finished(self.session)

    def answered(self, how: str) -> None:
        if not self.answer.done():
            self.answer.set_result(how)


_room: _Room | None = None
_room_came: asyncio.Event | None = None  # drive()'s, while it waits for a room session
_driving = False
# drive() has been, in this process: its one look decided this boot (a look that could
# not tell, "unknown", is not taken again before the next start of the brain).
_driven = False


def _offer(room: _Room) -> None:
    global _room
    _room = room
    room_answered()  # a room session came: an ask for one is answered
    if _room_came is not None:
        _room_came.set()


def _withdraw(room: _Room) -> None:
    global _room
    if _room is room:
        _room = None


def listening() -> bool:
    """Should a room session offer itself to first boot (run)? While first boot could be
    due, and while drive() is at work (which has marked this boot tried by then): not
    once it is done, so no room session that comes after waits on a hand-over that will
    not come (one that came before waits until it ends, cancelled with it)."""
    return ENABLED and is_new() and (_driving or not (_driven or tried_this_boot()))


async def drive(connectivity=None, sleep=asyncio.sleep, engine_up=None,
                headless=None, busy=None, taken=None) -> bool:
    """First boot, from the brain's start-up: when due, hand the setup to a room session,
    or start it with no voice (above) -- never under a conversation or a call: the setup
    network would take the box off the network they are on. The voiceless start needs
    no conversation at all on the box (`busy`); the room session's, none but its own
    (`taken`): a room in its own conversation says the welcome itself, while one on hold
    for a call would play it into the hold and start the network under the call. Each
    looks once, after the slot has stopped changing hands (_settled), and a busy box
    means not before the brain starts again. True when this started setup mode, either
    way. Runs as a task nobody awaits, so whatever
    goes wrong is logged here."""
    global _driving, _room_came, _driven
    if not could_be_due():
        return False
    if not wifi_voice.available():
        logger.info("first boot: no Wi-Fi setup unit on this box — nothing to start")
        _driven = True
        return False
    _driving, _room_came = True, asyncio.Event()
    try:
        if not await due(connectivity, sleep):
            return False
        await _engine(engine_up or _engine_answers, sleep)
        for _ in range(ROOM_ASKS):
            if not is_new():
                return False  # online meanwhile (watch() wrote the marker)
            room = await _a_room()
            if room is None:
                continue
            if not await _settled(sleep):
                return False
            if not is_new():
                return False  # online while the room session came, or the slot settled
            if (taken or _taken_by_another)():
                logger.info("first boot: a call or another conversation has the box — not "
                            "handing the room session the setup")
                return False
            how = await _hand_over(room)
            if how != GONE:
                return how == STARTED
        room_answered()  # no more asks: a late dial would open a session nobody waits for
        if not await _settled(sleep) or not is_new():
            return False
        if (busy or _box_busy)():
            logger.info("first boot: no room session took the setup, and the box is in a "
                        "conversation or a call — not taking it off its network")
            return False
        return await (headless or _start_headless)()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a task nobody awaits: say what went wrong
        logger.exception("first boot: could not go into setup mode")
        return False
    finally:
        _driving, _room_came, _driven = False, None, True
        room_answered()


def _box_busy() -> bool:
    """A conversation is live (a room session in use, Talk, a call on hold) or a call is
    in or asking (session_arbiter)."""
    from teaport_brain import session_arbiter as arb
    status = arb.ARBITER.status()
    return bool(status["live"] or status["call"])


async def _settled(sleep) -> bool:
    """Wait out the slot changing hands (session_arbiter's settling: a sleeping room being
    ended for Talk, a conversation going on hold or coming back from it), when status()
    shows the before or the after. False when it has not within SETTLE_WAIT_SECS."""
    waited = 0.0
    while _settling():
        if waited >= SETTLE_WAIT_SECS:
            logger.info(f"first boot: the box is still changing conversations after "
                        f"{SETTLE_WAIT_SECS:g} s — taking it as busy")
            return False
        await sleep(SETTLE_POLL_SECS)
        waited += SETTLE_POLL_SECS
    return True


def _settling() -> bool:
    from teaport_brain import session_arbiter as arb
    return arb.ARBITER.settling()


def _taken_by_another() -> bool:
    """Something other than the room's own session has the box: a call in or asking, a
    conversation on hold for one, or a live Talk session (session_arbiter)."""
    from teaport_brain import session_arbiter as arb
    status = arb.ARBITER.status()
    return bool(status["call"] or status["held"]
                or (status["live"] and status["holder"] != arb.ROOM))


async def _engine(up, sleep) -> bool:
    """Wait for the speech engine to answer: ENGINE_WAIT_SECS of waiting between looks at
    most (each look takes up to a second more). A room session without its STT ends
    before it begins. Late, it goes on all the same (a box without a room mic needs no
    engine for its setup network)."""
    waited = 0.0
    while not await up():
        if waited >= ENGINE_WAIT_SECS:
            logger.warning(f"first boot: the speech engine is not answering after "
                           f"{ENGINE_WAIT_SECS:g} s — going on without it")
            return False
        await sleep(ENGINE_POLL_SECS)
        waited += ENGINE_POLL_SECS
    return True


async def _engine_answers() -> bool:
    """The engine answers its voice list (engine_tts.served_voices: kept once it has)."""
    from teaport_brain import engine_tts
    return await asyncio.to_thread(engine_tts.served_voices, 1.0, True) is not None


async def _a_room() -> _Room | None:
    """The room session that is up, or the one the bridge opens when asked (want_room)
    within ROOM_WAIT_SECS; None when none came, or it went again at once."""
    assert _room_came is not None
    _room_came.clear()
    if _room is not None and _room.live():
        return _room
    want_room("first-boot setup")
    logger.info("first boot: asking the room mic for a session")
    try:
        await asyncio.wait_for(_room_came.wait(), ROOM_WAIT_SECS)
    except asyncio.TimeoutError:
        logger.info(f"first boot: no room session came within {ROOM_WAIT_SECS:g} s")
        return None
    return _room if _room is not None and _room.live() else None


async def _hand_over(room: _Room) -> str:
    """Give the room session the setup (run) and hear how it went (STARTED, BUSY, GONE,
    FAILED). One that does not say within HANDOVER_SECS counts as FAILED: it may have
    started, and a second start would only fight it."""
    if not room.turn.done():
        room.turn.set_result(None)
    try:
        how = await asyncio.wait_for(asyncio.shield(room.answer), HANDOVER_SECS)
    except asyncio.TimeoutError:
        logger.warning("first boot: the room session has not said how its setup went — "
                       "leaving it to it")
        return FAILED
    if how == GONE:
        logger.info("first boot: the room session ended before its setup began")
    return how


def _box_language() -> str:
    """The catalog the box's own voice speaks (TTS_LANGUAGE, else its TTS_VOICE): the
    language the setup network's screens begin in when no session hands them one."""
    from teaport_brain import engine_tts, i18n
    espeak = engine_tts._resolve_lang(setting("TTS_LANGUAGE"), setting("TTS_VOICE"))[1]
    return i18n.for_espeak(espeak).lang


async def _start_headless() -> bool:
    """The setup network with no voice to go with it: its screen shows the way
    (wifi_setup.py), and its own time limit puts things back. True when it started."""
    logger.info("first boot: no room session took the setup — starting the setup "
                "network without a voice; the screen shows the way")
    from teaport_brain.wifi_setup import hand_language
    try:
        await asyncio.to_thread(hand_language, _box_language())
    except OSError as e:
        logger.warning(f"first boot: could not hand the screens their language: {e}")
    rc, err = await wifi_voice._run_helper("restart", wifi_voice.UNIT)
    if rc != 0:
        logger.warning(f"first boot: could not start {wifi_voice.UNIT}: {err}")
        return False
    return True
