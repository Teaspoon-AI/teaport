#
# The owner's login helper (login_apply.py, #421) is the run user's only way to make an
# account, and the run user is the agent's: what it takes must be one login, once, on a
# box provisioning marked for it. What has to hold:
#   - create is refused, before any account tool changes anything, without the pending
#     marker, with a marker the run user could have written, once a `teaport` user or its
#     home directory exists, when the brain itself runs as `teaport`, and for a password
#     too short, too long, not text, or with control or invisible characters;
#   - a create makes the user with a home, /bin/bash and the sudo group, sets the password
#     through chpasswd's stdin, and removes the marker: it works once;
#   - a step that fails undoes the ones before it, and the marker stays; an account a
#     create left unfinished (killed between useradd and chpasswd, or an undo that could
#     not run) is pending while the marker is there, and the next create finishes it;
#   - the password reaches chpasswd's stdin and nothing else: no command line, no reply,
#     no stdout or stderr, even when a tool or the helper itself fails;
#   - main refuses without root, takes its request on stdin, blocks every signal it can
#     (a real process, sent the ones sudo passes on, still finishes), leaves no core dump,
#     and lets one create run at a time (a second is BUSY, not a failure);
#   - the run user's side (owner_login.py) calls it through sudo with the password on
#     stdin, and the package ships it with its sudoers line.
# All passwords here are made up for the test.
#
# Run: python test_login_apply.py   (or via pytest test_suite.py)
#
import io
import json
import os
import pathlib
import re

import pytest

from teaport_brain import i18n, login_apply, owner_login

ROOT = pathlib.Path(__file__).resolve().parents[2]
GOOD = "correct-horse-battery"   # a made-up password


