#
# "Connect your OpenRouter account" (#282): OpenRouter's sign-in (OAuth PKCE) from the
# owner's Settings, back through Teaspoon's static page (web/connect/index.html), or,
# without that page, with the code OpenRouter shows pasted into Settings.
#
# What has to hold:
#   - The button is offered on the Settings site, for an OpenRouter endpoint, unless the
#     key is set where Settings cannot change it. The static page decides only the way
#     back: with it, a callback and a signed state; without it (not hosted, not HTTPS, a
#     garbled answer), no callback and no state, and the code is pasted. Asking for the
#     page never takes the AI account step down, and is one ask at a time.
#   - Start (the owner's session) gives OpenRouter's sign-in address with an S256
#     challenge of a verifier only the box holds, and a fresh session cookie.
#   - /settings/openrouter: a GET (no cookie: SameSite=Strict) shows a Finish button and
#     spends nothing; the page's referrer policy is same-origin, as a form under
#     no-referrer posts `Origin: null`, which is refused. Its POST needs the session and
#     the Settings origin, a state this box signed for its own origin and the live
#     nonce; it is spent there, once; the code and verifier are exchanged; the key is
#     saved as a pasted one is.
#   - A refused sign-in, a refused key, a late or replayed finish, a forged state: a plain
#     message, nothing saved, paste still there, no developer text. A check with no
#     answer does not stop the key OpenRouter just gave. A key OpenClaw did not take is
#     held for "Save it again", in memory, for a while.
#   - A pasted code: one that is no code spends nothing; with no sign-in waiting, say so.
#   - The end of Wi-Fi setup offers it through "open my settings"' own code, where that
#     is usable and the box has no key of the owner's.
#   - ai_account._request: a garbled answer is OSError; a dripping server is cut at the
#     deadline. exchange_code: the documented request, plain reasons.
#   - No key, code or verifier in the journal; every ...token=, code= and state= out of
#     the access log.
#   - The static page: current with its catalogs (13 languages) and its CSP hashes, no
#     third-party anything, the box's path, and it goes on only to a home address.
#
# No test talks to OpenRouter or to the static page's host: openrouter_connect._fetch,
# openrouter_connect.exchange, ai_account._post and config_ui.check_key are fakes, and
# every key, code and account here is made up.
#
# Run: python test_openrouter_connect.py   (or via pytest)
#
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import httpx
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_settings_access as sa  # noqa: E402  (its pinned-pipecat check runs first)

import appliance  # noqa: E402
from loguru import logger  # noqa: E402
from pipecat.frames.frames import TTSSpeakFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from tempdirs import tempdir  # noqa: E402
from teaport_brain import (  # noqa: E402
    ai_account, config_ui, gateway_server, i18n, openrouter_connect, settings_access, wifi_setup,
    wifi_voice,
)

FAKE_KEY = "sk-or-v1-" + "7" * 64          # what the made-up exchange hands back
FAKE_CODE = "c0de-" + "a1" * 12             # a made-up sign-in code
PAGE = (b'<!doctype html><html><head>' + openrouter_connect.PAGE_MARK
        + b'</head><body></body></html>')
EN = i18n.get("en")


def _setup(page=(200, PAGE)):
    """test_settings_access's box (front door, Settings site, OpenRouter endpoint, fake
    key check), with the static page answering `page` and a fake exchange."""
    home, check, checks = sa._setup()
    fetched = []

    def fetch(url):
        fetched.append(url)
        if isinstance(page, BaseException):
            raise page
        return page

    openrouter_connect._fetch = fetch
    openrouter_connect._probe = None
    openrouter_connect._pending = None
    openrouter_connect._held = None
    openrouter_connect._clock = lambda: 5000.0
    ai_account.save_key = _REAL_SAVE
    exchanged = []

    def exchange(code, verifier, timeout=None):
        exchanged.append((code, verifier))
        return exchange.answer

    exchange.answer = (FAKE_KEY, "")
    openrouter_connect.exchange = exchange
    return home, check, checks, fetched, exchanged


_REAL_SAVE = ai_account.save_key


def _client(cookie=None, origin=sa.ORIGIN, fetch_site=None, proto="https", host=f"{sa.HOST}:{sa.PORT}"):
    """Through the Settings site as Caddy is one, with this module's routes mounted as
    gateway_server mounts them: before Settings' /settings/<code>."""
    app = FastAPI()
    app.include_router(config_ui.router)
    app.include_router(config_ui.router, prefix="/settings")
    app.include_router(openrouter_connect.router)
    app.include_router(settings_access.router)
    app.add_middleware(settings_access.SettingsGuard)
    headers = {"Host": host}
    if proto:
        headers["X-Forwarded-Proto"] = proto
    if origin:
        headers["Origin"] = origin
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site
    cookies = {settings_access.COOKIE: cookie} if cookie else None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
                             base_url="http://box", headers=headers, cookies=cookies)


def _session() -> str:
    return settings_access.session_cookie()


def _key_file(home) -> str | None:
    try:
        with open(os.path.join(home, "llm_key")) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


