#
# A refused key or a spent account is said as what it is, and shown in status (#205).
#
# A 401, 402 or 403 from the provider fails every turn until someone changes the key or
# the account, so "could you try again?" is the wrong thing to say. LLMErrorSpeaker
# speaks a distinct account line for pipecat's AUTHENTICATION / QUOTA / AUTHORIZATION
# categories and keeps the old line for the failures that pass (429, 5xx, timeouts).
# The brain also records the run of failures (llm_error_speaker.ANSWERS), /health serves
# it, and `teaport status` / `teaport doctor` print it until a request is accepted.
#
# An account out of credit -- a 402, or the provider's words for it -- is said as that:
# "my AI account has run out of credit", with where to add it (openrouter.ai on
# OpenRouter's endpoint) and the settings phrase where it is heard.
#
# The provider responses here are synthetic, shaped like an OpenAI-compatible gateway's
# error bodies; the links and key ids in them are made up.
#
# Run: python test_llm_account_error.py   (or via pytest test_suite.py; hermetic)
#
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import httpx  # noqa: E402
import openai  # noqa: E402

from pipecat.frames.frames import ErrorFrame, TTSSpeakFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.openai.llm import OpenAILLMService  # noqa: E402
from pipecat.utils.errors import ErrorCategory  # noqa: E402

from teaport_brain import llm_error_speaker as les  # noqa: E402
from teaport_brain import services  # noqa: E402
from teaport_brain.llm_error_speaker import (  # noqa: E402
    ACCOUNT_SETTINGS_TEXT, ACCOUNT_STILL_TEXT, ACCOUNT_TEXT, CREDIT_OTHER_SETTINGS_TEXT,
    CREDIT_OTHER_TEXT, CREDIT_SETTINGS_TEXT, CREDIT_STILL_TEXT, CREDIT_TEXT, ERROR_TEXT,
    GLITCH_NO_RETRY_TEXT, AnswerHealth, LLMErrorSpeaker, failure_reason, is_out_of_credit,
    redact)

# Made-up secrets that must never surface in anything the new code says or serves.
FAKE_KEY = "sk-test-0123456789abcdefABCDEF"
FAKE_KEY_ID = "f00dfeedcafe0123456789abcdef0123"
FAKE_LINK = f"https://provider.example/settings/keys/{FAKE_KEY_ID}"
SECRETS = (FAKE_KEY, FAKE_KEY_ID, "provider.example", "://")


def _status_error(code: int, message: str):
    """What the OpenAI SDK raises for an HTTP error from the provider: the subclass for
    the status, the body's "error" object parsed into .body (openai _make_status_error)."""
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(code, request=request)
    body = {"message": message, "code": code}
    cls = {401: openai.AuthenticationError, 403: openai.PermissionDeniedError,
           429: openai.RateLimitError}.get(code, openai.APIStatusError)
    if code >= 500:
        cls = openai.InternalServerError
    return cls(f"Error code: {code} - {{'error': {body!r}}}", response=response, body=body)


SPENT_KEY_403 = lambda: _status_error(  # noqa: E731 -- the gateway's spent-key shape
    403, f"Key limit exceeded (total limit). Manage it using {FAKE_LINK}")
KEY_CASES = {
    "401 bad key": lambda: _status_error(401, f"Invalid API key {FAKE_KEY}"),
    "403 spent key": SPENT_KEY_403,
}
# The account out of credit: OpenRouter's two 402 bodies.
CREDIT_CASES = {
    "402 no credit": lambda: _status_error(402, "Insufficient credits. Add more at "
                                           f"{FAKE_LINK}"),
    "402 in flight": lambda: _status_error(
        402, "This request would exceed your available credits given your current "
             "in-flight requests"),
}
ACCOUNT_CASES = {**KEY_CASES, **CREDIT_CASES}
OPENROUTER = "https://openrouter.ai/api/v1"
PASSING_CASES = {
    "429 rate limit": lambda: _status_error(429, "Rate limit exceeded, slow down."),
    "500 server": lambda: _status_error(500, "Internal error"),
    "503 overloaded": lambda: _status_error(503, "Upstream overloaded"),
    "read timeout": lambda: httpx.ReadTimeout("timed out"),
    "sdk timeout": lambda: openai.APITimeoutError(
        request=httpx.Request("POST", "https://provider.example/v1/chat/completions")),
}


