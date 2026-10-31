#
# pairing.py: the QR pairing offer, its /p/<code> redirect and the new-device watcher,
# against a fake openclaw CLI.
#
# What has to hold:
#   - The short link for the default front door fits the avatar's 53-byte QR code, and
#     a front door too long for it is refused before anything is minted.
#   - The loopback browserUrl is rewritten to the front door (base and gatewayUrl) with
#     every other fragment value kept byte for byte; a non-loopback one is left alone.
#   - /p/<code> redirects once (Location keeps the fragment, no-store, no-referrer);
#     then "already used"; an unknown or late code gets "expired".
#   - The offer expires; a new start() cancels the old one; a handful of wrong codes
#     while it is live cancel it.
#   - A device new since the mint (or approved again) is "paired" with its listed
#     name; an opened link with nothing new is "opened"; nothing is polled before the
#     link is opened.
#   - The browserUrl, its fragment and the live code never reach the log.
#   - The real subprocess path (openclaw_client._run_openclaw) runs a stub CLI.
#
# Run: python test_pairing.py   (or via pytest)
#
import asyncio
import json
import os
import stat
import sys
import time

import httpx
from fastapi import FastAPI
from loguru import logger

from teaport_brain import agent_backend, openclaw_client, pairing
import appliance
from tempdirs import tempdir

BOOTSTRAP = "bt-SECRET_xyz.123~"
BROWSER_URL = ("http://127.0.0.1:18789/#bootstrapToken=" + BOOTSTRAP
               + "&bootstrapProfile=owner&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789")


REAL_CLI = pairing._cli


class FakeCli:
    """openclaw dashboard --json / devices list --json, scripted."""

    def __init__(self, devices=None, dashboard=None, rc=0):
        self.devices = [list(devices or [])]  # one list per call; the last repeats
        self.dashboard = dashboard or {
            "ok": True, "url": "http://127.0.0.1:18789/", "httpUrl": "http://127.0.0.1:18789/",
            "wsUrl": "ws://127.0.0.1:18789", "port": 18789, "tokenIncluded": False,
            "browserUrl": BROWSER_URL,
            "browserBootstrapExpiresAtMs": int(time.time() * 1000) + 600_000}
        self.rc = rc
        self.calls = []

    async def __call__(self, args, timeout):
        self.calls.append(tuple(args))
        if args[:1] == ["dashboard"]:
            return self.rc, ("plugin noise\n" + json.dumps(self.dashboard)).encode(), b""
        lists = self.devices
        current = lists.pop(0) if len(lists) > 1 else lists[0]
        return 0, json.dumps({"pending": [], "paired": current}, indent=2).encode(), b""


def _setup(cli: FakeCli, host="teaport.local"):
    pairing._cli = cli
    pairing.FRONTDOOR_HOST = host
    pairing._live = None
    pairing._spent = None
    pairing._clock = time.monotonic
    pairing.POLL_SECS = 0.01
    pairing.OPENED_WAIT_SECS = 0.3
    agent_backend.HAS_AGENT = True


def _client():
    app = FastAPI()
    app.include_router(pairing.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://box")


class _Logs:
    def __enter__(self):
        self.lines = []
        self._id = logger.add(lambda m: self.lines.append(str(m)), level="DEBUG")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)

    @property
    def text(self):
        return "".join(self.lines)


DEV_A = {"deviceId": "dev-a", "displayName": "Old laptop", "approvedAtMs": 1000}
DEV_NEW = {"deviceId": "dev-b", "displayName": "Chrome on Pixel", "clientId": "openclaw-control-ui",
           "approvedAtMs": 2000}


def test_the_default_link_fits_the_qr_code():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        assert offer.link == f"https://teaport.local/p/{offer.code}", offer.link
        assert len(offer.link.encode()) <= 53, len(offer.link.encode())
        assert len(offer.code) == 6 and set(offer.code) <= set(pairing.CODE_ALPHABET)
        assert not set(offer.code) & set("0O1IL")
        pairing.cancel(offer)
    asyncio.run(go())


def test_a_front_door_too_long_for_the_qr_code_is_refused_before_minting():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli, host="a" * 37 + ".local")
        try:
            await pairing.start()
            raise AssertionError("minted for a link the screen cannot show")
        except pairing.PairingUnavailable as e:
            assert "too long" in e.reason
        assert cli.calls == []
        _setup(cli, host="")
        try:
            await pairing.start()
            raise AssertionError("minted with no front door")
        except pairing.PairingUnavailable as e:
            assert "front door" in e.reason
        assert cli.calls == []
    asyncio.run(go())