async def _start(c) -> dict[str, str]:
    r = await c.post("/settings/api/openrouter/start")
    assert r.status_code == 200, r.text
    url = urllib.parse.urlsplit(r.json()["url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "openrouter.ai", "/auth")
    return dict(urllib.parse.parse_qsl(url.query))


def _esc(text: str) -> str:
    return text.replace("'", "&#x27;").replace('"', "&quot;")


# ------------------------------------------------------------------ the round trip

def test_the_owner_connects_through_the_page_and_the_key_is_saved_as_a_pasted_one():
    async def run():
        home, check, checks, fetched, exchanged = _setup()
        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
        try:
            async with _client(cookie=_session()) as c:
                account = (await c.get("/settings/api/account")).json()
                assert account["connect"] == {"offered": True, "page": True, "pending": False,
                                              "retry": False}
                assert account["text"]["connect"] == "Connect your OpenRouter account"
                r = await c.post("/settings/api/openrouter/start")
                cookie = r.headers["set-cookie"].lower()
                for part in ("httponly", "secure", "samesite=strict", "path=/settings"):
                    assert part in cookie, cookie  # a fresh hour for the round trip
                assert r.headers["cache-control"] == "no-store" and r.json()["page"] is True
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.json()["url"]).query))
            pending = openrouter_connect._pending
            assert q["callback_url"] == "https://get.teaspoon.tech/connect/index.html"
            assert q["code_challenge_method"] == "S256" and q["key_label"] == "Teaport"
            want = base64.urlsafe_b64encode(hashlib.sha256(pending.verifier.encode()).digest())
            assert q["code_challenge"] == want.rstrip(b"=").decode()
            assert 43 <= len(pending.verifier) <= 128 and pending.verifier not in r.text
            head, nonce, _sig = q["state"].split(".")
            assert openrouter_connect._unb64(head).decode() == sa.ORIGIN and nonce == pending.nonce
            # The static page sends the phone here: another site's navigation, so no cookie.
            landing = f"/settings/openrouter?code={FAKE_CODE}&state={urllib.parse.quote(q['state'])}"
            async with _client(origin=None, fetch_site="cross-site") as c:
                for _ in range(2):  # a GET spends nothing
                    got = await c.get(landing)
                    assert got.status_code == 200 and "Finish" in got.text, got.text
                    assert got.headers["cache-control"] == "no-store"
                    # The form must post with the Settings origin, not `Origin: null`.
                    assert got.headers["referrer-policy"] == "same-origin"
                    assert '<meta name="referrer" content="same-origin">' in got.text
                    assert "no-referrer" not in got.text
            assert openrouter_connect._pending is pending and exchanged == []
            # Its button: the Settings page's own POST, with the session.
            async with _client(cookie=_session(), fetch_site="same-origin") as c:
                done = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert done.status_code == 200, done.text
                assert "Your OpenRouter account is connected" in done.text
                assert exchanged == [(FAKE_CODE, pending.verifier)]
                assert checks[-1] == (FAKE_KEY, "https://openrouter.ai/api/v1")
                # A key just given: a 401 is asked again; the account's credit is read.
                assert check.asked == {"issued": True, "account": True}, check.asked
                assert _key_file(home) == FAKE_KEY
                # Once: the same finish again is refused, and nothing more is exchanged.
                again = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert again.status_code == 410 and "expired" in again.text
                assert len(exchanged) == 1
                account = (await c.get("/settings/api/account")).json()
                assert account["key_set"] is True and account["connect"]["pending"] is False
            assert len(fetched) == 1  # the static page was asked once, then remembered
        finally:
            logger.remove(sink)
        journal = "\n".join(lines)
        for secret in (FAKE_KEY, FAKE_CODE, pending.verifier):
            assert secret not in journal
    asyncio.run(run())


def test_without_the_page_the_code_openrouter_shows_is_pasted_into_settings():
    async def run():
        for page in ((404, b"Not Found"), (200, b"<html>something else</html>"),
                     TimeoutError("no answer")):
            home, _c, _cs, fetched, exchanged = _setup(page=page)
            async with _client(cookie=_session()) as c:
                account = (await c.get("/settings/api/account")).json()
                assert account["connect"] == {"offered": True, "page": False, "pending": False,
                                              "retry": False}, page
                assert "OpenRouter then shows a code" in account["text"]["connect_note_code"]
                r = await c.post("/settings/api/openrouter/start")
                assert r.status_code == 200 and r.json()["page"] is False
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.json()["url"]).query))
                # OpenRouter's no-callback mode: no callback_url and no state (it is not
                # returned there); the challenge is required and is there.
                assert set(q) == {"code_challenge", "code_challenge_method", "key_label"}, q
                verifier = openrouter_connect._pending.verifier
                assert (await c.get("/settings/api/account")).json()["connect"]["pending"] is True
                done = await c.post("/settings/api/openrouter/code", json={"code": f" {FAKE_CODE} "})
                assert done.status_code == 200 and done.json()["ok"] is True, done.text
                assert done.json()["pending"] is False
                assert exchanged == [(FAKE_CODE, verifier)] and _key_file(home) == FAKE_KEY
                # Asked again only after a while.
                await c.get("/settings/api/account")
                assert len(fetched) == 1, page
                openrouter_connect._clock = lambda: 5000.0 + openrouter_connect.PROBE_FAILED_SECS + 1
                await c.get("/settings/api/account")
                assert len(fetched) == 2, page
        # Not an https:// address: not asked at all; the sign-in still works, with a code.
        _home, _c, _cs, fetched, _ex = _setup()
        os.environ["TEAPORT_OPENROUTER_CONNECT_URL"] = "http://example.invalid/"
        try:
            async with _client(cookie=_session()) as c:
                connect = (await c.get("/settings/api/account")).json()["connect"]
                assert connect["offered"] is True and connect["page"] is False
            assert fetched == []
        finally:
            del os.environ["TEAPORT_OPENROUTER_CONNECT_URL"]
    asyncio.run(run())


