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
#     on the way back either. It is never logged here.
#
# One offer at a time, in memory only: start() mints, wait() follows it, cancel() ends
# it, and a second start() cancels the first. The offer ends at whichever comes first
# of OpenClaw's own deadline (browserBootstrapExpiresAtMs, less a margin for the
# browser to load the page and redeem it) and OFFER_SECS, both counted from before the
# CLI ran. The code is six characters from an alphabet a reader cannot confuse aloud or
# on a small screen (no 0/O, 1/I/L); while it is live it is as good as an admin
# password, so it is not logged, and a handful of wrong codes while an offer waits
# (MAX_WRONG) cancel it: guessing gets a few tries out of 31^6, not all night.
#
# Whether the new browser actually paired is OpenClaw's to say: `openclaw devices list
# --json` is snapshotted at mint ({deviceId: approvedAtMs}), and once the link has been
# opened, and only then, it is polled every POLL_SECS (one CLI at a time, the next
# started POLL_SECS after the last ended) for a device that is new, or approved again
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
import json
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from teaport_brain import agent_backend, openclaw_client
from teaport_brain.settings import setting
from teaport_brain.wifi import PAGE_CSS, logo_html

FRONTDOOR_HOST = setting("TEAPORT_FRONTDOOR_HOST")

# The avatar's QR limit (display.Screen.fits_qr reads the live one; this is what every
# avatar that draws QR codes says, and the link is sized for it up front).
QR_MAX_BYTES = 53
CODE_LEN = 6
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, no 1/I/L
OPENCLAW_HANDOFF_SECS = 600.0  # `dashboard` docs: "a single-use, ten-minute bootstrap"
REDEEM_MARGIN_SECS = 20.0      # the browser still has to load the UI and redeem it
OFFER_SECS = 300.0             # ours: the screen holds the code no longer than this
MAX_WRONG = 5
POLL_SECS = 2.0
OPENED_WAIT_SECS = 120.0
DASHBOARD_TIMEOUT_SECS = 30.0  # a cold CLI through the NemoClaw shim takes ~14 s
DEVICES_TIMEOUT_SECS = 20.0

PAIRED, OPENED, EXPIRED, CANCELLED = "paired", "opened", "expired", "cancelled"

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
    outcome: str        # PAIRED | OPENED | EXPIRED | CANCELLED
    device: str | None  # the new device's display name when PAIRED


@dataclass(eq=False)
class _Live:
    offer: Offer
    url: str | None             # the rewritten browserUrl; None once redeemed
    known: dict[str, int]       # deviceId -> approvedAtMs at mint
    opened: bool = False
    cancelled: bool = False
    wrong: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # opened or cancelled


_live: _Live | None = None
_spent: str | None = None  # the last redeemed code, for "already used"
_minting = 0               # generation: a newer start() supersedes one still minting


# ------------------------------------------------------------------ the CLI

async def _cli(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """The openclaw CLI, resolved and run the way every other caller runs it
    (openclaw_client: OPENCLAW_BIN, the NemoClaw shim, killed on cancel). Replaced in
    tests."""
    return await openclaw_client._run_openclaw(args, timeout=timeout, capture=True)


def _json_object(out: bytes) -> dict | None:
    """The one JSON object the CLI printed, skipping anything a plugin printed before
    it."""
    text = out.decode(errors="replace")
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


async def _paired_devices() -> list[dict]:
    """`openclaw devices list --json`'s paired list. Raises PairingUnavailable."""
    try:
        rc, out, _ = await _cli(["devices", "list", "--json"], DEVICES_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise PairingUnavailable("openclaw devices list timed out") from None
    except OSError as e:
        raise PairingUnavailable(f"openclaw CLI did not start ({e.strerror or e})") from None
    obj = _json_object(out)
    if rc != 0 or obj is None or not isinstance(obj.get("paired", []), list):
        raise PairingUnavailable(f"openclaw devices list failed (rc={rc})")
    return [d for d in obj.get("paired", []) if isinstance(d, dict) and d.get("deviceId")]


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
    obj = _json_object(out) or {}
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

async def start() -> Offer:
    """Mint a new offer, cancelling the one before it. Raises PairingUnavailable."""
    global _live, _minting
    if _live is not None:
        cancel(_live.offer)
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
    if live is not None and live.offer is offer and not live.cancelled:
        live.cancelled = True
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
    """Follow `offer` to its end. An offer that is no longer the live one (a newer
    start() replaced it) is "cancelled". One waiter per offer."""
    global _live
    live = _live
    if live is None or live.offer is not offer:
        return Result(CANCELLED, None)
    try:
        if not live.opened and not live.cancelled:
            try:
                await asyncio.wait_for(live.changed.wait(), max(0.0, offer.expires_at - _clock()))
            except asyncio.TimeoutError:
                pass
        if live.cancelled:
            return Result(CANCELLED, None)
        if not live.opened:
            logger.info("pairing: the code expired unused")
            return Result(EXPIRED, None)
        live.changed.clear()  # from here it is set only by cancel()
        deadline = _clock() + OPENED_WAIT_SECS
        while _clock() < deadline:
            done, devices = await _or_cancelled(live, _poll(), deadline - _clock())
            if live.cancelled:
                return Result(CANCELLED, None)
            found = _new_device(devices, live.known) if done and devices is not None else None
            if found is not None:
                name = device_name(found)
                logger.info(f"pairing: paired {name!r}")
                return Result(PAIRED, name)
            await _or_cancelled(live, asyncio.sleep(POLL_SECS), deadline - _clock())
            if live.cancelled:
                return Result(CANCELLED, None)
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
<p>{body}</p>
<p>{ca}</p></main></body></html>"""


def _page(heading: str, body: str, status: int) -> HTMLResponse:
    ca_url = f"http://{FRONTDOOR_HOST}/teaport-ca.crt"
    ca = (f"If your phone warns that the connection is not private, install "
          f'<a href="{html.escape(ca_url, quote=True)}">Teaport\'s certificate</a> first, '
          f"then scan a new code.")
    return HTMLResponse(_PAGE.format(title=html.escape(heading), css=PAGE_CSS, logo=logo_html(),
                                     heading=html.escape(heading), body=html.escape(body), ca=ca),
                        status_code=status, headers=_NO_STORE)


def _used() -> HTMLResponse:
    return _page("This code was already used",
                 "Each code connects one device. To connect another one, ask Teaport for a "
                 "new code.", 410)


def _expired() -> HTMLResponse:
    return _page("This code has expired",
                 "Ask Teaport for a new one, and scan it within a few minutes.", 410)


router = APIRouter()


@router.get("/p/{code}")
async def redeem(code: str) -> Response:
    """Unauthenticated: the browser asking has no token yet; the code is the grant."""
    global _spent
    code = code.strip().upper()
    if _spent is not None and hmac.compare_digest(code.encode(), _spent.encode()):
        return _used()  # a second scan of a used code is not a guess
    live = _live
    if live is not None and not live.cancelled and not live.opened:
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
            logger.warning(f"pairing: {live.wrong} wrong codes; offer cancelled")
            cancel(live.offer)
    return _expired()
