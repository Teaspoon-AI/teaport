#
# Readying a box from a terminal (#283): `teaport key set` and `teaport setup-card`
# (teaport_brain/provision.py, cli/teaport).
#
# What has to hold:
#   - key set saves a key as the setup page does (ai_account.adopt): checked first, then
#     the brain's key file and OpenClaw's, both or neither. Only a key that works is
#     saved; exit 0 saved, 3 refused by the provider, 2 anything else (an empty line or
#     a non-key included: no verdict on a key). The key comes on stdin, never shows in
#     the output. A run stopped by SIGTERM (or SIGHUP) mid-check saves nothing and says so.
#   - It runs in the brain's environment: brain.env's values (the endpoint, OpenClaw's
#     settings) count, the shell's do not -- a stray LLM_API_KEY or LLM_BASE_URL in the
#     operator's shell changes nothing; HOME is the run user's own and PATH systemd's, as a
#     unit has them. brain.env's own LLM_API_KEY would shadow the key
#     file, so nothing is saved and the line to remove is named.
#   - --starter writes the trial marker (the key's SHA-256) for an OpenRouter endpoint
#     only, and only once the save went through: a save that does not leaves the marker as
#     it was (none stays none). A marker that cannot be written is not "done".
#   - --new-key asks again when the provider refuses a key it has just made.
#   - setup-card gives the setup network's name, password and QR text as setup mode
#     shows them; no fixed password, or one setup would not use, is no card. With
#     --new-password a fresh grouped password replaces the old line in wifi-setup.env,
#     every other line kept; the new text never touches a file the run user owns (root
#     reading one could be pointed at any root-only file), holds only setup settings and
#     comments, reaches root on stdin, is installed root's and readable by the run user's
#     group (as install.sh's env files), and the card is made from that same text after
#     the install -- never a card whose password the box does not have. An all-hex setup name the unit refuses is
#     refused quietly: --json output stays one JSON object.
#   - The CLI never runs the brain's python as root, nor as anyone but the brain's user.
#
# No test talks to a provider (urllib's opener is replaced in the child process) or to a
# real OpenClaw (test_agent_key's fake CLI); every key here is made up, built at runtime.
# The child's password-database HOME is the test's own, and the run fails if the real
# user's teaport state or key directory changes (REAL_DIRS).
#
# Run: python test_provision.py   (or via pytest)
#
import argparse
import hashlib
import io
import json
import os
import pathlib
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402
from tempdirs import tempdir  # noqa: E402
from test_agent_key import FAKE_CLI  # noqa: E402
from teaport_brain import provision, wifi_setup  # noqa: E402

BRAIN = pathlib.Path(__file__).resolve().parents[1]
CLI = BRAIN.parent / "cli" / "teaport"
OPENROUTER = "https://openrouter.ai/api/v1"


REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
REAL_DIRS = [os.path.join(REAL_HOME, ".local", "state", "teaport"),
             os.path.join(REAL_HOME, ".config", "teaport")]


def _snapshot():
    out = {}
    for d in REAL_DIRS:
        try:
            for name in os.listdir(d):
                st = os.stat(os.path.join(d, name))
                out[(d, name)] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            pass
    return out


REAL_BEFORE = _snapshot()


def _key(c="5"):
    return "sk-or-v1-" + c * 64


# The child: urllib's opener answers as OpenRouter would (per FAKE_PROVIDER, a list of
# statuses for /key, one per request), then provision.main as the CLI runs it.
CHILD = r"""
import io, json, os, pwd, sys, time, urllib.error, urllib.request
answers = json.loads(sys.argv.pop(1))
# the run user's HOME (from the password database, as User= gives a unit) is the test's
real_getpwuid, test_home = pwd.getpwuid, os.environ["HOME"]
def fake_getpwuid(uid):
    p = real_getpwuid(uid)
    return pwd.struct_passwd((p.pw_name, p.pw_passwd, p.pw_uid, p.pw_gid, p.pw_gecos, test_home, p.pw_shell))
pwd.getpwuid = fake_getpwuid
record = {"data": {"limit": 5.0, "usage": 0.25, "limit_remaining": 4.75, "is_free_tier": False}}
credits = {"data": {"total_credits": 5.0, "total_usage": 0.25}}
class R(io.BytesIO):
    status = 200
def fake_open(self, req, timeout=None):
    url = req.full_url
    if url.endswith("/key"):
        status = answers.pop(0) if answers else 200
        if status == "slow":
            print("asking", file=sys.stderr, flush=True)
            time.sleep(30)
            status = 200
        if status != 200:
            raise urllib.error.HTTPError(url, status, "no", {}, io.BytesIO(b'{"error":{"message":"User not found."}}'))
        return R(json.dumps(record).encode())
    return R(json.dumps(credits).encode())
urllib.request.OpenerDirector.open = fake_open
from teaport_brain import provision
sys.exit(provision.main(sys.argv[1:]))
"""


