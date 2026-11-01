#
# pairing.py: the QR pairing offer, its /p/<code> redirect and the new-device watcher,
# against a fake openclaw CLI.
#
# What has to hold:
#   - The short link for the default front door fits the avatar's 53-byte QR code, and
#     a front door too long for it is refused before anything is minted.
#   - The loopback browserUrl is rewritten to the front door (base and gatewayUrl) with
#     every other fragment value kept byte for byte; a non-loopback one is left alone.
#   - /p/<code>: a GET shows two choices and spends nothing; the POST of a choice
#     spends the code and mints then: the dashboard 303s (Location keeps the fragment,
#     no-store, no-referrer), the app gets its setup code once; a failed mint ends it;
#     then "already used", which says to trust the box first; an unknown or late code
#     gets "expired". Only what came through Caddy over HTTPS (a loopback peer saying
#     X-Forwarded-Proto https, or uvicorn's proxy-headers scheme) is answered; the rest
#     is a 404 that is not a wrong guess.
#   - available() needs a front door. One pairing at a time: its first start() reads
#     the device list once; asking again gives a new code in the same pairing (the old
#     one "already used" if tapped, else "expired", never a guess), and the pairing
#     goes on watching for a device an earlier code brings in -- "paired" even if the
#     new code is never used, "opened" (not "expired") when a handoff's watch outlasts
#     the code, with spent() saying "finish on your device". cancel() ends it,
#     in-flight taps included (no 303 after it); withdraw() keeps a pairing with a
#     handoff made; wrong codes lock the code ("locked" when nothing else is left).
#   - A tap's mint that ends any way but the handoff drops the tap; the pairing is
#     "failed" only when that leaves nothing to wait for and no handoff was made. No
#     poll runs beside a mint. A POST body past 1 KiB is a 413 that spends nothing and
#     is no guess.
#   - A device new since the snapshot (or approved again) is "paired" with its listed
#     name; nothing is polled before a handoff is made, and polls are ~5 s apart for
#     at most ~2 minutes (5 for the app).
#   - A devices list without "paired" is a failed read, not an empty box; banners
#     before the CLI's JSON are skipped by the one parser (openclaw_client.cli_json),
#     which the consult uses too.
#   - The browserUrl, its fragment and the live code never reach the log (the access
#     log's /p/<code> is masked by gateway_server: test_local_audio's uvicorn test).
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
SETUP_CODE = "eyJ1cmwiOiJ3c3M6Ly90ZWFwb3J0LmxvY2FsIiwiYm9vdHN0cmFwVG9rZW4iOiJTRVRVUC1TRUNSRVQifQ"
BROWSER_URL = ("http://127.0.0.1:18789/#bootstrapToken=" + BOOTSTRAP
               + "&bootstrapProfile=owner&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789")


REAL_CLI = pairing._cli
DEFAULT_POLL_SECS, DEFAULT_OPENED_WAIT_SECS = pairing.POLL_SECS, pairing.OPENED_WAIT_SECS
DEFAULT_APP_WAIT_SECS = pairing.APP_WAIT_SECS


class FakeCli:
    """openclaw dashboard --json / qr --json --limited / devices list --json, scripted."""

    def __init__(self, devices=None, dashboard=None, rc=0, qr=None, qr_rc=0):
        self.devices = [list(devices or [])]  # one list per call; the last repeats
        self.dashboard = dashboard or {
            "ok": True, "url": "http://127.0.0.1:18789/", "httpUrl": "http://127.0.0.1:18789/",
            "wsUrl": "ws://127.0.0.1:18789", "port": 18789, "tokenIncluded": False,
            "browserUrl": BROWSER_URL,
            "browserBootstrapExpiresAtMs": int(time.time() * 1000) + 600_000}
        self.rc = rc
        self.qr = qr or {"setupCode": SETUP_CODE, "gatewayUrl": "wss://teaport.local",
                         "auth": "token", "urlSource": "plugins.entries.device-pair.config.publicUrl",
                         "access": "limited"}
        self.qr_rc = qr_rc
        self.calls = []

    async def __call__(self, args, timeout):
        self.calls.append(tuple(args))
        if args[:1] == ["dashboard"]:
            return self.rc, ("plugin noise\n" + json.dumps(self.dashboard)).encode(), b""
        if args[:1] == ["qr"]:
            return self.qr_rc, json.dumps(self.qr, indent=2).encode(), b""
        lists = self.devices
        current = lists.pop(0) if len(lists) > 1 else lists[0]
        return 0, json.dumps({"pending": [], "paired": current}, indent=2).encode(), b""


def _setup(cli: FakeCli, host="teaport.local"):
    pairing._cli = cli
    pairing.FRONTDOOR_HOST = host
    pairing._session = None
    pairing._ended = None
    pairing._creating = None
    pairing._spent.clear()
    pairing._recent.clear()
    pairing._clock = time.monotonic
    pairing.POLL_SECS = 0.01
    pairing.OPENED_WAIT_SECS = 0.3
    pairing.APP_WAIT_SECS = 0.3
    agent_backend.HAS_AGENT = True


def _client(peer="127.0.0.1", proto="https"):
    """A client as Caddy is one: a loopback peer that says X-Forwarded-Proto https."""
    app = FastAPI()
    app.include_router(pairing.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 40000)),
                             base_url="http://box",
                             headers={"X-Forwarded-Proto": proto} if proto else {})


async def _take(c, code, choice="dashboard"):
    """Press a button on the /p/ page."""
    return await c.post(f"/p/{code}", data={"choice": choice})


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
# The OpenClaw iPhone app: a device of its own, node and operator roles on one entry.
DEV_PHONE = {"deviceId": "dev-p", "deviceFamily": "iPhone", "platform": "iOS 19.1",
             "clientId": "openclaw-ios", "approvedAtMs": 3000,
             "tokens": [{"role": "node", "scopes": []},
                        {"role": "operator", "scopes": ["operator.read", "operator.write"]}]}
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
        # A devices list that fails: no offer.
        cli = FakeCli([DEV_A])
        async def broken(args, timeout):
            return 1, b"", b"gateway closed"
        _setup(cli)
        pairing._cli = broken
        try:
            await pairing.start()
            raise AssertionError("an offer with no snapshot")
        except pairing.PairingUnavailable as e:
            assert "devices list failed" in e.reason, e.reason
        assert pairing._session is None
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
            r = await _take(c, offer.code.lower())
            assert r.status_code == 303, r.status_code
            assert r.headers["location"] == pairing.front_door_url(BROWSER_URL, "teaport.local")
            assert "#bootstrapToken=" in r.headers["location"]
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["referrer-policy"] == "no-referrer"
            again = await c.get(f"/p/{offer.code}")
            assert again.status_code == 410 and "already used" in again.text
            # Said plainly: the likely reason a phone scans twice, and what to do.
            assert "did not open" in again.text and "trust the box first" in again.text
            assert 'href="http://teaport.local/teaport-ca.crt"' in again.text
            assert "ask Teaport for a new code" in again.text
            assert BOOTSTRAP not in again.text
            assert (await _take(c, offer.code, "app")).status_code == 410  # spent for both
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
            assert pairing._session is None
            # Never opened: devices list ran once, for the snapshot.
            assert cli.calls.count(("devices", "list", "--json")) == 1
        finally:
            pairing.OFFER_SECS = 300.0
    asyncio.run(go())


