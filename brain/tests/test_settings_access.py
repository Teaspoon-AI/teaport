#
# "open my settings" (#279): the phrase, the one-time code, the session the config page
# takes in place of the token, the key checked on save, and the key typed in setup mode.
#
# What has to hold:
#   - The phrase (room and Talk; it needs no model) puts a code up as a QR code of
#     https://<front door>/settings/<code>; with no screen that draws one, the voice reads
#     the address and the code; offline (where Wi-Fi setup is on) it says to get online
#     first; with no front door, that Settings cannot be reached. Talk about settings,
#     a question, or a long sentence is not the phrase.
#   - /settings/<code>: a GET spends nothing; its POST spends the code and answers with a
#     session cookie (HttpOnly, Secure, SameSite=Strict, Path=/settings, bounded) and a
#     redirect to the page. A code typed at /settings works the same. A used code is
#     "already used", a late one "expired"; wrong ones lock the code. Only what came
#     through the front door is answered.
#   - The config routes under /settings take the session in place of the token: reads
#     with the cookie, writes with the cookie and the front door's Origin. Another origin,
#     no origin, the brain's own port, a forged, expired or old-token session: 401. The
#     token keeps working.
#   - Saving LLM_API_KEY checks it first: a refused or unchecked key is not written, and
#     the answer says why in the reader's language, unless the save says force_key.
#   - The check (ai_account.check_key) reads both shapes of OpenRouter's key record,
#     takes a spent key as refused, a 401/403's words cleaned of links and keys, and no
#     answer as unknown.
#   - The setup-mode page offers the key with the Wi-Fi choice; once online it is
#     adopted only if it works, and the voice says how it went.
#   - The no-model line points at the phrase where it is heard.
#
# No test talks to a provider (ai_account._get and config_ui.check_key are fakes), and
# every key, code and account here is made up.
#
# Run: python test_settings_access.py   (or via pytest)
#
import asyncio
import json
import os
import sys
import threading

import httpx
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import TTSSpeakFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

import appliance  # noqa: E402
from tempdirs import tempdir  # noqa: E402
from teaport_brain import (  # noqa: E402
    agent_key, ai_account, config_ui, i18n, llm_error_speaker, openrouter_connect, pairing,
    settings_access, wifi_setup, wifi_voice,
)
from teaport_brain import gateway_server  # noqa: E402

HOST = "teaport-9e35.local"
PORT = 8443                       # TEAPORT_SETTINGS_PORT's default: an origin of its own
ORIGIN = f"https://{HOST}:{PORT}"
TOKEN = "t0k3n-made-up"
FAKE_KEY = "sk-or-v1-" + "0" * 64   # a made-up key of the real shape


class Screen:
    """A screen that draws QR codes (or not: qr=False)."""

    def __init__(self, qr=True):
        self.qr, self.shown = qr, []

    def fits_qr(self, text):
        return self.qr and len(text.encode()) <= pairing.QR_MAX_BYTES

    def show(self, title, lines, secs=None, qr=None):
        self.shown.append((title, list(lines), secs, qr))
        return True

    def draws(self, feature):
        return True


class Voice(wifi_voice.PhraseVoice):
    """The phrases processor without a pipeline: what it says is collected."""

    def __init__(self, screen, online=None, **kw):
        super().__init__(phrases=[settings_access.SettingsPhrase(screen=lambda: screen)], **kw)
        self.pushed = []
        if online is not None:
            async def box_on_network():
                return online
            self.box_on_network = box_on_network

    @property
    def said(self):
        return [f.text for f in self.pushed if isinstance(f, TTSSpeakFrame)]

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)


def _setup(host=HOST, port=PORT):
    pairing.FRONTDOOR_HOST = host
    settings_access.SETTINGS_PORT = port
    pairing.TRUSTED_CERT = False
    settings_access._live = None
    settings_access._codes = pairing.CodeBook()
    settings_access._screen = None
    settings_access._clock = lambda: 1000.0
    settings_access._wall = lambda: 1_800_000_000.0
    os.environ["GATEWAY_TOKEN"] = TOKEN
    os.environ.pop("LLM_API_KEY", None)
    etc, home = tempdir("settings-etc-"), tempdir("settings-home-")
    with open(os.path.join(etc, "brain.env"), "w") as f:
        f.write("LLM_BASE_URL=https://openrouter.ai/api/v1\n")
    config_ui.ETC_DIR = etc
    config_ui.SIP_CONF = os.path.join(home, "teaport-sip.conf")
    os.environ["HOME"] = home
    writes = []

    async def privileged(action, target, stdin=None):
        writes.append((action, target))
        if action == "write":
            with open(os.path.join(etc, target), "w") as f:
                f.write(stdin)

    async def states(units):
        return {u: {"state": "active", "active_since": 0.0} for u in units}

    config_ui._privileged = privileged
    config_ui._unit_states = states
    config_ui._rows()["LLM_API_KEY"]["file"] = os.path.join(home, "llm_key")
    checks = []

    def check(key, base_url, timeout=None, **asked):
        checks.append((key, base_url))
        check.asked = asked  # issued=, account= (ai_account.check_key)
        return check.answer

    check.answer = ai_account.Check(ai_account.WORKS)
    config_ui.check_key = check
    # A saved key is not handed to an OpenClaw on the machine running the tests
    # (test_agent_key.py has that path, with a fake CLI).
    agent_key.SHARES = False
    # OpenRouter's sign-in page is not asked for over the network (test_openrouter_connect.py
    # has that path): here it is not there, so the AI account step offers the paste field.
    openrouter_connect._fetch = lambda url: (404, b"")
    openrouter_connect._probe = None
    openrouter_connect._pending = None
    return home, check, checks


def _client(proto="https", origin=ORIGIN, cookie=None, lang=None, port=PORT, fetch_site=None,
            host=HOST):
    """Through the Settings site as Caddy is one: a loopback peer saying https, with the
    browser's Host (the Settings port) passed on."""
    app = FastAPI()
    app.include_router(config_ui.router)
    app.include_router(config_ui.router, prefix="/settings")
    app.include_router(settings_access.router)
    app.add_middleware(settings_access.SettingsGuard)
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    headers["Host"] = f"{host}:{port}" if port != 443 else host
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site
    if origin:
        headers["Origin"] = origin
    if lang:
        headers["Accept-Language"] = lang
    cookies = {settings_access.COOKIE: cookie} if cookie else None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
                             base_url="http://box", headers=headers, cookies=cookies)


