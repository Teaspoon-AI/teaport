#
# teaport — pair a laptop or phone by scanning a QR code on the box (issues #243, #244).
#
# OpenClaw already has the handoffs. `openclaw dashboard --json` mints a browserUrl, a
# link to the Control UI whose URL fragment carries a single-use, ten-minute owner
# bootstrap: the browser that opens it is left paired as an administrator device, with
# no `openclaw devices approve` step. `openclaw qr --json --limited` mints a setup code
# for the OpenClaw phone app (its node token and an operator token without
# operator.admin), which the app's Add Gateway takes pasted; the official app is
# approved by itself. What stands between those and a phone camera is this module:
#
#   - the browserUrl points at loopback. The gateway listens on 127.0.0.1 behind the
#     front door (Caddy, install.sh write_caddyfile), so the minted
#     http://127.0.0.1:18789/#bootstrapToken=...&bootstrapProfile=owner&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789
#     is rewritten as OpenClaw's own docs say (web/control-ui/connect-and-pair.md): its
#     base becomes https://<front door>/ and its gatewayUrl fragment value wss://<front
#     door>, every other fragment value kept byte for byte. The front door is
#     TEAPORT_FRONTDOOR_HOST, never a request's Host header (whatever the asker typed).
#     The setup code needs no rewrite: the device-pair plugin's publicUrl, which the
#     installer sets to wss://<front door>, is already its gateway URL; one that names
#     loopback is refused.
#   - both are far too long for the OLED. The avatar draws a scannable QR code of at
#     most 53 bytes (display.Screen.fits_qr); the bootstrap alone is longer, and a setup
#     code is ~160 bytes. So the screen shows https://<front door>/p/<code> instead, ~30
#     bytes, and /p/<code> (router, below; Caddy routes /p/* to the brain) is a page in
#     the phone's browser with two buttons: "Open the dashboard here" and "Use the
#     OpenClaw app". The GET spends nothing, so a link preview or a prefetch cannot use
#     the code up; the button's POST spends it, and only then is the handoff minted:
#     the dashboard's answered with a 303 to the rewritten browserUrl (the fragment
#     rides in the Location header, and a browser never sends a fragment to a server),
#     the app's with a page that shows the setup code once, with a Copy button and the
#     app's steps. Neither is ever logged, the access log's request line has the code
#     masked (gateway_server._RedactTokens: /p/<redacted>), and both pages are
#     no-store. /p/ answers only what came through Caddy over HTTPS
#     (_via_front_door); the brain's own port, open to the LAN for the config page,
#     gets a 404 there that does not count as a guess.
#
# Minting at the tap, not at start(), is the choice made in #244: the screen shows the
# code as soon as the device list is read (a few seconds, not the ~13 s a dashboard
# mint took on the box), no owner bootstrap exists for a code nobody uses, and only the
# handoff the person chose is made. The wait moves behind the button ("Starting, this
# can take a few seconds"; the buttons go grey, as a second tap would find the code
# spent and replace the handoff with "already used"). A mint that fails says so on the
# page ("ask Teaport again"). The choice's POST is read up to MAX_FORM_BYTES and no
# further.
#
# One pairing at a time, in memory only (a brain restart forgets it, and its codes are
# then "expired"): a _Session, from the first start() to its Result, for at most
# SESSION_MAX_SECS while nothing is in progress (a start() after that with no tap
# minting and no watch running, or after the pairing was left with nothing to wait for
# and nobody waiting, ends it and opens a fresh one: no snapshot is lent to a later ask;
# one still at work gets a new code, and its watch is what is bounded). It owns
#
#   - the snapshot: `openclaw devices list --json`, read once, at that first start()
#     ({deviceId: approvedAtMs}; a list without its "paired" key is a failed read,
#     never an empty box);
#   - the current code. Asking again (start() while the pairing goes on) gives a new
#     code in the same pairing; the one before can no longer be used ("already used"
#     if it was tapped, else "expired"). A code lasts OFFER_SECS (OpenClaw's own
#     handoffs, minted later, last ten minutes, so ours is the bound). It is six
#     characters from an alphabet a reader cannot confuse aloud or on a small screen
#     (no 0/O, 1/I/L); while it can be used it is as good as an admin password, so it
#     is not logged, and MAX_WRONG wrong codes end it, that code alone (guessing gets a
#     few tries out of 31^6; any of the last few dozen codes issued, of this pairing
#     or an earlier one, is never a guess);
#   - every tap: a choice pressed on a code, minting its handoff, then made and watched
#     for its window (OPENED_WAIT_SECS for the dashboard, APP_WAIT_SECS for the app:
#     pasting a code into another app takes longer than a redirect), or failed and
#     dropped. A tap still minting when the person asks again is not thrown away: the
#     phone gets its handoff, and the same pairing watches for its device;
#   - its end, which wait() runs (the one waiter): a new device in the list, "paired";
#     cancel() or close(), "cancelled". Every end stops the taps still minting (their
#     CLI killed, their phone given "expired", no handoff), "paired" too; or nothing left to wait for -- the current code
#     dead or run out, no tap minting, no watch running -- then "opened" if any tap made
#     a handoff (the code was used, and what happened on that phone is not ours to see:
#     an untrusted certificate, a setup code never pasted, a closed tab), else "locked"
#     after wrong codes, else "expired". A failed mint makes it "failed" only when it
#     leaves nothing to wait for and no handoff was made; otherwise the pairing goes on.
#     withdraw() (the user's stop: the code off the screen) stops every tap still
#     minting, always, then keeps a pairing whose made handoff is watched, and ends one
#     without.
#
# One OpenClaw CLI at a time per pairing (_Session.cli): a poll and a mint never run side
# by side -- a tap cuts a running poll short (its CLI killed), and its mint starts once
# the poll has let go. The list is polled every POLL_SECS (the next poll started
# POLL_SECS after the last ended) while a made handoff's watch runs, and not while
# anything mints: at most 24 runs for a dashboard tap, 60 for an app tap. What counts is a device new
# since the snapshot, or approved again since (the browser handoff can also repair a
# browser paired before). The app pairs as a device of its own (deviceFamily
# "iPhone"), its node and operator roles one entry with a token per role; were they
# ever two entries, the newest approval names it (device_name()). spent() tells the
# screen's owner when its code can no longer be used while the pairing still waits on
# something, so the QR code can make way for "finish on your device".
#
# Lean on purpose (box RAM is tight): no thread, no task of its own, nothing polled
# until a handoff is made; the polling runs inside the caller's wait(), each poll is a
# cold OpenClaw CLI that exits, and nothing minted is kept (the handoff goes straight
# into the response). What stays after a pairing is its Result and the last few tapped
# codes, six characters each, so a second scan of a used QR says "already used".
#
# The pages are English. The phone page of Wi-Fi setup follows Accept-Language through
# i18n.py, but every string there is translated in every catalog (the i18n tests hold
# it to that); these pages are not worth that until the voice side of pairing is
# translated too.
#
# No Pipecat import: the router rides gateway_server.py's app, and nothing here needs
# the pipeline.
#
import asyncio
import collections
import hmac
import html
import ipaddress
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from loguru import logger

