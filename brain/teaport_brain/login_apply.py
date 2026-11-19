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
#           (PENDING_PATH) is there; success removes the marker, so it works once. A
#           failed step undoes the ones before it.
# The marker means "no owner login yet". While it is there, a `teaport` account that a
# create began and never gave a password (a power cut between useradd and chpasswd) is
# finished by the next create -- its home directory if useradd did not get that far, its
# password, the sudo group -- and the marker goes. Only an account that is plainly such a
# leftover counts (Ops.unfinished: no password ever set, our GECOS, and a home that is
# missing or its own with no .ssh); any other `teaport` account is EXISTS, marker or not,
# so an account someone prepared -- a password set, keys planted -- is never handed to the
# owner as theirs: they see "already has a login". Provisioning refuses a box that already
# has a `teaport` account, or a `teaport` group (which useradd --user-group cannot take),
# before it writes the marker; the helper refuses the group too (GROUP).
# The marker is root's: provisioning writes it at seal (Teaspoon-AI/teaport-appliance#3),
# in a root-only directory outside every path the run user can write (/etc/teaport is the
# run user's through config_apply; the state directory is its own). Nothing in this
# package writes it, and no verb here does.
#
# Known window, not closed here: until the owner creates the login -- not merely until
# the box is set up: Settings offers the step for as long as the marker is there -- a
# compromised run user (the agent) could create it first, and the owner then sees "this
# Teaport already has a login" in place of the step. A run user compromised before the
# owner types the password could also capture it, since the setup page and Settings run
# as that user. The setup page makes the step required, which ends the window at setup
# for a box that goes through it; the marker limits it to a box whose owner has no login
# yet. Neither prevents it.
#
# The verb is the only argument; the rest is a JSON object on stdin, so the password never
# goes on a command line, where any user's ps can read it. The reply is a JSON object on
# stdout; a request this does not do gets {"refused": why, "why": code} and exit 2. The
# password reaches nothing but chpasswd's stdin: no reply, no log line, no error carries
# it -- chpasswd's own output is not passed on, and an error nobody expected is reported
# by its type alone (main), since an exception's text can quote its input. The process
# can leave no core dump (RLIMIT_CORE 0, and not dumpable), which would hold it.
#
# Nothing the run user can do stops a create part-way. Every signal that can be blocked is
# (sudo passes the run user's signals on: TERM, INT, HUP, but also USR1, USR2, ALRM and
# QUIT, which it can send from another process group). That is not enough: sudo itself,
# sent SIGALRM, ends its command -- HUP, TERM, then SIGKILL to its process group two
# seconds later (use_pty, Ubuntu's default). So a create runs detached (_detached): a
# child in a session and process group of its own does the whole of it, the lock
# included, and the process sudo started only relays its answer. sudo can kill that one;
# the child finishes regardless, its marker removal included. The child is still in the
# caller's cgroup, though: stopping the caller's unit (SIGKILL once its TimeoutStopSec
# runs out), the OOM killer, a reboot or a power cut can stop it part-way. A create takes
# about a second; the next one usually finishes what such a stop left -- when that is
# plainly a leftover (above). A cut inside useradd or userdel can also leave what reads
# as someone's login (EXISTS) or a lone `teaport` group (GROUP): a reset is the way out.
#
# Run as the sudoers line (packaging/deb/sudoers-sync) names it:
#   /usr/bin/python3 /usr/lib/teaport/login_apply.py <verb>
# the root-owned copy the teaport-brain package ships, on the SYSTEM python, never the
# venv's (like config_apply.py and wifi_apply.py). Standard library only. In the brain's
# tree owner_login.py reads its constants and password rule, and tests run it in-process
# (handle()) against a pretend useradd, chpasswd, getent, usermod, mkhomedir_helper and
# userdel, and as a real process for the signals and the detached create.
#
import ctypes
import fcntl
import json
import os
import resource
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
# The minimum in characters, as the owner counts; the maximum in UTF-8 bytes, as PAM does
# (pam_unix takes at most 512).
MIN_PASSWORD = 12
MAX_PASSWORD_BYTES = 256
MAX_REQUEST = 4096
TOOL_SECS = 30
# The account tools' PATH, our own (sudo's secure_path sets one too), and English output.
TOOL_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
# status's answers.
PENDING, CREATED, NOT_OFFERED = "pending", "created", "not_offered"
# Why a create did not happen, as codes; owner_login.py turns them into the words people
# see. FAILED: a step failed (a retry finishes or redoes it); NOT_OFFERED (status's word
# too): no pending marker; BUSY: another create holds the lock (it is not a failure: that
# one is under way).
SHORT, LONG, CHARACTER, EXISTS, FAILED, REFUSED, BUSY, GROUP = (
    "short", "long", "character", "exists", "failed", "refused", "busy", "group")
