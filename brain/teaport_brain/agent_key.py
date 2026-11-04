#
# teaport — OpenClaw uses the brain's LLM key (#281): one key, one balance.
#
# On a box with OpenClaw beside the brain (TEAPORT_AGENT=openclaw), both pay for their
# answers at OpenRouter. When the brain's key is saved (ai_account.save_key: the Settings
# page, the setup-mode page, and #282's OAuth through adopt), the same key is handed to
# OpenClaw as its OpenRouter key, so the box has one key and one balance and the owner
# swaps it once. Nothing else here starts a change: a key written by hand or by the
# installer is not copied.
#
# How OpenClaw is told, its own supported way (OpenClaw 2026.9.7):
#
#     openclaw models auth paste-api-key --provider openrouter [--agent <id>]   (key on stdin)
#
# docs/cli/models.md ("paste-api-key ... In automation, pipe the key on stdin"), and the
# code behind it (src/commands/models/auth.ts modelsAuthPasteApiKeyCommand ->
# auth-api-key.ts saveModelProviderApiKey): it updates the provider's configured profile
# or its active saved key (else creates openrouter:manual), records the profile in
# openclaw.json, and leaves model selection alone; then it asks the running gateway to
# reload its auth (models.authRefresh, auth-refresh.ts). The key is read from stdin when
# stdin is no terminal (readPastedSecret), so it never appears in a process listing.
# Exit 0 is saved. The CLI still exits 0 when the gateway did not confirm applying it, and
# says so on stderr with a "Warning:" line (auth-refresh.ts, auth-api-key.ts): any such
# warning means a restart is owed -- auth.profiles and models hot-apply otherwise
# (docs/gateway/configuration/hot-reload.md). One warning is not that: "the configured
# Gateway does not share this auth state" (a gateway on another host) means sharing
# cannot apply on this setup at all: the brain's key is saved, OpenClaw keeps its own
# (this host's own store gets its previous key back; recorded when that fails), and status
# says so (REMOTE) until a later save finds the gateway sharing this host's store, or the
# first look after a brain start reads `gateway.mode` as local again (recheck_remote).
# A gateway that is not running (connection refused) is no warning: it reads the store
# when it starts.
#
# Which agent: OPENCLAW_AGENT_ID when the operator set it (the agent this box talks to,
# openclaw_client.AGENT_ID); else no --agent at all, and OpenClaw picks it its own way --
# for an auth change, its sole configured agent (models/shared.ts resolveModelsTargetAgent
# -> resolveSoleAgentId: `main` on a box with no agent list). No guess from directory
# names. A box with several agents and none named here makes the CLI fail ("Pass --agent
# <id>"), and the save is rolled back like any other failure.
#
# Already the same key, with no save recorded (provisioning gave both the starter key):
# the Restarter looks once per brain, while nobody is talking, at what OpenClaw itself
# shows -- `openclaw models status --json`, auth.providers[].profiles.labels (docs/cli/
# models.md "Auth overview"), where each api-key profile appears as
# "<profile>=<masked key>", masked by OpenClaw's maskApiKey (src/security/secret-mask.ts:
# the first and last 8 characters of a long key). OpenClaw offers no hash of a saved key
# and its state files are its own, never read here. Every OpenRouter profile showing the
# brain key's mask: recorded as shared. A different mask: "separate". No answer, or
# anything that is not a mask (a secret reference, a token, OAuth), or a credential that
# is not the saved profiles (kind "models.json": a file/exec/store SecretRef, its own
# secret, which can win over the profiles -- seen on a box after onboarding): "unsynced" (not
# known yet; the first save records it). The look writes only if nothing changed while
# its CLI ran (a save meanwhile wins).
#
# The restart (Restarter, in the brain): `openclaw gateway restart --safe`, an RPC to the
# gateway (gateway.restart.request, lifecycle-safe-restart.ts: a 10 s call) that returns
# at once -- the gateway itself then restarts after its own running work drains. Only once
# no conversation is live on any front-end (the session arbiter: room awake, Talk, a
# call, one on hold or one ringing) and none has been for IDLE_SECS since the last one
# ended (the arbiter's change pokes the Restarter, which stamps it); checked again right
# before. What is left is the moment between that check and the RPC: a conversation that
# starts in it meets a gateway restarting (its consults wait or fail for some seconds).
# A gateway that is not running (connection refused) has nothing to restart: it reads the
# store when it starts, and the owed restart is dropped. The setup-mode unit, a process of
# its own, records the need in the state file and the brain's Restarter acts on it.
#
# Both places or neither (ai_account.save_key): one save at a time on the box (save_lock:
# a lock in this process and a flock across processes, the setup-mode unit's saves
# included), from reading the previous key to the last state write. The brain's key file
# is written first, then OpenClaw is told; if OpenClaw fails, the brain's file is put
# back. If OpenClaw may have taken the new key before failing (it said "API key saved"
# first, or it ran out of time), it is given the old one back -- possible only when the
# old one is a key this module gave it (the state file's fingerprint). When it is not, or
# that fails too, the state says "mismatch" until a save goes through, and /health and
# `teaport status` say that the two may be on different keys. A save that is cancelled or
# breaks midway is put back the same way; one torn by a crash leaves its "saving" marker,
# which the Restarter turns into a mismatch once no save holds the lock.
#
# The state file (~/.local/state/teaport/agent_key.json, mode 600): the SHA-256 of the
# key OpenClaw was last given from here (credit.py's marker recipe: the key alone, no
# newline), whether a restart is owed, a mismatch's reason, the save in progress (two
# fingerprints), what the first look saw, and whether the gateway shares this host's
# store. Fingerprints, never a key. Nothing here logs a key: the CLI's stderr is logged
# redacted (ai_account.redact), and save_key marks both keys never_log for the journal.
#
# No Pipecat import: the setup-mode unit runs this too.
#
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import weakref
from dataclasses import dataclass