def test_the_offer_is_bounded_by_ours_and_nothing_is_minted_up_front():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        left = offer.expires_at - time.monotonic()
        assert pairing.OFFER_SECS - 2 < left <= pairing.OFFER_SECS, left
        assert cli.calls == [("devices", "list", "--json")]  # no handoff until a tap
        pairing.cancel(offer)
    asyncio.run(go())


def test_a_handoff_that_is_already_expiring_fails_at_the_tap():
    async def go():
        cli = FakeCli([DEV_A])
        cli.dashboard["browserBootstrapExpiresAtMs"] = int(time.time() * 1000) + 5_000
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            r = await _take(c, offer.code)
        assert r.status_code == 503 and "Ask Teaport again" in r.text
        assert (await pairing.wait(offer)) == pairing.Result("failed", None)
    asyncio.run(go())


def test_asking_again_gives_a_new_code_in_the_same_pairing():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        await asyncio.sleep(0)
        second = await pairing.start()
        assert second.session == first.session and second.code != first.code
        assert cli.calls.count(("devices", "list", "--json")) == 1  # one snapshot
        assert not waiter.done()  # the same pairing goes on
        async with _client() as c:
            r = await c.get(f"/p/{first.code}")
            assert r.status_code == 410 and "expired" in r.text  # untapped: "expired"
            assert pairing._session.wrong == 0  # its own old code is no guess
            assert (await _take(c, second.code)).status_code == 303
            r = await c.get(f"/p/{second.code}")
            assert "already used" in r.text
        assert (await asyncio.wait_for(waiter, 2)) == pairing.Result("opened", None)
        assert (await pairing.wait(second)) == pairing.Result("opened", None)  # its Result
    asyncio.run(go())


def test_wrong_codes_lock_a_live_offer():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(offer))
        wrong = "".join(c for c in pairing.CODE_ALPHABET if c not in offer.code)[:6]
        async with _client() as c:
            for _ in range(pairing.MAX_WRONG):
                assert (await c.get(f"/p/{wrong}")).status_code == 410
            assert (await waiter) == pairing.Result("locked", None)
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
            assert (await _take(c, offer.code)).status_code == 303
        assert (await waiter) == pairing.Result("paired", "Chrome on Pixel")
        assert pairing._session is None
    asyncio.run(go())


def test_a_repaired_device_counts_and_its_label_wins():
    async def go():
        cli = FakeCli()
        relabeled = {**DEV_A, "operatorLabel": "Kitchen Mac", "approvedAtMs": 5000}
        cli.devices = [[DEV_A], [relabeled]]
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            assert (await _take(c, offer.code)).status_code == 303
        assert (await pairing.wait(offer)) == pairing.Result("paired", "Kitchen Mac")
    asyncio.run(go())


def test_an_opened_link_with_nothing_new_is_opened():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            assert (await _take(c, offer.code)).status_code == 303
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
            assert (await _take(c, offer.code)).status_code == 303
        waiter = asyncio.ensure_future(pairing.wait(offer))
        await asyncio.sleep(0.05)
        pairing.cancel(offer)
        assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("cancelled", None)
    asyncio.run(go())


def test_withdraw_cancels_an_unopened_offer_and_keeps_a_confirmation():
    """The code off the screen: one nobody opened is cancelled; one already opened is
    spent, and its device is still looked for (pair_device's cancel)."""
    async def go():
        cli = FakeCli()
        cli.devices = [[DEV_A], [DEV_A], [DEV_A], [DEV_A, DEV_NEW]]
        _setup(cli)
        offer = await pairing.start()
        assert pairing.withdraw(offer) is False
        assert (await pairing.wait(offer)) == pairing.Result("cancelled", None)
        assert pairing.withdraw(offer) is False  # no longer live: nothing to do

        offer = await pairing.start()
        async with _client() as c:
            assert (await _take(c, offer.code)).status_code == 303
        assert pairing.withdraw(offer) is True
        assert (await asyncio.wait_for(pairing.wait(offer), 5)) == pairing.Result(
            "paired", "Chrome on Pixel")
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
                await _take(c, offer.code)
                await c.get(f"/p/{offer.code}")
            await pairing.wait(offer)
            codes = [offer.code]
            # The app's setup code: shown on the page, never logged.
            cli = FakeCli()
            cli.devices = [[DEV_A], [DEV_A, DEV_PHONE]]
            _setup(cli)
            offer = await pairing.start()
            async with _client() as c:
                page = await _take(c, offer.code, "app")
            assert SETUP_CODE in page.text
            await pairing.wait(offer)
            codes.append(offer.code)
            # A failed mint is logged by its reason, never by what the CLI printed.
            for bad in (FakeCli([DEV_A], rc=1, dashboard={"ok": False, "browserUrl": BROWSER_URL}),
                        FakeCli([DEV_A], qr_rc=1, qr={"setupCode": SETUP_CODE})):
                _setup(bad)
                offer = await pairing.start()
                async with _client() as c:
                    await _take(c, offer.code, "dashboard" if bad.rc else "app")
                await pairing.wait(offer)
                codes.append(offer.code)
        assert "pairing: paired" in logs.text, logs.text
        assert "handoff failed" in logs.text, logs.text
        for secret in (BOOTSTRAP, "bootstrapToken", "#", SETUP_CODE, "SETUP-SECRET", *codes):
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
                    "elif sys.argv[1:] == ['qr', '--json', '--limited']:\n"
                    f"    print(json.dumps({{'setupCode': {SETUP_CODE!r}, 'gatewayUrl': 'wss://teaport.local'}}))\n"
                    "else:\n"
                    "    print(json.dumps({'pending': [], 'paired': []}))\n")
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        old_bin = openclaw_client.OPENCLAW_BIN
        _setup(FakeCli())
        pairing._cli = REAL_CLI
        openclaw_client.OPENCLAW_BIN = stub
        try:
            for choice in ("dashboard", "app"):
                offer = await pairing.start()
                async with _client() as c:
                    r = await _take(c, offer.code, choice)
                if choice == "dashboard":
                    assert r.status_code == 303, r.status_code
                    assert r.headers["location"].startswith("https://teaport.local/#bootstrapToken=")
                else:
                    assert r.status_code == 200 and SETUP_CODE in r.text, r.status_code
                pairing.cancel(offer)
                await pairing.wait(offer)
        finally:
            openclaw_client.OPENCLAW_BIN = old_bin
    asyncio.run(go())