from teaport_brain import agent_backend, openclaw_client
from teaport_brain.settings import setting
from teaport_brain.wifi import PAGE_CSS, logo_html

# Unset: no front door is known (a voice-only install, or one from before #243), and
# pairing is not offered at all (available()).
FRONTDOOR_HOST = (setting("TEAPORT_FRONTDOOR_HOST") or "").strip()

# The avatar's QR limit (display.Screen.fits_qr reads the live one; this is what every
# avatar that draws QR codes says, and the link is sized for it up front).
QR_MAX_BYTES = 53
CODE_LEN = 6
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, no 1/I/L
OPENCLAW_HANDOFF_SECS = 600.0  # `dashboard` docs: "a single-use, ten-minute bootstrap"
REDEEM_MARGIN_SECS = 20.0      # the browser still has to load the UI and redeem it
OFFER_SECS = 300.0             # ours: the screen holds the code no longer than this
MAX_WRONG = 5
POLL_SECS = 5.0                # each poll is a cold OpenClaw CLI
OPENED_WAIT_SECS = 120.0       # after the dashboard choice: the browser pairs in seconds
# After the app choice: copy the code, switch to the app, open Add Gateway, paste. Five
# minutes covers a slow hand and is the "next few minutes" the page promises; the setup
# code itself lasts ten, which would double the polls for the rare late paste (that one
# still pairs, and is told as "opened").
APP_WAIT_SECS = 300.0
DASHBOARD_TIMEOUT_SECS = 30.0  # a cold CLI through the NemoClaw shim takes ~14 s
DEVICES_TIMEOUT_SECS = 20.0
QR_TIMEOUT_SECS = 30.0
# The longest a pairing lasts, asked again or not: its device snapshot is no older than
# this when a device is judged new against it. Past it, the next start() opens a fresh
# pairing (a fresh snapshot) instead of giving this one a new code.
SESSION_MAX_SECS = 900.0

PAIRED, OPENED, EXPIRED = "paired", "opened", "expired"
CANCELLED = "cancelled"    # cancel(), or withdraw() with nothing to wait for
LOCKED = "locked"          # MAX_WRONG wrong codes, and nothing else to wait for
FAILED = "failed"          # a tap's handoff could not be made, and nothing else to wait for
DASHBOARD, APP = "dashboard", "app"  # what the person chose on the /p/ page

_clock = time.monotonic  # replaced in tests