from loguru import logger

from teaport_brain import agent_backend, openclaw_client
from teaport_brain.settings import secret_file, setting, state_dir

SHARES = setting("TEAPORT_AGENT_SHARES_KEY")
PROVIDER = "openrouter"
STATE_FILE = "agent_key.json"
PASTE_TIMEOUT_SECS = 60.0     # a cold CLI on the box takes several seconds; this is past it
STATUS_TIMEOUT_SECS = 60.0    # `openclaw models status --json`: a cold CLI, no model call
RESTART_TIMEOUT_SECS = 60.0   # a cold CLI, a 5 s metadata refresh and the 10 s RPC
IDLE_SECS = 30.0              # nobody talking for this long before OpenClaw restarts
POLL_SECS = 30.0              # how often the Restarter reads the state file
RETRY_SECS = 600.0            # after a failed restart
ERROR_BACKOFF_SECS = 60.0     # after the Restarter's own pass failed
LOCK_RETRY_SECS = 0.1         # the save lock's flock, tried without blocking the loop
SAVE_WAIT_SECS = 180.0        # past a whole other save (two pastes and their settling)

# What status() says (/health "agent_key"; `teaport status` words them).
OFF, ABSENT, OTHER, REMOTE, NONE, UNSYNCED, SEPARATE, SHARED, RESTART, MISMATCH = (
    "off", "absent", "other", "remote", "none", "unsynced", "separate", "shared",
    "restart", "mismatch")
# What the CLI says when it has written the key, whatever went wrong after.
_WROTE = "API key saved"
_WARNING = "Warning:"
_REMOTE_HINT = "does not share this auth state"
_REFUSED = "ECONNREFUSED"
_UNSET = "Config path is valid but unset"
REMOTE_REASON = ("the OpenClaw gateway this host talks to does not share its auth store, "
                 "so OpenClaw keeps its own key")


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def agent_args() -> list[str]:
    """--agent for an auth change: the one named (OPENCLAW_AGENT_ID), else none -- OpenClaw
    resolves its own (the module note)."""
    return ["--agent", openclaw_client.AGENT_ID] if openclaw_client.AGENT_ID else []