def _code_of(screen):
    link = screen.shown[-1][3]
    assert link.startswith(f"{ORIGIN}/settings/"), link
    return link.rsplit("/", 1)[1]


async def _signed_in(c, code) -> str:
    r = await c.post(f"/settings/{code}")
    assert r.status_code == 303 and r.headers["location"] == settings_access.PAGE_PATH, r.text
    return r.cookies[settings_access.COOKIE]


# ------------------------------------------------------------------ phrase -> phone

def test_the_phrase_shows_a_code_and_the_phone_gets_settings_without_the_token():
    async def run():
        _setup()
        screen = Screen()
        v = Voice(screen)
        assert await v._heard("Open my settings.")
        assert v.said == [i18n.get("en")._(settings_access.SHOWN_TEXT)]
        title, lines, secs, link = screen.shown[-1]
        assert title == "Settings" and secs == pairing.OFFER_SECS and len(link) <= pairing.QR_MAX_BYTES
        code = _code_of(screen)
        assert f"Code {code}" in lines
        async with _client() as c:
            # Not signed in: the page's routes refuse, as they always did.
            assert (await c.get("/settings/api/config")).status_code == 401
            assert (await c.get("/settings/api/licenses")).status_code == 401
            # The licenses page is the page's, not a code.
            page = await c.get("/settings/licenses")
            assert page.status_code == 200 and "Open-source licenses" in page.text
            # A GET spends nothing: a link preview, then the real tap.
            for _ in range(2):
                r = await c.get(f"/settings/{code}")
                assert r.status_code == 200 and "Continue" in r.text and r.headers["cache-control"] == "no-store"
            r = await c.post(f"/settings/{code}")
            assert r.status_code == 303 and r.headers["location"] == "/settings/config"
            cookie = r.headers["set-cookie"].lower()
            for part in ("httponly", "secure", "samesite=strict", "path=/settings", "max-age=3600"):
                assert part in cookie, cookie
            session = r.cookies[settings_access.COOKIE]
            assert screen.shown[-1][3] is None  # the QR code is down: "go on on your phone"
        async with _client(cookie=session) as c:
            got = await c.get("/settings/api/config")
            assert got.status_code == 200 and "LLM_API_KEY" not in got.json()["values"]["brain_env"]
            lic = await c.get("/settings/api/licenses")
            assert lic.status_code == 200 and lic.json()["packages"], lic.text
            account = (await c.get("/settings/api/account")).json()
            assert account["offered"] is True and account["key_set"] is False
            assert account["text"]["title"] == "AI account"
            put = await c.put("/settings/api/config", json={"store": "brain_env",
                                                            "values": {"LLM_API_KEY": FAKE_KEY}})
            assert put.status_code == 200, put.text
            assert (await c.get("/settings")).headers["location"] == "/settings/config"
        # The token keeps working, on the brain's own port too (from the box itself).
        async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
            assert (await c.get("/api/config", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 200
    asyncio.run(run())


def test_an_expired_reused_or_wrong_code_is_refused():
    async def run():
        _setup()
        screen = Screen()
        v = Voice(screen)
        await v._heard("open my settings")
        code = _code_of(screen)
        async with _client() as c:
            # The brain's own port (no X-Forwarded-Proto from Caddy): nothing, and no guess.
            async with _client(proto=None) as lan:
                assert (await lan.get(f"/settings/{code}")).status_code == 404
                assert (await lan.post(f"/settings/{code}")).status_code == 404
            await _signed_in(c, code.lower())  # case does not matter
            again = await c.post(f"/settings/{code}")
            assert again.status_code == 410 and "already used" in again.text
            assert (await c.get(f"/settings/{code}")).status_code == 410
            # A code past its five minutes.
            await v._heard("open my settings")
            late = _code_of(screen)
            settings_access._clock = lambda: 1000.0 + pairing.OFFER_SECS + 1
            r = await c.get(f"/settings/{late}")
            assert r.status_code == 410 and "expired" in r.text
            # Asking again replaces the code: the one before is expired, never a guess.
            settings_access._clock = lambda: 2000.0
            await v._heard("open my settings")
            first = _code_of(screen)
            await v._heard("show me your settings")
            second = _code_of(screen)
            assert (await c.get(f"/settings/{first}")).status_code == 410
            # Wrong codes lock the live one.
            for _ in range(pairing.MAX_WRONG):
                assert (await c.get("/settings/ZZZZZZ")).status_code == 410
            assert (await c.post(f"/settings/{second}")).status_code == 410
            # A code read aloud, typed in at /settings.
            await v._heard("open my settings")
            typed = _code_of(screen)
            form = await c.get("/settings")
            assert form.status_code == 200 and 'name="code"' in form.text
            wrong = await c.post("/settings", data={"code": "AAAAAA"})
            assert wrong.status_code == 200 and "didn" in wrong.text
            # The wrong one counted: still under the limit, the right one works.
            r = await c.post("/settings", data={"code": f" {typed[:3]} {typed[3:].lower()} "})
            assert r.status_code == 303 and settings_access.COOKIE in r.cookies
    asyncio.run(run())


def test_the_session_cannot_be_used_from_another_origin_or_outside_its_bounds():
    async def run():
        _setup()
        screen = Screen()
        await Voice(screen)._heard("open my settings")
        async with _client() as c:
            session = await _signed_in(c, _code_of(screen))
        body = {"store": "brain_env", "values": {"TTS_VOICE": "af_bella"}}
        async with _client(cookie=session, origin="https://elsewhere.example") as c:
            assert (await c.put("/settings/api/config", json=body)).status_code == 401
            assert (await c.post("/settings/api/restart", json={"unit": "teaport-brain"})).status_code == 401
        async with _client(cookie=session, origin=None) as c:
            assert (await c.put("/settings/api/config", json=body)).status_code == 401
            assert (await c.get("/settings/api/config")).status_code == 200  # a read needs no origin
        async with _client(cookie=session, proto=None) as c:  # the brain's own port
            assert (await c.get("/settings/api/config")).status_code == 404  # not served there
        async with _client(cookie=session) as c:
            assert (await c.put("/settings/api/config", json=body)).status_code == 200
        expiry, nonce, sig = session.split(".")
        forged = f"{int(expiry) + 60}.{nonce}.{sig}"
        async with _client(cookie=forged) as c:
            assert (await c.get("/settings/api/config")).status_code == 401
        settings_access._wall = lambda: 1_800_000_000.0 + settings_access.SESSION_SECS + 1
        async with _client(cookie=session) as c:
            assert (await c.get("/settings/api/config")).status_code == 401
        settings_access._wall = lambda: 1_800_000_000.0
        os.environ["GATEWAY_TOKEN"] = "a-new-token"  # rotating the token ends every session
        async with _client(cookie=session) as c:
            assert (await c.get("/settings/api/config")).status_code == 401
    asyncio.run(run())


# ------------------------------------------------------------------ the key on save

def test_a_key_is_checked_before_it_is_saved():
    async def run():
        home, check, checks = _setup()
        key_file = os.path.join(home, "llm_key")
        put = {"store": "brain_env", "values": {"LLM_API_KEY": FAKE_KEY}}
        auth = {"authorization": f"Bearer {TOKEN}"}
        async with _client(proto=None, origin=None, lang="es-MX,es;q=0.9", port=7861, host="127.0.0.1") as c:
            check.answer = ai_account.Check(ai_account.REFUSED, "User not found", ours=False)
            r = await c.put("/api/config", json=put, headers=auth)
            assert r.status_code == 400 and r.json()["key_check"] == {
                "state": "refused", "reason": "User not found"}
            assert not os.path.exists(key_file)
            assert checks[-1] == (FAKE_KEY, "https://openrouter.ai/api/v1")
            check.answer = ai_account.Check(ai_account.UNKNOWN, ai_account.UNREACHABLE)
            r = await c.put("/api/config", json=put, headers=auth)
            es = i18n.get("es")
            assert r.json()["key_check"] == {"state": "unknown",
                                             "reason": es._(ai_account.UNREACHABLE)}
            assert not os.path.exists(key_file)
            # "Save anyway".
            r = await c.put("/api/config", json={**put, "force_key": True}, headers=auth)
            assert r.status_code == 200 and open(key_file).read() == FAKE_KEY + "\n"
            check.answer = ai_account.Check(ai_account.WORKS)
            r = await c.put("/api/config", json={**put, "values": {"LLM_API_KEY": "sk-or-v1-other"}},
                            headers=auth)
            assert r.status_code == 200 and open(key_file).read() == "sk-or-v1-other\n"
            # A save without the key checks nothing.
            n = len(checks)
            await c.put("/api/config", json={"store": "brain_env", "values": {"TTS_VOICE": "af_bella"}},
                        headers=auth)
            assert len(checks) == n
            # The owner's step speaks the phone's language.
            account = (await c.get("/api/account", headers=auth)).json()
            assert account["lang"] == "es" and account["key_set"] is True
            assert account["text"]["save"] == es._(config_ui.ACCOUNT_TEXT["save"])
    asyncio.run(run())


class Provider:
    """ai_account._get, faked: (status, body) or an exception; or {path end: that} per
    URL; or a list of them, one per request (the last one repeated)."""

    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def __call__(self, url, key, timeout):
        self.urls.append(url)
        answer = self.answer
        if isinstance(answer, list):
            answer = answer[min(len(self.urls), len(answer)) - 1]
        if isinstance(answer, dict):
            answer = next(a for end, a in answer.items() if url.endswith(end))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body


def test_a_key_just_given_is_asked_again_before_its_refusal_counts():
    """A key OpenRouter's sign-in gave a moment ago can be refused (401) before it is
    known everywhere: with `issued`, a 401 is asked again after each wait; a 402 or 403,
    or a key not just given, is refused at once."""
    real, real_sleep, real_clock = ai_account._get, ai_account._sleep, ai_account._clock
    base = "https://openrouter.ai/api/v1"
    waited = []
    ai_account._sleep = waited.append
    ai_account._clock = lambda: 100.0  # no time passes but the waits (not counted here)
    no = (401, {"error": {"message": "User not found."}})
    ok = (200, {"data": {"limit": None, "usage": 0}})
    try:
        ai_account._get = p = Provider([no, no, ok])
        assert ai_account.check_key(FAKE_KEY, base, issued=True).state == ai_account.WORKS
        assert waited == [1.0, 2.0] and len(p.urls) == 3
        waited.clear()
        ai_account._get = p = Provider([no])
        got = ai_account.check_key(FAKE_KEY, base, issued=True)
        assert got.state == ai_account.REFUSED and got.reason == "User not found", got
        assert waited == list(ai_account.ISSUED_RETRY_SECS) and len(p.urls) == 4
        for issued, answer in ((False, no), (True, (403, {})), (True, (402, {}))):
            waited.clear()
            ai_account._get = p = Provider([answer, ok])
            got = ai_account.check_key(FAKE_KEY, base, issued=issued)
            assert got.state == ai_account.REFUSED and not waited and len(p.urls) == 1, answer
    finally:
        ai_account._get, ai_account._sleep, ai_account._clock = real, real_sleep, real_clock


def test_the_whole_check_keeps_one_deadline():
    """However many asks an issued key takes, and the account's credit after them, the
    check ends within its timeout plus the waits: each request gets what is left, a wait
    that would leave too little is not taken, and /credits is skipped when the time is
    up (the record's reading stands in)."""
    real, real_sleep, real_clock = ai_account._get, ai_account._sleep, ai_account._clock
    base = "https://openrouter.ai/api/v1"
    now = [0.0]
    asked = []

    def slow(answers, secs):
        def get(url, key, timeout):
            asked.append((url.rsplit("/", 1)[1], round(timeout, 3)))
            now[0] += min(secs, timeout)
            status, body = answers[min(len(asked), len(answers)) - 1]
            return status, json.dumps(body).encode()
        return get

    def sleep(secs):
        now[0] += secs

    no = (401, {"error": {"message": "User not found."}})
    free = (200, {"data": {"limit": None, "usage": 0, "is_free_tier": True}})
    ai_account._sleep, ai_account._clock = sleep, lambda: now[0]
    try:
        # Each 401 takes 3 s: the asks get what is left of 10 s + 6 s of waits, the last
        # one its MIN_REQUEST_SECS, and the check ends at the deadline.
        ai_account._get = slow([no], 3.0)
        got = ai_account.check_key(FAKE_KEY, base, timeout=10.0, issued=True, account=True)
        assert got.state == ai_account.REFUSED and now[0] <= 16.0, (got, now[0])
        assert asked == [("key", 10.0), ("key", 10.0), ("key", 7.0), ("key", 1.0)], asked
        # A slower one: the last wait would leave too little, so it is not taken.
        asked.clear(); now[0] = 0.0
        ai_account._get = slow([no], 5.0)
        got = ai_account.check_key(FAKE_KEY, base, timeout=10.0, issued=True)
        assert got.state == ai_account.REFUSED and now[0] <= 16.0, (got, now[0])
        assert [u for u, _ in asked] == ["key", "key", "key"], asked
        # A slow key answer leaves too little for /credits: the record's reading counts.
        asked.clear(); now[0] = 0.0
        ai_account._get = slow([free], 9.5)
        got = ai_account.check_key(FAKE_KEY, base, timeout=10.0, account=True)
        assert got.note == ai_account.NO_ACCOUNT_CREDIT and [u for u, _ in asked] == ["key"]
        # Otherwise /credits gets only what is left.
        asked.clear(); now[0] = 0.0
        ai_account._get = slow([free, (200, {"data": {"total_credits": 5, "total_usage": 1}})], 4.0)
        got = ai_account.check_key(FAKE_KEY, base, timeout=10.0, account=True)
        assert got.note == "" and asked == [("key", 10.0), ("credits", 6.0)], asked
        assert now[0] <= 10.0
    finally:
        ai_account._get, ai_account._sleep, ai_account._clock = real, real_sleep, real_clock


def test_a_key_on_an_account_with_no_credit_is_saved_and_said():
    """A key with no limit of its own reads as having credit whatever its account holds:
    for a save (`account`), OpenRouter's account credit is read too. None left: the key
    works, with the note. Credit left, or no answer about it: nothing said. The monitor's
    check (no `account`) asks nothing more. Made-up amounts."""
    real = ai_account._get
    base = "https://openrouter.ai/api/v1"
    key = (200, {"data": {"label": "made-up", "limit": None, "usage": 0, "is_free_tier": True}})
    paid = (200, {"data": {"label": "made-up", "limit": None, "usage": 3, "is_free_tier": False}})
    empty = (200, {"data": {"total_credits": 0, "total_usage": 0}})
    try:
        ai_account._get = p = Provider({"/key": key, "/credits": empty})
        got = ai_account.check_key(FAKE_KEY, base, account=True)
        assert got.state == ai_account.WORKS and got.note == ai_account.NO_ACCOUNT_CREDIT, got
        assert p.urls == [base + "/key", base + "/credits"]
        ai_account._get = p = Provider({"/key": key, "/credits": empty})
        assert ai_account.check_key(FAKE_KEY, base).note == "" and p.urls == [base + "/key"]
        # /credits read: it decides, a paid account spent to nothing included.
        for record in (key, paid):
            for credits, none in (((200, {"data": {"total_credits": 10, "total_usage": 9.5}}), False),
                                  ((200, {"data": {"total_credits": 10, "total_usage": 10.0}}), True)):
                ai_account._get = Provider({"/key": record, "/credits": credits})
                got = ai_account.check_key(FAKE_KEY, base, account=True)
                assert got.state == ai_account.WORKS, credits
                assert got.note == (ai_account.NO_ACCOUNT_CREDIT if none else ""), (record, credits)
        # A total of 0 beside a record that says the account has paid: they disagree, so
        # nothing is said (a never-paid account's 0 is the case above).
        ai_account._get = Provider({"/key": paid, "/credits": empty})
        assert ai_account.check_key(FAKE_KEY, base, account=True).note == ""
        # Not read (refused -- OpenRouter documents it for management keys --, no answer, a
        # shape not read): the key's record decides: never paid, no limit of its own.
        for credits in ((403, {"error": {"message": "Only management keys can perform this operation"}}),
                        (401, {}), (200, {"data": {}}), (200, b"not json"), OSError("no route")):
            for record, none in ((key, True), (paid, False),
                                 ((200, {"data": {"limit": 5, "limit_remaining": 5, "is_free_tier": True}}), False)):
                ai_account._get = Provider({"/key": record, "/credits": credits})
                got = ai_account.check_key(FAKE_KEY, base, account=True)
                assert got.state == ai_account.WORKS, credits
                assert got.note == (ai_account.NO_ACCOUNT_CREDIT if none else ""), (record, credits)

        async def run():
            home, _, _ = _setup()
            config_ui.check_key = ai_account.check_key  # the real check, its requests faked
            ai_account._get = Provider({"/key": key, "/credits": empty})
            auth = {"authorization": f"Bearer {TOKEN}"}
            async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
                r = await c.put("/api/config", headers=auth,
                                json={"store": "brain_env", "values": {"LLM_API_KEY": FAKE_KEY}})
            assert r.status_code == 200, r.text
            assert r.json()["notice"] == ai_account.NO_ACCOUNT_CREDIT, r.text
            with open(os.path.join(home, "llm_key")) as f:
                assert f.read().strip() == FAKE_KEY
            got = await ai_account.adopt(FAKE_KEY, base)  # setup mode: saved, and said
            assert got.state == ai_account.WORKS and got.note == ai_account.NO_ACCOUNT_CREDIT
        asyncio.run(run())
    finally:
        ai_account._get = real


def test_the_check_reads_the_providers_answer():
    real = ai_account._get
    base = "https://openrouter.ai/api/v1"
    try:
        for record in ({"data": {"label": "made-up", "limit": 5, "limit_remaining": 3.5, "usage": 1.5}},
                       {"label": "made-up", "limit": 5, "limit_remaining": 3.5, "usage": 1.5}):
            ai_account._get = p = Provider((200, record))
            got = ai_account.check_key(FAKE_KEY, base)
            assert got.state == ai_account.WORKS and got.data["limit_remaining"] == 3.5, got
            assert p.urls == ["https://openrouter.ai/api/v1/key"]
        ai_account._get = Provider((200, {"data": {"limit": 5, "limit_remaining": 0, "usage": 5}}))
        assert ai_account.check_key(FAKE_KEY, base) == ai_account.Check(
            ai_account.REFUSED, ai_account.NO_CREDIT, data={"limit": 5, "limit_remaining": 0, "usage": 5})
        ai_account._get = Provider((200, {"data": {"limit": None, "limit_remaining": None, "usage": 9}}))
        assert ai_account.check_key(FAKE_KEY, base).state == ai_account.WORKS  # no limit: fine
        ai_account._get = Provider((401, {"error": {"message": "Key sk-or-v1-abcd****ef12 is not valid. "
                                                               "See https://openrouter.ai/keys/ab12 for help."}}))
        got = ai_account.check_key(FAKE_KEY, base)
        assert got.state == ai_account.REFUSED and not got.ours
        assert got.reason == "Key is not valid", got.reason
        ai_account._get = Provider((403, b"<html>forbidden</html>"))
        assert ai_account.check_key(FAKE_KEY, base) == ai_account.Check(
            ai_account.REFUSED, ai_account.REFUSED_BARE)
        for answer in ((500, {"error": "boom"}), (429, {}), OSError("no route")):
            ai_account._get = Provider(answer)
            assert ai_account.check_key(FAKE_KEY, base) == ai_account.Check(
                ai_account.UNKNOWN, ai_account.UNREACHABLE), answer
        ai_account._get = p = Provider((200, {"data": []}))
        assert ai_account.check_key(FAKE_KEY, "https://api.groq.com/openai/v1/").state == ai_account.WORKS
        assert p.urls == ["https://api.groq.com/openai/v1/models"]
        assert ai_account.check_key("  ", base) == ai_account.Check(ai_account.REFUSED, ai_account.EMPTY)
        assert ai_account.check_key("sk-a\nsk-b", base).reason == ai_account.NOT_A_KEY
        assert ai_account.check_key(FAKE_KEY, "") == ai_account.Check(ai_account.UNKNOWN, ai_account.NO_SERVICE)
    finally:
        ai_account._get = real


def test_a_key_whose_effective_budget_is_used_up_is_not_saved():
    """The save check reads what is left the way the trial-credit monitor does
    (ai_account.credit_left): a key with room under its own limit but none in its
    effective budget is refused, on the page and in setup mode, and nothing is saved.
    A budget with room left, or a malformed one, changes nothing (shapes from a live
    read, made-up amounts)."""
    def record(budget):
        return {"data": {"label": "made-up", "limit": 5, "limit_remaining": 4.0, "usage": 1.0,
                         "effective_budget": budget, "budgets": [budget]}}

    spent = {"limit_usd": 2.0, "spend_usd": 2.0, "remaining_usd": 0.0, "reset_interval": "monthly",
             "resets_at": "2026-12-01T00:00:00Z", "scope": "key", "include_byok": False}
    real = ai_account._get

    async def run():
        home, _, _ = _setup()
        config_ui.check_key = ai_account.check_key  # the real check, its request faked
        ai_account._get = Provider((200, record(spent)))
        auth = {"authorization": f"Bearer {TOKEN}"}
        async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
            r = await c.put("/api/config", headers=auth,
                            json={"store": "brain_env", "values": {"LLM_API_KEY": FAKE_KEY}})
        assert r.status_code == 400 and r.json()["key_check"] == {
            "state": ai_account.REFUSED, "reason": ai_account.NO_CREDIT}, r.text
        assert not os.path.exists(os.path.join(home, "llm_key"))
        got = await ai_account.adopt(FAKE_KEY, "https://openrouter.ai/api/v1")
        assert got.state == ai_account.REFUSED and got.reason == ai_account.NO_CREDIT, got
        assert not os.path.exists(os.path.join(home, "llm_key"))
        for budget in ({**spent, "spend_usd": 0.5, "remaining_usd": 1.5}, {"limit_usd": "2", "remaining_usd": "0"}):
            ai_account._get = Provider((200, record(budget)))
            assert ai_account.check_key(FAKE_KEY, "https://openrouter.ai/api/v1").state == ai_account.WORKS, budget

    try:
        asyncio.run(run())
    finally:
        ai_account._get = real


# ------------------------------------------------------------------ the voice's other ways

def test_without_a_screen_the_address_and_code_are_read_out():
    async def run():
        _setup()
        v = Voice(Screen(qr=False))
        assert await v._heard("open the settings page")
        code = settings_access._live.code.code
        line = v.said[-1]
        assert "teaport dash nine E three five dot local slash settings" in line, line
        assert wifi_voice.spell(code.lower(), i18n.get("en")) in line and "capital" not in line
    asyncio.run(run())


def test_offline_or_without_a_front_door_it_says_so_and_talk_is_not_the_phrase():
    async def run():
        _setup()
        v = Voice(Screen(), online=False)
        assert await v._heard("open my settings")
        assert v.said == [settings_access.OFFLINE_TEXT] and settings_access._live is None
        _setup(host="")
        v = Voice(Screen())
        assert await v._heard("open my settings")
        assert v.said == [settings_access.UNAVAILABLE_TEXT]
        _setup()
        v = Voice(Screen())
        for talk in ("How do I open my settings?", "open the settings on my phone",
                     "my phone settings are a mess", "can you please go and open my settings for me now",
                     "I opened my settings yesterday"):
            assert not await v._heard(talk), talk
        assert v.said == []
    asyncio.run(run())


def test_the_no_model_line_points_at_the_phrase_where_it_is_heard():
    async def run():
        s = llm_error_speaker.LLMErrorSpeaker(no_model=True, settings=True)
        assert await s._no_model_line() == llm_error_speaker.NO_MODEL_SETTINGS_TEXT
        s = llm_error_speaker.LLMErrorSpeaker(no_model=True)
        assert await s._no_model_line() == llm_error_speaker.NO_MODEL_TEXT
    asyncio.run(run())


def test_a_settings_code_never_reaches_the_access_log():
    assert gateway_server._redact('"GET /settings/AB3CD9 HTTP/1.1"') == '"GET /settings/<redacted> HTTP/1.1"'
    for path in ("/settings/config", "/settings/api/config", "/settings/health", "/settings/trust"):
        assert gateway_server._redact(f'"GET {path} HTTP/1.1"') == f'"GET {path} HTTP/1.1"'


def test_no_token_does_not_open_the_front_door():
    """An unset GATEWAY_TOKEN leaves the brain's own port open to the box itself, never
    the front door's mount: there only a Settings session counts (#279 review)."""
    async def run():
        _setup()
        os.environ["GATEWAY_TOKEN"] = ""
        body = {"store": "brain_env", "values": {"TTS_VOICE": "af_bella"}}
        async with _client() as c:
            assert (await c.get("/settings/api/config")).status_code == 401
            assert (await c.put("/settings/api/config", json=body)).status_code == 401
            assert (await c.post("/settings/api/restart", json={"unit": "teaport-brain"})).status_code == 401
            assert (await c.post("/settings/api/wifi/connect", json={"ssid": "x"})).status_code == 401
        async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
            assert (await c.get("/api/config")).status_code == 200  # the box itself, open
        screen = Screen()
        await Voice(screen)._heard("open my settings")
        async with _client() as c:
            session = await _signed_in(c, _code_of(screen))
        async with _client(cookie=session) as c:
            assert (await c.put("/settings/api/config", json=body)).status_code == 200
    asyncio.run(run())


def test_the_brains_own_port_serves_nothing_under_settings():
    """Every /settings route, whoever mounted it, answers only through the Settings site:
    on the brain's LAN port the page shell, its API, /settings/health and the code pages
    are a 404 (a live check found /settings/config served the shell there), and /config
    there answers only the box itself."""
    async def run():
        _setup()
        app = gateway_server.app  # the brain's own app: every router it mounts, the guard
        lan = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.1.50", 40000)),
                                base_url="http://teaport.local:7861")
        site = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
                                 base_url="http://box",
                                 headers={"X-Forwarded-Proto": "https", "Host": f"{HOST}:{PORT}"})
        async with lan, site:
            for path in ("/settings/config", "/settings/config/logo.svg",
                         "/settings/config/logo-dark.svg", "/settings/api/config",
                         "/settings/api/account", "/settings/health", "/settings/trust",
                         "/settings", "/settings/AB3CD9"):
                r = await lan.get(path)
                assert r.status_code == 404 and "<html" not in r.text.lower(), (path, r.status_code)
            for path in ("/config", "/config/logo.svg", "/config/logo-dark.svg", "/api/config", "/api/account"):
                r = await lan.get(path)
                assert r.status_code == 403 and "<html" not in r.text.lower(), (path, r.status_code)
            assert f"{ORIGIN}/settings/config" in (await lan.get("/config")).json()["detail"]
            # A loopback peer that does not say https (no Caddy) is no Settings site either.
            r = await lan.get("/settings/config", headers={"Host": f"{HOST}:{PORT}"})
            assert r.status_code == 404
            # Through the Settings site: the shell, and the API wants a session.
            r = await site.get("/settings/config")
            assert r.status_code == 200 and "<html" in r.text.lower()
            assert r.headers["x-frame-options"] == "DENY"
            assert (await site.get("/settings/api/config")).status_code == 401
    asyncio.run(run())