class PairingUnavailable(Exception):
    """No offer could be made. `reason` is short English for the journal: it never
    carries the CLI's output, which can hold the link."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Offer:
    code: str          # CODE_LEN characters of CODE_ALPHABET
    link: str          # https://<front door>/p/<code>, at most QR_MAX_BYTES of UTF-8
    expires_at: float  # time.monotonic() deadline
    session: int = 0   # the pairing session it is a code of (_Session.id)


@dataclass(frozen=True)
class Result:
    outcome: str        # PAIRED | OPENED | EXPIRED | CANCELLED | LOCKED | FAILED
    device: str | None = None  # the new device's display name when PAIRED


@dataclass(eq=False)
class _Tap:
    """A choice pressed on a code: minting its handoff, then made (watched until
    `until`), or failed (dropped from the session)."""
    choice: str
    until: float               # minting: as long as the mint can take plus the window
    minting: bool = True
    mint: asyncio.Future | None = None  # the mint's task, cancelled to stop it
    stopped: bool = False      # stopped by the pairing (cancel, withdraw, its end)


@dataclass(eq=False)
class _Session:
    """One pairing, from the first start() to its Result. See the module header."""
    id: int
    known: dict[str, int]      # deviceId -> approvedAtMs at the first start()
    opened_at: float           # _clock() at the first start(): SESSION_MAX_SECS from it
    code: Offer                # the current code (a re-ask replaces it)
    code_dead: bool = False    # used, locked, or withdrawn (expiry is the clock's)
    locked: bool = False       # MAX_WRONG wrong codes ended the current code
    wrong: int = 0             # wrong codes against the current one
    taps: list[_Tap] = field(default_factory=list)
    made: bool = False         # any tap's handoff was made
    result: Result | None = None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)  # result is set
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # pulsed (_pulse)
    waiting: bool = False      # a wait() is following it
    # One OpenClaw CLI at a time per pairing: a poll and a mint never run side by side.
    cli: asyncio.Lock = field(default_factory=asyncio.Lock)

    def code_usable(self) -> bool:
        return not self.code_dead and _clock() < self.code.expires_at

    def minting(self) -> bool:
        return any(t.minting for t in self.taps)

    def watch_until(self) -> float | None:
        """The end of the watch for made handoffs (a minting tap's claim is not one:
        nothing is polled beside a mint)."""
        return max((t.until for t in self.taps if not t.minting), default=None)

    def live_claim(self) -> bool:
        """Something still to wait for besides the current code: a tap minting, or a
        made handoff's watch running."""
        until = self.watch_until()
        return self.minting() or (until is not None and _clock() < until)

    def over(self) -> Result | None:
        """The Result once nothing is left to wait for (the current code dead or run out,
        no tap minting, no watch running), else None: "opened" if any tap made a
        handoff, else "locked" if wrong codes ended the current code, else "expired"."""
        if self.code_usable() or self.live_claim():
            return None
        return Result(OPENED if self.made else LOCKED if self.locked else EXPIRED)


def _window(choice: str | None) -> float:
    return APP_WAIT_SECS if choice == APP else OPENED_WAIT_SECS


_session: _Session | None = None
_ended: tuple[int, Result] | None = None  # the last session's id and Result, for wait()
_sessions = 0                             # ids
_creating: asyncio.Future | None = None   # a first start() reading the device list
# Codes tapped lately, for "already used" (the current session's and the one before).
_spent: collections.deque = collections.deque(maxlen=8)
# Every code issued lately, by any pairing: scanning one again (a photo of an old QR) is
# never a wrong guess against the current code.
_recent: collections.deque = collections.deque(maxlen=32)


def _pulse(s: _Session) -> None:
    """Wake whoever waits on `s` changing (a code, a tap, its end)."""
    s.changed.set()
    s.changed = asyncio.Event()


def _stop_mints(s: _Session) -> None:
    """Every tap still minting stops: its CLI is killed (_run_openclaw, on cancel), and
    its phone gets "expired" with no handoff (redeem)."""
    for t in [t for t in s.taps if t.minting]:
        t.stopped = True
        s.taps.remove(t)
        if t.mint is not None:
            t.mint.cancel()


def _end(s: _Session, result: Result) -> None:
    global _session, _ended
    if s.result is None:
        s.result = result
        _stop_mints(s)  # after any end, "paired" too: no handoff from an ended pairing
        s.stopped.set()
        _pulse(s)
        _ended = (s.id, result)
        if _session is s:
            _session = None


# ------------------------------------------------------------------ the CLI

