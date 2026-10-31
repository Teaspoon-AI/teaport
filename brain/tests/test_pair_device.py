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
    """pairing's interface, one pairing at a time: start() opens one (after `gate`, when
    set) or gives the live one a new code; wait() blocks until end() says how; a code
    in `opened` means a handoff was made in its pairing (withdraw() then keeps it)."""

    def __init__(self, unavailable=None):
        self.unavailable = unavailable
        self.started, self.cancelled = [], []
        self.gate = None
        self.opened = set()       # offers whose pairing has a handoff made
        self.fail_after = None    # a reason to raise with on the next start()
        self._pairings = 0
        self._ends = {}           # pairing id -> future Result
        self._used = {}           # offer -> future bool (spent())

    def _live(self, sid):
        fut = self._ends.get(sid)
        return fut is not None and not fut.done()

    async def start(self):
        if self.unavailable:
            raise pairing.PairingUnavailable(self.unavailable)
        if self.fail_after:
            raise pairing.PairingUnavailable(self.fail_after)
        if self.gate is not None:
            await self.gate.wait()
        loop = asyncio.get_running_loop()
        if not self._live(self._pairings):
            self._pairings += 1
            self._ends[self._pairings] = loop.create_future()
        for old in self.started:  # a new code: the one before cannot be used
            fut = self._used.get(old)
            if old.session == self._pairings and fut is not None and not fut.done():
                fut.set_result(False)
        offer = pairing.Offer(f"c{len(self.started)}", LINK, loop.time() + 600,
                              session=self._pairings)
        self.started.append(offer)
        return offer

    async def wait(self, offer):
        return await self._ends[offer.session]

    async def spent(self, offer):
        """True once use() says the code was used, False once the pairing ends or a new
        code replaces this one."""
        used = self._used.setdefault(offer, asyncio.get_running_loop().create_future())
        await asyncio.wait({used, self._ends[offer.session]},
                           return_when=asyncio.FIRST_COMPLETED)
        return used.done() and used.result()

    def use(self, offer):
        """The code was spent on a choice at /p/ (pairing.redeem)."""
        fut = self._used.setdefault(offer, asyncio.get_running_loop().create_future())
        if not fut.done():
            fut.set_result(True)

    def cancel(self, offer):
        self.cancelled.append(offer)
        self.end(offer, "cancelled")

    def withdraw(self, offer):
        if self._live(offer.session) and any(o.session == offer.session for o in self.opened):
            return True
        self.cancel(offer)
        return False

    def end(self, offer, outcome, device=None):
        fut = self._ends.get(offer.session)
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
    for name in ("start", "wait", "cancel", "withdraw", "spent"):
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

@pytest.fixture(autouse=True)
def _front_door(monkeypatch):
    monkeypatch.setitem(tools.HOST_CHECKS, "pairing", lambda: True)


def _offered(front_end, via=None):
    return "pair_device" in [t.name for t in tools.active_tools(
        tools.ToolContext(has_tts=True, front_end=front_end, via=via))]


def test_offered_in_the_room_and_on_talk_never_on_a_call(monkeypatch):
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    assert _offered("room") and _offered("talk")
    assert not _offered("sip") and not _offered(None)


def test_not_through_the_discord_bridge(monkeypatch):
    """Discord connects to /talk, but relays someone far from the box's screen."""
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    assert not _offered("talk", via="discord")


def test_not_on_a_box_without_a_front_door(monkeypatch):
    monkeypatch.setattr(tools, "HAS_AGENT", True)
    monkeypatch.setitem(tools.HOST_CHECKS, "pairing", lambda: False)
    assert not _offered("room") and not _offered("talk")


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
    for front_end, via, want in (("room", None, True), ("talk", None, True),
                                 ("talk", "discord", False), ("sip", None, False)):
        built = []
        real = pd.DevicePairing
        monkeypatch.setattr(agent_session, "DevicePairing",
                            lambda: built.append(real()) or built[-1])
        session = agent_session.build_agent_session(
            _Transport(), context_notes=front_end != "sip", front_end=front_end, via=via)
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
    assert respond and text == "Your Firefox on Linux is now connected."


@pytest.mark.parametrize("outcome", ["expired", "opened", pd.FAILED])
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


