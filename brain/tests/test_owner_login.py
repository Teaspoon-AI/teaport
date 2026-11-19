#
# The owner's login (#421) where the owner meets it: the setup page's "Create your login"
# step (wifi_setup.py) and the Settings card (config_ui.py), both through owner_login.py,
# here running the root helper in-process against pretend account tools. What has to hold:
#   - the step and the card are there only while a login is pending, in the reader's
#     language, and go away once it exists;
#   - on the setup page the step is required while a login is pending; the login is made
#     as the form arrives, before the join; a password it does not take, or a create that
#     fails, brings the page back with the reason and no join; a second tap while the
#     first is under way gets the "joining" page; a post from another site is refused;
#   - the password reaches no log line, no status file, no page and no answer.
# All passwords here are made up for the test.
#
# Run: python test_owner_login.py   (or via pytest test_suite.py)
#
import html
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from loguru import logger

from teaport_brain import config_ui, i18n, login_apply, owner_login
from teaport_brain import wifi_setup as ws
from tempdirs import tempdir
from test_login_apply import FakeTools, _box
from test_wifi_setup import FakeNM, _Recorded, _run_setup
from test_config_ui import H, setup as _config_client

GOOD = "correct-horse-battery"   # a made-up password
EN = i18n.get("en")


def _login(tmp_path, **kw):
    tools, ops, marker = _box(tmp_path, **kw)
    return tools, marker, owner_login.Login(ops=ops, marker=marker)


# ------------------------------------------------------------------ the setup page


def test_the_step_is_on_the_page_only_while_a_login_is_pending():
    page = html.unescape(ws.render_page([], "", EN, login=True))
    assert 'name="login_password"' in page and 'name="login_again"' in page
    assert page.count('autocomplete="new-password"') == 2
    assert "Create your login" in page and "Your username is teaport." in page
    assert "Use at least 12 characters." in page and "later" not in page
    assert page.count('required minlength="12"') == 2
    # The form's button goes grey on the first tap: a second one posts nothing.
    assert "onsubmit=\"this.querySelector('button[type=submit]').disabled=true\"" in page
    assert 'name="login_password"' not in ws.render_page([], "", EN)
    ja = ws.render_page([], "", i18n.get("ja"), login=True)
    assert owner_login.say(i18n.get("ja"), owner_login.TEXT["title"]) in ja
    assert "Create your login" not in ja and "teaport" in ja


def test_the_form_carries_both_passwords_as_typed_and_hidden():
    password, again = ws.parse_login(b"ssid=home&login_password=+a+b+&login_again=x")
    assert (password.reveal(), again.reveal()) == (" a b ", "x")
    assert repr(password) == str(again) == "Secret(***)"
    assert [x.reveal() for x in ws.parse_login(b"ssid=home")] == ["", ""]


def test_a_login_made_with_the_wifi_choice_comes_before_the_join(tmp_path, capsys):
    tools, marker, login = _login(tmp_path)
    nm, pages = FakeNM([(True, "", "home")]), []
    rc, seen = _run_setup(nm, login=login, pages=pages, post=[
        {"ssid": "home", "password": "homepass1", "login_password": GOOD, "login_again": GOOD}])
    assert rc == 0 and [s["phase"] for s in seen] == ["scanning", "ap_up", "joining", "connected"]
    assert 'name="login_password"' in pages[0]
    assert "Your login is ready. Sign in as teaport" in pages[1]
    assert tools.users["teaport"] == GOOD and not marker.present()
    assert ("join", "home", "homepass1", False) in nm.events   # the Wi-Fi's own password
    # Nowhere else: not in the status file's phases, nor the unit's log.
    out = capsys.readouterr()
    assert GOOD not in json.dumps(seen) + out.out + out.err
    assert "the owner's login: created" in out.out


def test_while_a_login_is_pending_the_step_is_required(tmp_path):
    tools, marker, login = _login(tmp_path)
    page = _Page(login)
    try:
        reply = page.post()
        assert "Your password needs at least 12 characters." in reply and page.setup.request is None
        assert not tools.changes() and marker.present()
    finally:
        page.close()


