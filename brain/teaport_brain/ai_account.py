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
# Connecting with OpenRouter's OAuth (#282) ends the same way: whatever hands over a key
# calls adopt(), which is check_key() then store_key().
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
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from loguru import logger

from teaport_brain.i18n import N_
from teaport_brain.settings import ROWS, write_secret

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


def _get(url: str, key: str, timeout: float) -> tuple[int, bytes]:
    """(status, body) of an authenticated GET. Raises OSError for no answer. Replaced in
    tests: no test talks to a provider."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}",
                                               "Accept": "application/json",
                                               "User-Agent": "teaport"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 -- http(s) only
            return r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536) if e.fp is not None else b""


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
    if not 200 <= status < 300:
        logger.info(f"ai account: the key check got HTTP {status}")
        return Check(UNKNOWN, UNREACHABLE)
    if not is_openrouter(base_url):
        return Check(WORKS)
    try:
        record = key_record(json.loads(body.decode("utf-8", "replace")))
    except ValueError:
        record = {}
    remaining = _number(record.get("limit_remaining"))
    if remaining is not None and remaining <= 0:
        return Check(REFUSED, NO_CREDIT, data=record)
    return Check(WORKS, data=record)


# ------------------------------------------------------------------ keeping it

def key_path() -> str:
    """The file the brain reads LLM_API_KEY from (its schema row's `file`)."""
    return os.path.expanduser(ROWS["LLM_API_KEY"]["file"])


def store_key(key: str) -> None:
    """Write `key` where the brain reads it, read again by the next session. The caller
    has checked it (or the owner said to save it anyway)."""
    write_secret(key_path(), key.strip())
    logger.info("ai account: a new key was saved")


def adopt(key: str, base_url: str | None, timeout: float = CHECK_TIMEOUT_SECS) -> Check:
    """Check `key`, and save it only if it works: the setup-mode page's way, where nobody
    is left to say "save anyway" (and #282's, once OAuth hands over a key). A key set in
    the process's environment (an env-file copy, which every reader prefers to the file)
    is not replaced from here: SET_ELSEWHERE."""
    if os.environ.get("LLM_API_KEY", "").strip():
        return Check(REFUSED, SET_ELSEWHERE)
    result = check_key(key, base_url, timeout)
    if result.state == WORKS:
        store_key(key)
    return result
