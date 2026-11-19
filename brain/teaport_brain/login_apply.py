#
# teaport — the owner's login (#421): the one way the run user can make the box's admin
# account, `teaport`, with the password the owner typed on the setup page (wifi_setup.py)
# or in Settings (config_ui.py), both through owner_login.py. As root, behind sudo.
#
# The run user is the one the agent runs as (#416, #418). If it could make an account that
# has sudo whenever it liked, a compromised agent could make itself root. So this works
# once, and only on a box that asked for it:
#   status  is a login pending here (PENDING), already made (CREATED), or not offered
#           (NOT_OFFERED: no pending marker and no `teaport` user, as on a hobbyist's own
#           install, where this helper does nothing at all);
#   create  the `teaport` user, with a home directory, /bin/bash and the sudo group, its
#           password set through chpasswd on stdin. Only while the pending marker
#           (PENDING_PATH) is there and no `teaport` user (nor its home directory) exists;
#           success removes the marker, so it works once. A failed step undoes the ones
#           before it: all or nothing.
# The marker is root's: provisioning writes it at seal (Teaspoon-AI/teaport-appliance#3),
# in a root-only directory outside every path the run user can write (/etc/teaport is the
# run user's through config_apply; the state directory is its own). Nothing in this
# package writes it, and no verb here does.
#
# Known window, not closed here: on a sealed box nobody has set up yet, a compromised
# agent could create the login before the owner does. The owner then sees "this Teaport
# already has a login" where the step would be. The marker limits this to a box that
# has not been set up; it does not prevent it.
#
# The verb is the only argument; the rest is a JSON object on stdin, so the password never
# goes on a command line, where any user's ps can read it. The reply is a JSON object on
# stdout; a request this does not do gets {"refused": why, "why": code} and exit 2. The
# password reaches nothing but chpasswd's stdin: no reply, no log line, no error carries
# it -- chpasswd's own output is not passed on, and an error nobody expected is reported
# by its type alone (main), since an exception's text can quote its input.
#
# A create is never cut short by a stop: SIGTERM, SIGINT and SIGHUP are blocked for the
# whole run (the run user can signal its own sudo, and systemd stopping setup signals
# every process of the unit). One left half-done would be a `teaport` user with no
# password and the marker still there, which no later create could finish. Only SIGKILL
# can, which nothing here can answer.
#
# Run as the sudoers line (packaging/deb/sudoers-sync) names it:
#   /usr/bin/python3 /usr/lib/teaport/login_apply.py <verb>
# the root-owned copy the teaport-brain package ships, on the SYSTEM python, never the
# venv's (like config_apply.py and wifi_apply.py). Standard library only. In the brain's
# tree owner_login.py reads its constants and password rule, and tests run it in-process
# (handle()) against a pretend useradd, chpasswd, getent and userdel.
#
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import unicodedata

USERNAME = "teaport"
HOME_DIR = "/home/" + USERNAME
SHELL = "/bin/bash"
ADMIN_GROUP = "sudo"
# The one-shot gate. Its directory is root's, mode 0700: the run user cannot see in it,
# let alone write there (it asks `status`).
PENDING_PATH = "/var/lib/teaport-owner/pending"
LOCK_PATH = "/run/teaport-login-apply/lock"
# The brain's unit, whose User= is the run user: the login must not be that account.
BRAIN_UNIT = "teaport-brain.service"
MIN_PASSWORD = 12
MAX_PASSWORD = 256
MAX_REQUEST = 4096
TOOL_SECS = 30
# The account tools' PATH, our own (sudo's secure_path sets one too), and English output.
TOOL_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
# status's answers.
PENDING, CREATED, NOT_OFFERED = "pending", "created", "not_offered"
# Why a create did not happen, as codes; owner_login.py turns them into the words people
# see. FAILED: a step failed and what came before it was undone; NOT_OFFERED (status's
# word too): no pending marker.
SHORT, LONG, CHARACTER, EXISTS, FAILED, REFUSED = (
    "short", "long", "character", "exists", "failed", "refused")
# Characters no password should have: control characters (a newline would end
# chpasswd's line), invisible formatting (zero-width, direction marks), lone surrogates
# (not text at all), and line or paragraph separators.
_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Refused(Exception):
    """A request this helper does not do. `why` is one of the codes above."""

    def __init__(self, text: str, why: str = REFUSED):
        super().__init__(text)
        self.why = why


def check_password(password) -> str:
    """The password, if it is one this helper sets; Refused otherwise."""
    if not isinstance(password, str):
        raise Refused("the password must be a string")
    if len(password) < MIN_PASSWORD:
        raise Refused(f"a password has at least {MIN_PASSWORD} characters", SHORT)
    if len(password) > MAX_PASSWORD:
        raise Refused(f"a password has at most {MAX_PASSWORD} characters", LONG)
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in password):
        raise Refused("a password has no control or invisible characters", CHARACTER)
    return password


def run_tool(argv: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=TOOL_SECS,
                           env=TOOL_ENV)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"


