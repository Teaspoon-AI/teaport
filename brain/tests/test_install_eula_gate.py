#
# install.sh's engine license gate with the download gate (worker/): acceptance recorded with
# Teaspoon AI (who accepted, the exact license, how), the download token that returns, a
# later run getting a new token from its receipt, the gated downloads carrying it, and
# --accept-eula=<version> naming the exact version. Also the manifest without access.api:
# the gate as it was, local only.
#
# As in test_install_engine_only.py, the functions are taken out of install.sh as they are
# and run by bash with curl replaced by a fake of the license server, against a temp dir.
#
# Run: python test_install_eula_gate.py   (or via pytest test_suite.py)
#
import hashlib
import json
import os
import pathlib
import pty
import re
import select
import subprocess
import time

import appliance
from tempdirs import tempdir
from test_install_engine_only import INSTALL, STUBS, _fn

LICENSE = "Teaport Engine License 1.0\nThe terms.\n"
SHA = hashlib.sha256(LICENSE.encode()).hexdigest()
API = "https://get.teaspoon.tech/api"
GATED = ["https://get.teaspoon.tech/dl/engine/", "https://get.teaspoon.tech/dl/models/"]

# The license server, the license text and the downloads, as curl sees them. The API's
# answers come from FAKE_ACCEPT / FAKE_TOKEN ("<status> <json>"); every request is logged.
FAKE_CURL = r'''
curl() {
  local out="" url="" code_fmt="" header_stdin=0 a
  while [ $# -gt 0 ]; do
    case "$1" in
      -o) out="$2"; shift ;;
      -w) code_fmt="$2"; shift ;;
      -H) [ "$2" = @- ] && header_stdin=1; shift ;;
      --data-binary|--max-time|--retry|-C) shift ;;
      -*) ;;
      *) url="$1" ;;
    esac
    shift
  done
  printf 'CURL %s %s\n' "$url" "$*" >> "$T/curl.log"
  case "$url" in
    */api/accept|*/api/token)
      local reply status body; body="$(cat)"
      printf '%s %s\n' "${url##*/}" "$body" >> "$T/api.log"
      if [ "${url##*/}" = accept ]; then reply="$FAKE_ACCEPT"; else reply="$FAKE_TOKEN"; fi
      status="${reply%% *}"
      [ "$status" = 000 ] && return 7
      printf '%s' "${reply#* }" > "$out"; printf '%s' "$status" ;;
    */eula/*) printf '%s' "$LICENSE_TEXT" > "$out" ;;
    *) local h="" n status; [ "$header_stdin" = 1 ] && h="$(cat)"
       printf '%s|%s\n' "$url" "$h" >> "$T/dl.log"
       # FAKE_DL: the status of each download in turn ("416 200"), 200 when it runs out.
       n="$(wc -l < "$T/dl.log")"; status="$(echo "${FAKE_DL:-}" | cut -d' ' -f"$n")"; status="${status:-200}"
       [ -z "$code_fmt" ] || printf '%s' "$status"
       [ "$status" = 200 ] || return 22
       printf 'bytes of %s' "$url" > "$out" ;;
  esac
}
own_dir() { mkdir -p "$1"; }
less() { cat "$@"; }
'''

FNS = ("manifest_cleanup", "exit_trap_base", "manifest_signers", "fetch_manifest", "mget", "gated", "sha256_ok", "download",
       "receipt_get", "api_post", "api_field",
       "device_id", "git_read", "installer_revision", "eula_identity", "eula_token", "dl_token", "eula_record",
       "eula_gate")


def _manifest(tmp, access=True, version="1.0"):
    m = {"channel": "stable",
         "engine": {"eula": {"version": version, "url": "https://get.teaspoon.tech/eula/l.txt", "sha256": SHA}}}
    if access:
        m["access"] = {"api": API, "gated": GATED}
    p = pathlib.Path(tmp, "mf.json")
    p.write_text(json.dumps(m))
    return p


