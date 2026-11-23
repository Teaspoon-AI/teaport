"""A new, offline box goes into setup mode by itself (#278, first_boot.py): the four
cases of the issue -- new and offline: setup mode with the welcome, no wake word; new and
online: the marker, quiet; set up and offline: nothing; cancelled, then rebooted: setup
again while still new -- plus what keeps it from nagging or from guessing (one try a
boot, no connectivity check, the setting off), the watcher that writes the marker, and
the room conversation kept awake while setup runs (the bridge's "working")."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
from pinned_pipecat import require_pinned  # noqa: E402
from tempdirs import tempdir  # noqa: E402

require_pinned()

from pipecat.frames.frames import OutputTransportMessageUrgentFrame  # noqa: E402

import test_wifi_voice as wvt  # noqa: E402
from teaport_brain import first_boot, i18n, wifi_voice as wv  # noqa: E402
from teaport_brain.wake_gate import MicConversation, WakeGate  # noqa: E402

EN = i18n.get("en")


@pytest.fixture
def box(monkeypatch):
    """A box: its state directory, its boot id and uptime, the setting on."""
    root = tempdir("teaport-firstboot-")
    monkeypatch.setenv("HOME", os.path.join(root, "home"))
    boot, up = os.path.join(root, "boot_id"), os.path.join(root, "uptime")
    monkeypatch.setattr(first_boot, "_BOOT_ID", boot)
    monkeypatch.setattr(first_boot, "_UPTIME", up)
    monkeypatch.setattr(first_boot, "ENABLED", True)
    monkeypatch.setattr(first_boot, "CONFIRM_SECS", 0.01)
    monkeypatch.setattr(first_boot, "_tried_here", False)
    monkeypatch.setattr(wv, "POLL_SECS", 0.01)
    monkeypatch.setattr(wv, "LIVENESS_SECS", 0.05)
    # The setup unit is "installed" (first_boot.drive), the driver's waits are short, and
    # nothing is left from another test's driver or room session (another event loop's).
    monkeypatch.setattr(wv, "available", lambda: True)
    monkeypatch.setattr(first_boot, "ROOM_WAIT_SECS", 0.05)
    monkeypatch.setattr(first_boot, "HANDOVER_SECS", 2.0)
    for name, value in (("_room", None), ("_room_came", None), ("_driving", False),
                        ("_driven", False), ("_wanted", None)):
        monkeypatch.setattr(first_boot, name, value)
    monkeypatch.setattr(first_boot, "_box_busy", lambda: False)
    monkeypatch.setattr(first_boot, "_taken_by_another", lambda: False)
    monkeypatch.setattr(first_boot, "_settling", lambda: False)

    boots = iter(range(1, 1000))

    def reboot(boot_id=None, uptime=120.0):
        """A new boot: its id and uptime, and a brain process that has tried nothing."""
        with open(boot, "w") as f:
            f.write((boot_id or f"boot-{next(boots)}") + "\n")
        with open(up, "w") as f:
            f.write(f"{uptime} 0.0\n")
        first_boot._tried_here = first_boot._driven = False
    reboot()
    return SimpleNamespace(reboot=reboot, root=root)


class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


def _room(asleep=True):
    """A room session as first_boot.run sees it: its Wi-Fi setup (faked unit), its wake
    gate, its task."""
    path = wvt._path()
    session = SimpleNamespace(wifi=wvt.Voice(path), task=_Task(),
                              wake_gate=WakeGate(["hey teaport"], asleep=asleep,
                                                 store=MicConversation()))
    return session, path


def _online(*states):
    """A connectivity check answering `states` in turn (the last one from then on)."""
    async def check(timeout=None):
        check.asked += 1
        check.timeouts.append(timeout)
        return states[min(check.asked, len(states)) - 1]
    check.asked, check.timeouts = 0, []
    return check


def _messages(voice, kind):
    return [f.message for f in voice.pushed if isinstance(f, OutputTransportMessageUrgentFrame)
            and f.message.get("type") == kind]


async def _engine_up():
    return True


async def _run(session, *states, headless=None):
    """First boot as the brain runs it, with the connectivity check faked: `session` is
    the room session up (gateway_server starts first_boot.run on it while
    first_boot.listening()), and first_boot.drive decides and hands it the setup. True
    when the setup began on `session`. No voiceless start unless `headless` is given (an
    async fake): `_run.headless` counts the starts asked for."""
    orig = wv.connectivity
    wv.connectivity = check = _online(*states)
    _run.check, _run.headless = check, 0

    async def no_room():
        _run.headless += 1
        return headless is not None and await headless()
    took = asyncio.ensure_future(first_boot.run(session)) if first_boot.listening() else None
    try:
        await asyncio.sleep(0)  # it offers itself, as a session past its STT check does
        await first_boot.drive(engine_up=_engine_up, headless=no_room)
        if took is None or not took.done():
            return False
        return took.result()
    finally:
        wv.connectivity = orig
        if took is not None and not took.done():
            took.cancel()
            await asyncio.gather(took, return_exceptions=True)


# ---- the four cases ---------------------------------------------------------------------

def test_new_and_offline_goes_into_setup_mode_with_the_welcome_and_no_wake_word(box):
    async def run():
        session, path = _room(asleep=True)
        assert await _run(session, "none")
        v = session.wifi
        # Awake without a wake word, and the bridge told so.
        assert not session.wake_gate.asleep and session.wake_gate.woke
        woke = [f.message for f in session.task.queued]
        assert woke and woke[0]["type"] == "wake", woke
        # The welcome in place of the question, the unit started, no confirmation asked.
        assert v.said[0] == EN._(wv.WELCOME_TEXT), v.said
        assert v.said[0].startswith("Hi, I'm Teaport. Let's get me online.")
        assert v.state == "running" and v.calls == ["restart"]
        wvt._write(path, phase="ap_up", screen=True, qr=True, **wvt.DETAILS)
        await asyncio.sleep(0.1)
        assert v.said[-1].startswith(EN._("I'm in setup mode.")), v.said
        assert "Scan the code on my screen" in v.said[-1]
        # The room conversation is kept awake while it runs, with nothing to type through.
        kept = _messages(v, "working")
        assert kept and kept[0]["call_id"] == wv.WORKING_ID and kept[0]["typing"] is False
        assert kept[0]["secs"] == wv.KEEP_AWAKE_SECS
        assert first_boot.is_new(), "the marker before the box was ever online"
        await v._stop_following()
    asyncio.run(run())


def test_new_and_online_writes_the_marker_and_stays_quiet(box):
    async def run():
        session, _ = _room()
        assert not await _run(session, "full")
        assert not first_boot.is_new()
        assert session.wifi.said == [] and session.wifi.calls == []
        assert session.wake_gate.asleep and session.task.queued == []
        # And never again, offline or not.
        assert not first_boot.could_be_due()
    asyncio.run(run())


def test_set_up_and_offline_does_nothing_by_itself(box):
    async def run():
        first_boot.reached_online("synthetic")
        session, _ = _room()
        check = _online("none")
        orig, wv.connectivity = wv.connectivity, check
        try:
            assert not first_boot.listening(), "a set-up box's room waits for first boot"
            assert not await first_boot.drive(engine_up=_engine_up)
        finally:
            wv.connectivity = orig
        assert check.asked == 0, "a set-up box asked about connectivity at all"
        assert session.wifi.said == [] and session.wake_gate.asleep
    asyncio.run(run())


def test_a_cancel_says_how_to_start_again_and_the_next_boot_starts_it_again(box):
    async def run():
        session, path = _room()
        assert await _run(session, "none")
        v = session.wifi
        wvt._write(path, phase="ap_up", screen=True, qr=True, **wvt.DETAILS)
        await asyncio.sleep(0.1)
        assert await v._heard("cancel")
        await asyncio.sleep(0.05)
        assert v.state == "idle" and v.calls == ["restart", "stop"]
        again = EN._(wv.AGAIN_TEXT)
        assert v.said[-1].endswith(again), v.said[-1]
        assert v.said[-1].startswith(EN._("Okay, I've left setup mode")), v.said[-1]
        assert _messages(v, "working")[-1].get("done"), "the bridge still thinks it runs"
        # The same boot (a brain restart, a new room session): not again.
        later, _ = _room()
        assert not await _run(later, "none")
        assert later.wifi.said == []
        # A setup the user starts by voice afterwards carries no first-boot line.
        assert await v._heard("set up wifi") and await v._heard("yes")
        await v._cancel()
        assert not v.said[-1].endswith(again), v.said[-1]
        # The next boot, still new: setup mode again.
        box.reboot("boot-2")
        next_boot, _ = _room()
        assert await _run(next_boot, "none")
        assert next_boot.wifi.said[0] == EN._(wv.WELCOME_TEXT)
        await next_boot.wifi._stop_following()
    asyncio.run(run())


def test_a_timeout_also_says_how_to_start_again(box):
    async def run():
        session, path = _room()
        assert await _run(session, "limited")
        wvt._write(path, phase="ap_up", screen=True, qr=True, **wvt.DETAILS)
        await asyncio.sleep(0.1)
        wvt._write(path, phase="timeout", **wvt.DETAILS)
        await asyncio.sleep(0.1)
        v = session.wifi
        assert v.state == "idle" and v.said[-1].endswith(EN._(wv.AGAIN_TEXT)), v.said
    asyncio.run(run())


def test_a_setup_that_connects_writes_the_marker_and_says_no_more(box):
    async def run():
        session, path = _room()
        assert await _run(session, "none")
        wvt._write(path, phase="ap_up", screen=True, qr=True, **wvt.DETAILS)
        await asyncio.sleep(0.1)
        wvt._write(path, phase="connected", target="Synthetic Net")
        await asyncio.sleep(0.1)
        v = session.wifi
        assert v.state == "idle" and "online again" in v.said[-1], v.said
        assert not v.said[-1].endswith(EN._(wv.AGAIN_TEXT))
        assert not first_boot.is_new()
    asyncio.run(run())


# ---- what keeps it from nagging or guessing ---------------------------------------------

def test_it_waits_out_the_boot_grace_period_first(box):
    async def run():
        box.reboot(uptime=10.0)
        slept = []

        async def sleep(secs):
            slept.append(secs)
        assert await first_boot.due(connectivity=_online("none"), sleep=sleep)
        assert slept[0] == pytest.approx(first_boot.GRACE_SECS - 10.0), slept
    asyncio.run(run())


def test_a_room_session_that_ended_before_the_hand_over_is_not_handed_it(box):
    """The room session that was up ended while the grace period ran (the bridge
    redialled, the engine restarted): the setup is not given to it -- drive() asks the
    bridge for a new one, and none coming, starts the setup network without a voice."""
    async def run():
        session, _ = _room()
        session.task.has_finished = lambda: True
        started = []

        async def headless():
            started.append(True)
            return True
        assert not await _run(session, "none", headless=headless)
        assert session.wifi.said == [] and session.wifi.calls == []
        assert _run.headless == 1 and started == [True]
        assert first_boot.tried_this_boot(), "the boot's try is drive()'s, spent when due"
    asyncio.run(run())


def test_no_connectivity_check_no_guess(box):
    async def run():
        session, _ = _room()
        assert not await _run(session, None)
        assert first_boot.is_new() and not first_boot.tried_this_boot()
        assert session.wifi.said == []
    asyncio.run(run())


def test_off_it_does_nothing_and_writes_nothing(box, monkeypatch):
    monkeypatch.setattr(first_boot, "ENABLED", False)

    async def run():
        session, _ = _room()
        assert not await _run(session, "none")
        first_boot.reached_online("synthetic")
        await first_boot.watch(connectivity=_online("full"))
        assert not os.path.exists(first_boot.state_dir())
    asyncio.run(run())


def test_the_watcher_writes_the_marker_once_the_box_is_online(box):
    async def run():
        states = iter(["none", "limited", "full"])

        async def check():
            return next(states)

        async def sleep(_secs):
            pass
        await asyncio.wait_for(first_boot.watch(connectivity=check, sleep=sleep), 5)
        assert not first_boot.is_new()
    asyncio.run(run())


def test_a_setup_already_under_way_is_left_alone(box):
    async def run():
        session, _ = _room(asleep=False)
        v = session.wifi
        assert await v._heard("set up wifi")       # asking: "do you want to ...?"
        assert not await _run(session, "none")
        assert v.state == "confirm" and EN._(wv.WELCOME_TEXT) not in v.said
    asyncio.run(run())


# ---- review round: no false offline, no loop, no stale persona, no silent end ------------

def test_a_slow_or_passing_answer_is_not_offline(box):
    """An online box must never be taken off its network for a check that timed out ("")
    or one moment of "limited": offline is two answers saying so."""
    async def run():
        for states in (("",), ("", "limited", "")):
            box.reboot()
            session, _ = _room()
            assert not await _run(session, *states), states
            assert session.wifi.said == [] and session.wifi.calls == [], states
            # Given the long timeout, not the phrase's short one; read again, up to a limit.
            assert set(_run.check.timeouts) == {first_boot.CHECK_SECS}, _run.check.timeouts
            assert _run.check.asked == first_boot.READINGS
            assert not first_boot.tried_this_boot(), "an unknown answer spent the boot's try"
        # Two answers that say so, with a timeout between them: offline (the next start
        # of the brain: this one's driver has had its look).
        box.reboot()
        session, _ = _room()
        assert await _run(session, "limited", "", "none")
        await session.wifi._stop_following()
        # A passing "limited", or timeouts, then full: online, the marker, nothing said.
        for states in (("limited", "full"), ("", "", "full")):
            os.remove(os.path.join(first_boot.state_dir(), "setup-done")) \
                if not first_boot.is_new() else None
            box.reboot()
            session, _ = _room()
            assert not await _run(session, *states), states
            assert session.wifi.said == [] and not first_boot.is_new(), states
    asyncio.run(run())


def test_once_a_boot_holds_when_the_try_cannot_be_written(box, monkeypatch):
    """No boot id to compare, and setup-tried not writable: the process still remembers,
    so a wake-word room opening a session after every sleep is not set up on a loop."""
    def refuse(*_a, **_k):
        raise OSError("read-only state directory")

    async def run():
        with open(first_boot._BOOT_ID, "w") as f:
            f.write("")
        monkeypatch.setattr(first_boot, "_write", refuse)
        first, _ = _room()
        assert await _run(first, "none")
        await first.wifi._stop_following()
        second, _ = _room()
        assert not await _run(second, "none")
        assert second.wifi.said == []
    asyncio.run(run())


def test_a_setup_the_owner_started_spends_the_boots_try(box):
    """Started by voice during the grace period and cancelled: no setup nobody asked for
    at the end of it."""
    async def run():
        session, _ = _room(asleep=False)
        v = session.wifi
        assert await v._heard("set up wifi") and await v._heard("yes")
        await v._cancel()
        assert first_boot.tried_this_boot()
        assert not v.said[-1].endswith(EN._(wv.AGAIN_TEXT)), "a first-boot line on the owner's"
        later, _ = _room()
        assert not await _run(later, "none")
        assert later.wifi.said == []
    asyncio.run(run())


def test_the_retry_line_only_where_a_retry_can_help(box):
    async def run():
        cases = [
            ({"phase": "error", "reason": wv.NO_WIFI_DEVICE}, False),     # nothing to retry
            ({"phase": "error", "reason": wv.PAGE_FAILED}, False),        # says so already
            ({"phase": "error", "reason": "the setup network would not start"}, True),
        ]
        for status, again in cases:
            box.reboot()
            session, path = _room()
            assert await _run(session, "none")
            wvt._write(path, **status)
            await asyncio.sleep(0.1)
            v = session.wifi
            assert v.state == "idle", (status, v.said)
            assert v.said[-1].endswith(EN._(wv.AGAIN_TEXT)) == again, (status, v.said[-1])
            assert v.said[-1].count(EN._("try again")) <= 1, v.said[-1]
        # Set up meanwhile (online once): no "say set up Wi-Fi to try again".
        box.reboot()
        session, path = _room()
        assert await _run(session, "none")
        first_boot.reached_online("synthetic")
        v = session.wifi
        v._first_boot, v.state = True, "running"
        await v._say_end("Synthetic end line.")
        assert v.said[-1] == "Synthetic end line."
        await v._stop_following()
    asyncio.run(run())


def test_a_first_boot_setup_leaves_setup_mode_when_the_box_comes_online(box):
    """Ethernet plugged in while the setup network is up: the watcher writes the marker,
    and the setup stops itself -- not while it joins the network picked on the phone."""
    async def run():
        session, path = _room()
        assert await _run(session, "none")
        v = session.wifi
        wvt._write(path, phase="ap_up", screen=True, qr=True, **wvt.DETAILS)
        await asyncio.sleep(0.1)
        first_boot.reached_online("synthetic: Ethernet")
        await asyncio.sleep(0.1)
        assert v.state == "idle" and v.calls == ["restart", "stop"], (v.state, v.calls)
        assert v.said[-1] == EN._(wv.ONLINE_NOW_TEXT), v.said
        assert _messages(v, "working")[-1].get("done")
        # Joining: the box online is the setup's own doing; it carries on.
        box.reboot()
        os.remove(os.path.join(first_boot.state_dir(), "setup-done"))
        session, path = _room()
        assert await _run(session, "none")
        v = session.wifi
        wvt._write(path, phase="joining", target="Synthetic Net", **wvt.DETAILS)
        await asyncio.sleep(0.1)
        first_boot.reached_online("synthetic: joined")
        await asyncio.sleep(0.1)
        assert v.state == "running" and v.calls == ["restart"], (v.state, v.calls)
        await v._stop_following()
    asyncio.run(run())


def test_the_first_boot_wake_is_a_wake(box):
    """The same wake as a wake word's (WakeGate.wake): the voice context asked again, a
    recent conversation restored -- with no greeting cue: the welcome is the greeting."""
    async def run():
        session, _ = _room(asleep=True)
        gate = session.wake_gate
        asked = []

        async def refresh():
            asked.append(True)
        gate.refresh = refresh
        gate.store.save([{"role": "user", "content": "synthetic earlier words"}])
        gate.context = SimpleNamespace(added=[], add_messages=lambda m: gate.context.added.extend(m),
                                       add_message=lambda m: gate.context.added.append(m))
        assert await _run(session, "none")
        assert asked == [True] and gate.refresh is None
        assert gate.context.added == [{"role": "user", "content": "synthetic earlier words"}]
        woke = [f.message for f in session.task.queued if hasattr(f, "message")]
        assert woke[0]["type"] == "wake" and woke[0]["greeted"] is False, woke
        await session.wifi._stop_following()
    asyncio.run(run())


def test_the_watcher_gives_up_without_a_connectivity_check(box):
    async def run():
        check = _online(None)
        await asyncio.wait_for(first_boot.watch(connectivity=check), 5)
        assert check.asked == 1 and first_boot.is_new()
    asyncio.run(run())


def test_a_failure_in_the_unawaited_task_is_logged_not_lost(box):
    from loguru import logger
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="ERROR")

    async def run():
        session, _ = _room()

        async def broken():
            raise RuntimeError("synthetic failure")
        session.wifi.begin_first_boot = broken
        assert await _run(session, "none") is False
    try:
        asyncio.run(run())
    finally:
        logger.remove(sink)
    assert any("could not go into setup mode" in m for m in seen), seen


def test_a_room_in_setup_mode_is_live_for_the_arbiter():
    """While Wi-Fi setup asks or runs, the room is no room nobody uses: a newcomer is
    refused or asks, as for a conversation, and does not end it without a word."""
    from teaport_brain import gateway_server as gs
    idle_gate = SimpleNamespace(user_heard=False)
    for state, gate, asleep in (("idle", None, True), ("running", None, False),
                                ("confirm", None, False),
                                ("idle", SimpleNamespace(asleep=True), True),
                                ("running", SimpleNamespace(asleep=True), False)):
        s = SimpleNamespace(wifi=SimpleNamespace(state=state), followup_gate=idle_gate)
        assert gs.session_asleep(s, gate, room=True, keepalive=45) is asleep, (state, gate)
    assert not gs.session_asleep(None, None, room=False, keepalive=45)


def test_an_unanswered_question_lapses_by_itself_and_the_room_is_free_again(monkeypatch):
    """"Set up Wi-Fi", then nobody says a word: in a room without wake words nothing else
    would ever end the question, and the room would stay busy with setup for good -- every
    Talk client refused, every caller busy."""
    from teaport_brain import gateway_server as gs
    monkeypatch.setattr(wv, "CONFIRM_SECS", 0.05)

    async def run():
        session, _ = _room(asleep=False)
        session.followup_gate = SimpleNamespace(user_heard=False)
        v = session.wifi
        assert await v._heard("set up wifi")
        assert v.state == "confirm"
        assert not gs.session_asleep(session, None, room=True, keepalive=45)
        await asyncio.sleep(0.15)
        assert v.state == "idle", "the question never lapsed"
        assert gs.session_asleep(session, None, room=True, keepalive=45)
        # A question answered "no", then asked again: the new one gets its own full time,
        # the first one's timer does not end it.
        assert await v._heard("set up wifi")
        assert await v._heard("no") and v.state == "idle"
        await asyncio.sleep(0.03)
        assert await v._heard("set up wifi") and v.state == "confirm"
        await asyncio.sleep(0.03)                  # past the first question's time
        assert v.state == "confirm", "an older question's timer ended the new one"
        await asyncio.sleep(0.05)
        assert v.state == "idle"
    asyncio.run(run())


def test_a_room_in_setup_cannot_be_asked_about_a_call():
    """While setup asks or runs it takes every word said, so a call's question would never
    get its answer and its default would put the room on hold mid-setup: the room cannot
    be asked, and the arbiter turns the caller away (busy)."""
    from teaport_brain import gateway_server as gs
    from teaport_brain import session_arbiter as arb
    asked = []
    for state, expected in (("idle", True), ("confirm", False), ("running", False)):
        s = SimpleNamespace(call_prompt=object(), wifi=SimpleNamespace(state=state))
        assert gs.session_can_be_asked(s) is expected, state
        room = arb.Claim(arb.ROOM, client="local-audio", asleep=lambda: False,
                         ask=asked.append, hold=asked.append, resume=asked.append,
                         can_ask=lambda s=s: gs.session_can_be_asked(s))
        call = arb.Claim(arb.CALL, client="sip")
        assert arb.decide(room, call) == (arb.PROMPT if expected else arb.REFUSED), state
    assert not gs.session_can_be_asked(SimpleNamespace(call_prompt=None, wifi=None))


def test_the_first_boot_wake_is_logged_as_what_it_is(box):
    """Not as a wake word nobody said: the brain logs "woken for first-boot setup", and
    the bridge, told why and no phrase, says the same."""
    from loguru import logger
    from teaport_brain.local_audio import wake_note
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="INFO")

    async def run():
        session, _ = _room(asleep=True)
        assert await _run(session, "none")
        await session.wifi._stop_following()
        return [f.message for f in session.task.queued if hasattr(f, "message")][0]
    try:
        message = asyncio.run(run())
    finally:
        logger.remove(sink)
    assert any(m.startswith("woken for first-boot setup") for m in seen), seen
    assert not any("wake word" in m for m in seen), seen
    assert "phrase" not in message and message["why"] == "first-boot setup", message
    assert wake_note(message) == "woken for first-boot setup"
    assert wake_note({"type": "wake", "phrase": "hey teaport"}) == 'wake word "hey teaport" heard'


def test_the_typing_bed_leaves_setups_working_message_alone():
    from teaport_brain.thinking_sound import ThinkingSound
    from pipecat.processors.frame_processor import FrameDirection

    async def run():
        bed = ThinkingSound(consults_async=True)
        started = []

        async def start(why):
            started.append(why)

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            pass
        bed._start, bed.push_frame = start, push
        msg = {"type": "working", "call_id": wv.WORKING_ID, "typing": False, "secs": 60}
        await bed.process_frame(OutputTransportMessageUrgentFrame(message=msg),
                                FrameDirection.DOWNSTREAM)
        assert started == []
        await bed.process_frame(OutputTransportMessageUrgentFrame(
            message={"type": "working", "call_id": "c1"}), FrameDirection.DOWNSTREAM)
        assert started == ["background consult"]
    asyncio.run(run())


def test_the_markers_live_in_the_users_state_directory(box):
    assert first_boot.state_dir() == os.path.join(box.root, "home", ".local", "state", "teaport")
    first_boot.reached_online("synthetic")
    with open(os.path.join(first_boot.state_dir(), "setup-done")) as f:
        stamp = f.read()
    assert stamp.endswith("synthetic\n") and time.strftime("%Y") in stamp


# ---- #457: the brain starts first boot, with or without a room mic ----------------------

async def _quick(_secs):
    await asyncio.sleep(0)


def _asks(monkeypatch, then=None):
    """Record each ask for the room (first_boot.want_room); `then(id)` after each."""
    asks, real = [], first_boot.want_room

    def want_room(why):
        ask = real(why)
        asks.append(ask)
        if then is not None:
            then(ask)
        return ask
    monkeypatch.setattr(first_boot, "want_room", want_room)
    return asks


def _bridge(sessions):
    """The room mic's bridge, as far as first boot sees it: each ask it sees in
    /talk/status (first_boot.room_wanted) dialled once, and the next of `sessions` comes
    up past its STT check -- where gateway_server starts first_boot.run on it, only while
    first_boot.listening() (`started` holds None for a session it left out)."""
    started, seen = [], set()

    async def bridge():
        while True:
            wanted = first_boot.room_wanted()
            if wanted is not None and wanted["id"] not in seen and sessions:
                seen.add(wanted["id"])
                session = sessions.pop(0)
                started.append(asyncio.ensure_future(first_boot.run(session))
                               if first_boot.listening() else None)
            await asyncio.sleep(0.005)
    return bridge, started


def test_the_brain_asks_the_room_mic_once_the_engine_answers(box, monkeypatch):
    """Nobody has spoken to the box, and the bridge's first session at boot ended for want
    of the engine: no room session is up. The brain waits for the engine, asks the bridge
    for one (/talk/status "wanted"), and the session that comes takes the setup."""
    asks = _asks(monkeypatch)
    session, _ = _room(asleep=False)
    bridge, started = _bridge([session])
    answers = iter([False, False, True])

    async def engine_up():
        assert first_boot.room_wanted() is None, "asked for the room before the engine"
        return next(answers)
    slept = []

    async def sleep(secs):
        slept.append(secs)
        await asyncio.sleep(0)

    async def run():
        wv.connectivity = _online("none")
        dialler = asyncio.ensure_future(bridge())
        try:
            assert await first_boot.drive(sleep=sleep, engine_up=engine_up,
                                          headless=_no_headless)
        finally:
            dialler.cancel()
            wv.connectivity = _real_connectivity
        assert slept.count(first_boot.ENGINE_POLL_SECS) == 2, slept
        assert len(asks) == 1 and len(started) == 1
        assert started[0] is not None, "the session asked for was left out (listening)"
        assert await started[0] is True
        assert session.wifi.said[0] == EN._(wv.WELCOME_TEXT), session.wifi.said
        assert session.wifi.calls == ["restart"]
        # The ask is over, and no room session offers itself once the driver is done.
        assert first_boot.room_wanted() is None and not first_boot.listening()
        await session.wifi._stop_following()
    asyncio.run(run())


_real_connectivity = wv.connectivity


class _VoicelessStart(BaseException):
    """Not an Exception: drive() logs those and goes on, and the test would pass."""


async def _no_headless():
    raise _VoicelessStart("started the setup network with no voice")


def test_the_room_session_up_already_takes_it_without_an_ask(box, monkeypatch):
    """A wake-word room asleep, or a room nobody has spoken to yet: its session is up, so
    there is nothing to ask the bridge for."""
    asks = _asks(monkeypatch)

    async def run():
        session, _ = _room(asleep=True)
        assert await _run(session, "none")
        assert asks == [] and _run.headless == 0
        assert not session.wake_gate.asleep
        await session.wifi._stop_following()
    asyncio.run(run())


def test_a_room_that_went_before_the_hand_over_is_asked_for_again(box, monkeypatch):
    """The session the bridge opened ended at once (its STT not up after all): the brain
    asks again, with a new ask the bridge dials, and the next session takes the setup."""
    asks = _asks(monkeypatch)
    gone, _ = _room(asleep=False)
    gone.task.has_finished = lambda: True
    session, _ = _room(asleep=False)
    bridge, started = _bridge([gone, session])

    async def run():
        wv.connectivity = _online("none")
        dialler = asyncio.ensure_future(bridge())
        try:
            assert await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                          headless=_no_headless)
        finally:
            dialler.cancel()
            wv.connectivity = _real_connectivity
        assert len(asks) == 2 and asks[0] != asks[1], asks
        assert None not in started, "a session asked for was left out (listening)"
        assert gone.wifi.said == [] and session.wifi.said[0] == EN._(wv.WELCOME_TEXT)
        assert await started[1] is True
        # The session that went is still waiting: its disconnect cancels it (gateway_server).
        assert not started[0].done()
        started[0].cancel()
        await asyncio.gather(started[0], return_exceptions=True)
        assert first_boot._room is None
        await session.wifi._stop_following()
    asyncio.run(run())


def test_no_room_mic_starts_the_setup_network_with_no_voice(box, monkeypatch):
    """No room session came after ROOM_ASKS asks (no card, no bridge): the setup network
    starts by itself, its screens in the box's own voice's language, so the screen shows
    the network, the password and the code."""
    asks = _asks(monkeypatch)
    ran, langs = [], []

    async def helper(action, unit):
        ran.append((action, unit))
        return 0, ""
    from teaport_brain import wifi_setup
    monkeypatch.setattr(wv, "_run_helper", helper)
    monkeypatch.setattr(wifi_setup, "hand_language", langs.append)
    settings = {"TTS_VOICE": "ef_dora", "TTS_LANGUAGE": None}
    monkeypatch.setattr(first_boot, "setting", settings.get)

    async def run():
        wv.connectivity = _online("none")
        try:
            assert await first_boot.drive(sleep=_quick, engine_up=_engine_up)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())
    assert len(asks) == first_boot.ROOM_ASKS == len(set(asks)), asks
    assert ran == [("restart", wv.UNIT)]
    assert langs == [i18n.for_espeak("es").lang] and langs[0] != i18n.for_espeak("en-us").lang
    assert first_boot.room_wanted() is None, "the ask outlived the driver"
    assert first_boot.tried_this_boot()


def test_a_setup_network_that_will_not_start_is_said_in_the_log(box, monkeypatch):
    async def helper(action, unit):
        return 1, "synthetic: sudo refused"
    from teaport_brain import wifi_setup
    monkeypatch.setattr(wv, "_run_helper", helper)
    monkeypatch.setattr(wifi_setup, "hand_language", lambda lang: None)

    async def run():
        wv.connectivity = _online("none")
        try:
            assert not await first_boot.drive(sleep=_quick, engine_up=_engine_up)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())


def test_an_engine_that_never_answers_does_not_hold_setup_back(box, monkeypatch):
    polls = []

    async def engine_up():
        polls.append(True)
        return False
    started = []

    async def headless():
        started.append(True)
        return True

    async def run():
        wv.connectivity = _online("none")
        try:
            assert await first_boot.drive(sleep=_quick, engine_up=engine_up,
                                          headless=headless)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())
    assert len(polls) == first_boot.ENGINE_WAIT_SECS / first_boot.ENGINE_POLL_SECS + 1
    assert started == [True]


def test_online_meanwhile_starts_nothing(box, monkeypatch):
    """Ethernet plugged in while the brain waits for a room session: the watcher writes
    the marker, and the setup network is not started for a box that no longer needs it."""
    asks = _asks(monkeypatch, then=lambda _ask: first_boot.reached_online("synthetic"))

    async def run():
        wv.connectivity = _online("none")
        try:
            assert not await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                              headless=_no_headless)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())
    assert len(asks) == 1 and not first_boot.is_new()


def test_a_room_session_that_never_answers_is_left_to_it(box, monkeypatch):
    """The session took the setup and has not said how it went: it may have started, so
    neither a second room session nor the voiceless start follows."""
    monkeypatch.setattr(first_boot, "HANDOVER_SECS", 0.05)
    asks = _asks(monkeypatch)

    async def run():
        session, _ = _room(asleep=False)
        hold = asyncio.Event()

        async def stuck():
            await hold.wait()
            return True
        session.wifi.begin_first_boot = stuck
        assert not await _run(session, "none")
        assert asks == [] and _run.headless == 0
    asyncio.run(run())


def test_no_setup_unit_no_first_boot(box, monkeypatch):
    monkeypatch.setattr(wv, "available", lambda: False)

    async def run():
        session, _ = _room()
        assert not await _run(session, "none")
        assert _run.check.asked == 0 and not first_boot.tried_this_boot()
        assert session.wifi.said == []
        assert not first_boot.listening(), "room sessions wait on a driver that has given up"
    asyncio.run(run())


def test_busy_is_a_live_conversation_or_a_call_not_a_sleeping_room(monkeypatch):
    """_box_busy reads the arbiter: a live holder, one on hold, or a call in or asking.
    A room mic asleep, or one nobody has spoken to (not live), leaves the box free."""
    from teaport_brain import session_arbiter as arb
    cases = [({"live": False, "call": False}, False), ({"live": True, "call": False}, True),
             ({"live": False, "call": True}, True), ({"live": True, "call": True}, True)]
    for status, busy in cases:
        monkeypatch.setattr(arb.ARBITER, "status", lambda status=status: dict(status))
        assert first_boot._box_busy() is busy, status  # no box fixture: the real one


def test_a_room_session_without_spoken_setup_does_not_offer_itself(box):
    """A room with no Wi-Fi setup voice (its tool off) cannot take the setup: it never
    answers the brain's ask, so the brain starts the setup network with no voice."""
    async def run():
        session = SimpleNamespace(wifi=None, task=_Task(), wake_gate=None)
        assert not await first_boot.run(session)
        assert first_boot._room is None

        async def headless():
            return True
        assert not await _run(session, "none", headless=headless)
        assert _run.headless == 1
    asyncio.run(run())