def test_the_account_step_says_when_the_owners_account_ran_out_of_credit():
    """Answers refused for credit on the owner's own key (not the trial credit): the step
    shows that the account ran out, and where credit is added, as an error -- in the
    reader's language -- until the provider accepts a request again or another key is
    saved. A refused key is not "out of credit"."""
    from pipecat.frames.frames import ErrorFrame
    from pipecat.utils.errors import ErrorCategory
    from teaport_brain import credit, llm_error_speaker
    from test_llm_account_error import CREDIT_CASES, SPENT_KEY_403

    async def run():
        home, *_ = _setup()
        with open(os.path.join(home, "llm_key"), "w") as f:
            f.write("sk-or-v1-" + "3" * 64)            # the owner's: no starter marker
        assert credit.MONITOR.state() == credit.OWN
        real = llm_error_speaker.ANSWERS
        health = llm_error_speaker.ANSWERS = llm_error_speaker.AnswerHealth()
        try:
            async with _client(cookie=_session()) as c:
                assert (await c.get("/settings/api/account")).json()["credit"]["line"] is None
                health.failed(ErrorFrame(error="x", exception=SPENT_KEY_403(),
                                         category=ErrorCategory.AUTHORIZATION))
                assert (await c.get("/settings/api/account")).json()["credit"]["line"] is None
                health.failed(ErrorFrame(error="x", exception=CREDIT_CASES["402 in flight"](),
                                         category=ErrorCategory.QUOTA))
                shown = (await c.get("/settings/api/account")).json()["credit"]
                assert shown["out"] is True, shown
                assert shown["line"] == EN._(ai_account.RAN_OUT_OF_CREDIT), shown
                assert "openrouter.ai/settings/credits" in shown["line"]
                es = (await c.get("/settings/api/account",
                                  headers={"Accept-Language": "es"})).json()["credit"]
                assert es["line"] == i18n.get("es")._(ai_account.RAN_OUT_OF_CREDIT) != shown["line"]
                # The owner saves another key: the page no longer says it ran out (its
                # first request will tell), though no request has been accepted yet.
                with open(os.path.join(home, "llm_key"), "w") as f:
                    f.write("sk-or-v1-" + "4" * 64)
                shown = (await c.get("/settings/api/account")).json()["credit"]
                assert shown["line"] is None and "out" not in shown, shown
                with open(os.path.join(home, "llm_key"), "w") as f:
                    f.write("sk-or-v1-" + "3" * 64)            # the refused one again
                assert (await c.get("/settings/api/account")).json()["credit"]["out"] is True
                health.succeeded()
                shown = (await c.get("/settings/api/account")).json()["credit"]
                assert shown["line"] is None and "out" not in shown, shown
        finally:
            llm_error_speaker.ANSWERS = real
    asyncio.run(run())


def test_where_a_key_from_the_sign_in_could_not_be_saved_it_is_not_offered():
    async def run():
        _home, _c, _cs, fetched, _ex = _setup()
        # The operator's /config (the token, the brain's own port): not offered, nothing asked.
        async with _client(origin=None, proto=None, host="127.0.0.1:7861") as c:
            got = await c.get("/api/account", headers={"authorization": f"Bearer {sa.TOKEN}"})
            assert got.status_code == 200 and got.json()["connect"]["offered"] is False
        assert fetched == []
        # A key set elsewhere (not brain.env): the paste says so; no button, no start.
        os.environ["LLM_API_KEY"] = "sk-or-v1-" + "5" * 64
        try:
            async with _client(cookie=_session()) as c:
                account = (await c.get("/settings/api/account")).json()
                assert account["offered"] is True and account["connect"]["offered"] is False
                assert (await c.post("/settings/api/openrouter/start")).status_code == 409
        finally:
            del os.environ["LLM_API_KEY"]
        # Not an OpenRouter endpoint: neither.
        with open(os.path.join(config_ui.ETC_DIR, "brain.env"), "w") as f:
            f.write("LLM_BASE_URL=https://api.example.invalid/v1\n")
        async with _client(cookie=_session()) as c:
            assert (await c.get("/settings/api/account")).json()["offered"] is False
            assert (await c.post("/settings/api/openrouter/start")).status_code == 409
    asyncio.run(run())


def test_the_finish_needs_the_session_the_origin_and_this_boxs_live_state():
    async def run():
        home, *_rest, exchanged = _setup()
        async with _client(cookie=_session()) as c:
            q = await _start(c)
        state, form = q["state"], {"code": FAKE_CODE, "state": q["state"]}
        # No session (it ran out, or another browser): a plain page; nothing spent.
        async with _client() as c:
            r = await c.post("/settings/openrouter", data=form)
            assert r.status_code == 401 and "open my settings" in r.text
        # What a form under a no-referrer page sends: Origin: null. Refused, so the page
        # must not be one (the round trip above checks it is same-origin).
        async with _client(cookie=_session(), origin="null", fetch_site="same-origin") as c:
            assert (await c.post("/settings/openrouter", data=form)).status_code == 401
        # Another origin, or one a browser marks as another site's: not the owner's page.
        async with _client(cookie=_session(), origin="https://evil.example") as c:
            assert (await c.post("/settings/openrouter", data=form)).status_code == 401
        async with _client(cookie=_session(), fetch_site="cross-site") as c:
            assert (await c.post("/settings/openrouter", data=form)).status_code == 401
        # The brain's own port serves nothing here.
        async with _client(cookie=_session(), proto=None) as c:
            assert (await c.post("/settings/openrouter", data=form)).status_code == 404
            assert (await c.post("/settings/api/openrouter/start")).status_code == 404
        assert exchanged == [] and openrouter_connect._pending is not None
        head, nonce, sig = state.split(".")
        other = openrouter_connect._b64(b"https://192.168.1.50:8443")
        forged = [f"{head}.{nonce}.{'0' * len(sig)}",                         # not signed here
                  openrouter_connect.make_state("https://192.168.1.50:8443", nonce),  # another origin
                  f"{other}.{nonce}.{sig}", "", "a.b", "%%%.x.y"]
        async with _client(cookie=_session()) as c:
            for bad in forged:
                r = await c.get(f"/settings/openrouter?code={FAKE_CODE}&state={urllib.parse.quote(bad)}")
                assert r.status_code == 410, bad
                r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": bad})
                assert r.status_code == 410, bad
            assert exchanged == []
            # A finish after its time: expired, and the owner is told to connect again.
            openrouter_connect._clock = lambda: 5000.0 + openrouter_connect.PENDING_SECS + 1
            r = await c.post("/settings/openrouter", data=form)
            assert r.status_code == 410 and "connect again" in r.text
            # A new start replaces the one before.
            openrouter_connect._clock = lambda: 9000.0
            first = (await _start(c))["state"]
            second = (await _start(c))["state"]
            r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": first})
            assert r.status_code == 410
            r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": second})
            assert r.status_code == 200 and _key_file(home) == FAKE_KEY
        # Start needs the session too.
        async with _client() as c:
            assert (await c.post("/settings/api/openrouter/start")).status_code == 401
    asyncio.run(run())


