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
# page ("ask Teaport again") and ends the offer ("failed"), so the screen comes down.
# The choice's POST is read up to MAX_FORM_BYTES and no further.
#
# One offer at a time, in memory only (a brain restart forgets it, and its code is then
# "expired"): start() makes it, wait() follows it, cancel() ends it ("cancelled"),
# withdraw() ends it too unless its code was spent (then the device is still looked
# for, and the confirmation is worth keeping), and a second start() supersedes the
# first ("superseded"). A spent first one is superseded too -- the person may be asking
# again after trusting the certificate, or to switch from the app to the dashboard --
# and the new offer takes over its snapshot and its watch: it polls from its start to
# the old watch's end, used or not, so a device the old code still brings in (the first
# setup code pasted after all) ends the new offer "paired", by name. A tap whose handoff
# is still being minted then is not thrown away: the phone gets it, and its device is
# caught the same way; if that mint fails, its share of the watch is withdrawn (_Watch). Any way out of a spent code's mint that is not
# the handoff ends the offer ("failed"): a spent code is never left waiting. The
# code works for OFFER_SECS from start() (OpenClaw's own handoffs, minted later, last
# ten minutes, so ours is the bound). It is six characters from an alphabet a reader
# cannot confuse aloud or on a small screen (no 0/O, 1/I/L); while it is live it is as
# good as an admin password, so it is not logged, and a handful of wrong codes while an
# offer waits (MAX_WRONG) end it ("locked"): guessing gets a few tries out of 31^6.
#
# Whether the new device actually paired is OpenClaw's to say: `openclaw devices list
# --json` is snapshotted at start() ({deviceId: approvedAtMs}; a list without its
# "paired" key is a failed read, never an empty box), and once a handoff has been made
# (or a watch was taken over, above), and only then, it is polled every POLL_SECS (one CLI at a time, the next started
# POLL_SECS after the last ended) for OPENED_WAIT_SECS after the dashboard choice, at
# most 24 runs, or APP_WAIT_SECS after the app's, at most 60 (pasting a code into
# another app takes longer than a browser redirect), for a device that is new, or
# approved again since (the browser handoff can also repair a browser that was paired
# before). The app pairs as a device of its own (deviceFamily "iPhone"), its
# node and operator roles one entry with a token per role; were they ever two entries,
# the newest approval names it. Found: "paired", with device_name(). Nothing within
# that window: "opened" -- the code was used, and what happened on that phone is
# not ours to see (an untrusted certificate, a setup code never pasted, a closed tab).
# spent() tells the screen's owner when the code has been used, so the QR code, of no
# more use, can make way for "finish on your phone" while the device is looked for.
#
# Lean on purpose (box RAM is tight): no thread, no task of its own, nothing polled
# until a handoff is made (or a watch taken over); the polling runs inside the caller's
# wait(), each poll is a cold OpenClaw CLI that exits, and nothing minted is kept (the
# handoff goes straight into the response). What stays is the last spent code, six
# characters, so a second scan of a used QR says "already used" rather than "expired".
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

PAIRED, OPENED, EXPIRED = "paired", "opened", "expired"
CANCELLED = "cancelled"    # cancel() was called
SUPERSEDED = "superseded"  # a newer start() replaced the offer before its link was used
LOCKED = "locked"          # MAX_WRONG wrong codes while it waited
FAILED = "failed"          # the code was used, but OpenClaw could not make the handoff
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


@dataclass(frozen=True)
class Result:
    outcome: str        # PAIRED | OPENED | EXPIRED | CANCELLED | SUPERSEDED | LOCKED | FAILED
    device: str | None = None  # the new device's display name when PAIRED


@dataclass(eq=False)
class _Live:
    offer: Offer
    known: dict[str, int]       # deviceId -> approvedAtMs at mint
    opened: bool = False        # the code was spent on a choice (DASHBOARD or APP)
    choice: str | None = None   # which one
    ended: str | None = None    # CANCELLED | SUPERSEDED | LOCKED | FAILED once ended early
    wrong: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)    # the handoff was made
    stopped: asyncio.Event = field(default_factory=asyncio.Event)  # ended early
    # Until when the device list is polled (see _Watch); shared with the offer that
    # replaces this one while it is still running (start()).
    watch: "_Watch" = field(default_factory=lambda: _Watch())


@dataclass(eq=False)
class _Watch:
    """How long the device list is polled: the latest of the claims on it, one per tap
    (keyed by its offer). A tap claims, while its handoff is minted, as long as the mint
    can take plus its choice's window; made, the window from then; failed, nothing (the
    claim is withdrawn). An offer that replaces a watching one shares its _Watch, so a
    tap of the old code still in flight extends -- or, failing, no longer extends -- the
    new offer's watch, with no pointer between the two offers."""
    claims: dict = field(default_factory=dict)

    def until(self) -> float | None:
        return max(self.claims.values(), default=None)

    def running(self) -> bool:
        until = self.until()
        return until is not None and _clock() < until


