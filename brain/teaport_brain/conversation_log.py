#
# conversation_log.py — what people say stays out of the journal unless the owner asks (#114).
#
# The journal is persistent on an appliance, and every phone call, Talk session and room
# conversation passes through the brain's log. Left alone, it is a transcript of all of
# them: the ledger's line for every turn, the judges' verdicts with the words they read,
# the room's own "user: ..." lines, pipecat's "Generating TTS [...]". A box that answers
# calls from the public must not keep one by default, so:
#
#   - every brain log line that carries words someone said or will hear (the caller's,
#     the room's, the bot's replies, a tool query made from them, a fact they asked to
#     remember) passes them through quoted(): with TEAPORT_LOG_CONVERSATIONS on, their
#     repr, as before; off (the default), only their length. The line keeps its place,
#     timing and shape, which is what most diagnosis reads.
#   - install() does the same for pipecat's own lines that carry text (_PIPECAT_TEXT),
#     in every process that runs a pipeline, through loguru's own hook for rewriting a
#     record before any sink sees it (logger.configure(patcher=...)). A pipecat upgrade
#     that adds or rewords one fails test_conversation_log's premise check, which scans
#     every pipecat module the brain loads, rather than leaking quietly.
#
# Some words are never logged, switch or no switch: a one-time code the box reads out
# (settings_access.py, #279), which is the owner's access until it is used. never_log()
# names them, and the patcher blanks them in every record of every logger.
#
# The diagnostic switches (LEDGER_TRACE, TEAPORT_TRACE, TEAPORT_ENDPOINT_DEBUG,
# TEAPORT_RAW_LLM_CAPTURE) follow the same rule: their lines carry words only with
# TEAPORT_LOG_CONVERSATIONS on too. TEAPORT_AUDIO_DUMP is the one that records on its
# own: it writes caller audio files, and is off unless it names a directory.
#
import re
import sys
import threading
import time

from loguru import logger

from teaport_brain.settings import setting

LOG_WORDS = setting("TEAPORT_LOG_CONVERSATIONS")


def quoted(said) -> str:
    """`said` for a log line: its repr with TEAPORT_LOG_CONVERSATIONS on, else its size.
    A str reads as `<N chars>`, a list or tuple of words as `<N words>`."""
    if LOG_WORDS:
        return repr(said)
    if isinstance(said, (list, tuple, set, frozenset)):
        return f"<{len(said)} words>"
    return f"<{len(said or '')} chars>"


# pipecat's log lines that carry conversation text (pipecat 1.12), in the modules the
# brain loads: each named group w1, w2 is text, and is blanked to its length.
_PIPECAT_TEXT = (
    re.compile(r"Generating TTS \[(?P<w1>.*)\]\Z", re.S),
    re.compile(r"not speaking \[(?P<w1>.*)\]\Z", re.S),
    re.compile(r"TTS request: \S+ - (?P<w1>.*)\Z", re.S),
    re.compile(r"Failed to parse function call arguments: (?P<w1>.*)\Z", re.S),
    re.compile(r"Calling function \[[^\]]*\] with arguments (?P<w1>.*)\Z", re.S),
    re.compile(r"TTS progress: .* accumulated=(?P<w1>.*) remaining=(?P<w2>.*)\Z", re.S),
    re.compile(r"\(eager: \[(?P<w1>.*)\], committed: \[(?P<w2>.*)\]\)\Z", re.S),
    re.compile(r"speculating on eager end of turn: \[(?P<w1>.*)\]\Z", re.S),
    re.compile(r"force-complete: raw_remaining (?P<w1>.*) does not contain "
               r"remaining_text (?P<w2>.*), discarding\Z", re.S),
    re.compile(r"force-completing slot with remaining text (?P<w1>.*)\Z", re.S),
    # The whole context, every turn (the OpenAI-compatible service's request line).
    re.compile(r"Generating chat from context (?P<w1>.*)\Z", re.S),
    # The TTS's word sequencer: one word, or a frame whose repr holds its text.
    re.compile(r"Dropping stale word '(?P<w1>.*)' from unknown", re.S),
    re.compile(r"Dropping (?:buffered )?word '(?P<w1>.*)' not recognised", re.S),
    re.compile(r"Emitting overflow word '(?P<w1>.*)'\Z", re.S),
    re.compile(r"Flushing Aggregated Frame (?P<w1>.*)\Z", re.S),
)