def _llm():
    llm = services.make_llm()
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))
    llm.push_frame = capture
    return llm, pushed


async def _raise_through(llm, pushed, exc) -> ErrorFrame:
    """The error the LLM service pushes for `exc`, as base_llm reports a failed
    completion: push_error -> push_error_frame (where pipecat settles the category and
    BoundedOpenAILLMService records it)."""
    pushed.clear()
    await llm.push_error(error_msg=f"Error during completion: {exc}", exception=exc)
    errors = [f for f, d in pushed if isinstance(f, ErrorFrame)]
    assert len(errors) == 1 and pushed[0][1] == FrameDirection.UPSTREAM, pushed
    return errors[0]


async def _spoken(speaker, frame) -> list[str]:
    said = []

    async def capture(f, direction=FrameDirection.DOWNSTREAM):
        if isinstance(f, TTSSpeakFrame):
            said.append(f.text)
    speaker.push_frame = capture
    await speaker.process_frame(frame, FrameDirection.UPSTREAM)
    return said


def _no_secrets(text: str, where: str):
    for s in SECRETS:
        assert s not in text, f"{where} leaks {s!r}: {text!r}"


async def test_account_failures_speak_the_account_line():
    """401 and 403 (the spent-key body included): the account line, once. A 402: that the
    account is out of credit (on this endpoint, not OpenRouter's: no site named)."""
    services.ANSWERS = AnswerHealth()
    llm, pushed = _llm()
    for name, make in ACCOUNT_CASES.items():
        frame = await _raise_through(llm, pushed, make())
        assert frame.category in les.ACCOUNT_CATEGORIES, (name, frame.category)
        said = await _spoken(LLMErrorSpeaker(), frame)
        want = CREDIT_OTHER_TEXT if name in CREDIT_CASES else ACCOUNT_TEXT
        assert said == [want], (name, said)


class _Endpoint:
    """LLM_BASE_URL for the lines read while it lasts (setting() reads it per call)."""

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        self.was = os.environ.get("LLM_BASE_URL")
        os.environ["LLM_BASE_URL"] = self.url

    def __exit__(self, *exc):
        if self.was is None:
            os.environ.pop("LLM_BASE_URL", None)
        else:
            os.environ["LLM_BASE_URL"] = self.was


async def test_out_of_credit_says_so_and_where_credit_is_added():
    """Each 402 body: "run out of credit". On OpenRouter's endpoint the line names
    openrouter.ai; where "open my settings" is heard it names the phrase too; never
    "needs attention", which sent the owner looking at the key."""
    llm, pushed = _llm()
    for name, make in CREDIT_CASES.items():
        frame = await _raise_through(llm, pushed, make())
        assert frame.category is ErrorCategory.QUOTA and is_out_of_credit(frame), name
        for url, settings, want in ((OPENROUTER, True, CREDIT_SETTINGS_TEXT),
                                    (OPENROUTER, False, CREDIT_TEXT),
                                    ("http://127.0.0.1:9/v1", True, CREDIT_OTHER_SETTINGS_TEXT),
                                    ("http://127.0.0.1:9/v1", False, CREDIT_OTHER_TEXT)):
            with _Endpoint(url):
                said = await _spoken(LLMErrorSpeaker(settings=settings), frame)
            assert said == [want], (name, url, settings, said)
    for text in (CREDIT_SETTINGS_TEXT, CREDIT_TEXT, CREDIT_OTHER_SETTINGS_TEXT,
                 CREDIT_OTHER_TEXT, CREDIT_STILL_TEXT):
        assert "out of credit" in text and "attention" not in text, text
    assert "openrouter.ai" in CREDIT_SETTINGS_TEXT and "openrouter.ai" in CREDIT_TEXT
    assert "openrouter" not in (CREDIT_OTHER_SETTINGS_TEXT + CREDIT_OTHER_TEXT).lower()
    for text in (CREDIT_SETTINGS_TEXT, CREDIT_OTHER_SETTINGS_TEXT):
        assert '"open my settings"' in text, text
    for text in (CREDIT_TEXT, CREDIT_OTHER_TEXT, CREDIT_STILL_TEXT):
        assert "settings" not in text, text      # a phone call does not open them


