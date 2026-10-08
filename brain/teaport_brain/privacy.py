#
# teaport — caller ids out of the journal.
#
# Who is calling is the caller's personal data, and the user's rule is that it never
# reaches the journal. The brain's own lines never name the caller (sip_server logs
# whether there was a caller id, not what it said), but the caller has to be SAID: the
# question put to a live conversation names them (call_prompt.py), so the words go
# through lines the brain does not compose itself -- the ledger's "LEDGER +assistant",
# pipecat's "Generating TTS [...]" at DEBUG, the TTS service's own debug lines, and
# every later dump of that conversation's LLM context, which keeps the question for the
# rest of the conversation.
#
# So every log record is masked on its way out: install() puts a loguru patcher on the
# one logger everything here and in pipecat writes through, and it replaces each form
# of a caller id the SIP front-end registered (remember(): the name or number as the
# face shows it, the raw SIP user and its digits, the display name, and their
# JSON-escaped spellings, as a context dump writes them) with "<caller>". A caller is
# remembered from call.incoming on and kept among the last KEEP callers -- not dropped
# at the hangup, since the question stays in the held conversation's context, and its
# dumps, after the call. It lives in this process's memory only.
#
import json
import re
import threading
from collections import OrderedDict

from loguru import logger

MASK = "<caller>"
# How many callers' forms are kept: the conversation that was asked about one keeps
# the question in its context for as long as it lasts.
KEEP = 32
# Shorter forms are not masked: they would match ordinary text.
MIN_CHARS = 3

_lock = threading.Lock()
_callers: "OrderedDict[str, tuple[str, ...]]" = OrderedDict()
_pattern: re.Pattern | None = None


def _forms(*texts) -> set[str]:
    out = set()
    for t in texts:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if len(t) < MIN_CHARS:
            continue
        out.add(t)
        out.add(json.dumps(t)[1:-1])                      # as a context dump escapes it
        out.add(json.dumps(t, ensure_ascii=False)[1:-1])
        digits = re.sub(r"\D", "", t)
        if len(digits) >= 7:                                # a phone number, however written
            out.add(digits)
            if digits.startswith("1") and len(digits) == 11:
                out.add(digits[1:])
                d = digits[1:]
                out.add(f"{d[:3]} {d[3:6]} {d[6:]}")
    return {f for f in out if len(f) >= MIN_CHARS}


def remember(key: str, *texts) -> None:
    """Mask every form of `texts` (a caller id, its display name, its SIP user) from
    now on, under `key` (the call id)."""
    forms = _forms(*texts)
    if not forms:
        return
    global _pattern
    with _lock:
        _callers.pop(key, None)
        _callers[key] = tuple(forms)
        while len(_callers) > KEEP:
            _callers.popitem(last=False)
        every = {f for fs in _callers.values() for f in fs}
        # Longest first, so "+1 555 123 4567" goes whole rather than leaving "+1 ".
        _pattern = re.compile("|".join(re.escape(f) for f in sorted(every, key=len,
                                                                   reverse=True)))


def mask(text: str) -> str:
    p = _pattern
    if p is None or not isinstance(text, str):
        return text
    return p.sub(MASK, text)


def _patch(record) -> None:
    if _pattern is not None:
        record["message"] = mask(record["message"])


def install() -> None:
    """Mask caller ids in every log record of this process (once, at start)."""
    logger.configure(patcher=_patch)


def clear() -> None:
    """Forget every caller (tests)."""
    global _pattern
    with _lock:
        _callers.clear()
        _pattern = None
