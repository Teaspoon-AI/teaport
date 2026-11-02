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
# The diagnostic switches (LEDGER_TRACE, TEAPORT_TRACE, TEAPORT_ENDPOINT_DEBUG,
# TEAPORT_RAW_LLM_CAPTURE) follow the same rule: their lines carry words only with
# TEAPORT_LOG_CONVERSATIONS on too. TEAPORT_AUDIO_DUMP is the one that records on its
# own: it writes caller audio files, and is off unless it names a directory.
#
import re

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
)


def redact(record: dict) -> None:
    """A loguru patcher: blanks the text in pipecat's conversation-carrying lines."""
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


def install() -> None:
    """Redact pipecat's text-carrying lines in this process (a no-op on what they log
    with TEAPORT_LOG_CONVERSATIONS on). Called first thing by each entry point, the way a
    pipecat app owns its logging setup. configure() sets loguru's one logger-wide patcher;
    neither pipecat nor the brain sets another."""
    logger.configure(patcher=redact)
