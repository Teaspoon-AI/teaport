#
# provision.py — the box's AI key and its setup card from a terminal (#283):
# `teaport key set` and `teaport setup-card`.
#
# Whoever readies a box -- an appliance's provisioning over SSH, or an owner at a
# terminal -- does two things that otherwise only the owner's pages do: give the box a
# key, and fix the setup network's password so it can be printed. Both go through the code
# the pages use, so a provisioned box is one the pages would have made, and nothing is
# written a second way:
#
#   key    the key on stdin (never an argument: a process listing shows those), checked
#          and saved as the setup-mode page saves one (ai_account.adopt: check_key, then
#          save_key -- the brain's key file and OpenClaw's, both or neither, #281). Only a
#          key that works is saved. --starter also marks it as the trial key
#          (credit.MARKER, the key's SHA-256: #280), so the box watches the key's credit
#          and says when it runs low: written only once the save went through, so a save
#          that fails, is refused or is stopped midway leaves the marker as it was. A run
#          stopped between the save and the marker leaves the key unmarked (no warnings),
#          and says nothing either way: a provisioning step that did not exit 0 is run
#          again. One run at a time (LOCK), so two --starter runs never mark each other's
#          key. --new-key: the provider made the key moments ago, so a refusal is asked
#          again (check_key's `issued`; a fresh OpenRouter key can be refused for a
#          moment, seen in #282).
#   card   the setup network's name, its password and the QR code's text, as the box
#          shows them in setup mode (wifi_setup: setup_ssid, wifi_qr) -- for a printed
#          insert -- from the wifi-setup.env text on stdin. The password is the one that
#          text fixes; with none, each setup makes a fresh one, so there is nothing to
#          print and the card says so. --new only marks the card as one just made.
#   setup-env   wifi-setup.env's text (stdin) with a fresh password
#          (wifi_setup.new_password) in place of its old one, on stdout: what `teaport
#          setup-card --new-password` installs. Never a file: the CLI keeps the text in
#          memory, checks it, hands it to root on stdin, and then shows the card made from
#          that same text. A file of this process's -- the run user's, the agent's -- that
#          root then reads is one the agent could swap for a link to any file root can
#          read (review of #419).
#
# The environment is the units': the CLI's own is dropped, the run user's HOME, USER and
# LOGNAME come from the password database as User= gives them, PATH is systemd's default
# (a CLI on the operator's own PATH, an npm-global openclaw say, is not one the brain
# would find), and brain.env is read in (settings.parse_env, systemd's rules), so the
# endpoint, OpenClaw's settings and an LLM_API_KEY copy that would shadow the key file are
# what the brain sees -- not whatever the operator's shell carries. The card also reads
# wifi-setup.env, after brain.env, as teaport-wifi-setup.service does; the CLI passes its
# text on stdin, as it may be root's to read. Everything reading settings is imported only
# after that, as modules read some settings at import. Logging is the units' too
# (conversation_log.install: keys redacted, no variables in a traceback), at WARNING.
#
# SIGTERM and SIGHUP (an SSH session dropping) raise, as in the setup unit, so a save they
# stop is put back by save_key's own clean-up instead of being left torn. As a
# KeyboardInterrupt (Stopped): asyncio re-raises that from whatever callback or task it
# lands in, where a plain exception would be logged and lost. A stop during the key check
# ends the run once the check's own deadline has passed (check_key's thread is not
# interrupted: about 10 s, 16 s with --new-key).
#
# A running brain needs nothing told: it reads the key file per session, the credit
# monitor reads again within its poll or after the next conversation, and an OpenClaw
# restart owed by the save is recorded for the brain's Restarter, as for the setup unit.
#
# Run as the box's run user (the CLI checks): the key file, the marker and OpenClaw's
# store are that user's.
#
import argparse
import fcntl
import getpass
import json
import os
import pwd
import signal
import sys

from teaport_brain.settings import ENV_LINE, parse_env

# systemd's default PATH for a system unit (the brain's sets none of its own).
UNIT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# A unit's locale is the system's (systemd reads this file), never the SSH client's: the
# openclaw CLI's words are matched in English (agent_key). No XDG_RUNTIME_DIR either: the
# brain has none, and openclaw_client gives every CLI run the user's own.
LOCALE_FILE = "/etc/default/locale"
PASSWORD_NAME = "WIFI_SETUP_PASSWORD"
LOCK = "provision.lock"