def test_the_brains_gateway_url_is_not_handed_to_the_cli():
    # brain.env sets OPENCLAW_GATEWAY_URL for the brain's HTTP lane. The CLI reads the
    # same name as an override and then refuses its configured credentials (rc 1), so
    # the stub fails the same way when it sees it.
    async def go():
        d = tempdir("pairing-cli-env-")
        stub = os.path.join(d, "openclaw")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    "if 'OPENCLAW_GATEWAY_URL' in os.environ:\n"
                    "    print('gateway url override requires explicit credentials',"
                    " file=sys.stderr); sys.exit(1)\n"
                    "if sys.argv[1] == 'dashboard':\n"
                    f"    print(json.dumps({{'ok': True, 'browserUrl': {BROWSER_URL!r}}}))\n"
                    "elif sys.argv[1:] == ['qr', '--json', '--limited']:\n"
                    f"    print(json.dumps({{'setupCode': {SETUP_CODE!r}, 'gatewayUrl': 'wss://teaport.local'}}))\n"
                    "else:\n"
                    "    print(json.dumps({'pending': [], 'paired': []}))\n")
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        old_bin, old_url = openclaw_client.OPENCLAW_BIN, os.environ.get("OPENCLAW_GATEWAY_URL")
        _setup(FakeCli())
        pairing._cli = REAL_CLI
        openclaw_client.OPENCLAW_BIN = stub
        os.environ["OPENCLAW_GATEWAY_URL"] = "http://127.0.0.1:18789"
        try:
            offer = await pairing.start()
            async with _client() as c:
                assert (await _take(c, offer.code, "app")).status_code == 200
            pairing.cancel(offer)
            await pairing.wait(offer)
        finally:
            openclaw_client.OPENCLAW_BIN = old_bin
            if old_url is None:
                os.environ.pop("OPENCLAW_GATEWAY_URL", None)
            else:
                os.environ["OPENCLAW_GATEWAY_URL"] = old_url
    asyncio.run(go())


def test_a_device_is_named_for_a_person_never_by_its_client_id():
    # The shapes `openclaw devices list --json` gave on the box: a Control UI browser has
    # no name of its own, only clientId "openclaw-control-ui" and navigator.platform.
    ui = {"deviceId": "d1", "clientId": "openclaw-control-ui"}
    assert pairing.device_name({**ui, "platform": "iPhone"}) == "iPhone"
    assert pairing.device_name({**ui, "platform": "MacIntel"}) == "Mac"
    assert pairing.device_name({**ui, "platform": "Win32"}) == "Windows PC"
    assert pairing.device_name({**ui, "platform": "Linux x86_64"}) == "Linux computer"
    assert pairing.device_name({**ui, "platform": "web"}) == ""
    assert pairing.device_name(ui) == ""
    # A native app's family wins over its OS string; a real label wins over both.
    assert pairing.device_name({"clientId": "openclaw-ios", "platform": "iOS 18.3.2",
                                "deviceFamily": "iPhone"}) == "iPhone"
    assert pairing.device_name({"displayName": "Grokani viewer",
                                "platform": "Linux x86_64"}) == "Grokani viewer"
    assert pairing.device_name({"operatorLabel": "Kitchen tablet", "displayName": "x",
                                "platform": "iPad"}) == "Kitchen tablet"


def test_every_page_with_the_certificate_link_says_how_to_trust_it():
    # One collapsible block per kind of device, closed until tapped, no script needed;
    # the failure page has no certificate link and no help.
    _setup(FakeCli())
    pages = {"choose": pairing._choose("K7Q2XM"), "app": pairing._app_code("SETUP"),
             "used": pairing._used(), "expired": pairing._expired()}
    for name, r in pages.items():
        body = r.body.decode()
        assert 'href="http://teaport.local/teaport-ca.crt"' in body, name
        for device in ("iPhone or iPad", "Android", "Windows", "Mac"):
            assert f"<summary>{device}</summary>" in body, (name, device)
        assert body.count("<details>") == 4 and "<details open" not in body, name
        assert "Certificate Trust Settings" in body and "Always Trust" in body, name
    assert "<details>" not in pairing._failed().body.decode()


def test_available_needs_a_front_door():
    _setup(FakeCli([DEV_A]), host="")
    assert pairing.available() is False
    _setup(FakeCli([DEV_A]))
    assert pairing.available() is True


def test_p_answers_only_through_the_front_door():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        # The brain's own port, from the LAN: cleartext, or a forged header from a
        # peer that is not loopback. 404, and no number of them is a wrong guess.
        async with _client(peer="192.168.1.9", proto="https") as lan, \
                _client(proto=None) as plain:
            for _ in range(pairing.MAX_WRONG + 2):
                assert (await lan.get("/p/WRONG2")).status_code == 404
                assert (await plain.get("/p/WRONG2")).status_code == 404
            assert (await lan.get(f"/p/{offer.code}")).status_code == 404
            assert (await plain.get(f"/p/{offer.code}")).status_code == 404
        assert pairing._session.wrong == 0 and pairing._session.result is None
        async with _client() as c:
            assert (await _take(c, offer.code)).status_code == 303
        pairing.cancel(offer)
    asyncio.run(go())