class Box:
    """A run user's HOME, /etc/teaport, and test_agent_key's fake openclaw CLI."""

    def __init__(self, base_url=OPENROUTER, extra_env=""):
        self.home = tempdir("prov-home-")
        self.etc = tempdir("prov-etc-")
        self.cli_dir = tempdir("prov-cli-")
        cli = os.path.join(self.cli_dir, "openclaw")
        with open(cli, "w") as f:
            f.write(FAKE_CLI)
        os.chmod(cli, stat.S_IRWXU)
        with open(os.path.join(self.etc, "brain.env"), "w") as f:
            f.write(f"LLM_BASE_URL={base_url}\nTEAPORT_AGENT=openclaw\nOPENCLAW_BIN={cli}\n"
                    f"FAKE_OPENCLAW_DIR={self.cli_dir}\n{extra_env}")

    def modes(self, *modes):
        """What the fake CLI's next runs do, counted from here."""
        with open(os.path.join(self.cli_dir, "modes"), "w") as f:
            f.write("".join(m + "\n" for m in modes))
        try:
            os.remove(os.path.join(self.cli_dir, "n"))
        except FileNotFoundError:
            pass

    def _argv(self, args, provider):
        return [sys.executable, "-c", CHILD, json.dumps(list(provider)),
                "key", "--etc", self.etc, *args]

    def _env(self, shell_env=None):
        return {"HOME": self.home, "PATH": os.environ["PATH"], "PYTHONPATH": str(BRAIN),
                **(shell_env or {})}

    def run(self, *args, key="", provider=(), shell_env=None):
        return subprocess.run(self._argv(args, provider), input=key, capture_output=True,
                              text=True, env=self._env(shell_env), timeout=60)

    def start(self, *args, key="", provider=()):
        p = subprocess.Popen(self._argv(args, provider), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self._env())
        p.stdin.write(key)
        p.stdin.close()
        return p

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def read(self, *parts):
        try:
            return open(self.path(*parts)).read()
        except FileNotFoundError:
            return None

    @property
    def key_file(self):
        return self.read(".config", "teaport", "llm_key")

    @property
    def marker(self):
        return self.read(".local", "state", "teaport", "starter_key_sha256")

    @property
    def openclaw_keys(self):
        try:
            return open(os.path.join(self.cli_dir, "keys")).read().splitlines()
        except FileNotFoundError:
            return []


# ------------------------------------------------------------------ key set

def test_a_starter_key_is_saved_for_the_brain_and_openclaw_and_marked():
    box, key = Box(), _key()
    # the operator's shell carries its own endpoint and key: neither counts
    p = box.run("--starter", "--json", key=key + "\n",
                shell_env={"LLM_API_KEY": _key("9"), "LLM_BASE_URL": "http://elsewhere/v1"})
    assert p.returncode == 0, p.stderr + p.stdout
    out = json.loads(p.stdout)
    assert out["ok"] and out["starter"] and out["state"] == "works"
    assert (out["limit"], out["remaining"]) == (5.0, 4.75)
    assert key not in p.stdout + p.stderr
    assert box.key_file == key + "\n"
    assert box.marker.strip() == hashlib.sha256(key.encode()).hexdigest()
    assert box.openclaw_keys == [key]  # on stdin, brain.env's OPENCLAW_BIN and agent setting
    argv = open(os.path.join(box.cli_dir, "argv")).read()
    assert "models auth paste-api-key --provider openrouter" in argv and key not in argv


def test_a_plain_save_writes_no_marker_and_says_it_in_words():
    box, key = Box(), _key()
    p = box.run(key=key)
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith("saved: the key works (4.75 USD left of 5.00)")
    assert box.key_file == key + "\n" and box.marker is None


def test_a_refused_key_is_not_saved_and_the_marker_stays_as_it_was():
    box, old, new = Box(), _key("1"), _key("2")
    assert box.run("--starter", key=old).returncode == 0
    marker = box.marker
    p = box.run("--starter", "--json", key=new, provider=[401])
    assert p.returncode == provision.REFUSED, p.stderr + p.stdout
    out = json.loads(p.stdout)
    assert not out["ok"] and out["state"] == "refused" and "User not found" in out["reason"]
    assert box.key_file == old + "\n" and box.marker == marker
    assert box.openclaw_keys == [old]