async def test_out_of_credit_in_the_account_window_is_its_short_line():
    """The account window, as for a refused key: the full line once, then the short one
    of the failure's own kind -- out of credit, or a key to fix."""
    llm, pushed = _llm()
    speaker = LLMErrorSpeaker()
    credit = await _raise_through(llm, pushed, CREDIT_CASES["402 in flight"]())
    key = await _raise_through(llm, pushed, SPENT_KEY_403())
    assert await _spoken(speaker, credit) == [CREDIT_OTHER_TEXT]
    assert await _spoken(speaker, credit) == [CREDIT_STILL_TEXT]
    assert await _spoken(speaker, key) == [ACCOUNT_STILL_TEXT]


async def test_the_providers_credit_words_count_without_a_402():
    """An error the stream itself carried has no HTTP status (pipecat's UNKNOWN): its body's
    code 402, or the provider's words, make it out of credit. A free tier's rate limit
    that suggests buying credit, a key's own limit, and an outage that names none, do
    not."""
    llm, pushed = _llm()
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    in_stream = openai.APIError("Insufficient credits", request,
                                body={"message": "Insufficient credits", "code": 402})
    words_only = openai.APIError("x", request, body={
        "message": "This request would exceed your available credits given your current "
                   "in-flight requests"})
    for exc in (in_stream, words_only):
        frame = await _raise_through(llm, pushed, exc)
        assert frame.category is ErrorCategory.UNKNOWN, frame.category
        assert is_out_of_credit(frame) and les.is_account_problem(frame), exc
        assert await _spoken(LLMErrorSpeaker(settings=True), frame) == [CREDIT_OTHER_SETTINGS_TEXT]
    free_tier = _status_error(429, "Rate limit exceeded: free-models-per-day. Add 10 "
                                   "credits to unlock 1000 free model requests per day")
    outage = openai.APIError("x", request, body={"message": "Upstream error", "code": 502})
    for exc, want in ((free_tier, ERROR_TEXT), (outage, ERROR_TEXT),
                      (SPENT_KEY_403(), ACCOUNT_TEXT)):
        frame = await _raise_through(llm, pushed, exc)
        assert not is_out_of_credit(frame), exc
        assert await _spoken(LLMErrorSpeaker(), frame) == [want], exc
    # Words alone, on an exception no provider request raises: not the provider's.
    bug = ErrorFrame(error="x", processor=llm, exception=ValueError("insufficient credits"))
    assert not is_out_of_credit(bug)


async def test_where_the_phrase_is_heard_the_account_line_names_it():
    """The room and Talk (settings: "open my settings" is usable there): the account line
    says the phrase that fixes it. A phone call keeps the neutral line."""
    from teaport_brain.llm_error_speaker import ACCOUNT_SETTINGS_TEXT
    llm, _ = _llm()
    frame = ErrorFrame(error="x", processor=llm, category=ErrorCategory.AUTHENTICATION)
    assert await _spoken(LLMErrorSpeaker(settings=True), frame) == [ACCOUNT_SETTINGS_TEXT]
    assert await _spoken(LLMErrorSpeaker(settings=False), frame) == [ACCOUNT_TEXT]
    assert '"open my settings"' in ACCOUNT_SETTINGS_TEXT


async def test_passing_failures_keep_todays_line():
    services.ANSWERS = AnswerHealth()
    llm, pushed = _llm()
    for name, make in PASSING_CASES.items():
        frame = await _raise_through(llm, pushed, make())
        assert frame.category not in les.ACCOUNT_CATEGORIES, (name, frame.category)
        said = await _spoken(LLMErrorSpeaker(), frame)
        assert said == [ERROR_TEXT], (name, said)


async def test_an_unclassified_frame_falls_back_to_its_exception():
    """An ErrorFrame that never went through push_error_frame has no category yet."""
    llm, _ = _llm()
    frame = ErrorFrame(error="x", exception=SPENT_KEY_403(), processor=llm)
    assert frame.category is None
    assert await _spoken(LLMErrorSpeaker(), frame) == [ACCOUNT_TEXT]
    frame = ErrorFrame(error="x", processor=llm)
    assert await _spoken(LLMErrorSpeaker(), frame) == [ERROR_TEXT]


