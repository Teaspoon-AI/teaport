#
# Sliced turn text end to end (stt.py TURN TEXT, TEAPORT_STT_TURN_TEXT=slice): the REAL
# brain (teaport-brain, its SIP front-end) against the fake gateway and the fake engine
# speaking the preview stream (fake_engine.py `preview`).
#
#   1. The preview stops short of the caller's last word ("...is it a large" for "...is
#      it a large city?"): the LLM is asked on the provisional words at once, and the
#      final's added word is logged as such and corrected into the conversation's record.
#   2. The preview has every word: asked on them, the final as asked, nothing corrected.
#   3. The setting on, an engine without the preview stream: the call runs on segments,
#      as before.
#   4. 1. with the preview asked at the verdict (TEAPORT_STT_PREVIEW_AT_STOP off) rather
#      than at the VAD stop (the default, stt.py THE PEEK).
#
# Run: python test_turn_text_call.py
#
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from fake_engine import DEFAULT_TRANSCRIPT, FakeEngine  # noqa: E402
from fake_sip_gateway import run_call  # noqa: E402
from test_sip_fake_gateway import QUESTION_WAV, Rig, user_texts  # noqa: E402

SHORT = DEFAULT_TRANSCRIPT.rsplit(" ", 1)[0]      # the preview missing "city?"


async def _call(engine: FakeEngine, peek: bool = True):
    """One call through the real brain with slices asked for (previewed at the VAD stop,
    or at the verdict); the brain's journal."""
    rig = Rig()
    rig.engine = engine
    was = {k: os.environ.get(k) for k in ("TEAPORT_STT_TURN_TEXT",
                                          "TEAPORT_STT_PREVIEW_AT_STOP")}
    os.environ["TEAPORT_STT_TURN_TEXT"] = "slice"
    os.environ["TEAPORT_STT_PREVIEW_AT_STOP"] = "true" if peek else "false"
    try:
        async with rig:
            rig.start_brain()
            assert await rig.gw.wait_brain(60), "teaport-brain never connected to the gateway"
            s = await run_call(rig.gw, caller="+15551234567", wav=QUESTION_WAV,
                               hangup="done", hangup_after=60)
            await asyncio.sleep(0.5)
            with open(rig.logs[0], encoding="utf-8", errors="replace") as f:
                journal = f.read()
            return s, journal
    finally:
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _turn_lines(journal):
    return [line for line in journal.splitlines() if "[TURN]" in line]


async def test_a_preview_short_of_the_last_word_is_answered_then_corrected():
    engine = FakeEngine(preview=True, previews=[SHORT])
    s, journal = await _call(engine)
    assert engine.stt_finals == [DEFAULT_TRANSCRIPT], engine.stt_finals
    assert SHORT in engine.stt_previews, engine.stt_previews
    asked = user_texts(engine)
    assert SHORT in asked and DEFAULT_TRANSCRIPT not in asked, (
        f"the LLM was not asked on the provisional words: {asked}")
    assert s["reply_onset"] is not None and s["reply_latency"] < 4, s
    lines = _turn_lines(journal)
    assert any("final adds added=['city']" in line for line in lines), lines
    assert any("-> corrected in the context" in line for line in lines), lines
    assert "turn text: SLICED" in journal


async def test_previewed_at_the_verdict_the_same_short_preview_is_corrected_too():
    engine = FakeEngine(preview=True, previews=[SHORT])
    s, journal = await _call(engine, peek=False)
    asked = user_texts(engine)
    assert SHORT in asked and DEFAULT_TRANSCRIPT not in asked, asked
    lines = _turn_lines(journal)
    assert any("final adds added=['city']" in line for line in lines), lines
    assert "peek" not in journal, "previewed at the verdict: no peek"


async def test_a_full_preview_is_the_final_and_nothing_is_corrected():
    engine = FakeEngine(preview=True)
    s, journal = await _call(engine)
    asked = user_texts(engine)
    assert DEFAULT_TRANSCRIPT in asked, asked
    lines = _turn_lines(journal)
    assert lines and all("as asked" in line for line in lines), lines
    assert "corrected" not in journal


async def test_an_engine_without_the_preview_runs_the_call_on_segments():
    engine = FakeEngine()
    s, journal = await _call(engine)
    assert DEFAULT_TRANSCRIPT in user_texts(engine)
    assert "offers no preview stream" in journal and not _turn_lines(journal)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}", flush=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
