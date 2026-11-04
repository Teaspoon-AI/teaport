#
# "Connect your OpenRouter account" (#282): OpenRouter's sign-in (OAuth PKCE) from the
# owner's Settings, through Teaspoon's static page (web/connect/index.html) and back.
#
# What has to hold:
#   - The button is offered on the Settings site only, for an OpenRouter endpoint, and
#     only while the static page answers as itself (asked from the box, the answer kept
#     for a while); otherwise the AI account step says so and the paste field stays.
#   - Start (the owner's session) gives OpenRouter's sign-in address: the static page as
#     callback_url, an S256 challenge of a verifier only the box holds, a state that
#     carries the Settings origin and a nonce under the box's signature; and a fresh
#     session cookie.
#   - /settings/openrouter: a GET (from the static page, no cookie: SameSite=Strict) shows
#     a Finish button and spends nothing; its POST needs the session and the Settings
#     origin, a state this box signed for its own origin and the live nonce. It is spent
#     there, once, whatever comes of it; the code and verifier are exchanged; the key is
#     checked and saved as a pasted one is (the key file, the starter warnings' end).
#   - A refused sign-in, a refused key, a late or replayed finish, a forged state: a plain
#     message, nothing saved, paste still there. The owner saying no at OpenRouter: a
#     plain message.
#   - The code the static page shows, pasted in Settings, finishes the same sign-in.
#   - ai_account.exchange_code: the documented request, the key from the answer, plain
#     reasons, no redirect followed.
#   - No key, code or verifier in the journal or the access log.
#   - The static page: a strict CSP whose hashes match its inline script and style, no
#     third-party anything, the box's path, and it goes on only to a home address.
#   - The setup page points at Settings for the sign-in (setup mode has no internet).
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
import subprocess
import sys
import urllib.parse

import httpx
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_settings_access as sa  # noqa: E402  (its pinned-pipecat check runs first)

import appliance  # noqa: E402
from loguru import logger  # noqa: E402
from teaport_brain import (  # noqa: E402
    ai_account, config_ui, gateway_server, i18n, openrouter_connect, settings_access, wifi_setup,
)

FAKE_KEY = "sk-or-v1-" + "7" * 64          # what the made-up exchange hands back
FAKE_CODE = "c0de-" + "a1" * 12             # a made-up sign-in code
PAGE = (b'<!doctype html><html><head>' + openrouter_connect.PAGE_MARK
        + b'</head><body></body></html>')
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web", "connect",
                   "index.html")


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
    openrouter_connect._clock = lambda: 5000.0
    exchanged = []

    def exchange(code, verifier, timeout=None):
        exchanged.append((code, verifier))
        return exchange.answer

    exchange.answer = (FAKE_KEY, "")
    openrouter_connect.exchange = exchange
    return home, check, checks, fetched, exchanged


def _client(cookie=None, origin=sa.ORIGIN, fetch_site=None, proto="https"):
    """Through the Settings site as Caddy is one, with this module's routes mounted as
    gateway_server mounts them: before Settings' /settings/<code>."""
    app = FastAPI()
    app.include_router(config_ui.router)
    app.include_router(config_ui.router, prefix="/settings")
    app.include_router(openrouter_connect.router)
    app.include_router(settings_access.router)
    app.add_middleware(settings_access.SettingsGuard)
    headers = {"Host": f"{sa.HOST}:{sa.PORT}"}
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


# ------------------------------------------------------------------ the whole round trip