class FakeTools:
    """getent, systemctl, useradd, chpasswd, usermod and userdel as a state: the users
    there are, their groups, what each call was. `fail`: a tool that exits 1 (useradd's
    "half" makes the user and then fails, as a useradd that could not finish the home
    directory does). `killed`: the process dies in that tool (a SIGKILL or a power cut:
    nothing after it runs)."""

    def __init__(self, home, users=(), run_user="teaspoon", fail=(), noisy=False, killed=None):
        self.home, self.users, self.run_user = home, dict.fromkeys(users, ""), run_user
        self.fail, self.noisy, self.killed = set(fail), noisy, killed
        self.groups = {u: set() for u in users}
        self.calls, self.stdins = [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(list(argv))
        if stdin is not None:
            self.stdins.append(stdin)
        tool = argv[0]
        if tool == self.killed:
            raise Killed(tool)
        if tool == "getent":
            uid = os.getuid()   # the test's own: the home directory it makes is "the account's"
            return (0, f"{argv[2]}:x:{uid}:{uid}::/home/{argv[2]}:/bin/bash\n", "") if argv[2] in self.users else (2, "", "")
        if tool == "systemctl":
            return 0, self.run_user + "\n", ""
        if tool == "useradd":
            if "useradd" in self.fail:
                return 1, "", "useradd: cannot lock /etc/passwd"
            self.users[argv[-1]] = ""
            self.groups[argv[-1]] = {argv[argv.index("--groups") + 1]}
            os.makedirs(self.home)
            if "useradd-half" in self.fail:
                return 12, "", "useradd: cannot create directory"
            return 0, "", ""
        if tool == "chpasswd":
            user, _, pw = stdin.decode().rstrip("\n").partition(":")
            if "chpasswd" in self.fail:
                # A chatty tool that echoes what it was given: none of it may be passed on.
                return 1, "", f"chpasswd: line 1: {pw if self.noisy else ''} refused"
            self.users[user] = pw
            return 0, "", ""
        if tool == "mkhomedir_helper":
            if "mkhomedir_helper" in self.fail:
                return 127, "", "mkhomedir_helper is not installed"
            # As the real one: the account's directory, /etc/skel's files, 0777 less umask.
            os.mkdir(self.home, 0o777 & ~int(argv[2], 8))
            os.chmod(self.home, 0o777 & ~int(argv[2], 8))
            (pathlib.Path(self.home) / ".profile").write_text("# skel\n")
            return 0, "", ""
        if tool == "usermod":
            if "usermod" in self.fail:
                return 1, "", "usermod: cannot lock /etc/group"
            self.groups[argv[-1]].add(argv[argv.index("--groups") + 1])
            return 0, "", ""
        if tool == "userdel":
            if "userdel" in self.fail:
                return 1, "", "userdel: cannot lock /etc/passwd"
            if argv[-1] not in self.users:
                return 6, "", "userdel: user 'teaport' does not exist"
            self.users.pop(argv[-1])
            if os.path.isdir(self.home):
                os.rmdir(self.home)
            return 0, "", ""
        raise AssertionError(argv)

    def changes(self):
        return [c for c in self.calls if c[0] in ("useradd", "chpasswd", "usermod", "userdel")]


class Killed(BaseException):
    """The helper's process dying in a tool: no finally of it runs on the box."""


def _box(tmp_path, marker=True, **kw):
    """A pretend box: its tools, its marker (root's here = this test's uid), the ops."""
    d = tmp_path / "owner"
    d.mkdir(mode=0o700)
    if marker:
        (d / "pending").write_text("")
        (d / "pending").chmod(0o600)
    tools = FakeTools(str(tmp_path / "home/teaport"), **kw)
    (tmp_path / "home").mkdir()
    ops = login_apply.Ops(tools, home=tools.home)
    return tools, ops, login_apply.Marker(str(d / "pending"), owner=os.getuid())


@pytest.mark.parametrize("req, why", [
    ({"password": "short-one"}, login_apply.SHORT),
    ({"password": "x" * 257}, login_apply.LONG),
    ({"password": "パスワード" * 18}, login_apply.LONG),        # 90 characters, 270 bytes
    ({"password": "\U0001F511" * 65}, login_apply.LONG),       # 65 characters, 260 bytes
    ({"password": 123456789012}, login_apply.REFUSED),
    ({}, login_apply.REFUSED),
    ({"password": "line-one-password\nroot:x"}, login_apply.CHARACTER),
    ({"password": "nul-in-the\x00-middle"}, login_apply.CHARACTER),
    ({"password": "zero​width-password"}, login_apply.CHARACTER),
    ({"password": "direction‮mark-password"}, login_apply.CHARACTER),
    ({"password": "lone-surrogate-\ud800-pw"}, login_apply.CHARACTER),
])
def test_a_password_the_helper_does_not_set_is_refused_before_any_account_changes(tmp_path, req, why):
    tools, ops, marker = _box(tmp_path)
    with pytest.raises(login_apply.Refused) as e:
        login_apply.handle(ops, marker, "create", req)
    assert e.value.why == why
    assert not tools.changes() and marker.present()


def test_a_long_password_with_spaces_and_any_script_is_fine():
    assert login_apply.check_password("ein langes Passwort für mich") == "ein langes Passwort für mich"
    assert login_apply.check_password("パスワードはこれです、長い") == "パスワードはこれです、長い"
    # The maximum is in UTF-8 bytes, as PAM counts.
    assert login_apply.check_password("x" * 256)
    assert login_apply.check_password("パスワード" * 17)               # 255 bytes
    assert login_apply.check_password("\U0001F511" * 64)               # 256 bytes


def test_without_the_marker_nothing_is_offered_and_nothing_is_made(tmp_path):
    tools, ops, marker = _box(tmp_path, marker=False)
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.NOT_OFFERED}
    with pytest.raises(login_apply.Refused) as e:
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert e.value.why == login_apply.NOT_OFFERED and not tools.changes()


def _no_marker_with(tmp_path, change):
    tools, ops, marker = _box(tmp_path)
    change(pathlib.Path(marker.path))
    assert not marker.present()
    with pytest.raises(login_apply.Refused):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert not tools.changes()


@pytest.mark.parametrize("change", [
    lambda p: p.parent.chmod(0o770),            # a directory its group can write
    lambda p: p.parent.chmod(0o757),            # or anyone
    lambda p: p.chmod(0o620),                   # a marker its group could have written
    lambda p: (p.unlink(), p.symlink_to(p.parent / "elsewhere"), (p.parent / "elsewhere").write_text("")),
    lambda p: (p.unlink(), p.mkdir()),          # not a file
])
def test_a_marker_the_run_user_could_have_made_is_no_marker(tmp_path, change):
    _no_marker_with(tmp_path, change)


def test_a_marker_that_is_not_roots_is_no_marker(tmp_path):
    tools, ops, marker = _box(tmp_path)
    assert marker.present()
    assert not login_apply.Marker(marker.path, owner=os.getuid() + 1).present()
    assert login_apply.Marker().path == login_apply.PENDING_PATH and login_apply.Marker().owner == 0