def test_the_operator_page_answers_only_the_box_itself():
    """http://<box>:7861/config is plain HTTP: the token it asks for would cross the LAN
    in cleartext, and an unset one would open it to the LAN. Only a loopback peer no
    proxy forwarded gets it -- with no token set too; /talk/status, which shares the
    token check, stays on the LAN for the room bridge."""
    async def run():
        _setup()
        app = gateway_server.app

        def peer(host, **headers):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 40000)),
                                     base_url="http://127.0.0.1:7861", headers=headers)
        auth = {"authorization": f"Bearer {TOKEN}"}
        async with peer("127.0.0.1") as box, peer("::1") as box6:
            assert (await box.get("/config")).status_code == 200
            assert (await box6.get("/config")).status_code == 200
            assert (await box.get("/api/config")).status_code == 401  # still the token
            assert (await box.get("/api/config", headers=auth)).status_code == 200
        async with peer("192.168.1.50") as lan:
            r = await lan.get("/api/config", headers=auth)  # the right token, the wrong way
            assert r.status_code == 403, r.text
            assert (await lan.post("/api/restart", headers=auth, json={"unit": "teaport-brain"})).status_code == 403
            assert (await lan.get("/talk/status", headers=auth)).status_code == 200
        async with peer("127.0.0.1", Host="localhost:7861") as box, peer("::1", Host="[::1]:7861") as box6:
            assert (await box.get("/api/config", headers=auth)).status_code == 200
            assert (await box6.get("/api/config", headers=auth)).status_code == 200
        # A name rebound to 127.0.0.1 by a page in a browser on the box (DNS rebinding).
        async with peer("127.0.0.1", Host="evil.example:7861") as rebound:
            assert (await rebound.get("/api/config", headers=auth)).status_code == 403
        # A loopback peer a proxy forwarded is not the box itself.
        async with peer("127.0.0.1", **{"X-Forwarded-For": "192.168.1.50"}) as fwd:
            assert (await fwd.get("/api/config", headers=auth)).status_code == 403
        async with peer("127.0.0.1", **{"X-Forwarded-Proto": "http"}) as fwd:
            assert (await fwd.get("/api/config", headers=auth)).status_code == 403
        # No token set: open on the box, still nothing for the LAN.
        os.environ["GATEWAY_TOKEN"] = ""
        try:
            async with peer("127.0.0.1") as box, peer("192.168.1.50") as lan:
                assert (await box.get("/api/config")).status_code == 200
                assert (await lan.get("/api/config")).status_code == 403
        finally:
            os.environ["GATEWAY_TOKEN"] = TOKEN
    asyncio.run(run())


