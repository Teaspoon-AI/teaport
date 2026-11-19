#
# The AI disclosure (ai_disclosure.py): every phone call is told it is talking to an AI,
# and nothing the operator can set turns that off; the room may add a greeting; every
# session's model is told it is an AI and says so if asked.
#
#   - a phone call's notice is the disclosure, then the recording notice when there is
#     one, in the call's language
#   - every call is told, resumed or not, recorded or not, recording notice on or off
#   - a call whose disclosure does not play out is hung up, not greeted
#   - no setting in the schema reads like a disclosure switch except the room greeting,
#     which can only turn one ON, and ai_disclosure reads no other setting
#   - the room greeting: off by default, on only for the room, joined with the trial
#     credit's line when both are due
#   - every session's starting context carries the "you are an AI" line, right after the
#     identity
#
import asyncio
import os
import pathlib
import sys
import tempfile
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from teaport_brain import ai_disclosure, call_recording, credit  # noqa: E402
from teaport_brain.agent_session import StartNotice, _build_initial_messages  # noqa: E402

import test_sip_call_lifecycle as lifecycle  # noqa: E402

LINE = "You're speaking with an AI assistant."
NOTICE = "This call is being recorded."
SCHEMA = pathlib.Path(ai_disclosure.__file__).with_name("config_schema.toml")


# ---------------------------------------------------------------- the line

def test_the_call_notice_is_the_disclosure_then_the_recording_notice():
    assert ai_disclosure.call_notice(None) == LINE
    assert ai_disclosure.call_notice("en-us") == LINE
    assert ai_disclosure.call_notice("en-us", recorded=True) == f"{LINE} {NOTICE}"


def test_the_call_notice_is_in_the_calls_language():
    assert ai_disclosure.call_notice("es") == "Está hablando con un asistente de IA."
    assert ai_disclosure.call_notice("es", recorded=True) == (
        "Está hablando con un asistente de IA. Esta llamada está siendo grabada.")
    assert ai_disclosure.call_notice("cmn") == "您正在与AI助手通话。"
    assert ai_disclosure.room_line("fr-fr") == "Je suis un assistant IA."


# ---------------------------------------------------------------- no off switch

def test_no_setting_turns_the_phone_disclosure_off():
    schema = tomllib.loads(SCHEMA.read_text(encoding="utf-8"))
    rows = [r for r in schema["settings"]
            if r.get("group") == "ai_disclosure"
            or any(w in r["name"] for w in ("DISCLOS", "AI_GREETING", "AI_NOTICE"))]
    assert [r["name"] for r in rows] == ["TEAPORT_ROOM_AI_GREETING"], (
        "the only AI-disclosure setting is the room greeting, which turns one ON; a "
        "setting that turns the phone disclosure off is what Article 50 forbids: "
        f"{[r['name'] for r in rows]}")
    assert rows[0]["default"] is False and rows[0]["type"] == "flag"
    # The module reads that one setting and no other.
    src = pathlib.Path(ai_disclosure.__file__).read_text(encoding="utf-8")
    assert src.count("setting(") == 1 and 'setting("TEAPORT_ROOM_AI_GREETING")' in src


async def test_every_call_is_told_resumed_recorded_or_not():
    for mode, notice_on, want in (("off", True, LINE),
                                  ("both", True, f"{LINE} {NOTICE}"),
                                  ("both", False, LINE)):
        with tempfile.TemporaryDirectory() as d:
            saved = {k: getattr(call_recording, k) for k in ("MODE", "ENABLED", "NOTICE", "DIR")}
            call_recording.MODE, call_recording.ENABLED = mode, mode != "off"
            call_recording.NOTICE, call_recording.DIR = notice_on, d
            h = lifecycle._Harness()
            try:
                await h.start()
                await h.call_state("A", "confirmed")
                assert await lifecycle.wait_until(lambda: "A" in lifecycle._FakeSession.greeted)
                assert lifecycle._FakeSession.notices["A"] == want, (mode, notice_on)
                # The brain restarted under a call: told again.
                await h.send_raw({"type": "call.incoming", "call_id": "B",
                                  "from": "sip:caller@example.invalid",
                                  "to": "sip:svc@example.invalid", "replay": True})
                await h.call_state("B", "confirmed", replay=True)
                assert await lifecycle.wait_until(lambda: "B" in lifecycle._FakeSession.greeted)
                assert lifecycle._FakeSession.notices["B"] == want, (mode, notice_on)
            finally:
                await h.stop()
                for k, v in saved.items():
                    setattr(call_recording, k, v)


async def test_a_call_not_told_is_hung_up():
    h = lifecycle._Harness()
    try:
        await h.start()
        lifecycle._FakeSession.untold = {"A"}
        await h.call_state("A", "confirmed")
        assert await lifecycle.wait_until(
            lambda: any(m.get("type") == "call.hangup" and m.get("call_id") == "A"
                        for m in h.controls)), (
            "the caller was not told they are talking to an AI, and the call went on")
        assert await lifecycle.wait_until(lambda: len(h.reclaims) == 1), (
            "the untold call was never torn down")
    finally:
        await h.stop()


# ---------------------------------------------------------------- the room greeting

class _Credit:
    """credit.MONITOR's notice, for one test."""

    def __init__(self, msgid):
        self.msgid, self.told_ = msgid, []

    def notice(self, settings=True):
        return self.msgid

    def told(self, msgid):
        self.told_.append(msgid)


def _start_notice(ai_greeting, msgid=None):
    monitor = _Credit(msgid)
    saved, credit.MONITOR = credit.MONITOR, monitor
    try:
        n = StartNotice(lambda: "en-us", None, None, ai_greeting=ai_greeting)
        line = n.line()
        n.told()
        return line, monitor.told_
    finally:
        credit.MONITOR = saved


def test_the_room_greeting_is_off_by_default():
    assert ai_disclosure.ROOM_GREETING is False


def test_the_room_greeting_and_the_credit_line():
    assert _start_notice(False) == (None, [])
    assert _start_notice(True) == ("I'm an AI assistant.", [])
    line, told = _start_notice(True, credit.HALF_TEXT)
    assert line.startswith("I'm an AI assistant. ") and len(line) > len("I'm an AI assistant. ")
    assert told == [credit.HALF_TEXT], "the credit line is recorded as told"
    line, told = _start_notice(False, credit.HALF_TEXT)
    assert not line.startswith("I'm an AI") and told == [credit.HALF_TEXT]


def test_the_room_greeting_is_the_rooms_only():
    src = pathlib.Path(sys.modules[StartNotice.__module__].__file__).read_text(encoding="utf-8")
    assert 'ai_greeting=ai_disclosure.ROOM_GREETING and front_end == "room"' in src


# ---------------------------------------------------------------- truthful answers

def test_every_session_is_told_it_is_an_ai_right_after_its_identity():
    for kw in ({}, {"context_notes": True}, {"recorded": "This call is recorded."}):
        for lang in (None, "Spanish"):
            msgs = _build_initial_messages("You are Sam, the owner's helper.", lang, **kw)
            assert msgs[0]["content"].startswith("You are Sam")
            assert msgs[1] == {"role": "system", "content": ai_disclosure.PROMPT_LINE}, msgs
    assert "not a person" in ai_disclosure.PROMPT_LINE
    assert "AI assistant" in ai_disclosure.PROMPT_LINE


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