def test_uvicorns_proxy_headers_still_let_caddy_through():
    # In the brain, uvicorn's ProxyHeadersMiddleware (trusting 127.0.0.1) has already
    # turned Caddy's headers into the scheme and the phone's address.
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        app = FastAPI()
        app.include_router(pairing.router)
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
        def client(peer):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=wrapped, client=(peer, 40000)),
                base_url="http://box",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.168.1.50"})
        async with client("192.168.1.9") as lan:  # not trusted: the headers are ignored
            assert (await lan.get(f"/p/{offer.code}")).status_code == 404
        async with client("127.0.0.1") as caddy:
            assert (await _take(caddy, offer.code)).status_code == 303
        pairing.cancel(offer)
    asyncio.run(go())


def test_a_devices_list_without_paired_is_a_failure_not_an_empty_box():
    async def go():
        cli = FakeCli([DEV_A])
        async def no_paired(args, timeout):
            if args[:1] == ["devices"]:
                return 0, b'{"level": "info", "msg": "plugin loaded"}\n{"pending": []}\n', b""
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = no_paired
        try:
            await pairing.start()
            raise AssertionError("snapshot taken from an object with no paired list")
        except pairing.PairingUnavailable as e:
            assert "devices list failed" in e.reason
        # A banner object before the real one is skipped, not read as the result.
        async def banner_first(args, timeout):
            if args[:1] == ["devices"]:
                body = json.dumps({"pending": [], "paired": [DEV_A]}, indent=2)
                return 0, ('{"level": "info"}\n' + body + "\n").encode(), b""
            return await cli(args, timeout)
        pairing._cli = banner_first
        offer = await pairing.start()
        assert pairing._session.known == {"dev-a": 1000}
        pairing.cancel(offer)
    asyncio.run(go())


def test_cli_json_is_the_one_parser_for_cli_output():
    found = openclaw_client.cli_json(
        b'banner\n{"level": "info"}\n{\n  "paired": [\n    {\n      "deviceId": "x"\n    }\n  ]\n}\n',
        lambda d: isinstance(d.get("paired"), list))
    assert found == {"paired": [{"deviceId": "x"}]}, found
    # Nested objects are not candidates on their own.
    assert openclaw_client.cli_json(b'{\n  "a": [\n    {\n "paired": []\n }\n  ]\n}\n',
                                    lambda d: "paired" in d) is None
    assert openclaw_client.cli_json(b"no json here") is None
    # The consult reads its payload through it, banners and all.
    out = b'[plugins] loaded\n{\n  "payloads": [\n    {\n      "text": " hi "\n    }\n  ]\n}\n'
    assert openclaw_client._consult_payload_text(out) == "hi"
    assert openclaw_client._consult_payload_text(b'{"result": {"payloads": [{"text": "r"}]}}') == "r"


def test_a_get_spends_nothing_and_shows_the_two_choices():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            # A link preview, a prefetch, the phone opening the link twice.
            for _ in range(pairing.MAX_WRONG + 2):
                r = await c.get(f"/p/{offer.code}")
                assert r.status_code == 200, r.status_code
        assert "Open the dashboard here" in r.text and "Use the OpenClaw app" in r.text
        assert f'action="/p/{offer.code}"' in r.text and 'method="post"' in r.text
        assert 'href="http://teaport.local/teaport-ca.crt"' in r.text
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert BOOTSTRAP not in r.text and SETUP_CODE not in r.text
        live = pairing._session
        assert live.code_usable() and not live.taps and live.wrong == 0 and live.result is None
        assert cli.calls == [("devices", "list", "--json")]  # nothing minted, nothing polled
        # A post that chooses nothing spends nothing either.
        async with _client() as c:
            r = await c.post(f"/p/{offer.code}", data={"choice": "other"})
            assert r.status_code == 200 and "Use the OpenClaw app" in r.text
            assert (await _take(c, offer.code)).status_code == 303
        pairing.cancel(offer)
    asyncio.run(go())


def test_the_app_choice_spends_the_code_and_shows_the_setup_code_once():
    async def go():
        cli = FakeCli()
        cli.devices = [[DEV_A], [DEV_A], [DEV_A, DEV_PHONE]]
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            r = await _take(c, offer.code, "app")
            assert r.status_code == 200, r.status_code
            assert SETUP_CODE in r.text and 'id="copy"' in r.text
            assert "navigator.clipboard" in r.text and "select()" in r.text
            assert "Add Gateway" in r.text and "Paste the code" in r.text
            assert "works once" in r.text and "next few minutes" in r.text
            assert 'href="http://teaport.local/teaport-ca.crt"' in r.text
            assert r.headers["cache-control"] == "no-store"
            assert ("qr", "--json", "--limited") in cli.calls
            assert not any(call[:1] == ("dashboard",) for call in cli.calls)
            # Shown once: the code is spent, for both choices and for a plain GET.
            for again in (await c.get(f"/p/{offer.code}"), await _take(c, offer.code, "app"),
                          await _take(c, offer.code)):
                assert again.status_code == 410 and "already used" in again.text
                assert SETUP_CODE not in again.text
        # The phone's app pairs as a device of its own, named for its kind.
        assert (await pairing.wait(offer)) == pairing.Result("paired", "iPhone")
    asyncio.run(go())


def test_the_apps_node_and_operator_as_two_entries_are_one_pairing():
    async def go():
        node = {"deviceId": "dev-n", "deviceFamily": "iPhone", "approvedAtMs": 3000}
        operator = {"deviceId": "dev-o", "displayName": "Ana's iPhone", "approvedAtMs": 3001}
        cli = FakeCli()
        cli.devices = [[DEV_A], [DEV_A, node, operator]]
        _setup(cli)
        offer = await pairing.start()
        async with _client() as c:
            assert (await _take(c, offer.code, "app")).status_code == 200
        assert (await pairing.wait(offer)) == pairing.Result("paired", "Ana's iPhone")
    asyncio.run(go())


def test_a_failed_app_mint_ends_the_offer_and_says_so():
    async def go():
        for cli in (FakeCli([DEV_A], qr_rc=1, qr={"error": "no route"}),
                    FakeCli([DEV_A], qr={"setupCode": SETUP_CODE, "gatewayUrl": "ws://127.0.0.1:18789"})):
            _setup(cli)
            offer = await pairing.start()
            async with _client() as c:
                r = await _take(c, offer.code, "app")
                assert r.status_code == 503 and "could not start" in r.text, r.status_code
                assert "Ask Teaport again" in r.text and SETUP_CODE not in r.text
                assert "already used" in (await c.get(f"/p/{offer.code}")).text
            assert (await pairing.wait(offer)) == pairing.Result("failed", None)
            assert pairing._session is None
            # The screen came down, so a new offer can be made at once.
            pairing.cancel(await pairing.start())
    asyncio.run(go())


