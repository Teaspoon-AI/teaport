#
# teaport — stop OpenClaw's idle browser (#385)
#
# A consult that makes the agent use its `browser` tool has the gateway start Chromium
# (one profile, a child of the gateway), and OpenClaw never stops it: on the 8 GB box it
# held ~1 GB for 9.5 h on 11-07, and under that pressure agent turns took up to 16 s just to
# start (11-09). OpenClaw starts it again on the next browse, in a few seconds, with the
# profile's logins and cookies intact (they live in its user-data directory). So once the
# browser has been idle for TEAPORT_BROWSER_IDLE_STOP_SECS, the brain stops it, the way
# `openclaw browser stop` does.
#
# Idle means none of these for that long:
#   - a consult of this brain's in flight, or ended (tools._count_consult: the brain's own
#     start and end of each consult);
#   - an agent run the brain started still going (tools._held: one called as its session
#     ended, or one that outlived its session's wait);
#   - an agent session of the gateway's running, or updated (sessions_list: every agent
#     run, whoever started it -- a Control UI chat, Talk, a cron job -- marks its session
#     running when it starts and stamps it when it ends; the brain cannot see the
#     browser's users any other way). A run may go on for hours (OpenClaw's agent timeout
#     is 48 h), so a "running" row is a live run unless it predates the gateway that is
#     running now, which a run cannot outlive;
#   - the browser starting (a new Chromium process: whoever started it is using it);
#   - a change in the browser's open pages (their CDP target list: ids and addresses).
# Whatever cannot be told is a use: the cost of a browser held is memory, the cost of a
# wrong stop a killed run. So no stop while the gateway's sessions cannot be read, while
# its listing is restricted (a sandboxed agent's "tree" or "spawned" visibility), or
# while it may not reach back to the gateway's start. What sessions_list cannot show at
# all is a run of ANOTHER agent (it lists the default agent's sessions unless agent-to-
# agent access is on): on a box with a second agent that browses, raise the setting or
# set it to 0.
# OpenClaw exposes no browser "last used" time of its own: its per-session tab registry is
# in-process, and its sweeper loses track of tabs ("ownership mismatch", #385).
#
# What it costs: a timer, and every POLL_SECS one loopback request to the gateway (the
# browser tool's status, ~0.2 s) -- two more only while Chromium runs. Not the CLI: one
# `openclaw browser status` is ~10 s of CPU on the box. The stop is the browser tool's own
# (POST /stop on OpenClaw's browser control, what the CLI's stop calls), over /tools/invoke.
# Nothing in a turn. Runs in teaport-brain's loop (gateway_server's lifespan), which serves
# the room, Talk and the phone line alike, so every consult of the box's is counted here.
#
import asyncio
import json
import os
import time
import urllib.request
from urllib.parse import urlparse

from loguru import logger

from teaport_brain import openclaw_client as oc
from teaport_brain.agent_backend import HAS_AGENT
from teaport_brain.settings import setting

IDLE_SECS = setting("TEAPORT_BROWSER_IDLE_STOP_SECS")
# How often the browser is looked at: the stop comes up to this much later than IDLE_SECS
# after the last use.
POLL_SECS = 120.0
# Agent sessions asked for, newest first: OpenClaw's own cap. A run's session is stamped
# when it starts and when it ends, not in between (OpenClaw's session-lifecycle-state.ts),
# so a running one sorts by its start, and the cap is applied before the visibility
# filter. A listing with more past it must reach back past the gateway's start, or a run
# older than its oldest row could be missed (the box's 200 rows went back 200 h, 11-09).
SESSIONS_LOOKED_AT = 200
# A "running" row stamped this close before the gateway's start is still its run (the
# clocks are read apart).
_START_SLACK_SECS = 60.0
# OpenClaw's own stop waits up to 15 s for Chromium to exit.
_STOP_TIMEOUT_SECS = 20.0
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _page_list(cdp_url: str, timeout: float = 3.0) -> list | None:
    """The browser's open pages (CDP /json/list), or None when it cannot be read. Only on
    loopback: the brain reads no other host's browser."""
    parsed = urlparse(cdp_url or "")
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK:
        return None
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/list", timeout=timeout) as r:
            targets = json.load(r)
    except (OSError, ValueError):
        return None
    if not isinstance(targets, list):
        return None
    return sorted((str(t.get("id")), str(t.get("url")))
                  for t in targets if isinstance(t, dict) and t.get("type") == "page")


def _gateway_start(browser_pid) -> float | None:
    """When the gateway that runs the browser started (wall clock), or None when that
    cannot be told. Chromium is the gateway's child: its parent's start, from /proc."""
    if not isinstance(browser_pid, int) or browser_pid <= 0:
        return None
    try:
        with open(f"/proc/{browser_pid}/stat") as f:
            ppid = int(f.read().rsplit(") ", 1)[1].split()[1])
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            cmdline = f.read()
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError, StopIteration):
        return None
    argv = cmdline.split(b"\0")
    if b"gateway" not in argv or not any(b"openclaw" in a for a in argv):
        return None           # not the gateway's own child (an attached or sandboxed browser)
    start = oc._proc_start(ppid)
    return btime + start / ticks if start is not None and ticks > 0 else None


