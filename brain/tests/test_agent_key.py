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
from test_llm_account_error import _cli  # `teaport status` / `doctor` with /health stubbed

# That module sets an LLM key and endpoint in this process's environment for its own tests;
# the brain's key here is the key file's.
for _name in ("LLM_API_KEY", "LLM_BASE_URL", "TEAPORT_URL"):
    os.environ.pop(_name, None)

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
if [ "$1 $2" = "models auth" ]; then key=$(cat); printf '%s\n' "$key" >> "$d/keys"; fi
case "$mode" in
  ok) echo "Auth profile: openrouter:manual (openrouter/api_key)" ;;
  restart) echo 'Warning: Model auth changes were saved, but the running Gateway could not refresh them. Run `openclaw gateway restart` to apply the saved changes.' >&2 ;;
  fail) echo "Error: The API-key profile belongs to another sign-in. Manage that saved sign-in first." >&2; exit 1 ;;
  wrote) echo "Error: API key saved, but provider settings could not be applied: busy. Reopen Models and save the key again." >&2; exit 1 ;;
  echo) echo "Error: not accepted: $key" >&2; exit 1 ;;
  slow) sleep 20 ;;
  agents) echo 'Error: the model command needs an agent: main, work. Pass --agent <id>.' >&2; exit 1 ;;
  status) echo "[plugins] loaded"; cat "$d/status.json" ;;
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
    assert _status()["state"] == agent_key.UNSYNCED  # set by the installer: not known yet
    _save(NEW)
    assert _key_file() == NEW + "\n"
    # No agent named here: no --agent, OpenClaw picks its own (its sole agent).
    assert box.argv == ["models auth paste-api-key --provider openrouter"], box.argv
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
    later = {"s": 0.0}
    r = agent_key.Restarter(clock=lambda: time.monotonic() + later["s"])

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
        later["s"] = 1000.0  # RETRY_SECS later, nobody talking meanwhile
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


def test_with_several_agents_and_none_named_the_save_is_rolled_back():
    """OpenClaw resolves the agent itself (no --agent); where it cannot -- several agents,
    none named -- the CLI fails before writing and nothing changes."""
    box = Fake(["agents"], key_file=OLD)
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved:
        pass
    assert box.argv == ["models auth paste-api-key --provider openrouter"]
    assert _key_file() == OLD + "\n" and _status()["state"] == agent_key.UNSYNCED


def test_every_cli_run_gets_the_users_runtime_dir():
    run_dir = tempdir("agent-key-run-")
    real = os.path.isdir
    os.path.isdir = lambda p: p == "/run/user/4242" or real(p)
    try:
        env = openclaw_client.child_env({"PATH": "/usr/bin", "OPENCLAW_GATEWAY_URL": "x"}, uid=4242)
        assert env["XDG_RUNTIME_DIR"] == "/run/user/4242" and "OPENCLAW_GATEWAY_URL" not in env
        assert env["PATH"].startswith(os.path.dirname(openclaw_client.OPENCLAW_BIN))
        kept = openclaw_client.child_env({"XDG_RUNTIME_DIR": run_dir}, uid=4242)
        assert kept["XDG_RUNTIME_DIR"] == run_dir  # one already set is kept
        assert "XDG_RUNTIME_DIR" not in openclaw_client.child_env({}, uid=4243)  # no such dir
    finally:
        os.path.isdir = real
    # And the CLI really gets it (the fake prints its environment's value to argv's log).
    box = Fake(["ok"], key_file=OLD)
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write('printf "XDG=%s\\n" "$XDG_RUNTIME_DIR" >> "$FAKE_OPENCLAW_DIR/env"\n')
    saved = os.environ.pop("XDG_RUNTIME_DIR", None)
    try:
        _save(NEW)
    finally:
        if saved is not None:
            os.environ["XDG_RUNTIME_DIR"] = saved
    want = f"/run/user/{os.getuid()}" if os.path.isdir(f"/run/user/{os.getuid()}") else ""
    assert open(os.path.join(box.dir, "env")).read() == f"XDG={want}\n"