def _script(tmp, body, **env):
    head = STUBS + FAKE_CURL + "".join(_fn(f) for f in FNS)
    # The manifest, a local one with no signature: taken as given only because the test says
    # so (TEAPORT_MANIFEST_UNSIGNED=1), and checked by the repository's manifest_sig.
    head += (f'MANIFEST_CHECK_PY="{INSTALL.parent}/brain/teaport_brain/manifest_sig.py"; MANIFEST_UNSIGNED=1\n'
             'MANIFEST_SIGNERS_PKG="$T/no-package/allowed_signers"; DOWNLOAD_HOST=get.teaspoon.tech\n')
    head += ('HERE="${HERE:-$T}"; STATE="$T/state"; EULA_RECEIPT="$STATE/eula-accepted.json"; GATED=(); DL_TOKEN=""\n'
             'ACCEPTANCE_ID=""; ACCEPT_USE=""; MANIFEST_URL="$T/mf.json"; EULA_API="" EULA_ID="" EULA_FACTORY=""\n'
             'ACCEPT_EULA="${ACCEPT_EULA:-}" ACCEPT_EMAIL="${ACCEPT_EMAIL:-}"\n'
             'ACCEPT_LICENSE_ID="${ACCEPT_LICENSE_ID:-}"\n')
    # TMPDIR: the functions' own mktemp files (the manifest, the license text) land in the
    # test's dir, removed with it. ACCEPT_*: only what a test passes (GitHub's runner images
    # set ACCEPT_EULA=Y for their own tools).
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("ACCEPT_")}
    e = {**inherited, "T": str(tmp), "TMPDIR": str(tmp), "LICENSE_TEXT": LICENSE,
         "FAKE_ACCEPT": '200 {"acceptance_id": "acc-0123456789", "token": "tok-first", "expires_at": "x"}',
         "FAKE_TOKEN": '200 {"token": "tok-again", "expires_at": "x"}', **env}
    return head + body, e


def _run(tmp, body, **env):
    script, e = _script(tmp, body, **env)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout + p.stderr


def _api(tmp):
    log = pathlib.Path(tmp, "api.log")
    return [(ln.split(" ", 1)[0], json.loads(ln.split(" ", 1)[1])) for ln in log.read_text().splitlines()] \
        if log.exists() else []


def _receipt(tmp):
    return json.loads(pathlib.Path(tmp, "state/eula-accepted.json").read_text())


GATE = 'fetch_manifest; eula_gate; echo "TOKEN=$DL_TOKEN"\n'
WHO = {"ACCEPT_EMAIL": "ada@example.com"}


def test_accepting_by_flag_records_who_and_what_with_teaspoon_ai():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)
    assert rc == 0, out
    assert "TOKEN=tok-first" in out and "and with Teaspoon AI" in out, out
    [(what, body)] = _api(tmp_path)
    assert what == "accept"
    device = body.pop("device_id")
    assert re.fullmatch(r"[0-9a-f]{64}", device) or device == "", device
    assert body == {"channel": "stable", "eula_version": "1.0", "eula_sha256": SHA, "method": "flag",
                    "email": "ada@example.com", "use": "personal",
                    "installer": "unknown"}, body   # HERE: the test dir, not a checkout
    r = _receipt(tmp_path)
    assert {k: r[k] for k in ("version", "sha256", "method", "acceptance_id", "recorded_with", "email", "use")} == {
        "version": "1.0", "sha256": SHA, "method": "flag", "acceptance_id": "acc-0123456789",
        "recorded_with": API, "email": "ada@example.com", "use": "personal"}, r
    assert "license_id" not in r and "name" not in r and "name" not in body
    sudo = pathlib.Path(tmp_path, "sudo.log").read_text()
    assert sudo.index("install -m 0600 /dev/null") < sudo.index("tee " + str(tmp_path / "state/eula-accepted.json")), sudo
    assert pathlib.Path(tmp_path, "state/EULA-v1.0.txt").read_text() == LICENSE


