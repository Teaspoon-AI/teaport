#
# OpenClaw's idle browser is stopped (browser_idle.py, #385)
#
# What has to hold:
#   - The browser is stopped once nothing has used it for the idle time: no consult of the
#     brain's, no agent session running or updated, no change in its open pages.
#   - Never while a consult runs, nor before the idle time has passed since one ended.
#   - Never while an agent session runs (a Control UI chat the brain knows nothing of),
#     however long ago it started, unless it predates the gateway running now; a recent
#     update counts as a use. A gateway whose start cannot be told: every run is live.
#   - The browser starting is a use, and so is a change in its open pages.
#   - Unknown is not idle: a sessions read that fails, a listing restricted to a
#     sandbox's own sessions, or one that may not reach back to the gateway's start never
#     leads to a stop.
#   - An agent run the brain started counts until it ends (tools._held), one called as
#     its session ended included.
#   - Not running, no gateway, or the setting 0: nothing is asked to stop.
#   - The stop names the browser's profile, and a stop that did not take is retried only
#     after another full idle time.
#   - tools._count_consult feeds the brain's count.
#
# A fake gateway and clock; nothing is reached.
#
# Run: python test_browser_idle.py   (or via pytest)
#
import asyncio

import appliance

from teaport_brain import browser_idle

IDLE = 600.0


class Gateway:
    def __init__(self, sessions=(), running=True, sessions_ok=True, stop_takes=True,
                 started=0.0):
        self.sessions = list(sessions)
        self.running = running
        self.sessions_ok = sessions_ok
        self.stop_takes = stop_takes
        self.started = started          # the gateway's start (wall clock), None: unknown
        self.extra = {}                 # more of the sessions listing (hasMore, visibility)
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
            return ((True, {"sessions": self.sessions, **self.extra}) if self.sessions_ok
                    else (False, {}))
        raise AssertionError(tool)

    def gateway_start(self, pid):
        assert pid == self.pid
        return self.started

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
                                gateway_start=gw.gateway_start, idle_secs=idle)


def _seen(gw, clock, idle=IDLE):
    """A monitor that has had its first look at the browser, at the brain's start."""
    m = _monitor(gw, clock, idle)
    if gw.running and idle > 0:
        check(m)
    return m


def check(m):
    return asyncio.run(m.check())


def test_an_idle_browser_is_stopped_after_the_idle_time_and_not_before():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    assert check(m) == "in-use"           # the brain's start counts as a use
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
    gw, clock = Gateway(), Clock()
    m = _seen(gw, clock)
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