def test_the_marker_lives_outside_everything_the_run_user_can_write():
    path = login_apply.PENDING_PATH
    assert not path.startswith(("/etc/teaport", "/var/lib/teaport/", "/run/", "/tmp/", "/home/"))
    assert path.startswith("/var/lib/teaport-owner/")


def test_a_create_makes_the_admin_account_once(tmp_path):
    tools, ops, marker = _box(tmp_path)
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.PENDING}
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": True, "username": "teaport"}
    useradd = next(c for c in tools.calls if c[0] == "useradd")
    assert useradd[-1] == "teaport"
    assert useradd[useradd.index("--home-dir") + 1] == tools.home and "--create-home" in useradd
    assert useradd[useradd.index("--shell") + 1] == "/bin/bash"
    assert useradd[useradd.index("--groups") + 1] == "sudo"
    assert tools.stdins == [f"teaport:{GOOD}\n".encode()]       # chpasswd's stdin, only
    assert tools.users["teaport"] == GOOD and not marker.present()
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.CREATED}
    with pytest.raises(login_apply.Refused) as e:                # once
        login_apply.handle(ops, marker, "create", {"password": "another-password-1"})
    assert e.value.why == login_apply.EXISTS and tools.users["teaport"] == GOOD
    assert not any(GOOD in " ".join(c) for c in tools.calls)    # never on a command line


def test_a_login_made_first_by_someone_else_is_reported_as_existing(tmp_path):
    # The known window: whoever creates first gets it (the marker goes with it); the
    # owner is told it exists.
    tools, ops, marker = _box(tmp_path, marker=False, users=["teaport"])
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.CREATED}
    with pytest.raises(login_apply.Refused) as e:
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert e.value.why == login_apply.EXISTS and not tools.changes()


def test_the_login_is_never_the_run_users_account(tmp_path):
    tools, ops, marker = _box(tmp_path, run_user="teaport")
    with pytest.raises(login_apply.Refused, match="the brain runs as teaport"):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert not tools.changes() and marker.present()


def test_a_home_directory_already_there_is_not_taken_over(tmp_path):
    # The undo removes the home directory: one that was there first is not the helper's.
    tools, ops, marker = _box(tmp_path)
    os.makedirs(tools.home)
    (pathlib.Path(tools.home) / "notes.txt").write_text("someone's file")
    with pytest.raises(login_apply.Refused, match="already there"):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert not tools.changes() and (pathlib.Path(tools.home) / "notes.txt").exists()


@pytest.mark.parametrize("fail", ["useradd", "useradd-half", "chpasswd"])
def test_a_step_that_fails_undoes_the_ones_before_it(tmp_path, fail):
    tools, ops, marker = _box(tmp_path, fail=[fail])
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": False, "why": login_apply.FAILED}
    assert "teaport" not in tools.users and not os.path.exists(tools.home)
    assert marker.present()                                     # still pending: try again
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.PENDING}
    if fail != "useradd":
        assert ["userdel", "--remove", "teaport"] in tools.calls


def test_an_account_a_killed_create_left_is_pending_and_the_next_create_finishes_it(tmp_path):
    # Killed between useradd and chpasswd (SIGKILL, a power cut): no undo ran, so the
    # account is there with no password, and the marker too.
    tools, ops, marker = _box(tmp_path, killed="chpasswd")
    with pytest.raises(Killed):
        login_apply.handle(login_apply.Ops(_no_undo(tools), home=tools.home), marker, "create", {"password": GOOD})
    assert tools.users["teaport"] == "" and marker.present()
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.PENDING}
    tools.killed = None
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": True, "username": "teaport"}
    assert tools.users["teaport"] == GOOD and "sudo" in tools.groups["teaport"] and not marker.present()
    usermod = next(c for c in tools.calls if c[0] == "usermod")
    assert usermod == ["usermod", "--append", "--groups", "sudo", "--shell", "/bin/bash", "teaport"]
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.CREATED}


def _no_undo(tools):
    """The tools as a killed process sees them: its finally, the undo, never runs."""
    def run(argv, stdin=None):
        if argv[0] == "userdel":   # the in-process finally's undo: on the box it never ran
            return 0, "", ""
        return tools(argv, stdin)
    return run


