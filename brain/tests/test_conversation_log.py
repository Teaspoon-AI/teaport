#
# test_conversation_log.py — an installed box records no conversation by default (#114).
#
# What it pins:
#   - every switch that records conversation content is off in the schema, and the
#     installer writes none of them, so a fresh install leaves them at those defaults;
#   - quoted() gives a line's words only with TEAPORT_LOG_CONVERSATIONS on;
#   - the ledger's per-turn line, the journal's transcript before this change, carries
#     no words by default;
#   - the PREMISE the pipecat redaction rests on: every log call in the pipecat modules
#     a brain process loads that interpolates conversation text is one redact() blanks. A
#     pipecat upgrade that adds or rewords one fails here instead of leaking.
#
# Run: python test_conversation_log.py   (or via pytest)
#
import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appliance  # noqa: E402
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from loguru import logger  # noqa: E402

import pipecat  # noqa: E402
from teaport_brain import config_schema, conversation_log  # noqa: E402
from teaport_brain.conversation_log import quoted  # noqa: E402
from teaport_brain.transcript_ledger import TranscriptLedger, Utterance  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

# Each records conversation content when on; the value that means off.
RECORDING = {
    "TEAPORT_LOG_CONVERSATIONS": False,
    "TEAPORT_RAW_LLM_CAPTURE": False,
    "LEDGER_TRACE": False,
    "TEAPORT_TRACE": False,
    "TEAPORT_ENDPOINT_DEBUG": False,
    "TEAPORT_AUDIO_DUMP": None,  # a directory; none = off
}

SECRET = "the caller's private words"


def check_recording_switches_default_off():
    rows = {r["name"]: r for r in config_schema.load()["settings"]}
    for name, off in RECORDING.items():
        assert name in rows, name
        assert rows[name].get("default") == off, (name, rows[name].get("default"))
    install = (REPO / "install.sh").read_text()
    for name in RECORDING:
        assert f"{name}=" not in install, f"install.sh writes {name}"


def check_quoted_is_a_size_unless_asked():
    assert not conversation_log.LOG_WORDS, "TEAPORT_LOG_CONVERSATIONS is set in this env"
    assert quoted(SECRET) == f"<{len(SECRET)} chars>"
    assert quoted(None) == "<0 chars>"
    assert quoted(["two", "words"]) == "<2 words>"
    conversation_log.LOG_WORDS = True
    try:
        assert quoted(SECRET) == repr(SECRET)
        assert quoted(["two", "words"]) == repr(["two", "words"])
    finally:
        conversation_log.LOG_WORDS = False


def _captured(fn) -> str:
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        fn()
    finally:
        logger.remove(sink)
    return "".join(lines)


def check_ledger_line_keeps_no_words():
    out = _captured(lambda: (
        TranscriptLedger._log(None, Utterance("user", SECRET, 1.0, 2.0)),
        TranscriptLedger._log(None, Utterance("assistant", SECRET + " and more", 2.0, 3.0,
                                              interrupted=True, heard_fraction=0.5,
                                              heard_text=SECRET)),
    ))
    assert "LEDGER +user" in out and "LEDGER +assistant CUT" in out, out
    assert "private" not in out, out


# Interpolated names that hold what someone said or will hear (and any *_text).
_TEXTUAL = ("text", "arguments", "transcript", "messages", "content", "accumulated",
            "remaining")


def _sample(call: ast.Call) -> str | None:
    """The message a pipecat logger call would log, SECRET where it interpolates text;
    None when it interpolates none."""
    if not call.args or not isinstance(call.args[0], ast.JoinedStr):
        return None
    parts, textual = [], False
    for v in call.args[0].values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        else:
            if (isinstance(v.value, ast.Call) and isinstance(v.value.func, ast.Name)
                    and v.value.func.id == "len"):
                parts.append("3")  # a count, not the words
                continue
            names = {n.id if isinstance(n, ast.Name) else n.attr
                     for n in ast.walk(v.value) if isinstance(n, (ast.Name, ast.Attribute))}
            if any(n.lstrip("_") in _TEXTUAL or n.endswith(("_text", "_remaining")) for n in names):
                textual = True
                parts.append(SECRET)
            else:
                parts.append("ctx-1")
    return "".join(parts) if textual else None


def check_pipecat_text_lines_are_redacted():
    # Every pipecat module a brain process loads: the server (phone, Talk) and the room.
    import teaport_brain.gateway_server  # noqa: F401
    import teaport_brain.local_audio  # noqa: F401
    mods = sorted(m for m in sys.modules if m.startswith("pipecat"))
    assert len(mods) > 50, f"only {len(mods)} pipecat modules loaded: the import broke"
    found = 0
    for name in mods:
        path = getattr(sys.modules[name], "__file__", None) or ""
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(pathlib.Path(path).read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logger"):
                continue
            msg = _sample(node)
            if msg is None:
                continue
            found += 1
            record = {"name": name, "message": msg}
            conversation_log.redact(record)
            assert SECRET not in record["message"], (
                f"{name}:{node.lineno} logs conversation text redact() does not "
                f"blank: {msg!r} -- add its shape to conversation_log._PIPECAT_TEXT")
    assert found, "no text-carrying pipecat log line found: the scan itself is broken"


def check_install_redacts_pipecat_records():
    conversation_log.install()
    try:
        scope = {"__name__": "pipecat.services.tts_service", "logger": logger}
        out = _captured(lambda: exec(
            "logger.debug(f'TTS: Generating TTS [{text}]')", scope, {"text": SECRET}))
        assert "Generating TTS [<26 chars>]" in out, out
        out = _captured(lambda: logger.info(f"teaport line {SECRET}"))
        assert SECRET in out, "the patcher must touch pipecat's lines only"
    finally:
        logger.configure(patcher=None)


if __name__ == "__main__":
    appliance.run_tests([
        check_recording_switches_default_off,
        check_quoted_is_a_size_unless_asked,
        check_ledger_line_keeps_no_words,
        check_pipecat_text_lines_are_redacted,
        check_install_redacts_pipecat_records,
    ])
