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
#   - an agent run of the gateway's going, whoever started it -- a Control UI chat, Talk,
#     a cron job (sessions_list with activeOnly: the gateway's live run state, not the
#     sessions' stored status, which a run stamps only when it starts and ends, and a run
#     may go 48 h) -- or an agent session stamped since (sessions_list's activeMinutes:
#     a row stamped within the idle time; not the first row, which is a pinned one). The
#     brain cannot see the browser's users any other way;
#   - the browser starting (a new Chromium process: whoever started it is using it);
#   - a change in the browser's open pages (their CDP target list: ids and addresses).
# Whatever cannot be told is a use: the cost of a browser held is memory, the cost of a
# wrong stop a killed run. So no stop while the gateway's sessions cannot be read, or while
# they are listed only in part: OpenClaw's tools.sessions.visibility set to anything but
# "all" (the listing then says so). What sessions_list cannot show at all is a run of
# ANOTHER agent (it lists the default agent's sessions unless agent-to-agent access is
# on): on a box with a second agent that browses, raise the setting or set it to 0. A
# browser held for a reason that can last (a consult, a run, an unknown) is logged once,
# when it starts being held for it.
# OpenClaw exposes no browser "last used" time of its own: its per-session tab registry is
# in-process, and its sweeper loses track of tabs ("ownership mismatch", #385).
#
# What it costs: a timer, and every POLL_SECS one loopback request to the gateway (the
# browser tool's status, ~0.2 s) -- three more, small, only while Chromium runs. Not the
# CLI: one `openclaw browser status` is ~10 s of CPU on the box. The stop is the browser
# tool's own (POST /stop on OpenClaw's browser control, what the CLI's stop calls), over
# /tools/invoke. Nothing in a turn. Runs in teaport-brain's loop (gateway_server's
# lifespan), which serves the room, Talk and the phone line alike, so every consult of the
# box's is counted here.
#
import asyncio
import json
import math
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
# Rows asked for of the sessions stamped lately. Pinned ones sort first, so a page this
# full may hold back a newer stamp: then the browser counts as used now.
_RECENT_ROWS = 3
# OpenClaw's own stop waits up to 15 s for Chromium to exit.
_STOP_TIMEOUT_SECS = 20.0
# The longest a consult of the brain's can be in flight: the longest of its ceilings, and
# a margin for the agent's run to wind down after it. Counted past this, a start was
# never matched by its end (a leak), which holds the browser for good: said once.
_CONSULT_CEILING_SECS = max(setting("TEAPORT_ASYNC_CONSULT_TIMEOUT"),
                            setting("TEAPORT_OPENCLAW_AGENT_CONSULT_TIMEOUT"),
                            setting("TEAPORT_CONSULT_TIMEOUT")) + 300.0
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


def _listed(ok: bool, details: dict) -> list | None:
    """A sessions_list answer's rows, or None when it cannot be taken as the whole
    picture: the call failed, or OpenClaw listed only part (it adds `visibility` to a
    listing whose tools.sessions.visibility is not "all")."""
    if not ok or details.get("visibility") is not None:
        return None
    rows = details.get("sessions")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


class Monitor:
    """`invoke(tool, args, timeout=...)` is oc.invoke_ok and `pages(cdp_url)` _page_list,
    replaced in tests; `clock` is wall time (the gateway's updatedAt is wall-clock
    milliseconds)."""

    def __init__(self, clock=time.time, invoke=None, pages=None, idle_secs=None):
        self._clock = clock
        self._invoke = invoke or (lambda tool, args, timeout=5.0:
                                  oc.invoke_ok(tool, args, timeout=timeout))
        self._pages = pages or (lambda url: asyncio.to_thread(_page_list, url))
        self._idle_secs = float(IDLE_SECS if idle_secs is None else idle_secs)
        self._consults = 0
        self._busy_since = None        # when the consult count last left 0
        self._leak_told = False
        self._last_use = clock()       # the brain's start counts as a use: a full wait first
        self._seen_pages = None
        self._seen_pid = None
        self._held_for = None          # the reason last logged for holding the browser

    # -- the consults (tools._count_consult) and the agent runs the brain started
    #    (tools._held)
    def consult(self, delta: int) -> None:
        """A consult or an agent run of this brain's started (+1) or ended (-1). Either
        is a use."""
        self._consults = max(0, self._consults + delta)
        now = self._clock()
        self._last_use = max(self._last_use, now)
        if not self._consults:
            self._busy_since, self._leak_told = None, False
        elif self._busy_since is None:
            self._busy_since = now

    def _used(self, at: float) -> None:
        self._last_use = max(self._last_use, min(at, self._clock()))

    def _held(self, reason: str | None) -> None:
        """Log `reason` for holding the browser when it is a new one (None: no reason that
        can last, the browser was only used lately)."""
        if reason is not None and reason != self._held_for:
            logger.info(f"browser idle stop: leaving OpenClaw's browser running: {reason}")
        self._held_for = reason

    async def check(self) -> str:
        """One look, and the stop when it is due. What it found, for the log and tests:
        off, consult, not-running, unknown, running, in-use, stopped or stop-failed."""
        if self._idle_secs <= 0:
            return "off"
        if self._consults:
            now = self._clock()
            self._used(now)
            if (not self._leak_told and self._busy_since is not None
                    and now - self._busy_since > _CONSULT_CEILING_SECS):
                self._leak_told = True
                logger.warning(f"browser idle stop: a consult has been counted as running "
                               f"for {(now - self._busy_since) / 60:.0f} min, past any "
                               f"consult's ceiling: a start without its end? The browser "
                               f"is not stopped while it counts")
            return "consult"
        # The default profile's browser (OpenClaw's browser.defaultProfile): the one the
        # agent's browser tool uses unless told otherwise. A run that names another
        # profile still shows as a live run below.
        ok, status = await self._invoke("browser", {"action": "status"})
        if not ok:
            self._held(None)
            return "unknown"           # no gateway, or no browser tool: nothing to stop
        if not status.get("running"):
            self._seen_pages = self._seen_pid = None
            self._held(None)
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
        # Any agent run going now, by the gateway's live run state (2026.9.7 and later;
        # an older gateway refuses activeOnly, which is unknown).
        active = _listed(*await self._invoke("sessions_list",
                                             {"activeOnly": True, "limit": 1}))
        # The sessions stamped within the idle time (and a little more: the stop is judged
        # on the newest of them). By activeMinutes, which OpenClaw applies before its sort
        # and limit: the first row of a plain listing is a pinned session, however old,
        # and the rows it drops after the limit ("global", "unknown") could leave none.
        newest = _listed(*await self._invoke("sessions_list", {
            "activeMinutes": math.ceil(self._idle_secs / 60) + 2, "limit": _RECENT_ROWS}))
        now = self._clock()
        if active is None or newest is None:
            self._used(now)
            self._held("can't tell whether an agent is using it (the gateway's sessions "
                       "could not be read in full)")
            return "unknown"
        if active:
            self._used(now)
            self._held("an agent run is going")
            return "running"
        for row in newest:
            if isinstance(row.get("updatedAt"), (int, float)):
                self._used(row["updatedAt"] / 1000.0)
        if len(newest) >= _RECENT_ROWS:
            self._used(now)            # a full page: a newer stamp may be past it
        self._held(None)
        idle = now - self._last_use
        if idle < self._idle_secs or self._consults:
            return "in-use"
        rss = _tree_rss_mb(pid)
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
