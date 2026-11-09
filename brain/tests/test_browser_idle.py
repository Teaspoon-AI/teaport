#
# OpenClaw's idle browser is stopped (browser_idle.py, #385)
#
# What has to hold:
#   - The browser is stopped once nothing has used it for the idle time: no consult of the
#     brain's, no agent run going, no agent session stamped since, no browser start, no
#     change in its open pages.
#   - Never while a consult runs, nor before the idle time has passed since one ended.
#   - Never while the gateway has an agent run going (a Control UI chat the brain knows
#     nothing of), however long ago it started: its live run state, not a stored status.
#   - Unknown is not idle: a sessions read that fails (activeOnly refused by an older
#     gateway included), or a listing OpenClaw says is partial, never leads to a stop.
#   - A browser held for a reason that can last (a run, an unknown) is logged once when
#     the reason starts; a consult counted past any ceiling is warned about once.
#   - Not running, no gateway, no token, or the setting 0: nothing is asked to stop.
#   - The stop names the browser's profile and gets longer than a status read; a stop
#     that did not take is retried only after another full idle time.
#   - tools._count_consult and tools._held (agent runs the brain started, one called as
#     its session ended included) feed the brain's count.
#
# A fake gateway and clock; nothing is reached.
#
# Run: python test_browser_idle.py   (or via pytest)
#
import asyncio
import contextlib

import appliance

from loguru import logger

from teaport_brain import browser_idle

IDLE = 600.0


class Gateway:
    def __init__(self, sessions=(), running=True, sessions_ok=True, stop_takes=True):
        self.sessions = list(sessions)  # the stored rows, newest first
        self.active = []                # the rows with a live run (activeOnly)
        self.running = running
        self.sessions_ok = sessions_ok
        self.active_ok = True           # an older gateway refuses activeOnly
        self.stop_takes = stop_takes
        self.extra = {}                 # more of each sessions listing (visibility)
        self.pid = 4242
        self.calls = []
        self.timeouts = {}
        self.pages = [("p1", "https://example.com/")]

    async def invoke(self, tool, args, timeout=5.0):
        self.calls.append((tool, dict(args)))
        self.timeouts[(tool, args.get("action"))] = timeout
        if tool == "browser" and args["action"] == "status":
            return True, {"running": self.running, "profile": "openclaw",
                          "cdpUrl": "http://127.0.0.1:18800",
                          "pid": self.pid if self.running else None}
        if tool == "browser" and args["action"] == "stop":
            if self.stop_takes:
                self.running = False
            return True, {"running": self.running}
        if tool == "sessions_list":
            if not self.sessions_ok or (args.get("activeOnly") and not self.active_ok):
                return False, {}
            rows = self.active if args.get("activeOnly") else self.sessions
            return True, {"sessions": rows[:args.get("limit", 100)], **self.extra}
        raise AssertionError(tool)

    async def list_pages(self, url):
        assert url == "http://127.0.0.1:18800"
        return list(self.pages)

    def stops(self):
        return [a for t, a in self.calls if t == "browser" and a["action"] == "stop"]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _monitor(gw, clock, idle=IDLE):
    return browser_idle.Monitor(clock=clock, invoke=gw.invoke, pages=gw.list_pages,
                                idle_secs=idle)


def check(m):
    return asyncio.run(m.check())


@contextlib.contextmanager
def _logged():
    lines = []
    sink = logger.add(lambda msg: lines.append((msg.record["level"].name,
                                                msg.record["message"])),
                      filter=lambda r: r["name"] == "teaport_brain.browser_idle")
    try:
        yield lines
    finally:
        logger.remove(sink)


