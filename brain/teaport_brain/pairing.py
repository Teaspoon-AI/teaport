#
# teaport — pair a laptop or phone browser by scanning a QR code on the box (issue #243).
#
# OpenClaw already has the handoff: `openclaw dashboard --json` mints a browserUrl, a
# link to the Control UI whose URL fragment carries a single-use, ten-minute owner
# bootstrap. The browser that opens it is left paired as an administrator device, with
# no `openclaw devices approve` step. Two things stand between that link and a phone
# camera, and this module is both:
#
#   - the link points at loopback. The gateway listens on 127.0.0.1 behind the front
#     door (Caddy, install.sh write_caddyfile), so the minted
#     http://127.0.0.1:18789/#bootstrapToken=...&bootstrapProfile=owner&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789
#     is rewritten as OpenClaw's own docs say (web/control-ui/connect-and-pair.md): its
#     base becomes https://<front door>/ and its gatewayUrl fragment value wss://<front
#     door>, every other fragment value kept byte for byte. The front door is
#     TEAPORT_FRONTDOOR_HOST, never a request's Host header: the link is built before
#     any request, and a Host header is whatever the asker typed.
#   - the link is far too long for the OLED. The avatar draws a scannable QR code of at
#     most 53 bytes (display.Screen.fits_qr), and the bootstrap alone is longer. So the
#     screen shows https://<front door>/p/<code> instead, ~30 bytes, and /p/<code>
#     (router, below; Caddy routes /p/* to the brain) answers its first hit with a 302
#     to the rewritten browserUrl. The fragment rides in the Location header, and a
#     browser never sends a fragment to a server, so the bootstrap reaches no access log
#     on the way back either. It is never logged here, and the access log's request
#     line has the code masked (gateway_server._RedactTokens: /p/<redacted>). /p/
#     answers only what came through Caddy over HTTPS (_via_front_door); the brain's
#     own port, open to the LAN for the config page, gets a 404 there that does not
#     count as a guess.
#
# One offer at a time, in memory only (a brain restart forgets it, and its code is then
# "expired"): start() mints, wait() follows it, cancel() ends it ("cancelled"),
# withdraw() ends it too unless its link was opened (then the device is still looked
# for: the code is spent, and the confirmation is worth keeping), and a
# second start() supersedes the first ("superseded") -- unless the first's link has
# been opened and its device is still being looked for, when start() is refused
# instead of losing that confirmation. The offer ends at whichever comes first
# of OpenClaw's own deadline (browserBootstrapExpiresAtMs, less a margin for the
# browser to load the page and redeem it) and OFFER_SECS, both counted from before the
# CLI ran. The code is six characters from an alphabet a reader cannot confuse aloud or
# on a small screen (no 0/O, 1/I/L); while it is live it is as good as an admin
# password, so it is not logged, and a handful of wrong codes while an offer waits
# (MAX_WRONG) end it ("locked"): guessing gets a few tries out of 31^6, not all night.
#
# Whether the new browser actually paired is OpenClaw's to say: `openclaw devices list
# --json` is snapshotted at mint ({deviceId: approvedAtMs}; a list without its
# "paired" key is a failed read, never an empty box), and once the link has been
# opened, and only then, it is polled every POLL_SECS (one CLI at a time, the next
# started POLL_SECS after the last ended, so at most ~20 in OPENED_WAIT_SECS) for a device that is new, or approved again
# since (the handoff can also repair a browser that was paired before). Found:
# "paired", with the name `devices list` itself shows (operator label, then the
# client's display name, then its client id, then the device id). Nothing within
# OPENED_WAIT_SECS: "opened" -- the link was used, and what happened in that browser
# is not ours to see (an untrusted certificate the phone would not click through, a
# private window, a closed tab).
#
# Lean on purpose (box RAM is tight): no thread, no task of its own, nothing polled
# while no link is opened; the polling runs inside the caller's wait(), each poll is a
# cold OpenClaw CLI that exits, and when the offer ends its browserUrl is dropped (it
# already is at redemption). What stays is the last redeemed code, six characters, so
# a second scan of a used QR says "already used" rather than "expired".
#
# The pages are English. The phone page of Wi-Fi setup follows Accept-Language through
# i18n.py, but every string there is translated in every catalog (the i18n tests hold
# it to that); two short pages a user sees only when something went wrong
# are not worth that until the voice side of pairing is translated too.
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
OPENED_WAIT_SECS = 120.0
DASHBOARD_TIMEOUT_SECS = 30.0  # a cold CLI through the NemoClaw shim takes ~14 s
DEVICES_TIMEOUT_SECS = 20.0