class Ops:
    """The account tools. `run(argv, stdin=None) -> (rc, stdout, stderr)`; `home` is the
    new account's home directory (a test's temporary one)."""

    def __init__(self, run=run_tool, home: str = HOME_DIR):
        self._run, self.home = run, home

    def exists(self) -> bool:
        rc, _, _ = self._run(["getent", "passwd", USERNAME])
        if rc not in (0, 2):   # 2: no such user; anything else is not an answer
            raise Refused("could not tell whether a login exists")
        return rc == 0

    def run_user(self) -> str:
        _, out, _ = self._run(["systemctl", "show", "-p", "User", "--value", BRAIN_UNIT])
        return out.strip()

    def useradd(self) -> bool:
        rc, _, _ = self._run(["useradd", "--create-home", "--home-dir", self.home,
                              "--shell", SHELL, "--groups", ADMIN_GROUP, "--user-group",
                              "--comment", "Teaport owner", USERNAME])
        return rc == 0

    def chpasswd(self, password: str) -> bool:
        # Only the exit status is used: chpasswd's output is never passed on.
        rc, _, _ = self._run(["chpasswd"], stdin=f"{USERNAME}:{password}\n".encode())
        return rc == 0

    def undo(self) -> None:
        """No `teaport` user: take away the one this create made. Under the lock, after
        create found none and no home directory, so any there is now is this run's."""
        if self.exists():
            self._run(["userdel", "--remove", USERNAME])


class Marker:
    """The pending marker: there, and root's alone. A marker the run user could have put
    there -- in a directory it can write, or a file that is not root's or is a link -- is
    no marker: honouring it would let the run user open the gate again. `owner` is the
    uid the file and directory must belong to (root; a test's own)."""

    def __init__(self, path: str = PENDING_PATH, owner: int = 0):
        self.path, self.owner = path, owner

    def _roots(self, st: os.stat_result, kind) -> bool:
        return kind(st.st_mode) and st.st_uid == self.owner and not st.st_mode & 0o022

    def present(self) -> bool:
        try:
            directory = os.lstat(os.path.dirname(self.path))
            marker = os.lstat(self.path)
        except FileNotFoundError:
            return False
        return self._roots(directory, stat.S_ISDIR) and self._roots(marker, stat.S_ISREG)

    def remove(self) -> None:
        os.unlink(self.path)


def status(ops: Ops, marker: Marker) -> dict:
    if ops.exists():
        return {"state": CREATED}
    return {"state": PENDING if marker.present() else NOT_OFFERED}


def create(ops: Ops, marker: Marker, req: dict) -> dict:
    password = check_password(req.get("password"))
    if ops.run_user() == USERNAME:
        raise Refused(f"the brain runs as {USERNAME}: the login must be another account")
    if ops.exists():
        raise Refused("this box already has a login", EXISTS)
    if os.path.lexists(ops.home):
        # An undo removes the home directory: one that was there first is not this run's.
        raise Refused(f"{ops.home} is already there")
    if not marker.present():
        raise Refused("no login is pending on this box", NOT_OFFERED)
    done = False
    try:
        if ops.useradd() and ops.chpasswd(password):
            # Gone with the success, or the whole create is undone: a marker left behind
            # would open the gate again for whoever later removes the account.
            marker.remove()
            done = True
    finally:
        if not done:
            ops.undo()
    return {"ok": True, "username": USERNAME} if done else {"ok": False, "why": FAILED}


def handle(ops: Ops, marker: Marker, verb: str, req) -> dict:
    """One request: check it, do it, say how it went. Raises Refused for anything else."""
    if not isinstance(req, dict):
        raise Refused("the request must be a JSON object")
    if verb == "status":
        return status(ops, marker)
    if verb == "create":
        return create(ops, marker, req)
    raise Refused(f"unknown request {verb!r}")


def _lock(path: str = LOCK_PATH) -> int:
    """An exclusive lock on root's own file, held until the process exits: two creates at
    once would each find no user, and the second's undo would remove the first's."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Refused("another login is being created") from None
    return fd


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        _reply({"refused": "run as root, through the sudoers line", "why": REFUSED})
        return 2
    if len(argv) != 1:
        _reply({"refused": "usage: login_apply.py <verb> (the request as JSON on stdin)",
                "why": REFUSED})
        return 2
    # Blocked for good: the process ends after its reply, and a stop that came meanwhile
    # is never delivered (the module comment says why).
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        data = sys.stdin.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise Refused("the request is too long")
        try:
            req = json.loads(data) if data.strip() else {}
        except ValueError:
            raise Refused("the request is not JSON") from None
        if argv[0] == "create":
            _lock()
        _reply(handle(Ops(), Marker(), argv[0], req))
        return 0
    except Refused as e:
        _reply({"refused": str(e), "why": e.why})
        return 2
    except Exception as e:  # noqa: BLE001 -- see the module comment: never its text
        sys.stderr.write(f"login_apply: {type(e).__name__}\n")
        _reply({"ok": False, "why": FAILED})
        return 1


if __name__ == "__main__":
    sys.exit(main())