# Exit codes. key: 0 saved; 3 the provider refused the key; 2 not saved for another
# reason (no answer, OpenClaw did not take it, not a key at all, a setting in the way, a
# usage error); 1 broken (an error this module did not expect: Python's own exit). card: 0
# shown; 1 nothing to show (no password fixed, one setup would not use, no Wi-Fi device).
OK, NO_CARD, NOT_DONE, REFUSED = 0, 1, 2, 3


class Stopped(KeyboardInterrupt):
    """SIGTERM or SIGHUP (a KeyboardInterrupt, so asyncio passes it on: the module note)."""


def _on_signal(signum, frame):
    # Python's default is to die on the spot, past every finally. Raise instead, once.
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)
    raise Stopped()


def unit_environment(brain_env: str, extra: str = "") -> dict[str, str]:
    """The environment a unit with User=<this user> and EnvironmentFile=brain.env (then
    `extra`'s text) has."""
    me = pwd.getpwuid(os.getuid())
    try:
        locale = _read(LOCALE_FILE)
    except OSError:
        locale = ""  # unreadable: no locale, as a unit would have none from it
    env = {k: v for k, v in parse_env(locale).items() if k == "LANG" or k.startswith("LC_")}
    env.update(HOME=me.pw_dir, USER=me.pw_name, LOGNAME=me.pw_name, SHELL=me.pw_shell,
               PATH=UNIT_PATH)
    env.update(parse_env(brain_env))
    env.update(parse_env(extra))
    return env


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _say(args, data: dict, text: str) -> None:
    print(json.dumps(data) if args.json else text)


def _fail(args, code: int, reason: str, **data) -> int:
    if args.json:
        print(json.dumps({"ok": False, "reason": reason, **data}))
    else:
        print(f"error: {reason}", file=sys.stderr)
    return code


# ------------------------------------------------------------------ key

def _read_key(stream=None) -> str:
    stream = stream or sys.stdin
    if stream.isatty():
        return getpass.getpass("AI key (not shown): ")
    return stream.read(4096)