def test_an_idle_browser_is_stopped_after_the_idle_time_and_not_before():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    assert check(m) == "in-use"           # the brain's start, and the browser seen
    clock.t += IDLE - 1
    assert check(m) == "in-use" and not gw.stops()
    clock.t += 2
    assert check(m) == "stopped"
    assert gw.stops() == [{"action": "stop", "profile": "openclaw"}]
    # OpenClaw's stop waits up to 15 s for Chromium: more than a status read gets.
    assert gw.timeouts[("browser", "stop")] > 15 > gw.timeouts[("browser", "status")]
    clock.t += 10 * IDLE
    assert check(m) == "not-running" and len(gw.stops()) == 1


def test_never_while_a_consult_runs_nor_until_the_idle_time_after_it():
    """Built as the first version was (no argument of the later reviews'), so this case
    reads the same on every version of the monitor."""
    gw, clock = Gateway(), Clock()
    m = browser_idle.Monitor(clock=clock, invoke=gw.invoke, pages=gw.list_pages,
                             idle_secs=IDLE)
    check(m)
    asked = len(gw.calls)
    m.consult(1)
    clock.t += 10 * IDLE
    assert check(m) == "consult" and not gw.stops()
    # Nothing even asked of the gateway while one runs.
    assert len(gw.calls) == asked
    m.consult(-1)                         # ended now: a full wait from here
    clock.t += IDLE - 1
    assert check(m) == "in-use" and not gw.stops()
    clock.t += 2
    assert check(m) == "stopped"


def test_a_run_going_holds_it_however_long_ago_it_started():
    """OpenClaw lets a run go 48 h, and its stored row is stamped only when it starts and
    ends: the gateway's live run state (activeOnly) is what says it is going."""
    clock = Clock()
    run = {"key": "agent:main:main", "status": "running",
           "updatedAt": (clock.t - 30 * 3600) * 1000}
    gw = Gateway(sessions=[run])
    gw.active = [run]
    m = _monitor(gw, clock)
    for _ in range(5):
        clock.t += 10 * IDLE
        assert check(m) == "running" and not gw.stops()
    # It ended, stamped now: a full wait from its end.
    gw.active = []
    run.update(status="done", updatedAt=clock.t * 1000)
    clock.t += 60
    assert check(m) == "in-use"
    clock.t += IDLE - 60 + 1
    assert check(m) == "stopped"


def test_a_stored_running_status_without_a_live_run_is_no_use():
    """A row a gateway restart left "running" holds nothing: only the live state does."""
    clock = Clock()
    dead = {"key": "agent:main:cron:x", "status": "running",
            "updatedAt": (clock.t - 30 * 3600) * 1000}
    gw = Gateway(sessions=[dead])
    m = _monitor(gw, clock)
    check(m)
    clock.t += IDLE + 1
    assert check(m) == "stopped"


def test_a_session_stamped_lately_holds_it_from_its_stamp():
    clock = Clock()
    done = {"key": "agent:main:openai:x", "status": "done",
            "updatedAt": (clock.t + 10 * IDLE - 60) * 1000}
    gw = Gateway(sessions=[done])
    m = _monitor(gw, clock)
    check(m)
    clock.t += 10 * IDLE
    assert check(m) == "in-use"
    clock.t += IDLE - 60 + 1
    assert check(m) == "stopped"


def test_however_long_the_gateway_has_run_the_newest_stamp_is_enough():
    """Round 2 of the review: a listing that had to reach back to the gateway's start
    stopped nothing once 200 rows post-dated it. Only the newest row is read now."""
    clock = Clock()
    rows = [{"key": f"agent:main:openai:{i}", "status": "done",
             "updatedAt": (clock.t - 3600 * (i + 1)) * 1000} for i in range(500)]
    gw = Gateway(sessions=rows)
    gw.extra = {"hasMore": True}
    m = _monitor(gw, clock)
    check(m)
    clock.t += IDLE + 1
    assert check(m) == "stopped"
    asked = [a for t, a in gw.calls if t == "sessions_list"]
    assert all(a.get("limit") == 1 for a in asked), asked