def test_the_operator_token_opens_settings_over_https():
    """The operator's way from another device: the token as a bearer header on the
    Settings site, beside the owner's session. Not as ?token= (it would sit in URLs), not
    a wrong one, not from another origin, and an unset token opens nothing."""
    async def run():
        _setup()
        auth = {"authorization": f"Bearer {TOKEN}"}
        put = {"store": "brain_env", "values": {"LLM_BASE_URL": "https://example.test/v1"}}
        async with _client() as c:
            assert (await c.get("/settings/api/config", headers=auth)).status_code == 200
            assert (await c.put("/settings/api/config", headers=auth, json=put)).status_code == 200
            assert (await c.get(f"/settings/api/config?token={TOKEN}")).status_code == 401
            assert (await c.get("/settings/api/config",
                                headers={"authorization": "Bearer wrong"})).status_code == 401
        async with _client(fetch_site="same-site", origin=f"https://{HOST}") as c:
            assert (await c.get("/settings/api/config", headers=auth)).status_code == 401
            assert (await c.put("/settings/api/config", headers=auth, json=put)).status_code == 401
        async with _client(port=443, origin=f"https://{HOST}") as c:
            assert (await c.get("/settings/api/config", headers=auth)).status_code == 404
        os.environ["GATEWAY_TOKEN"] = ""
        try:
            async with _client() as c:
                assert (await c.get("/settings/api/config",
                                    headers={"authorization": "Bearer "})).status_code == 401
        finally:
            os.environ["GATEWAY_TOKEN"] = TOKEN
    asyncio.run(run())


