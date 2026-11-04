#
# OpenClaw uses the brain's LLM key (#281): one key, one balance.
#
# What has to hold:
#   - A key saved through the one save (ai_account.save_key: the Settings page, the
#     setup-mode page, adopt) is handed to OpenClaw with its own CLI,
#     `openclaw models auth paste-api-key --provider openrouter --agent <id>`, the key on
#     stdin and never in the arguments; the state file keeps its fingerprint, not the key.
#   - Both or neither: a CLI that fails before writing leaves the brain's key file as it
#     was (or absent, as it was); one that may have written (it said "API key saved", or
#     ran out of time) is given the previous key back when that is the key it had from
#     here, and otherwise the state says the two may differ (mismatch) until a save goes
#     through. The Settings page says the key was not saved, in the reader's language.
#   - A step after the save failing (brain.env's copy not dropped) takes the save back.
#   - No OpenClaw (TEAPORT_AGENT=none), the sharing off, an endpoint that is not
#     OpenRouter's, or no openclaw command: nothing is run, and status says why.
#   - The CLI's "Run `openclaw gateway restart`" means a restart is owed: the Restarter
#     runs `openclaw gateway restart --safe` only once nothing has been live for
#     IDLE_SECS, checked again right before, and retries a failed one later.
#   - /health "agent_key" and `teaport status` / `teaport doctor` say where it stands.
#   - No key reaches the journal: an echoing CLI's stderr is logged redacted.
#
# No test runs a real OpenClaw: the CLI is a fake script (FAKE_CLI) that records what it
# was given. Every key here is made up, built at runtime.
#
# Run: python test_agent_key.py   (or via pytest)
#
import asyncio
import json
import os
import stat
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

import appliance
from tempdirs import tempdir
from teaport_brain import agent_backend, agent_key, ai_account, config_ui, i18n, openclaw_client

OPENROUTER = "https://openrouter.ai/api/v1"
OLD = "sk-or-v1-" + "1" * 64
NEW = "sk-or-v1-" + "2" * 64

# The fake `openclaw`: one line of $FAKE_OPENCLAW_DIR/modes per run says how it behaves.
FAKE_CLI = r"""#!/bin/bash
d="$FAKE_OPENCLAW_DIR"
n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$d/n"
printf '%s\n' "$*" >> "$d/argv"
mode=$(sed -n "${n}p" "$d/modes" 2>/dev/null); [ -n "$mode" ] || mode=ok
key=""
if [ "$1" = models ]; then key=$(cat); printf '%s\n' "$key" >> "$d/keys"; fi
case "$mode" in
  ok) echo "Auth profile: openrouter:manual (openrouter/api_key)" ;;
  restart) echo 'Warning: Model auth changes were saved, but the running Gateway could not refresh them. Run `openclaw gateway restart` to apply the saved changes.' >&2 ;;
  fail) echo "Error: The API-key profile belongs to another sign-in. Manage that saved sign-in first." >&2; exit 1 ;;
  wrote) echo "Error: API key saved, but provider settings could not be applied: busy. Reopen Models and save the key again." >&2; exit 1 ;;
  echo) echo "Error: not accepted: $key" >&2; exit 1 ;;
  slow) sleep 20 ;;
esac
"""


class Fake:
    """A box: HOME, the key file, the fake CLI, and what the CLI was given."""

    def __init__(self, modes=(), *, key_file: str | None = None):
        self.home = tempdir("agent-key-home-")
        os.environ["HOME"] = self.home
        os.environ.pop("LLM_API_KEY", None)
        self.dir = tempdir("agent-key-cli-")
        os.environ["FAKE_OPENCLAW_DIR"] = self.dir
        cli = os.path.join(self.dir, "openclaw")
        with open(cli, "w") as f:
            f.write(FAKE_CLI)
        os.chmod(cli, stat.S_IRWXU)
        openclaw_client.OPENCLAW_BIN = cli
        agent_backend.HAS_AGENT = True
        agent_key.SHARES = True
        self.modes(*modes)
        if key_file is not None:
            ai_account.store_key(key_file)

    def modes(self, *modes):
        with open(os.path.join(self.dir, "modes"), "w") as f:
            f.write("".join(m + "\n" for m in modes))
        for name in ("n", "argv", "keys"):
            try:
                os.remove(os.path.join(self.dir, name))
            except FileNotFoundError:
                pass

    def _lines(self, name):
        try:
            return open(os.path.join(self.dir, name)).read().splitlines()
        except FileNotFoundError:
            return []

    @property
    def argv(self):
        return self._lines("argv")

    @property
    def keys(self):
        return self._lines("keys")