def test_wrong_codes_count_the_same_on_get_and_post():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        wrong = "".join(c for c in pairing.CODE_ALPHABET if c not in offer.code)[:6]
        async with _client() as c:
            for i in range(pairing.MAX_WRONG - 1):
                r = await (c.get(f"/p/{wrong}") if i % 2 else _take(c, wrong, "app"))
                assert r.status_code == 410
            assert pairing._session.wrong == pairing.MAX_WRONG - 1
            assert (await c.get(f"/p/{offer.code}")).status_code == 200  # not a guess
            assert pairing._session.wrong == pairing.MAX_WRONG - 1
            assert (await _take(c, wrong)).status_code == 410
        assert (await pairing.wait(offer)) == pairing.Result("locked", None)
    asyncio.run(go())


def test_a_post_answers_only_through_the_front_door():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        async with _client(peer="192.168.1.9", proto="https") as lan, \
                _client(proto=None) as plain:
            for c in (lan, plain):
                for choice in ("dashboard", "app"):
                    assert (await _take(c, offer.code, choice)).status_code == 404
                for _ in range(pairing.MAX_WRONG + 1):
                    assert (await _take(c, "WRONG2")).status_code == 404
        live = pairing._session
        assert live.code_usable() and not live.taps and live.wrong == 0 and live.result is None
        assert cli.calls == [("devices", "list", "--json")]
        pairing.cancel(offer)
    asyncio.run(go())


def test_a_double_tap_finds_the_code_spent_while_the_first_mints():
    async def go():
        cli = FakeCli([DEV_A])
        slow = asyncio.Event()

        async def slow_cli(args, timeout):
            if args[:1] == ["qr"]:
                await slow.wait()
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = slow_cli
        offer = await pairing.start()
        async with _client() as c:
            first = asyncio.ensure_future(_take(c, offer.code, "app"))
            await asyncio.sleep(0.05)
            second = await _take(c, offer.code, "dashboard")
            assert second.status_code == 410 and "already used" in second.text
            slow.set()
            assert (await first).status_code == 200
        assert [call[:1] for call in cli.calls].count(("qr",)) == 1
        assert not any(call[:1] == ("dashboard",) for call in cli.calls)
        pairing.cancel(offer)
        await pairing.wait(offer)
    asyncio.run(go())


def test_the_app_is_watched_longer_than_the_dashboard():
    async def go():
        for choice, window in (("dashboard", 0.2), ("app", 1.0)):
            cli = FakeCli([DEV_A])
            _setup(cli)
            pairing.OPENED_WAIT_SECS, pairing.APP_WAIT_SECS = 0.2, 1.0
            offer = await pairing.start()
            async with _client() as c:
                await _take(c, offer.code, choice)
            t0 = time.monotonic()
            assert (await pairing.wait(offer)) == pairing.Result("opened", None)
            took = time.monotonic() - t0
            assert window - 0.05 <= took < window + 0.5, (choice, took)
    asyncio.run(go())


def test_spent_says_when_the_code_was_used_and_ends_with_the_offer():
    async def go():
        _setup(FakeCli([DEV_A]))
        offer = await pairing.start()
        used = asyncio.ensure_future(pairing.spent(offer))
        await asyncio.sleep(0.02)
        assert not used.done()  # a GET spends nothing
        async with _client() as c:
            await c.get(f"/p/{offer.code}")
            await asyncio.sleep(0.02)
            assert not used.done()
            await _take(c, offer.code, "app")
        assert (await asyncio.wait_for(used, 1)) is True
        await pairing.wait(offer)
        # An offer that ends unused: False, and nothing is left waiting.
        offer = await pairing.start()
        unused = asyncio.ensure_future(pairing.spent(offer))
        await asyncio.sleep(0.02)
        pairing.cancel(offer)
        assert (await asyncio.wait_for(unused, 1)) is False
        await pairing.wait(offer)
        assert (await pairing.spent(offer)) is False
        # A tap is "spent" from the tap (the phone is busy with it while it mints); its
        # failure then ends the pairing, and the screen comes down.
        _setup(FakeCli([DEV_A], qr_rc=1, qr={}))
        offer = await pairing.start()
        failed = asyncio.ensure_future(pairing.spent(offer))
        async with _client() as c:
            await _take(c, offer.code, "app")
        assert (await asyncio.wait_for(failed, 1)) is True
        assert (await pairing.wait(offer)) == pairing.Result("failed")
    asyncio.run(go())


def test_a_device_from_an_earlier_code_ends_the_pairing_paired():
    """The review's late_device: the app chosen, the user asks again, then pastes the
    first setup code after all. The same pairing was watching: "paired", though the new
    code was never used."""
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.APP_WAIT_SECS = 5
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            assert (await _take(c, first.code, "app")).status_code == 200
        await asyncio.sleep(0.05)
        second = await pairing.start()
        new_code_spent = asyncio.ensure_future(pairing.spent(second))
        await asyncio.sleep(0.05)
        assert cli.calls.count(("devices", "list", "--json")) >= 2  # still watching
        cli.devices = [[DEV_A, DEV_PHONE]]  # the first code's app pairs
        assert (await asyncio.wait_for(waiter, 2)) == pairing.Result("paired", "iPhone")
        assert (await pairing.wait(second)) == pairing.Result("paired", "iPhone")
        assert (await new_code_spent) is False  # never used; the pairing ended
        assert not any(c[:1] == ("dashboard",) for c in cli.calls)
    asyncio.run(go())


def test_a_watch_outlasting_the_code_ends_opened_and_spent_says_finish():
    """The review's watch_outlasts_expiry: the current code runs out while a handoff's
    watch still runs. The screen is told "finish on your device", the pairing ends
    "opened" (never "nothing was paired"), and the dead code is not used again."""
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.APP_WAIT_SECS = 0.6
        first = await pairing.start()
        async with _client() as c:
            await _take(c, first.code, "app")
        pairing.OFFER_SECS = 0.2
        try:
            second = await pairing.start()
        finally:
            pairing.OFFER_SECS = 300.0
        waiter = asyncio.ensure_future(pairing.wait(second))
        finish = asyncio.ensure_future(pairing.spent(second))
        assert (await asyncio.wait_for(finish, 1)) is True  # ran out, the watch goes on
        assert not waiter.done()
        async with _client() as c:
            r = await c.get(f"/p/{second.code}")
            assert r.status_code == 410 and "expired" in r.text
        assert (await asyncio.wait_for(waiter, 2)) == pairing.Result("opened", None)
    asyncio.run(go())