def test_a_failed_handoff_says_so_and_offers_another_try(monkeypatch):
    # pairing's FAILED: the code was used, the dashboard link or the app's setup code
    # could not be made. Context only, as the other endings at the phone are: its page
    # has already told the person to ask again.
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.use(fake.started[0])
        await _turn()
        fake.end(fake.started[0], pd.FAILED)
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert not dp.live and screen.calls[-2:] == [("clear",), ("close", True)]
    [(text, respond)] = dp.notes.added
    assert not respond and "could not finish connecting" in text
    assert "try again" in text and text.isascii()


def test_a_used_code_makes_way_for_finish_on_your_device(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        offer = fake.started[0]
        before = list(screen.calls)
        fake.use(offer)
        await _turn()
        during = screen.calls[len(before):]
        fake.end(offer, "paired", "iPhone")
        await _turn()
        return dp, screen, during

    dp, screen, during = asyncio.run(run())
    assert during == [("show", "Pair", pd.SPENT_LINES, None, None)], during
    assert all(line.isascii() for line in pd.SPENT_LINES)
    assert screen.calls[-2] == ("show", "Pair", ["Paired", "iPhone"], pd.PAIRED_SCREEN_SECS, None)


def test_an_unused_code_keeps_its_qr_and_leaves_no_task(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], "expired")
        await _turn()
        tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("pair-device")]
        return screen, tasks

    screen, tasks = asyncio.run(run())
    assert not any(c[0] == "show" and c[2] == pd.SPENT_LINES for c in screen.calls)
    assert not tasks, tasks


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
        state = (dp._watcher is first and not first.done(), dp.live, dp._offer,
                 list(fake.started), list(dp.notes.added))
        dp.close()
        return state, screen

    (one_watcher, live, offer, started, notes), screen = asyncio.run(run())
    assert one_watcher, "a new code in the same pairing is followed by the same watcher"
    assert live and len(started) == 2 and offer is started[1]
    assert started[0].session == started[1].session
    assert len(_shown(screen)) == 2 and ("clear",) not in screen.calls[:-2], (
        "the screen blinked off between the two codes")
    assert notes == [], "our own replacement was told as news"


def test_a_cancel_not_ours_takes_the_code_down_and_says_nothing(monkeypatch):
    """pairing's own cancel (or its wait failing) is not another session's code."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], "cancelled")
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert screen.calls[-2:] == [("clear",), ("close", True)]
    assert dp.notes.added == []


def test_wrong_tries_take_the_code_down_with_a_short_line(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.end(fake.started[0], pd.LOCKED)
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert screen.calls[-2:] == [("clear",), ("close", True)]
    [(text, respond)] = dp.notes.added
    assert not respond and "wrong tries" in text


def test_asking_again_after_the_code_was_used_puts_up_a_new_one_quietly(monkeypatch):
    """The first code was used and its device is still awaited; the user asks again
    (trusted the certificate, or wants the dashboard instead of the app). A new code
    goes up, and the first one's end is ours: nothing is told about it."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        first = fake.started[0]
        fake.use(first)
        await _turn()
        result = await _call(dp)
        await _turn()
        return result, dp, fake, screen, first

    result, dp, fake, screen, first = asyncio.run(run())
    assert result["ok"] and "camera" in result["note"]
    assert dp.live and dp._offer is fake.started[1] and dp._offer is not first
    assert _shown(screen)[-1][4] == LINK  # the new QR code is up
    assert dp.notes.added == [], dp.notes.added


def _shown(screen):
    return [c for c in screen.calls if c[0] == "show"]