def test_no_agent_and_a_failed_cli_are_unavailable():
    async def go():
        _setup(FakeCli([DEV_A]))
        agent_backend.HAS_AGENT = False
        try:
            await pairing.start()
            raise AssertionError("minted with no agent")
        except pairing.PairingUnavailable as e:
            assert "agent" in e.reason
        _setup(FakeCli([DEV_A], dashboard={"ok": False, "reason": "Gateway not ready"}, rc=1))
        try:
            await pairing.start()
            raise AssertionError("an offer from a failed mint")
        except pairing.PairingUnavailable as e:
            assert e.reason == "openclaw dashboard failed: Gateway not ready", e.reason
        assert pairing._live is None
    asyncio.run(go())


def test_the_loopback_rewrite_keeps_every_other_fragment_value():
    out = pairing.front_door_url(BROWSER_URL, "teaport.local")
    assert out == ("https://teaport.local/#bootstrapToken=" + BOOTSTRAP
                   + "&bootstrapProfile=owner&gatewayUrl=wss%3A%2F%2Fteaport.local"), out
    # A Control UI base path survives on both, and an unknown fragment value is kept.
    based = ("http://localhost:18789/ui/#x=1%2B2&bootstrapToken=t&gatewayUrl="
             "ws%3A%2F%2F%5B%3A%3A1%5D%3A18789%2Fui&bootstrapProfile=owner")
    assert pairing.front_door_url(based, "box.lan") == (
        "https://box.lan/ui/#x=1%2B2&bootstrapToken=t&gatewayUrl=wss%3A%2F%2Fbox.lan%2Fui"
        "&bootstrapProfile=owner")
    # No gatewayUrl: the front door's is added.
    assert pairing.front_door_url("http://127.0.0.1:1/#bootstrapToken=t", "h") == \
        "https://h/#bootstrapToken=t&gatewayUrl=wss%3A%2F%2Fh"
    # Not loopback: OpenClaw's own address, untouched.
    tls = "https://10.0.0.5:18789/#bootstrapToken=t&gatewayUrl=wss%3A%2F%2F10.0.0.5%3A18789"
    assert pairing.front_door_url(tls, "teaport.local") == tls


def test_the_code_is_single_use():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        async with _client() as c:
            r = await c.get(f"/p/{offer.code.lower()}")
            assert r.status_code == 302, r.status_code
            assert r.headers["location"] == pairing.front_door_url(BROWSER_URL, "teaport.local")
            assert "#bootstrapToken=" in r.headers["location"]
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["referrer-policy"] == "no-referrer"
            again = await c.get(f"/p/{offer.code}")
            assert again.status_code == 410 and "already used" in again.text
            assert "http://teaport.local/teaport-ca.crt" in again.text
            assert BOOTSTRAP not in again.text
            assert pairing._live.url is None  # dropped at redemption
            pairing.cancel(offer)
            await pairing.wait(offer)
            # The offer is over, and a second scan still says "used", not "expired".
            assert "already used" in (await c.get(f"/p/{offer.code}")).text
            assert "expired" in (await c.get("/p/ZZZZZZ")).text
    asyncio.run(go())


def test_the_offer_expires():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.OFFER_SECS = 0.05
        try:
            offer = await pairing.start()
            assert offer.expires_at - time.monotonic() <= 0.05
            result = await pairing.wait(offer)
            assert result == pairing.Result("expired", None), result
            async with _client() as c:
                r = await c.get(f"/p/{offer.code}")
            assert r.status_code == 410 and "expired" in r.text
            assert pairing._live is None
            # Never opened: devices list ran once, for the snapshot.
            assert cli.calls.count(("devices", "list", "--json")) == 1
        finally:
            pairing.OFFER_SECS = 300.0
    asyncio.run(go())


def test_openclaws_deadline_bounds_the_offer():
    async def go():
        cli = FakeCli([DEV_A])
        cli.dashboard["browserBootstrapExpiresAtMs"] = int(time.time() * 1000) + 60_000
        _setup(cli)
        offer = await pairing.start()
        left = offer.expires_at - time.monotonic()
        assert 60 - pairing.REDEEM_MARGIN_SECS - 2 < left <= 60 - pairing.REDEEM_MARGIN_SECS, left
        pairing.cancel(offer)
    asyncio.run(go())


def test_a_new_start_cancels_the_old_offer():
    async def go():
        _setup(FakeCli([DEV_A]))
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        await asyncio.sleep(0)
        second = await pairing.start()
        assert (await waiter) == pairing.Result("cancelled", None)
        async with _client() as c:
            if first.code != second.code:  # 1 in 31^6 they are the same
                assert (await c.get(f"/p/{first.code}")).status_code == 410
            assert (await c.get(f"/p/{second.code}")).status_code == 302
        assert (await pairing.wait(first)) == pairing.Result("cancelled", None)
        pairing.cancel(second)
    asyncio.run(go())