def test_a_refused_trial_key_writes_no_marker_where_there_was_none():
    box = Box()
    p = box.run("--starter", key=_key(), provider=[403])
    assert p.returncode == provision.REFUSED, p.stderr
    assert box.marker is None and box.key_file is None


def test_a_marker_that_cannot_be_written_is_not_done():
    box = Box()
    os.makedirs(box.path(".local", "state", "teaport", "starter_key_sha256"))  # in the way
    p = box.run("--starter", key=_key())
    assert p.returncode == provision.NOT_DONE
    assert "the key was saved, but it could not be marked as the trial key" in p.stderr
    assert box.key_file == _key() + "\n"


def test_a_run_stopped_mid_check_saves_nothing_and_says_so():
    for sig in (signal.SIGTERM, signal.SIGHUP):
        box = Box()
        p = box.start("--starter", key=_key(), provider=["slow"])
        assert p.stderr.readline().strip() == "asking"
        p.send_signal(sig)
        p.wait(timeout=30)
        # not communicate(): its stdin is closed already (start), and 3.12's communicate
        # flushes a stdin it still holds
        err = p.stderr.read()
        p.stdout.close()
        p.stderr.close()
        assert p.returncode == provision.NOT_DONE, (sig, err)
        assert "stopped: the key was not saved" in err, err
        assert box.key_file is None and box.marker is None and box.openclaw_keys == []


def test_a_stop_inside_an_event_loop_callback_is_not_lost():
    # asyncio logs and drops an Exception raised in a callback; a stop must reach main
    import asyncio

    async def run():
        asyncio.get_running_loop().call_soon(provision._on_signal, signal.SIGTERM, None)
        await asyncio.sleep(1)
        return "finished"

    old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        asyncio.run(run())
        raise AssertionError("the stop was lost")
    except KeyboardInterrupt as e:
        assert isinstance(e, provision.Stopped)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


def test_a_key_openclaw_does_not_take_is_not_saved():
    box, old, new = Box(), _key("1"), _key("2")
    assert box.run("--starter", key=old).returncode == 0
    marker = box.marker
    box.modes("fail")
    p = box.run("--starter", key=new)
    assert p.returncode == provision.NOT_DONE, p.stderr
    assert "not saved" in p.stderr
    assert box.key_file == old + "\n" and box.marker == marker


def test_a_copy_in_brain_env_stops_the_save_and_is_named():
    box = Box(extra_env=f"LLM_API_KEY={_key('3')}\n")
    p = box.run(key=_key())
    assert p.returncode == provision.NOT_DONE
    assert "brain.env sets LLM_API_KEY" in p.stderr and _key("3") not in p.stderr
    assert box.key_file is None and box.openclaw_keys == []


def test_starter_is_for_openrouter_only():
    box = Box(base_url="http://127.0.0.1:9/v1")
    p = box.run("--starter", key=_key())
    assert p.returncode == provision.NOT_DONE and "--starter is for an OpenRouter key" in p.stderr
    assert box.key_file is None and box.marker is None


def test_a_key_just_made_is_asked_again_on_a_refusal():
    box = Box()
    assert box.run(key=_key(), provider=[401]).returncode == provision.REFUSED
    p = box.run("--new-key", key=_key(), provider=[401])
    assert p.returncode == 0, p.stderr
    assert box.key_file == _key() + "\n"


def test_an_empty_line_or_a_non_key_is_no_verdict_on_a_key():
    box = Box()
    p = box.run("--starter", key="\n")
    assert p.returncode == provision.NOT_DONE and "the key is empty" in p.stderr
    p = box.run(key="two\nlines")
    assert p.returncode == provision.NOT_DONE and "doesn't look like a key" in p.stderr
    assert box.marker is None and box.key_file is None


# ------------------------------------------------------------------ setup card

class FakeNM:
    def __init__(self, dev="wlP1p1s0", mac="48:8f:4c:01:9e:35"):
        self.dev, self._mac = dev, mac

    def wifi_device(self):
        return self.dev

    def mac(self, dev):
        return self._mac


