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
    ai_account, config_ui, i18n, llm_error_speaker, pairing, settings_access, wifi_setup,
    wifi_voice,
)
from teaport_brain import gateway_server  # noqa: E402

HOST = "teaport-9e35.local"
ORIGIN = f"https://{HOST}"
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
            async def box_online():
                return online
            self.box_online = box_online

    @property
    def said(self):
        return [f.text for f in self.pushed if isinstance(f, TTSSpeakFrame)]

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)


def _setup(host=HOST):
    pairing.FRONTDOOR_HOST = host
    pairing.TRUSTED_CERT = False
    settings_access._offer = None
    settings_access._dead = False
    settings_access._wrong = 0
    settings_access._spent.clear()
    settings_access._recent.clear()
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

    def check(key, base_url, timeout=None):
        checks.append((key, base_url))
        return check.answer

    check.answer = ai_account.Check(ai_account.WORKS)
    config_ui.check_key = check
    return home, check, checks


def _client(proto="https", origin=ORIGIN, cookie=None, lang=None):
    """Through the front door as Caddy is one: a loopback peer saying https."""
    app = FastAPI()
    app.include_router(config_ui.router)
    app.include_router(config_ui.router, prefix="/settings")
    app.include_router(settings_access.router)
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    if origin:
        headers["Origin"] = origin
    if lang:
        headers["Accept-Language"] = lang
    cookies = {settings_access.COOKIE: cookie} if cookie else None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
                             base_url="http://box", headers=headers, cookies=cookies)


def _code_of(screen):
    link = screen.shown[-1][3]
    assert link.startswith(f"https://{HOST}/settings/"), link
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
            account = (await c.get("/settings/api/account")).json()
            assert account["offered"] is True and account["key_set"] is False
            assert account["text"]["title"] == "AI account"
            put = await c.put("/settings/api/config", json={"store": "brain_env",
                                                            "values": {"LLM_API_KEY": FAKE_KEY}})
            assert put.status_code == 200, put.text
            assert (await c.get("/settings")).headers["location"] == "/settings/config"
        # The token keeps working, on the brain's own port too.
        async with _client(proto=None, origin=None) as c:
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
            assert (await c.get("/settings/api/config")).status_code == 401
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
        async with _client(proto=None, origin=None, lang="es-MX,es;q=0.9") as c:
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
    """ai_account._get, faked: (status, body) or an exception."""

    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def __call__(self, url, key, timeout):
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        status, body = self.answer
        return status, json.dumps(body).encode() if not isinstance(body, bytes) else body


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
        assert ai_account.check_key(FAKE_KEY, "http://127.0.0.1:8182/v1/").state == ai_account.WORKS
        assert p.urls == ["http://127.0.0.1:8182/v1/models"]
        assert ai_account.check_key("  ", base) == ai_account.Check(ai_account.REFUSED, ai_account.EMPTY)
        assert ai_account.check_key("sk-a\nsk-b", base).reason == ai_account.NOT_A_KEY
        assert ai_account.check_key(FAKE_KEY, "") == ai_account.Check(ai_account.UNKNOWN, ai_account.NO_SERVICE)
    finally:
        ai_account._get = real


# ------------------------------------------------------------------ the voice's other ways

def test_without_a_screen_the_address_and_code_are_read_out():
    async def run():
        _setup()
        v = Voice(Screen(qr=False))
        assert await v._heard("open the settings page")
        code = settings_access._offer.code
        line = v.said[-1]
        assert "teaport dash nine E three five dot local slash settings" in line, line
        assert wifi_voice.spell(code.lower(), i18n.get("en")) in line and "capital" not in line
    asyncio.run(run())


def test_offline_or_without_a_front_door_it_says_so_and_talk_is_not_the_phrase():
    async def run():
        _setup()
        v = Voice(Screen(), online=False)
        assert await v._heard("open my settings")
        assert v.said == [settings_access.OFFLINE_TEXT] and settings_access._offer is None
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
        assert ai_account.adopt(FAKE_KEY, "https://openrouter.ai/api/v1").reason == ai_account.SET_ELSEWHERE
    finally:
        os.environ.pop("LLM_API_KEY")


def test_the_voice_says_how_the_key_went():
    async def run():
        en = i18n.get("en")
        for account, line in (({"result": "saved", "reason": "", "ours": True}, wifi_voice.ACCOUNT_SAVED_TEXT),
                              ({"result": "refused", "reason": "User not found", "ours": False},
                               wifi_voice.ACCOUNT_NOT_SAVED_TEXT),
                              (None, None)):
            v = wifi_voice.WifiSetupVoice(run=lambda *a: None, status_path=os.path.join(tempdir(), "s.json"))
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