def test_a_session_that_ends_as_it_is_handed_the_setup_is_not_set_up(box):
    """Taken for live, then ended before it woke to the hand-over: it starts nothing
    (run's own check), and the brain goes on without it."""
    async def run():
        session, _ = _room(asleep=False)

        def handed():
            room = first_boot._room
            return room is not None and room.session is session and room.turn.done()
        session.task.has_finished = handed

        async def headless():
            return True
        assert not await _run(session, "none", headless=headless)
        assert session.wifi.said == [] and session.wifi.calls == []
        assert _run.headless == 1
    asyncio.run(run())


def test_a_session_that_ends_mid_start_is_not_started_again(box):
    """The bridge went while the room session was starting the setup network: that start
    runs on (shielded), so neither another room session nor the voiceless start follows
    -- two starts of the unit would fight."""
    async def run():
        session, _ = _room(asleep=False)
        v = session.wifi
        release = asyncio.Event()
        real_run = v._run

        async def slow_run(action, unit):
            await release.wait()
            return await real_run(action, unit)
        v._run = slow_run
        wv.connectivity = _online("none")
        took = asyncio.ensure_future(first_boot.run(session))
        await asyncio.sleep(0)
        driver = asyncio.ensure_future(first_boot.drive(engine_up=_engine_up,
                                                        headless=_no_headless))
        try:
            for _ in range(200):
                if v.state == "running":
                    break
                await asyncio.sleep(0.01)
            assert v.state == "running", "the start never began"
            took.cancel()                        # the bridge disconnected mid-start
            await asyncio.gather(took, return_exceptions=True)
            assert await asyncio.wait_for(driver, 2) is True
            release.set()
            await asyncio.sleep(0.05)
            assert v.calls == ["restart"], v.calls
        finally:
            wv.connectivity = _real_connectivity
            await v._stop_following()
    asyncio.run(run())