def set_key(args, key: str) -> int:
    from teaport_brain import ai_account, credit
    from teaport_brain.settings import setting, state_dir, write_secret

    key = key.strip()
    base_url = setting("LLM_BASE_URL")
    if os.environ.get("LLM_API_KEY", "").strip():
        return _fail(args, NOT_DONE, f"{args.etc}/brain.env sets LLM_API_KEY, which the brain "
                     "reads before its key file: remove that line (the key belongs in "
                     f"{ai_account.key_path()}), then run this again")
    if not ai_account.is_openrouter(base_url) and args.starter:
        return _fail(args, NOT_DONE, "--starter is for an OpenRouter key (the trial credit is "
                     "read from OpenRouter), and LLM_BASE_URL in brain.env is "
                     f"{base_url or 'not set'}")
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    with open(os.path.join(state_dir(), LOCK), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = ai_account.adopt_blocking(key, base_url, issued=args.new_key)
        saved = result.state == ai_account.WORKS
        left = ai_account.credit_left(result.data)
        data = {"ok": saved, "state": result.state, "reason": result.reason,
                "note": result.note, "starter": False,
                "limit": left.limit, "remaining": left.remaining, "usage": left.usage}
        if not saved:
            # The provider's refusal of the key; our own (an empty line, not a key, a copy
            # in the environment) is no verdict on a key.
            ours = (ai_account.EMPTY, ai_account.NOT_A_KEY, ai_account.SET_ELSEWHERE)
            refused = result.state == ai_account.REFUSED and not (result.ours and result.reason in ours)
            code = REFUSED if refused else NOT_DONE
            return _fail(args, code, f"not saved: {result.reason or result.state}",
                         **{k: v for k, v in data.items() if k not in ("ok", "reason")})
        if args.starter:
            try:
                write_secret(os.path.join(state_dir(), credit.MARKER), credit.key_hash(key))
            except OSError as e:
                return _fail(args, NOT_DONE, "the key was saved, but it could not be marked "
                             f"as the trial key ({e.strerror or e}): run this again",
                             **{k: v for k, v in data.items() if k not in ("ok", "reason")})
            data["starter"] = True
    credit_text = ""
    if left.remaining is not None:
        credit_text = f" ({left.remaining:.2f} USD left"
        credit_text += f" of {left.limit:.2f})" if left.limit is not None else ")"
    lines = [f"saved: the key works{credit_text}"]
    if args.starter:
        lines.append("marked as the trial key: the box will say when its credit runs low")
    if result.note:
        lines.append(f"note: {result.note}")
    _say(args, data, "\n".join(lines))
    return OK


# ------------------------------------------------------------------ card

def with_password(env_text: str, password: str) -> str:
    """wifi-setup.env's text with its password replaced (every line naming it dropped, one
    appended), every other line as it was."""
    kept = []
    for line in env_text.splitlines():
        m = ENV_LINE.match(line)
        if m and m.group(1) == PASSWORD_NAME and not line.lstrip().startswith("#"):
            continue
        kept.append(line)
    kept.append(f"{PASSWORD_NAME}={password}")
    return "\n".join(kept) + "\n"


def card(args, setup_env: str, nm=None) -> int:
    from teaport_brain import wifi_setup
    from teaport_brain.settings import setting

    nm = nm or wifi_setup.NM()
    dev = nm.wifi_device()
    if not dev:
        return _fail(args, NO_CARD, "this box has no Wi-Fi device, so it has no setup network")
    # quiet: the unit logs a refused name (to stdout, which here is the card's)
    ssid = wifi_setup.setup_ssid(nm.mac(dev), setting("WIFI_SETUP_SSID", default=""), quiet=True)
    password = (setting(PASSWORD_NAME, default="") or "").strip()
    if not password:
        return _fail(args, NO_CARD, "no fixed setup password: each setup makes a fresh "
                     "one, so there is nothing to print (run with --new-password)")
    if not wifi_setup.valid_password(password):
        return _fail(args, NO_CARD, f"{PASSWORD_NAME} is not 8-63 letters, digits and "
                     "- _ . @ ! # & * or space, so setup would use a random one "
                     "(run with --new-password)")
    qr = wifi_setup.wifi_qr(ssid, password)
    _say(args, {"ok": True, "ssid": ssid, "password": password, "qr": qr,
                "new": bool(args.new)},
         f"Network   {ssid}\nPassword  {password}\nQR code   {qr}")
    return OK


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--etc", default="/etc/teaport")
    common.add_argument("--json", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")
    ap = argparse.ArgumentParser(prog="python -m teaport_brain.provision",
                                 description="teaport key set / teaport setup-card")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("key", parents=[common],
                       help="check and save the box's AI key, read from stdin")
    k.add_argument("--starter", action="store_true",
                   help="the box's trial key: watch its credit (OpenRouter only)")
    k.add_argument("--new-key", action="store_true",
                   help="the provider made the key moments ago: ask again on a refusal")
    c = sub.add_parser("card", parents=[common],
                       help="the setup network's name, password and QR text")
    c.add_argument("--new", action="store_true", help="mark the card as one just made")
    sub.add_parser("setup-env", parents=[common],
                   help="wifi-setup.env's text (stdin) with a fresh password, on stdout")
    args = ap.parse_args(argv)
    if args.cmd == "setup-env":
        # The text and a fresh password, nothing else on stdout (the CLI installs it as
        # it is). A byte-order mark a Windows editor left is dropped, or its first line
        # would be neither read nor replaced.
        from teaport_brain import wifi_setup
        text = sys.stdin.read().removeprefix("\ufeff")
        sys.stdout.write(with_password(text, wifi_setup.new_password()))
        return OK

    # Read the key, and wifi-setup.env's text, before the environment is replaced.
    stdin_text = _read_key() if args.cmd == "key" else sys.stdin.read()
    brain_env = _read(os.path.join(args.etc, "brain.env"))
    env = unit_environment(brain_env, stdin_text if args.cmd == "card" else "")
    os.environ.clear()
    os.environ.update(env)
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, _on_signal)

    from teaport_brain import conversation_log
    conversation_log.install("INFO" if args.verbose else "WARNING")
    try:
        if args.cmd == "key":
            return set_key(args, stdin_text)
        return card(args, stdin_text)
    except KeyboardInterrupt:  # Stopped too
        if args.cmd == "key":
            from teaport_brain import ai_account, credit
            key = stdin_text.strip()
            if not (key and ai_account.current_key() == key):
                return _fail(args, NOT_DONE, "stopped: the key was not saved")
            if args.starter and not credit.on_starter(key):
                return _fail(args, NOT_DONE, "stopped: the key was saved, but not marked as "
                             "the trial key: run this again")
            return _fail(args, NOT_DONE, "stopped: the key is saved, but this run did not "
                         "finish: run this again to be sure")
        return _fail(args, NOT_DONE, "stopped: setup-card did not finish")


if __name__ == "__main__":
    sys.exit(main())