# The most account-tool calls a create makes (each up to TOOL_SECS): systemctl, getent
# passwd, then getent group, useradd, chpasswd and an undo's userdel for a new account,
# or getent shadow, mkhomedir_helper, chpasswd and usermod to finish one. The caller's
# timeout is built on it (owner_login.HELPER_SECS); tests hold both paths to it.
MAX_TOOL_CALLS = 6
# A shadow password field that no chpasswd ever set: empty, or locked with no hash behind
# it (useradd's "!", and the forms other tools write).
_NEVER_SET = ("", "!", "!!", "*", "!*")
GECOS = "Teaport owner"
# userdel's exit status for "no such user".
_USERDEL_NO_USER = 6
# prctl(2)'s PR_SET_DUMPABLE.
_PR_SET_DUMPABLE = 4
# Characters no password should have: control characters (a newline would end
# chpasswd's line), invisible formatting (zero-width, direction marks), lone surrogates
# (not text at all), and line or paragraph separators.
_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")


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
    # Before the length in bytes: a lone surrogate has no UTF-8 at all.
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in password):
        raise Refused("a password has no control or invisible characters", CHARACTER)
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise Refused(f"a password has at most {MAX_PASSWORD_BYTES} bytes", LONG)
    return password


def run_tool(argv: list[str], stdin: bytes | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=TOOL_SECS,
                           env=TOOL_ENV)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"
    except FileNotFoundError:   # a tool the system lacks: a failed step, said as one
        return 127, "", f"{argv[0]} is not installed"


# Where useradd reads the mode of a new home directory (HOME_MODE, else 0777 less UMASK).
LOGIN_DEFS = "/etc/login.defs"
HOME_MODE = 0o750   # neither set there (Ubuntu's own login.defs says HOME_MODE 0750)


def home_mode(path: str = LOGIN_DEFS) -> int:
    """The mode useradd gives a new home directory, as login.defs says."""
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in ("HOME_MODE", "UMASK"):
                    try:
                        values[parts[0]] = int(parts[1], 8) & 0o777
                    except ValueError:
                        pass
    except OSError:
        pass
    if "HOME_MODE" in values:
        return values["HOME_MODE"]
    return 0o777 & ~values["UMASK"] if "UMASK" in values else HOME_MODE