@pytest.mark.parametrize("defs, mode", [
    ("UMASK\t\t022\nHOME_MODE\t0750\n", 0o750),     # Ubuntu's own
    ("UMASK 027\n", 0o750),
    ("UMASK 077\n", 0o700),
    ("# nothing set\n", 0o750),
    (None, 0o750),                                     # no login.defs at all
])
def test_an_unfinished_account_without_its_home_gets_one_as_useradd_makes_it(tmp_path, defs, mode):
    tools, _, marker = _box(tmp_path, users=["teaport"])  # a create killed inside useradd
    login_defs = tmp_path / "login.defs"
    if defs is not None:
        login_defs.write_text(defs)
    ops = login_apply.Ops(tools, home=tools.home, login_defs=str(login_defs))
    assert login_apply.handle(ops, marker, "create", {"password": GOOD})["ok"] is True
    helper = next(c for c in tools.calls if c[0] == "mkhomedir_helper")
    assert helper == ["mkhomedir_helper", "teaport", f"{0o777 & ~mode:04o}"]
    st = os.stat(tools.home)
    assert oct(st.st_mode & 0o777) == oct(mode) and st.st_uid == os.getuid()
    assert (pathlib.Path(tools.home) / ".profile").exists() and not marker.present()


def test_an_unfinished_account_keeps_a_home_it_already_has(tmp_path):
    tools, ops, marker = _box(tmp_path, users=["teaport"])
    os.makedirs(tools.home)
    (pathlib.Path(tools.home) / "notes.txt").write_text("kept")
    assert login_apply.handle(ops, marker, "create", {"password": GOOD})["ok"] is True
    assert not any(c[0] == "mkhomedir_helper" for c in tools.calls)
    assert (pathlib.Path(tools.home) / "notes.txt").read_text() == "kept"


@pytest.mark.parametrize("make", [
    lambda home: pathlib.Path(home).write_text("a file"),
    lambda home: os.symlink("/etc", home),
])
def test_a_home_path_that_is_not_the_accounts_directory_is_refused(tmp_path, make):
    tools, ops, marker = _box(tmp_path, users=["teaport"])
    make(tools.home)
    with pytest.raises(login_apply.Refused, match="not the login's own directory"):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert not tools.changes() and marker.present()


def test_a_home_directory_owned_by_someone_else_is_refused(tmp_path):
    tools, _, marker = _box(tmp_path, users=["teaport"])
    os.makedirs(tools.home)
    real = tools.__call__

    def other_uid(argv, stdin=None):   # the account's uid is not the directory's owner
        rc, out, err = real(argv, stdin)
        return (rc, out.replace(f":{os.getuid()}:", f":{os.getuid() + 1}:", 1), err) if argv[0] == "getent" else (rc, out, err)
    ops = login_apply.Ops(other_uid, home=tools.home)
    with pytest.raises(login_apply.Refused, match="not the login's own directory"):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert not tools.changes() and marker.present()


def test_without_mkhomedir_helper_the_finish_fails_and_stays_pending(tmp_path):
    tools, ops, marker = _box(tmp_path, users=["teaport"], fail=["mkhomedir_helper"])
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": False, "why": login_apply.FAILED}
    assert tools.users["teaport"] == "" and marker.present()
    # And a tool that is not there at all is a failed step, not an error.
    assert login_apply.run_tool(["/nonexistent/mkhomedir_helper"])[0] == 127


def test_the_package_needs_mkhomedir_helper():
    assert "  - libpam-modules-bin\n" in (ROOT / "packaging/nfpm.yaml").read_text()


def test_an_unfinished_account_that_cannot_be_finished_stays_pending(tmp_path):
    tools, ops, marker = _box(tmp_path, users=["teaport"], fail=["usermod"])
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": False, "why": login_apply.FAILED}
    assert "teaport" in tools.users and marker.present()        # not undone: not this run's
    assert not any(c[0] == "userdel" for c in tools.calls)
    tools.fail.clear()
    assert login_apply.handle(ops, marker, "create", {"password": GOOD})["ok"] is True


def test_an_undo_that_fails_never_hides_the_result_and_a_retry_finishes(tmp_path):
    tools, ops, marker = _box(tmp_path, fail=["chpasswd", "userdel"])
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": False, "why": login_apply.FAILED}
    assert tools.users["teaport"] == "" and marker.present()    # left unfinished, pending
    assert login_apply.handle(ops, marker, "status", {}) == {"state": login_apply.PENDING}
    tools.fail.clear()
    assert login_apply.handle(ops, marker, "create", {"password": GOOD})["ok"] is True
    assert tools.users["teaport"] == GOOD