def test_the_record_names_the_installers_commit():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    here = tmp_path / "checkout"
    here.mkdir()
    git = ["git", "-C", str(here), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    rev = subprocess.run(git + ["rev-parse", "--short=12", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", HERE=str(here), **WHO)
    assert rc == 0, out
    [(_, body)] = _api(tmp_path)
    assert body["installer"] == f"git:{rev}", body
    # An install.sh inside another repository does not name that repository's commit.
    inner = here / "copied"
    inner.mkdir()
    (tmp_path / "state/eula-accepted.json").unlink(missing_ok=True)
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", HERE=str(inner), **WHO)
    assert rc == 0, out
    assert _api(tmp_path)[-1][1]["installer"] == "unknown"


def test_an_api_with_a_trailing_slash_is_asked_without_a_double_one():
    """access.api "…/api/" (manifest/check.py refuses it): /api/accept and /api/token, not
    /api//accept, which the gate 404s."""
    tmp_path = pathlib.Path(tempdir())
    m = _manifest(tmp_path)
    data = json.loads(m.read_text())
    data["access"]["api"] = API + "/"
    m.write_text(json.dumps(data))
    assert _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)[0] == 0
    rc, out = _run(tmp_path, GATE)
    assert rc == 0 and "TOKEN=tok-again" in out, out
    urls = [ln.split(" ")[1] for ln in pathlib.Path(tmp_path, "curl.log").read_text().splitlines()]
    assert f"{API}/accept" in urls and f"{API}/token" in urls and not any("//accept" in u or "//token" in u for u in urls), urls
    assert _receipt(tmp_path)["recorded_with"] == API


def test_licensed_use_sends_its_license_id():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", ACCEPT_LICENSE_ID="TS-2026-0042", **WHO)
    assert rc == 0, out
    [(_, body)] = _api(tmp_path)
    assert body["use"] == "licensed" and body["license_id"] == "TS-2026-0042", body
    assert _receipt(tmp_path)["license_id"] == "TS-2026-0042"


def test_the_flag_must_name_the_exact_version_and_a_refusal_does_not_say_it():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    for wrong in ("1", "1.00", "2.0", "yes"):
        rc, out = _run(tmp_path, GATE, ACCEPT_EULA=wrong, **WHO)
        assert rc == 1 and f"DIE --accept-eula={wrong} does not name the current engine license" in out, out
        assert "1.0" not in out.replace(f"--accept-eula={wrong}", ""), out
    assert _api(tmp_path) == [] and not pathlib.Path(tmp_path, "state/eula-accepted.json").exists()


def test_without_a_terminal_it_needs_the_flag_and_who_accepts():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    rc, out = _run(tmp_path, GATE)
    assert rc == 1 and "pass --accept-eula=<its version>" in out, out
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0")
    assert rc == 1 and "needs who accepts it: --email" in out, out
    assert _api(tmp_path) == []


def test_no_record_no_install():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    for reply, want in (('400 {"error": "bad_request", "message": "email: an email address"}',
                         "(HTTP 400: email: an email address)"),
                        ('500 {}', "(HTTP 500)"), ("000 x", "(HTTP 000)"),
                        ('200 {"acceptance_id": "acc-0123456789"}', "without an acceptance id or download token")):
        rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", FAKE_ACCEPT=reply, **WHO)
        assert rc == 1 and want in out, (reply, out)
        assert not pathlib.Path(tmp_path, "state/eula-accepted.json").exists()


def test_a_later_run_gets_a_new_token_from_its_receipt_without_asking():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    assert _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)[0] == 0
    pathlib.Path(tmp_path, "api.log").unlink()
    pathlib.Path(tmp_path, "curl.log").unlink()
    rc, out = _run(tmp_path, GATE)   # no flag, no terminal, no name
    assert rc == 0 and "TOKEN=tok-again" in out and "engine license v1.0 already accepted" in out, out
    assert _api(tmp_path) == [("token", {"acceptance_id": "acc-0123456789"})]
    assert "/eula/" not in pathlib.Path(tmp_path, "curl.log").read_text()


def test_the_server_asking_again_means_accepting_again():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    assert _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)[0] == 0
    gone = '409 {"error": "eula_changed", "message": "the license changed to version 1.1: read and accept it"}'
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=gone)
    assert rc == 1 and "asks for the engine license to be accepted again" in out and "pass --accept-eula" in out, out
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN='404 {"message": "no acceptance"}', ACCEPT_EULA="1.0",
                   FAKE_ACCEPT='200 {"acceptance_id": "acc-second-one", "token": "tok-2"}', **WHO)
    assert rc == 0 and "TOKEN=tok-2" in out, out
    assert _receipt(tmp_path)["acceptance_id"] == "acc-second-one"
    # 400: a receipt whose id the server cannot take (edited, cut short) is accepted again too.
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN='400 {"message": "acceptance_id: the id"}', ACCEPT_EULA="1.0", **WHO)
    assert rc == 0 and "asks for the engine license to be accepted again" in out and "TOKEN=tok-first" in out, out
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN='403 {"message": "this acceptance was revoked"}')
    assert rc == 1 and "refuses this box's acceptance of the engine license: this acceptance was revoked" in out, out