PAIRED, OPENED, EXPIRED = "paired", "opened", "expired"
CANCELLED = "cancelled"    # cancel() was called
SUPERSEDED = "superseded"  # a newer start() replaced the offer before its link was used
LOCKED = "locked"          # MAX_WRONG wrong codes while it waited
# PairingUnavailable.reason while an opened offer is still confirming its device (the
# pair_device tool matches on this, not on the wording).
STILL_CONFIRMING = "still confirming the last device paired"

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
    outcome: str        # PAIRED | OPENED | EXPIRED | CANCELLED | SUPERSEDED | LOCKED
    device: str | None = None  # the new device's display name when PAIRED


@dataclass(eq=False)
class _Live:
    offer: Offer
    url: str | None             # the rewritten browserUrl; None once redeemed
    known: dict[str, int]       # deviceId -> approvedAtMs at mint
    opened: bool = False
    ended: str | None = None    # CANCELLED | SUPERSEDED | LOCKED once ended early
    wrong: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # opened or ended


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
    """The name `openclaw devices list` shows for a paired device."""
    for key in ("operatorLabel", "displayName", "clientId", "deviceId"):
        value = str(device.get(key) or "").strip()
        if value:
            return value
    return ""


def _new_device(devices: list[dict], known: dict[str, int]) -> dict | None:
    """A device that is not in the snapshot, or was approved again since it was taken.
    The newest approval wins when there are several."""
    fresh = [d for d in devices
             if int(d.get("approvedAtMs") or 0) > known.get(str(d["deviceId"]), -1)]
    return max(fresh, key=lambda d: int(d.get("approvedAtMs") or 0), default=None)


async def _mint() -> tuple[str, float]:
    """(browserUrl, OpenClaw's own deadline as seconds left). Raises PairingUnavailable;
    the reason is OpenClaw's own {"ok": false, "reason"} when it gave one."""
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
    left = OPENCLAW_HANDOFF_SECS
    expires_ms = obj.get("browserBootstrapExpiresAtMs")
    if isinstance(expires_ms, (int, float)):
        left = min(left, expires_ms / 1000.0 - time.time())
    return url, left


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
    """Mint a new offer, superseding one whose link is not used yet. Raises
    PairingUnavailable, also while the last offer's link has been opened and its
    device is still being looked for: ending that would lose its confirmation, and a
    snapshot taken now could miss that device and later report it as the new one."""
    global _live, _minting
    if _live is not None and _live.ended is None:
        if _live.opened:
            raise PairingUnavailable(STILL_CONFIRMING)
        _end(_live, SUPERSEDED)
    _minting += 1
    mine = _minting
    if not agent_backend.HAS_AGENT:
        raise PairingUnavailable("no agent on this box (TEAPORT_AGENT=none)")
    host = _check_host(FRONTDOOR_HOST)
    t0 = _clock()
    # The snapshot first: a device that pairs between the two calls is then still new.
    known = _approvals(await _paired_devices())
    browser_url, openclaw_left = await _mint()
    now = _clock()
    expires_at = min(t0 + OFFER_SECS, t0 + OPENCLAW_HANDOFF_SECS - REDEEM_MARGIN_SECS,
                     now + openclaw_left - REDEEM_MARGIN_SECS)
    if expires_at <= now:
        raise PairingUnavailable("OpenClaw's handoff expired as it was minted")
    if mine != _minting:  # a newer start() came in while this one minted
        raise PairingUnavailable("superseded by a newer pairing request")
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    offer = Offer(code=code, link=_link(host, code), expires_at=expires_at)
    _live = _Live(offer=offer, url=front_door_url(browser_url, host), known=known)
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
        live.url = None
        live.changed.set()