def test_settings_answers_only_on_its_own_origin():
    """The gateway's origin (443) shares the site, not the origin: a request Caddy handed
    over from there, or one a browser marks cross-origin, gets nothing."""
    async def run():
        _setup()
        screen = Screen()
        await Voice(screen)._heard("open my settings")
        code = _code_of(screen)
        async with _client(port=443, origin=f"https://{HOST}") as c:
            assert (await c.get(f"/settings/{code}")).status_code == 404
        async with _client() as c:
            session = await _signed_in(c, code)
        async with _client(cookie=session, port=443, origin=f"https://{HOST}") as c:
            assert (await c.get("/settings/api/config")).status_code == 404  # another site
        async with _client(cookie=session, fetch_site="same-site", origin=f"https://{HOST}") as c:
            assert (await c.get("/settings/api/config")).status_code == 401
            assert (await c.put("/settings/api/config", json={"store": "brain_env", "values": {
                "LLM_BASE_URL": "https://elsewhere.example/v1"}})).status_code == 401
        async with _client(cookie=session, fetch_site="same-origin") as c:
            assert (await c.get("/settings/api/config")).status_code == 200
    asyncio.run(run())


def test_a_key_in_the_environment_is_handled_the_same_on_both_pages():
    """brain.env's copy: the page drops it and this brain forgets its own, so the saved
    key is the one used next. A copy from elsewhere: neither page saves (#279 review)."""
    async def run():
        home, check, _ = _setup()
        etc = config_ui.ETC_DIR
        with open(os.path.join(etc, "brain.env"), "a") as f:
            f.write("LLM_API_KEY=sk-old-made-up\n")
        os.environ["LLM_API_KEY"] = "sk-old-made-up"
        auth = {"authorization": f"Bearer {TOKEN}"}
        put = {"store": "brain_env", "values": {"LLM_API_KEY": FAKE_KEY}}
        async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
            r = await c.put("/api/config", json=put, headers=auth)
            assert r.status_code == 200 and r.json()["superseded"] == ["LLM_API_KEY"]
            assert "LLM_API_KEY" not in os.environ
            assert open(os.path.join(home, "llm_key")).read() == FAKE_KEY + "\n"
            assert "LLM_API_KEY" not in open(os.path.join(etc, "brain.env")).read()
            os.environ["LLM_API_KEY"] = "sk-from-a-drop-in"  # not brain.env's
            r = await c.put("/api/config", json={**put, "force_key": True}, headers=auth)
            assert r.status_code == 400 and r.json()["key_check"]["reason"] == ai_account.SET_ELSEWHERE
        assert (await ai_account.adopt(FAKE_KEY, "https://openrouter.ai/api/v1")).reason == ai_account.SET_ELSEWHERE
        os.environ.pop("LLM_API_KEY")
    asyncio.run(run())