def test_a_failed_tap_drops_its_claim_and_the_pairing_ends_as_the_rest_decides():
    """A tap of an earlier code fails while a newer code is up: the pairing goes on (a
    code can still be used), stops waiting on that tap, and with nothing else ends
    "expired". "failed" only when the failure leaves nothing to wait for."""
    async def go():
        cli = FakeCli([DEV_A])
        gate = asyncio.Event()

        async def slow_fail(args, timeout):
            if args[:1] == ["qr"]:
                await gate.wait()
                return 1, b"", b"no route"
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = slow_fail
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            tap = asyncio.ensure_future(_take(c, first.code, "app"))
            await asyncio.sleep(0.05)
            pairing.OFFER_SECS = 0.4
            try:
                second = await pairing.start()
            finally:
                pairing.OFFER_SECS = 300.0
            gate.set()
            r = await asyncio.wait_for(tap, 1)
            assert r.status_code == 503 and "could not start" in r.text
        s = pairing._session
        assert s.result is None and not s.taps and not s.live_claim()
        before = cli.calls.count(("devices", "list", "--json"))
        assert (await asyncio.wait_for(waiter, 2)) == pairing.Result("expired", None)
        assert cli.calls.count(("devices", "list", "--json")) == before  # nothing polled
        assert second.session == first.session
    asyncio.run(go())


def test_no_poll_beside_a_mint_and_cancel_stops_an_inflight_tap():
    """The review's overlap_and_close: a tap minting while a handoff is watched -- no
    poll runs beside the mint CLI; then cancel() (close()): the pairing ends
    "cancelled", and the minting phone gets "expired", not a 303."""
    async def go():
        cli = FakeCli([DEV_A])
        log, gate, dashboards = [], asyncio.Event(), []

        async def logged(args, timeout):
            log.append(("start", args[0]))
            if args[:1] == ["dashboard"]:
                dashboards.append(1)
                if len(dashboards) == 2:
                    await gate.wait()
            out = await cli(args, timeout)
            log.append(("end", args[0]))
            return out
        _setup(cli)
        pairing._cli = logged
        pairing.OPENED_WAIT_SECS = 5
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            assert (await _take(c, first.code)).status_code == 303  # watched from here
            await asyncio.sleep(0.05)
            second = await pairing.start()
            tap = asyncio.ensure_future(_take(c, second.code))  # mints, held
            await asyncio.sleep(0.05)
            mark = len(log)
            await asyncio.sleep(0.2)
            assert ("start", "devices") not in log[mark:], log[mark:]
            pairing.cancel(second)
            assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("cancelled", None)
            gate.set()
            r = await asyncio.wait_for(tap, 1)
            assert r.status_code == 410 and "expired" in r.text, r.status_code
            assert "location" not in r.headers
    asyncio.run(go())


def test_asking_again_during_a_tap_keeps_the_tapped_phones_handoff():
    """Re-asked while the first code's handoff is being minted: the phone that tapped in
    time still gets its setup code, and the new offer catches its device."""
    async def go():
        cli = FakeCli([DEV_A])
        gate = asyncio.Event()

        async def slow(args, timeout):
            if args[:1] == ["qr"]:
                await gate.wait()
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = slow
        pairing.APP_WAIT_SECS = 5
        first = await pairing.start()
        async with _client() as c:
            tap = asyncio.ensure_future(_take(c, first.code, "app"))
            await asyncio.sleep(0.05)
            second = await pairing.start()  # asked again while it mints
            w2 = asyncio.ensure_future(pairing.wait(second))
            await asyncio.sleep(0.05)
            assert not tap.done()
            gate.set()
            r = await asyncio.wait_for(tap, 1)
            assert r.status_code == 200 and SETUP_CODE in r.text, r.status_code
        cli.devices = [[DEV_A, DEV_PHONE]]
        assert (await asyncio.wait_for(w2, 2)) == pairing.Result("paired", "iPhone")
        # And the dashboard: its redirect is kept the same way.
        cli2 = FakeCli([DEV_A])
        gate2 = asyncio.Event()

        async def slow2(args, timeout):
            if args[:1] == ["dashboard"]:
                await gate2.wait()
            return await cli2(args, timeout)
        _setup(cli2)
        pairing._cli = slow2
        first = await pairing.start()
        async with _client() as c:
            tap = asyncio.ensure_future(_take(c, first.code))
            await asyncio.sleep(0.05)
            second = await pairing.start()
            gate2.set()
            r = await asyncio.wait_for(tap, 1)
            assert r.status_code == 303 and "#bootstrapToken=" in r.headers["location"]
        pairing.cancel(second)
    asyncio.run(go())


def test_a_confirming_offer_is_still_stopped_by_cancel():
    async def go():
        _setup(FakeCli([DEV_A]))
        pairing.APP_WAIT_SECS = 30
        offer = await pairing.start()
        async with _client() as c:
            await _take(c, offer.code, "app")
        waiter = asyncio.ensure_future(pairing.wait(offer))
        await asyncio.sleep(0.05)
        assert pairing.withdraw(offer) is True and not waiter.done()  # spent: kept
        pairing.cancel(offer)  # pair_device's close()
        assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("cancelled", None)
        assert pairing._session is None
    asyncio.run(go())