def test_an_update_stamped_in_the_future_is_now_not_later():
    clock = Clock()
    ahead = {"key": "agent:main:main", "status": "done", "updatedAt": (clock.t + 1e6) * 1000}
    gw = Gateway(sessions=[ahead])
    m = _monitor(gw, clock)
    assert check(m) == "in-use"
    clock.t += IDLE + 1
    ahead["updatedAt"] = 0
    assert check(m) == "stopped"


def test_unknown_is_not_idle():
    # The sessions cannot be read.
    gw, clock = Gateway(sessions_ok=False), Clock()
    m = _monitor(gw, clock)
    for _ in range(3):
        clock.t += 10 * IDLE
        assert check(m) == "unknown" and not gw.stops()
    gw.sessions_ok = True
    assert check(m) == "in-use" and not gw.stops()   # the failed read counted as a use
    clock.t += IDLE + 1
    assert check(m) == "stopped"
    # A gateway that refuses activeOnly (older than 2026.9.7): its live runs are unknown.
    gw, clock = Gateway(), Clock()
    gw.active_ok = False
    m = _monitor(gw, clock)
    for _ in range(3):
        clock.t += 10 * IDLE
        assert check(m) == "unknown" and not gw.stops()
    # A listing OpenClaw marks partial (tools.sessions.visibility not "all").
    gw, clock = Gateway(), Clock()
    gw.extra = {"visibility": {"mode": "tree", "warning": "restricted"}}
    m = _monitor(gw, clock)
    for _ in range(3):
        clock.t += 10 * IDLE
        assert check(m) == "unknown" and not gw.stops()


def test_a_lasting_reason_is_logged_once_when_it_starts():
    gw, clock = Gateway(sessions_ok=False), Clock()
    m = _monitor(gw, clock)
    with _logged() as lines:
        for _ in range(4):
            clock.t += 10 * IDLE
            check(m)
        gw.sessions_ok, gw.active = True, [{"key": "agent:main:main", "updatedAt": 0}]
        for _ in range(3):
            clock.t += 10 * IDLE
            check(m)
        gw.active = []                    # the run ended: a recent use, no lasting reason
        clock.t += 60
        check(m)
        gw.sessions_ok = False            # unknown again: said again
        clock.t += 60
        check(m)
    held = [text for level, text in lines if "leaving OpenClaw's browser running" in text]
    assert len(held) == 3, held
    assert "can't tell" in held[0] and "agent run is going" in held[1] and "can't tell" in held[2]


def test_a_consult_counted_past_any_ceiling_is_warned_about_once():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    m.consult(1)
    with _logged() as lines:
        clock.t += browser_idle._CONSULT_CEILING_SECS - 1
        check(m)
        assert not [t for lvl, t in lines if lvl == "WARNING"]
        for _ in range(3):
            clock.t += 120
            assert check(m) == "consult"
        m.consult(-1)
        m.consult(1)                      # a new stretch: its own clock
        clock.t += 120
        check(m)
    warned = [t for lvl, t in lines if lvl == "WARNING"]
    assert len(warned) == 1 and "past any consult's ceiling" in warned[0], warned
    assert not gw.stops()


def test_the_browser_starting_is_a_use():
    """A run the brain cannot count started it: a full idle time from its first look."""
    gw, clock = Gateway(running=False), Clock()
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "not-running"
    gw.running = True
    clock.t += 60
    assert check(m) == "in-use" and not gw.stops()
    clock.t += IDLE - 1
    assert check(m) == "in-use"
    clock.t += 2
    assert check(m) == "stopped"
    # Restarted between two looks, with no look seeing it down: a new process, a use.
    gw.running, gw.pid = True, 5151
    clock.t += 10 * IDLE
    assert check(m) == "in-use" and len(gw.stops()) == 1


