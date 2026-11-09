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
#   - an agent session of the gateway's running, or updated (sessions_list: every agent
#     run, whoever started it -- a Control UI chat, Talk, a cron job -- marks its session
#     running when it starts and stamps it when it ends; the brain cannot see the
#     browser's users any other way);
#   - a change in the browser's open pages (their CDP target list: ids and addresses).
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
# An agent session that says "running" but was stamped (started) this long ago is a run
# that died without saying so, not a use of the browser (it would hold it up for good).
STALE_RUN_SECS = 2 * 3600.0
# Agent sessions looked at, newest first. A run's session is stamped when it starts and
# when it ends, not in between (OpenClaw's session-lifecycle-state.ts), so one running is
# found by its status, among the sessions stamped since it started: a day of the box's
# consults is a few dozen.
SESSIONS_LOOKED_AT = 50
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
    """`invoke(tool, args)` is oc.invoke_ok and `pages(cdp_url)` _page_list, replaced in
    tests; `clock` is wall time (the gateway's updatedAt is wall-clock milliseconds)."""

    def __init__(self, clock=time.time, invoke=None, pages=None, idle_secs=None):
        self._clock = clock
        self._invoke = invoke or (lambda tool, args: oc.invoke_ok(tool, args, timeout=5.0))
        self._pages = pages or (lambda url: asyncio.to_thread(_page_list, url))
        self._idle_secs = float(IDLE_SECS if idle_secs is None else idle_secs)
        self._consults = 0
        self._last_use = clock()       # the brain's start counts as a use: a full wait first
        self._seen_pages = None

    # -- the consults (tools._count_consult)
    def consult(self, delta: int) -> None:
        """A consult of this brain's started (+1) or ended (-1). Either is a use."""
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
        ok, status = await self._invoke("browser", {"action": "status"})
        if not ok:
            return "unknown"           # no gateway, or no browser tool: nothing to stop
        if not status.get("running"):
            self._seen_pages = None
            return "not-running"
        pages = await self._pages(status.get("cdpUrl"))
        if pages is not None:
            if pages != self._seen_pages:
                if self._seen_pages is not None:
                    self._used(self._clock())
                self._seen_pages = pages
        ok, listed = await self._invoke("sessions_list", {"limit": SESSIONS_LOOKED_AT})
        if not ok:
            self._used(self._clock())  # the agent's activity is unknown: never a stop on it
            return "unknown"
        now = self._clock()
        for s in listed.get("sessions") or ():
            if not isinstance(s, dict) or not isinstance(s.get("updatedAt"), (int, float)):
                continue
            updated = s["updatedAt"] / 1000.0
            if s.get("status") == "running" and now - updated < STALE_RUN_SECS:
                self._used(now)
            else:
                self._used(updated)
        idle = now - self._last_use
        if idle < self._idle_secs or self._consults:
            return "in-use"
        rss = _tree_rss_mb(status.get("pid"))
        args = {"action": "stop"}
        if isinstance(status.get("profile"), str) and status["profile"]:
            args["profile"] = status["profile"]
        stopped, after = await self._invoke("browser", args)
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
            try:
                await self.check()
            except Exception as e:  # noqa: BLE001 -- the loop must outlive a bad look
                logger.warning(f"browser idle stop: the look failed: {e!r}")


MONITOR = Monitor()