def test_no_voiceless_setup_under_a_conversation_or_a_call(box, monkeypatch):
    """A Talk session on a LAN with no internet (offline to the check), and no room
    session came: the setup network would cut it off unannounced, so nothing starts."""
    monkeypatch.setattr(first_boot, "_box_busy", lambda: True)

    async def run():
        wv.connectivity = _online("none")
        try:
            assert not await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                              headless=_no_headless)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())


def test_the_owners_own_setup_is_left_to_them(box):
    """The room session is asking "do you want to set up Wi-Fi?": drive() leaves it, and
    does not count it as setup mode begun (the owner may say no)."""
    async def run():
        session, _ = _room(asleep=False)
        assert await session.wifi._heard("set up wifi")
        took = asyncio.ensure_future(first_boot.run(session))
        await asyncio.sleep(0)
        wv.connectivity = _online("none")
        try:
            assert await first_boot.drive(engine_up=_engine_up, headless=_no_headless) is False
        finally:
            wv.connectivity = _real_connectivity
        assert await took is False and session.wifi.state == "confirm"
    asyncio.run(run())


def test_once_the_driver_is_done_no_room_session_waits_on_it(box):
    """A look that could not tell (every check timed out) ends this start's driver; the
    room sessions that come later do not each wait on a hand-over that will not come."""
    async def run():
        session, _ = _room()
        assert not await _run(session, "")
        assert not first_boot.tried_this_boot() and not first_boot.listening()
        box.reboot()                       # the next start of the brain looks again
        assert first_boot.listening()
    asyncio.run(run())


