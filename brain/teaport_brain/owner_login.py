#
# teaport — the owner's login (#421), from the run user's side: what the setup page
# (wifi_setup.py, offline) and Settings (config_ui.py, online) share -- whether a login is
# pending, creating it, and the words the owner reads about it.
#
# The creating is login_apply.py's, as root, behind its sudoers line, and only once, on a
# box that provisioning marked (see there, and its known window). The run user is the
# agent's: it may ask, never decide. The password goes to the helper on stdin and nowhere
# else -- not into a log line, the status file, an error or an answer -- so this module
# logs nothing and its results carry codes, never the request. No Pipecat import: the
# setup unit runs this alone on a box short of memory.
#
from teaport_brain import login_apply, root_helper
from teaport_brain.i18n import N_

# The root-owned copy the sudoers line names (packaging/deb/sudoers-sync).
HELPER = "/usr/lib/teaport/login_apply.py"
# A create's longest path is five account-tool calls (systemctl, getent, useradd,
# chpasswd, and an undo's userdel), each to the helper's own limit.
HELPER_SECS = 5 * login_apply.TOOL_SECS + 30
# The helper never stops part-way (it blocks the stop signals): how long a stopped caller
# waits for it to finish before it gives up on it (the create goes on regardless).
HELPER_STOP_SECS = 30
USERNAME = login_apply.USERNAME
MIN_PASSWORD = login_apply.MIN_PASSWORD
PENDING, CREATED, NOT_OFFERED = login_apply.PENDING, login_apply.CREATED, login_apply.NOT_OFFERED
EXISTS, BUSY = login_apply.EXISTS, login_apply.BUSY
# Why a create did not happen, beyond the helper's codes: the two passwords differ.
MISMATCH = "mismatch"

# What the owner reads, in their language (say()). {username} and {count} are filled in.
TEXT = {
    "title": N_("Create your login"),
    "intro": N_("You'll use this to sign in to Teaport from a computer. Your username is "
                "{username}."),
    "password": N_("Choose a password"),
    "again": N_("Type it again"),
    "rule": N_("Use at least {count} characters."),
    "create": N_("Create login"),
    "creating": N_("Creating your login…"),
    "created": N_("Your login is ready. Sign in as {username} with the password you chose."),
}
WHY = {
    MISMATCH: N_("The two passwords don't match."),
    login_apply.SHORT: N_("Your password needs at least {count} characters."),
    login_apply.LONG: N_("That password is too long."),
    login_apply.CHARACTER: N_("Your password can't contain invisible or control characters."),
    login_apply.EXISTS: N_("This Teaport already has a login."),
    login_apply.NOT_OFFERED: N_("Creating a login isn't available on this Teaport."),
    login_apply.FAILED: N_("Teaport couldn't create your login. Try again."),
    # Another create is under way (a second tap, Settings and the setup page at once): not
    # a failure, so said as the progress it is.
    BUSY: TEXT["creating"],
    login_apply.REFUSED: N_("Teaport couldn't create your login. Try again."),
}


def say(t, msgid: str) -> str:
    """`msgid` (one of TEXT's or WHY's) in t's language, its placeholders filled in."""
    return t._(msgid).format(username=USERNAME, count=login_apply.MIN_PASSWORD)


def sudo_helper(verb: str, req: dict) -> dict:
    return root_helper.call(HELPER, verb, req, timeout=HELPER_SECS,
                            stop_secs=HELPER_STOP_SECS, name="the login helper")


class Login:
    """The owner's login. `helper(verb, req) -> reply` is injectable; given `ops` and
    `marker` (tests), the requests run in-process through login_apply.handle()."""

    def __init__(self, helper=None, ops: login_apply.Ops | None = None,
                 marker: login_apply.Marker | None = None):
        if helper is None:
            helper = self._in_process if ops is not None else sudo_helper
        self._helper, self._ops, self._marker = helper, ops, marker

    def _in_process(self, verb: str, req: dict) -> dict:
        try:
            return login_apply.handle(self._ops, self._marker, verb, req)
        except login_apply.Refused as e:
            return {"ok": False, "err": str(e), "why": e.why}

    def state(self) -> str:
        """PENDING, CREATED or NOT_OFFERED. A helper that cannot answer (no sudoers line,
        an install without it) offers nothing: the step is only shown when it can work."""
        state = self._helper("status", {}).get("state")
        return state if state in (PENDING, CREATED) else NOT_OFFERED

    def pending(self) -> bool:
        return self.state() == PENDING

    def create(self, password: str, again: str) -> tuple[bool, str]:
        """(True, "") once the login exists, else (False, a WHY code). The password rule
        is checked here first (the helper's own, so the two never disagree), so a typo
        costs no sudo; the helper checks it again."""
        if password != again:
            return False, MISMATCH
        try:
            login_apply.check_password(password)
        except login_apply.Refused as e:
            return False, e.why
        r = self._helper("create", {"password": password})
        if r.get("ok") is True:
            return True, ""
        why = r.get("why")
        return False, why if why in WHY else login_apply.FAILED