def test_the_finish_form_fits_the_longest_code_and_state():
    origin = "https://" + "a" * 63 + ".local:65535"  # longer than any front door name
    state = openrouter_connect.make_state(origin, "n" * 22)
    body = urllib.parse.urlencode({"code": "%" * ai_account.CODE_MAX_CHARS, "state": state})
    assert len(body.encode()) < openrouter_connect.FORM_MAX_BYTES, len(body)


def test_every_failure_is_said_plainly_and_saves_nothing():
    async def run():
        home, check, _checks, _fetched, exchanged = _setup()
        cases = [
            ("exchange", (None, ai_account.SIGN_IN_REFUSED), "OpenRouter didn't accept the sign-in"),
            ("exchange", (None, ai_account.UNREACHABLE), "the AI service didn't answer"),
            ("check", ai_account.Check(ai_account.REFUSED, ai_account.NO_CREDIT),
             "this account has no credit left"),
            ("check", ai_account.Check(ai_account.REFUSED, "User not found.", ours=False),
             "User not found"),
        ]
        for what, answer, words in cases:
            openrouter_connect.exchange.answer = (FAKE_KEY, "")
            check.answer = ai_account.Check(ai_account.WORKS)
            if what == "exchange":
                openrouter_connect.exchange.answer = answer
            else:
                check.answer = answer
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
            assert r.status_code == 400, r.text
            assert "couldn&#x27;t connect your OpenRouter account" in r.text, r.text
            assert f"{_esc(words)}. You can try again" in r.text and "paste a key" in r.text, r.text
            assert _key_file(home) is None
            assert openrouter_connect._pending is None  # spent: a code works once
            assert openrouter_connect._held is None     # a refused key is not kept
        # The owner said no at OpenRouter (no code comes back).
        async with _client(origin=None) as c:
            r = await c.get("/settings/openrouter")
            assert r.status_code == 200 and _esc(EN._(openrouter_connect.NOT_CONNECTED_TEXT)) in r.text
        assert len(exchanged) == len(cases)  # one each; the "no" page exchanged nothing
        # The save itself broke (an env file not written): plain words, no developer text.
        async def broken(request, body, issued=False):
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="write brain.env: config_apply rc=3")
        real = config_ui.save_values
        config_ui.save_values = broken
        try:
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
            assert r.status_code == 400 and "config_apply" not in r.text and "brain.env" not in r.text
            assert _esc(EN._(openrouter_connect.SAVE_BROKE)) in r.text
        finally:
            config_ui.save_values = real
    asyncio.run(run())


def test_an_account_with_no_credit_is_connected_and_said():
    """The owner signed in to an account with nothing to spend: connected (it is their
    account), and the page says what to do (ai_account.NO_ACCOUNT_CREDIT)."""
    async def run():
        home, check, _checks, _fetched, _exchanged = _setup()
        check.answer = ai_account.Check(ai_account.WORKS, note=ai_account.NO_ACCOUNT_CREDIT)
        async with _client(cookie=_session()) as c:
            q = await _start(c)
            r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
            assert r.status_code == 200 and _key_file(home) == FAKE_KEY, r.text
            assert _esc(EN._(openrouter_connect.CONNECTED_TEXT)) in r.text
            assert _esc(EN._(ai_account.NO_ACCOUNT_CREDIT)) in r.text, r.text
            # The pasted code's way says the same.
            await _start(c)
            r = await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})
            assert r.status_code == 200 and r.json()["message"].endswith(
                EN._(ai_account.NO_ACCOUNT_CREDIT)), r.text
        # The real check, its requests faked: refused once (a key just given), then its
        # record, then the account's credit -- connected, and said.
        home, *_rest = _setup()
        real = (config_ui.check_key, ai_account._get, ai_account._sleep)
        try:
            config_ui.check_key = ai_account.check_key
            ai_account._get = p = sa.Provider([
                (401, {"error": {"message": "User not found."}}),
                (200, {"data": {"limit": None, "usage": 0, "is_free_tier": True}}),
                (200, {"data": {"total_credits": 0, "total_usage": 0}})])
            ai_account._sleep = lambda secs: None
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
            assert r.status_code == 200 and _key_file(home) == FAKE_KEY, r.text
            assert _esc(EN._(ai_account.NO_ACCOUNT_CREDIT)) in r.text, r.text
            assert [u.rsplit("/", 1)[1] for u in p.urls] == ["key", "key", "credits"], p.urls
        finally:
            config_ui.check_key, ai_account._get, ai_account._sleep = real
    asyncio.run(run())


