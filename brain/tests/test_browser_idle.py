#
# OpenClaw's idle browser is stopped (browser_idle.py, #385)
#
# What has to hold:
#   - The browser is stopped once nothing has used it for the idle time: no consult of the
#     brain's, no agent session running or updated, no change in its open pages.
#   - Never while a consult runs, nor before the idle time has passed since one ended.
#   - Never while an agent session runs (a Control UI chat the brain knows nothing of),
#     unless that "running" is hours stale; a recent update counts as a use.
#   - A change in the open pages is a use; the first look at them is not.
#   - Unknown is not idle: a sessions read that fails never leads to a stop.
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
    def __init__(self, sessions=(), running=True, sessions_ok=True, stop_takes=True):
        self.sessions = list(sessions)
        self.running = running
        self.sessions_ok = sessions_ok
        self.stop_takes = stop_takes
        self.calls = []
        self.pages = [("p1", "https://example.com/")]

    async def invoke(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "browser" and args["action"] == "status":
            return True, {"running": self.running, "profile": "openclaw",
                          "cdpUrl": "http://127.0.0.1:18800", "pid": None}
        if tool == "browser" and args["action"] == "stop":
            if self.stop_takes:
                self.running = False
            return True, {"running": self.running}
        if tool == "sessions_list":
            return (True, {"sessions": self.sessions}) if self.sessions_ok else (False, {})
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


def test_an_idle_browser_is_stopped_after_the_idle_time_and_not_before():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    assert check(m) == "in-use"           # the brain's start counts as a use
    clock.t += IDLE - 1
    assert check(m) == "in-use" and not gw.stops()
    clock.t += 2
    assert check(m) == "stopped"
    assert gw.stops() == [{"action": "stop", "profile": "openclaw"}]
    clock.t += 10 * IDLE
    assert check(m) == "not-running" and len(gw.stops()) == 1


def test_never_while_a_consult_runs_nor_until_the_idle_time_after_it():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    m.consult(1)
    clock.t += 10 * IDLE
    assert check(m) == "consult" and not gw.stops()
    # Nothing even asked of the gateway while one runs.
    assert gw.calls == []
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
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "in-use"
    clock.t += IDLE - 60 + 1
    assert check(m) == "stopped"


def test_a_running_session_hours_stale_is_a_dead_run_not_a_use():
    clock = Clock()
    dead = {"key": "agent:main:cron:x", "status": "running",
            "updatedAt": (clock.t + 10 * IDLE - browser_idle.STALE_RUN_SECS - 1) * 1000}
    gw = Gateway(sessions=[dead])
    m = _monitor(gw, clock)
    clock.t += 10 * IDLE
    assert check(m) == "stopped"


def test_an_update_stamped_in_the_future_is_now_not_later():
    clock = Clock()
    ahead = {"key": "agent:main:main", "status": "done", "updatedAt": (clock.t + 1e6) * 1000}
    gw = Gateway(sessions=[ahead])
    m = _monitor(gw, clock)
    assert check(m) == "in-use"
    clock.t += IDLE + 1
    ahead["updatedAt"] = 0
    assert check(m) == "stopped"


def test_a_change_in_the_open_pages_is_a_use_and_the_first_look_is_not():
    gw, clock = Gateway(), Clock()
    m = _monitor(gw, clock)
    clock.t += IDLE - 10
    assert check(m) == "in-use"           # the first look at the pages: not a use
    gw.pages = [("p1", "https://example.com/next")]
    clock.t += 20                         # past the idle time since the start...
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

    async def no_gateway(tool, args):
        return False, {}
    m = browser_idle.Monitor(clock=clock, invoke=no_gateway, pages=gw.list_pages,
                             idle_secs=IDLE)
    clock.t += 10 * IDLE
    assert check(m) == "unknown"


def test_a_stop_that_did_not_take_waits_a_full_idle_time():
    gw, clock = Gateway(stop_takes=False), Clock()
    m = _monitor(gw, clock)
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


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