def test_a_receipt_never_recorded_with_teaspoon_ai_is_accepted_again():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/eula-accepted.json").write_text('{"version": "1.0", "method": "tty"}')
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)
    assert rc == 0 and "is not recorded with Teaspoon AI" in out and "TOKEN=tok-first" in out, out
    assert [w for w, _ in _api(tmp_path)] == ["accept"]


def test_a_manifest_without_access_keeps_the_gate_local():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path, access=False)
    rc, out = _run(tmp_path, GATE + 'echo "GATED=${#GATED[@]}"\n', ACCEPT_EULA="1.0")   # no name needed
    assert rc == 0 and "TOKEN=\n" in out and "GATED=0" in out and "with Teaspoon AI" not in out, out
    assert _api(tmp_path) == [] and "acceptance_id" not in _receipt(tmp_path)
    rc, out = _run(tmp_path, GATE)
    assert rc == 0 and "already accepted" in out and _api(tmp_path) == [], out


def test_gated_downloads_carry_the_token_on_stdin_and_others_none():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    body = ('fetch_manifest; DL_TOKEN=tok-xyz\n'
            'download https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\n'
            'download https://get.teaspoon.tech/dl/models/kokoro/k "$T/k" 7a7e47fb713f59bb0e0f13d1814484740518c8e789a875160fb8d004fae350f2\n'
            'download https://get.teaspoon.tech/dl/brain/b.deb "$T/b" 54fecd91a7c88b8345a565c0b9ce312a5776f8fc6ea70b5f15c168b3c664f9c3\n'
            'download https://get.teaspoon.tech/dl/engineering/x "$T/x" 99e70066e16964d8162b4e12feac063a4a81fac80fb3ce76616ea478259dd593\n')
    rc, out = _run(tmp_path, body)
    assert rc == 0, out
    assert pathlib.Path(tmp_path, "dl.log").read_text().splitlines() == [
        "https://get.teaspoon.tech/dl/engine/e|Authorization: Bearer tok-xyz",
        "https://get.teaspoon.tech/dl/models/kokoro/k|Authorization: Bearer tok-xyz",
        "https://get.teaspoon.tech/dl/brain/b.deb|",
        "https://get.teaspoon.tech/dl/engineering/x|"]
    assert "tok-xyz" not in pathlib.Path(tmp_path, "curl.log").read_text() + pathlib.Path(tmp_path, "sudo.log").read_text()
    rc, out = _run(tmp_path, 'fetch_manifest\ndownload https://get.teaspoon.tech/dl/engine/e "$T/e-again" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\n')
    assert rc == 1 and "no download token" in out, out