class Ops:
    """The account tools. `run(argv, stdin=None) -> (rc, stdout, stderr)`; `home` is the
    new account's home directory (a test's temporary one)."""

    def __init__(self, run=run_tool, home: str = HOME_DIR, login_defs: str = LOGIN_DEFS):
        self._run, self.home, self.login_defs = run, home, login_defs

    def passwd(self) -> list[str] | None:
        """The account's passwd entry, split; None when there is none."""
        rc, out, _ = self._run(["getent", "passwd", USERNAME])
        if rc not in (0, 2):   # 2: no such user; anything else is not an answer
            raise Refused("could not tell whether a login exists")
        return out.strip().split(":") if rc == 0 else None

    def group_exists(self) -> bool:
        rc, _, _ = self._run(["getent", "group", USERNAME])
        if rc not in (0, 2):
            raise Refused("could not tell whether a teaport group exists")
        return rc == 0

    def unfinished(self, entry: list[str]) -> bool:
        """`entry` is plainly a create's leftover, to be finished (the module comment):
        no password was ever set, the GECOS is ours, and the home directory is missing or
        a real directory of its own with no .ssh. Anything else is someone's login."""
        if len(entry) < 7 or not entry[2].isdigit() or entry[4] != GECOS:
            return False
        rc, out, _ = self._run(["getent", "shadow", USERNAME])
        fields = out.strip().split(":")
        if rc != 0 or len(fields) < 2 or fields[1] not in _NEVER_SET:
            return False
        try:
            st = os.lstat(self.home)
        except FileNotFoundError:
            return True
        return (stat.S_ISDIR(st.st_mode) and st.st_uid == int(entry[2])
                and not os.path.lexists(os.path.join(self.home, ".ssh")))

    def home_ready(self) -> bool:
        """An unfinished account's home directory, there (unfinished() checked it) or made
        as useradd would make it: mkhomedir_helper (libpam-modules-bin, which the package
        depends on) creates it owned by the account, from /etc/skel, at the mode
        login.defs gives a new home."""
        if os.path.lexists(self.home):
            return True
        umask = 0o777 & ~home_mode(self.login_defs)
        rc, _, _ = self._run(["mkhomedir_helper", USERNAME, f"{umask:04o}"])
        return rc == 0

    def run_user(self) -> str:
        _, out, _ = self._run(["systemctl", "show", "-p", "User", "--value", BRAIN_UNIT])
        return out.strip()

    def useradd(self) -> bool:
        rc, _, _ = self._run(["useradd", "--create-home", "--home-dir", self.home,
                              "--shell", SHELL, "--groups", ADMIN_GROUP, "--user-group",
                              "--comment", GECOS, USERNAME])
        return rc == 0

    def chpasswd(self, password: str) -> bool:
        # Only the exit status is used: chpasswd's output is never passed on.
        rc, _, _ = self._run(["chpasswd"], stdin=f"{USERNAME}:{password}\n".encode())
        return rc == 0

    def admin(self) -> bool:
        """An unfinished account gets what useradd gives a new one: the sudo group (kept
        with any others) and the shell."""
        rc, _, _ = self._run(["usermod", "--append", "--groups", ADMIN_GROUP,
                              "--shell", SHELL, USERNAME])
        return rc == 0

    def undo(self) -> None:
        """No `teaport` user: take away the one this create made. Under the lock, after
        create found none and no home directory, so any there now is this run's. No lookup
        first (one that fails would hide the result): "no such user" is as good. One that
        stays (userdel failed) is an unfinished account the next create finishes."""
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
    entry = ops.passwd()
    if not marker.present():
        return {"state": CREATED if entry else NOT_OFFERED}
    if entry:
        return {"state": PENDING if ops.unfinished(entry) else CREATED}
    if ops.group_exists():
        raise Refused("a teaport group already exists", GROUP)
    return {"state": PENDING}


def create(ops: Ops, marker: Marker, req: dict) -> dict:
    password = check_password(req.get("password"))
    if ops.run_user() == USERNAME:
        raise Refused(f"the brain runs as {USERNAME}: the login must be another account")
    entry = ops.passwd()
    if not marker.present():
        if entry:
            raise Refused("this box already has a login", EXISTS)
        raise Refused("no login is pending on this box", NOT_OFFERED)
    if entry:
        if not ops.unfinished(entry):
            raise Refused("this box already has a login", EXISTS)
        return _finish(ops, marker, password)
    if ops.group_exists():
        raise Refused("a teaport group already exists", GROUP)
    if os.path.lexists(ops.home):
        # An undo removes the home directory: one that was there first is not this run's.
        raise Refused(f"{ops.home} is already there")
    done = False
    try:
        if ops.useradd() and ops.chpasswd(password):
            # Gone with the success, or the create is undone: a marker left behind would
            # keep the gate open for whoever later removes the account.
            marker.remove()
            done = True
    finally:
        if not done:
            ops.undo()
    return {"ok": True, "username": USERNAME} if done else {"ok": False, "why": FAILED}