def _status_json(labels, kind="profiles", provider="openrouter"):
    return {"auth": {"providers": [
        {"provider": "anthropic", "effective": {"kind": "env"}, "profiles": {"labels": []}},
        {"provider": provider, "effective": {"kind": kind, "detail": "~/.openclaw/state"},
         "profiles": {"count": len(labels), "labels": labels}}]}}


def test_openclaws_masked_key_is_compared_with_the_brains():
    m = agent_key.mask(OLD)
    assert m == OLD[:8] + "..." + OLD[-8:]
    assert agent_key.mask("abcdefghij") == "ab...ij" and agent_key.mask("abc") == "a...c"
    same = agent_key.compare_labels(_status_json([f"openrouter:manual={m}"]), OLD)
    assert same == agent_key.SAME
    assert agent_key.compare_labels(
        _status_json([f"openrouter:manual={m} [cooldown 4m]"]), OLD) == agent_key.SAME
    assert agent_key.compare_labels(
        _status_json([f"openrouter:manual={agent_key.mask(NEW)}"]), OLD) == agent_key.DIFFERENT
    for unknown in (None, {}, {"auth": {"providers": "x"}},
                    _status_json([f"openrouter:manual={m}"], kind="env"),
                    _status_json([f"x={m}"], provider="openai"),
                    _status_json([]), _status_json(["openrouter:work=OAuth", 3]),
                    _status_json([f"openrouter:a={m}", f"openrouter:b={agent_key.mask(NEW)}"])):
        assert agent_key.compare_labels(unknown, OLD) is None, unknown