async def _or_cancelled(live: _Live, aw, timeout: float):
    """(done, value): `aw`'s value, unless the offer is cancelled or `timeout` passes
    first, when `aw` is cancelled (a CLI child is killed) and done is False."""
    task = asyncio.ensure_future(aw)
    stop = asyncio.ensure_future(live.changed.wait())
    try:
        await asyncio.wait({task, stop}, timeout=max(0.0, timeout),
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
        if not live.opened and live.ended is None:
            try:
                await asyncio.wait_for(live.changed.wait(), max(0.0, offer.expires_at - _clock()))
            except asyncio.TimeoutError:
                pass
        if live.ended:
            return Result(live.ended)
        if not live.opened:
            logger.info("pairing: the code expired unused")
            return Result(EXPIRED, None)
        live.changed.clear()  # from here it is set only by cancel()
        # (start() refuses while this polls, and wrong codes no longer count.)
        deadline = _clock() + OPENED_WAIT_SECS
        while _clock() < deadline:
            done, devices = await _or_cancelled(live, _poll(), deadline - _clock())
            if live.ended:
                return Result(live.ended)
            found = _new_device(devices, live.known) if done and devices is not None else None
            if found is not None:
                name = device_name(found)
                logger.info(f"pairing: paired {name!r}")
                return Result(PAIRED, name)
            await _or_cancelled(live, asyncio.sleep(POLL_SECS), deadline - _clock())
            if live.ended:
                return Result(live.ended)
        logger.info(f"pairing: link opened, no new device within {OPENED_WAIT_SECS:.0f} s")
        return Result(OPENED, None)
    finally:
        live.url = None
        if _live is live:
            _live = None


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
<p>{body}</p></main></body></html>"""


def _ca_link() -> str:
    url = html.escape(f"http://{FRONTDOOR_HOST}/teaport-ca.crt", quote=True)
    return f'<a href="{url}">Teaport\'s certificate</a>'


def _page(heading: str, body: str, status: int) -> HTMLResponse:
    """`body` is HTML: plain text through html.escape plus _ca_link()."""
    return HTMLResponse(_PAGE.format(title=html.escape(heading), css=PAGE_CSS, logo=logo_html(),
                                     heading=html.escape(heading), body=body),
                        status_code=status, headers=_NO_STORE)


def _used() -> HTMLResponse:
    # The scan that used the code was redirected at once, so this is the page a phone
    # sees when it scans again -- most often because the dashboard did not open the
    # first time, and the usual reason is a phone that does not trust the box yet.
    return _page("This code was already used",
                 html.escape("Each code connects one device. If the dashboard did not open "
                             "on this phone, it needs to trust the box first: install ")
                 + _ca_link() + html.escape(", then ask Teaport for a new code."), 410)


def _expired() -> HTMLResponse:
    return _page("This code has expired",
                 html.escape("Ask Teaport for a new one, and scan it within a few minutes. "
                             "If your phone warns that the connection is not private, install ")
                 + _ca_link() + html.escape(" first."), 410)


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


router = APIRouter()


@router.get("/p/{code}")
async def redeem(code: str, request: Request) -> Response:
    """Unauthenticated: the browser asking has no token yet; the code is the grant."""
    global _spent
    if not _via_front_door(request):
        return PlainTextResponse("Not Found", status_code=404)  # not a guess either
    code = code.strip().upper()
    if _spent is not None and hmac.compare_digest(code.encode(), _spent.encode()):
        return _used()  # a second scan of a used code is not a guess
    live = _live
    if live is not None and live.ended is None and not live.opened:
        if _clock() >= live.offer.expires_at:
            return _expired()
        if hmac.compare_digest(code.encode(), live.offer.code.encode()) and live.url:
            url, live.url = live.url, None
            live.opened = True
            _spent = live.offer.code
            live.changed.set()
            logger.info("pairing: the code was used; waiting for the new device")
            return RedirectResponse(url, status_code=302, headers=_NO_STORE)
        live.wrong += 1
        if live.wrong >= MAX_WRONG:
            logger.warning(f"pairing: {live.wrong} wrong codes; offer locked")
            _end(live, LOCKED)
    return _expired()