def test_any_failure_after_the_code_is_spent_ends_the_offer():
    async def go():
        # The review's wedge: a gatewayUrl urlsplit() cannot parse.
        with _Logs() as logs:
            _setup(FakeCli([DEV_A], qr={"setupCode": SETUP_CODE, "gatewayUrl": "wss://[fe80::1",
                                        "access": "limited"}))
            offer = await pairing.start()
            waiter = asyncio.ensure_future(pairing.wait(offer))
            async with _client() as c:
                r = await _take(c, offer.code, "app")
            assert r.status_code == 503 and "could not start" in r.text and SETUP_CODE not in r.text
            assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("failed", None)
            assert SETUP_CODE not in logs.text
        # Anything else the mint raises, by its type only: its message can hold the handoff.
        for boom in (RuntimeError(BROWSER_URL), ValueError(SETUP_CODE)):
            with _Logs() as logs:
                cli = FakeCli([DEV_A])

                async def raising(args, timeout, boom=boom, cli=cli):
                    if args[:1] in (["dashboard"], ["qr"]):
                        raise boom
                    return await cli(args, timeout)
                _setup(cli)
                pairing._cli = raising
                offer = await pairing.start()
                async with _client() as c:
                    r = await _take(c, offer.code, "dashboard")
                assert r.status_code == 503, r.status_code
                assert (await asyncio.wait_for(pairing.wait(offer), 1)) == pairing.Result("failed")
                assert type(boom).__name__ in logs.text
                assert BOOTSTRAP not in logs.text and SETUP_CODE not in logs.text
                pairing.cancel(await pairing.start())  # not wedged: a new offer is made
        # The request cancelled while it mints (the phone went away).
        cli = FakeCli([DEV_A])
        gate = asyncio.Event()

        async def hanging(args, timeout):
            if args[:1] == ["qr"]:
                await gate.wait()
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = hanging
        offer = await pairing.start()
        spent = asyncio.ensure_future(pairing.spent(offer))
        live = pairing._session
        waiter = asyncio.ensure_future(pairing.wait(offer))

        class _Req:
            url = type("U", (), {"scheme": "https"})()
            client = None
            headers = {"content-length": "12"}

            async def stream(self):
                yield b"choice=app"
        handler = asyncio.ensure_future(pairing.redeem(offer.code, _Req()))
        await asyncio.sleep(0.05)
        assert live.taps and not handler.done()
        assert (await asyncio.wait_for(spent, 1)) is True  # "finish on your device"
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        assert live.result == pairing.Result("failed") and not live.taps
        assert (await asyncio.wait_for(waiter, 1)) == pairing.Result("failed")
    asyncio.run(go())


def test_a_big_post_is_refused_unread_and_spends_nothing():
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        offer = await pairing.start()
        wrong = "".join(c for c in pairing.CODE_ALPHABET if c not in offer.code)[:6]
        big = "choice=app&pad=" + "x" * 2000
        async with _client() as c:
            for code in (offer.code, wrong):
                r = await c.post(f"/p/{code}", content=big.encode(),
                                 headers={"content-type": "application/x-www-form-urlencoded"})
                assert r.status_code == 413, r.status_code

            async def chunks():  # no Content-Length: read until past the cap
                for _ in range(10):
                    yield b"choice=app&pad=" + b"x" * 300
            r = await c.post(f"/p/{offer.code}", content=chunks())
            assert r.status_code == 413, r.status_code
            live = pairing._session
            assert live.code_usable() and not live.taps and live.wrong == 0 and live.result is None
            assert not any(call[:1] == ("qr",) for call in cli.calls)
            assert (await _take(c, offer.code, "app")).status_code == 200  # still usable
        pairing.cancel(offer)
    asyncio.run(go())