def test_the_key_check_follows_no_redirect_and_keeps_its_deadline():
    import http.server
    import threading as th
    import time as tm
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/v1/models"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{srv.server_port}/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = ai_account.check_key(FAKE_KEY, f"http://127.0.0.1:{srv.server_port}/v1")
        assert got == ai_account.Check(ai_account.UNKNOWN, ai_account.UNREACHABLE), got
        assert [p for p, _ in seen] == ["/v1/models"]  # the redirect was not followed
    finally:
        srv.shutdown()
    real = ai_account._request
    ai_account._request = lambda url, key, timeout: tm.sleep(5)  # a lookup that stalls
    try:
        t0 = tm.monotonic()
        got = ai_account.check_key(FAKE_KEY, "https://openrouter.ai/api/v1", timeout=0.3)
        assert got.state == ai_account.UNKNOWN and tm.monotonic() - t0 < 2
    finally:
        ai_account._request = real


def test_nothing_may_frame_settings():
    """Clickjacking: a gateway page is same-site, so a frame of Settings would carry the
    cookie. Every answer under /settings forbids framing; the rest of the brain is as it was."""
    async def run():
        _setup()
        screen = Screen()
        await Voice(screen)._heard("open my settings")
        code = _code_of(screen)
        async with _client() as c:
            for r in (await c.get("/settings"), await c.get(f"/settings/{code}"),
                      await c.get("/settings/trust"), await c.get("/settings/api/config"),
                      await c.get("/settings/config")):
                assert r.headers["content-security-policy"] == "frame-ancestors 'none'", r.url
                assert r.headers["x-frame-options"] == "DENY", r.url
        async with _client(proto=None, origin=None, port=7861, host="127.0.0.1") as c:
            r = await c.get("/config")
            assert "x-frame-options" not in r.headers
    asyncio.run(run())