def test_the_undo_needs_no_lookup(tmp_path):
    # A getent that stops answering after useradd must not turn the result into an error.
    tools, ops, marker = _box(tmp_path, fail=["chpasswd"])
    real = tools.__call__

    def flaky(argv, stdin=None):
        if argv[0] == "getent" and "teaport" in tools.users:
            return 1, "", "getent: nss down"
        return real(argv, stdin)
    ops = login_apply.Ops(flaky, home=tools.home)
    assert login_apply.handle(ops, marker, "create", {"password": GOOD}) == {"ok": False, "why": login_apply.FAILED}
    assert "teaport" not in tools.users


def test_a_marker_that_cannot_be_removed_undoes_the_login(tmp_path, monkeypatch):
    # Left behind, it would open the gate again for whoever later removes the account.
    tools, ops, marker = _box(tmp_path)

    def stuck():
        raise PermissionError("read-only")
    monkeypatch.setattr(marker, "remove", stuck)
    with pytest.raises(PermissionError):
        login_apply.handle(ops, marker, "create", {"password": GOOD})
    assert "teaport" not in tools.users and ["userdel", "--remove", "teaport"] in tools.calls


@pytest.mark.parametrize("verb, req", [("bogus", {}), ("create", ["not", "an", "object"])])
def test_anything_else_is_refused(tmp_path, verb, req):
    tools, ops, marker = _box(tmp_path)
    with pytest.raises(login_apply.Refused):
        login_apply.handle(ops, marker, verb, req)
    assert not tools.changes()


# ------------------------------------------------------------------ main