def test_an_agent_session_running_or_recently_updated_holds_it():
    clock = Clock()
    # A Control UI chat the brain knows nothing of, still running.
    running = {"key": "agent:main:main", "status": "running",
               "updatedAt": (clock.t - 3 * IDLE) * 1000}
    gw = Gateway(sessions=[running])
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    running["updatedAt"] = (clock.t - 3 * IDLE) * 1000
    assert check(m) == "in-use" and not gw.stops()
    # It finished: the wait counts from when it was last seen running.
    running.update(status="done", updatedAt=(clock.t - 60) * 1000)
    clock.t += IDLE - 1
    assert check(m) == "in-use" and not gw.stops()
    clock.t += 2
    assert check(m) == "stopped"
    # A session updated a minute ago and done: the wait counts from that update.
    gw = Gateway(sessions=[{"key": "agent:main:openai:x", "status": "done",
                            "updatedAt": (clock.t + 10 * IDLE - 60) * 1000}])
    m = _seen(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "in-use"
    clock.t += IDLE - 60 + 1
    assert check(m) == "stopped"


def test_a_run_hours_long_holds_it_and_one_from_before_the_gateway_does_not():
    """OpenClaw lets a run go 48 h, and its row is stamped only when it starts and ends:
    a "running" row is live unless it predates the gateway running now."""
    clock = Clock()
    gw_start = clock.t - 3 * 86400.0
    long_run = {"key": "agent:main:main", "status": "running",
                "updatedAt": (gw_start + 3600) * 1000}
    gw = Gateway(sessions=[long_run], started=gw_start)
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "in-use" and not gw.stops()
    # A row left "running" by a gateway that has since restarted is no run.
    gw = Gateway(sessions=[dict(long_run, updatedAt=(gw_start - 3600) * 1000)],
                 started=gw_start)
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "in-use"           # the browser's first look: a use
    clock.t += IDLE + 1
    assert check(m) == "stopped"
    # The gateway's start cannot be told: every running row is a live run.
    gw = Gateway(sessions=[dict(long_run, updatedAt=0)], started=None)
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    check(m)
    clock.t += 10 * IDLE
    assert check(m) == "in-use" and not gw.stops()


def test_a_listing_that_may_not_reach_the_gateways_start_is_unknown():
    clock = Clock()
    gw_start = clock.t - 86400.0
    done = {"key": "agent:main:openai:x", "status": "done",
            "updatedAt": (gw_start - 10) * 1000}
    gw = Gateway(sessions=[done], started=gw_start)
    gw.extra = {"hasMore": True}
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    check(m)
    clock.t += IDLE + 1
    assert check(m) == "stopped"          # its end is past the gateway's start: complete
    gw = Gateway(sessions=[dict(done, updatedAt=(gw_start + 10) * 1000)], started=gw_start)
    gw.extra = {"hasMore": True}
    m = _monitor(gw, clock)
    for _ in range(5):
        clock.t += 10 * IDLE
        assert check(m) == "unknown" and not gw.stops()
    gw.started = None                     # nor when the gateway's start is unknown
    clock.t += 10 * IDLE
    assert check(m) == "unknown" and not gw.stops()


def test_a_listing_restricted_to_a_sandbox_is_unknown():
    """OpenClaw adds `visibility` to a listing that leaves sessions out (a sandboxed
    agent's "tree" or "spawned" view): what it leaves out could be browsing."""
    gw, clock = Gateway(), Clock()
    gw.extra = {"visibility": {"mode": "tree", "warning": "restricted"}}
    m = _monitor(gw, clock)
    for _ in range(5):
        clock.t += 10 * IDLE
        assert check(m) == "unknown" and not gw.stops()


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


def test_an_update_stamped_in_the_future_is_now_not_later():
    clock = Clock()
    ahead = {"key": "agent:main:main", "status": "done", "updatedAt": (clock.t + 1e6) * 1000}
    gw = Gateway(sessions=[ahead])
    m = _monitor(gw, clock)
    assert check(m) == "in-use"
    clock.t += IDLE + 1
    ahead["updatedAt"] = 0
    assert check(m) == "stopped"


def test_a_change_in_the_open_pages_is_a_use():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    clock.t += IDLE - 10
    assert check(m) == "in-use"           # the browser's first look: a use, now
    clock.t += IDLE - 10
    assert check(m) == "in-use"           # the same pages: no use
    gw.pages = [("p1", "https://example.com/next")]
    clock.t += 20                         # past the idle time since the first look...
    assert check(m) == "in-use" and not gw.stops()   # ...but the page just changed
    clock.t += IDLE + 1
    assert check(m) == "stopped"


def test_unknown_is_not_idle():
    gw, clock = Gateway(sessions_ok=False), Clock()
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "unknown" and not gw.stops()
    gw.sessions_ok = True
    assert check(m) == "in-use" and not gw.stops()   # the failed read counted as a use
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
    m = _seen(gw, clock)
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
        assert held.done() and m._consults == 0
    try:
        asyncio.run(scenario())
    finally:
        browser_idle.MONITOR = saved


def test_the_gateways_start_is_the_browsers_parents():
    import subprocess
    import time as _time
    # A stand-in gateway (its command line names it) with a stand-in browser under it.
    gw = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; wait", "openclaw", "gateway"],
                          stdout=subprocess.PIPE, text=True)
    try:
        child = int(gw.stdout.readline())
        started = browser_idle._gateway_start(child)
        assert started is not None and abs(started - _time.time()) < 5, started
    finally:
        gw.kill()
        gw.wait()
    # A parent that is not the gateway (this test's runner): unknown.
    import os
    assert browser_idle._gateway_start(os.getpid()) is None
    assert browser_idle._gateway_start(None) is None


def test_no_gateway_token_is_asked_nothing_and_said_nothing(monkeypatch=None):
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