class _CountingCli(FakeCli):
    """FakeCli that counts CLIs running at once, each taking `delay` seconds."""

    def __init__(self, *a, delay=0.05, **k):
        super().__init__(*a, **k)
        self.delay, self.active, self.most = delay, 0, 0

    async def __call__(self, args, timeout):
        self.active += 1
        self.most = max(self.most, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().__call__(args, timeout)
        finally:
            self.active -= 1


def test_never_two_clis_at_once_a_poll_gives_way_to_a_tap():
    """The review's r1: a tap made while a poll runs. The poll is cut short (its CLI
    killed) and the mint runs after it, never beside it; the poll's wait wakes on the
    tap too."""
    async def go():
        cli = _CountingCli([DEV_A], delay=0.2)
        _setup(cli)
        pairing.POLL_SECS, pairing.OPENED_WAIT_SECS = 0.05, 5
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            assert (await _take(c, first.code)).status_code == 303
            await asyncio.sleep(0.05)  # wait() is inside a 0.2 s poll now
            second = await pairing.start()
            t0 = time.monotonic()
            assert (await _take(c, second.code, "app")).status_code == 200
            assert time.monotonic() - t0 < 0.6  # the poll gave way, not waited out
        pairing.cancel(second)
        await waiter
        assert cli.most == 1, cli.most
    asyncio.run(go())


def test_a_tap_still_minting_when_the_pairing_ends_paired_gets_no_handoff():
    async def go():
        cli = FakeCli([DEV_A])
        gate = asyncio.Event()
        mint_cancelled = []

        async def held(args, timeout):
            if args[:1] == ["qr"]:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    mint_cancelled.append(True)
                    raise
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = held
        pairing.OPENED_WAIT_SECS = 5
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            assert (await _take(c, first.code)).status_code == 303
            second = await pairing.start()
            tap = asyncio.ensure_future(_take(c, second.code, "app"))
            await asyncio.sleep(0.05)
            # The first tap's browser pairs; the pairing ends while the second mints.
            pairing._end(pairing._session, pairing.Result("paired", "Chrome on Pixel"))
            r = await asyncio.wait_for(tap, 1)
        assert r.status_code == 410 and SETUP_CODE not in r.text
        assert mint_cancelled == [True]  # its CLI was stopped, not run out
        assert (await waiter) == pairing.Result("paired", "Chrome on Pixel")
    asyncio.run(go())


def test_the_users_stop_stops_minting_taps_with_or_without_a_handoff():
    """The review's r2: cancel (withdraw) while a tap mints. No handoff after the stop,
    either way; a handoff already made goes on being watched."""
    async def go():
        for earlier_handoff in (False, True):
            cli = FakeCli([DEV_A])
            gate = asyncio.Event()

            async def held(args, timeout, cli=cli, gate=gate):
                if args[:1] == ["qr"]:
                    await gate.wait()
                return await cli(args, timeout)
            _setup(cli)
            pairing._cli = held
            pairing.OPENED_WAIT_SECS = 5
            offer = await pairing.start()
            waiter = asyncio.ensure_future(pairing.wait(offer))
            async with _client() as c:
                if earlier_handoff:
                    assert (await _take(c, offer.code)).status_code == 303
                    offer = await pairing.start()
                tap = asyncio.ensure_future(_take(c, offer.code, "app"))
                await asyncio.sleep(0.05)
                kept = pairing.withdraw(offer)
                gate.set()
                r = await asyncio.wait_for(tap, 1)
            assert r.status_code == 410 and SETUP_CODE not in r.text, earlier_handoff
            assert kept is earlier_handoff
            if earlier_handoff:
                assert not waiter.done()  # the dashboard's device is still looked for
                cli.devices = [[DEV_A, DEV_NEW]]
                assert (await asyncio.wait_for(waiter, 2)) == pairing.Result(
                    "paired", "Chrome on Pixel")
            else:
                assert (await waiter) == pairing.Result("cancelled", None)
    asyncio.run(go())


def test_locked_is_per_code_and_an_old_locked_code_is_no_guess():
    """The review's r3: a code locked by wrong guesses, then asked again: the next code
    running out unused is "expired", not "locked"; the locked code scanned again counts
    nothing against the new one. (A tap minting keeps the pairing going meanwhile.)"""
    async def go():
        cli = FakeCli([DEV_A])
        gate = asyncio.Event()

        async def held_fail(args, timeout):
            if args[:1] == ["qr"]:
                await gate.wait()
                return 1, b"", b"no route"
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = held_fail
        first = await pairing.start()
        waiter = asyncio.ensure_future(pairing.wait(first))
        async with _client() as c:
            tap = asyncio.ensure_future(_take(c, first.code, "app"))  # keeps it going
            await asyncio.sleep(0.05)
            second = await pairing.start()
            wrong = "".join(x for x in pairing.CODE_ALPHABET
                            if x not in first.code + second.code)[:6]
            for _ in range(pairing.MAX_WRONG):
                await c.get(f"/p/{wrong}")
            s = pairing._session
            assert s.locked and not s.code_usable()
            pairing.OFFER_SECS = 0.3
            try:
                third = await pairing.start()
            finally:
                pairing.OFFER_SECS = 300.0
            assert not s.locked and s.code_usable() and s.wrong == 0
            for _ in range(pairing.MAX_WRONG + 1):  # the locked code, scanned again
                assert (await c.get(f"/p/{second.code}")).status_code == 410
            assert s.wrong == 0 and s.code_usable()
            gate.set()
            assert (await asyncio.wait_for(tap, 1)).status_code == 503
        assert (await asyncio.wait_for(waiter, 2)) == pairing.Result("expired", None)
        assert third.session == first.session
    asyncio.run(go())


def test_a_pairing_nobody_follows_is_not_lent_to_the_next_ask():
    """The review's r4, pairing's side: a pairing left with nothing to wait for and no
    waiter is ended by the next start(), which opens a fresh one (a fresh snapshot);
    one past SESSION_MAX_SECS too, whatever it still had."""
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.OFFER_SECS = 0.05
        try:
            stale = await pairing.start()
        finally:
            pairing.OFFER_SECS = 300.0
        await asyncio.sleep(0.1)  # its code ran out, nobody waited
        cli.devices = [[DEV_A, DEV_PHONE]]  # paired meanwhile, some other way
        fresh = await pairing.start()
        assert fresh.session != stale.session
        assert pairing._session.known == {"dev-a": 1000, "dev-p": 3000}
        assert (await pairing.wait(stale)) == pairing.Result("expired", None)
        # Past the bound: a fresh pairing even with a usable code.
        old_max = pairing.SESSION_MAX_SECS
        pairing.SESSION_MAX_SECS = 0.05
        try:
            await asyncio.sleep(0.1)
            newest = await pairing.start()
        finally:
            pairing.SESSION_MAX_SECS = old_max
        assert newest.session != fresh.session
        pairing.cancel(newest)
    asyncio.run(go())


def test_the_bound_spares_a_pairing_with_work_in_progress():
    """Past SESSION_MAX_SECS, a pairing still watching a handoff (or minting) gets a new
    code as usual, its snapshot kept; one with nothing in progress is replaced."""
    async def go():
        cli = FakeCli([DEV_A])
        _setup(cli)
        pairing.OPENED_WAIT_SECS = 5
        first = await pairing.start()
        async with _client() as c:
            assert (await _take(c, first.code)).status_code == 303
        old_max = pairing.SESSION_MAX_SECS
        pairing.SESSION_MAX_SECS = 0.05
        try:
            await asyncio.sleep(0.1)
            again = await pairing.start()
            assert again.session == first.session  # the watch goes on
            assert cli.calls.count(("devices", "list", "--json")) == 1
            cli.devices = [[DEV_A, DEV_NEW]]
            assert (await asyncio.wait_for(pairing.wait(again), 2)) == pairing.Result(
                "paired", "Chrome on Pixel")
        finally:
            pairing.SESSION_MAX_SECS = old_max
    asyncio.run(go())


def test_a_code_from_an_earlier_pairing_is_no_guess():
    async def go():
        _setup(FakeCli([DEV_A]))
        old = await pairing.start()
        pairing.cancel(old)
        await pairing.wait(old)
        new = await pairing.start()
        assert new.session != old.session
        async with _client() as c:
            for _ in range(pairing.MAX_WRONG + 2):  # a photo of the old QR
                assert (await c.get(f"/p/{old.code}")).status_code == 410
        s = pairing._session
        assert s.wrong == 0 and s.code_usable() and not s.locked
        pairing.cancel(new)
    asyncio.run(go())


def test_cancel_kills_a_minting_cli():
    async def go():
        cli = FakeCli([DEV_A])
        cancelled = []

        async def hangs(args, timeout):
            if args[:1] == ["dashboard"]:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            return await cli(args, timeout)
        _setup(cli)
        pairing._cli = hangs
        offer = await pairing.start()
        async with _client() as c:
            tap = asyncio.ensure_future(_take(c, offer.code))
            await asyncio.sleep(0.05)
            pairing.cancel(offer)  # close()
            r = await asyncio.wait_for(tap, 1)
        assert r.status_code == 410 and "location" not in r.headers
        assert cancelled == [True]
    asyncio.run(go())


def test_polls_are_spaced_and_bounded():
    # Each poll is a cold OpenClaw CLI: ~5 s apart, for ~2 minutes after the link opens.
    assert DEFAULT_POLL_SECS >= 5.0 and DEFAULT_OPENED_WAIT_SECS <= 120.0
    assert DEFAULT_OPENED_WAIT_SECS / DEFAULT_POLL_SECS <= 24
    # The app's paste takes longer: five minutes, at most 60 cold CLI runs, one at a time.
    assert DEFAULT_APP_WAIT_SECS == 300.0 and DEFAULT_APP_WAIT_SECS / DEFAULT_POLL_SECS <= 60


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