def test_a_call_cut_off_while_its_code_is_made_keeps_the_code_that_is_up(monkeypatch):
    """A barge-in cancels the second call while its code is made: no new code came, so
    the one up is still the pairing's, and stays up and followed."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.gate = asyncio.Event()
        second = asyncio.create_task(_call(dp))
        await _turn()
        assert screen.calls[-1][0] == "show", "the old code went before the new was made"
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await _turn()
        return dp, screen

    dp, screen = asyncio.run(run())
    assert dp.live and dp._offer.code == "c0" and dp._screen is not None
    assert ("clear",) not in screen.calls and screen.calls[-1][0] == "show"
    assert dp.notes.added == []


def test_a_cancel_while_the_code_is_made_shows_nothing(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        fake.gate = asyncio.Event()
        call = asyncio.create_task(_call(dp))
        await _turn()
        gone = await _call(dp, cancel=True)
        fake.gate.set()
        result = await call
        return gone, result, dp, fake, screen

    gone, result, dp, fake, screen = asyncio.run(run())
    assert gone["ok"] and "off the screen" in gone["note"]
    assert result["ok"] is False and not dp.live and _shown(screen) == []
    assert fake.cancelled == fake.started, "the code made after the cancel stayed live"


def test_a_session_closed_while_the_code_is_made_shows_nothing(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        fake.gate = asyncio.Event()
        call = asyncio.create_task(dp.start())
        await _turn()
        dp.close()
        fake.gate.set()
        outcome = await call
        again = await dp.start()
        await _turn()
        return outcome, again, dp, fake, screen

    outcome, again, dp, fake, screen = asyncio.run(run())
    assert outcome[0] == pd.STOPPED and again[0] == pd.STOPPED
    assert not dp.live and dp._watcher is None and _shown(screen) == []
    assert fake.cancelled == fake.started and len(fake.started) == 1


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


def test_a_new_code_that_cannot_be_had_leaves_the_one_up(monkeypatch):
    """Asking again fails (pairing.start() raises): the code up is still the pairing's,
    so it stays, followed as before, and nothing is told."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.fail_after = "openclaw CLI did not start (No such file or directory)"
        result = await _call(dp)
        await _turn()
        return result, dp, screen

    result, dp, screen = asyncio.run(run())
    assert result["ok"] is False and "try again" in result["error"]
    assert dp.live and dp._offer.code == "c0" and ("clear",) not in screen.calls
    assert dp.notes.added == []


def test_cancel_after_the_link_was_opened_still_tells_if_it_connects(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        offer = fake.started[0]
        fake.opened.add(offer)
        result = await _call(dp, cancel=True)
        down = list(screen.calls)
        fake.end(offer, "paired", "Firefox on Linux")
        await _turn()
        return result, down, dp, fake, screen

    result, down, dp, fake, screen = asyncio.run(run())
    assert result["ok"] and "off the screen" in result["note"] and "notice" in result["note"]
    assert "nothing was paired" not in result["note"]
    assert down[-2:] == [("clear",), ("close", True)] and fake.cancelled == []
    assert screen.calls[-2][:3] == ("show", "Pair", ["Paired", "Firefox on Linux"])
    [(text, respond)] = dp.notes.added
    assert respond and text == "Your Firefox on Linux is now connected."


def test_the_session_ending_stops_a_confirmation_too(monkeypatch):
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        fake.opened.add(fake.started[0])
        watcher = dp._watcher
        dp.close()
        await _turn()
        return watcher, fake

    watcher, fake = asyncio.run(run())
    assert watcher.done() and fake.cancelled == fake.started


def test_a_new_pairing_after_one_ended_unseen_gets_its_own_watcher(monkeypatch):
    """The last pairing ended (its watcher not run yet) just as the user asks again:
    the new code is a new pairing, followed by a new watcher; the old end is not told
    over the new code, nor does it take the new code down."""
    async def run():
        dp, fake, screen = _session(monkeypatch)
        await _call(dp)
        old = dp._watcher
        fake.end(fake.started[0], "expired")  # ended; old watcher has not run
        await _call(dp)
        await _turn()
        return dp, fake, screen, old

    dp, fake, screen, old = asyncio.run(run())
    assert fake.started[1].session != fake.started[0].session
    assert old.done() and dp._watcher is not None and dp._watcher is not old
    assert dp.live and dp._offer is fake.started[1] and screen.calls[-1][0] == "show"
    assert dp.notes.added == []


def test_a_short_line_covers_the_wait_for_the_code(monkeypatch):
    # Spoken only when the model called the tool without a word (tools._wrap).
    for lang, want in (("en", "One moment, let me set that up."),
                       ("es", "Un momento, déjame prepararlo."),
                       ("it", "Un attimo, lo preparo."),
                       ("cmn", "稍等，我来准备一下。"),
                       ("fr", "One moment, let me set that up.")):
        line = tools._fallback_line("pair_device", {}, lang)
        assert line == want, (lang, line)
        # It promises nothing the outcome may contradict (no screen, unavailable,
        # stopped), and says nothing the tool's own note has the model say next.
        for word in ("code", "screen", "camera", "link", "QR", "código", "codice", "码"):
            assert word not in line, (lang, word)
    for cancel in (True, "true"):
        assert tools._fallback_line("pair_device", {"cancel": cancel}, "en") is None
    assert tools._fallback_line("pair_device", {"cancel": False}, "en")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