def _card(env_text="", nm=None, **flags):
    """provision.card as main runs it: the environment brain.env and wifi-setup.env give."""
    args = argparse.Namespace(**{"json": True, "new": False, "etc": "/etc/teaport", **flags})
    saved = dict(os.environ)
    env = provision.unit_environment("LLM_BASE_URL=" + OPENROUTER, env_text)
    os.environ.clear()
    os.environ.update(env)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = provision.card(args, env_text, nm=nm or FakeNM())
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return rc, json.loads(buf.getvalue())


def test_the_card_is_what_setup_mode_shows():
    rc, out = _card("WIFI_SETUP_PASSWORD=k7mq-x4tn-p9wr\n")
    assert rc == 0, out
    assert out["ssid"] == wifi_setup.setup_ssid("48:8f:4c:01:9e:35") == "teaport-9e35"
    assert out["password"] == "k7mq-x4tn-p9wr" and not out["new"]
    assert out["qr"] == wifi_setup.wifi_qr("teaport-9e35", "k7mq-x4tn-p9wr")


def test_an_all_hex_name_is_refused_quietly():
    # the unit refuses it (phones read it as bytes) and logs that, to stdout; the card's
    # stdout is its JSON alone
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc, out = _card("WIFI_SETUP_SSID=cafe\nWIFI_SETUP_PASSWORD=k7mq-x4tn-p9wr\n")
    assert rc == 0 and out["ssid"] == "teaport-9e35" and buf.getvalue() == ""


def test_a_named_setup_network_and_an_escaped_qr():
    rc, out = _card("WIFI_SETUP_SSID=Tea;port\nWIFI_SETUP_PASSWORD=abcd-efgh-jk23\n")
    assert rc == 0 and out["ssid"] == "Tea;port"
    assert out["qr"] == r"WIFI:T:WPA;S:Tea\;port;P:abcd-efgh-jk23;;"


def test_no_fixed_password_or_one_setup_would_not_use_is_no_card():
    rc, out = _card("")
    assert rc == provision.NO_CARD and "nothing to print" in out["reason"]
    rc, out = _card("WIFI_SETUP_PASSWORD=short\n")
    assert rc == provision.NO_CARD and "random one" in out["reason"]
    rc, out = _card("WIFI_SETUP_PASSWORD=k7mq-x4tn-p9wr\n", nm=FakeNM(dev=None))
    assert rc == provision.NO_CARD and "no Wi-Fi device" in out["reason"]


def test_a_new_password_replaces_only_its_line_and_comes_on_stdout_alone():
    before = "# printed on the insert\nWIFI_SETUP_SSID=teaport-shop\nWIFI_SETUP_PASSWORD=old-old-old\n"
    p = subprocess.run([sys.executable, "-c", "import sys; from teaport_brain import provision; "
                        "sys.exit(provision.main(['setup-env']))"], input=before,
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "PYTHONPATH": str(BRAIN)})
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[:2] == ["# printed on the insert", "WIFI_SETUP_SSID=teaport-shop"] and len(lines) == 3
    name, _, password = lines[2].partition("=")
    assert name == "WIFI_SETUP_PASSWORD" and wifi_setup.valid_password(password)
    assert re.fullmatch(r"[abcdefghjkmnprstuvwxyz23456789]{4}(-[abcdefghjkmnprstuvwxyz23456789]{4}){2}",
                        password)
    # and the card made from that text shows it
    rc, out = _card(p.stdout, new=True)
    assert rc == 0 and out["new"] and out["password"] == password and out["ssid"] == "teaport-shop"


def test_a_byte_order_mark_does_not_hide_the_old_password():
    p = subprocess.run([sys.executable, "-c", "import sys; from teaport_brain import provision; "
                        "sys.exit(provision.main(['setup-env']))"],
                       input="\ufeffWIFI_SETUP_PASSWORD=old-old-old\nWIFI_SETUP_SSID=s\n",
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "PYTHONPATH": str(BRAIN)})
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert lines[0] == "WIFI_SETUP_SSID=s" and lines[1].startswith("WIFI_SETUP_PASSWORD=")
    assert "old-old-old" not in p.stdout and "\ufeff" not in p.stdout and len(lines) == 2


def test_with_password_keeps_comments_naming_it():
    text = "#WIFI_SETUP_PASSWORD=example\nWIFI_SETUP_PASSWORD=a\nexport WIFI_SETUP_PASSWORD = b\n"
    assert provision.with_password(text, "new") == "#WIFI_SETUP_PASSWORD=example\nWIFI_SETUP_PASSWORD=new\n"
    assert provision.with_password("", "new") == "WIFI_SETUP_PASSWORD=new\n"