def test_a_dry_run_asks_the_server_nothing():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    rc, out = _run(tmp_path, "DRY_RUN=1\n" + GATE)
    assert rc == 0 and f"then record it with {API}" in out and _api(tmp_path) == [], out
    assert _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)[0] == 0
    pathlib.Path(tmp_path, "api.log").unlink()
    rc, out = _run(tmp_path, "DRY_RUN=1\n" + GATE)
    assert rc == 0 and f"get a download token from {API}" in out and _api(tmp_path) == [], out


def test_on_a_terminal_it_shows_the_license_then_asks_who_and_how():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    script, env = _script(tmp_path, GATE)
    pid, fd = pty.fork()
    if pid == 0:
        os.execve("/bin/bash", ["bash", "-c", script], env)
    answers = [("read the license", "\n"), ("Type \"accept\"", "accept\n"),
               ("email address", "ada@example.com\n"), ("1 or 2", "3\n"), ("1 or 2", "2\n"),
               ("license id", "TS-7\n")]
    seen, asked, deadline = "", [], time.time() + 20
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096).decode(errors="replace")
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        # Answer each prompt once: "1 or 2" comes twice, the second after a wrong answer.
        if answers and seen.count(answers[0][0]) > asked.count(answers[0][0]):
            asked.append(answers[0][0])
            os.write(fd, answers.pop(0)[1].encode())
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, seen
    assert "The terms." in seen and "TOKEN=tok-first" in seen, seen
    [(_, body)] = _api(tmp_path)
    assert (body["method"], body["email"], body["use"], body["license_id"]) == \
        ("tty", "ada@example.com", "licensed", "TS-7"), body
    assert "name" not in body and "full name" not in seen, seen



def test_the_parser_wants_a_version_and_values():
    for args, want in ((["--accept-eula"], "--accept-eula needs the license's version: --accept-eula=<version>"),
                       (["--accept-eula="], "--accept-eula needs the license's version"),
                       (["--license-id"], "--license-id needs a value"), (["--email", ""], "--email needs a value")):
        p = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True)
        assert p.returncode == 2 and want in p.stderr, (args, p.stderr)
    p = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert "--accept-eula=<version> --email <email>" in p.stdout, p.stdout


def test_a_name_from_an_older_script_is_taken_and_dropped():
    # Scripts written when the installer asked for a name still pass --name: they must keep
    # working, and the name must go nowhere.
    for args in (["--name", "Ada Lovelace", "--help"], ["--name=Ada Lovelace", "--help"]):
        p = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True)
        assert p.returncode == 0 and "--name is no longer used" in p.stderr, (args, p.stderr)



def test_a_server_that_cannot_be_asked_now_stops_only_a_download_that_needs_it():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    assert _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)[0] == 0
    down = '503 {"message": "down"}'
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=down)   # a repair with everything in place
    assert rc == 0 and "cannot get a download token" in out and "(HTTP 503: down)" in out and "TOKEN=\n" in out, out
    get = GATE + 'download https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\necho GOT\n'
    rc, out = _run(tmp_path, get, FAKE_TOKEN=down)
    assert rc == 1 and "cannot download https://get.teaspoon.tech/dl/engine/e without a token" in out, out
    assert [w for w, _ in _api(tmp_path)][-2:] == ["token", "token"]   # asked again at the download
    pathlib.Path(tmp_path, "api.log").unlink()
    rc, out = _run(tmp_path, GATE + 'FAKE_TOKEN=\'200 {"token": "tok-later"}\'\n'
                   + 'download https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\necho GOT\n', FAKE_TOKEN=down)
    assert rc == 0 and "GOT" in out, out
    assert pathlib.Path(tmp_path, "dl.log").read_text().splitlines()[-1].endswith("Bearer tok-later")