def _window(choice: str | None) -> float:
    return APP_WAIT_SECS if choice == APP else OPENED_WAIT_SECS


_live: _Live | None = None
_spent: str | None = None  # the last redeemed code, for "already used"
_minting = 0               # generation: a newer start() supersedes one still minting


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


# ------------------------------------------------------------------ the offer

def available() -> bool:
    """Whether pairing can be offered at all: the front door's name is known."""
    return bool(FRONTDOOR_HOST)


async def start() -> Offer:
    """Make a new offer, superseding the live one ("superseded"). Raises
    PairingUnavailable. When the live one's code was spent and its device is still
    being looked for, the new offer takes over its snapshot instead of reading a fresh
    one, and its watch: polling starts at once and runs to the old watch's end, whether
    or not the new code is ever used. A device that the old code still brings in (the
    person trusted the certificate, pasted the first setup code after all, asked again
    to switch to the dashboard) is then new to this offer, and ends it "paired". An old
    code whose handoff is still being minted is not thrown away either: its phone gets
    the handoff (redeem), and the device it pairs is caught here."""
    global _live, _minting
    inherited, watch = None, _Watch()
    if _live is not None and _live.ended is None:
        if _live.watch.running():
            inherited, watch = dict(_live.known), _live.watch
        _end(_live, SUPERSEDED)
    _minting += 1
    mine = _minting
    if not agent_backend.HAS_AGENT:
        raise PairingUnavailable("no agent on this box (TEAPORT_AGENT=none)")
    host = _check_host(FRONTDOOR_HOST)
    t0 = _clock()
    # The snapshot now, before any handoff exists: whatever pairs later is new. The
    # handoff itself is minted when the person chooses on the /p/ page (redeem).
    known = inherited if inherited is not None else _approvals(await _paired_devices())
    now = _clock()
    expires_at = t0 + min(OFFER_SECS, OPENCLAW_HANDOFF_SECS - REDEEM_MARGIN_SECS)
    if mine != _minting:  # a newer start() came in while this one minted
        raise PairingUnavailable("superseded by a newer pairing request")
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    offer = Offer(code=code, link=_link(host, code), expires_at=expires_at)
    _live = _Live(offer=offer, known=known, watch=watch)
    logger.info(f"pairing: offer made, {expires_at - now:.0f} s to scan "
                f"({len(known)} device(s) paired before)")
    return offer


def cancel(offer: Offer) -> None:
    """End `offer` if it is still the live one; its wait() says "cancelled"."""
    live = _live
    if live is not None and live.offer is offer:
        _end(live, CANCELLED)


def withdraw(offer: Offer) -> bool:
    """Take `offer`'s code out of use, as when it comes off the screen. A link not
    opened yet is cancelled, as cancel() does (False); one already opened is spent, and
    is left to confirm its device: its wait() still says how (True)."""
    live = _live
    if live is None or live.offer is not offer or live.ended is not None:
        return False
    if live.opened:
        return True
    _end(live, CANCELLED)
    return False


def _end(live: _Live, why: str) -> None:
    if live.ended is None:
        live.ended = why
        live.stopped.set()


async def _or_cancelled(live: _Live, aw, timeout: float | None):
    """(done, value): `aw`'s value, unless the offer is cancelled or `timeout` passes
    first, when `aw` is cancelled (a CLI child is killed) and done is False."""
    task = asyncio.ensure_future(aw)
    stop = asyncio.ensure_future(live.stopped.wait())
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


async def wait(offer: Offer) -> Result:
    """Follow `offer` to its end. An offer that is no longer the live one (its wait()
    already returned, or a newer start() replaced it) is "superseded". One waiter per
    offer."""
    global _live
    live = _live
    if live is None or live.offer is not offer:
        return Result(SUPERSEDED)
    try:
        while live.ended is None:
            now = _clock()
            if live.opened and not live.ready.is_set():
                # Its own handoff is being minted (bounded by the CLI's timeout, which
                # then ends the offer); no poll beside that CLI.
                await _or_cancelled(live, live.ready.wait(), POLL_SECS)
            elif live.watch.running():
                done, devices = await _or_cancelled(live, _poll(), live.watch.until() - now)
                if live.ended:
                    break
                found = _new_device(devices, live.known) if done and devices is not None else None
                if found is not None:
                    name = device_name(found)
                    logger.info(f"pairing: paired {name!r}")
                    return Result(PAIRED, name)
                await _or_cancelled(live, asyncio.sleep(POLL_SECS),
                                    min(POLL_SECS, (live.watch.until() or 0.0) - _clock()))
            elif live.ready.is_set():
                logger.info(f"pairing: code used ({live.choice}), no new device in time")
                return Result(OPENED, None)
            elif now >= offer.expires_at:
                logger.info("pairing: the code expired unused")
                return Result(EXPIRED, None)
            else:  # unused, and nothing to watch for: until it is used or expires
                await _or_cancelled(live, live.ready.wait(), offer.expires_at - now)
        return Result(live.ended)
    finally:
        live.stopped.set()  # spent() waits on it too
        if _live is live:
            _live = None