def _finish(ops: Ops, marker: Marker, password: str) -> dict:
    """The leftover an earlier create made (unfinished()): its home directory and group,
    its password last -- once it has one it is no longer a leftover (unfinished()), so
    nothing may be left to do after it but the marker. Nothing is undone on a failure:
    until chpasswd succeeds it is still a leftover, and the next create tries again."""
    if ops.home_ready() and ops.admin() and ops.chpasswd(password):
        marker.remove()
        return {"ok": True, "username": USERNAME}
    return {"ok": False, "why": FAILED}


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
        raise Refused("another login is being created", BUSY) from None
    return fd


def _harden() -> None:
    """No core dump of this process, which would hold the password: none at all
    (RLIMIT_CORE), and not dumpable (which also keeps other processes of the same uid
    from reading its memory)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _serve(verb: str, req) -> tuple[int, dict]:
    """One request, to its exit status and reply; an error nobody expected by its type
    alone (the module comment says why), in "error" for main to report."""
    try:
        if verb == "create":
            _lock()   # this process's own: the one that does the work holds it
        return 0, handle(Ops(), Marker(), verb, req)
    except Refused as e:
        return 2, {"refused": str(e), "why": e.why}
    except Exception as e:  # noqa: BLE001 -- see the module comment: never its text
        return 1, {"ok": False, "why": FAILED, "error": type(e).__name__}


def _detached(work) -> tuple[int, dict]:
    """`work()` in a child of its own session and process group, out of reach of what
    sudo does to its command (the module comment): its answer comes back on a pipe, and
    if this process is killed meanwhile the child finishes all the same. The child keeps
    the blocked signals and the core limits (inherited across fork) and lets go of the
    caller's stdin, stdout and stderr, so a caller whose sudo was killed is not held open
    by it."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setsid()
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            answer = json.dumps(work()).encode()
            try:
                while answer:
                    answer = answer[os.write(w, answer):]
            except OSError:
                pass   # the relay is gone (sudo killed it): the work is done regardless
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    while chunk := os.read(r, 65536):
        chunks.append(chunk)
    os.close(r)
    os.waitpid(pid, 0)
    try:
        rc, reply = json.loads(b"".join(chunks))
        return int(rc), dict(reply)
    except (ValueError, TypeError):
        return 1, {"ok": False, "why": FAILED, "error": "no answer from the create"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        _reply({"refused": "run as root, through the sudoers line", "why": REFUSED})
        return 2
    if len(argv) != 1:
        _reply({"refused": "usage: login_apply.py <verb> (the request as JSON on stdin)",
                "why": REFUSED})
        return 2
    # Blocked for good: the process ends after its reply, and a signal that came meanwhile
    # is never delivered (the module comment says why). SIGKILL and SIGSTOP cannot be.
    signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
    _harden()
    try:
        data = sys.stdin.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise Refused("the request is too long")
        try:
            req = json.loads(data) if data.strip() else {}
        except ValueError:
            raise Refused("the request is not JSON") from None
    except Refused as e:
        _reply({"refused": str(e), "why": e.why})
        return 2
    except Exception as e:  # noqa: BLE001 -- see the module comment: never its text
        sys.stderr.write(f"login_apply: {type(e).__name__}\n")
        _reply({"ok": False, "why": FAILED})
        return 1
    verb = argv[0]
    rc, reply = _detached(lambda: _serve(verb, req)) if verb == "create" else _serve(verb, req)
    if "error" in reply:
        sys.stderr.write(f"login_apply: {reply.pop('error')}\n")
    _reply(reply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
