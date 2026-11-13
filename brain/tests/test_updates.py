#
# Is a newer release out? (updates.py: the updater's looking; nothing is installed)
#
# What has to hold:
#   - A component the channel's manifest names differently from the installed one is
#     "changed"; one it has not released (TODO, empty, absent) never is, and neither
#     is one that is the same.
#   - The channel is the setting, else the one the installed manifest came from, else
#     stable; TEAPORT_MANIFEST_URL replaces the address.
#   - A look that fails keeps the last answer, marked not fresh; a restart does not forget
#     it; with the check off (0 s) nothing is read and /health says "off".
#   - `teaport update --check --json`'s code path (main) prints the status as JSON and
#     fails, saying why, when nothing is installed or the manifest cannot be read.
#   - `teaport status` shows the release line, and nothing where the brain says nothing.
#
# Synthetic manifests only; nothing is fetched.
#
# Run: python test_updates.py   (or via pytest)
#
import asyncio
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import appliance  # noqa: E402
from tempdirs import tempdir  # noqa: E402

from teaport_brain import updates  # noqa: E402


def _manifest(engine="1.6", brain="v1", plugin="0.1.0", openclaw="2026.9.7", sha="aa",
              channel="stable", version="1.0.0"):
    return {"version": version, "channel": channel,
            "engine": {"release": engine}, "brain": {"repo": "x/y", "tag": brain},
            "plugin": {"npm": "p", "version": plugin},
            "openclaw": {"version": openclaw, "node": "24"},
            "models": {"kokoro": {"sha256": sha}, "voxtral": {"sha256": "bb"}}}


def test_a_component_the_channel_names_differently_is_changed():
    got = updates.compare(_manifest(), _manifest(engine="1.7", sha="cc"))
    assert got["available"] and got["changed"] == ["engine", "models"], got
    assert got["components"]["engine"] == {"installed": "1.6", "latest": "1.7", "changed": True}
    assert not got["components"]["brain"]["changed"]
    same = updates.compare(_manifest(), _manifest())
    assert not same["available"] and same["changed"] == [], same


def test_a_new_version_name_alone_is_not_an_update():
    got = updates.compare(_manifest(), _manifest(version="9.9.9"))
    assert not got["available"] and got["changed"] == [], got
    assert got["components"]["version"] == {"installed": "1.0.0", "latest": "9.9.9",
                                            "changed": False}, got


def test_what_the_channel_has_not_released_is_not_an_update():
    latest = _manifest(engine="TODO", brain="", plugin="0.2.0")
    del latest["openclaw"]
    got = updates.compare(_manifest(), latest)
    assert got["changed"] == ["plugin"], got
    assert got["components"]["engine"]["latest"] is None, got
    beta = _manifest()
    for part in ("engine", "brain", "plugin", "openclaw"):
        beta[part] = {k: "TODO" for k in beta[part]}
    beta["models"] = {"kokoro": {"sha256": "TODO"}}
    assert not updates.compare(_manifest(), beta)["available"]


def test_a_placeholder_model_hash_does_not_hide_a_real_change_in_another():
    latest = _manifest()
    latest["models"] = {"kokoro": {"sha256": "TODO"}, "voxtral": {"sha256": "cc"}}
    got = updates.compare(_manifest(), latest)
    assert got["changed"] == ["models"], got
    latest["models"]["voxtral"]["sha256"] = "bb"
    assert not updates.compare(_manifest(), latest)["available"]


def test_a_component_the_box_has_no_record_of_is_changed():
    old = _manifest()
    del old["plugin"]
    got = updates.compare(old, _manifest())
    assert got["changed"] == ["plugin"], got
    assert updates.compare({}, _manifest())["available"]


def test_the_channel_and_the_address():
    assert updates.channel_of("beta", {"channel": "stable"}) == "beta"
    assert updates.channel_of("", {"channel": "beta"}) == "beta"
    assert updates.channel_of("", {}) == "stable"
    assert updates.channel_of("nightly", {"channel": "nightly"}) == "stable"
    assert updates.manifest_url("beta", {}) == "https://get.teaspoon.tech/manifest/beta.json"
    assert updates.manifest_url("beta", {"TEAPORT_MANIFEST_URL": "/m.json"}) == "/m.json"


def test_a_look_reads_the_channels_manifest_and_names_it():
    seen = []

    def fetch(url):
        seen.append(url)
        return _manifest(engine="1.7")
    got = updates.check("beta", fetch=fetch, installed=_manifest(), clock=lambda: 5.0, env={})
    assert seen == ["https://get.teaspoon.tech/manifest/beta.json"], seen
    assert got["channel"] == "beta" and got["checked"] == 5.0 and got["fresh"]
    assert got["changed"] == ["engine"], got
    # No setting: the channel the installed manifest came from.
    got = updates.check("", fetch=fetch, installed=_manifest(channel="beta"), env={})
    assert got["channel"] == "beta", got