def test_a_room_on_hold_for_a_call_is_not_handed_the_setup(box, monkeypatch):
    """A room that said yes to a call is on hold, its session still up: handed the setup,
    it would play the welcome into the hold and start the setup network under the call.
    Nothing starts, the voiceless start neither, and this boot has had its try."""
    monkeypatch.setattr(first_boot, "_taken_by_another", lambda: True)

    async def run():
        session, _ = _room(asleep=False)
        assert not await _run(session, "none")
        assert session.wifi.said == [] and session.wifi.calls == []
        assert _run.headless == 0 and first_boot.tried_this_boot()
    asyncio.run(run())


def test_taken_is_a_call_a_hold_or_talk_not_the_rooms_own_conversation(monkeypatch):
    """_taken_by_another reads the arbiter: a call in or asking, a conversation on hold, a
    live Talk session. The room's own session, awake or asleep, leaves it to the room."""
    from teaport_brain import session_arbiter as arb
    free = {"live": False, "call": False, "held": None, "holder": None}
    cases = [({}, False),
             ({"live": True, "holder": arb.ROOM}, False),    # the room's own conversation
             ({"holder": arb.ROOM}, False),                  # a room asleep
             ({"live": True, "holder": arb.TALK}, True),
             ({"call": True}, True),
             ({"held": arb.ROOM}, True),                     # "held" alone is enough
             ({"live": True, "call": True, "held": arb.ROOM, "holder": arb.CALL}, True)]
    for change, taken in cases:
        status = {**free, **change}
        monkeypatch.setattr(arb.ARBITER, "status", lambda status=status: dict(status))
        assert first_boot._taken_by_another() is taken, status  # no box fixture: the real one