def test_a_brain_only_update_without_the_settings_site_says_so():
    """`install.sh --only brain` renders no Caddy site and writes no TEAPORT_SETTINGS_PORT:
    the phrase says Settings cannot be reached rather than show a QR code to a port
    nothing listens on, and no line points the owner at it."""
    async def run():
        _setup(port=None)
        screen = Screen()
        v = Voice(screen)
        assert await v._heard("open my settings")
        assert v.said == [settings_access.UNAVAILABLE_TEXT] and screen.shown == []
        assert not settings_access.available() and settings_access.heard_here()
    asyncio.run(run())


def test_a_code_read_aloud_never_reaches_the_journal():
    from loguru import logger
    from teaport_brain import conversation_log
    async def run():
        _setup()
        v = Voice(Screen(qr=False))
        await v._heard("open my settings")
        return wifi_voice.spell(settings_access._live.code.code.lower(), i18n.get("en"))
    spelled = asyncio.run(run())
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    logger.configure(patcher=conversation_log.redact)
    old = conversation_log.LOG_WORDS
    conversation_log.LOG_WORDS = True  # even with the conversation in the journal
    try:
        logger.bind().debug(f"Generating TTS [go there, and enter the code {spelled}. It works once]")
        parts = spelled.split(", ")
        logger.debug(f"clause={', '.join(parts[1:4])!r}")  # a clause cut out of the line
    finally:
        conversation_log.LOG_WORDS = old
        logger.remove(sink)
        logger.configure(patcher=None)
    text = "".join(lines)
    assert spelled not in text and ", ".join(parts[1:4]) not in text, text
    assert "<code>" in text
    # Only the code: two items in a row, or letters inside words, are anyone's text; and
    # a code past its life is forgotten.
    record = {"message": f"DATA, BASE {parts[1]}, {parts[2]}", "name": "x"}
    conversation_log.redact(record)
    assert record["message"] == f"DATA, BASE {parts[1]}, {parts[2]}", record
    conversation_log.never_log("q, r, s", sep=", ", secs=-1)
    record = {"message": "q, r, s", "name": "x"}
    conversation_log.redact(record)
    assert record["message"] == "q, r, s" and all(
        "q" not in p.pattern for _, p in conversation_log._NEVER)