def _key_file():
    try:
        return open(ai_account.key_path()).read()
    except FileNotFoundError:
        return None


def _status():
    return agent_key.status(ai_account.current_key(), True)


def _save(key):
    return asyncio.run(ai_account.save_key(key, OPENROUTER))


def test_a_save_hands_openclaw_the_key_on_stdin():
    box = Fake(["ok"], key_file=OLD)
    assert _status()["state"] == agent_key.SEPARATE  # set by the installer: not copied
    _save(NEW)
    assert _key_file() == NEW + "\n"
    assert box.argv == ["models auth paste-api-key --provider openrouter --agent main"], box.argv
    assert box.keys == [NEW]
    assert _status()["state"] == agent_key.SHARED
    state = open(os.path.join(box.home, ".local/state/teaport", agent_key.STATE_FILE)).read()
    assert NEW not in state and json.loads(state)["sha256"] == agent_key.fingerprint(NEW)
    assert oct(os.stat(os.path.join(box.home, ".local/state/teaport", agent_key.STATE_FILE)).st_mode)[-3:] == "600"
    # The agent this box talks to is the one named.
    openclaw_client.AGENT_ID = "house"
    try:
        box.modes("ok")
        _save(OLD)
        assert box.argv == ["models auth paste-api-key --provider openrouter --agent house"]
    finally:
        openclaw_client.AGENT_ID = None


def test_a_cli_that_fails_before_writing_leaves_both_as_they_were():
    box = Fake(["ok"], key_file=OLD)
    _save(OLD)  # OpenClaw has OLD from here
    box.modes("fail")
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved as e:
        assert e.reason == ai_account.NOT_SHARED
    assert _key_file() == OLD + "\n"
    assert box.keys == [NEW]  # tried once, nothing given back: it wrote nothing
    assert _status()["state"] == agent_key.SHARED
    # No key file before: none after.
    Fake(["fail"])
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved:
        pass
    assert _key_file() is None


def test_a_cli_that_may_have_written_gets_the_previous_key_back():
    box = Fake(["ok"], key_file=OLD)
    _save(OLD)
    for mode in ("wrote", "slow"):
        box.modes(mode, "ok")
        agent_key.PASTE_TIMEOUT_SECS = 1.0
        try:
            try:
                _save(NEW)
                raise AssertionError("saved")
            except ai_account.NotSaved:
                pass
        finally:
            agent_key.PASTE_TIMEOUT_SECS = 60.0
        assert box.keys == [NEW, OLD], (mode, box.keys)
        assert _key_file() == OLD + "\n"
        assert _status()["state"] == agent_key.SHARED, _status()


def test_when_the_previous_key_cannot_be_given_back_status_says_they_differ():
    box = Fake(["wrote"], key_file=OLD)  # OpenClaw's key was never set from here
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved:
        pass
    assert box.keys == [NEW] and _key_file() == OLD + "\n"
    s = _status()
    assert s["state"] == agent_key.MISMATCH and "halfway" in s["reason"], s
    # Known, but OpenClaw refuses it back too.
    box.modes("ok")
    _save(OLD)
    assert _status()["state"] == agent_key.SHARED  # a save that goes through clears it
    box.modes("wrote", "fail")
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved:
        pass
    assert box.keys == [NEW, OLD] and _status()["state"] == agent_key.MISMATCH


def test_nothing_is_run_where_openclaw_does_not_take_the_key():
    for setup, state in ((lambda: setattr(agent_backend, "HAS_AGENT", False), agent_key.OFF),
                         (lambda: setattr(agent_key, "SHARES", False), agent_key.OFF),
                         (lambda: setattr(openclaw_client, "OPENCLAW_BIN", "/nonexistent/openclaw"),
                          agent_key.ABSENT)):
        box = Fake(["fail"], key_file=OLD)
        setup()
        _save(NEW)  # saved: the brain's part is all there is
        assert _key_file() == NEW + "\n" and box.argv == [], box.argv
        assert _status()["state"] == state, _status()
    box = Fake(["fail"], key_file=OLD)
    asyncio.run(ai_account.save_key(NEW, "http://127.0.0.1:8182/v1"))
    assert box.argv == [] and agent_key.status(NEW, False)["state"] == agent_key.OTHER
    agent_backend.HAS_AGENT = True
    agent_key.SHARES = True