def test_the_owner_connects_and_the_key_is_saved_as_a_pasted_one():
    async def run():
        home, check, checks, fetched, exchanged = _setup()
        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
        try:
            async with _client(cookie=_session()) as c:
                account = (await c.get("/settings/api/account")).json()
                assert account["connect"] == {"offered": True, "pending": False}
                assert account["text"]["connect"] == "Connect your OpenRouter account"
                r = await c.post("/settings/api/openrouter/start")
                cookie = r.headers["set-cookie"].lower()
                for part in ("httponly", "secure", "samesite=strict", "path=/settings"):
                    assert part in cookie, cookie  # a fresh hour for the round trip
                assert r.headers["cache-control"] == "no-store"
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.json()["url"]).query))
            pending = openrouter_connect._pending
            assert q["callback_url"] == "https://get.teaspoon.tech/connect/"
            assert q["code_challenge_method"] == "S256" and q["key_label"] == "Teaport"
            want = base64.urlsafe_b64encode(hashlib.sha256(pending.verifier.encode()).digest())
            assert q["code_challenge"] == want.rstrip(b"=").decode()
            assert 43 <= len(pending.verifier) <= 128 and pending.verifier not in r.text
            head, nonce, _sig = q["state"].split(".")
            assert openrouter_connect._unb64(head).decode() == sa.ORIGIN and nonce == pending.nonce
            async with _client(cookie=_session()) as c:
                assert (await c.get("/settings/api/account")).json()["connect"]["pending"] is True
            # The static page sends the phone here: another site's navigation, so no cookie.
            landing = f"/settings/openrouter?code={FAKE_CODE}&state={urllib.parse.quote(q['state'])}"
            async with _client(origin=None, fetch_site="cross-site") as c:
                for _ in range(2):  # a GET spends nothing
                    got = await c.get(landing)
                    assert got.status_code == 200 and "Finish" in got.text, got.text
                    assert got.headers["cache-control"] == "no-store"
                    assert got.headers["referrer-policy"] == "no-referrer"
            assert openrouter_connect._pending is pending and exchanged == []
            # Its button: the Settings page's own POST, with the session.
            async with _client(cookie=_session(), fetch_site="same-origin") as c:
                done = await c.post("/settings/openrouter", data={"code": FAKE_CODE, "state": q["state"]})
                assert done.status_code == 200, done.text
                assert "Your OpenRouter account is connected" in done.text
                assert exchanged == [(FAKE_CODE, pending.verifier)]
                assert checks[-1] == (FAKE_KEY, "https://openrouter.ai/api/v1")
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


def test_every_failure_is_said_plainly_and_saves_nothing():
    async def run():
        home, check, _checks, _fetched, exchanged = _setup()
        en = i18n.get("en")
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
            assert words.replace("'", "&#x27;") in r.text and "paste a key" in r.text, r.text
            assert f"{words.replace(chr(39), '&#x27;')}. You can try again" in r.text, r.text
            assert _key_file(home) is None
            assert openrouter_connect._pending is None  # spent: a code works once
        # The owner said no at OpenRouter (no code comes back).
        async with _client(origin=None) as c:
            r = await c.get("/settings/openrouter")
            assert r.status_code == 200 and en._(openrouter_connect.NOT_CONNECTED_TEXT).replace(
                "'", "&#x27;") in r.text
        assert len(exchanged) == len(cases)  # one each; the "no" page exchanged nothing
    asyncio.run(run())


def test_the_code_the_static_page_shows_can_be_pasted_in_settings():
    async def run():
        home, *_rest, exchanged = _setup()
        async with _client(cookie=_session()) as c:
            r = await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})
            assert r.status_code == 400 and r.json()["ok"] is False and "expired" in r.json()["message"]
            await _start(c)
            verifier = openrouter_connect._pending.verifier
            r = await c.post("/settings/api/openrouter/code", json={"code": f"  {FAKE_CODE} "})
            assert r.status_code == 200 and r.json() == {
                "ok": True, "message": i18n.get("en")._(openrouter_connect.CONNECTED_TEXT)}
            assert exchanged == [(f"  {FAKE_CODE} ", verifier)] and _key_file(home) == FAKE_KEY
        async with _client() as c:  # the session, as everywhere in Settings
            assert (await c.post("/settings/api/openrouter/code", json={"code": FAKE_CODE})).status_code == 401
    asyncio.run(run())


