"""The pair_device tool (issue #243), its session half (pair_device.py): offered only in
the room or a Talk session with a gateway, the code only ever on the box's screen, the
turn never held, and how the offer ended told to the model once."""
import asyncio
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain import agent_session, pairing, persona, tools  # noqa: E402
from teaport_brain import pair_device as pd  # noqa: E402

LINK = "https://box.example/p/abc123"


# ---- fakes -----------------------------------------------------------------------

class _Screen:
    """display.Screen's surface, recording what it was asked to do."""

    def __init__(self, qr=True, max_bytes=53, takes=True):
        self.qr, self.max_bytes, self.takes = qr, max_bytes, takes
        self.calls = []

    def draws(self, feature):
        return feature == "screen" or (feature == "qr" and self.qr)

    def fits_qr(self, text):
        return self.qr and len(text.encode()) <= self.max_bytes

    def show(self, title, lines, secs=None, qr=None):
        self.calls.append(("show", title, list(lines), secs, qr))
        return self.takes

    def clear(self):
        self.calls.append(("clear",))

    def close(self, take_down=True):
        self.calls.append(("close", take_down))


class _Pairing:
    """pairing's interface: start() mints, wait() blocks until end() says how."""

    def __init__(self, unavailable=None):
        self.unavailable = unavailable
        self.started, self.cancelled = [], []
        self._ends = {}

    async def start(self):
        if self.unavailable:
            raise pairing.PairingUnavailable(self.unavailable)
        for live, fut in list(self._ends.items()):
            if not fut.done():
                self.cancel(live)  # one live offer per process
        offer = pairing.Offer(f"c{len(self.started)}", LINK,
                              asyncio.get_running_loop().time() + 600)
        self.started.append(offer)
        self._ends[offer] = asyncio.get_running_loop().create_future()
        return offer

    async def wait(self, offer):
        return await self._ends[offer]

    def cancel(self, offer):
        self.cancelled.append(offer)
        self.end(offer, "cancelled")

    def end(self, offer, outcome, device=None):
        fut = self._ends.get(offer)
        if fut is not None and not fut.done():
            fut.set_result(pairing.Result(outcome, device))


class _Notes:
    def __init__(self):
        self.added = []

    def add_notice(self, text, respond=False):
        self.added.append((text, respond))


class _Params:
    def __init__(self, args=None):
        self.arguments, self.results = args or {}, []

    async def result_callback(self, result, **kw):
        self.results.append(result)


def _session(monkeypatch, screen=None, **kw):
    fake = _Pairing(**kw)
    for name in ("start", "wait", "cancel"):
        monkeypatch.setattr(pd.pairing, name, getattr(fake, name))
    screen = screen or _Screen()
    dp = pd.DevicePairing(notes=_Notes(), screen=lambda: screen,
                          clock=lambda: asyncio.get_running_loop().time())
    return dp, fake, screen


async def _call(dp, front_end="room", **args):
    params = _Params(args)
    await tools._pair_device(params, pairing=dp, front_end=front_end)
    return params.results[0]


async def _turn():
    for _ in range(5):
        await asyncio.sleep(0)


# ---- where it is offered ---------------------------------------------------------

def _offered(front_end):
    return "pair_device" in [t.name for t in tools.active_tools(
        tools.ToolContext(has_tts=True, front_end=front_end))]


def test_offered_in_the_room_and_on_talk_never_on_a_call(monkeypatch):
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    assert _offered("room") and _offered("talk")
    assert not _offered("sip") and not _offered(None)


def test_needs_the_gateway_and_its_switch(monkeypatch):
    monkeypatch.setattr(tools, "HAS_AGENT", False)
    assert not _offered("room")
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    monkeypatch.setattr(tools, "TOOLS", tuple(
        dataclasses.replace(t, enabled=t.name != "pair_device") for t in tools.TOOLS))
    assert not _offered("room") and not _offered("talk")


def test_the_prompt_names_it_in_its_own_sentence(monkeypatch):
    """The tuned tools paragraph stays as it is for a session that also has it."""
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    room = tools.active_tools(tools.ToolContext(has_tts=True, front_end="room"))
    sip = tools.active_tools(tools.ToolContext(has_tts=True, front_end="sip"))
    with_it, without = persona.tools_paragraph(room), persona.tools_paragraph(sip)
    assert with_it.startswith(without) and without.startswith(persona._TOOLS_WITH_AGENT)
    assert "pair_device" in with_it[len(without):] and "pair_device" not in without
    # Agent-first lets the model call it itself: no consult can show the box's screen.
    assert "pair_device" in agent_session.agent_first_directive(room)


def test_the_session_builds_it_only_where_the_tool_is(monkeypatch):
    from test_client_notes import _Transport
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    for front_end, want in (("room", True), ("talk", True), ("sip", False)):
        built = []
        real = pd.DevicePairing
        monkeypatch.setattr(agent_session, "DevicePairing",
                            lambda: built.append(real()) or built[-1])
        session = agent_session.build_agent_session(
            _Transport(), context_notes=front_end != "sip", front_end=front_end)
        assert bool(built) is want, front_end
        if want:
            assert built[0].notes is session.client_notes is not None
            # Ended or disconnected, the code comes down (pair_device.close).
            handlers = session.task._event_handlers["on_pipeline_finished"].handlers
            assert any(getattr(h, "__name__", "") == "_close_pairing" for h in handlers)