def test_a_part_left_whole_is_taken_and_one_past_the_end_starts_again():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    url = "https://get.teaspoon.tech/dl/engine/e"
    whole = f"bytes of {url}"
    sha = hashlib.sha256(whole.encode()).hexdigest()
    body = f'fetch_manifest; DL_TOKEN=tok\ndownload {url} "$T/e" {sha}\necho DONE\n'
    (tmp_path / "e.part").write_text(whole)                       # curl finished, the check did not
    rc, out = _run(tmp_path, body)
    assert rc == 0 and "DONE" in out and not (tmp_path / "dl.log").exists(), out
    assert (tmp_path / "e").read_text() == whole
    (tmp_path / "e").unlink()
    (tmp_path / "e.part").write_text(whole + " and more")          # longer than the file
    rc, out = _run(tmp_path, body, FAKE_DL="416 200")
    assert rc == 0 and "DONE" in out and (tmp_path / "e").read_text() == whole, out
    assert len((tmp_path / "dl.log").read_text().splitlines()) == 2
    (tmp_path / "e").unlink(); (tmp_path / "dl.log").unlink()
    rc, out = _run(tmp_path, body, FAKE_DL="416 416")
    assert rc == 1 and "download failed (HTTP 416)" in out, out
    (tmp_path / "dl.log").unlink()
    rc, out = _run(tmp_path, body, FAKE_DL="401")
    assert rc == 1 and f"download refused (HTTP 401): {url}" in out, out


def test_the_acceptance_id_and_who_accepted_never_reach_a_command_line():
    for fn in ("eula_token", "eula_record", "eula_gate"):
        body = _fn(fn)
        for secret in ('"$2")', '"$ACCEPTANCE_ID" "', '"$ACCEPT_EMAIL" "'):
            assert secret not in body.replace('TEAPORT_ACCEPTANCE_ID="$2"', ""), (fn, secret)


MAKER_ID = "maker-" + "A" * 24


def _factory_receipt(tmp, version="1.0"):
    """The receipt a box's factory agent writes as it provisions (teaport#464)."""
    (tmp / "state").mkdir(exist_ok=True)
    (tmp / "state/eula-accepted.json").write_text(json.dumps(
        {"version": version, "method": "factory", "acceptance_id": MAKER_ID, "recorded_with": API}))


def test_a_factory_receipt_gets_tokens_whatever_the_license_version_without_a_prompt():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path, version="2.0")   # the license moved on since the box was provisioned
    _factory_receipt(tmp_path, version="1.0")
    body = GATE + 'download https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\necho GOT\n'
    rc, out = _run(tmp_path, body)   # no terminal, no flag, no email
    assert rc == 0 and "GOT" in out and "TOKEN=tok-again" in out, out
    assert "factory install: the engine's files download under Teaspoon AI's record" in out, out
    assert _api(tmp_path) == [("token", {"acceptance_id": MAKER_ID})]
    assert "/eula/" not in pathlib.Path(tmp_path, "curl.log").read_text()   # no license shown
    assert _receipt(tmp_path)["method"] == "factory"                         # and the receipt kept


def test_a_factory_receipt_the_server_no_longer_takes_means_the_owner_accepts():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    _factory_receipt(tmp_path)
    expired = '409 {"error": "maker_expired", "message": "this box\'s factory install record no longer gets downloads"}'
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=expired)   # no terminal: nobody to accept
    assert rc == 1 and "no longer gets downloads" in out and "pass --accept-eula" in out, out
    assert "accepted again" not in out, out   # nobody accepted it before
    # The record asked once, not again as if it were an acceptance of this version.
    assert [w for w, _ in _api(tmp_path)] == ["token"], _api(tmp_path)
    pathlib.Path(tmp_path, "api.log").unlink()
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=expired, ACCEPT_EULA="1.0", **WHO)   # the owner accepts
    assert rc == 0 and "TOKEN=tok-first" in out, out
    r = _receipt(tmp_path)
    assert (r["method"], r["acceptance_id"]) == ("flag", "acc-0123456789"), r
    assert [w for w, _ in _api(tmp_path)] == ["accept"], _api(tmp_path)
    # Revoked: the maker's record says nothing about the owner, who accepts as when it ran
    # out (a revoked acceptance, by contrast, is refused outright).
    _factory_receipt(tmp_path)
    revoked = '403 {"message": "this box\'s factory install record was revoked"}'
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=revoked)
    assert rc == 1 and "was revoked" in out and "is to be accepted" in out and "pass --accept-eula" in out, out
    pathlib.Path(tmp_path, "api.log").unlink()
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN=revoked, ACCEPT_EULA="1.0", **WHO)
    assert rc == 0 and _receipt(tmp_path)["method"] == "flag" and [w for w, _ in _api(tmp_path)] == ["accept"], out