# Text no record may carry (never_log): (until, pattern), each dropped once its code has
# expired -- a code's life is minutes, and nothing is kept past it.
_NEVER: tuple = ()
# never_log and the patcher run on any thread (loguru logs from all of them): the tuple is
# replaced whole, under this lock, so an entry added while another thread drops the
# expired ones is never lost.
_NEVER_LOCK = threading.Lock()
NEVER_MARK = "<code>"
# A clause of a spelled code is blanked from this many of its items in a row: fewer is
# too common to be the code ("two, three" is anyone's words).
NEVER_MIN_ITEMS = 3


def never_log(text: str, sep: str | None = None, secs: float = 600.0) -> None:
    """`text` never reaches the journal for the next `secs` (its code's life, and some):
    the patcher blanks it in every record, TEAPORT_LOG_CONVERSATIONS or not. With `sep` (a
    code spelled out, items joined by it), any NEVER_MIN_ITEMS or more of its items in a
    row are blanked too -- the TTS can log a line cut into clauses -- and only as whole
    items (never inside a word: "DATA, BASE" is no "A, B")."""
    if not text:
        return
    items = text.split(sep) if sep else [text]
    joiner = re.escape(sep.strip() if sep else "") + r"\s*"
    runs = {joiner.join(re.escape(x.strip()) for x in items[i:j])
            for i in range(len(items))
            for j in range(i + min(NEVER_MIN_ITEMS, len(items)), len(items) + 1)}
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(sorted(runs, key=len, reverse=True)) + r")(?!\w)")
    global _NEVER
    with _NEVER_LOCK:
        _NEVER = _NEVER + ((time.monotonic() + secs, pattern),)


def redact(record: dict) -> None:
    """A loguru patcher: blanks never_log()'s text in every record, and the text in
    pipecat's conversation-carrying lines (unless TEAPORT_LOG_CONVERSATIONS)."""
    global _NEVER
    if _NEVER:
        now = time.monotonic()
        if any(until <= now for until, _ in _NEVER):
            with _NEVER_LOCK:
                _NEVER = tuple(e for e in _NEVER if e[0] > now)
        for _, pattern in _NEVER:
            record["message"] = pattern.sub(NEVER_MARK, record["message"])
    if LOG_WORDS or not (record.get("name") or "").startswith("pipecat"):
        return
    msg = record["message"]
    for pat in _PIPECAT_TEXT:
        m = pat.search(msg)
        if m:
            for g in sorted(m.groupdict(), key=m.start, reverse=True):
                msg = msg[:m.start(g)] + f"<{len(m.group(g))} chars>" + msg[m.end(g):]
            record["message"] = msg
            return


def install(level: str | None = None) -> None:
    """This process's logging: loguru's stderr sink (the journal), at `level` (else
    LOGURU_LEVEL, default DEBUG; never below DEBUG unless TEAPORT_LOG_CONVERSATIONS
    is on), with pipecat's text-carrying lines redacted.
    Called first thing by each entry point, the way a pipecat app owns its logging
    setup. configure() sets loguru's one logger-wide patcher; neither pipecat nor the
    brain sets another.

    diagnose is off unless TEAPORT_LOG_CONVERSATIONS is on: loguru's default sink prints
    every frame's local variables in a traceback (its docs warn it leaks data in
    production), and a frame holding a caller's words would put them in the journal."""
    level = level or setting("LOGURU_LEVEL")
    if not LOG_WORDS and logger.level(level).no < logger.level("DEBUG").no:
        # TRACE lines are not redacted (test_conversation_log scans DEBUG and up):
        # pipecat's carry transcripts and word times.
        level = "DEBUG"
    logger.remove()
    logger.add(sys.stderr, level=level, diagnose=bool(LOG_WORDS))
    logger.configure(patcher=redact)
