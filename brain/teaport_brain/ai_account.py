#
# teaport — the AI account: the key the brain's answers are paid with (#279).
#
# One way to take a key, whichever page it was typed on: check it with the provider
# first (check_key), then write it where the brain reads it (store_key: the LLM_API_KEY
# secret file, read per session, so the next conversation uses it with no restart).
# Two pages take one:
#
#   - Settings (config_ui.py, PUT /api/config): any save that carries LLM_API_KEY, the
#     owner's "AI account" step and the operator's row alike. A key the provider refuses,
#     or that cannot be checked, is not saved unless the save says so again ("force_key":
#     the page's "Save anyway"), and the page shows why (Check.reason).
#   - the setup-mode page (wifi_setup.py): the key rides with the Wi-Fi choice, and is
#     checked once the box is online through the network picked (setup mode has no
#     internet). Nobody is on that page by then to say "save anyway", so only a key that
#     works is saved; the voice says how it went (wifi_voice.py).
#
# Connecting with OpenRouter's sign-in (OAuth PKCE, #282, openrouter_connect.py) ends the
# same way: the key exchange_code() gets for the sign-in is saved as Settings saves a
# pasted one (config_ui.save_values: check_key, then save_key).
#
# save_key is the one save, whichever page: it writes the brain's key file (store_key) and,
# on a box with OpenClaw, hands OpenClaw the same key (agent_key.give, #281: one key, one
# balance). Both or neither: if OpenClaw does not take it, the key file is put back and
# the save says why (NotSaved); agent_key.py says what is done when even that fails.
#
# The check is one cheap authenticated request. OpenRouter: GET /api/v1/key, the key's own
# record (free, no tokens spent); it also carries the key's limit and usage, which the
# starter-credit monitor reads (#280). Any other OpenAI-compatible endpoint: GET
# <base>/models with the key. A 401, 402 or 403 is the provider refusing the key; its own
# words, cleaned (clean_reason), are the reason. No answer (no connection, a timeout, a
# 5xx) is "unknown": nothing is known to be wrong with the key.
#
# No Pipecat import: the setup-mode unit runs this alone, on a box short of memory. The
# request is urllib's, run in a thread by the async callers.
#
# Nothing here logs a key, and a reason never carries one (clean_reason; the provider
# echoes masked keys and links to the key's own page). The same cleaning serves the
# brain's record of failed answers (llm_error_speaker.failure_reason, #205).
#
import asyncio
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from loguru import logger

from teaport_brain import agent_key, conversation_log
from teaport_brain.i18n import N_
from teaport_brain.settings import ROWS, secret_file, setting, write_secret

WORKS, REFUSED, UNKNOWN = "works", "refused", "unknown"
CHECK_TIMEOUT_SECS = 10.0
OPENROUTER_HOST = "openrouter.ai"
# A key is one line of printable characters; past this it is not a key that was pasted.
KEY_MAX_CHARS = 256

# Our own reasons (msgids, translated where shown). The provider's are its own words.
EMPTY = N_("the key is empty")
NOT_A_KEY = N_("that doesn't look like a key")
NO_SERVICE = N_("no AI service is set up on this box")
UNREACHABLE = N_("the AI service didn't answer")
REFUSED_BARE = N_("the AI service refused this key")
NO_CREDIT = N_("this account has no credit left")
SET_ELSEWHERE = N_("this box's key is set by its installer, so it can't be changed here")
# A save the agent (OpenClaw) did not take: nothing changed (save_key).
NOT_SHARED = N_("Teaport couldn't switch everything over to this key, so it kept the one "
                "it had. Try again in a minute.")
# One that OpenClaw may have taken all the same: the state records a mismatch.
NOT_FINISHED = N_("Teaport couldn't finish switching to this key. Try saving it again.")