async def test_the_account_window_full_line_then_short_never_try_again():
    """The full account line once per account window; every other failed account turn
    inside it gets the short line, never silence; a passing failure inside it gets the
    vague line without "try again" (it would contradict "trying again won't help"), on
    the vague line's own clock -- not silence: the account may have been topped up.
    A passing glitch said a moment earlier does not swallow the account line."""
    llm, _ = _llm()
    speaker = LLMErrorSpeaker()
    account = ErrorFrame(error="x", processor=llm, category=ErrorCategory.AUTHORIZATION)
    glitch = ErrorFrame(error="x", processor=llm, category=ErrorCategory.SERVER)
    assert await _spoken(speaker, glitch) == [ERROR_TEXT]
    assert await _spoken(speaker, account) == [ACCOUNT_TEXT]
    assert await _spoken(speaker, account) == [ACCOUNT_STILL_TEXT]
    assert await _spoken(speaker, account) == [ACCOUNT_STILL_TEXT]
    window = les._DEBOUNCE_SECS
    # Past the ordinary window, short of the account one.
    speaker._said -= window + 1
    speaker._said_account -= window + 1
    assert await _spoken(speaker, glitch) == [GLITCH_NO_RETRY_TEXT]
    assert await _spoken(speaker, glitch) == []     # still the vague line's 30 s clock
    assert await _spoken(speaker, account) == [ACCOUNT_STILL_TEXT]
    # Past the account window: the full line again, and the vague line is back.
    speaker._said_account -= window * les.ACCOUNT_DEBOUNCE_FACTOR
    speaker._said -= window * les.ACCOUNT_DEBOUNCE_FACTOR
    assert await _spoken(speaker, glitch) == [ERROR_TEXT]
    assert await _spoken(speaker, account) == [ACCOUNT_TEXT]


async def test_the_notices_follow_the_voice_language():
    """Said in the session's voice language (i18n catalogs), read at each notice."""
    from teaport_brain import i18n
    llm, _ = _llm()
    lang = ["es"]
    speaker = LLMErrorSpeaker(lang_fn=lambda: lang[0])
    account = ErrorFrame(error="x", processor=llm, category=ErrorCategory.AUTHORIZATION)
    es = i18n.for_espeak("es")
    full = await _spoken(speaker, account)
    assert full == [es._(ACCOUNT_TEXT)] and full != [ACCOUNT_TEXT], full
    lang[0] = "fr-fr"
    short = await _spoken(speaker, account)
    assert short == [i18n.for_espeak("fr-fr")._(ACCOUNT_STILL_TEXT)], short
    assert short != [ACCOUNT_STILL_TEXT]


async def test_other_processors_errors_are_not_spoken():
    from pipecat.processors.frame_processor import FrameProcessor
    frame = ErrorFrame(error="x", processor=FrameProcessor(),
                       category=ErrorCategory.AUTHENTICATION)
    assert await _spoken(LLMErrorSpeaker(), frame) == []


def test_spoken_lines_are_plain_words():
    assert "try again" not in GLITCH_NO_RETRY_TEXT
    for text in (ACCOUNT_TEXT, ACCOUNT_STILL_TEXT, ERROR_TEXT, GLITCH_NO_RETRY_TEXT,
                 CREDIT_TEXT, CREDIT_SETTINGS_TEXT, CREDIT_OTHER_TEXT,
                 CREDIT_OTHER_SETTINGS_TEXT, CREDIT_STILL_TEXT):
        assert not re.search(r"\b(LLM|API|language model|speech engine|\d{3})\b", text,
                             re.IGNORECASE), text


def test_the_reason_is_the_providers_words_without_links_or_keys():
    assert failure_reason(SPENT_KEY_403()) == "Key limit exceeded (total limit)"
    assert failure_reason(ACCOUNT_CASES["401 bad key"]()) == "Invalid API key"
    assert failure_reason(ACCOUNT_CASES["402 no credit"]()) == "Insufficient credits"
    assert failure_reason(httpx.ReadTimeout("")) == "ReadTimeout"
    assert failure_reason(None) == ""
    # No body: the exception's text, its status prefix and link gone.
    bare = RuntimeError(f"Error code: 403 - refused. See {FAKE_LINK} for {FAKE_KEY}")
    assert failure_reason(bare) == "refused", failure_reason(bare)
    for make in (*ACCOUNT_CASES.values(), *PASSING_CASES.values()):
        _no_secrets(failure_reason(make()), "failure_reason")
    # A key the provider masked keeps no part of itself, not even its visible tail.
    masked = _status_error(401, "Incorrect key provided: sk-proj-****\u2026abcd. Check it.")
    assert failure_reason(masked) == "Incorrect key provided: Check it", failure_reason(masked)
    assert redact("key sk-proj-****\u2026abcd") == "key <key>"
    assert redact("tail ****abcd only") == "tail <key> only"