def test_the_units_environment_drops_the_shells():
    saved, locale_file = dict(os.environ), provision.LOCALE_FILE
    locale = os.path.join(tempdir("prov-locale-"), "locale")
    with open(locale, "w") as f:
        f.write('LANG="en_US.UTF-8"\nLC_TIME=en_GB.UTF-8\nOTHER=x\n')
    try:
        provision.LOCALE_FILE = locale
        os.environ.update({"HOME": "/elsewhere", "PATH": "/opt/npm/bin:/usr/bin",
                           "LLM_API_KEY": "x", "OPENAI_API_KEY": "y", "LANG": "de_DE.UTF-8",
                           "XDG_RUNTIME_DIR": "/run/user/4242"})
        env = provision.unit_environment("LLM_BASE_URL=a\nLLM_MODEL='m'\n", "WIFI_SETUP_SSID=s\n")
    finally:
        provision.LOCALE_FILE = locale_file
        os.environ.clear()
        os.environ.update(saved)
    me = pwd.getpwuid(os.getuid())
    assert (env["HOME"], env["USER"]) == (me.pw_dir, me.pw_name)  # as User= gives them
    assert env["PATH"] == provision.UNIT_PATH
    # the system's locale, not the SSH client's; no runtime dir (openclaw_client gives one)
    assert (env["LANG"], env["LC_TIME"]) == ("en_US.UTF-8", "en_GB.UTF-8") and "OTHER" not in env
    assert "XDG_RUNTIME_DIR" not in env
    assert "LLM_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert (env["LLM_BASE_URL"], env["LLM_MODEL"], env["WIFI_SETUP_SSID"]) == ("a", "m", "s")


# ------------------------------------------------------------------ the CLI

# A stand-in for the brain's python (-I -m teaport_brain.provision <sub> ...): logs its
# argv, and its stdin per subcommand. setup-env prints FAKE_FRESH (default: one password
# line); card prints a card. FAKE_MODE=fail: setup-env fails.
FAKE_PY = r"""#!/bin/bash
d="$FAKE_DIR"; sub="$4"; printf '%s\n' "$*" >> "$d/argv"; cat > "$d/stdin.$sub"
case "$sub" in
  setup-env) [ "${FAKE_MODE:-}" = fail ] && exit 1
             printf '%s\n' "${FAKE_FRESH:-WIFI_SETUP_PASSWORD=abcd-efgh-jk23}" ;;
  card) echo "Network   teaport-9e35" ;;
esac
"""
FAKE_SUDO = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DIR/sudo"
case "$1" in
  install) [ "${FAKE_SUDO_FAIL:-}" = 1 ] && exit 1; n=$#; src="${@:n-1:1}"; dst="${@:n:1}"; cp "$src" "$dst" ;;
  cat) chmod u+r "$2"; cat "$2" ;;   # root reads it whatever its mode