def test_online_while_the_room_session_came_is_not_handed_the_setup(box, monkeypatch):
    """The box came online (the watcher wrote the marker) while the brain waited for a room
    session: that session is not handed a setup the box no longer needs."""
    real = first_boot._a_room

    async def a_room():
        room = await real()
        first_boot.reached_online("synthetic")
        return room
    monkeypatch.setattr(first_boot, "_a_room", a_room)

    async def run():
        session, _ = _room(asleep=False)
        assert not await _run(session, "none")
        assert session.wifi.said == [] and session.wifi.calls == []
        assert _run.headless == 0
    asyncio.run(run())


def test_no_ask_is_left_out_for_the_voiceless_start(box, monkeypatch):
    """No room session came: the last ask is withdrawn before the setup network starts, so
    a late dial does not open a session (and its greeting) nobody waits for."""
    asks = _asks(monkeypatch)
    wanted = []

    async def headless():
        wanted.append(first_boot.room_wanted())
        return True

    async def run():
        wv.connectivity = _online("none")
        try:
            assert await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                          headless=headless)
        finally:
            wv.connectivity = _real_connectivity
        assert len(asks) == first_boot.ROOM_ASKS and wanted == [None], (asks, wanted)
    asyncio.run(run())


def _settles_after(monkeypatch, looks):
    """The arbiter's slot changing hands for `looks` looks, then settled."""
    seen = []

    def settling():
        seen.append(len(seen) < looks)
        return seen[-1]
    monkeypatch.setattr(first_boot, "_settling", settling)
    return seen


