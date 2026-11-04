#
# teaport — OpenClaw uses the brain's LLM key (#281): one key, one balance.
#
# On a box with OpenClaw beside the brain (TEAPORT_AGENT=openclaw), both pay for their
# answers at OpenRouter. When the brain's key is saved (ai_account.save_key: the Settings
# page, the setup-mode page, and #282's OAuth through adopt), the same key is handed to
# OpenClaw as its OpenRouter key, so the box has one key and one balance and the owner
# swaps it once. Nothing else here starts a change: a key written by hand or by the
# installer is not copied (status says "separate").
#
# How OpenClaw is told, its own supported way (OpenClaw 2026.9.7):
#
#     openclaw models auth paste-api-key --provider openrouter --agent <id>   (key on stdin)
#
# docs/cli/models.md ("paste-api-key ... In automation, pipe the key on stdin"), and the
# code behind it (src/commands/models/auth.ts modelsAuthPasteApiKeyCommand ->
# auth-api-key.ts saveModelProviderApiKey): it updates the provider's configured profile
# or its active saved key (else creates openrouter:manual), records the profile in
# openclaw.json, and leaves model selection alone; then it asks the running gateway to
# reload its auth (models.authRefresh, auth-refresh.ts). The key is read from stdin when
# stdin is no terminal (readPastedSecret), so it never appears in a process listing.
# Exit 0 is saved. When the gateway could not confirm applying it, the CLI still exits 0
# and says so on stderr with "Run `openclaw gateway restart`" (both warnings carry it):
# that is the one case a restart is needed -- auth.profiles and models hot-apply
# otherwise (docs/gateway/configuration/hot-reload.md). A gateway that is not running
# (connection refused) is no warning: it reads the store when it starts.
#
# The restart (Restarter, in the brain): `openclaw gateway restart --safe`, which lets the
# gateway's own running work drain first, and only once no conversation is live on any
# front-end (the session arbiter: room awake, Talk, a call, one on hold or one ringing) and
# none has been for IDLE_SECS; checked again right before. Until then status says
# "restart". The setup-mode unit, a process of its own, records the need in the state
# file and the brain's Restarter acts on it.
#
# Both places or neither (ai_account.save_key): the brain's key file is written first,
# then OpenClaw is told; if OpenClaw fails, the brain's file is put back. If OpenClaw may
# have taken the new key before failing (it said "API key saved" first, or it ran out of
# time), it is given the old one back -- possible only when the old one is a key this
# module gave it (the state file's fingerprint). When it is not, or that fails too, the
# state says "mismatch" until a save goes through, and /health and `teaport status` say
# that the two may be on different keys.
#
# The state file (~/.local/state/teaport/agent_key.json, mode 600): the SHA-256 of the
# key OpenClaw was last given from here (credit.py's marker recipe: the key alone, no
# newline), whether a restart is owed, and a mismatch's reason. A fingerprint, never the
# key. Nothing here logs a key: the CLI's stderr is logged redacted (ai_account.redact),
# and save_key marks both keys never_log for the journal.
#
# No Pipecat import: the setup-mode unit runs this too.
#
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass

from loguru import logger

from teaport_brain import agent_backend, openclaw_client
from teaport_brain.settings import setting, state_dir

SHARES = setting("TEAPORT_AGENT_SHARES_KEY")
PROVIDER = "openrouter"
STATE_FILE = "agent_key.json"
PASTE_TIMEOUT_SECS = 60.0     # a cold CLI on the box takes several seconds; this is past it
RESTART_TIMEOUT_SECS = 360.0  # --safe drains for at most 5 minutes, then restarts
IDLE_SECS = 30.0              # nobody talking for this long before OpenClaw restarts
POLL_SECS = 30.0              # how often the Restarter reads the state file
RETRY_SECS = 600.0            # after a failed restart

# What status() says (/health "agent_key"; `teaport status` words them).
OFF, ABSENT, OTHER, NONE, SEPARATE, SHARED, RESTART, MISMATCH = (
    "off", "absent", "other", "none", "separate", "shared", "restart", "mismatch")
# What the CLI says when it has written the key, whatever went wrong after.
_WROTE = "API key saved"
_RESTART_HINT = "openclaw gateway restart"
_REMOTE_HINT = "does not share this auth state"


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def cli_installed() -> bool:
    return os.access(openclaw_client.OPENCLAW_BIN, os.X_OK)