def test_the_first_look_records_a_key_openclaw_already_has():
    """Provisioning gave both the same key, and no save recorded it: one look, while
    nobody is talking, at `openclaw models status --json` settles it."""
    for labels, state in (([f"openrouter:manual={agent_key.mask(OLD)}"], agent_key.SHARED),
                          ([f"openrouter:manual={agent_key.mask(NEW)}"], agent_key.SEPARATE),
                          (None, agent_key.UNSYNCED)):
        box = Fake(["status"], key_file=OLD)
        if labels is not None:
            with open(os.path.join(box.dir, "status.json"), "w") as f:
                json.dump(_status_json(labels), f, indent=2)
        else:
            box.modes("fail")
        assert agent_key.unlooked(OLD, True)
        agent_key.IDLE_SECS, agent_key.POLL_SECS = 0.2, 0.05
        live = {"now": True}

        async def run():
            r = agent_key.Restarter()
            task = asyncio.create_task(r.run(live=lambda: live["now"], brain=lambda: (OLD, True)))
            await asyncio.sleep(0.4)
            assert box.argv == [], "looked while a conversation was live"
            live["now"] = False
            r.poke()
            await asyncio.sleep(1.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            asyncio.run(run())
        finally:
            agent_key.IDLE_SECS, agent_key.POLL_SECS = 30.0, 30.0
        assert box.argv == ["models status --json"], box.argv  # once, read-only
        assert _status()["state"] == state, (labels, _status())
        assert agent_key.unlooked(OLD, True) == (state == agent_key.UNSYNCED)
    # A different key seen, then saved: the save records it as shared.
    box.modes("ok")
    _save(OLD)
    assert _status()["state"] == agent_key.SHARED


# ------------------------------------------------------------------ review round 1

A_KEY = "sk-or-v1-" + "a" * 64
B_KEY = "sk-or-v1-" + "b" * 64


class Held:
    """agent_key._paste faked in-process: what OpenClaw holds, and a script per key."""

    def __init__(self, held, script):
        self.held, self.script, self.calls = held, script, []

    async def __call__(self, key):
        self.calls.append(key)
        delay, told = self.script(key)
        await asyncio.sleep(delay)
        if told.ok or told.wrote:
            self.held = key
        return told


def _patched(paste=None, shown=None):
    real = (agent_key._paste, agent_key._shown)
    if paste is not None:
        agent_key._paste = paste
    if shown is not None:
        agent_key._shown = shown
    return real


def _restore(real):
    agent_key._paste, agent_key._shown = real


def test_two_saves_at_once_never_interleave():
    """(repro race.py) Tab A's paste fails slowly before writing while tab B's succeeds:
    one save at a time, so A's put-back can never land after B's save."""
    Fake([], key_file=OLD)
    agent_key.update(sha256=agent_key.fingerprint(OLD), mismatch=None, restart=False)
    held = Held(OLD, lambda k: (0.2, agent_key.Told(False, detail="busy")) if k == A_KEY
                else (0.05, agent_key.Told(True)))
    real = _patched(paste=held)

    async def one(k, wait):
        await asyncio.sleep(wait)
        try:
            await ai_account.save_key(k, OPENROUTER)
            return "saved"
        except ai_account.NotSaved:
            return "not saved"

    async def main():
        return await asyncio.gather(one(A_KEY, 0), one(B_KEY, 0.05))

    try:
        assert asyncio.run(main()) == ["not saved", "saved"]
    finally:
        _restore(real)
    assert held.calls == [A_KEY, B_KEY], "B's paste ran inside A's save"
    assert ai_account.current_key() == B_KEY and held.held == B_KEY
    assert _status()["state"] == agent_key.SHARED


def test_saves_in_two_processes_take_turns():
    """The flock: a save in another process (the setup-mode unit) holds the lock; this
    one waits for it rather than interleaving."""
    import subprocess
    import sys
    box = Fake(["ok"], key_file=OLD)
    script = ("import asyncio, sys, time\n"
              "from teaport_brain import agent_key\n"
              "async def main():\n"
              "    async with agent_key.save_lock():\n"
              "        print('held', flush=True); time.sleep(0.8)\n"
              "asyncio.run(main())\n")
    other = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ))
    try:
        assert other.stdout.readline().strip() == "held"
        assert agent_key.saving_now()
        t0 = time.monotonic()
        _save(NEW)
        assert time.monotonic() - t0 >= 0.5, "the save did not wait for the other process"
    finally:
        other.wait(10)
    assert box.keys == [NEW] and not agent_key.saving_now()