def test_the_hand_over_waits_out_the_slot_changing_hands(box, monkeypatch):
    """A room coming back from hold after a call reads as a plain room in its own
    conversation: the brain looks only once the resume is over."""
    seen = _settles_after(monkeypatch, 3)
    looked = []
    monkeypatch.setattr(first_boot, "_taken_by_another",
                        lambda: looked.append(len(seen)) or False)

    async def run():
        session, _ = _room(asleep=False)
        assert await _run(session, "none")
        assert looked == [4], (looked, seen)                  # after the 3 unsettled looks
        await session.wifi._stop_following()
    asyncio.run(run())


def test_a_slot_that_never_settles_starts_nothing(box, monkeypatch):
    """Still changing hands after SETTLE_WAIT_SECS: taken as busy, for the room session's
    setup and for the voiceless start alike."""
    monkeypatch.setattr(first_boot, "_settling", lambda: True)
    monkeypatch.setattr(first_boot, "SETTLE_WAIT_SECS", 0.5)

    async def run():
        session, _ = _room(asleep=False)
        assert not await _run(session, "none")
        assert session.wifi.said == [] and session.wifi.calls == [] and _run.headless == 0
        box.reboot()
        wv.connectivity = _online("none")
        try:
            assert not await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                              headless=_no_headless)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())