def mask(key: str) -> str:
    """`key` as OpenClaw shows a saved one (maskApiKey, src/security/secret-mask.ts)."""
    k = key.strip()
    if not k:
        return "missing"
    if len(k) <= 6:
        return f"{k[:1]}...{k[-1:]}"
    if len(k) <= 16:
        return f"{k[:2]}...{k[-2:]}"
    return f"{k[:8]}...{k[-8:]}"


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


async def settle() -> None:
    """Wait out a stopped CLI's end (openclaw_client.settle): for a caller whose event loop
    is about to close."""
    await openclaw_client.settle()


# ------------------------------------------------------------------ the state file

def _path(name: str = STATE_FILE) -> str:
    return os.path.join(state_dir(), name)


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _locked():
    """The state file's read-modify-write, one at a time across processes (the brain and
    the setup-mode unit both write it). Held only around a JSON read and write, never
    across an await. The async paths take it in a thread (aupdate); save_key's marker
    writes and its clean-up take it on the event loop, on purpose -- a write in a thread
    that a cancellation leaves running could land after the clean-up -- for a few
    milliseconds at most, as nothing holds it longer."""
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    fd = os.open(_path() + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=state_dir(), prefix=STATE_FILE + ".")  # mode 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def update(change=None, **values) -> dict:
    """Set `values` in the state (and `at`), or apply `change(data)` to it: under the lock,
    from what the file holds now. `change` returning False writes nothing."""
    with _locked():
        data = load()
        if change is not None and change(data) is False:
            return data
        data.update(values)
        data["at"] = time.time()
        _write(data)
        return data


async def aupdate(change=None, **values) -> dict:
    return await asyncio.to_thread(update, change, **values)


def mismatch(reason: str) -> None:
    """Record that OpenClaw and the brain may be on different keys, until a save goes
    through: status() says so (MISMATCH), whatever else was recorded (a "remote" setup
    included). Sync: a save being cancelled records it."""
    update(sha256=None, mismatch=reason, restart=False, unshareable=None)
    logger.warning(f"agent key: {reason}")


# ------------------------------------------------------------------ one save at a time

_LOOP_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


class Busy(Exception):
    """Another key change held the save lock for longer than SAVE_WAIT_SECS."""