def test_write_secret_from_two_threads_at_once():
    import threading
    from teaport_brain import settings
    Fake([])
    path = os.path.join(tempdir("agent-key-secret-"), "llm_key")
    errors = []

    def write(v):
        try:
            for _ in range(200):
                settings.write_secret(path, v)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(k,)) for k in (A_KEY, B_KEY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert open(path).read() in (A_KEY + "\n", B_KEY + "\n")
    assert sorted(os.listdir(os.path.dirname(path))) == ["llm_key"]  # no temp files left
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_the_first_look_never_overwrites_a_save_that_landed_meanwhile():
    """(repro look_race.py) OpenClaw still shows K1 when asked; a save of K2, owing a
    restart, lands while the look's CLI runs: the look writes nothing."""
    Fake([], key_file=OLD)

    async def shown():
        await asyncio.sleep(0.2)
        return _status_json([f"openrouter:manual={agent_key.mask(OLD)}"])

    held = Held(OLD, lambda k: (0.05, agent_key.Told(True, restart=True)))
    real = _patched(paste=held, shown=shown)

    async def main():
        await asyncio.gather(agent_key.look(ai_account.brain_key),
                             ai_account.save_key(NEW, OPENROUTER))

    os.environ["LLM_BASE_URL"] = OPENROUTER
    try:
        asyncio.run(main())
    finally:
        _restore(real)
        os.environ.pop("LLM_BASE_URL", None)
    data = agent_key.load()
    assert data["sha256"] == agent_key.fingerprint(NEW) and data["restart"] is True, data
    assert _status()["state"] == agent_key.RESTART


def test_a_restart_waits_idle_secs_after_the_conversation_ends():
    """(repro idle.py) With POLL_SECS == IDLE_SECS, as in production, and a conversation
    longer than both: the restart comes IDLE_SECS after it ended, not at once."""
    Fake([])
    agent_key.update(sha256="x" * 64, restart=True, mismatch=None)
    agent_key.IDLE_SECS = agent_key.POLL_SECS = 0.3
    live = {"now": True}
    r = agent_key.Restarter()
    listeners, at = [], []

    async def restart():
        at.append(time.monotonic())
        return True, ""

    r._restart = restart

    async def main():
        t = asyncio.create_task(r.run(live=lambda: live["now"], listeners=listeners))
        await asyncio.sleep(1.0)
        live["now"] = False
        end = time.monotonic()
        for fn in listeners:
            fn()  # the arbiter: the holder changed
        await asyncio.sleep(0.8)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return end

    try:
        end = asyncio.run(main())
    finally:
        agent_key.IDLE_SECS, agent_key.POLL_SECS = 30.0, 30.0
    assert len(at) == 1 and at[0] - end >= 0.3, [a - end for a in at]


def test_a_save_cancelled_or_broken_midway_is_put_back():
    """A cancellation during the paste (the brain or the setup unit stopping), or an
    error in the save's own steps: the key file as it was, the marker cleared, and a
    mismatch recorded once OpenClaw may have been told."""
    for boom in (asyncio.CancelledError, OSError):
        Fake([], key_file=OLD)

        async def paste(key, boom=boom):
            await asyncio.sleep(0.05)
            raise boom()

        real = _patched(paste=paste)
        try:
            asyncio.run(ai_account.save_key(NEW, OPENROUTER))
            raise AssertionError("saved")
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            _restore(real)
        assert _key_file() == OLD + "\n", boom
        data = agent_key.load()
        assert not data.get("saving") and "stopped midway" in data["mismatch"], data
        assert _status()["state"] == agent_key.MISMATCH
    # Cancelled from outside while the CLI runs: the same.
    Fake([], key_file=OLD)

    async def slow(key):
        await asyncio.sleep(5)
        return agent_key.Told(True)

    real = _patched(paste=slow)

    async def main():
        t = asyncio.create_task(ai_account.save_key(NEW, OPENROUTER))
        await asyncio.sleep(0.2)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main())
    finally:
        _restore(real)
    assert _key_file() == OLD + "\n" and _status()["state"] == agent_key.MISMATCH
    # A save torn by a crash leaves its marker: a mismatch once no save holds the lock.
    Fake([], key_file=OLD)
    agent_key.begin(NEW, OLD)
    assert agent_key.settle_torn() and _status()["state"] == agent_key.MISMATCH
    assert not agent_key.load().get("saving")


def test_any_warning_after_a_clean_exit_owes_a_restart():
    """OpenClaw's "the configured Gateway could not be identified or refreshed" warning
    carries no restart command: written, not applied, so a restart is owed all the same."""
    box = Fake(["ok"], key_file=OLD)
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write("echo 'Warning: Model auth changes were saved, but the configured Gateway could not "
                "be identified or refreshed. Apply the auth change on the Gateway host, or restart "
                "it there.' >&2\n")
    _save(NEW)
    assert _status()["state"] == agent_key.RESTART