def test_a_change_in_the_open_pages_is_a_use():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    assert check(m) == "in-use"           # the browser's first look: a use, now
    clock.t += IDLE - 10
    assert check(m) == "in-use"           # the same pages: no use
    gw.pages = [("p1", "https://example.com/next")]
    clock.t += 20                         # past the idle time since the first look...
    assert check(m) == "in-use" and not gw.stops()   # ...but the page just changed
    clock.t += IDLE + 1
    assert check(m) == "stopped"


def test_nothing_is_stopped_when_off_not_running_or_without_a_gateway():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock, idle=0)
    clock.t += 10 * IDLE
    assert check(m) == "off" and gw.calls == []
    gw = Gateway(running=False)
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "not-running" and not gw.stops()

    async def no_gateway(tool, args, timeout=5.0):
        return False, {}
    m = browser_idle.Monitor(clock=clock, invoke=no_gateway, pages=gw.list_pages,
                             idle_secs=IDLE)
    clock.t += 10 * IDLE
    assert check(m) == "unknown"


def test_a_stop_that_did_not_take_waits_a_full_idle_time():
    gw, clock = Gateway(stop_takes=False), Clock()
    m = _monitor(gw, clock)
    check(m)
    clock.t += IDLE + 1
    assert check(m) == "stop-failed" and len(gw.stops()) == 1
    clock.t += IDLE - 1
    assert check(m) == "in-use" and len(gw.stops()) == 1
    clock.t += 2
    assert check(m) == "stop-failed" and len(gw.stops()) == 2


def test_pages_are_read_on_loopback_only():
    assert browser_idle._page_list("http://192.168.1.9:18800") is None
    assert browser_idle._page_list("ws://127.0.0.1:18800") is None
    assert browser_idle._page_list("") is None


def test_the_consult_count_feeds_the_brains():
    from teaport_brain import tools
    saved = browser_idle.MONITOR
    browser_idle.MONITOR = m = _monitor(Gateway(), Clock())
    try:
        tools._count_consult(None, 1)
        assert m._consults == 1
        gate = type("Gate", (), {})()
        tools._count_consult(gate, 1)
        tools._count_consult(gate, -1)
        tools._count_consult(None, -1)
        assert m._consults == 0 and gate.consults_running == 0
        tools._count_consult(None, -1)    # never below none
        assert m._consults == 0
    finally:
        browser_idle.MONITOR = saved


def test_an_agent_run_the_brain_started_counts_until_it_ends():
    """tools._held: a run called as its session ended (_run_detached) has no wait that
    _count_consult sees, and one that outlives its wait (_run_to_its_end) runs on."""
    from teaport_brain import tools
    saved = browser_idle.MONITOR
    browser_idle.MONITOR = m = _monitor(Gateway(), Clock())

    async def scenario():
        release = asyncio.Event()

        async def run():
            await release.wait()
            return None

        tools._run_detached(run())
        await asyncio.sleep(0)
        assert m._consults == 1
        assert await m.check() == "consult"
        release.set()
        await asyncio.sleep(0.01)
        assert m._consults == 0

        async def fails():
            raise RuntimeError("the agent failed")
        held = tools._held(fails())
        await asyncio.sleep(0.01)
        assert held.done() and isinstance(held.exception(), RuntimeError) and m._consults == 0
    try:
        asyncio.run(scenario())
    finally:
        browser_idle.MONITOR = saved


def test_no_gateway_token_is_asked_nothing_and_said_nothing():
    from teaport_brain import openclaw_client as oc
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    saved = (browser_idle.POLL_SECS, browser_idle.HAS_AGENT, oc._token)
    browser_idle.POLL_SECS, browser_idle.HAS_AGENT = 0.001, True
    oc._token = lambda: None

    async def a_while():
        try:
            await asyncio.wait_for(m.run(), 0.05)
        except asyncio.TimeoutError:
            pass
    try:
        asyncio.run(a_while())
    finally:
        browser_idle.POLL_SECS, browser_idle.HAS_AGENT, oc._token = saved
    assert gw.calls == []


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