def test_a_helper_that_cannot_answer_shows_no_step_and_the_box_gets_online():
    login = owner_login.Login(helper=lambda verb, req: {"ok": False, "err": "sudo: a password is required"})
    nm, pages = FakeNM([(True, "", "home")]), []
    rc, _ = _run_setup(nm, login=login, pages=pages, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0 and 'name="login_password"' not in pages[0]


def test_without_a_pending_login_the_page_has_no_step(tmp_path):
    tools, marker, login = _login(tmp_path, marker=False)
    nm, pages = FakeNM([(True, "", "home")]), []
    rc, _ = _run_setup(nm, login=login, pages=pages, post=[
        {"ssid": "home", "password": "homepass1", "login_password": GOOD, "login_again": GOOD}])
    assert rc == 0 and 'name="login_password"' not in pages[0]
    assert not tools.changes()   # fields posted anyway are not acted on


class _Page:
    """The setup page's server alone (no run): what a post answers and whether it asked
    for a join."""

    def __init__(self, login):
        self.setup = ws.Setup(FakeNM([]), _Recorded(tempdir()), port=0, bind="127.0.0.1", login=login)
        self.setup.networks = [{"ssid": "home", "signal": 80, "open": False, "security": "WPA2"}]
        self.setup.login_offered = login.pending()
        self.setup.serve()

    def post(self, headers=None, **form):
        body = urllib.parse.urlencode({"ssid": "home", "password": "homepass1", **form}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.setup.port}/connect", body, headers=headers or {})
        return html.unescape(urllib.request.urlopen(req).read().decode())

    def close(self):
        self.setup.server.shutdown()


@pytest.mark.parametrize("again, password, says", [
    (GOOD + "!", GOOD, "The two passwords don't match."),
    ("", GOOD, "The two passwords don't match."),
    ("short-one", "short-one", "Your password needs at least 12 characters."),
])
def test_a_password_it_does_not_take_brings_the_page_back_and_no_join(tmp_path, capsys, again, password, says):
    tools, marker, login = _login(tmp_path)
    page = _Page(login)
    try:
        reply = page.post(login_password=password, login_again=again)
        assert says in reply and 'name="login_password"' in reply and page.setup.request is None
        assert password not in reply and not tools.changes()
        reply = page.post(login_password=GOOD, login_again=GOOD)   # fixed: on it goes
        assert "Teaport is joining" in reply and page.setup.request == ("home", "homepass1", False)
        assert tools.users["teaport"] == GOOD
    finally:
        page.close()
    out = capsys.readouterr()
    assert GOOD not in out.out + out.err and password not in out.out + out.err


def test_a_create_that_fails_keeps_the_step_and_one_that_exists_drops_it(tmp_path):
    tools, marker, login = _login(tmp_path, fail=["chpasswd"])
    page = _Page(login)
    try:
        reply = page.post(login_password=GOOD, login_again=GOOD)
        assert "Teaport couldn't create your login. You can connect now" in reply
        assert 'name="login_password"' in reply and page.setup.request is None
        assert "teaport" not in tools.users and marker.present()
        # Someone else made it meanwhile (the known window; the marker went with it): said,
        # and the step goes.
        tools.fail.clear()
        tools.users["teaport"] = "someone-elses"
        marker.remove()
        reply = page.post(login_password=GOOD, login_again=GOOD)
        assert "This Teaport already has a login." in reply and 'name="login_password"' not in reply
        assert tools.users["teaport"] == "someone-elses" and page.setup.request is None
        assert "Teaport is joining" in page.post()   # the Wi-Fi alone now
    finally:
        page.close()


def test_after_a_system_failure_the_step_can_be_skipped_for_this_run(tmp_path):
    tools, marker, login = _login(tmp_path, fail=["chpasswd"])
    page = _Page(login)
    try:
        # A password the owner can fix keeps the step required.
        reply = page.post(login_password="short-one", login_again="short-one")
        assert "Your password needs at least 12 characters." in reply and 'required minlength' in reply
        # A failure that is the system's: said, and the fields are no longer required.
        reply = page.post(login_password=GOOD, login_again=GOOD)
        assert ("Teaport couldn't create your login. You can connect now and create it later "
                "in Settings.") in reply
        assert 'name="login_password"' in reply and "required" not in reply
        # Empty now connects; the login waits for the settings (still pending).
        reply = page.post()
        assert "Teaport is joining" in reply and "Your login is ready" not in reply
        assert page.setup.request == ("home", "homepass1", False) and marker.present()
    finally:
        page.close()


def test_after_a_system_failure_a_typed_password_is_still_checked_and_tried(tmp_path):
    tools, marker, login = _login(tmp_path, fail=["chpasswd"])
    page = _Page(login)
    try:
        page.post(login_password=GOOD, login_again=GOOD)                 # the failure
        reply = page.post(login_password=GOOD, login_again=GOOD + "x")   # a typo: said
        assert "The two passwords don't match." in reply and page.setup.request is None
        tools.fail.clear()
        reply = page.post(login_password=GOOD, login_again=GOOD)         # works this time
        assert "Your login is ready" in reply and tools.users["teaport"] == GOOD
    finally:
        page.close()


class _SlowLogin(owner_login.Login):
    """A login whose create waits for `go`, then answers `result` (a helper reply)."""

    def __init__(self, result):
        self.go, self.started, self.creates = threading.Event(), threading.Event(), 0

        def helper(verb, req):
            if verb == "status":
                return {"state": "pending"}
            self.creates += 1
            self.started.set()
            self.go.wait(10)
            return result
        super().__init__(helper=helper)


def _two_taps(page, login):
    """The first tap in a thread, held in its create; the second tap meanwhile. Both
    answers, once the first is let go."""
    first = {}
    thread = threading.Thread(target=lambda: first.update(
        reply=page.post(login_password=GOOD, login_again=GOOD)))
    thread.start()
    assert login.started.wait(5)
    second = {}
    tap = threading.Thread(target=lambda: second.update(reply=page.post(login_password=GOOD, login_again=GOOD)))
    tap.start()
    time.sleep(0.3)
    assert "reply" not in second            # it waits for the first one's answer
    login.go.set()
    thread.join(10)
    tap.join(10)
    return first["reply"], second["reply"]


def test_a_second_tap_gets_the_first_ones_failure_and_nothing_joins(tmp_path):
    login = _SlowLogin({"ok": False, "why": "failed"})
    page = _Page(login)
    try:
        first, second = _two_taps(page, login)
        assert "couldn't create your login" in first and "couldn't create your login" in second
        assert "Teaport is joining" not in second and page.setup.request is None
        assert login.creates == 1
    finally:
        page.close()


def test_a_second_tap_gets_the_first_ones_success(tmp_path):
    login = _SlowLogin({"ok": True, "username": "teaport"})
    page = _Page(login)
    try:
        first, second = _two_taps(page, login)
        for reply in (first, second):
            assert "Teaport is joining" in reply and "Your login is ready" in reply
        assert page.setup.request == ("home", "homepass1", False) and login.creates == 1
        # A tap after the join is asked for: the joining page, and nothing made again.
        assert "Teaport is joining" in page.post(login_password=GOOD, login_again=GOOD)
        assert login.creates == 1
    finally:
        page.close()


def test_a_second_tap_that_waits_too_long_hears_the_login_is_still_being_made(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "POST_WAIT_SECS", 0.3)
    login = _SlowLogin({"ok": True, "username": "teaport"})
    page = _Page(login)
    try:
        thread = threading.Thread(target=lambda: page.post(login_password=GOOD, login_again=GOOD))
        thread.start()
        assert login.started.wait(5)
        reply = page.post(login_password=GOOD, login_again=GOOD)
        assert "Creating your login…" in reply and "Teaport is joining" not in reply
        login.go.set()
        thread.join(10)
    finally:
        page.close()


def _raw(page, method, host, headers=(), body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", page.setup.port, timeout=5)
    try:
        conn.putrequest(method, "/connect" if method == "POST" else "/hotspot-detect.html", skip_host=True)
        conn.putheader("Host", host)
        for k, v in headers:
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        return r.status, r.getheader("Location"), r.read().decode()
    finally:
        conn.close()


@pytest.mark.parametrize("host, own", [
    ("10.42.0.1", True), ("10.42.0.1:80", True), ("Teaport-9E35.local", True),
    ("captive.apple.com", False),            # a captive probe: sent to the page's own name
    ("connectivitycheck.gstatic.com", False),
    ("evil.example", False),                 # a name rebound to the box (dnsmasq answers all)
    ("", False),
])
def test_the_page_answers_only_under_its_own_names(tmp_path, host, own):
    tools, marker, login = _login(tmp_path)
    page = _Page(login)
    page.setup.avahi_name = "teaport-9e35"
    try:
        status, location, _ = _raw(page, "GET", host)
        assert (status, location) == ((200, None) if own else (302, "http://10.42.0.1/"))
    finally:
        page.close()


@pytest.mark.parametrize("host, headers, refused", [
    ("10.42.0.1", {"Origin": "http://evil.example"}, True),
    ("10.42.0.1", {"Origin": "https://evil.example:8443"}, True),
    ("10.42.0.1", {"Origin": "http://10.42.0.1.evil.example"}, True),
    ("10.42.0.1", {"Sec-Fetch-Site": "cross-site"}, True),
    ("10.42.0.1", {"Origin": "http://10.42.0.1", "Sec-Fetch-Site": "cross-site"}, True),
    # DNS rebinding: evil.example's page, rebound to the box, posts as its own origin.
    ("evil.example", {"Origin": "http://evil.example"}, True),
    ("evil.example", {}, True),
    # A captive probe's name is not the page's: the sheet was sent to 10.42.0.1 first.
    ("captive.apple.com", {"Origin": "http://captive.apple.com"}, True),
    ("10.42.0.1", {}, False),                                    # no Origin at all
    ("10.42.0.1", {"Origin": "null"}, False),                    # some captive browsers
    ("10.42.0.1", {"Origin": "http://10.42.0.1"}, False),
    ("10.42.0.1:80", {"Origin": "http://10.42.0.1:80"}, False),
    ("teaport-9e35.local", {"Origin": "http://Teaport-9E35.local"}, False),
    ("10.42.0.1", {"Origin": "http://10.42.0.1", "Sec-Fetch-Site": "same-origin"}, False),
])
def test_a_post_that_is_not_the_pages_own_is_refused(tmp_path, host, headers, refused):
    tools, marker, login = _login(tmp_path)
    page = _Page(login)
    page.setup.avahi_name = "teaport-9e35"
    form = urllib.parse.urlencode({"ssid": "" if not refused else "home", "password": "homepass1",
                                   "login_password": GOOD, "login_again": GOOD}).encode()
    try:
        status, _, body = _raw(page, "POST", host, [*headers.items(),
                               ("Content-Type", "application/x-www-form-urlencoded")], form)
        if refused:
            assert status == 403 and not tools.changes() and page.setup.request is None
        else:   # through to the form's own checks: a missing network is said, nothing made
            assert status == 200 and "Pick a network" in html.unescape(body) and not tools.changes()
    finally:
        page.close()


def test_the_ios_captive_sheet_gets_the_page_and_posts_to_it(tmp_path):
    # captive.apple.com is sent to the page's own name; the sheet then posts there.
    tools, marker, login = _login(tmp_path)
    page = _Page(login)
    try:
        status, location, _ = _raw(page, "GET", "captive.apple.com")
        assert status == 302 and location == "http://10.42.0.1/"
        status, _, body = _raw(page, "GET", "10.42.0.1")
        assert status == 200 and 'action="/connect' in body
        form = urllib.parse.urlencode({"ssid": "home", "password": "homepass1",
                                       "login_password": GOOD, "login_again": GOOD}).encode()
        status, _, body = _raw(page, "POST", "10.42.0.1", [("Origin", "http://10.42.0.1"),
                               ("Content-Type", "application/x-www-form-urlencoded")], form)
        assert status == 200 and "Your login is ready" in html.unescape(body)
    finally:
        page.close()


def test_a_traceback_that_prints_its_variables_never_shows_the_password(monkeypatch):
    # loguru's diagnose (TEAPORT_LOG_CONVERSATIONS=1) prints the variables of each frame.
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", diagnose=True, backtrace=True)

    class Popen:
        def __init__(self, *a, **kw):
            self.returncode = None

        def communicate(self, *a, **kw):
            raise RuntimeError("the pipe broke")

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(owner_login.root_helper.subprocess, "Popen", Popen)
    password, again = owner_login.Secret(GOOD), owner_login.Secret(GOOD)
    try:
        try:
            owner_login.Login().create(password, again)
        except RuntimeError:
            logger.exception("the login failed")
    finally:
        logger.remove(sink)
    out = "\n".join(lines)
    assert "the pipe broke" in out and "Secret(***)" in out and GOOD not in out


# ------------------------------------------------------------------ Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    client = _config_client()[0]
    tools, marker, login = _login(tmp_path)
    monkeypatch.setattr(config_ui, "_owner_login", lambda: login)
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield client, tools, marker, lines
    logger.remove(sink)


def test_the_card_is_offered_while_a_login_is_pending(settings):
    client, tools, marker, _ = settings
    assert client.get("/api/login").status_code == 401
    state = client.get("/api/login", headers=H).json()
    assert state["offered"] is True and state["username"] == "teaport"
    assert state["text"]["title"] == "Create your login"
    assert state["text"]["intro"].endswith("Your username is teaport.")
    es = client.get("/api/login", headers={**H, "accept-language": "es-MX"}).json()
    assert es["lang"] == "es" and es["text"]["title"] == owner_login.say(i18n.get("es"), owner_login.TEXT["title"])
    marker.remove()
    assert client.get("/api/login", headers=H).json()["offered"] is False


def test_the_card_creates_the_login_once_and_says_so(settings):
    client, tools, marker, lines = settings
    r = client.post("/api/login", headers=H, json={"password": GOOD, "again": GOOD + "x"}).json()
    assert r == {"ok": False, "why": "mismatch", "message": "The two passwords don't match."}
    r = client.post("/api/login", headers=H, json={"password": GOOD, "again": GOOD}).json()
    assert r["ok"] is True and r["message"].startswith("Your login is ready. Sign in as teaport")
    assert tools.users["teaport"] == GOOD
    assert client.get("/api/login", headers=H).json()["offered"] is False
    r = client.post("/api/login", headers=H, json={"password": GOOD, "again": GOOD}).json()
    assert r["ok"] is False and r["why"] == "exists" and r["message"] == "This Teaport already has a login."
    # The log has the outcomes, and never the password.
    log = "\n".join(lines)
    assert "the owner's login created from Settings" in log and "not created (exists)" in log
    assert GOOD not in log


@pytest.mark.parametrize("body", ['["a list"]', '{"password": "only-one-of-them"}',
                                  '{"password": 1, "again": 1}', "not json"])
def test_a_request_that_is_not_two_passwords_is_a_400_that_does_not_quote_it(settings, body):
    client, tools, _, lines = settings
    r = client.post("/api/login", headers={**H, "content-type": "application/json"}, content=body)
    assert r.status_code == 400 and "only-one-of-them" not in r.text
    assert not tools.changes() and "only-one-of-them" not in "\n".join(lines)


def test_a_create_already_under_way_is_said_as_progress(settings, monkeypatch):
    client = settings[0]
    busy = owner_login.Login(helper=lambda verb, req: {"ok": False, "err": "another", "why": "busy"})
    monkeypatch.setattr(config_ui, "_owner_login", lambda: busy)
    r = client.post("/api/login", headers=H, json={"password": GOOD, "again": GOOD}).json()
    assert r == {"ok": False, "why": "busy", "message": "Creating your login…"}


def test_the_page_draws_the_card_from_the_routes():
    page = open(os.path.join(config_ui.STATIC_DIR, "config.html"), encoding="utf-8").read()
    assert '<section class="account" id="login" hidden></section>' in page
    assert "api('/api/login'" in page and "renderLogin();" in page
    assert "autocomplete: 'new-password'" in page


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