async def spent(offer: Offer) -> bool:
    """Wait until `offer`'s code is spent and its handoff made (True: the person is
    finishing on their phone, and the code on the screen is of no more use), or the
    offer is over first (False). For the screen; how the offer ends is wait()'s."""
    live = _live
    if live is None or live.offer is not offer:
        return False
    if not live.ready.is_set() and not live.stopped.is_set():
        await _or_cancelled(live, live.ready.wait(), None)
    return live.ready.is_set() and live.ended is None


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
word-break:break-all;resize:none}.small{font-size:14px}"""

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
           " first, then scan a new code."))), script=_ONE_TAP)


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
           "for a new code if this one has run out."))), script=_COPY)


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
        ", then ask Teaport for a new code."), 410)


def _expired() -> HTMLResponse:
    return _page("This code has expired", _p(
        "Ask Teaport for a new one, and scan it within a few minutes. If your phone warns "
        "that the connection is not private, install ", _ca_link(), " first."), 410)


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


def _find(code: str) -> tuple[str, _Live | None]:
    """("used" | "expired" | "live", the live offer when "live") for a code at /p/. A
    code that is neither the last one used nor the live one is a wrong guess while an
    offer waits, and MAX_WRONG of them lock it. The live code itself, asked again, is
    never one."""
    code = code.strip().upper()
    if _spent is not None and hmac.compare_digest(code.encode(), _spent.encode()):
        return "used", None  # a second scan of a used code is not a guess
    live = _live
    if live is None or live.ended is not None or live.opened:
        return "expired", None
    if _clock() >= live.offer.expires_at:
        return "expired", None
    if hmac.compare_digest(code.encode(), live.offer.code.encode()):
        return "live", live
    live.wrong += 1
    if live.wrong >= MAX_WRONG:
        logger.warning(f"pairing: {live.wrong} wrong codes; offer locked")
        _end(live, LOCKED)
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
    state, live = _find(code)
    if state == "live":
        return _choose(live.offer.code)
    return _used() if state == "used" else _expired()


@router.post("/p/{code}")
async def redeem(code: str, request: Request) -> Response:
    """The choice: the code is spent here, before the handoff is minted (a second post
    while the first mints finds it used), and the handoff is made for what was chosen."""
    global _spent
    if not _via_front_door(request):
        return PlainTextResponse(_NOT_FOUND, status_code=404)
    form = await _small_form(request)
    if form is None:  # before the code is looked at: spends nothing, guesses nothing
        return PlainTextResponse("Payload Too Large", status_code=413)
    choice = (form.get("choice") or [""])[0]
    state, live = _find(code)
    if state != "live":
        return _used() if state == "used" else _expired()
    if choice not in (DASHBOARD, APP):
        return _choose(live.offer.code)  # nothing chosen: nothing spent
    live.opened, live.choice = True, choice
    live.watch.claims[live] = (_clock() + max(DASHBOARD_TIMEOUT_SECS, QR_TIMEOUT_SECS)
                               + _window(choice))
    _spent = live.offer.code
    logger.info(f"pairing: the code was used ({choice}); making the handoff")
    # The code is spent from here, so every way out either makes the handoff (ready) or
    # ends the offer: an offer left spent with neither would be waited on for ever, and
    # hold the screen with a dead code.
    try:
        try:
            made = await (_mint() if choice == DASHBOARD else _mint_setup_code())
            answer = (RedirectResponse(front_door_url(made, FRONTDOOR_HOST), status_code=303,
                                       headers=_NO_STORE)
                      if choice == DASHBOARD else _app_code(made))
        except PairingUnavailable as e:
            logger.warning(f"pairing: {choice} handoff failed: {e.reason}")
            _end(live, FAILED)
            return _failed()
        except Exception as e:  # noqa: BLE001 — by its type: the message can hold the handoff
            logger.warning(f"pairing: {choice} handoff failed: {type(e).__name__}")
            _end(live, FAILED)
            return _failed()
        # Superseded while it minted (asked again meanwhile): the phone still gets what it
        # tapped for, and the offer that replaced this one watches for its device
        # (start(), _Watch). Cancelled: the box's session is over, the screen down.
        if live.ended is not None and live.ended != SUPERSEDED:
            return _expired()
        live.watch.claims[live] = _clock() + _window(choice)
        live.ready.set()  # wait() starts looking for the new device
        return answer
    finally:
        if not live.ready.is_set():
            # No handoff: this tap's claim goes, so a watch it was extending (its own, or
            # the one the offer that replaced it shares) shrinks back to the others'.
            live.watch.claims.pop(live, None)
            _end(live, FAILED)  # the request itself was cancelled (the phone went away)