@contextlib.asynccontextmanager
async def save_lock(wait: float | None = None):
    """One save at a time on the box: an asyncio lock for this process's saves (in order),
    and a flock on agent_key.save.lock for the other processes' (the setup-mode unit). The
    flock is tried without blocking and retried, so the event loop never waits on it and
    a cancelled waiter holds nothing. Raises Busy after `wait` (SAVE_WAIT_SECS) seconds."""
    deadline = time.monotonic() + (SAVE_WAIT_SECS if wait is None else wait)
    loop = asyncio.get_running_loop()
    lock = _LOOP_LOCKS.setdefault(loop, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        raise Busy() from None
    try:
        fd = _open_save_lock()
        try:
            while not _try_flock(fd):
                if time.monotonic() >= deadline:
                    raise Busy()
                await asyncio.sleep(LOCK_RETRY_SECS)
            yield
        finally:
            os.close(fd)  # releases the flock
    finally:
        lock.release()


def _open_save_lock() -> int:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    return os.open(_path("agent_key.save.lock"), os.O_RDWR | os.O_CREAT, 0o600)


def saving_now() -> bool:
    """Does any process hold the save lock right now?"""
    try:
        fd = _open_save_lock()
    except OSError:
        return False
    try:
        return not _try_flock(fd)
    finally:
        os.close(fd)


def begin(new: str | None, prev: str | None) -> None:
    """The save in progress, before the brain's file is written: settle_torn() finds it if
    the save never ends."""
    update(saving={"new": fingerprint(new) if new else None,
                   "prev": fingerprint(prev) if prev else None})


def end(saved: bool = False) -> int:
    """The save is over: its marker cleared, and, for one that went through, the save
    generation moved on (a Saved's undo applies only while it is still its own). Returns
    the generation now."""
    def over(data):
        data["saving"] = None
        if saved:
            data["gen"] = int(data.get("gen") or 0) + 1
    return int(update(over).get("gen") or 0)


def generation() -> int:
    return int(load().get("gen") or 0)


def settle_torn(file_key=None) -> bool:
    """A save marker with no save holding the lock is a save that never got to its end.
    Recorded as a mismatch (True) -- unless the save had in fact gone through in both
    places, its end-of-save bookkeeping alone failing (a full disk): OpenClaw was given the
    marker's key (the state's fingerprint) and the brain's key file holds it (`file_key()`:
    settings.secret_file, the file alone whatever the environment says; None for a
    missing or unreadable one, which is then a plain mismatch). That one is closed as the save it was. Either way the save
    generation moves on, so no Saved from before it can undo over it. The save lock is
    taken first (without waiting: a save under way holds it, and this is no torn save),
    and the marker is read and cleared only while it is held, so a save that ends or
    begins meanwhile is never mistaken for one."""
    try:
        fd = _open_save_lock()
    except OSError:
        return False
    try:
        if not _try_flock(fd):
            return False
        data = load()
        marker = data.get("saving")
        if not marker:
            return False
        on_file = (file_key or (lambda: secret_file("LLM_API_KEY")))()
        if (marker.get("new") and data.get("sha256") == marker["new"] and on_file
                and fingerprint(on_file) == marker["new"]):
            end(saved=True)
            logger.info("agent key: a save's last state write had failed; it went through, "
                        "and is closed now")
            return False
        mismatch("a key change was interrupted: OpenClaw and the brain may hold different keys")
        end(saved=True)
        return True
    finally:
        os.close(fd)


# ------------------------------------------------------------------ the CLI

@dataclass(frozen=True)
class Ran:
    rc: int | None            # None: it did not run to the end
    out: bytes = b""
    err: bytes = b""
    detail: str = ""          # why not, when rc is None
    timed_out: bool = False

    @property
    def text(self) -> str:
        return self.err.decode("utf-8", "replace")


def _last_line(err: bytes) -> str:
    """The CLI's last stderr line, for the journal: redacted (ai_account.redact)."""
    from teaport_brain.ai_account import redact  # ai_account imports this module
    lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    return redact(lines[-1])[:200] if lines else ""


async def _cli(args: list[str], timeout: float, stdin: bytes | None = None) -> Ran:
    """One `openclaw` run (openclaw_client._run_openclaw), its failures to start or finish
    as a Ran rather than an exception. A cancellation is the caller's and goes through."""
    try:
        rc, out, err = await openclaw_client._run_openclaw(
            args, timeout=timeout, capture=True, stdin=stdin)
    except asyncio.TimeoutError:
        return Ran(None, detail="the openclaw command took too long", timed_out=True)
    except OSError as e:
        return Ran(None, detail=f"the openclaw command did not start ({type(e).__name__})")
    return Ran(rc, out, err)


# ------------------------------------------------------------------ telling OpenClaw

@dataclass(frozen=True)
class Told:
    ok: bool
    restart: bool = False     # saved, but the gateway has not applied it yet
    wrote: bool = False       # (a failure) OpenClaw may hold the new key all the same
    remote: bool = False      # saved here, but the gateway shares no store with this host
    detail: str = ""          # for the journal: redacted, one line


async def _paste(key: str) -> Told:
    """Give OpenClaw `key` as its OpenRouter key (the module note). Replaced in tests: no
    test runs a real OpenClaw."""
    ran = await _cli(["models", "auth", "paste-api-key", "--provider", PROVIDER, *agent_args()],
                     PASTE_TIMEOUT_SECS, stdin=(key + "\n").encode("utf-8"))
    if ran.rc is None:
        return Told(False, wrote=ran.timed_out, detail=ran.detail)
    if ran.rc != 0:
        return Told(False, wrote=_WROTE in ran.text, detail=f"rc {ran.rc}: {_last_line(ran.err)}")
    if _REMOTE_HINT in ran.text:
        return Told(True, remote=True, detail=_last_line(ran.err))
    # Exit 0 with a warning: written, not confirmed applied -- a restart applies it.
    return Told(True, restart=_WARNING in ran.text, detail=_last_line(ran.err))


@dataclass(frozen=True)
class Gave:
    """give()'s answer when OpenClaw did not take the key: `halfway` when it may hold it
    all the same (the state records a mismatch)."""
    detail: str
    halfway: bool = False


async def give(key: str, prev: str | None, openrouter: bool) -> Gave | None:
    """Hand `key` to OpenClaw, as part of ai_account.save_key (under save_lock). None:
    done, or nothing to do on this box (skipped, or a gateway that shares no store here).
    A Gave: OpenClaw did not take it, and holds what it held before (given back when it
    may have taken the new one and the old one is known), or -- halfway -- the state
    records the mismatch. `prev`: the key the brain used until now."""
    why_not = skipped(openrouter)
    if why_not is not None:
        logger.info(f"agent key: OpenClaw not given the new key: {why_not[1]}")
        return None
    fp = fingerprint(key)
    told = await _paste(key)
    if told.ok and told.remote:
        # Sharing cannot apply here: put this host's own store back where it was (it is
        # not the gateway's), and save the brain's key alone.
        logger.info(f"agent key: {REMOTE_REASON} ({told.detail})")
        reason = REMOTE_REASON
        back = await _paste(prev) if prev else None
        if back is None or not back.ok:
            # This host's own store keeps the new key: not the gateway's, so nothing that
            # answers uses it, but said as it is.
            reason += ("; this host's own OpenClaw store kept the new key" +
                       (f" (putting the previous one back failed: {back.detail})" if back else ""))
            logger.warning(f"agent key: {reason}")
        await aupdate(unshareable=reason)
        return None
    if told.ok:
        def took(data):
            data.update(sha256=fp, mismatch=None, unshareable=None,
                        restart=bool(told.restart or (data.get("restart")
                                                      and data.get("sha256") == fp)))
        await aupdate(took)
        logger.info("agent key: OpenClaw has the new key" +
                    (f" (it restarts to use it once nobody is talking: {told.detail})"
                     if told.restart else ""))
        if told.restart:
            RESTARTER.poke()
        return None
    logger.warning(f"agent key: OpenClaw did not take the new key: {told.detail}")
    if not told.wrote:
        return Gave(told.detail)
    if prev and load().get("sha256") == fingerprint(prev):
        # A CLI that ran out of time is still being stopped (_end_cli): it holds the auth
        # store until it is gone, and a second one would find it busy.
        await settle()
        back = await _paste(prev)
        if back.ok and not back.remote:
            logger.info("agent key: OpenClaw has its previous key back")
            if back.restart:
                await aupdate(restart=True)
                RESTARTER.poke()
            return Gave(told.detail)
        logger.warning(f"agent key: giving OpenClaw its previous key back failed: {back.detail}")
    await asyncio.to_thread(mismatch, "a key change failed halfway: OpenClaw may hold the new "
                                      "key while the brain kept the previous one")
    return Gave(told.detail, halfway=True)


# ------------------------------------------------------------------ the first look

SAME, DIFFERENT = "same", "different"
_MASKED = re.compile(r"\S{1,8}\.\.\.\S{1,8}")
_NOT_A_MASK = ("ref(", "token:", "marker(", "OAuth", "missing")


def compare_labels(status_json: dict | None, key: str) -> str | None:
    """From `openclaw models status --json`: SAME when OpenClaw's OpenRouter credential is
    saved profiles and every one shows `key`'s mask, DIFFERENT when every one shows
    another mask, None when it cannot be told (no answer, no OpenRouter row, another kind
    of credential, anything not shaped like a mask, a mix)."""
    auth = status_json.get("auth") if isinstance(status_json, dict) else None
    rows = auth.get("providers") if isinstance(auth, dict) else None
    if not isinstance(rows, list):
        return None
    row = next((r for r in rows if isinstance(r, dict) and r.get("provider") == PROVIDER), None)
    if row is None or (row.get("effective") or {}).get("kind") != "profiles":
        return None
    labels = (row.get("profiles") or {}).get("labels")
    if not isinstance(labels, list) or not labels:
        return None
    shown = []
    for label in labels:
        if not isinstance(label, str) or "=" not in label:
            return None
        value = label.split("=", 1)[1].split(" [", 1)[0].strip()  # " [cooldown ...]"
        if value.startswith(_NOT_A_MASK) or not _MASKED.fullmatch(value):
            return None
        shown.append(value)
    want = mask(key)
    if all(m == want for m in shown):
        return SAME
    if all(m != want for m in shown):
        return DIFFERENT
    return None


async def _shown() -> dict | None:
    """`openclaw models status --json`; replaced in tests."""
    ran = await _cli(["models", "status", "--json", *agent_args()], STATUS_TIMEOUT_SECS)
    if ran.rc not in (0, 1):  # 1: a route has an auth issue, and the JSON is still written
        return None
    return openclaw_client.cli_json(ran.out, lambda d: "auth" in d)


def unlooked(current: str | None, openrouter: bool) -> bool:
    """Is there a first look to take: OpenClaw shares keys here, the brain has one, and
    nothing is recorded about OpenClaw holding it (no save, no look at this key)?"""
    if not current or skipped(openrouter) is not None:
        return False
    data = load()
    fp = fingerprint(current)
    return (not data.get("mismatch") and not data.get("saving")
            and data.get("sha256") != fp and data.get("looked") != fp)


async def _gateway_mode() -> str | None:
    """`openclaw config get gateway.mode` (docs/cli/config.md): "remote" for a gateway on
    another host (call.ts isImplicitLocalGatewayTarget: the CLI's own test), "local" when
    unset -- exit 1 with "Config path is valid but unset" (config-cli.ts: the runtime
    default applies) -- and None when it could not be read: any other exit 1 (an invalid
    config, a read failure) or failure. Replaced in tests."""
    ran = await _cli(["config", "get", "gateway.mode"], STATUS_TIMEOUT_SECS)
    if ran.rc == 1 and _UNSET in ran.text:
        return "local"
    if ran.rc != 0:
        return None
    words = ran.out.decode("utf-8", "replace").split()
    return words[-1] if words else None


async def recheck_remote(openrouter: bool) -> None:
    """A "remote" setup recorded by an earlier save, looked at again (the first look, once
    per brain): a gateway no longer in remote mode clears it, and the next save shares the
    key again; one still remote, or that cannot be read, keeps it."""
    if not load().get("unshareable") or skipped(openrouter) is not None:
        return
    if await _gateway_mode() not in (None, "remote"):
        await aupdate(unshareable=None)
        logger.info("agent key: the OpenClaw gateway is local again: the next save shares the key")


async def look(brain) -> str | None:
    """The first look (the module note), for the key `brain()` gives ((key, openrouter)):
    SAME recorded as shared, DIFFERENT as separate; nothing when it cannot be told. Writes
    only if the state and the brain's key are as they were before the CLI ran: a save
    that landed meanwhile is never overwritten. Returns what was seen."""
    current, openrouter = brain()
    await recheck_remote(openrouter)
    if not unlooked(current, openrouter):
        return None
    before = load()
    seen = compare_labels(await _shown(), current)
    if seen is None:
        logger.info("agent key: OpenClaw's status doesn't show whether it uses the brain's key "
                    "(no saved OpenRouter profile it is known to use); the next key saved in "
                    "Settings gives it the same one")
        return None
    fp = fingerprint(current)

    def settle_look(data):
        if data != before or brain()[0] != current:
            return False  # a save (or another look) got there first
        if seen == SAME:
            data.update(sha256=fp, looked=fp, restart=False)
        else:
            data.update(looked=fp)

    if (await aupdate(settle_look)).get("looked") != fp:
        return None
    logger.info(f"agent key: OpenClaw shows {'the same key as' if seen == SAME else 'another key than'} "
                f"the brain")
    return seen


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
    if data.get("unshareable"):
        return {"state": REMOTE, "reason": data["unshareable"]}
    if not current:
        return {"state": NONE, "reason": "the brain has no key"}
    if data.get("sha256") != fingerprint(current):
        if data.get("looked") == fingerprint(current):
            return {"state": SEPARATE,
                    "reason": "OpenClaw shows another OpenRouter key than the brain's"}
        return {"state": UNSYNCED,
                "reason": "not known yet whether OpenClaw has the brain's key; the next key "
                          "saved in Settings gives it the same one"}
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
    "restart"), once no conversation has been live for IDLE_SECS; as quietly, takes the
    first look (look) once per brain where nothing is recorded; and turns a torn save's
    marker into a mismatch. One per brain (RESTARTER), run beside the sessions
    (gateway_server's lifespan)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_live = clock()  # a brain that just started waits IDLE_SECS too

    def poke(self) -> None:
        """Something happened (the arbiter's holder changed -- a conversation started or
        ended -- or a save in this process): it counts as activity, and the Restarter looks
        again. Safe from any thread, and only the stamp where the Restarter is not
        running."""
        self._last_live = self._clock()
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass  # its loop has closed

    async def _restart(self) -> tuple[bool, str]:
        """`openclaw gateway restart --safe` (the module note): (done, why not). Replaced
        in tests."""
        ran = await _cli(["gateway", "restart", "--safe"], RESTART_TIMEOUT_SECS)
        if ran.rc is None:
            return False, ran.detail
        if ran.rc != 0:
            if _REFUSED in ran.text:  # anywhere in its output, not only the last line
                return False, f"the gateway is not running ({_REFUSED})"
            return False, f"rc {ran.rc}: {_last_line(ran.err)}"
        return True, ""

    async def _pass(self, live, brain, state: dict) -> float:
        """One look at what is owed; how long to wait before the next. Quiet is counted
        from the last moment anything was live: a pass that sees it live stamps it, and so
        does the first pass that sees it idle again -- every way of going idle (a call
        hung up, Talk closed, a room mic falling asleep, which tells the arbiter's
        listeners nothing) -- as does poke() on any holder change."""
        now_live = live()
        if now_live or state.get("was_live"):
            self._last_live = self._clock()
        state["was_live"] = now_live
        await asyncio.to_thread(settle_torn)
        quiet_for = self._clock() - self._last_live
        if not state["looked"] and quiet_for >= IDLE_SECS and not live():
            state["looked"] = True  # once per brain, whatever it shows
            await look(brain)
            return 0.0
        owed = load()
        if (owed.get("restart") and owed.get("sha256") and not owed.get("mismatch")
                and quiet_for >= IDLE_SECS and self._clock() >= state["retry_at"]
                and not live()):  # again, right before
            ok, why = await self._restart()
            if ok or _REFUSED in why:
                # Restarted; or not running at all, so it reads the store when it starts.
                def done(data):
                    if data.get("sha256") != owed.get("sha256"):
                        return False  # another key meanwhile: its own restart stands
                    data.update(restart=False, restart_error=None)

                await aupdate(done)
                logger.info("agent key: OpenClaw restarted to use the new key" if ok else
                            "agent key: OpenClaw is not running; it uses the new key when it starts")
            else:
                state["retry_at"] = self._clock() + RETRY_SECS
                await aupdate(restart_error=why)
                logger.warning(f"agent key: restarting OpenClaw failed ({why}); "
                               f"trying again in {RETRY_SECS:.0f} s")
            return 0.0
        timeout = POLL_SECS
        if (owed.get("restart") or not state["looked"]) and quiet_for < IDLE_SECS:
            timeout = min(timeout, IDLE_SECS - quiet_for + 0.1)
        return timeout

    async def run(self, live, listeners: list | None = None, brain=None) -> None:
        """`live()`: is a conversation live anywhere (or a call asking for the engine)?
        `listeners`: the session arbiter's, which tell it a holder changed. `brain()`: the
        brain's key and whether its endpoint is OpenRouter's (ai_account.brain_key; None:
        no first look)."""
        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        wake = self._wake
        if listeners is not None:
            listeners.append(self.poke)
        self._last_live = self._clock()  # a brain that just started waits IDLE_SECS too
        state = {"looked": brain is None, "retry_at": 0.0}
        try:
            while True:
                try:
                    timeout = await self._pass(live, brain, state)
                except Exception as e:  # noqa: BLE001 -- the loop must outlive a bad pass
                    logger.warning(f"agent key: the Restarter's pass failed: {e!r}")
                    timeout = ERROR_BACKOFF_SECS
                if timeout <= 0:
                    continue
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