def _tree_rss_mb(pid) -> int | None:
    """Resident memory of process `pid` and its children, in MB (for the log line)."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    total = 0
    for p, _start in oc._tree(pid):
        try:
            with open(f"/proc/{p}/statm") as f:
                total += int(f.read().split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return total * 4096 // (1 << 20) if total else None


class Monitor:
    """`invoke(tool, args, timeout)` is oc.invoke_ok, `pages(cdp_url)` _page_list and
    `gateway_start(pid)` _gateway_start, replaced in tests; `clock` is wall time (the
    gateway's updatedAt is wall-clock milliseconds)."""

    def __init__(self, clock=time.time, invoke=None, pages=None, gateway_start=None,
                 idle_secs=None):
        self._clock = clock
        self._invoke = invoke or (lambda tool, args, timeout=5.0:
                                  oc.invoke_ok(tool, args, timeout=timeout))
        self._pages = pages or (lambda url: asyncio.to_thread(_page_list, url))
        self._gateway_start = gateway_start or _gateway_start
        self._idle_secs = float(IDLE_SECS if idle_secs is None else idle_secs)
        self._consults = 0
        self._last_use = clock()       # the brain's start counts as a use: a full wait first
        self._seen_pages = None
        self._seen_pid = None

    # -- the consults (tools._count_consult) and the agent runs the brain started
    #    (tools._held)
    def consult(self, delta: int) -> None:
        """A consult or an agent run of this brain's started (+1) or ended (-1). Either
        is a use."""
        self._consults = max(0, self._consults + delta)
        self._last_use = max(self._last_use, self._clock())

    def _used(self, at: float) -> None:
        self._last_use = max(self._last_use, min(at, self._clock()))

    async def check(self) -> str:
        """One look, and the stop when it is due. What it found, for the log and tests:
        off, consult, not-running, unknown, in-use, stopped or stop-failed."""
        if self._idle_secs <= 0:
            return "off"
        if self._consults:
            self._used(self._clock())
            return "consult"
        # The default profile's browser (OpenClaw's browser.defaultProfile): the one the
        # agent's browser tool uses unless told otherwise. A run that names another
        # profile still shows as a running session below.
        ok, status = await self._invoke("browser", {"action": "status"})
        if not ok:
            return "unknown"           # no gateway, or no browser tool: nothing to stop
        if not status.get("running"):
            self._seen_pages = self._seen_pid = None
            return "not-running"
        pid = status.get("pid")
        if pid != self._seen_pid:      # started since the last look (or a first look)
            self._used(self._clock())
            self._seen_pid = pid
        pages = await self._pages(status.get("cdpUrl"))
        if pages is not None:
            if pages != self._seen_pages:
                if self._seen_pages is not None:
                    self._used(self._clock())
                self._seen_pages = pages
        ok, listed = await self._invoke("sessions_list", {"limit": SESSIONS_LOOKED_AT})
        if not ok or listed.get("visibility") is not None:
            # The agent's activity is unknown, or only partly shown (OpenClaw adds
            # `visibility` to a listing restricted to a sandboxed agent's own sessions):
            # never a stop on it.
            self._used(self._clock())
            return "unknown"
        now = self._clock()
        started = self._gateway_start(pid)
        oldest = None
        for s in listed.get("sessions") or ():
            if not isinstance(s, dict) or not isinstance(s.get("updatedAt"), (int, float)):
                continue
            updated = s["updatedAt"] / 1000.0
            oldest = updated if oldest is None else min(oldest, updated)
            if s.get("status") == "running" and (
                    started is None or updated >= started - _START_SLACK_SECS):
                self._used(now)        # a live run: stamped by the gateway running now
            else:
                self._used(updated)
        if listed.get("hasMore") and (started is None or oldest is None or oldest > started):
            self._used(now)            # a run older than the listing's end may be missed
            return "unknown"
        idle = now - self._last_use
        if idle < self._idle_secs or self._consults:
            return "in-use"
        rss = _tree_rss_mb(status.get("pid"))
        args = {"action": "stop"}
        if isinstance(status.get("profile"), str) and status["profile"]:
            args["profile"] = status["profile"]
        stopped, after = await self._invoke("browser", args, timeout=_STOP_TIMEOUT_SECS)
        self._seen_pages = None
        self._used(self._clock())      # a failed stop is tried again after a full wait
        if not stopped or after.get("running"):
            logger.warning(f"browser idle stop: OpenClaw's browser has been idle for "
                           f"{idle / 60:.0f} min, but it did not stop")
            return "stop-failed"
        logger.info(f"browser idle stop: stopped OpenClaw's browser after {idle / 60:.0f} "
                    f"min idle" + (f" (it held ~{rss} MB)" if rss else ""))
        return "stopped"

    async def run(self) -> None:
        """The brain's loop (gateway_server's lifespan). Nothing without an agent gateway,
        or with the stop off (TEAPORT_BROWSER_IDLE_STOP_SECS=0)."""
        if not HAS_AGENT:
            return
        if self._idle_secs <= 0:
            logger.info("browser idle stop: off (TEAPORT_BROWSER_IDLE_STOP_SECS=0)")
            return
        while True:
            await asyncio.sleep(POLL_SECS)
            if not oc._token():
                continue               # no gateway token (yet): nothing to ask, and quietly
            try:
                await self.check()
            except Exception as e:  # noqa: BLE001 -- the loop must outlive a bad look
                logger.warning(f"browser idle stop: the look failed: {e!r}")


MONITOR = Monitor()