def test_a_key_openrouter_just_gave_is_not_thrown_away():
    async def run():
        home, check, _checks, _fetched, exchanged = _setup()
        # The check got no answer: the key came from OpenRouter a moment ago, so it is saved.
        check.answer = ai_account.Check(ai_account.UNKNOWN, ai_account.UNREACHABLE)
        async with _client(cookie=_session()) as c:
            q = await _start(c)
            r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
        assert r.status_code == 200 and _key_file(home) == FAKE_KEY
        # A pasted key with the same check still needs "Save anyway" (unchanged).
        async with _client(cookie=_session()) as c:
            put = await c.put("/settings/api/config", json={"store": "brain_env",
                                                            "values": {"LLM_API_KEY": sa.FAKE_KEY}})
            assert put.status_code == 400 and put.json()["key_check"]["state"] == "unknown"
        # OpenClaw did not take it (both or neither): held, and "Save it again" saves it.
        home, check, *_rest = _setup()
        tries = []

        async def refused_once(key, base_url):
            tries.append(key)
            if len(tries) == 1:
                raise ai_account.NotSaved(ai_account.NOT_SHARED, "openclaw said no")
            return await _REAL_SAVE(key, base_url)

        ai_account.save_key = refused_once
        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
        try:
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                r = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert r.status_code == 400 and _esc(EN._(ai_account.NOT_SHARED)).rstrip(".") in r.text
                assert _key_file(home) is None
                account = (await c.get("/settings/api/account")).json()
                assert account["connect"]["retry"] is True and account["connect"]["pending"] is False
                again = await c.post("/settings/api/openrouter/retry")
                assert again.status_code == 200 and again.json() == {
                    "ok": True, "message": EN._(openrouter_connect.CONNECTED_TEXT), "retry": False}
                assert tries == [FAKE_KEY, FAKE_KEY] and _key_file(home) == FAKE_KEY
                # Nothing held any more.
                gone = await c.post("/settings/api/openrouter/retry")
                assert gone.status_code == 400 and gone.json()["message"] == EN._(
                    openrouter_connect.NOT_HELD_TEXT)
            # Held for a while only.
            tries.clear()
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                openrouter_connect._clock = lambda: 5000.0 + openrouter_connect.HELD_SECS + 1
                assert (await c.post("/settings/api/openrouter/retry")).status_code == 400
            async with _client() as c:  # the session, as everywhere in Settings
                assert (await c.post("/settings/api/openrouter/retry")).status_code == 401
        finally:
            logger.remove(sink)
            ai_account.save_key = _REAL_SAVE
        assert FAKE_KEY not in "\n".join(lines)
    asyncio.run(run())


def test_a_held_key_is_never_saved_over_a_newer_one():
    async def run():
        home, *_rest = _setup()
        calls = []

        async def refused_first(key, base_url):
            calls.append(key)
            if len(calls) == 1:
                raise ai_account.NotSaved(ai_account.NOT_SHARED, "openclaw said no")
            return await _REAL_SAVE(key, base_url)

        ai_account.save_key = refused_first
        try:
            async with _client(cookie=_session()) as c:
                q = await _start(c)
                await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert (await c.get("/settings/api/account")).json()["connect"]["retry"] is True
                # The owner pastes another key, and it saves: the held one is let go of
                # (ai_account.SAVED_HOOKS: whichever page saved).
                put = await c.put("/settings/api/config", json={"store": "brain_env",
                                                                "values": {"LLM_API_KEY": sa.FAKE_KEY}})
                assert put.status_code == 200 and _key_file(home) == sa.FAKE_KEY
                assert (await c.get("/settings/api/account")).json()["connect"]["retry"] is False
                r = await c.post("/settings/api/openrouter/retry")
                assert r.status_code == 400 and _key_file(home) == sa.FAKE_KEY
                # A new sign-in lets go of one too.
                calls.clear()
                q = await _start(c)
                await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert openrouter_connect._held is not None
                await _start(c)
                assert openrouter_connect._held is None
                # And one past its time is dropped, string and all, when looked at.
                calls.clear()
                q = await _start(c)
                await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                openrouter_connect._clock = lambda: 5000.0 + openrouter_connect.HELD_SECS + 1
                assert openrouter_connect._held_key() is None and openrouter_connect._held is None
        finally:
            ai_account.save_key = _REAL_SAVE
    asyncio.run(run())


def test_a_pasted_code_is_checked_before_it_spends_the_sign_in():
    async def run():
        home, *_rest, exchanged = _setup()
        async with _client(cookie=_session()) as c:
            r = await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})
            assert r.status_code == 400 and r.json()["message"] == EN._(openrouter_connect.NO_SIGN_IN_TEXT)
            await _start(c)
            pending = openrouter_connect._pending
            for bad in ("", "   ", "two words", "x" * (ai_account.CODE_MAX_CHARS + 1)):
                r = await c.post("/settings/api/openrouter/code", json={"code": bad})
                assert r.status_code == 400 and r.json()["message"] == EN._(
                    openrouter_connect.NOT_A_CODE_TEXT) and r.json()["pending"] is True, bad
            assert openrouter_connect._pending is pending and exchanged == []  # nothing spent
            r = await c.post("/settings/api/openrouter/code", content=b"not json",
                             headers={"content-type": "application/json"})
            assert r.status_code == 400
            # A code OpenRouter refuses spends it: the field goes (pending false).
            openrouter_connect.exchange.answer = (None, ai_account.SIGN_IN_REFUSED)
            r = await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})
            assert r.status_code == 400 and r.json()["pending"] is False
            assert "OpenRouter didn&#x27;t accept" not in r.text  # JSON, not HTML
            assert "OpenRouter didn't accept the sign-in" in r.json()["message"]
        async with _client() as c:  # the session, as everywhere in Settings
            assert (await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})).status_code == 401
    asyncio.run(run())


# ------------------------------------------------------------------ asking for the page