def test_adopt_saves_nothing_that_openclaw_did_not_take():
    real = ai_account._get
    ai_account._get = lambda url, key, timeout: (200, b'{"data": {"limit_remaining": 4.0}}')
    try:
        Fake(["fail"], key_file=OLD)
        got = ai_account.adopt_blocking(NEW, OPENROUTER)
        assert got.state == ai_account.UNKNOWN and got.reason == ai_account.NOT_SHARED, got
        assert _key_file() == OLD + "\n"
        box = Fake(["ok"], key_file=OLD)
        assert ai_account.adopt_blocking(NEW, OPENROUTER).state == ai_account.WORKS
        assert _key_file() == NEW + "\n" and box.keys == [NEW]
    finally:
        ai_account._get = real


def _page(box):
    etc = tempdir("agent-key-etc-")
    with open(os.path.join(etc, "brain.env"), "w") as f:
        f.write(f"LLM_BASE_URL={OPENROUTER}\nLLM_API_KEY={OLD}\n")
    config_ui.ETC_DIR = etc
    config_ui.SIP_CONF = os.path.join(box.home, "teaport-sip.conf")
    os.environ["GATEWAY_TOKEN"] = "t0k3n"
    config_ui.check_key = lambda key, base_url, timeout=None: ai_account.Check(ai_account.WORKS)

    async def states(units):
        return {u: {"state": "active", "active_since": 0.0} for u in units}

    config_ui._unit_states = states
    app = FastAPI()
    app.include_router(config_ui.router)
    return TestClient(app), etc


def test_the_settings_page_saves_both_or_says_it_did_not():
    box = Fake(["fail"], key_file=OLD)

    async def privileged(action, target, stdin=None):
        with open(os.path.join(config_ui.ETC_DIR, target), "w") as f:
            f.write(stdin)

    config_ui._privileged = privileged
    client, etc = _page(box)
    put = {"store": "brain_env", "values": {"LLM_API_KEY": NEW}}
    h = {"authorization": "Bearer t0k3n", "accept-language": "de"}
    r = client.put("/api/config", headers=h, json=put)
    assert r.status_code == 400, r.text
    assert r.json()["errors"]["LLM_API_KEY"] == i18n.get("de")._(ai_account.NOT_SHARED), r.json()
    assert _key_file() == OLD + "\n" and f"LLM_API_KEY={OLD}" in open(os.path.join(etc, "brain.env")).read()
    assert NEW not in r.text
    box.modes("ok")
    r = client.put("/api/config", headers=h, json=put)
    assert r.status_code == 200 and r.json()["superseded"] == ["LLM_API_KEY"], r.text
    assert _key_file() == NEW + "\n" and box.keys == [NEW]
    assert "LLM_API_KEY" not in open(os.path.join(etc, "brain.env")).read()
    assert _status()["state"] == agent_key.SHARED


def test_a_failed_step_after_the_save_takes_it_back():
    box = Fake(["ok", "ok"], key_file=OLD)

    async def privileged(action, target, stdin=None):
        raise RuntimeError("the helper failed")

    config_ui._privileged = privileged
    client, etc = _page(box)
    os.environ["LLM_API_KEY"] = OLD  # what the brain uses: brain.env's copy
    _save(OLD)
    box.modes("ok", "ok")
    try:
        r = TestClient(client.app, raise_server_exceptions=False).put(
            "/api/config", headers={"authorization": "Bearer t0k3n"},
            json={"store": "brain_env", "values": {"LLM_API_KEY": NEW}})
        assert r.status_code == 500
        assert box.keys == [NEW, OLD], box.keys  # given, then taken back
        assert _key_file() == OLD + "\n" and _status()["state"] == agent_key.SHARED, _status()
    finally:
        os.environ.pop("LLM_API_KEY", None)


