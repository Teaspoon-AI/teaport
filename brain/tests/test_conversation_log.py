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
import io
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
    "TEAPORT_CALL_RECORDING": "off",
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


# Interpolated names that may hold what someone said or will hear: the text itself, a
# word, a frame (a TextFrame's repr holds its text), a context or its messages. Broad on
# purpose: a line flagged here is either redacted or listed below as reviewed.
_TEXTUAL = ("text", "arguments", "transcript", "messages", "content", "accumulated",
            "remaining", "word", "words", "context", "frame", "get_messages_for_logging")
_TEXTUAL_SUFFIXES = ("_text", "_remaining", "_word", "_frame", "_context")

# Flagged lines that carry no words, reviewed (pipecat 1.12), by module and shape (the
# line's literal text, {} for each interpolation): a reworded line is reviewed again.
_REVIEWED = {
    # The pipeline worker names its own control frames (Start, End, Cancel, Stop, the
    # interruption worker frames) and errors.
    ("pipecat.pipeline.worker", s) for s in (
        "{}: Starting. Waiting for {} to reach the end of the pipeline...",
        "{}: {} reached the end of the pipeline, pipeline is now ready.",
        "{}: Closing. Waiting for {} to reach the end of the pipeline...",
        "{}: {} reached the end of the pipeline, pipeline is closing.",
        "{}: {} reached the end of the pipeline.",
        "{}: timeout waiting for {} to reach the end of the pipeline (being blocked somewhere?), stopping the pipeline.",
        "{}: timeout waiting for {} to reach the end of the pipeline (being blocked somewhere?).",
        "{}: received end worker frame upstream {}",
        "{}: received cancel worker frame upstream {}",
        "{}: received stop worker frame upstream {}",
        "{}: received interruption worker frame upstream {}",
        "{}: received end worker frame downstream {}",
        "{}: received stop worker frame downstream {}",
        "{}: received cancel worker frame downstream {}",
        "{}: received interruption worker frame downstream {}",
        "A fatal error occurred: {}",
        "{}: Something went wrong: {}",
    )
} | {
    # Request and tool-call ids, image sizes, counts, a service's name, settings.
    ("pipecat.processors.aggregators.llm_context_summarizer", "{}: Received summary result (request_id={})"),
    ("pipecat.processors.aggregators.llm_context_summarizer", "{}: Ignoring stale summary result (request_id={})"),
    ("pipecat.processors.aggregators.llm_context_summarizer", "{}: Context summarization failed: {}"),
    ("pipecat.processors.aggregators.llm_response_universal", "{} FunctionCallInProgressFrame: [{}:{}]"),
    ("pipecat.processors.aggregators.llm_response_universal", "{} FunctionCallResultFrame: [{}:{}]"),
    ("pipecat.processors.aggregators.llm_response_universal", "{} FunctionCallCancelFrame: [{}:{}]"),
    ("pipecat.processors.aggregators.llm_response_universal", "{} Appending AssistantImageRawFrame to LLM context (size: {})"),
    ("pipecat.processors.aggregators.llm_response_universal", "{} Appending UserImageRawFrame to LLM context (size: {})"),
    ("pipecat.processors.aggregators.llm_response_universal", "FunctionCallResultFrame tool_call_id [{}] is not running"),
    ("pipecat.processors.aggregators.llm_response_universal", "FunctionCallResultFrame tool_call_id [{}] is not in progress"),
    ("pipecat.processors.aggregators.llm_response_universal", "{}: detected realtime service `{}`; auto-enabled realtime_service_mode."),
    ("pipecat.processors.frameworks.rtvi.observer", "{}: function_call_report_level set to {}"),
    ("pipecat.processors.frameworks.rtvi.observer", "{}: vad_user_speaking_enabled set to {}"),
    ("pipecat.processors.frameworks.rtvi.observer", "{}: bot_llm_marker_enabled set to {}"),
    ("pipecat.services.llm_service", "{}: Processing summarization request {}"),
    ("pipecat.services.llm_service", "{}: Generating summary for {} messages (index 0 to {}), target_context_tokens={}"),
    ("pipecat.services.stt_service", "Empty audio frame received for STT service: {} {}"),
    ("pipecat.services.stt_service", "STT service {}"),
    ("pipecat.services.tts_service", "{} cleaning up TTS context {}"),
    # Start, End and Cancel frames; audio frames; an error's processor and message.
    ("pipecat.services.ai_service", "{}: exception processing {}: {}"),
    ("pipecat.transports.base_output", "{} destination [{}] not registered for frame {}"),
    ("pipecat.transports.base_output", "{} Error writing {} to transport: {}"),
    ("pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy",
     "{}: releasing the user mute without the bot having completed its first speech, after an error from {}: {}"),
}


