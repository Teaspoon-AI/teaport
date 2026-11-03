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

    boots = iter(range(1, 1000))

    def reboot(boot_id=None, uptime=120.0):
        """A new boot: its id and uptime, and a brain process that has tried nothing."""
        with open(boot, "w") as f:
            f.write((boot_id or f"boot-{next(boots)}") + "\n")
        with open(up, "w") as f:
            f.write(f"{uptime} 0.0\n")
        first_boot._tried_here = False
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


async def _run(session, *states):
    """first_boot.run with the connectivity check faked."""
    orig = wv.connectivity
    wv.connectivity = check = _online(*states)
    try:
        return await first_boot.run(session)
    finally:
        wv.connectivity = orig
        _run.check = check


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
            assert not await first_boot.run(session)
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


def test_a_session_that_ended_during_the_grace_period_leaves_the_try_to_the_next(box):
    async def run():
        session, _ = _room()
        session.task.has_finished = lambda: True
        assert not await _run(session, "none")
        assert not first_boot.tried_this_boot() and session.wifi.said == []
        fresh, _ = _room()
        assert await _run(fresh, "none")
        await fresh.wifi._stop_following()
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
        # Two answers that say so, with a timeout between them: offline.
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