def test_no_key_reaches_the_journal():
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        Fake(["echo"], key_file=OLD)
        try:
            _save(NEW)
        except ai_account.NotSaved as e:
            assert NEW not in e.detail
        Fake(["ok"], key_file=OLD)
        _save(NEW)
    finally:
        logger.remove(sink)
    text = "".join(lines)
    assert "OpenClaw did not take the new key" in text and "<key>" in text, text
    assert NEW not in text and OLD not in text


def test_a_restart_is_owed_and_waits_until_nobody_is_talking():
    box = Fake(["restart"], key_file=OLD)
    _save(NEW)
    assert _status()["state"] == agent_key.RESTART
    agent_key.IDLE_SECS, agent_key.POLL_SECS, agent_key.RETRY_SECS = 0.4, 0.05, 100.0
    live = {"now": True}
    restarts = []
    r = agent_key.Restarter()

    async def restart():
        restarts.append(live["now"])
        return (len(restarts) > 1, "rc 1: the gateway is busy")

    r._restart = restart
    listeners = []

    async def run():
        task = asyncio.create_task(r.run(live=lambda: live["now"], listeners=listeners))
        await asyncio.sleep(0.8)
        assert restarts == [], "restarted while a conversation was live"
        assert listeners == [r.poke]
        live["now"] = False
        for fn in listeners:
            fn()  # the arbiter's holder changed
        await asyncio.sleep(0.2)
        assert restarts == [], "restarted before IDLE_SECS of quiet"
        await asyncio.sleep(0.6)
        assert restarts == [False], restarts  # tried once; it failed
        s = _status()
        assert s["state"] == agent_key.RESTART and "busy" in s["last_error"], s
        await asyncio.sleep(0.3)
        assert len(restarts) == 1, "retried before RETRY_SECS"
        r._clock = lambda: time.monotonic() + 1000.0  # RETRY_SECS later
        r.poke()
        await asyncio.sleep(0.3)
        assert restarts == [False, False], restarts
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert listeners == []

    try:
        asyncio.run(run())
    finally:
        agent_key.IDLE_SECS, agent_key.POLL_SECS, agent_key.RETRY_SECS = 30.0, 30.0, 600.0
    assert _status()["state"] == agent_key.SHARED, _status()
    # The real restart: the CLI's own, letting the gateway's work drain (--safe).
    box.modes("ok")
    assert asyncio.run(agent_key.Restarter()._restart()) == (True, "")
    assert box.argv == ["gateway restart --safe"], box.argv


def test_health_status_and_doctor_say_where_it_stands():
    Fake(["ok"], key_file=OLD)
    _save(NEW)
    os.environ["LLM_BASE_URL"] = OPENROUTER
    try:
        from teaport_brain import gateway_server
        shown = asyncio.run(gateway_server.health())["agent_key"]
    finally:
        os.environ.pop("LLM_BASE_URL", None)
    from test_llm_account_error import _cli
    label = "model key (OpenClaw)"
    cases = (
        (shown, "OpenClaw uses the same key as the brain", "✓"),
        ({"state": "restart", "reason": "x", "last_error": "rc 1: busy"},
         "restarts to use it once nobody is talking (last try: rc 1: busy)", "!"),
        ({"state": "mismatch", "reason": "a key change failed halfway"},
         "may be on different keys: a key change failed halfway", "✗"),
        ({"state": "off", "reason": "no OpenClaw on this box (TEAPORT_AGENT=none)"},
         "not shared: no OpenClaw on this box", None),
        ({"state": "separate", "reason": "x"}, "OpenClaw has a key of its own", None),
    )
    for health, words, mark in cases:
        status = [ln for ln in _cli("cmd_status", {"ok": True, "agent_key": health}).splitlines()
                  if label in ln]
        assert len(status) == 1 and words in status[0], (health, status)
        doctor = [ln for ln in _cli("cmd_doctor", {"ok": True, "agent_key": health}).splitlines()
                  if "model key:" in ln]
        if mark is None:
            assert doctor == [], doctor
        else:
            assert len(doctor) == 1 and doctor[0].lstrip().startswith(mark), doctor
    assert label not in _cli("cmd_status", {"ok": True})  # a brain that does not say


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