def _main(monkeypatch, capsys, verb, stdin, ops=None, marker=None, euid=0):
    """main() as sudo runs it, on a pretend box; (exit, reply, stderr, the masks set)."""
    masks = []
    monkeypatch.setattr(login_apply.os, "geteuid", lambda: euid)
    monkeypatch.setattr(login_apply.sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(login_apply.signal, "pthread_sigmask", lambda how, sigs: masks.append((how, set(sigs))))
    monkeypatch.setattr(login_apply, "_harden", lambda: masks.append("hardened"))
    monkeypatch.setattr(login_apply, "_lock", lambda: 0)
    if ops is not None:
        monkeypatch.setattr(login_apply, "Ops", lambda: ops)
        monkeypatch.setattr(login_apply, "Marker", lambda: marker)
    rc = login_apply.main([verb] if isinstance(verb, str) else verb)
    out = capsys.readouterr()
    return rc, json.loads(out.out), out.err, masks


def test_main_refuses_without_root_and_reads_its_request_from_stdin(monkeypatch, capsys):
    rc, reply, _, _ = _main(monkeypatch, capsys, "status", "{}", euid=1000)
    assert rc == 2 and "root" in reply["refused"]
    for stdin, why in (("x" * (login_apply.MAX_REQUEST + 1), "too long"), ("{nope", "not JSON")):
        rc, reply, _, _ = _main(monkeypatch, capsys, "create", stdin)
        assert rc == 2 and why in reply["refused"]
    rc, reply, _, _ = _main(monkeypatch, capsys, ["create", GOOD], "{}")
    assert rc == 2 and "usage" in reply["refused"]


def test_main_creates_with_every_signal_held_off_and_no_core(monkeypatch, capsys, tmp_path):
    tools, ops, marker = _box(tmp_path)
    rc, reply, err, masks = _main(monkeypatch, capsys, "create", json.dumps({"password": GOOD}), ops, marker)
    assert rc == 0 and reply == {"ok": True, "username": "teaport"} and err == ""
    sig = login_apply.signal
    assert masks == [(sig.SIG_BLOCK, set(sig.valid_signals())), "hardened"]
    # What sudo passes on, which the run user can send from another process group.
    assert {sig.SIGTERM, sig.SIGINT, sig.SIGHUP, sig.SIGUSR1, sig.SIGUSR2, sig.SIGALRM,
            sig.SIGQUIT} <= masks[0][1]
    rc, reply, _, _ = _main(monkeypatch, capsys, "create", json.dumps({"password": GOOD}), ops, marker)
    assert rc == 2 and reply["why"] == login_apply.EXISTS


def test_the_password_reaches_no_reply_and_no_output_however_it_fails(monkeypatch, capsys, tmp_path):
    # A chpasswd that echoes it, then an error nobody expected whose text quotes it.
    tools, ops, marker = _box(tmp_path, fail=["chpasswd"], noisy=True)
    rc, reply, err, _ = _main(monkeypatch, capsys, "create", json.dumps({"password": GOOD}), ops, marker)
    assert rc == 0 and reply == {"ok": False, "why": login_apply.FAILED}
    assert GOOD not in json.dumps(reply) + err

    def blows_up(ops_, marker_, verb, req):
        raise ValueError(f"cannot handle {req['password']}")
    monkeypatch.setattr(login_apply, "handle", blows_up)
    rc, reply, err, _ = _main(monkeypatch, capsys, "create", json.dumps({"password": GOOD}), ops, marker)
    assert rc == 1 and reply == {"ok": False, "why": login_apply.FAILED}
    assert err == "login_apply: ValueError\n" and GOOD not in json.dumps(reply)
    # Not UTF-8: the decoder's error would quote a byte of it.
    monkeypatch.setattr(login_apply.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"password": "\xff\xfe"}'), encoding="utf-8"))
    monkeypatch.setattr(login_apply.os, "geteuid", lambda: 0)
    assert login_apply.main(["create"]) == 1
    assert capsys.readouterr().err == "login_apply: UnicodeDecodeError\n"


def test_one_create_at_a_time(tmp_path):
    # Two at once would each find no user, and the second's undo would remove the first's.
    path = str(tmp_path / "run/lock")
    held = login_apply._lock(path)
    try:
        with pytest.raises(login_apply.Refused, match="another login") as e:
            login_apply._lock(path)
        assert e.value.why == login_apply.BUSY     # under way, not failed
    finally:
        os.close(held)
    os.close(login_apply._lock(path))
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == oct(0o700)


def test_the_real_tools_get_a_path_of_their_own_and_the_password_as_bytes(monkeypatch):
    seen = {}

    def run(argv, input, capture_output, timeout, env):
        seen.update(argv=argv, input=input, env=env)

        class P:
            returncode, stdout, stderr = 0, b"", b""
        return P()
    monkeypatch.setattr(login_apply.subprocess, "run", run)
    assert login_apply.Ops().chpasswd(GOOD) is True
    assert seen["argv"] == ["chpasswd"] and seen["input"] == f"teaport:{GOOD}\n".encode()
    assert seen["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


# A real helper process, sent what the run user can send through sudo while it is between
# useradd and chpasswd, and checked for a core limit and dumpability.
_SIGNALLED = """
import json, os, resource, signal, sys
from teaport_brain import login_apply
import test_login_apply as t
Ops, Marker = login_apply.Ops, login_apply.Marker
tools = t.FakeTools(sys.argv[1])
real = tools.__call__
def run(argv, stdin=None):
    if argv[0] == "chpasswd":   # between the steps: everything sudo passes on
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1,
                  signal.SIGUSR2, signal.SIGALRM, signal.SIGQUIT):
            os.kill(os.getpid(), s)
    return real(argv, stdin)
os.makedirs(os.path.dirname(sys.argv[2]), mode=0o700)
open(sys.argv[2], "w").close()
os.chmod(os.path.dirname(sys.argv[2]), 0o700)   # root's alone (the umask may say otherwise)
os.chmod(sys.argv[2], 0o600)
os.geteuid = lambda: 0
login_apply.Ops = lambda: Ops(run, home=sys.argv[1])
login_apply.Marker = lambda: Marker(sys.argv[2], owner=os.getuid())
login_apply._lock = lambda: 0
rc = login_apply.main(["create"])
dumpable = __import__("ctypes").CDLL(None).prctl(3, 0, 0, 0, 0)   # PR_GET_DUMPABLE
print(json.dumps({"rc": rc, "users": tools.users, "core": resource.getrlimit(resource.RLIMIT_CORE),
                  "dumpable": dumpable}))
"""


def test_a_real_helper_signalled_between_the_steps_finishes_and_dumps_no_core(tmp_path):
    import subprocess
    import sys
    script = tmp_path / "signalled.py"
    script.write_text(_SIGNALLED)
    (tmp_path / "home").mkdir()
    pending = tmp_path / "owner" / "pending"
    p = subprocess.run([sys.executable, str(script), str(tmp_path / "home/teaport"), str(pending)],
                       input=json.dumps({"password": GOOD}), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": f"{ROOT / 'brain'}:{ROOT / 'brain/tests'}"})
    assert p.returncode == 0, p.stderr
    lines = p.stdout.strip().splitlines()
    assert json.loads(lines[0]) == {"ok": True, "username": "teaport"}      # the reply
    state = json.loads(lines[-1])
    assert state["rc"] == 0 and state["users"] == {"teaport": GOOD} and not pending.exists()
    assert state["core"] == [0, 0] and state["dumpable"] == 0


# ------------------------------------------------------------------ the run user's side


def _fake_sudo(tmp_path, reply, rc=0):
    tool = tmp_path / "sudo"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > {tmp_path}/argv\ncat > {tmp_path}/stdin\n'
                    f"printf '%s\\n' '{reply}'\nexit {rc}\n")
    tool.chmod(0o755)
    return f"{tmp_path}:{os.environ['PATH']}"


def test_through_sudo_the_password_goes_on_stdin_never_on_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"ok": true, "username": "teaport"}'))
    assert owner_login.Login().create(GOOD, GOOD) == (True, "")
    argv = (tmp_path / "argv").read_text().split()
    assert argv == ["-n", "/usr/bin/python3", "/usr/lib/teaport/login_apply.py", "create"], argv
    assert json.loads((tmp_path / "stdin").read_text()) == {"password": GOOD}
    # A refusal keeps its code; a helper that could not run, or said nothing, is a failure.
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"refused": "this box already has a login", "why": "exists"}', rc=2))
    assert owner_login.Login().create(GOOD, GOOD) == (False, owner_login.EXISTS)
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"refused": "another login is being created", "why": "busy"}', rc=2))
    assert owner_login.Login().create(GOOD, GOOD) == (False, owner_login.BUSY)
    assert owner_login.say(i18n.get("en"), owner_login.WHY[owner_login.BUSY]) == "Creating your login…"
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, "sudo: a password is required", rc=1))
    assert owner_login.Login().create(GOOD, GOOD) == (False, login_apply.FAILED)
    assert owner_login.Login().state() == owner_login.NOT_OFFERED     # no line: nothing offered
    monkeypatch.setenv("PATH", _fake_sudo(tmp_path, '{"state": "pending"}'))
    assert owner_login.Login().pending() is True
    assert json.loads((tmp_path / "stdin").read_text()) == {}