def skipped(openrouter: bool) -> tuple[str, str] | None:
    """Why OpenClaw is not given the brain's key on this box, as (state, words); None
    when it is."""
    if not agent_backend.HAS_AGENT:
        return OFF, "no OpenClaw on this box (TEAPORT_AGENT=none)"
    if not SHARES:
        return OFF, "turned off (TEAPORT_AGENT_SHARES_KEY)"
    if not openrouter:
        return OTHER, "the brain's AI service is not OpenRouter"
    if not cli_installed():
        return ABSENT, f"the openclaw command is not installed ({openclaw_client.OPENCLAW_BIN})"
    return None


# ------------------------------------------------------------------ the state file

def _path() -> str:
    return os.path.join(state_dir(), STATE_FILE)


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _locked():
    """The state file's read-modify-write, one process at a time (the brain and the
    setup-mode unit both write it)."""
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    fd = os.open(_path() + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(data: dict) -> None:
    tmp = f"{_path()}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _path())


def _update(**changes) -> dict:
    with _locked():
        data = {**load(), **changes, "at": time.time()}
        _write(data)
        return data


# ------------------------------------------------------------------ telling OpenClaw

@dataclass(frozen=True)
class Told:
    ok: bool
    restart: bool = False     # saved, but the gateway has not applied it yet
    wrote: bool = False       # (a failure) OpenClaw may hold the new key all the same
    detail: str = ""          # for the journal: redacted, one line


def _last_line(err: bytes) -> str:
    """The CLI's last stderr line, for the journal: redacted (ai_account.redact)."""
    from teaport_brain.ai_account import redact  # ai_account imports this module
    lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    return redact(lines[-1])[:200] if lines else ""


async def _paste(key: str) -> Told:
    """Give OpenClaw `key` as its OpenRouter key (the module note). Replaced in tests: no
    test runs a real OpenClaw."""
    try:
        rc, _, err = await openclaw_client._run_openclaw(
            ["models", "auth", "paste-api-key", "--provider", PROVIDER,
             *openclaw_client.agent_args()],
            timeout=PASTE_TIMEOUT_SECS, capture=True, stdin=(key + "\n").encode("utf-8"))
    except asyncio.TimeoutError:
        return Told(False, wrote=True, detail="the openclaw command took too long")
    except OSError as e:
        return Told(False, detail=f"the openclaw command did not start ({type(e).__name__})")
    text = err.decode("utf-8", "replace")
    if rc != 0:
        return Told(False, wrote=_WROTE in text, detail=f"rc {rc}: {_last_line(err)}")
    if _REMOTE_HINT in text:  # saved here, but the gateway that answers is another host's
        return Told(False, wrote=True, detail=_last_line(err))
    return Told(True, restart=_RESTART_HINT in text)


async def give(key: str, prev: str | None, openrouter: bool) -> str | None:
    """Hand `key` to OpenClaw, as part of ai_account.save_key. None: done, or nothing to do
    on this box (skipped). Otherwise a reason for the journal: OpenClaw did not take it,
    and holds what it held before (given back when it may have taken the new one and the
    old one is known), or the state records the mismatch. `prev`: the key the brain used
    until now, which OpenClaw holds when it was given it from here."""
    why_not = skipped(openrouter)
    if why_not is not None:
        logger.info(f"agent key: OpenClaw not given the new key: {why_not[1]}")
        return None
    before = load()
    told = await _paste(key)
    if told.ok:
        _update(sha256=fingerprint(key), mismatch=None,
                restart=bool(told.restart or (before.get("restart") and before.get("sha256"))))
        logger.info("agent key: OpenClaw has the new key" +
                    (" (it restarts to use it once nobody is talking)" if told.restart else ""))
        if told.restart:
            RESTARTER.poke()
        return None
    logger.warning(f"agent key: OpenClaw did not take the new key: {told.detail}")
    if not told.wrote:
        return told.detail
    if prev and before.get("sha256") == fingerprint(prev):
        # A CLI that ran out of time is still being stopped (_end_cli): it holds the auth
        # store until it is gone, and a second one would find it busy.
        await openclaw_client.settle()
        back = await _paste(prev)
        if back.ok:
            logger.info("agent key: OpenClaw has its previous key back")
            if back.restart:
                _update(restart=True)
                RESTARTER.poke()
            return told.detail
        logger.warning(f"agent key: giving OpenClaw its previous key back failed: {back.detail}")
    mismatch("a key change failed halfway: OpenClaw may hold the new key while the brain "
             "kept the previous one")
    return told.detail


def mismatch(reason: str) -> None:
    """Record that OpenClaw and the brain may be on different keys, until a save goes
    through: status() says so (MISMATCH)."""
    _update(sha256=None, mismatch=reason)
    logger.warning(f"agent key: {reason}")


# ------------------------------------------------------------------ status

def status(current: str | None, openrouter: bool) -> dict:
    """/health "agent_key": whether OpenClaw is on the brain's key. `current`: the key the
    brain uses (ai_account.current_key)."""
    why_not = skipped(openrouter)
    if why_not is not None:
        return {"state": why_not[0], "reason": why_not[1]}
    data = load()
    if data.get("mismatch"):
        return {"state": MISMATCH, "reason": data["mismatch"], "since": data.get("at")}
    if not current:
        return {"state": NONE, "reason": "the brain has no key"}
    if data.get("sha256") != fingerprint(current):
        return {"state": SEPARATE,
                "reason": "OpenClaw keeps its own key: the brain's was not saved from Settings"}
    if data.get("restart"):
        out = {"state": RESTART, "reason": "OpenClaw restarts to use the key once nobody is talking",
               "since": data.get("at")}
        if data.get("restart_error"):
            out["last_error"] = data["restart_error"]
        return out
    return {"state": SHARED, "reason": "OpenClaw uses the brain's key", "since": data.get("at")}


# ------------------------------------------------------------------ the owed restart

class Restarter:
    """Restarts the OpenClaw gateway when a key it was given needs one (the state file's
    "restart"), once no conversation has been live for IDLE_SECS. One per brain
    (RESTARTER), run beside the sessions (gateway_server's lifespan)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def poke(self) -> None:
        """Look again soon (a save in this process, a conversation's start or end). Safe
        from any thread, and a no-op where the Restarter is not running."""
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass  # its loop has closed

    async def _restart(self) -> tuple[bool, str]:
        """`openclaw gateway restart --safe`; replaced in tests."""
        try:
            rc, _, err = await openclaw_client._run_openclaw(
                ["gateway", "restart", "--safe"], timeout=RESTART_TIMEOUT_SECS, capture=True)
        except asyncio.TimeoutError:
            return False, "the restart took too long"
        except OSError as e:
            return False, f"the openclaw command did not start ({type(e).__name__})"
        if rc != 0:
            return False, f"rc {rc}: {_last_line(err)}"
        return True, ""

    async def run(self, live, listeners: list | None = None) -> None:
        """`live()`: is a conversation live anywhere (or a call asking for the engine)?
        `listeners`: the session arbiter's, which tell it a holder changed."""
        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        wake = self._wake
        if listeners is not None:
            listeners.append(self.poke)
        last_live = self._clock()  # a brain that just started waits IDLE_SECS too
        retry_at = 0.0
        try:
            while True:
                if live():
                    last_live = self._clock()
                owed = load()
                quiet_for = self._clock() - last_live
                if (owed.get("restart") and owed.get("sha256") and not owed.get("mismatch")
                        and quiet_for >= IDLE_SECS and self._clock() >= retry_at
                        and not live()):  # again, right before
                    ok, why = await self._restart()
                    if ok:
                        with _locked():
                            now = load()
                            if now.get("sha256") == owed.get("sha256"):
                                now.update(restart=False, restart_error=None)
                                _write(now)
                        logger.info("agent key: OpenClaw restarted to use the new key")
                    else:
                        retry_at = self._clock() + RETRY_SECS
                        _update(restart_error=why)
                        logger.warning(f"agent key: restarting OpenClaw failed ({why}); "
                                       f"trying again in {RETRY_SECS:.0f} s")
                    continue
                timeout = POLL_SECS
                if owed.get("restart") and quiet_for < IDLE_SECS:
                    timeout = min(timeout, IDLE_SECS - quiet_for + 0.1)
                try:
                    await asyncio.wait_for(wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                wake.clear()
        finally:
            if listeners is not None and self.poke in listeners:
                listeners.remove(self.poke)
            self._loop = self._wake = None


RESTARTER = Restarter()