def test_a_gateway_that_shares_no_store_here_saves_the_brains_key_alone():
    """Structural, not OpenClaw failing: the brain's key is saved, this host's store is put
    back, and status says OpenClaw keeps its own key here; a later save that the gateway
    takes clears it."""
    box = Fake(["ok"], key_file=OLD)
    _save(OLD)
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write("[ \"$(cat $FAKE_OPENCLAW_DIR/n)\" = 1 ] && echo 'Warning: Model auth changes were "
                "saved on this host, but the configured Gateway does not share this auth state. "
                "Run the auth command on the Gateway host.' >&2\nexit 0\n")
    box.modes("ok", "ok")
    _save(NEW)  # saved, no NotSaved
    assert _key_file() == NEW + "\n" and box.keys == [NEW, OLD]  # this host's store put back
    s = _status()
    assert s["state"] == agent_key.REMOTE and "does not share" in s["reason"], s
    out = _cli("cmd_doctor", {"ok": True, "agent_key": s})
    assert "model key:" not in out  # informational: nothing in doctor
    assert "not shared" in _cli("cmd_status", {"ok": True, "agent_key": s})
    with open(os.path.join(box.dir, "openclaw"), "w") as f:
        f.write(FAKE_CLI)
    box.modes("ok")
    _save(OLD)
    assert _status()["state"] == agent_key.SHARED


def test_a_halfway_failure_says_so_in_its_own_words():
    Fake(["wrote"], key_file=OLD)  # never given from here: cannot be given back
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved as e:
        assert e.reason == ai_account.NOT_FINISHED
    Fake(["fail"], key_file=OLD)
    try:
        _save(NEW)
        raise AssertionError("saved")
    except ai_account.NotSaved as e:
        assert e.reason == ai_account.NOT_SHARED


def test_anything_but_a_mask_is_not_compared():
    m = agent_key.mask(OLD)
    for value in ("ref(env:OPENROUTER_API_KEY)", "token:sk-or-v1...abcdefgh", "OAuth",
                  "missing", "marker(local)", "sk-or-v1-whole-key-shown"):
        assert agent_key.compare_labels(_status_json([f"openrouter:manual={value}"]), OLD) is None, value
    assert agent_key.compare_labels(_status_json([f"openrouter:manual={m}"]), OLD) == agent_key.SAME


def test_the_restart_flag_follows_the_state_now():
    """A restart owed for the key OpenClaw had is not carried over to a new key that
    applied cleanly; one owed for the same key stands."""
    box = Fake(["restart"], key_file=OLD)
    _save(OLD)
    assert _status()["state"] == agent_key.RESTART
    box.modes("ok")
    _save(NEW)
    assert _status()["state"] == agent_key.SHARED
    box.modes("restart", "ok")
    _save(NEW)
    _save(NEW)
    assert _status()["state"] == agent_key.RESTART


def test_a_stopped_gateway_is_not_restarted_again_and_again():
    Fake([])
    agent_key.update(sha256="y" * 64, restart=True, mismatch=None)
    agent_key.IDLE_SECS, agent_key.POLL_SECS = 0.1, 0.05
    calls = []
    r = agent_key.Restarter()

    async def restart():
        calls.append(1)
        return False, "rc 1: Gateway not reachable at ws://127.0.0.1:18789 (ECONNREFUSED)."

    r._restart = restart

    async def main():
        t = asyncio.create_task(r.run(live=lambda: False))
        await asyncio.sleep(0.6)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main())
    finally:
        agent_key.IDLE_SECS, agent_key.POLL_SECS = 30.0, 30.0
    assert calls == [1] and not agent_key.load()["restart"]


def test_the_restarter_outlives_a_bad_pass():
    Fake([])
    agent_key.update(sha256="z" * 64, restart=True, mismatch=None)
    agent_key.IDLE_SECS, agent_key.POLL_SECS = 0.05, 0.05
    real_backoff = agent_key.ERROR_BACKOFF_SECS
    agent_key.ERROR_BACKOFF_SECS = 0.1
    calls = []
    r = agent_key.Restarter()

    async def restart():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a bug")
        return True, ""

    r._restart = restart
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")

    async def main():
        t = asyncio.create_task(r.run(live=lambda: False))
        await asyncio.sleep(0.6)
        assert not t.done()
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main())
    finally:
        logger.remove(sink)
        agent_key.IDLE_SECS, agent_key.POLL_SECS = 30.0, 30.0
        agent_key.ERROR_BACKOFF_SECS = real_backoff
    assert len(calls) == 2 and not agent_key.load()["restart"]
    assert any("pass failed" in ln and "a bug" in ln for ln in lines), lines