def test_a_room_that_ended_as_the_slot_changed_hands_is_not_handed_it(box, monkeypatch):
    """The room session ended while the brain waited out the slot (a resume that failed):
    it starts nothing when handed the setup (run's own check), and the brain goes on
    without it."""
    session, _ = _room(asleep=False)
    ended = []

    def settling():
        ended.append(True)
        session.task.has_finished = lambda: True
        return len(ended) == 1
    monkeypatch.setattr(first_boot, "_settling", settling)

    async def run():
        async def headless():
            return True
        assert not await _run(session, "none", headless=headless)
        assert session.wifi.said == [] and _run.headless == 1
    asyncio.run(run())


def test_online_while_the_slot_settled_starts_nothing(box, monkeypatch):
    """The box came online while the brain waited out the slot changing hands: neither the
    room session's setup nor the voiceless start follows."""
    def online_meanwhile():
        first_boot.reached_online("synthetic")
        return False
    monkeypatch.setattr(first_boot, "_settling", online_meanwhile)

    async def run():
        session, _ = _room(asleep=False)
        assert not await _run(session, "none")
        assert session.wifi.said == [] and session.wifi.calls == [] and _run.headless == 0
    asyncio.run(run())


def test_no_voiceless_start_once_online_while_the_slot_settled(box, monkeypatch):
    def online_meanwhile():
        first_boot.reached_online("synthetic")
        return False
    monkeypatch.setattr(first_boot, "_settling", online_meanwhile)

    async def run():
        wv.connectivity = _online("none")
        try:
            assert not await first_boot.drive(sleep=_quick, engine_up=_engine_up,
                                              headless=_no_headless)
        finally:
            wv.connectivity = _real_connectivity
    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