esac
"""
FAKE_SYSTEMCTL = '#!/bin/sh\necho "$FAKE_UNIT_USER"\n'
FAKE_ID = '#!/bin/sh\n[ "$1" = -u ] && [ -n "$FAKE_UID" ] && { echo "$FAKE_UID"; exit 0; }\nexec /usr/bin/id "$@"\n'


def _cli(*args, stdin="", **env):
    d = tempdir("prov-clibox-")
    prefix, etc, stubs = (os.path.join(d, x) for x in ("opt", "etc", "bin"))
    for x in (os.path.join(prefix, "brain", "bin"), etc, stubs):
        os.makedirs(x)
    for path, text in ((os.path.join(prefix, "brain", "bin", "python"), FAKE_PY),
                       (os.path.join(stubs, "sudo"), FAKE_SUDO),
                       (os.path.join(stubs, "systemctl"), FAKE_SYSTEMCTL),
                       (os.path.join(stubs, "id"), FAKE_ID)):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    full = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "TEAPORT_PREFIX": prefix,
            "TEAPORT_ETC": etc, "FAKE_DIR": d, "FAKE_UNIT_USER": me, **env}
    if "SETUP_ENV" in env:
        with open(os.path.join(etc, "wifi-setup.env"), "w") as f:
            f.write(env["SETUP_ENV"])
        os.chmod(os.path.join(etc, "wifi-setup.env"), int(env.get("SETUP_ENV_MODE", "600"), 8))
    p = subprocess.run(["bash", str(CLI), *args], input=stdin, capture_output=True, text=True,
                       env=full, timeout=30)

    def read(name):
        try:
            return open(os.path.join(d, name)).read()
        except FileNotFoundError:
            return None
    p.read, p.etc = read, etc
    return p


def test_key_set_passes_the_key_on_stdin_and_the_etc_dir():
    p = _cli("key", "set", "--starter", "--json", stdin="the-key\n")
    assert p.returncode == 0, p.stderr
    assert p.read("argv").strip() == f"-I -m teaport_brain.provision key --etc {p.etc} --starter --json"
    assert p.read("stdin.key") == "the-key\n"


def test_the_cli_runs_the_brains_python_as_its_user_only():
    p = _cli("key", "set", FAKE_UID="0", stdin="k")
    assert p.returncode != 0 and "not as root" in p.stderr and p.read("argv") is None
    p = _cli("setup-card", FAKE_UNIT_USER="someone-else")
    assert p.returncode != 0 and "run this as someone-else" in p.stderr and p.read("argv") is None


def test_a_new_password_reaches_root_on_stdin_and_the_card_shows_what_was_installed():
    fresh = "# insert\nWIFI_SETUP_SSID=x\nWIFI_SETUP_PASSWORD=abcd-efgh-jk23"
    p = _cli("setup-card", "--new-password", "--json", SETUP_ENV="WIFI_SETUP_SSID=x\n",
             FAKE_FRESH=fresh)
    assert p.returncode == 0, p.stderr
    assert "Network   teaport-9e35" in p.stdout
    assert p.read("stdin.setup-env") == "WIFI_SETUP_SSID=x"   # the file's text
    # root reads the new text from stdin, never from a file this user could swap
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()
    assert p.read("sudo").splitlines() == [
        f"install -D -m 0640 -o root -g {group} /dev/stdin {p.etc}/wifi-setup.env"]
    assert open(os.path.join(p.etc, "wifi-setup.env")).read() == fresh + "\n"
    # the card is made from the installed text, after the install
    assert p.read("stdin.card") == fresh
    assert p.read("argv").splitlines()[-1].endswith("card --etc " + p.etc + " --json --new")


def test_no_card_when_the_password_could_not_be_installed():
    p = _cli("setup-card", "--new-password", FAKE_SUDO_FAIL="1")
    assert p.returncode != 0 and "Network" not in p.stdout and p.read("stdin.card") is None
    assert "the setup password was not changed" in p.stderr
    p = _cli("setup-card", "--new-password", FAKE_MODE="fail")
    assert p.returncode == 1 and "could not make a new setup password" in p.stderr
    assert p.read("sudo") is None and p.read("stdin.card") is None


def test_a_line_that_is_no_setup_setting_is_never_installed():
    for line in ("LD_PRELOAD=/tmp/x.so", "PATH=/tmp", "garbage", "export LD_LIBRARY_PATH=/tmp",
                 "WIFI_SETUP_SSID=y\rLD_PRELOAD=/tmp/x.so"):  # \r: a line end to systemd
        p = _cli("setup-card", "--new-password", SETUP_ENV="WIFI_SETUP_SSID=x\n",
                 FAKE_FRESH=f"WIFI_SETUP_SSID=x\n{line}\nWIFI_SETUP_PASSWORD=abcd-efgh-jk23")
        assert p.returncode == 1, (line, p.stderr)
        assert "is not a setup setting" in p.stderr or "carriage return" in p.stderr, p.stderr
        assert p.read("sudo") is None and p.read("stdin.card") is None
        assert open(os.path.join(p.etc, "wifi-setup.env")).read() == "WIFI_SETUP_SSID=x\n"


def test_wifi_setup_env_is_read_with_sudo_only_when_it_must_be():
    p = _cli("setup-card", SETUP_ENV="WIFI_SETUP_PASSWORD=abcd-efgh-jk23\n")
    assert p.returncode == 0 and p.read("sudo") is None
    assert p.read("stdin.card") == "WIFI_SETUP_PASSWORD=abcd-efgh-jk23"
    if os.geteuid() == 0:
        raise appliance.Unavailable("root reads a mode-000 file anyway")
    p = _cli("setup-card", SETUP_ENV="WIFI_SETUP_PASSWORD=abcd-efgh-jk23\n", SETUP_ENV_MODE="000")
    assert p.returncode == 0, p.stderr
    assert p.read("sudo").startswith("cat ") and p.read("stdin.card") == "WIFI_SETUP_PASSWORD=abcd-efgh-jk23"


def test_zz_the_real_users_files_are_untouched():
    # sorted last: after every other case here has run
    assert _snapshot() == REAL_BEFORE, "a test wrote to the real user's teaport files"


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