# ------------------------------------------------------------------ setup mode

class NM:
    """Just enough NetworkManager for one successful join."""

    def __init__(self):
        self.calls = []

    def wifi_device(self):
        return "wlan0"

    def active(self, dev):
        return None

    def scan(self, dev):
        return [{"ssid": "Home", "signal": 80, "open": False}]

    def ap_up(self, dev, ssid, password):
        return True, ""

    def addresses(self, dev):
        return [wifi_setup.AP_ADDRESS]

    def ap_down(self):
        pass

    def ap_delete(self):
        pass

    def undo(self):
        pass

    def join(self, dev, target, pw, hidden):
        return True, "", None

    def online(self, dev):
        return True


def test_the_setup_page_takes_the_key_and_adopts_it_once_online():
    home, _, _ = _setup()
    en = i18n.get("en")
    page = wifi_setup.render_page([{"ssid": "Home", "signal": 80, "open": False}], "", en, account=True)
    assert 'name="ai_key"' in page and "OpenRouter key" in page
    assert 'name="ai_key"' not in wifi_setup.render_page([], "", en)
    assert wifi_setup.parse_key(b"ssid=Home&password=12345678&ai_key=+sk-or-v1-x+") == "sk-or-v1-x"
    real = ai_account._get
    try:
        for answer, result in (((200, {"data": {"limit_remaining": 4.0}}), "saved"),
                               ((401, {"error": {"message": "User not found."}}), "refused")):
            key_file = ai_account.key_path()
            if os.path.exists(key_file):
                os.remove(key_file)
            ai_account._get = Provider(answer)
            status_dir = tempdir("setup-status-")
            setup = wifi_setup.Setup(NM(), wifi_setup.Status(status_dir, "r1"), minutes=1,
                                     bind="127.0.0.1", account="https://openrouter.ai/api/v1")
            setup.previous = None

            def post():  # what the page's POST handler does
                setup.key = wifi_setup.parse_key(f"ssid=Home&ai_key={FAKE_KEY}".encode())
                setup.request = ("Home", "12345678", False)
                setup.wake.set()

            original_sleep = wifi_setup.time.sleep
            wifi_setup.time.sleep = lambda s: None
            timer = threading.Timer(0.2, post)
            try:
                setup.server = object()  # no page server: the POST comes from the timer
                setup.publish = lambda name: None
                timer.start()
                assert setup._run("wlan0", "teaport-9e35", "47190352") == 0
            finally:
                timer.cancel()
                wifi_setup.time.sleep = original_sleep
            status = setup.status.data
            assert status["phase"] == "connected" and status["account"]["result"] == result, status
            assert FAKE_KEY not in json.dumps(status) and setup.key == ""
            assert os.path.exists(key_file) == (result == "saved")
    finally:
        ai_account._get = real
    # An env-file copy of the key wins over the file: setup mode does not pretend.
    os.environ["LLM_API_KEY"] = "sk-set-by-the-installer"
    try:
        assert ai_account.adopt_blocking(FAKE_KEY, "https://openrouter.ai/api/v1").reason == ai_account.SET_ELSEWHERE
    finally:
        os.environ.pop("LLM_API_KEY")


def test_the_phrase_is_pointed_at_only_where_it_works():
    """One test, settings_access.usable, for every line that says "open my settings": the
    phrase heard in that session (not a bridge, not a phone call) and the Settings site
    set up (#279 review). The CLI asks the same of brain.env."""
    _setup()
    phrase = settings_access.SettingsPhrase()
    assert settings_access.usable([phrase])
    assert not settings_access.usable([None]) and not settings_access.usable([])  # a bridge
    _setup(port=None)
    assert not settings_access.usable([phrase])                                   # no site
    from test_llm_account_error import _cli
    answers = {"state": "unconfigured", "reason": "no key"}
    site = "TEAPORT_FRONTDOOR_HOST=teaport-9e35.local\nTEAPORT_SETTINGS_PORT=8443\n"
    for env, says in ((site, True), ("TEAPORT_FRONTDOOR_HOST=teaport-9e35.local\n", False)):
        status = [ln for ln in _cli("cmd_status", {"ok": True, "answers": answers}, env).splitlines()
                  if "answers (brain)" in ln][0]
        assert ("open my settings" in status) == says, status
        doctor = [ln for ln in _cli("cmd_doctor", {"ok": True}, env).splitlines()
                  if "no LLM key" in ln][0]
        assert ("open my settings" in doctor) == says, doctor


def test_the_voice_says_how_the_key_went():
    async def run():
        en = i18n.get("en")
        _setup()
        refused = {"result": "refused", "reason": "User not found", "ours": False}
        for account, line, phrases in (
                ({"result": "saved", "reason": "", "ours": True}, wifi_voice.ACCOUNT_SAVED_TEXT, ()),
                ({"result": "saved", "reason": "", "ours": True, "no_credit": True},
                 wifi_voice.ACCOUNT_NO_CREDIT_TEXT, ()),
                (refused, wifi_voice.ACCOUNT_NOT_SAVED_TEXT, [settings_access.SettingsPhrase()]),
                (refused, wifi_voice.ACCOUNT_NOT_SAVED_PLAIN_TEXT, ()),  # the phrase not heard
                (None, None, ())):
            v = wifi_voice.WifiSetupVoice(run=lambda *a: None, phrases=phrases,
                                          status_path=os.path.join(tempdir(), "s.json"))
            said = []

            async def push(frame, direction=FrameDirection.DOWNSTREAM):
                if isinstance(frame, TTSSpeakFrame):
                    said.append(frame.text)

            v.push_frame = push
            v.state = "running"
            await v._announce({"phase": "connected", "target": "Home",
                               **({"account": account} if account else {})})
            assert said[0].startswith("I'm connected to Home"), said
            assert (en._(line) in said[0]) if line else said[0].endswith("online again."), said
    asyncio.run(run())


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