async def test_the_error_log_line_is_redacted():
    """pipecat logs ErrorFrame.error itself; it is redacted before pipecat sees it, while
    the exception stays whole for the category."""
    services.ANSWERS = AnswerHealth()
    llm, pushed = _llm()
    frame = await _raise_through(llm, pushed, SPENT_KEY_403())
    _no_secrets(frame.error, "the error log line")
    assert "<link>" in frame.error and frame.category is ErrorCategory.AUTHORIZATION


async def test_only_provider_failures_are_recorded():
    """An exception no provider request raises is the brain's own bug: not blamed on the
    provider in status."""
    health = services.ANSWERS = AnswerHealth()
    llm, pushed = _llm()
    await _raise_through(llm, pushed, ValueError("bad context"))
    await _raise_through(llm, pushed, KeyError("x"))
    assert health.status() == {"state": "untried"}, health.status()
    await _raise_through(llm, pushed, PASSING_CASES["read timeout"]())
    assert health.status()["state"] == "failing", health.status()
    await _raise_through(llm, pushed, asyncio.TimeoutError())
    assert health.status()["count"] == 2, health.status()


async def test_status_records_the_failure_and_clears_on_success():
    """Acceptance: /health's "answers" shows the account failure after one such turn,
    and stops showing it once the provider accepts a request (_open_stream)."""
    clock = [1000.0]
    health = services.ANSWERS = AnswerHealth(clock=lambda: clock[0])
    assert health.status() == {"state": "untried"}
    llm, pushed = _llm()
    await _raise_through(llm, pushed, SPENT_KEY_403())
    st = health.status()
    assert st["state"] == "account" and st["category"] == "authorization", st
    assert st["since"] == 1000.0 and st["count"] == 1, st
    assert st["reason"] == "Key limit exceeded (total limit)", st
    _no_secrets(json.dumps(st), "/health answers")
    clock[0] = 1060.0
    await _raise_through(llm, pushed, SPENT_KEY_403())
    st = health.status()
    assert st["since"] == 1000.0 and st["last"] == 1060.0 and st["count"] == 2, st
    assert "credit" not in st and not health.out_of_credit(), st     # a key to fix
    await _raise_through(llm, pushed, CREDIT_CASES["402 in flight"]())
    st = health.status()
    assert st["state"] == "account" and st["credit"] is True and health.out_of_credit(), st
    assert st["since"] == 1000.0 and st["count"] == 3, st              # the same run

    # A request the provider accepts: the stream opens.
    async def accepted(self, context):
        return iter(())
    original = OpenAILLMService.get_chat_completions
    OpenAILLMService.get_chat_completions = accepted
    try:
        await llm._open_stream(object())
    finally:
        OpenAILLMService.get_chat_completions = original
    assert health.status() == {"state": "ok"}, health.status()
    assert not health.out_of_credit()

    await _raise_through(llm, pushed, PASSING_CASES["500 server"]())
    assert health.status()["state"] == "failing", health.status()


async def test_a_tool_failure_is_not_a_provider_failure():
    health = services.ANSWERS = AnswerHealth()
    llm, pushed = _llm()
    pushed.clear()
    await llm.push_error(error_msg="handler raised", exception=ValueError("boom"),
                         category=ErrorCategory.APPLICATION)
    assert health.status() == {"state": "untried"}, health.status()


# ---- the CLI ---------------------------------------------------------------------

_CLI = os.path.join(HERE, "..", "..", "cli", "teaport")
_MARK = {"✓": "OK", "!": "WARN", "✗": "FAIL"}