# ------------------------------------------------------------------ review round 2

def test_a_save_that_ends_while_the_torn_check_runs_is_no_torn_save():
    """(repro torn_race.py) A live save holds the lock and its marker; it ends right after
    the check read the marker. The check takes the lock first, so it sees nothing torn."""
    import fcntl
    Fake([])
    os.makedirs(agent_key.state_dir(), exist_ok=True)
    fd = os.open(agent_key._path("agent_key.save.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    agent_key.begin(NEW, None)
    agent_key.update(sha256=agent_key.fingerprint(NEW), mismatch=None)
    real_load = agent_key.load
    fired = []

    def load():
        data = real_load()
        if data.get("saving") and not fired:
            fired.append(1)
            agent_key.load = real_load
            agent_key.end()
            os.close(fd)
        return data

    agent_key.load = load
    try:
        assert agent_key.settle_torn() is False
    finally:
        agent_key.load = real_load
        if not fired:
            os.close(fd)
    assert not agent_key.load().get("mismatch")
    # Held by a save under way: never torn, and its marker is left alone.
    fd = os.open(agent_key._path("agent_key.save.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    agent_key.begin(OLD, NEW)
    try:
        assert agent_key.settle_torn() is False and agent_key.load()["saving"]
    finally:
        os.close(fd)
    assert agent_key.settle_torn() is True  # nobody holds it now: torn
    assert agent_key.load()["mismatch"] and not agent_key.load()["saving"]


def test_a_room_falling_asleep_counts_as_the_end_of_a_conversation():
    """(repro idle_room.py) The room mic falling asleep tells the arbiter's listeners
    nothing; the pass that first sees it idle stamps the time."""
    Fake([])
    agent_key.update(sha256="x" * 64, restart=True, mismatch=None)
    agent_key.IDLE_SECS = agent_key.POLL_SECS = 0.3
    live = {"now": True}
    r = agent_key.Restarter()
    at = []

    async def restart():
        at.append(time.monotonic())
        return True, ""

    r._restart = restart

    async def main():
        t = asyncio.create_task(r.run(live=lambda: live["now"], listeners=[]))
        await asyncio.sleep(1.0)
        live["now"] = False  # asleep: no listener call
        end = time.monotonic()
        await asyncio.sleep(0.9)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return end

    try:
        end = asyncio.run(main())
    finally:
        agent_key.IDLE_SECS, agent_key.POLL_SECS = 30.0, 30.0
    assert len(at) == 1 and at[0] - end >= 0.3, [a - end for a in at]


def _remote_cli(box, back_ok=True):
    """The fake CLI: the first paste says the gateway shares no store here; the second
    (the host store put back) works, or fails."""
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write("[ \"$(cat $FAKE_OPENCLAW_DIR/n)\" = 1 ] && echo 'Warning: Model auth changes were "
                "saved on this host, but the configured Gateway does not share this auth state. "
                "Run the auth command on the Gateway host.' >&2\n"
                + ("" if back_ok else "[ \"$(cat $FAKE_OPENCLAW_DIR/n)\" = 2 ] && "
                   "{ echo 'Error: busy' >&2; exit 1; }\n") + "exit 0\n")


def test_remote_puts_the_host_store_back_whenever_the_paste_wrote_it():
    # Never given from here (no recorded sync): the host store gets the previous key back.
    box = Fake(["ok", "ok"], key_file=OLD)
    _remote_cli(box)
    _save(NEW)
    assert box.keys == [NEW, OLD] and _key_file() == NEW + "\n"
    s = _status()
    assert s["state"] == agent_key.REMOTE and "kept the new key" not in s["reason"], s
    # The put-back fails: said as it is.
    box = Fake(["ok", "ok"], key_file=OLD)
    _remote_cli(box, back_ok=False)
    _save(NEW)
    s = _status()
    assert s["state"] == agent_key.REMOTE and "kept the new key" in s["reason"], s
    assert "putting the previous one back failed" in s["reason"]
    # A mismatch is what it says, whatever was recorded before.
    agent_key.mismatch("test")
    assert _status()["state"] == agent_key.MISMATCH and not agent_key.load().get("unshareable")


def test_remote_is_looked_at_again_when_the_brain_starts():
    for mode, state in (("local", agent_key.UNSYNCED), ("remote", agent_key.REMOTE),
                        (None, agent_key.REMOTE)):
        Fake([], key_file=OLD)
        agent_key.update(unshareable=agent_key.REMOTE_REASON)

        async def gateway_mode(mode=mode):
            return mode

        async def shown():
            return None

        real_mode = agent_key._gateway_mode
        agent_key._gateway_mode = gateway_mode
        real = _patched(shown=shown)
        try:
            asyncio.run(agent_key.look(lambda: (OLD, True)))
        finally:
            agent_key._gateway_mode = real_mode
            _restore(real)
        assert _status()["state"] == state, (mode, _status())
    # The real read: `openclaw config get gateway.mode`, and an unset one is local.
    box = Fake([], key_file=OLD)
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write('[ "$1 $2 $3" = "config get gateway.mode" ] && { echo "[plugins] x"; echo remote; }\n')
    assert asyncio.run(agent_key._gateway_mode()) == "remote"
    assert box.argv[-1] == "config get gateway.mode"
    box.modes("fail")  # exit 1: unset, the runtime default
    assert asyncio.run(agent_key._gateway_mode()) == "local"


def test_undo_leaves_a_key_saved_since_alone():
    box = Fake(["ok", "ok"], key_file=OLD)
    _save(OLD)
    saved = _save(NEW)
    _save(A_KEY)  # another save lands before the undo
    keys_before = list(box.keys)
    asyncio.run(saved.undo())
    assert _key_file() == A_KEY + "\n" and box.keys == keys_before
    assert _status()["state"] == agent_key.SHARED and not agent_key.load().get("saving")


def test_a_save_waits_for_another_only_so_long():
    box = Fake(["ok"], key_file=OLD)
    real_wait = agent_key.SAVE_WAIT_SECS
    agent_key.SAVE_WAIT_SECS = 0.3

    async def main():
        async def hold():
            async with agent_key.save_lock():
                await asyncio.sleep(1.0)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        try:
            await ai_account.save_key(NEW, OPENROUTER)
            raise AssertionError("saved")
        except ai_account.NotSaved as e:
            assert e.reason == ai_account.NOT_SHARED and "still running" in e.detail
        await holder

    try:
        asyncio.run(main())
    finally:
        agent_key.SAVE_WAIT_SECS = real_wait
    assert _key_file() == OLD + "\n" and box.keys == []


def test_a_stopped_gateway_is_told_from_all_of_its_output():
    box = Fake([])
    with open(os.path.join(box.dir, "openclaw"), "a") as f:
        f.write('[ "$1" = gateway ] && { echo "Error: Gateway not reachable at ws://127.0.0.1:18789 '
                '(ECONNREFUSED)." >&2; echo "Hint: start it with openclaw gateway start" >&2; '
                'exit 1; }\n')
    ok, why = asyncio.run(agent_key.Restarter()._restart())
    assert not ok and agent_key._REFUSED in why, why


def test_health_status_and_doctor_say_where_it_stands():
    Fake(["ok"], key_file=OLD)
    _save(NEW)
    os.environ["LLM_BASE_URL"] = OPENROUTER
    try:
        from teaport_brain import gateway_server
        shown = asyncio.run(gateway_server.health())["agent_key"]
    finally:
        os.environ.pop("LLM_BASE_URL", None)
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
        ({"state": "unsynced", "reason": "x"}, "unknown, not yet synced", None),
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
