#
# The owner's login (#421) where the owner meets it: the setup page's "Create your login"
# step (wifi_setup.py) and the Settings card (config_ui.py), both through owner_login.py,
# here running the root helper in-process against pretend account tools. What has to hold:
#   - the step and the card are there only while a login is pending, in the reader's
#     language, and go away once it exists;
#   - on the setup page the login is made as the form arrives, before the join; a
#     password it does not take, or a create that fails, brings the page back with the
#     reason and no join; left empty, the step is skipped and the join goes on;
#   - the password reaches no log line, no status file, no page and no answer.
# All passwords here are made up for the test.
#
# Run: python test_owner_login.py   (or via pytest test_suite.py)
#
import html
import json
import os
import threading
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
    assert "Use at least 12 characters." in page
    assert 'name="login_password"' not in ws.render_page([], "", EN)
    ja = ws.render_page([], "", i18n.get("ja"), login=True)
    assert owner_login.say(i18n.get("ja"), owner_login.TEXT["title"]) in ja
    assert "Create your login" not in ja and "teaport" in ja


def test_the_form_carries_both_passwords_as_typed():
    assert ws.parse_login(b"ssid=home&login_password=+a+b+&login_again=x") == (" a b ", "x")
    assert ws.parse_login(b"ssid=home") == ("", "")


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


def test_an_empty_step_is_skipped_and_offered_later(tmp_path):
    tools, marker, login = _login(tmp_path)
    nm, pages = FakeNM([(True, "", "home")]), []
    rc, seen = _run_setup(nm, login=login, pages=pages, post=[{"ssid": "home", "password": "homepass1"}])
    assert rc == 0 and "Your login is ready" not in pages[1]
    assert "teaport" not in tools.users and marker.present() and login.pending()


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

    def post(self, **form):
        body = urllib.parse.urlencode({"ssid": "home", "password": "homepass1", **form}).encode()
        return html.unescape(urllib.request.urlopen(f"http://127.0.0.1:{self.setup.port}/connect",
                                                    body).read().decode())

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
        assert "Teaport couldn't create your login. Try again." in reply
        assert 'name="login_password"' in reply and page.setup.request is None
        assert "teaport" not in tools.users and marker.present()
        # Someone else made it meanwhile (the known window): said, and the step goes.
        tools.fail.clear()
        tools.users["teaport"] = "someone-elses"
        reply = page.post(login_password=GOOD, login_again=GOOD)
        assert "This Teaport already has a login." in reply and 'name="login_password"' not in reply
        assert tools.users["teaport"] == "someone-elses" and page.setup.request is None
        assert "Teaport is joining" in page.post()   # the Wi-Fi alone now
    finally:
        page.close()


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


def test_the_page_draws_the_card_from_the_routes():
    page = open(os.path.join(config_ui.STATIC_DIR, "config.html"), encoding="utf-8").read()
    assert '<section class="account" id="login" hidden></section>' in page
    assert "api('/api/login'" in page and "renderLogin();" in page
    assert "autocomplete: 'new-password'" in page


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