def test_a_typo_costs_no_sudo(tmp_path, monkeypatch):
    asked = []
    login = owner_login.Login(helper=lambda verb, req: asked.append(verb) or {"ok": True})
    assert login.create(GOOD, GOOD + "x") == (False, owner_login.MISMATCH)
    assert login.create("short-one", "short-one") == (False, login_apply.SHORT)
    assert login.create("tab\tin-the-password", "tab\tin-the-password") == (False, login_apply.CHARACTER)
    assert asked == []


def test_every_reason_has_words():
    codes = {login_apply.SHORT, login_apply.LONG, login_apply.CHARACTER, login_apply.EXISTS,
             login_apply.FAILED, login_apply.REFUSED, login_apply.NOT_OFFERED, login_apply.BUSY,
             owner_login.MISMATCH}
    assert set(owner_login.WHY) == codes


# ------------------------------------------------------------------ packaging


def test_the_package_ships_the_helper_on_the_system_python():
    nfpm = (ROOT / "packaging/nfpm.yaml").read_text()
    assert ("  - src: brain/teaport_brain/login_apply.py\n    dst: /usr/lib/teaport/login_apply.py\n") in nfpm
    assert owner_login.HELPER == "/usr/lib/teaport/login_apply.py"
    # Standard library only: it runs on the system python, outside the venv.
    imports = set(re.findall(r"^(?:import|from) (\w+)", (ROOT / "brain/teaport_brain/login_apply.py").read_text(), re.M))
    assert imports <= {"ctypes", "fcntl", "json", "os", "resource", "signal", "stat", "subprocess",
                       "sys", "unicodedata"}, imports
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "/usr/lib/teaport/login_apply.py" in ci   # compiled on the installed package


def test_the_caller_waits_for_a_whole_create():
    # systemctl, getent, useradd, chpasswd, and the undo's userdel, each to the helper's
    # limit: a caller that gave up first would not hear how it ended.
    assert owner_login.HELPER_SECS > 5 * login_apply.TOOL_SECS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