def test_wrong_codes_cancel_a_live_offer():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(offer))
        wrong = "".join(c for c in pairing.CODE_ALPHABET if c not in offer.code)[:6]
        async with _client() as c:
            for _ in range(pairing.MAX_WRONG):
                assert (await c.get(f"/p/{wrong}")).status_code == 410
            assert (await waiter) == pairing.Result("cancelled", None)
            r = await c.get(f"/p/{offer.code}")
            assert r.status_code == 410 and "expired" in r.text
    asyncio.run(go())


def test_a_new_device_after_the_link_is_paired():
    async def go():
        cli = FakeCli()
        cli.devices = [[DEV_A], [DEV_A], [DEV_A, DEV_NEW]]
        _setup(cli)
        offer = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(offer))
        await asyncio.sleep(0.05)
        assert cli.calls.count(("devices", "list", "--json")) == 1  # no poll before the link
        async with _client() as c:
            assert (await c.get(f"/p/{offer.code}")).status_code == 302
        assert (await waiter) == pairing.Result("paired", "Chrome on Pixel")
        assert pairing._live is None
    asyncio.run(go())


def test_a_repaired_device_counts_and_its_label_wins():
    async def go():
        cli = FakeCli()
        relabeled = {**DEV_A, "operatorLabel": "Kitchen Mac", "approvedAtMs": 5000}
        cli.devices = [[DEV_A], [relabeled]]
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            assert (await c.get(f"/p/{offer.code}")).status_code == 302
        assert (await pairing.wait(offer)) == pairing.Result("paired", "Kitchen Mac")
    asyncio.run(go())


def test_an_opened_link_with_nothing_new_is_opened():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            assert (await c.get(f"/p/{offer.code}")).status_code == 302
        assert (await pairing.wait(offer)) == pairing.Result("opened", None)
        assert cli.calls.count(("devices", "list", "--json")) >= 3
    asyncio.run(go())


def test_cancel_while_polling_stops_the_wait():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.OPENED_WAIT_SECS = 30
        offer = await pairing.start()
        async with _client() as c:
            assert (await c.get(f"/p/{offer.code}")).status_code == 302
        waiter = asyncio.ensure_future(pairing.wait(offer))
        await asyncio.sleep(0.05)
        pairing.cancel(offer)
        assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("cancelled", None)
    asyncio.run(go())


def test_the_fragment_and_the_code_are_never_logged():
    async def go():
        with _Logs() as logs:
            cli = FakeCli()
            cli.devices = [[DEV_A], [DEV_A, DEV_NEW]]
            _setup(cli)
            offer = await pairing.start()
            async with _client() as c:
                await c.get("/p/WRONG2")
                await c.get(f"/p/{offer.code}")
                await c.get(f"/p/{offer.code}")
            await pairing.wait(offer)
            # A failed mint is logged by its reason, never by what the CLI printed.
            bad = FakeCli([DEV_A], rc=1)
            bad.dashboard = {"ok": False, "browserUrl": BROWSER_URL}
            _setup(bad)
            try:
                await pairing.start()
            except pairing.PairingUnavailable as e:
                logger.warning(f"pair_device: {e.reason}")
        assert "pairing: paired" in logs.text, logs.text
        for secret in (BOOTSTRAP, "bootstrapToken", "#", offer.code):
            assert secret not in logs.text, (secret, logs.text)
    asyncio.run(go())


def test_the_real_subprocess_runs_a_stub_cli():
    # The CLI is run through openclaw_client's runner (OPENCLAW_BIN, PATH, kill on
    # cancel), not a copy of it.
    async def go():
        d = tempdir("pairing-cli-")
        stub = os.path.join(d, "openclaw")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable}\n"
                    "import json, sys\n"
                    "if sys.argv[1] == 'dashboard':\n"
                    f"    print(json.dumps({{'ok': True, 'browserUrl': {BROWSER_URL!r}}}))\n"
                    "else:\n"
                    "    print(json.dumps({'pending': [], 'paired': []}))\n")
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        old_bin = openclaw_client.OPENCLAW_BIN
        _setup(FakeCli())
        pairing._cli = REAL_CLI
        openclaw_client.OPENCLAW_BIN = stub
        try:
            offer = await pairing.start()
            assert pairing._live.url.startswith("https://teaport.local/#bootstrapToken=")
            pairing.cancel(offer)
        finally:
            openclaw_client.OPENCLAW_BIN = old_bin
    asyncio.run(go())


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