@dataclass(frozen=True)
class Check:
    """How a key fared. `reason` is English: one of the msgids above when `ours`, else
    the provider's own words (clean_reason). `data`: the provider's record of the key
    (OpenRouter's /api/v1/key "data"), {} for any other endpoint."""
    state: str               # WORKS | REFUSED | UNKNOWN
    reason: str = ""
    ours: bool = True
    data: dict = field(default_factory=dict)

    def shown(self, t) -> str:
        """The reason in t's language (the provider's own words stay as they are)."""
        return t._(self.reason) if self.ours and self.reason else self.reason

    def public(self, t=None) -> dict:
        """For a page: the state and the reason, in t's language when given."""
        return {"state": self.state,
                "reason": self.shown(t) if t is not None else self.reason}


# ------------------------------------------------------------------ cleaning a reason
# What may not leave the brain: links (a provider's key-limit message can name the key's
# management page, whose path carries the key's id) and anything key-shaped -- a key by
# its prefix (sk-, pk-, rk-, whole token), a key the provider masked ("sk-proj-****abcd",
# "****abcd": the visible tail is still part of a key), or a long opaque id.
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+", re.IGNORECASE)
_KEYLIKE = re.compile(r"\b(?:sk|pk|rk)[-_]\S*|\S*\*{3,}\S*|[A-Za-z0-9_-]{20,}")
_LINK, _KEY = "<link>", "<key>"
_STATUS_PREFIX = re.compile(r"^\s*error code:\s*\d+\s*-?\s*", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
REASON_MAX = 120


def redact(text: str) -> str:
    """`text` with every link replaced by <link> and every key-shaped token by <key>:
    the one redaction the error log line (services.push_error_frame), the record of
    failed answers and a key check's reason all go through."""
    return _KEYLIKE.sub(_KEY, _URL.sub(_LINK, text))


def clean_reason(text: str) -> str:
    """A provider's first line about a failure, safe to show: redacted, every sentence
    that carried a link dropped, keys removed, no status code, at most REASON_MAX
    characters. "" when nothing readable is left."""
    text = redact(_STATUS_PREFIX.sub("", (text.strip().splitlines() or [""])[0]))
    kept = [s for s in _SENTENCES.split(text) if _LINK not in s]
    text = " ".join(" ".join(kept).replace(_KEY, "").split()).strip(" -:;,.")
    if len(text) > REASON_MAX:
        text = text[:REASON_MAX - 1].rstrip() + "…"
    return text


# ------------------------------------------------------------------ the check

def is_openrouter(base_url: str | None) -> bool:
    """Whether `base_url` is OpenRouter's API (its host, or a subdomain of it)."""
    try:
        host = (urllib.parse.urlsplit((base_url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host == OPENROUTER_HOST or host.endswith("." + OPENROUTER_HOST)


def _check_url(base_url: str) -> str | None:
    """The cheap authenticated request for `base_url`, or None when it is no http(s) URL."""
    try:
        parts = urllib.parse.urlsplit(base_url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if is_openrouter(base_url):
        return f"{parts.scheme}://{parts.netloc}/api/v1/key"
    return base_url.strip().rstrip("/") + "/models"


def _provider_words(body: bytes) -> str:
    """The provider's message in an error body ({"error": {"message"}}, {"message"},
    {"error": "..."}, or plain text), cleaned."""
    text = body.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except ValueError:
        return clean_reason(text) if "<" not in text else ""  # an HTML error page says nothing
    if isinstance(obj, dict):
        err = obj.get("error")
        for found in ((err.get("message") if isinstance(err, dict) else err), obj.get("message")):
            if isinstance(found, str) and found.strip():
                return clean_reason(found)
    return ""


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def key_record(body) -> dict:
    """OpenRouter's record of a key from GET /api/v1/key, read tolerantly: the fields
    under "data" (the documented shape), or at the top level (an older one). {} when it
    is neither."""
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else body


@dataclass(frozen=True)
class Left:
    """What a key has left, from its record (credit_left). Any field None: not said.
    `usage` is what the record calls spent (for a page); `spent` is what is measured
    against `limit` for a share used (the half mark)."""
    limit: float | None = None
    remaining: float | None = None
    usage: float | None = None
    spent: float | None = None


def _budget_left(record: dict) -> Left | None:
    """The record's newer budget block, effective_budget {limit_usd, spend_usd,
    remaining_usd, reset_interval, resets_at, scope, include_byok} -- None unless limit_usd
    and remaining_usd are both numbers and the limit is above 0 (anything else in it is
    ignored; budgets[] is not read). Spent is spend_usd, else what the limit and the remainder imply."""
    block = record.get("effective_budget")
    if not isinstance(block, dict):
        return None
    limit, remaining = _number(block.get("limit_usd")), _number(block.get("remaining_usd"))
    if limit is None or remaining is None or limit <= 0:
        return None  # no budget set (a limit of 0 or less is none): the key's limit decides
    spent = _number(block.get("spend_usd"))
    spent = spent if spent is not None else max(0.0, limit - remaining)
    return Left(limit, remaining, spent, spent)


def credit_left(record: dict) -> Left:
    """How much a key has left, from its GET /api/v1/key record (key_record): the one
    reading the save-time check (check_key) and the trial credit's monitor (credit.judge)
    share. Two things can bound it: the key's own limit (limit, limit_remaining -- or
    limit minus usage -- and usage) and, in a newer record, its effective budget
    (_budget_left). Whichever has less left decides: its limit, what is left and what is
    spent. Malformed or missing fields are not read; Left() when nothing is said."""
    limit, usage = _number(record.get("limit")), _number(record.get("usage"))
    remaining = _number(record.get("limit_remaining"))
    if remaining is None and limit is not None and usage is not None:
        remaining = max(0.0, limit - usage)
    spent = None if limit is None or remaining is None else limit - remaining
    left = Left(limit, remaining, usage, spent)
    budget = _budget_left(record)
    if budget is not None and (remaining is None or budget.remaining < remaining):
        return budget
    return left


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as it is, never followed: following one would send the
    key's Authorization header to wherever it points (another host, plain http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cut_at(base, deadline: float):
    """`base` (an http.client connection class) whose socket is shut down at `deadline`
    (time.monotonic): every read after that ends at once. urllib's own timeout bounds
    each socket wait, not the whole: a server dripping a byte per wait could hold a
    request -- and the thread _deadline left it on -- for days."""
    class Cut(base):
        def connect(self):
            super().connect()
            timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._cut)
            timer.daemon = True
            timer.start()

        def _cut(self):
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):  # closed already, or never opened
                pass
    return Cut


class _CutHTTP(urllib.request.HTTPHandler):
    def __init__(self, deadline: float):
        super().__init__()
        self.deadline = deadline

    def http_open(self, req):
        return self.do_open(_cut_at(http.client.HTTPConnection, self.deadline), req)


class _CutHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, deadline: float):
        super().__init__()
        self.deadline = deadline

    def https_open(self, req):
        return self.do_open(_cut_at(http.client.HTTPSConnection, self.deadline), req,
                            context=self._context)


MAX_BODY_BYTES = 65536


def _read(r, deadline: float) -> bytes:
    """At most MAX_BODY_BYTES of `r`, and nothing past `deadline`."""
    out = b""
    while len(out) < MAX_BODY_BYTES:
        if time.monotonic() >= deadline:
            raise TimeoutError("the answer took too long")
        chunk = r.read1(MAX_BODY_BYTES - len(out))
        if not chunk:
            if getattr(r, "length", None):  # it said how long, and stopped short
                raise OSError("the answer was cut short")
            break
        out += chunk
    return out


def _request(url: str, key: str | None, timeout: float,
             body: bytes | None = None) -> tuple[int, bytes]:
    """One request: a GET with `key` as its bearer, or with `body` a JSON POST (no key:
    the sign-in's exchange, exchange_code). All of it within `timeout` (_cut_at), no
    redirect followed (_NoRedirects). Raises OSError for no answer, a broken one included
    (http.client's BadStatusLine, IncompleteRead, LineTooLong: a garbled answer is no
    answer)."""
    deadline = time.monotonic() + timeout
    headers = {"Accept": "application/json", "User-Agent": "teaport"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="GET" if body is None else "POST")
    opener = urllib.request.build_opener(_NoRedirects, _CutHTTP(deadline), _CutHTTPS(deadline))
    try:
        with opener.open(req, timeout=timeout) as r:  # noqa: S310 -- http(s) only
            return r.status, _read(r, deadline)
    except urllib.error.HTTPError as e:  # 3xx (not followed), 4xx, 5xx
        try:
            return e.code, _read(e, deadline) if e.fp is not None else b""
        except http.client.HTTPException as broken:
            raise OSError(f"a broken answer ({type(broken).__name__})") from broken
    except http.client.HTTPException as e:
        raise OSError(f"a broken answer ({type(e).__name__})") from e


def _get(url: str, key: str, timeout: float) -> tuple[int, bytes]:
    """(status, body) of an authenticated GET, within `timeout` overall (_deadline).
    Replaced in tests: no test talks to a provider."""
    return _deadline(lambda: _request(url, key, timeout), timeout)


def _post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """(status, body) of a JSON POST with no key, within `timeout` overall (_deadline).
    Replaced in tests: no test talks to a provider."""
    return _deadline(lambda: _request(url, None, timeout, body), timeout)


def _deadline(call, timeout: float) -> tuple[int, bytes]:
    """`call()`'s answer within `timeout` overall: urllib's own timeout bounds each socket
    wait but not the name lookup, and a router's DNS stall has held this box before.
    Raises OSError for no answer (TimeoutError past the deadline: the stuck lookup is
    left to finish on a daemon thread)."""
    done: dict = {}

    def run():
        try:
            done["answer"] = call()
        except BaseException as e:  # noqa: BLE001 -- handed to the caller below
            done["error"] = e

    worker = threading.Thread(target=run, name="ai-account-check", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("the key check took too long")
    if "error" in done:
        raise done["error"]
    return done["answer"]


def check_key(key: str, base_url: str | None, timeout: float = CHECK_TIMEOUT_SECS) -> Check:
    """Does the endpoint at `base_url` accept `key`? One authenticated request (see the
    module note). Never raises."""
    key = (key or "").strip()
    if not key:
        return Check(REFUSED, EMPTY)
    if len(key) > KEY_MAX_CHARS or key.splitlines() != [key] or not key.isprintable():
        return Check(REFUSED, NOT_A_KEY)
    url = _check_url(base_url or "")
    if url is None:
        return Check(UNKNOWN, NO_SERVICE)
    try:
        status, body = _get(url, key, timeout)
    except (OSError, ValueError) as e:  # URLError, a timeout, a refused connection
        logger.info(f"ai account: the key check got no answer ({type(e).__name__})")
        return Check(UNKNOWN, UNREACHABLE)
    if status in (401, 402, 403):
        words = _provider_words(body)
        logger.info(f"ai account: the provider refused the key ({status})")
        return Check(REFUSED, words, ours=False) if words else Check(REFUSED, REFUSED_BARE)
    if not 200 <= status < 300:  # a redirect (not followed), a 5xx, a rate limit
        logger.info(f"ai account: the key check got HTTP {status}")
        return Check(UNKNOWN, UNREACHABLE)
    if not is_openrouter(base_url):
        return Check(WORKS)
    try:
        record = key_record(json.loads(body.decode("utf-8", "replace")))
    except ValueError:
        record = {}
    remaining = credit_left(record).remaining
    if remaining is not None and remaining <= 0:  # the key's limit, or its budget, used up
        return Check(REFUSED, NO_CREDIT, data=record)
    return Check(WORKS, data=record)


# ------------------------------------------------------------------ the sign-in (#282)
# OpenRouter's OAuth PKCE (https://openrouter.ai/docs/use-cases/oauth-pkce): the code its
# sign-in hands back, with the verifier only this box holds, is exchanged for a key, once:
# POST /api/v1/auth/keys {code, code_verifier, code_challenge_method}, the answer's "key".
# A code is single-use and lasts ten minutes. The flow around it is openrouter_connect.py;
# the key goes the way every key goes: checked, then saved.
EXCHANGE_URL = f"https://{OPENROUTER_HOST}/api/v1/auth/keys"
CODE_MAX_CHARS = 512
SIGN_IN_REFUSED = N_("OpenRouter didn't accept the sign-in")


def code_shape(code: str) -> str | None:
    """`code` stripped when it could be a sign-in code (one printable word of at most
    CODE_MAX_CHARS), else None: what is not one is never sent, nor spends a sign-in."""
    code = (code or "").strip()
    if not code or len(code) > CODE_MAX_CHARS or not code.isprintable() or " " in code:
        return None
    return code


def exchange_code(code: str, verifier: str, timeout: float = CHECK_TIMEOUT_SECS
                  ) -> tuple[str | None, str]:
    """(the key, "") for a sign-in code and the verifier it was asked with, or (None, a
    reason msgid): SIGN_IN_REFUSED for a code OpenRouter does not take (wrong, used or
    expired: a 400 or 403; or not a code at all, code_shape), UNREACHABLE for no answer,
    or an answer without a key. Never raises. The key is kept out of the journal from
    here on (never_log)."""
    code = code_shape(code)
    if code is None:
        return None, SIGN_IN_REFUSED
    body = json.dumps({"code": code, "code_verifier": verifier,
                       "code_challenge_method": "S256"}).encode()
    try:
        status, raw = _post(EXCHANGE_URL, body, timeout)
    except (OSError, ValueError) as e:
        logger.info(f"ai account: the sign-in exchange got no answer ({type(e).__name__})")
        return None, UNREACHABLE
    if status in (400, 401, 403):
        logger.info(f"ai account: OpenRouter refused the sign-in code ({status})")
        return None, SIGN_IN_REFUSED
    if not 200 <= status < 300:
        logger.info(f"ai account: the sign-in exchange got HTTP {status}")
        return None, UNREACHABLE
    try:
        key = json.loads(raw.decode("utf-8", "replace")).get("key")
    except (ValueError, AttributeError):
        key = None
    if not isinstance(key, str) or not key.strip():
        logger.info("ai account: the sign-in exchange answered without a key")
        return None, UNREACHABLE
    key = key.strip()
    conversation_log.never_log(key, secs=3600.0)
    logger.info("ai account: OpenRouter gave a key for the sign-in")
    return key, ""


# ------------------------------------------------------------------ keeping it

def current_key() -> str | None:
    """The key the brain uses now: LLM_API_KEY from the environment, else its file (read
    each time: a key saved meanwhile counts at once). None when there is none."""
    return setting("LLM_API_KEY") or secret_file("LLM_API_KEY")


def key_path() -> str:
    """The file the brain reads LLM_API_KEY from (its schema row's `file`)."""
    return os.path.expanduser(ROWS["LLM_API_KEY"]["file"])


def store_key(key: str) -> None:
    """Write `key` where the brain reads it, read again by the next session: save_key's
    first step (every page saves through save_key)."""
    write_secret(key_path(), key.strip())
    logger.info("ai account: the key file was written")


def _read_file() -> str | None:
    """The key file as it is (to put it back), None when there is none."""
    try:
        with open(key_path(), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _put_back(text: str | None) -> None:
    if text is None:
        try:
            os.unlink(key_path())
        except FileNotFoundError:
            pass
    else:
        write_secret(key_path(), text.rstrip("\n"))


class NotSaved(Exception):
    """save_key did not save: `reason` is a msgid -- NOT_SHARED (nothing changed) or
    NOT_FINISHED (OpenClaw may hold the new key: the state records the mismatch)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail


@dataclass
class Saved:
    """A save that went through, and what undo() puts back: for a caller whose own next
    step failed (config_ui: brain.env's copy could not be dropped)."""
    key: str
    prev_file: str | None
    prev: str | None
    openrouter: bool
    gen: int = 0    # the save generation it made (agent_key.end)

    async def undo(self) -> None:
        """The key file as it was, and OpenClaw back on the brain's previous key, as one
        more save (save_lock, with its marker). Nothing when another save has gone through
        since -- the save generation has moved on, even for the same key again: that save
        stands. Where OpenClaw cannot follow (the brain had no key before, or OpenClaw does
        not take it), the state says the two may differ (MISMATCH)."""
        async with agent_key.save_lock():
            if agent_key.generation() != self.gen:
                logger.info("ai account: not taking the key back: another key was saved since")
                return
            sharing = agent_key.skipped(self.openrouter) is None
            if sharing:
                agent_key.begin(self.prev, self.key)
            undone = False
            try:
                _put_back(self.prev_file)
                logger.info("ai account: the new key was taken back")
                if not sharing:
                    return
                taken_back = "a key change was taken back, but OpenClaw kept the new key"
                if not self.prev:
                    agent_key.mismatch(taken_back)
                    return
                try:
                    gave = await agent_key.give(self.prev, self.key, self.openrouter)
                except BaseException:
                    agent_key.mismatch(taken_back)
                    raise
                if gave is not None and not gave.halfway:  # halfway: give recorded it
                    agent_key.mismatch(taken_back)
                undone = True
            finally:
                agent_key.end(saved=undone or not sharing)

    def unfinished(self, why: str, env_copy: bool) -> None:
        """The caller's next step failed and the key could not be taken back either (undo
        itself failed, or waited past SAVE_WAIT_SECS for another save). The key file and
        OpenClaw hold the new key. With `env_copy` -- brain.env held a copy of the key,
        which the brain reads first and which that failed step was to drop -- the brain
        still reads the old one: said in the state (MISMATCH) where OpenClaw shares it.
        Without one, all three agree on the new key: logged only."""
        if env_copy and agent_key.skipped(self.openrouter) is None:
            agent_key.mismatch(f"the key was saved but brain.env still holds the previous one "
                               f"({why}): save the key again")
        else:
            logger.warning(f"ai account: the key could not be taken back ({why}); it stays "
                           "saved, the same in the key file and OpenClaw")


async def save_key(key: str, base_url: str | None) -> Saved:
    """The one save (the module note): `key` into the brain's key file, and to OpenClaw as
    its OpenRouter key where the box has one and the brain's endpoint is OpenRouter's
    (`base_url`). The caller has checked it, or the owner said to save it anyway.

    One save at a time on the box (agent_key.save_lock), from reading the previous key to
    the last state write, so two pages (or the setup-mode unit and a page) never
    interleave. Raises NotSaved, with the key file as it was, when OpenClaw did not take
    it. A save that breaks or is cancelled midway puts the file back too, and, once
    OpenClaw may have been told, records a mismatch, before the exception goes on; one torn
    by a crash leaves its marker (agent_key.begin), which the brain turns into a
    mismatch.

    The file and marker writes are synchronous, on the event loop, like the clean-up: a
    few hundred bytes each, and a write in a thread that a cancellation left running could
    land after the clean-up put the file back. Waiting for another save is capped
    (agent_key.SAVE_WAIT_SECS): past it, NotSaved(NOT_SHARED) and nothing changed."""
    key = key.strip()
    openrouter = is_openrouter(base_url)
    sharing = agent_key.skipped(openrouter) is None
    try:
        async with agent_key.save_lock():
            return await _save_locked(key, openrouter, sharing)
    except agent_key.Busy:
        logger.warning("ai account: the new key was not saved: another key change is still running")
        raise NotSaved(NOT_SHARED, "another key change is still running") from None


async def _save_locked(key: str, openrouter: bool, sharing: bool) -> Saved:
    """save_key's work, under the save lock."""
    prev = current_key()
    for k in (key, prev):
        if k:
            conversation_log.never_log(k, secs=3600.0)
    prev_file = _read_file()
    if sharing:
        agent_key.begin(key, prev)
    told = False
    try:
        store_key(key)
        told = sharing
        gave = await agent_key.give(key, prev, openrouter)
        if gave is not None:
            _put_back(prev_file)
            logger.info("ai account: the new key was not saved, as OpenClaw did not take it")
            raise NotSaved(NOT_FINISHED if gave.halfway else NOT_SHARED, gave.detail)
    except NotSaved:
        if sharing:
            agent_key.end()
        raise
    except BaseException as e:
        # Cancelled (the brain or the setup unit stopping) or broken: synchronous from
        # here, so a second cancellation cannot cut the clean-up short.
        _put_back(prev_file)
        if told:
            agent_key.mismatch(f"a key change stopped midway ({type(e).__name__}): OpenClaw "
                               "may hold the new key while the brain kept the previous one")
        if sharing:
            agent_key.end()
        logger.warning(f"ai account: the new key was not saved ({type(e).__name__})")
        raise
    # The save went through in both places. Its last state write -- the marker cleared,
    # the generation moved on -- failing (a full disk) must not turn it into a 500: it is
    # logged, and the marker it leaves is one settle_torn recognises as a finished save
    # (OpenClaw's fingerprint and the key file both the new key) and closes, not a torn one.
    try:
        gen = agent_key.end(saved=True)
    except OSError as e:
        logger.warning(f"ai account: the key was saved, but its state could not be written ({e!r})")
        gen = agent_key.generation()
    return Saved(key, prev_file, prev, openrouter, gen)


# Where an environment copy of LLM_API_KEY comes from (shadowed): every reader prefers it
# to the key file, so a key saved to the file alone would change nothing.
IN_ENV_FILE, ELSEWHERE = "env_file", "elsewhere"


def shadowed(env_file: dict | None) -> str | None:
    """Would an environment copy of LLM_API_KEY shadow a key saved to its file? None: no
    copy. IN_ENV_FILE: brain.env holds one (`env_file`: its values, as the config page
    read them) -- the page drops it with its privileged helper and then forget_env_copy().
    ELSEWHERE: this process has one that brain.env does not explain (a unit drop-in), or
    the caller cannot read brain.env (env_file None: the setup unit, which loads it into
    its environment and cannot edit it). The one rule both pages follow (#279)."""
    if env_file is not None and (env_file.get("LLM_API_KEY") or "").strip():
        return IN_ENV_FILE
    if (os.environ.get("LLM_API_KEY") or "").strip():
        return ELSEWHERE
    return None


def forget_env_copy() -> None:
    """brain.env's copy was just dropped: this process stops reading its own copy too, so
    the key file counts from the next conversation, not the next restart."""
    os.environ.pop("LLM_API_KEY", None)


async def adopt(key: str, base_url: str | None, timeout: float = CHECK_TIMEOUT_SECS) -> Check:
    """Check `key`, and save it only if it works (save_key): the setup-mode page's way,
    where nobody is left to say "save anyway".
    With an environment copy it cannot drop (shadowed): SET_ELSEWHERE, nothing saved. A
    key that works but that OpenClaw did not take: UNKNOWN, with NotSaved's reason."""
    if shadowed(None) is not None:
        return Check(REFUSED, SET_ELSEWHERE)
    result = await asyncio.to_thread(check_key, key, base_url, timeout)
    if result.state == WORKS:
        try:
            await save_key(key, base_url)
        except NotSaved as e:
            return Check(UNKNOWN, e.reason, data=result.data)
    return result


def adopt_blocking(key: str, base_url: str | None) -> Check:
    """adopt() for a caller with no event loop (the setup-mode unit's thread)."""
    async def run():
        try:
            return await adopt(key, base_url)
        finally:
            await agent_key.settle()  # a stopped CLI's end, before the loop goes
    return asyncio.run(run())


def brain_key() -> tuple[str | None, bool]:
    """The brain's key now, and whether its endpoint (LLM_BASE_URL) is OpenRouter's: what
    agent_key's status and first look are about."""
    return current_key(), is_openrouter(setting("LLM_BASE_URL"))


def agent_key_status() -> dict:
    """/health "agent_key": whether OpenClaw is on the brain's key (agent_key.status)."""
    return agent_key.status(*brain_key())