def test_a_manifest_over_http_is_asked_for_with_our_own_user_agent():
    # The download host answers urllib's default agent with a 403, so `load` names itself.
    import http.server
    import threading
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("User-Agent"))
            if (self.headers.get("User-Agent") or "").startswith("Python-urllib"):
                self.send_error(403)
                return
            body = json.dumps({"version": "x"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = updates.load(f"http://127.0.0.1:{srv.server_port}/stable.json")
    finally:
        srv.shutdown()
        srv.server_close()
    assert got == {"version": "x"}, got
    assert seen == [updates.USER_AGENT] and "urllib" not in seen[0], seen


def test_the_monitor_keeps_the_last_answer_across_a_failed_look_and_a_restart():
    d = tempdir("teaport-updates-")
    path = os.path.join(d, "update.json")
    saved_dirs = (updates.PREFIX, updates.ETC)
    updates.PREFIX = updates.ETC = d
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(_manifest(), f)
    real = updates.CHECK_SECS
    updates.CHECK_SECS = 86400.0
    try:
        answers = [_manifest(engine="1.7"), OSError("offline")]

        def fetch(url):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        mon = updates.Monitor(clock=lambda: 7.0, fetch=fetch, path=path)
        assert mon.status() == {"state": "unknown", "fresh": False}, mon.status()
        assert asyncio.run(mon.refresh()) is True
        st = mon.status()
        assert st["state"] == "available" and st["changed"] == ["engine"] and st["fresh"], st
        assert st["channel"] == "stable" and st["checked"] == 7.0, st
        # The next look fails: the last answer stands, not fresh.
        assert asyncio.run(mon.refresh()) is False
        st = mon.status()
        assert st["state"] == "available" and st["fresh"] is False, st
        # A restart (a new monitor on the same file) still knows.
        again = updates.Monitor(path=path)
        assert again.status()["state"] == "available", again.status()
        # A file that is not a look is no look.
        with open(path, "w") as f:
            f.write('{"channel": 3}')
        assert updates.Monitor(path=path).status()["state"] == "unknown"
        with open(path, "w") as f:
            f.write('{"channel": "stable", "components": {}, "changed": ["engine"]}')
        assert updates.Monitor(path=path).status()["state"] == "unknown"
        # Off: nothing is said, and the loop does not read.
        updates.CHECK_SECS = 0
        assert updates.Monitor(path=path).status() == {"state": "off"}
        asyncio.run(asyncio.wait_for(updates.Monitor(fetch=lambda u: 1 / 0, path=path).run(), 1))
    finally:
        updates.CHECK_SECS = real
        updates.PREFIX, updates.ETC = saved_dirs


def test_main_prints_the_status_and_says_why_it_cannot():
    d = tempdir("teaport-updates-main-")
    latest = os.path.join(d, "latest.json")
    with open(latest, "w") as f:
        json.dump(_manifest(engine="1.7"), f)
    env = {"TEAPORT_MANIFEST_URL": latest}
    saved = {k: os.environ.get(k) for k in env}
    saved_dirs = (updates.PREFIX, updates.ETC)
    os.environ.update(env)

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = updates.main(list(argv) + ["--prefix", d, "--etc", d])
        return rc, out.getvalue(), err.getvalue()
    try:
        rc, out, err = run("check")
        assert rc == 1 and "not installed" in err, (rc, err)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(_manifest(), f)
        rc, out, err = run("check", "--json")
        got = json.loads(out)
        assert rc == 0 and got["changed"] == ["engine"] and got["channel"] == "stable", out
        rc, out, err = run("check")
        assert rc == 0 and "engine" in out and "1.6  ->  1.7" in out and "changed: engine" in out, out
        # The channel from brain.env, as the CLI is not run under the brain's environment.
        with open(os.path.join(d, "brain.env"), "w") as f:
            f.write("TEAPORT_UPDATE_CHANNEL=beta\n")
        assert json.loads(run("check", "--json")[1])["channel"] == "beta"
        assert json.loads(run("check", "--json", "--channel", "stable")[1])["channel"] == "stable"
        assert run("check", "--channel", "nightly")[0] == 2
        assert run("frobnicate")[0] == 2
        os.environ["TEAPORT_MANIFEST_URL"] = os.path.join(d, "missing.json")
        rc, out, err = run("check")
        assert rc == 1 and "cannot read the release manifest" in err, (rc, err)
    finally:
        updates.PREFIX, updates.ETC = saved_dirs
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_status_shows_the_release_line():
    from test_llm_account_error import _cli

    def line(health):
        found = [ln for ln in _cli("cmd_status", health).splitlines() if ln.lstrip().startswith("release")]
        return found[0].split("release", 1)[1].strip() if found else None
    assert line({"ok": True}) is None
    assert line({"ok": True, "update": {"state": "off"}}) is None
    assert line({"ok": True, "update": {"state": "unknown", "fresh": False}}) == "not checked yet"
    cur = {"state": "current", "channel": "stable", "fresh": True, "changed": []}
    assert line({"ok": True, "update": cur}) == "up to date (stable)"
    new = {"state": "available", "channel": "beta", "fresh": False, "changed": ["engine", "plugin"]}
    got = line({"ok": True, "update": new})
    assert got.startswith("a new release is out on beta: engine, plugin") and "last check failed" in got, got


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