def test_a_factory_receipt_with_the_server_down_stops_only_a_download_that_needs_it():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    _factory_receipt(tmp_path)
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN='503 {"message": "down"}')
    assert rc == 0 and "factory install" in out and "TOKEN=\n" in out, out
    rc, out = _run(tmp_path, GATE + 'download https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\n', FAKE_TOKEN='503 {}')
    assert rc == 1 and "without a token" in out, out
    assert [w for w, b in _api(tmp_path)][-2:] == ["token", "token"]
    # The server back, but the record revoked meanwhile: said as the factory record's, not
    # as an acceptance this box never made.
    body = GATE + 'FAKE_TOKEN=\'403 {"message": "revoked"}\'\ndownload https://get.teaspoon.tech/dl/engine/e "$T/e" 699e02e23f9e17f9e5a847cb2630b9067e00e2f56e7bd26de79eec15463359f5\n'
    rc, out = _run(tmp_path, body, FAKE_TOKEN='503 {}')
    assert rc == 1 and "factory install record" in out and "refuses this box's acceptance" not in out, out


def test_a_factory_receipt_is_never_an_acceptance_of_its_version():
    """Without a license server to ask, or with no record id in it, a factory receipt
    whose version matches the manifest's still is no one's acceptance."""
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path, access=False)
    _factory_receipt(tmp_path, version="1.0")
    rc, out = _run(tmp_path, GATE)
    assert rc == 1 and "already accepted" not in out and "pass --accept-eula" in out, out
    _manifest(tmp_path)
    (tmp_path / "state/eula-accepted.json").write_text(json.dumps({"version": "1.0", "method": "factory"}))
    rc, out = _run(tmp_path, GATE)
    assert rc == 1 and "already accepted" not in out and _api(tmp_path) == [], out


def test_an_owner_accepting_with_the_flag_is_recorded_even_while_the_factory_record_works():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    _factory_receipt(tmp_path)
    rc, out = _run(tmp_path, GATE, ACCEPT_EULA="1.0", **WHO)
    assert rc == 0 and "the license is accepted instead" in out and "TOKEN=tok-first" in out, out
    assert [w for w, _ in _api(tmp_path)] == ["accept"]
    assert _receipt(tmp_path)["method"] == "flag"


def test_the_receipt_the_station_writes_exactly_gets_tokens():
    """RELEASING.md's receipt: method and acceptance_id, nothing else."""
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/eula-accepted.json").write_text(json.dumps({"method": "factory", "acceptance_id": MAKER_ID}))
    rc, out = _run(tmp_path, GATE)
    assert rc == 0 and "TOKEN=tok-again" in out and _api(tmp_path) == [("token", {"acceptance_id": MAKER_ID})], out
    rc, out = _run(tmp_path, GATE, FAKE_TOKEN='404 {"message": "no acceptance"}')
    assert rc == 1 and "pass --accept-eula" in out, out


def test_a_factory_dry_run_asks_nothing():
    tmp_path = pathlib.Path(tempdir())
    _manifest(tmp_path)
    _factory_receipt(tmp_path)
    rc, out = _run(tmp_path, "DRY_RUN=1\n" + GATE)
    assert rc == 0 and f"factory install record: get a download token from {API}" in out and _api(tmp_path) == [], out


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
