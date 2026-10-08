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
# So every log line is masked on its way out, "<caller>" for each registered caller:
#
#   * install() puts a patcher on the one loguru logger everything here and in pipecat
#     writes through (the message), and replaces loguru's stderr handler -- the journal
#     -- with one that masks the whole formatted line, a traceback included, and never
#     prints variable values into it (diagnose off: loguru's default prints them).
#   * remember() registers a caller's forms (sip_server, at call.incoming): the caller
#     id as the face shows it, as the question says it (session_arbiter.
#     speakable_caller: cleaned and cut), the display name. A NAME is matched as whole
#     words, whatever its case, and in the JSON-escaped spellings a context dump writes.
#     A NUMBER (7 digits or more) is matched however it is written -- spaces, dashes,
#     dots, parentheses between its digits, with or without "+" and the country code,
#     with a trunk 0 in place of the country code (+44 7700 900123 as 07700 900123) --
#     never inside a longer run of digits. And a run of 4 or more of its digits that a
#     preview cut off at an ellipsis ("+1 555 123 45…") goes too.
#
# How long: a caller is kept among the last KEEP (32), in this process's memory only,
# never on disk -- not dropped at the hangup, since the question stays in the held
# conversation's context, and its later dumps, after the call. Bounded, so a box that
# takes calls all day keeps a small, fixed set.
#
import json
import os
import re
import sys
import threading
import unicodedata
from collections import OrderedDict

from loguru import logger

MASK = "<caller>"
KEEP = 32
# Shorter names are not masked (they would match ordinary words), nor shorter numbers
# (an extension, a status code).
MIN_NAME_CHARS = 3
MIN_NUMBER_DIGITS = 7
# The digits a cut-off preview may leave of a registered number.
MIN_PARTIAL_DIGITS = 4

_SEP = r"[\s\-.()/]*"
_lock = threading.Lock()
_callers: "OrderedDict[str, tuple[set, set]]" = OrderedDict()   # key -> (names, numbers)
_pattern: re.Pattern | None = None
_cores: tuple[str, ...] = ()   # every registered number's digit forms, for the partials
# A run of digits (and a number's separators) at a cut: before an ellipsis, or after one.
_PARTIAL = re.compile(r"(?<!\d)\+?\d[\d\s\-.()/]*\d(?=\s*(?:…|\.\.\.))"
                      r"|(?:(?<=…)|(?<=\.\.\.))\s*\d[\d\s\-.()/]*\d(?!\d)")


def _number_cores(digits: str) -> set[str]:
    """The ways a number's digits appear: whole, and without a country code of 1-3
    digits where what is left is still a full number (9 digits or more)."""
    cores = {digits}
    for k in (1, 2, 3):
        if len(digits) - k >= 9:
            cores.add(digits[k:])
    return cores


def _number_regex(core: str) -> str:
    body = _SEP.join(core)
    # Optional: a country code ("+1 ", "+44(", or "1-": a bare one only before a
    # separator, so a longer run of digits is never cut into), a "(", a trunk 0.
    return (r"(?<!\d)(?:\+\s*\d{1,3}" + _SEP + r"|\d{1,3}[\s\-.()/]+)?\(?(?:0" + _SEP
            + r")?" + body + r"(?!\d)")


def _name_regex(name: str) -> str:
    return r"(?<!\w)" + re.escape(name) + r"(?!\w)"


def _classify(texts) -> tuple[set, set]:
    names, numbers = set(), set()
    for t in texts:
        if not isinstance(t, str):
            continue
        t = " ".join(unicodedata.normalize("NFC", t).split())
        digits = re.sub(r"\D", "", t)
        if len(digits) >= MIN_NUMBER_DIGITS and len(digits) >= len(re.sub(r"\W", "", t)) - 1:
            numbers |= _number_cores(digits)          # a number, however written
        elif len(t) >= MIN_NAME_CHARS and any(ch.isalpha() for ch in t):
            names.add(t)
            for enc in (json.dumps(t)[1:-1], json.dumps(t, ensure_ascii=False)[1:-1]):
                names.add(enc)                        # as a context dump escapes it
    return names, numbers


def _rebuild() -> None:
    global _pattern, _cores
    names = {n for ns, _ in _callers.values() for n in ns}
    numbers = {d for _, ds in _callers.values() for d in ds}
    parts = [_number_regex(d) for d in sorted(numbers, key=len, reverse=True)]
    parts += [_name_regex(n) for n in sorted(names, key=len, reverse=True)]
    _pattern = re.compile("|".join(parts), re.IGNORECASE) if parts else None
    _cores = tuple(numbers)


def remember(key: str, *texts) -> None:
    """Mask every form of `texts` (a caller id, the way it is said, a display name, a
    SIP user that is a phone number) from now on, under `key` (the call id)."""
    names, numbers = _classify(texts)
    if not names and not numbers:
        return
    with _lock:
        _callers.pop(key, None)
        _callers[key] = (names, numbers)
        while len(_callers) > KEEP:
            _callers.popitem(last=False)
        _rebuild()


def _partial(m: re.Match) -> str:
    run = m.group(0)
    digits = re.sub(r"\D", "", run)
    if len(digits) >= MIN_PARTIAL_DIGITS and any(digits in core for core in _cores):
        lead = run[:len(run) - len(run.lstrip())]
        return lead + MASK
    return run


def mask(text: str) -> str:
    p = _pattern
    if p is None or not isinstance(text, str):
        return text
    text = p.sub(MASK, text)
    if _cores:
        text = _PARTIAL.sub(_partial, text)
    return text


def _patch(record) -> None:
    if _pattern is not None:
        record["message"] = mask(record["message"])


class _MaskedStderr:
    """loguru's stderr sink, each formatted line (traceback and all) masked."""

    def write(self, message: str) -> None:
        sys.stderr.write(mask(message))

    def flush(self) -> None:
        sys.stderr.flush()


def install() -> None:
    """Mask caller ids in every log line of this process (once, at start): the patcher
    for every sink, and the stderr sink -- the journal -- replaced by a masked one with
    loguru's diagnose (variable values in tracebacks) off."""
    logger.configure(patcher=_patch)
    logger.remove()
    logger.add(_MaskedStderr(), level=os.getenv("LOGURU_LEVEL", "DEBUG"), diagnose=False,
               backtrace=False, colorize=False)


def clear() -> None:
    """Forget every caller (tests)."""
    with _lock:
        _callers.clear()
        _rebuild()