def test_the_button_shows_only_while_the_static_page_answers_as_itself():
    async def run():
        for page in ((404, b"Not Found"), (200, b"<html>something else</html>"),
                     TimeoutError("no answer")):
            _home, _c, _cs, fetched, _ex = _setup(page=page)
            async with _client(cookie=_session()) as c:
                account = (await c.get("/settings/api/account")).json()
                assert account["connect"] == {"offered": False, "pending": False}, page
                assert account["text"]["connect_off"].startswith("Signing in with OpenRouter")
                assert (await c.post("/settings/api/openrouter/start")).status_code == 409
                # Asked again only after a while.
                await c.get("/settings/api/account")
                assert len(fetched) == 1, page
                openrouter_connect._clock = lambda: 5000.0 + openrouter_connect.PROBE_FAILED_SECS + 1
                await c.get("/settings/api/account")
                assert len(fetched) == 2, page
        # Turned off (not an https:// address): not asked at all.
        _home, _c, _cs, fetched, _ex = _setup()
        os.environ["TEAPORT_OPENROUTER_CONNECT_URL"] = "http://example.invalid/"
        try:
            async with _client(cookie=_session()) as c:
                assert (await c.get("/settings/api/account")).json()["connect"]["offered"] is False
            assert fetched == []
        finally:
            del os.environ["TEAPORT_OPENROUTER_CONNECT_URL"]
        # The operator's /config (the token, the brain's own port): the sign-in could not
        # come back there, so it is not offered, and nothing is asked.
        async with _client(origin=None, proto=None) as c:
            got = await c.get("/api/account", headers={"authorization": f"Bearer {sa.TOKEN}"})
            assert got.status_code == 200 and got.json()["connect"]["offered"] is False
        assert fetched == []
        # Not an OpenRouter endpoint: neither.
        with open(os.path.join(config_ui.ETC_DIR, "brain.env"), "w") as f:
            f.write("LLM_BASE_URL=https://api.example.invalid/v1\n")
        async with _client(cookie=_session()) as c:
            assert (await c.get("/settings/api/account")).json()["offered"] is False
            assert (await c.post("/settings/api/openrouter/start")).status_code == 409
    asyncio.run(run())


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
    import threading
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append((self.command, self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(307)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        do_GET = do_POST

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _ = ai_account._request(f"http://127.0.0.1:{srv.server_port}/keys", None, 5, b'{"a":1}')
        assert status == 307 and seen == [("POST", "/keys", b'{"a":1}')]
    finally:
        srv.shutdown()


def test_the_sign_in_never_reaches_the_access_log():
    line = f'"GET /settings/openrouter?code={FAKE_CODE}&state=abc.def.0123 HTTP/1.1"'
    assert gateway_server._redact(line) == '"GET /settings/openrouter?code=<redacted>&state=<redacted> HTTP/1.1"'
    assert gateway_server._redact('"POST /settings/api/openrouter/start HTTP/1.1"') == \
        '"POST /settings/api/openrouter/start HTTP/1.1"'


def test_gateway_server_mounts_the_routes_before_the_settings_code():
    paths = [getattr(r, "path", "") for r in gateway_server.app.routes]
    assert paths.index(openrouter_connect.LANDING_PATH) < paths.index("/settings/{code}")


# ------------------------------------------------------------------ the static page

def _web() -> str:
    with open(WEB, encoding="utf-8") as f:
        return f.read()


def _hash(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def test_the_static_page_is_strict_and_self_contained():
    page = _web()
    assert openrouter_connect.PAGE_MARK.decode() in page
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    policy = dict(p.strip().split(" ", 1) for p in csp.split(";"))
    script = re.findall(r"<script>(.*?)</script>", page, re.S)
    style = re.findall(r"<style>(.*?)</style>", page, re.S)
    assert len(script) == len(style) == 1
    assert policy == {"default-src": "'none'", "script-src": f"'sha256-{_hash(script[0])}'",
                      "style-src": f"'sha256-{_hash(style[0])}'", "base-uri": "'none'",
                      "form-action": "'none'"}, ("re-hash the page's inline blocks", policy)
    assert '<meta name="referrer" content="no-referrer">' in page
    assert not re.search(r"\b(?:src|href)=\"(?:https?:)?//", page)  # nothing from elsewhere
    assert f'var PATH = "{openrouter_connect.LANDING_PATH}";' in page
    assert "Code from OpenRouter" in page  # the field's name on the Settings page
    assert i18n.get("en")._(config_ui.ACCOUNT_TEXT["code"]) == "Code from OpenRouter"


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


# ------------------------------------------------------------------ setup mode

def test_the_setup_page_points_at_settings_for_the_sign_in():
    sa._setup()
    en = i18n.get("en")
    later = en._(wifi_setup.SIGN_IN_LATER)
    env = {"TEAPORT_FRONTDOOR_HOST": sa.HOST, "TEAPORT_SETTINGS_PORT": "8443"}
    os.environ.update(env)
    try:
        assert later.replace('"', "&quot;") in wifi_setup.render_account(en)
        os.environ["TEAPORT_OPENROUTER_CONNECT_URL"] = "http://off.invalid/"
        assert "signing in" not in wifi_setup.render_account(en)
        del os.environ["TEAPORT_OPENROUTER_CONNECT_URL"]
        del os.environ["TEAPORT_SETTINGS_PORT"]  # no Settings site (an older box)
        assert "signing in" not in wifi_setup.render_account(en)
    finally:
        for k in env:
            os.environ.pop(k, None)


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