class _Garbage:
    """A TCP server that answers whatever it is asked with `reply`, then `drip` bytes
    one at a time, `every` seconds apart."""

    def __init__(self, reply: bytes, drip: bytes = b"", every: float = 0.0):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.reply, self.drip, self.every = reply, drip, every
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(self.reply)
                for b in self.drip:
                    time.sleep(self.every)
                    conn.sendall(bytes([b]))
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.srv.close()


def test_a_garbled_or_dripping_answer_is_no_answer_and_the_paste_field_stays():
    for reply in (b"NOT HTTP AT ALL\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort",
                  b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000 + b"\r\n\r\n"):
        srv = _Garbage(reply)
        try:
            try:
                ai_account._request(f"http://127.0.0.1:{srv.port}/", None, 3)
                raise AssertionError(f"no error for {reply[:20]!r}")
            except OSError:
                pass
        finally:
            srv.close()
    # A server that drips its body a byte at a time: cut at the deadline, not days later.
    srv = _Garbage(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n", drip=b"x" * 1000, every=0.2)
    try:
        t0 = time.monotonic()
        try:
            ai_account._request(f"http://127.0.0.1:{srv.port}/", None, 1.0)
            raise AssertionError("a dripping answer was read to the end")
        except OSError:
            pass
        assert time.monotonic() - t0 < 2.5
    finally:
        srv.close()

    async def run():
        _setup()
        srv = _Garbage(b"NOT HTTP AT ALL\r\n\r\n")
        real = openrouter_connect.__dict__["_fetch"]
        calls = []

        def fetch(url):  # the real probe, at the garbage server
            calls.append(url)
            return ai_account._deadline(lambda: ai_account._request(
                f"http://127.0.0.1:{srv.port}/", None, 2), 2)

        openrouter_connect._fetch = fetch
        try:
            async with _client(cookie=_session()) as c:
                for _ in range(2):
                    got = await c.get("/settings/api/account")
                    assert got.status_code == 200 and got.json()["offered"] is True
                    assert got.json()["connect"]["page"] is False
            assert len(calls) == 1  # the failure was remembered
            # Whatever breaks in the sign-in's state, the account step stays.
            def boom(request, env):
                raise RuntimeError("unexpected")
            real_status = openrouter_connect.status
            openrouter_connect.status = boom
            try:
                async with _client(cookie=_session()) as c:
                    got = await c.get("/settings/api/account")
                    assert got.status_code == 200 and got.json()["offered"] is True
                    assert got.json()["connect"]["offered"] is False
            finally:
                openrouter_connect.status = real_status
        finally:
            openrouter_connect._fetch = real
            srv.close()
    asyncio.run(run())


def test_a_request_leaves_no_timer_and_a_stalled_handshake_is_cut():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    timers = lambda: [t for t in threading.enumerate() if isinstance(t, threading.Timer)]  # noqa: E731
    before = len(timers())
    try:
        for _ in range(3):
            assert ai_account._request(f"http://127.0.0.1:{srv.server_port}/", None, 30) == (200, b"ok")
        time.sleep(0.1)
        assert len(timers()) == before  # each request's cut was cancelled when it ended
    finally:
        srv.shutdown()
    # A server that takes the connection and never answers the TLS handshake.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)  # the kernel completes the TCP connect; nobody ever reads or writes
    try:
        t0 = time.monotonic()
        try:
            ai_account._request(f"https://127.0.0.1:{silent.getsockname()[1]}/", None, 1.0)
            raise AssertionError("a stalled handshake did not end")
        except OSError:
            pass
        assert time.monotonic() - t0 < 2.5
    finally:
        silent.close()


def test_the_page_is_asked_for_one_at_a_time():
    _setup()
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(url):
        calls.append(url)
        started.set()
        release.wait(5)
        return 200, PAGE

    openrouter_connect._fetch = slow
    first = []
    t = threading.Thread(target=lambda: first.append(openrouter_connect.ready()))
    t.start()
    assert started.wait(5)
    # While that one runs, others take what was last known (nothing yet), at once.
    assert openrouter_connect.ready() is None and len(calls) == 1
    release.set()
    t.join(5)
    assert first == ["https://get.teaspoon.tech/connect/index.html"]
    assert openrouter_connect.ready() == first[0] and len(calls) == 1


# ------------------------------------------------------------------ the exchange

def test_the_exchange_sends_the_documented_request_and_reads_the_key():
    calls = []
    real = ai_account._post

    def answer(status, body):
        def post(url, data, timeout):
            calls.append((url, json.loads(data)))
            if isinstance(status, BaseException):
                raise status
            return status, body
        return post

    try:
        ai_account._post = answer(200, json.dumps({"key": FAKE_KEY, "user_id": "u"}).encode())
        assert ai_account.exchange_code(FAKE_CODE, "v" * 64) == (FAKE_KEY, "")
        assert calls == [("https://openrouter.ai/api/v1/auth/keys", {
            "code": FAKE_CODE, "code_verifier": "v" * 64, "code_challenge_method": "S256"})]
        for status, body, why in ((403, b'{"error":"Invalid code"}', ai_account.SIGN_IN_REFUSED),
                                  (400, b"", ai_account.SIGN_IN_REFUSED),
                                  (500, b"", ai_account.UNREACHABLE),
                                  (200, b'{"nokey": 1}', ai_account.UNREACHABLE),
                                  (200, b"<html>", ai_account.UNREACHABLE),
                                  (TimeoutError(), b"", ai_account.UNREACHABLE)):
            ai_account._post = answer(status, body)
            assert ai_account.exchange_code(FAKE_CODE, "v" * 64) == (None, why), status
        calls.clear()
        for bad in ("", "  ", "a b", "x" * (ai_account.CODE_MAX_CHARS + 1), "a\nb"):
            assert ai_account.exchange_code(bad, "v" * 64) == (None, ai_account.SIGN_IN_REFUSED)
        assert calls == []  # never sent
    finally:
        ai_account._post = real


def test_a_post_follows_no_redirect():
    import http.server
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append((self.command, self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(307)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _ = ai_account._request(f"http://127.0.0.1:{srv.server_port}/keys", None, 5, b'{"a":1}')
        assert status == 307 and seen == [("POST", "/keys", b'{"a":1}')]
    finally:
        srv.shutdown()


def test_no_secret_in_a_url_reaches_the_access_log():
    for name in ("token", "access_token", "gateway_token", "auth_token", "code", "state"):
        for sep in ("?", "&"):
            line = f'"GET /settings/openrouter{"?x=1" if sep == "&" else ""}{sep}{name}=s3cr3t HTTP/1.1"'
            assert "s3cr3t" not in gateway_server._redact(line), line
    line = f'"GET /settings/openrouter?code={FAKE_CODE}&state=abc.def.0123 HTTP/1.1"'
    assert gateway_server._redact(line) == '"GET /settings/openrouter?code=<redacted>&state=<redacted> HTTP/1.1"'
    for kept in ('"POST /settings/api/openrouter/start HTTP/1.1"', '"GET /talk/status?client=a HTTP/1.1"'):
        assert gateway_server._redact(kept) == kept


def test_gateway_server_mounts_the_routes_before_the_settings_code():
    paths = [getattr(r, "path", "") for r in gateway_server.app.routes]
    assert paths.index(openrouter_connect.LANDING_PATH) < paths.index("/settings/{code}")


# ------------------------------------------------------------------ the end of Wi-Fi setup

def _setup_voice(screen, phrases=True):
    v = wifi_voice.WifiSetupVoice(
        run=lambda *a: None, status_path=os.path.join(tempdir(), "s.json"),
        phrases=[settings_access.SettingsPhrase(screen=lambda: screen)] if phrases else ())
    said = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSSpeakFrame):
            said.append(frame.text)

    async def on_network():
        return True

    v.push_frame = push
    v.box_on_network = on_network
    v.state = "running"
    return v, said


def test_the_end_of_wifi_setup_offers_to_connect_the_account():
    async def run():
        home, *_rest = _setup()
        screen = sa.Screen()
        v, said = _setup_voice(screen)
        await v._announce({"phase": "connected", "target": "Home"})
        assert len(said) == 1, said
        assert said[0].startswith("I'm connected to Home, and I'm online again. ")
        assert said[0].endswith(EN._(settings_access.CONNECT_SHOWN_TEXT)), said
        title, lines, _secs, link = screen.shown[-1]
        assert title == "Settings" and lines[0] == "Connect OpenRouter" and link.startswith(
            f"{sa.ORIGIN}/settings/")  # the same code "open my settings" makes
        assert lines[2] == f"Code {settings_access._live.code.code}"
        # Without a screen that draws it: the address and the code, read out.
        _setup()
        v, said = _setup_voice(sa.Screen(qr=False))
        await v._announce({"phase": "connected", "target": "Home"})
        assert "Now let's connect your OpenRouter account. In your phone's browser" in said[0]
        # Not offered: a key was typed on the setup page (its own line), the owner has a
        # key of their own, the phrase is not heard here, or Settings is not set up.
        for case in ("typed", "own key", "no phrase", "no settings site"):
            home, *_rest = _setup()
            screen = sa.Screen()
            v, said = _setup_voice(screen, phrases=case != "no phrase")
            status = {"phase": "connected", "target": "Home"}
            if case == "typed":
                status["account"] = {"result": "saved", "reason": "", "ours": True}
            elif case == "own key":
                with open(os.path.join(home, "llm_key"), "w") as f:
                    f.write("sk-or-v1-" + "3" * 64)
            elif case == "no settings site":
                settings_access.SETTINGS_PORT = None
            await v._announce(status)
            assert screen.shown == [] and len(said) == 1, (case, said)
            assert "OpenRouter" not in said[0], (case, said)
        settings_access.SETTINGS_PORT = sa.PORT
    asyncio.run(run())


def test_an_offer_that_breaks_after_speaking_is_not_said_again():
    async def run():
        _setup()
        v, said = _setup_voice(sa.Screen())
        real = settings_access.SettingsPhrase.open

        async def speaks_then_breaks(self, voice, lead="", connect=False):
            await voice.say(lead)
            raise RuntimeError("the screen went away")

        settings_access.SettingsPhrase.open = speaks_then_breaks
        try:
            await v._announce({"phase": "connected", "target": "Home"})
        finally:
            settings_access.SettingsPhrase.open = real
        assert said == ["I'm connected to Home, and I'm online again."], said
        # Broken before anything was said: the line is said, once.
        _setup()
        v, said = _setup_voice(sa.Screen())

        async def breaks(self, voice, lead="", connect=False):
            raise RuntimeError("no code")

        settings_access.SettingsPhrase.open = breaks
        try:
            await v._announce({"phase": "connected", "target": "Home"})
        finally:
            settings_access.SettingsPhrase.open = real
        assert said == ["I'm connected to Home, and I'm online again."], said
    asyncio.run(run())


def test_the_setup_page_promises_the_offer_only_where_the_box_makes_it():
    """The page's line and the offer follow one rule (ai_account.sign_in_after_setup):
    each case below is the same for both, the page reading what the setup unit can."""
    later = _esc(EN._(wifi_setup.SIGN_IN_LATER))
    env = {"TEAPORT_FRONTDOOR_HOST": sa.HOST, "TEAPORT_SETTINGS_PORT": str(sa.PORT),
           "LLM_BASE_URL": "https://openrouter.ai/api/v1"}

    async def offered() -> bool:
        screen = sa.Screen()
        v, said = _setup_voice(screen)
        await v._announce({"phase": "connected", "target": "Home"})
        return screen.shown != []

    async def run():
        for case, promised in (("no key", True), ("starter key", True), ("own key", False),
                               ("not openrouter", False), ("no settings site", False)):
            home, *_rest = _setup()
            os.environ.update(env)
            try:
                if case in ("starter key", "own key"):
                    key = "sk-or-v1-" + "4" * 64
                    with open(os.path.join(home, "llm_key"), "w") as f:
                        f.write(key)
                    if case == "starter key":  # the provisioned key's marker (credit.py)
                        os.makedirs(os.path.join(home, ".local", "state", "teaport"), exist_ok=True)
                        with open(os.path.join(home, ".local", "state", "teaport",
                                               "starter_key_sha256"), "w") as f:
                            f.write(hashlib.sha256(key.encode()).hexdigest())
                elif case == "not openrouter":
                    os.environ["LLM_BASE_URL"] = "https://api.example.invalid/v1"
                    with open(os.path.join(config_ui.ETC_DIR, "brain.env"), "w") as f:
                        f.write("LLM_BASE_URL=https://api.example.invalid/v1\n")
                elif case == "no settings site":
                    del os.environ["TEAPORT_SETTINGS_PORT"]
                    settings_access.SETTINGS_PORT = None
                page = later in wifi_setup.render_account(EN)
                offer = await offered()
                assert (page, offer) == (promised, promised), (case, page, offer)
            finally:
                settings_access.SETTINGS_PORT = sa.PORT
                for k in env:
                    os.environ.pop(k, None)
    asyncio.run(run())


# ------------------------------------------------------------------ the static page

def _web() -> str:
    with open(openrouter_connect.WEB_PAGE, encoding="utf-8") as f:
        return f.read()


def _hash(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def test_the_static_page_is_current_strict_and_self_contained():
    assert openrouter_connect.main(["--check"]) == 0, "run: python -m teaport_brain.openrouter_connect"
    page = _web()
    assert openrouter_connect.PAGE_MARK.decode() in page
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    policy = dict(p.strip().split(" ", 1) for p in csp.split(";"))
    script = re.findall(r"<script>(.*?)</script>", page, re.S)
    style = re.findall(r"<style>(.*?)</style>", page, re.S)
    assert len(script) == len(style) == 1
    assert policy == {"default-src": "'none'", "script-src": f"'sha256-{_hash(script[0])}'",
                      "style-src": f"'sha256-{_hash(style[0])}'", "base-uri": "'none'",
                      "form-action": "'none'"}, policy
    assert '<meta name="referrer" content="no-referrer">' in page
    assert not re.search(r"\b(?:src|href)=\"(?:https?:)?//", page)  # nothing from elsewhere
    assert f'var PATH = "{openrouter_connect.LANDING_PATH}";' in page
    # Its words, in every language the box speaks, from the catalogs; the field it names
    # is the Settings field's name in that language.
    table = json.loads(re.search(r"/\*strings\*/(.*?)/\*end\*/", page, re.S).group(1))
    assert len(table) == 13 and set(table["en"]) == set(openrouter_connect.PAGE_TEXT)
    for lang, words in table.items():
        t = i18n.get(lang.replace("-", "_"))
        assert t._(config_ui.ACCOUNT_TEXT["code"]) in words["t-carry"], lang
    # The markup's English is the catalog's English (the page without its script).
    for k, v in table["en"].items():
        if k != "copied":
            assert f'id="{k}"' in page and v in page, k


def test_a_translation_cannot_end_the_pages_script():
    real = openrouter_connect.page_strings
    openrouter_connect.page_strings = lambda: {"en": {"title": "a </script><b>x"}}
    try:
        out = openrouter_connect.render_page(_web())
    finally:
        openrouter_connect.page_strings = real
    assert out.count("</script>") == 1 and "\\u003c/script>\\u003cb>x" in out
    script = re.search(r"<script>(.*?)</script>", out, re.S).group(1)
    assert f"'sha256-{_hash(script)}'" in out  # the hash follows the escaped text


def test_the_static_page_goes_on_only_to_a_home_address():
    node = shutil.which("node")
    if not node:
        raise appliance.Unavailable("node is not installed")
    page = _web()
    fn = re.search(r"(function homeOrigin\(s\) \{.*?\n  \})\n", page, re.S).group(1)

    def state(origin: str) -> str:
        return openrouter_connect._b64(origin.encode()) + ".n0nce.519"

    cases = {
        "https://teaport-9e35.local:8443": "https://teaport-9e35.local:8443",
        "https://teaport.local": "https://teaport.local",
        "https://192.168.1.105:8443": "https://192.168.1.105:8443",
        "https://10.0.0.7:8443": "https://10.0.0.7:8443",
        "https://172.20.1.2:8443": "https://172.20.1.2:8443",
        "https://[fd12:3456::1]:8443": "https://[fd12:3456::1]:8443",
        "http://teaport.local:8443": None,          # not HTTPS
        "https://evil.example": None,               # not a home address
        "https://evil.example.local.evil.example": None,
        "https://8.8.8.8:8443": None,
        "https://172.32.0.1": None,
        "https://user@teaport.local:8443": None,
        "https://teaport.local:8443/path": None,    # an origin only
        "javascript:alert(1)": None,
    }
    script = fn + "\nconst cases = " + json.dumps({state(k): v for k, v in cases.items()}) + \
        ";\nconst out = {};\nfor (const s in cases) out[s] = homeOrigin(s);\n" \
        "out['garbage'] = homeOrigin('%%%.x.y');\nconsole.log(JSON.stringify(out));\n"
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert got.pop("garbage") is None
    assert {k: got[state(k)] for k in cases} == cases


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