# ---- what it does ----------------------------------------------------------------

def test_without_a_screen_that_draws_codes_it_says_so_and_mints_nothing(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch, screen=_Screen(qr=False))
        return await _call(dp), fake, screen, dp

    result, fake, screen, dp = asyncio.run(run())
    assert result["ok"] is False and "no screen" in result["error"]
    assert fake.started == [] and not dp.live
    assert not any(c[0] == "show" for c in screen.calls)


def test_a_code_too_long_for_the_screen_is_not_shown(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch, screen=_Screen(max_bytes=10))
        return await _call(dp), fake, screen, dp

    result, fake, screen, dp = asyncio.run(run())
    assert result["ok"] is False and "no screen" in result["error"]
    assert fake.cancelled == fake.started and not dp.live


def test_no_code_to_be_had_is_a_plain_failure(monkeypatch):
    async def run():
        dp, _, screen = _session(monkeypatch, unavailable="the gateway did not answer")
        return await _call(dp), screen

    result, screen = asyncio.run(run())
    assert result["ok"] is False and "try again" in result["error"]
    assert not any(c[0] == "show" for c in screen.calls)


def test_it_shows_the_code_and_returns_at_once(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        room = await asyncio.wait_for(_call(dp, "room"), 1)
        talk = await asyncio.wait_for(_call(dp, "talk"), 1)
        live = dp.live
        dp.close()
        return room, talk, screen, live

    room, talk, screen, live = asyncio.run(run())
    assert live, "the offer is not waited on in the background"
    shows = [c for c in screen.calls if c[0] == "show"]
    title, lines, secs, qr = shows[0][1:]
    assert title == "Pair" and secs is None and qr == LINK
    assert all(line.isascii() for line in lines)
    assert not any(LINK in line or "c0" in line for line in lines), "the code went in the text"
    for result in (room, talk):
        assert result["ok"] and "camera" in result["note"]
        assert "Never read out" in result["note"]
    assert "at the box" in talk["note"] and "at the box" not in room["note"]


def test_paired_shows_it_for_a_while_and_asks_the_voice_to_say_it(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], "paired", "Firefox on Linux")
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert not dp.live
    assert screen.calls[-2] == ("show", "Pair", ["Paired", "Firefox on Linux"],
                                pd.PAIRED_SCREEN_SECS, None)
    assert screen.calls[-1] == ("close", True)
    [(text, respond)] = dp.notes.added
    assert respond and "Firefox on Linux" in text and "connected" in text


@pytest.mark.parametrize("outcome", ["expired", "opened"])
def test_other_endings_take_the_code_down_and_are_context_only(monkeypatch, outcome):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], outcome)
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert ("clear",) in screen.calls and screen.calls[-1] == ("close", True)
    [(text, respond)] = dp.notes.added
    assert not respond and text.startswith("Pairing a device ended:")
    assert text.isascii() and "token" not in text


def test_cancel_takes_it_down_quietly(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        none = await _call(dp, cancel=True)
        await _call(dp)
        gone = await _call(dp, cancel="true")
        await _turn()
        return none, gone, dp, fake, screen

    none, gone, dp, fake, screen = asyncio.run(run())
    assert none["ok"] and "No code" in none["note"]
    assert gone["ok"] and "off the screen" in gone["note"]
    assert fake.cancelled == fake.started and not dp.live
    assert screen.calls[-2:] == [("clear",), ("close", True)]
    assert dp.notes.added == [], "our own cancel was told as news"


def test_a_second_call_replaces_the_code_rather_than_stacking(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        first = dp._watcher
        await _call(dp)
        await _turn()
        state = (first.done(), dp.live, list(fake.started), list(fake.cancelled))
        dp.close()
        return state, dp, screen

    (first_done, live, started, cancelled), dp, screen = asyncio.run(run())
    assert first_done and live and len(started) == 2 and cancelled[0] is started[0]
    shows = [c for c in screen.calls if c[0] == "show"]
    assert len(shows) == 2 and ("clear",) not in screen.calls[:len(screen.calls) - 2], (
        "the screen blinked off between the two codes")
    assert dp.notes is None or dp.notes.added == []


def test_another_sessions_code_is_left_on_the_screen(monkeypatch):
    """pairing allows one live offer per process: another session's new one cancels
    ours, and its screen (the same id) is not ours to take down."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], "cancelled")  # not through dp
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert ("clear",) not in screen.calls and screen.calls[-1] == ("close", False)
    [(text, respond)] = dp.notes.added
    assert not respond and "nothing was paired" in text


def test_the_session_ending_leaves_no_code_and_no_task(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        notes = dp.notes
        await _call(dp)
        watcher = dp._watcher
        dp.close()
        await _turn()
        return dp, fake, screen, watcher, notes

    dp, fake, screen, watcher, notes = asyncio.run(run())
    assert watcher.done() and not dp.live
    assert fake.cancelled == fake.started
    assert screen.calls[-2:] == [("clear",), ("close", True)]
    assert notes.added == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