async def _cli(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """The openclaw CLI, resolved and run the way every other caller runs it
    (openclaw_client: OPENCLAW_BIN, the NemoClaw shim, killed on cancel). Replaced in
    tests."""
    return await openclaw_client._run_openclaw(args, timeout=timeout, capture=True)


async def _paired_devices() -> list[dict]:
    """`openclaw devices list --json`'s paired list. Raises PairingUnavailable."""
    try:
        rc, out, _ = await _cli(["devices", "list", "--json"], DEVICES_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise PairingUnavailable("openclaw devices list timed out") from None
    except OSError as e:
        raise PairingUnavailable(f"openclaw CLI did not start ({e.strerror or e})") from None
    # "paired" must be there: an object without it (a banner, an error) read as no
    # devices would make every device already paired look new at the next poll.
    obj = openclaw_client.cli_json(out, lambda d: isinstance(d.get("paired"), list))
    if rc != 0 or obj is None:
        raise PairingUnavailable(f"openclaw devices list failed (rc={rc})")
    return [d for d in obj["paired"] if isinstance(d, dict) and d.get("deviceId")]


def _approvals(devices: list[dict]) -> dict[str, int]:
    return {str(d["deviceId"]): int(d.get("approvedAtMs") or 0) for d in devices}


def device_name(device: dict) -> str:
    """What to call a paired device on the screen and to the user: its operator label or
    the name the client gave itself, else what kind of device it is ("iPhone", "Mac"),
    else "". Never clientId or deviceId: every Control UI browser is
    "openclaw-control-ui", which says nothing to a person (seen on the box, 10-31)."""
    for key in ("operatorLabel", "displayName"):
        value = str(device.get(key) or "").strip()
        if value:
            return value
    return _device_kind(device)


# A browser's navigator.platform (what the Control UI reports as "platform"), or a native
# app's deviceFamily, to the kind of device a person would name. First match wins.
_KINDS = (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android phone"),
          ("mac", "Mac"), ("win", "Windows PC"), ("cros", "Chromebook"),
          ("linux", "Linux computer"))


def _device_kind(device: dict) -> str:
    for key in ("deviceFamily", "platform"):
        value = str(device.get(key) or "").strip().lower()
        for needle, kind in _KINDS:
            if needle in value:
                return kind
    return ""


def _new_device(devices: list[dict], known: dict[str, int]) -> dict | None:
    """A device that is not in the snapshot, or was approved again since it was taken.
    The newest approval wins when there are several."""
    fresh = [d for d in devices
             if int(d.get("approvedAtMs") or 0) > known.get(str(d["deviceId"]), -1)]
    return max(fresh, key=lambda d: int(d.get("approvedAtMs") or 0), default=None)


async def _mint() -> str:
    """A fresh browserUrl. Raises PairingUnavailable; the reason is OpenClaw's own
    {"ok": false, "reason"} when it gave one."""
    try:
        rc, out, _ = await _cli(["dashboard", "--json"], DASHBOARD_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise PairingUnavailable("openclaw dashboard timed out") from None
    except OSError as e:
        raise PairingUnavailable(f"openclaw CLI did not start ({e.strerror or e})") from None
    obj = openclaw_client.cli_json(out, lambda d: "ok" in d) or {}
    url = obj.get("browserUrl")
    if rc != 0 or obj.get("ok") is not True or not isinstance(url, str) or "#" not in url:
        reason = obj.get("reason")
        raise PairingUnavailable(f"openclaw dashboard failed: {reason}" if isinstance(reason, str)
                                 else f"openclaw dashboard failed (rc={rc})")
    expires_ms = obj.get("browserBootstrapExpiresAtMs")
    if isinstance(expires_ms, (int, float)) and expires_ms / 1000.0 - time.time() <= REDEEM_MARGIN_SECS:
        raise PairingUnavailable("OpenClaw's handoff expired as it was minted")
    return url


async def _mint_setup_code() -> str:
    """A setup code for the OpenClaw app (`openclaw qr --json --limited`: the app's node
    and operator tokens, without operator.admin). Raises PairingUnavailable, also when
    the code would send the app to a loopback gateway, which a phone cannot reach (the
    device-pair plugin's publicUrl, wss://<front door>, is not set)."""
    try:
        rc, out, _ = await _cli(["qr", "--json", "--limited"], QR_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise PairingUnavailable("openclaw qr timed out") from None
    except OSError as e:
        raise PairingUnavailable(f"openclaw CLI did not start ({e.strerror or e})") from None
    obj = openclaw_client.cli_json(out, lambda d: "setupCode" in d)
    code = obj.get("setupCode") if obj else None
    if rc != 0 or not isinstance(code, str) or not code.strip():
        raise PairingUnavailable(f"openclaw qr failed (rc={rc})")
    try:
        gateway = urllib.parse.urlsplit(str(obj.get("gatewayUrl") or ""))
        host = gateway.hostname
    except ValueError:  # "wss://[fe80::1": not a URL a phone can use either
        raise PairingUnavailable("openclaw qr named a gateway URL that does not parse") from None
    if _is_loopback(host):
        raise PairingUnavailable("openclaw qr named a loopback gateway (device-pair "
                                 "publicUrl unset)")
    logger.info(f"pairing: app setup code made (access {obj.get('access') or '?'}, "
                f"{gateway.scheme or '?'}://{host or '?'})")
    return code.strip()


# ------------------------------------------------------------------ the link

def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def front_door_url(browser_url: str, host: str) -> str:
    """browserUrl as the front door serves it (OpenClaw's connect-and-pair docs): a
    loopback base becomes https://<host> (its path, the Control UI's base path, kept),
    and a loopback gatewayUrl fragment value wss://<host> (its path kept). Every other
    fragment pair is kept as OpenClaw wrote it. A gatewayUrl that is missing is added,
    so the browser is never left to guess which gateway the grant is for. A URL that is
    not loopback is OpenClaw's own choice of address and is left alone."""
    base, _, fragment = browser_url.partition("#")
    parts = urllib.parse.urlsplit(base)
    if not _is_loopback(parts.hostname):
        return browser_url
    base = urllib.parse.urlunsplit(("https", host, parts.path or "/", parts.query, ""))
    pairs, seen = [], False
    for pair in fragment.split("&") if fragment else []:
        key, eq, value = pair.partition("=")
        if urllib.parse.unquote_plus(key) == "gatewayUrl":
            seen = True
            ws = urllib.parse.urlsplit(urllib.parse.unquote_plus(value))
            if _is_loopback(ws.hostname):
                pair = "gatewayUrl=" + urllib.parse.quote(
                    urllib.parse.urlunsplit(("wss", host, ws.path, ws.query, "")), safe="")
        pairs.append(pair)
    if not seen:
        pairs.append("gatewayUrl=" + urllib.parse.quote(f"wss://{host}", safe=""))
    return base + "#" + "&".join(pairs)


def _link(host: str, code: str) -> str:
    return f"https://{host}/p/{code}"


def _check_host(host: str) -> str:
    host = (host or "").strip()
    if not host:
        raise PairingUnavailable("no front door host (TEAPORT_FRONTDOOR_HOST)")
    if re.search(r"[\s/?#@\\]", host):
        raise PairingUnavailable("TEAPORT_FRONTDOOR_HOST is not a host name")
    if len(_link(host, "X" * CODE_LEN).encode()) > QR_MAX_BYTES:
        raise PairingUnavailable(f"TEAPORT_FRONTDOOR_HOST is too long for the screen's "
                                 f"{QR_MAX_BYTES}-byte QR code")
    return host


# ------------------------------------------------------------------ the session

def available() -> bool:
    """Whether pairing can be offered at all: the front door's name is known."""
    return bool(FRONTDOOR_HOST)


def _new_code(s_id: int, host: str) -> Offer:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    _recent.append(code)
    expires_at = _clock() + min(OFFER_SECS, OPENCLAW_HANDOFF_SECS - REDEEM_MARGIN_SECS)
    return Offer(code=code, link=_link(host, code), expires_at=expires_at, session=s_id)


async def start() -> Offer:
    """A code to show. The first start() opens a pairing session (reading the device
    list: the snapshot); while one is going, start() gives it a new code instead, and
    the one before can no longer be used ("already used" if it was tapped, else
    "expired"). Raises PairingUnavailable."""
    global _session, _sessions, _creating
    if not agent_backend.HAS_AGENT:
        raise PairingUnavailable("no agent on this box (TEAPORT_AGENT=none)")
    host = _check_host(FRONTDOOR_HOST)
    while _creating is not None and not _creating.done():
        try:  # another start() is opening the session: a new code in it, after
            await asyncio.shield(_creating)
        except Exception:  # noqa: BLE001 -- its failure is its caller's; try again here
            pass
    s = _session
    if s is not None and s.result is None:
        # A pairing nobody follows any more (nothing left to wait for), or one past its
        # bound, ends here rather than lend a new code its old snapshot.
        # The bound ends only a pairing with nothing in progress (no tap minting, no
        # watch running); one still at work gets a new code as usual, its snapshot
        # kept -- the watch is what is bounded there (OPENED_WAIT_SECS, APP_WAIT_SECS).
        ending = s.over() or (Result(OPENED if s.made else EXPIRED)
                              if _clock() - s.opened_at >= SESSION_MAX_SECS
                              and not s.live_claim() else None)
        if ending is not None:
            logger.info(f"pairing: ended {ending.outcome} before a new one opened")
            _end(s, ending)
    s = _session
    if s is not None and s.result is None:
        s.code, s.code_dead, s.locked, s.wrong = _new_code(s.id, host), False, False, 0
        _pulse(s)
        logger.info(f"pairing: a new code in the same pairing ({len(s.taps)} tap(s) so far)")
        return s.code
    _creating = asyncio.get_running_loop().create_future()
    try:
        known = _approvals(await _paired_devices())
    except BaseException as e:
        _creating.set_exception(e if isinstance(e, Exception) else PairingUnavailable("stopped"))
        _creating.exception()  # retrieved: nobody else need see it
        raise
    _sessions += 1
    s = _Session(id=_sessions, known=known, opened_at=_clock(),
                 code=_new_code(_sessions, host))
    _session = s
    _creating.set_result(None)
    logger.info(f"pairing: a pairing opened, {s.code.expires_at - _clock():.0f} s to scan "
                f"({len(known)} device(s) paired before)")
    return s.code


def _of(offer: Offer) -> _Session | None:
    s = _session
    return s if s is not None and s.id == offer.session and s.result is None else None


def cancel(offer: Offer) -> None:
    """End `offer`'s pairing: "cancelled", whatever it was waiting for (a tap still
    minting is stopped: its phone gets "expired", not its handoff)."""
    s = _of(offer)
    if s is not None:
        _end(s, Result(CANCELLED))


def withdraw(offer: Offer) -> bool:
    """The user stopped: `offer`, if it is still the current code, goes out of use, and
    every tap still minting stops (its phone gets "expired", no handoff). A made
    handoff's device is still looked for, its end told by wait() (True); with none
    watched, the pairing ends "cancelled", as cancel() does (False). An older code of
    the pairing touches nothing: the one up now is not the one taken down."""
    s = _of(offer)
    if s is None:
        return False
    if s.code is not offer:
        return s.made and s.live_claim()
    s.code_dead = True
    _stop_mints(s)
    if s.made and s.live_claim():
        _pulse(s)
        return True
    _end(s, Result(CANCELLED))
    return False


async def _or_stopped(s: _Session, aw, timeout: float | None, wake=None):
    """(done, value): `aw`'s value, unless the session ends, `wake` (an Event) is set or
    `timeout` passes first, when `aw` is cancelled (a CLI child is killed) and done is
    False."""
    task = asyncio.ensure_future(aw)
    stop = asyncio.ensure_future(s.stopped.wait() if wake is None
                                 else _first_of(s.stopped, wake))
    try:
        await asyncio.wait({task, stop},
                           timeout=None if timeout is None else max(0.0, timeout),
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (task, stop):
            if not t.done():
                t.cancel()
        await asyncio.gather(task, stop, return_exceptions=True)
    if task.cancelled() or not task.done():
        return False, None
    return True, task.result()


async def _first_of(*events: asyncio.Event) -> None:
    waits = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


async def _one_cli(s: _Session, aw):
    """`aw` (a CLI run) once no other of the pairing's is running."""
    async with s.cli:
        return await aw


async def wait(offer: Offer) -> Result:
    """Follow `offer`'s pairing to its end, whichever of its codes `offer` is. One waiter
    per pairing: it runs the watch. A pairing already over gives its Result."""
    s = _of(offer)
    if s is None:
        if _ended is not None and _ended[0] == offer.session:
            return _ended[1]
        return Result(CANCELLED)
    if s.waiting:  # a second waiter only hears the end
        await s.stopped.wait()
        return s.result
    s.waiting = True
    try:
        while s.result is None:
            changed, now = s.changed, _clock()
            until = s.watch_until()
            if s.minting():
                # A handoff being minted (bounded by its CLI's timeout, which then drops
                # the tap): no poll beside that CLI.
                await _or_stopped(s, changed.wait(), POLL_SECS)
            elif until is not None and now < until:
                # A tap starting meanwhile (changed) cuts the poll short, killing its
                # CLI, so the tap's mint does not wait out a slow list -- and the mint
                # takes the CLI lock only once this one has let it go.
                done, devices = await _or_stopped(s, _one_cli(s, _poll()), until - now,
                                                  wake=changed)
                found = _new_device(devices, s.known) if done and devices is not None else None
                if found is not None and s.result is None:
                    name = device_name(found)
                    logger.info(f"pairing: paired {name!r}")
                    _end(s, Result(PAIRED, name))
                    break
                if done:
                    await _or_stopped(s, s.changed.wait(), min(POLL_SECS, until - _clock()))
            elif s.code_usable():
                await _or_stopped(s, changed.wait(), s.code.expires_at - now)
            else:  # nothing left to wait for
                ending = s.over() or Result(EXPIRED)
                logger.info(f"pairing: ended {ending.outcome} ({len(s.taps)} tap(s))")
                _end(s, ending)
        return s.result
    finally:
        if s.result is None:  # the waiter itself was cancelled: nothing runs the watch
            _end(s, Result(CANCELLED))


async def spent(offer: Offer) -> bool:
    """Wait until `offer`'s code can no longer be used while its pairing still has
    something to wait for (a tap minting, a made handoff watched): True, and the QR
    code on the screen is of no more use -- the person finishes on their device. False
    when the pairing ends first, or a newer code replaced this one (the caller shows
    that one). For the screen; how the pairing ends is wait()'s."""
    s = _of(offer)
    while s is not None and s.result is None and s.code is offer:
        changed = s.changed
        if not s.code_usable():
            if s.live_claim():
                return True
            await _or_stopped(s, changed.wait(), POLL_SECS)  # wait() is ending it
        else:
            await _or_stopped(s, changed.wait(), s.code.expires_at - _clock())
    return False


async def _poll() -> list[dict] | None:
    try:
        return await _paired_devices()
    except PairingUnavailable as e:
        logger.debug(f"pairing: poll skipped: {e.reason}")
        return None


# ------------------------------------------------------------------ /p/<code>

_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title}</title><style>{css}</style></head>
<body><main><div class="logo">{logo}</div>
<h1>{heading}</h1>
{body}</main>{script}</body></html>"""

# A second choice under the first, and the setup code's box: on top of the Wi-Fi setup
# page's look (wifi.PAGE_CSS).
_EXTRA_CSS = """
form{margin:0}button.alt{background:var(--surface);color:var(--text);border:1px solid var(--border-strong)}
button[disabled]{opacity:.6}ol{color:var(--muted);padding-left:1.4em;margin:0 0 16px}
textarea{width:100%;box-sizing:border-box;min-height:7.5em;padding:10px 12px;font:13px/1.4 ui-monospace,monospace;
border:1px solid var(--border-strong);border-radius:var(--radius);background:var(--surface);color:inherit;
word-break:break-all;resize:none}.small{font-size:14px}
details{border:1px solid var(--border-strong);border-radius:var(--radius);padding:8px 12px;margin:0 0 8px}
summary{cursor:pointer;font-weight:600}details ol{margin:8px 0 4px}details p{margin:4px 0}"""

# The choice page's buttons go grey on the first tap: a second tap would post again,
# find the code spent, and replace the first answer (the handoff) with "already used".
_ONE_TAP = """<script>document.querySelectorAll("form").forEach(function(f){f.addEventListener("submit",
function(){f.parentNode.querySelectorAll("button").forEach(function(b){b.disabled=true});
var w=document.getElementById("wait");if(w)w.hidden=false})})</script>"""

# Copy with the clipboard API; where it is missing or refused, select the text so the
# phone's own Copy is one tap away (and execCommand, for older browsers).
_COPY = """<script>(function(){var t=document.getElementById("code"),b=document.getElementById("copy");
function sel(){t.focus();t.select();t.setSelectionRange(0,t.value.length)}
function done(){b.textContent="Copied"}
b.addEventListener("click",function(){sel();
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t.value).then(done,
function(){try{document.execCommand("copy")&&done()}catch(e){}})}
else{try{document.execCommand("copy")&&done()}catch(e){}}})})()</script>"""


def _ca_link() -> str:
    url = html.escape(f"http://{FRONTDOOR_HOST}/teaport-ca.crt", quote=True)
    return f'<a href="{url}">Teaport\'s certificate</a>'


# How to trust the box's certificate, per kind of device: one <details> each, closed until
# tapped (no script needed). The certificate's name is Caddy's ("Caddy Local Authority"),
# so that is what the steps tell people to look for. Steps follow each system's own menu
# names as of 2026; Android's differ between makers, so its steps name the search term.
_CA_NAME = "Caddy Local Authority"


def _ca_help() -> str:
    link = _ca_link()
    name = f"<strong>{_CA_NAME}</strong>"

    def block(title: str, steps: list[str], note: str = "") -> str:
        items = "".join(f"<li>{x}</li>" for x in steps)
        return (f"<details><summary>{html.escape(title)}</summary><ol>{items}</ol>"
                + (f'<p class="small">{note}</p>' if note else "") + "</details>")

    return "".join((
        _p("How to trust the box on:"),
        block("iPhone or iPad", [
            f"Open {link} in <strong>Safari</strong> and tap <strong>Allow</strong>.",
            "Open <strong>Settings</strong>, tap <strong>Profile Downloaded</strong> (or "
            "<strong>General</strong>, then <strong>VPN &amp; Device Management</strong>), "
            "and install the profile.",
            "Go to <strong>Settings</strong>, <strong>General</strong>, <strong>About</strong>, "
            f"<strong>Certificate Trust Settings</strong>, and turn on {name}. This is a "
            "separate step: without it the phone still does not trust the box."]),
        block("Android", [
            f"Download {link}.",
            "Open <strong>Settings</strong> and search for <strong>CA certificate</strong> "
            "(often under <strong>Security</strong>, then <strong>Encryption &amp; "
            "credentials</strong>, then <strong>Install a certificate</strong>).",
            "Choose <strong>CA certificate</strong>, confirm, and pick the downloaded "
            "teaport-ca.crt."],
            "Chrome and other browsers use it. Some apps ignore certificates you add."),
        block("Windows", [
            f"Download {link} and open it.",
            "Choose <strong>Install Certificate</strong>, then <strong>Current User</strong>.",
            "Choose <strong>Place all certificates in the following store</strong>, "
            "<strong>Browse</strong>, and <strong>Trusted Root Certification "
            "Authorities</strong>. Finish, and answer <strong>Yes</strong> to the warning.",
            "Close and reopen your browser."],
            "Firefox keeps its own list: <strong>Settings</strong>, <strong>Privacy &amp; "
            "Security</strong>, <strong>View Certificates</strong>, <strong>Authorities"
            "</strong>, <strong>Import</strong>, and tick <strong>Trust this CA to identify "
            "websites</strong>."),
        block("Mac", [
            f"Download {link} and open it. Keychain Access adds it to your login keychain.",
            f"In Keychain Access, double-click {name}, open <strong>Trust</strong>, and set "
            "<strong>When using this certificate</strong> to <strong>Always Trust</strong>.",
            "Close the window, enter your password, and reopen your browser."],
            "Firefox keeps its own list: see the Firefox step under Windows."),
    ))


def _p(*parts: str) -> str:
    """A paragraph of plain text (escaped) with HTML pieces: _p("text ", _ca_link())."""
    return "<p>" + "".join(x if x.startswith("<") else html.escape(x) for x in parts) + "</p>"


def _page(heading: str, body: str, status: int = 200, script: str = "") -> HTMLResponse:
    """`body` is HTML, built from _p() and fixed markup."""
    return HTMLResponse(_PAGE.format(title=html.escape(heading), css=PAGE_CSS + _EXTRA_CSS,
                                     logo=logo_html(), heading=html.escape(heading),
                                     body=body, script=script),
                        status_code=status, headers=_NO_STORE)


def _choose(code: str) -> HTMLResponse:
    """The landing for a live code: nothing is spent until a button is pressed (a link
    preview or a prefetch is a GET)."""
    action = html.escape(f"/p/{code}", quote=True)

    def button(choice: str, label: str, cls: str = "") -> str:
        return (f'<form method="post" action="{action}"><input type="hidden" name="choice" '
                f'value="{choice}"><button type="submit"{cls}>{html.escape(label)}</button></form>')

    return _page("Connect this device to Teaport", "".join((
        _p("Choose how you want to use Teaport on this device. The code works once."),
        button(DASHBOARD, "Open the dashboard here"),
        button(APP, "Use the OpenClaw app", ' class="alt"'),
        '<p id="wait" class="small" hidden>Starting, this can take a few seconds…</p>',
        _p("If your phone warns that the connection is not private, install ", _ca_link(),
           " first, then scan a new code."), _ca_help())), script=_ONE_TAP)


def _app_code(setup_code: str) -> HTMLResponse:
    """The setup code, shown once (no-store): the app's Add Gateway takes it pasted."""
    return _page("Add Teaport to the OpenClaw app", "".join((
        _p("Copy this code and paste it into the app. It works once, so paste it in the "
           "next few minutes."),
        f'<textarea id="code" readonly autocomplete="off" autocapitalize="none" '
        f'spellcheck="false" aria-label="Setup code">{html.escape(setup_code)}</textarea>',
        '<button type="button" id="copy">Copy code</button>',
        "<ol><li>Open the OpenClaw app.</li>"
        "<li>Open <strong>Add Gateway</strong> from the app's menu (or "
        "<strong>Settings</strong>, then <strong>Gateway</strong>, if it already has one).</li>"
        "<li>Paste the code and connect.</li></ol>",
        _p("The app connects only if this phone trusts the box: install ", _ca_link(),
           " and turn on full trust for it in the phone's settings first. Then ask Teaport "
           "for a new code if this one has run out."), _ca_help())), script=_COPY)


def _failed() -> HTMLResponse:
    return _page("Teaport could not start the connection",
                 _p("Something went wrong on the box. Ask Teaport again for a new code."), 503)




def _used() -> HTMLResponse:
    # The page a phone sees when it scans again -- most often because the dashboard did
    # not open the first time, and the usual reason is a phone that does not trust the
    # box yet.
    return _page("This code was already used", _p(
        "Each code connects one device. If the dashboard did not open on this phone, it "
        "needs to trust the box first: install ", _ca_link(),
        ", then ask Teaport for a new code.") + _ca_help(), 410)


def _expired() -> HTMLResponse:
    return _page("This code has expired", _p(
        "Ask Teaport for a new one, and scan it within a few minutes. If your phone warns "
        "that the connection is not private, install ", _ca_link(), " first.") + _ca_help(), 410)


def _via_front_door(request: Request) -> bool:
    """Whether the request came through Caddy over HTTPS. The brain listens on every
    interface (http://<box>:7861 is the config page), so without this anyone on the
    LAN could reach /p/ in cleartext, or burn a live offer with wrong guesses. uvicorn
    trusts X-Forwarded-* only from 127.0.0.1 (its forwarded_allow_ips default) and then
    sets the scheme from X-Forwarded-Proto, so "https" here means Caddy said so. Run
    without that middleware, the same rule is checked by hand: a loopback peer that
    says https."""
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    return (_is_loopback(peer)
            and request.headers.get("x-forwarded-proto", "").strip().lower() == "https")


def _find(code: str) -> tuple[str, _Session | None]:
    """("used" | "expired" | "live", the session when "live") for a code at /p/. A code
    that is none of the pairing's (nor one tapped lately) is a wrong guess while its
    current code can be used, and MAX_WRONG of them end that code. Its own codes, asked
    again, are never guesses."""
    code = code.strip().upper()
    if any(hmac.compare_digest(code.encode(), c.encode()) for c in _spent):
        return "used", None  # a second scan of a used code is not a guess
    s = _session
    if s is None or s.result is not None:
        return "expired", None
    usable = s.code_usable()
    if hmac.compare_digest(code.encode(), s.code.code.encode()):
        return ("live", s) if usable else ("expired", None)
    if any(hmac.compare_digest(code.encode(), c.encode()) for c in _recent):
        return "expired", None  # an earlier code, of this pairing or one before
    if usable:
        s.wrong += 1
        if s.wrong >= MAX_WRONG:
            logger.warning(f"pairing: {s.wrong} wrong codes; the code is locked")
            s.code_dead = s.locked = True
            _pulse(s)
    return "expired", None


_NOT_FOUND = "Not Found"
# The choice form is one short field. A body past this is not read on: no FastAPI or
# uvicorn setting in the brain bounds a request body (the config page's JSON routes sit
# behind the token), so this one, open to the network, reads at most this much.
MAX_FORM_BYTES = 1024


async def _small_form(request: Request) -> dict[str, list[str]] | None:
    """The urlencoded form, or None when the body is (or says it is) over
    MAX_FORM_BYTES."""
    declared = (request.headers.get("content-length") or "").strip()
    if declared and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    return urllib.parse.parse_qs(body.decode("utf-8", "replace"))
router = APIRouter()


@router.get("/p/{code}")
async def landing(code: str, request: Request) -> Response:
    """Unauthenticated: the browser asking has no token yet; the code is the grant.
    A GET spends nothing: it shows the two choices."""
    if not _via_front_door(request):
        return PlainTextResponse(_NOT_FOUND, status_code=404)  # not a guess either
    state, s = _find(code)
    if state == "live":
        return _choose(s.code.code)
    return _used() if state == "used" else _expired()


@router.post("/p/{code}")
async def redeem(code: str, request: Request) -> Response:
    """The choice: the code is spent here, before the handoff is minted (a second post
    while the first mints finds it used), and the handoff is made for what was chosen."""
    if not _via_front_door(request):
        return PlainTextResponse(_NOT_FOUND, status_code=404)
    form = await _small_form(request)
    if form is None:  # before the code is looked at: spends nothing, guesses nothing
        return PlainTextResponse("Payload Too Large", status_code=413)
    choice = (form.get("choice") or [""])[0]
    state, s = _find(code)
    if state != "live":
        return _used() if state == "used" else _expired()
    if choice not in (DASHBOARD, APP):
        return _choose(s.code.code)  # nothing chosen: nothing spent
    s.code_dead = True
    _spent.append(s.code.code)
    tap = _Tap(choice, _clock() + max(DASHBOARD_TIMEOUT_SECS, QR_TIMEOUT_SECS) + _window(choice))
    s.taps.append(tap)
    _pulse(s)
    logger.info(f"pairing: a code was used ({choice}); making the handoff")
    # The code is spent from here, so every way out either makes the handoff or drops
    # the tap: a tap left minting would hold the pairing (and its screen) for ever. The
    # mint runs as a task of its own, after any poll has let the CLI go (_one_cli), so
    # the pairing can stop it (cancel(), withdraw(), its end), killing its CLI.
    made = False
    tap.mint = asyncio.ensure_future(
        _one_cli(s, _mint() if choice == DASHBOARD else _mint_setup_code()))
    try:
        try:
            handoff = await tap.mint
            answer = (RedirectResponse(front_door_url(handoff, FRONTDOOR_HOST), status_code=303,
                                       headers=_NO_STORE)
                      if choice == DASHBOARD else _app_code(handoff))
        except asyncio.CancelledError:
            if tap.stopped:  # the pairing stopped it: no handoff after the user's stop
                return _expired()
            raise  # the request itself (the phone went away)
        except PairingUnavailable as e:
            logger.warning(f"pairing: {choice} handoff failed: {e.reason}")
            return _failed()
        except Exception as e:  # noqa: BLE001 — by its type: the message can hold the handoff
            logger.warning(f"pairing: {choice} handoff failed: {type(e).__name__}")
            return _failed()
        if tap.stopped or s.result is not None:
            return _expired()  # stopped or over while it minted: no handoff after that
        made = True
        tap.minting, tap.until, s.made = False, _clock() + _window(choice), True
        _pulse(s)  # wait() starts (or goes on) looking for the new device
        return answer
    finally:
        if not made:
            # No handoff: the tap is dropped (its CLI with it). The pairing fails with it
            # only when that leaves nothing to wait for and no handoff was made;
            # otherwise it goes on, and ends as its other parts decide.
            if not tap.mint.done():
                tap.mint.cancel()
            if tap in s.taps:
                s.taps.remove(tap)
            if s.result is None and not s.made and not s.live_claim() and not s.code_usable():
                _end(s, Result(FAILED))
            else:
                _pulse(s)