def _cli(cmd: str, health, brain_env: str | None = None) -> str:
    """`teaport <cmd>` with the brain's /health stubbed to `health` (None: no answer),
    nothing installed and no unit or port probed, as test_tts_pacing's _doctor_run."""
    from tempdirs import tempdir
    text = open(_CLI, encoding="utf-8").read()
    stub = (f"brain_health() {{ printf '%s' {shlex.quote(json.dumps(health))}; }}"
            if health is not None else "brain_health() { return 7; }")
    script = (text[:text.index('cmd="${1:-}"; shift || true')] + stub + "\n"
              "active() { echo inactive; }; installed() { return 1; }\n"
              "sip_enabled() { return 1; }; listening() { return 1; }\n"
              f"{cmd}\n")
    home = tempdir("teaport-answers-")
    env = dict(os.environ, HOME=home, TEAPORT_SECRETS_DIR=home,
               TEAPORT_PREFIX=os.path.join(home, "opt"), TEAPORT_ETC=os.path.join(home, "etc"))
    if brain_env is not None:  # the box's brain.env, for what the CLI reads from it
        os.makedirs(os.path.join(home, "etc"), exist_ok=True)
        with open(os.path.join(home, "etc", "brain.env"), "w") as f:
            f.write(brain_env)
    env.pop("LLM_API_KEY", None)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert not p.stderr, p.stderr
    return re.sub(r"\x1b\[[\d;]+m", "", p.stdout)


def _status_answers(health) -> str:
    lines = [ln for ln in _cli("cmd_status", health).splitlines() if "answers (brain)" in ln]
    assert len(lines) == 1, lines
    return lines[0].split("answers (brain)", 1)[1].strip()


def _doctor_answers(health) -> list[str]:
    out = _cli("cmd_doctor", health)
    return [f"{_MARK[m[1]]} {m[2]}" for m in re.finditer(r"^  (.) (answers: .*)$", out, re.M)]


def test_status_and_doctor_show_an_account_failure_until_a_success():
    """Acceptance: the record a refused turn leaves, through the CLI; and the record a
    success leaves. The reason shown is the brain's, already scrubbed."""
    health = AnswerHealth(clock=time.time)
    health.failed(ErrorFrame(error="x", exception=SPENT_KEY_403(),
                             category=ErrorCategory.AUTHORIZATION))
    served = {"ok": True, "answers": health.status()}
    shown = _status_answers(served)
    hhmm = time.strftime("%H:%M", time.localtime(health.status()["since"]))
    assert shown.startswith(f"refused since {hhmm}, last at {hhmm}, 1 time(s) "
                            "(Key limit exceeded (total limit))"), shown
    assert "settings page" in shown and "clears on the next answered request" in shown, shown
    doctor = _doctor_answers(served)
    assert len(doctor) == 1 and doctor[0].startswith("FAIL answers: refused since"), doctor
    for text in (shown, *doctor):
        _no_secrets(text, "the CLI")
        assert not re.search(r"\b(LLM|API|40[123])\b", text), text

    health.succeeded()
    served = {"ok": True, "answers": health.status()}
    assert _status_answers(served) == "working"
    assert _doctor_answers(served) == ["OK answers: working"]


def test_status_and_doctor_say_out_of_credit():
    """An account out of credit, through the CLI: the cause and where credit is added."""
    health = AnswerHealth(clock=time.time)
    health.failed(ErrorFrame(error="x", exception=CREDIT_CASES["402 no credit"](),
                             category=ErrorCategory.QUOTA))
    served = {"ok": True, "answers": health.status()}
    shown = _status_answers(served)
    assert shown.startswith("out of credit since "), shown
    assert "openrouter.ai/settings/credits" in shown and "(Insufficient credits)" in shown, shown
    doctor = _doctor_answers(served)
    assert len(doctor) == 1 and doctor[0].startswith("FAIL answers: out of credit since"), doctor
    for text in (shown, *doctor):
        _no_secrets(text, "the CLI")


def test_status_and_doctor_for_passing_failures_and_older_brains():
    health = AnswerHealth(clock=time.time)
    health.failed(ErrorFrame(error="x", exception=PASSING_CASES["503 overloaded"](),
                             category=ErrorCategory.SERVER))
    served = {"ok": True, "answers": health.status()}
    assert _status_answers(served).startswith("failing since "), _status_answers(served)
    doctor = _doctor_answers(served)
    assert len(doctor) == 1 and doctor[0].startswith("WARN answers: failing since"), doctor
    assert _status_answers({"ok": True, "answers": {"state": "untried"}}).startswith("none")
    # A brain that does not answer, or predates the field: status says so, doctor is quiet.
    for served in (None, {"ok": True}):
        assert _status_answers(served).startswith("unknown"), _status_answers(served)
        assert _doctor_answers(served) == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
