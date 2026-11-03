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
from pinned_pipecat import require_pinned  # noqa: E402
from tempdirs import tempdir  # noqa: E402

require_pinned()

from pipecat.frames.frames import OutputTransportMessageUrgentFrame  # noqa: E402

import test_wifi_voice as wvt  # noqa: E402
from teaport_brain import first_boot, i18n, wifi_voice as wv  # noqa: E402
from teaport_brain.wake_gate import WakeGate  # noqa: E402

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
    monkeypatch.setattr(wv, "POLL_SECS", 0.01)
    monkeypatch.setattr(wv, "LIVENESS_SECS", 0.05)

    def reboot(boot_id="boot-1", uptime=120.0):
        with open(boot, "w") as f:
            f.write(boot_id + "\n")
        with open(up, "w") as f:
            f.write(f"{uptime} 0.0\n")
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
                              wake_gate=WakeGate(["hey teaport"], asleep=asleep))
    return session, path


def _online(state):
    async def check():
        check.asked += 1
        return state
    check.asked = 0
    return check


def _messages(voice, kind):
    return [f.message for f in voice.pushed if isinstance(f, OutputTransportMessageUrgentFrame)
            and f.message.get("type") == kind]


async def _run(session, state):
    """first_boot.run with the connectivity check faked."""
    orig = wv.connectivity
    wv.connectivity = _online(state)
    try:
        return await first_boot.run(session)
    finally:
        wv.connectivity = orig


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
        assert slept == [pytest.approx(first_boot.GRACE_SECS - 10.0)], slept
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