def _sample(call: ast.Call) -> tuple[str, str] | None:
    """(the message a pipecat logger call would log, SECRET where it may interpolate
    words; its shape), or None when it interpolates nothing that may hold words."""
    if not call.args or not isinstance(call.args[0], ast.JoinedStr):
        return None
    parts, shape, textual = [], [], False
    for v in call.args[0].values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
            shape.append(str(v.value))
            continue
        shape.append("{}")
        if (isinstance(v.value, ast.Call) and isinstance(v.value.func, ast.Name)
                and v.value.func.id == "len"):
            parts.append("3")  # a count, not the words
            continue
        names = {n.id if isinstance(n, ast.Name) else n.attr
                 for n in ast.walk(v.value) if isinstance(n, (ast.Name, ast.Attribute))}
        if any(n.lstrip("_") in _TEXTUAL or n.endswith(_TEXTUAL_SUFFIXES) for n in names):
            textual = True
            parts.append(SECRET)
        else:
            parts.append("ctx-1")
    return ("".join(parts), "".join(shape)) if textual else None


def check_pipecat_text_lines_are_redacted():
    # Every pipecat module a brain process loads: the server (phone, Talk) and the room.
    import teaport_brain.gateway_server  # noqa: F401
    import teaport_brain.local_audio  # noqa: F401
    mods = sorted(m for m in sys.modules if m.startswith("pipecat"))
    assert len(mods) > 50, f"only {len(mods)} pipecat modules loaded: the import broke"
    redacted, reviewed = 0, set()
    for name in mods:
        path = getattr(sys.modules[name], "__file__", None) or ""
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(pathlib.Path(path).read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logger"):
                continue
            if node.func.attr == "trace":
                continue  # below every sink's level (install())
            sample = _sample(node)
            if sample is None:
                continue
            msg, shape = sample
            if (name, shape) in _REVIEWED:
                reviewed.add((name, shape))
                continue
            record = {"name": name, "message": msg}
            conversation_log.redact(record)
            assert SECRET not in record["message"], (
                f"{name}:{node.lineno} may log conversation text that redact() does not "
                f"blank: {shape!r} -- add its shape to conversation_log._PIPECAT_TEXT, "
                "or, once read and found to carry no words, to _REVIEWED")
            redacted += 1
    assert redacted >= 10, f"only {redacted} redacted lines found: the scan itself is broken"
    stale = _REVIEWED - reviewed
    assert not stale, f"reviewed lines pipecat no longer has (prune them): {sorted(stale)}"


def check_install_redacts_pipecat_records():
    conversation_log.install()
    try:
        scope = {"__name__": "pipecat.services.tts_service", "logger": logger}
        out = _captured(lambda: exec(
            "logger.debug(f'TTS: Generating TTS [{text}]')", scope, {"text": SECRET}))
        assert "Generating TTS [<26 chars>]" in out, out
        out = _captured(lambda: exec(
            "logger.debug(f'LLM: Generating chat from context {m}')", scope,
            {"m": [{"role": "user", "content": SECRET}]}))
        assert "Generating chat from context <" in out and "private" not in out, out
        out = _captured(lambda: logger.info(f"teaport line {SECRET}"))
        assert SECRET in out, "the patcher must touch pipecat's lines only"

    finally:
        logger.configure(patcher=lambda record: None)
        logger.remove()
        logger.add(sys.stderr)


def _fails(said):
    raise ValueError("boom")


def check_a_traceback_shows_no_locals():
    """loguru's default sink prints every frame's local variables in a traceback
    (diagnose): a frame holding a caller's words would put them in the journal."""
    buf, real = io.StringIO(), sys.stderr
    sys.stderr = buf          # install()'s sink writes to sys.stderr as it finds it
    try:
        conversation_log.install()
        try:
            _fails(SECRET)
        except ValueError:
            logger.exception("relay session error")
    finally:
        sys.stderr = real
        logger.configure(patcher=lambda record: None)
        logger.remove()
        logger.add(sys.stderr)
    out = buf.getvalue()
    assert "relay session error" in out and "boom" in out, out
    assert "private" not in out, out


def check_trace_stays_off_without_words():
    """TRACE lines are not redacted (the scan reads DEBUG and up), so LOGURU_LEVEL=TRACE
    must not bring them, with their transcripts, into the journal."""
    buf, real = io.StringIO(), sys.stderr
    old = os.environ.get("LOGURU_LEVEL")
    os.environ["LOGURU_LEVEL"] = "TRACE"
    sys.stderr = buf
    try:
        conversation_log.install()
        scope = {"__name__": "pipecat.turns.eager_end_of_turn_mixin", "logger": logger}
        exec("logger.trace(f'eager end of turn: [{t}]')", scope, {"t": SECRET})
        logger.debug("debug still shows")
    finally:
        sys.stderr = real
        if old is None:
            os.environ.pop("LOGURU_LEVEL", None)
        else:
            os.environ["LOGURU_LEVEL"] = old
        logger.configure(patcher=lambda record: None)
        logger.remove()
        logger.add(sys.stderr)
    out = buf.getvalue()
    assert "debug still shows" in out and "private" not in out, out


if __name__ == "__main__":
    appliance.run_tests([
        check_recording_switches_default_off,
        check_quoted_is_a_size_unless_asked,
        check_ledger_line_keeps_no_words,
        check_pipecat_text_lines_are_redacted,
        check_install_redacts_pipecat_records,
        check_a_traceback_shows_no_locals,
        check_trace_stays_off_without_words,
    ])
