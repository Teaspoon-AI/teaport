#
# A sliced turn's correction (turn_text.TurnTextCorrector; stt.py TURN TEXT) against what
# happens to the provisional final above the aggregator, and whose a speculation asked on
# the stop's preview is. From the 2026-10-27 review of the prototype.
#
#   * The correction rides behind the final it corrects through every processor that
#     holds the STT's frames -- RunControlClaims while the relay decides, the barge-in
#     judge while it judges -- since both hold endpointing.STT_FRAMES, one tuple for all.
#     It used to overtake the held final, find it nowhere and push it again: "Okay sure
#     Okay sure go ahead".
#   * The corrector never pushes words again. Words it finds nowhere were taken (Wi-Fi
#     setup, run control), judged a backchannel, or dropped by the barge-in guard, each of
#     which TakenWords records: its line says which, and the words stay where the policy
#     put them. A re-push used to decide them a second time, under another state: a
#     judged backchannel came back as a barge-in, a claimed utterance was acted on twice.
#   * A commit the corrector has not seen go by (the aggregator's own commit, from its
#     input task) is still found: commits are recorded as the aggregator writes them.
#   * A speculation asked on a stop's preview ahead of any turn is stood down when the
#     words are taken, and when a run of ours is asked.
#
# All utterances are synthetic.
#
# Run: python test_turn_text_taken.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import TranscriptionFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

import test_owed_render as render  # noqa: E402
import test_run_control as rc  # noqa: E402
import test_speculative_reply as specs  # noqa: E402
import test_turn_text_correction as ttc  # noqa: E402
from test_barge_judge import GateRig, _Judge, final as jfinal, interim as jinterim  # noqa: E402

from teaport_brain import owed, services  # noqa: E402
from teaport_brain.barge_pause import CONTINUE  # noqa: E402
from teaport_brain.run_control import RunControlResultFrame  # noqa: E402
from teaport_brain.speculate import Speculator  # noqa: E402
from teaport_brain.taken_turn import TakenWords  # noqa: E402
from teaport_brain.endpointing import TurnTextFinalFrame  # noqa: E402

DOWN = FrameDirection.DOWNSTREAM
UP = FrameDirection.UPSTREAM


def correction(asked, final, added=(), dropped=(), kind="adds", stop_n=1, pushed_at=0.0):
    return TurnTextFinalFrame(stop_n=stop_n, asked=asked, final=final, kind=kind,
                              added=tuple(added), dropped=tuple(dropped), pushed_at=pushed_at)


# ---- the correction rides behind the final it corrects ---------------------------------

async def test_run_control_holds_the_correction_behind_the_final_it_asks_about():
    h = rc._harness(consults=1, wait_secs=5)
    provisional = rc._final("Okay sure", stop_n=1)
    await rc._in(h, provisional)
    fix = correction("Okay sure", "Okay sure go ahead", added=("go", "ahead"))
    await rc._in(h, fix)
    assert rc._passed(h) == [], "the correction overtook the final the relay is asked about"
    await rc._in(h, RunControlResultFrame(request_id="r1", claimed=False))
    assert rc._passed(h) == [provisional, fix], rc._passed(h)
    assert [q["text"] for q in rc._questions(h)] == ["Okay sure"], "asked once"


async def test_the_barge_in_judge_holds_the_correction_behind_the_final_it_judges():
    judge = _Judge({"Okay sure": CONTINUE}, delay=0.25)
    async with GateRig(judge, wait=0.6) as rig:
        fix = correction("Okay sure.", "Okay sure, go ahead.", added=("go", "ahead"), stop_n=3)
        await rig.task.queue_frames([jinterim("Okay sure"), jfinal("Okay sure."), fix])
        await asyncio.sleep(0.1)
        passed = [f for _, d, f in rig.bottom.frames if d == DOWN]
        assert fix not in passed, "the correction overtook the final the judge holds"
        await asyncio.sleep(0.4)
        passed = [f for _, d, f in rig.bottom.frames if d == DOWN]
        assert rig.out() == [("close", 3)] and passed[-1] is fix, rig.out()


# ---- the corrector never pushes words again --------------------------------------------

async def test_words_placed_nowhere_are_logged_with_why_and_never_pushed_again():
    user = ttc._user()
    taken = TakenWords()
    c = ttc.Corrector(user, taken_words=taken)
    await taken.took("Okay sure", "judged a backchannel")
    await c._apply(correction("Okay sure", "Okay sure go ahead", added=("go", "ahead")))
    assert c.pushed == [] and c.unplaced == 1, c.pushed
    assert c.last_unplaced == "judged a backchannel"
    await c._apply(correction("so then", "so then yes", added=("yes",)))
    assert c.pushed == [] and c.unplaced == 2 and c.last_unplaced is None


async def test_a_commit_the_corrector_never_saw_go_by_is_still_found():
    # The aggregator commits (its own task: the ceiling, the safety net, the watchdog)
    # after the correction was forwarded: the commit's frame is behind the correction.
    user = ttc._user()
    c = ttc.Corrector(user)
    pushed_at = time.monotonic()
    await user._handle_transcription(TranscriptionFrame("so I was thinking", "u", "t"))
    frames = []

    async def push(frame, direction=DOWN):
        frames.append(frame)
    user.push_frame = push
    await user._push_aggregation()
    await c._apply(correction("so I was thinking", "so I was thinking about tea",
                              added=("about", "tea"), pushed_at=pushed_at))
    assert c.corrected == 1, c._tally()
    assert user.context.get_messages()[-1]["content"] == "so I was thinking about tea"


async def test_every_way_words_go_elsewhere_is_on_record():
    import test_wifi_voice as wvt
    from pipecat.frames.frames import BotStartedSpeakingFrame
    from teaport_brain.barge_pause import PauseAwareMinWordsStrategy
    taken = TakenWords()
    # Wi-Fi setup takes its phrase.
    v = wvt.Voice(wvt._path())
    v.taken_words = taken
    await v.process_frame(wvt._final("connect to wifi", 1), DOWN)
    assert taken.why("connect to wifi") == "taken by Wi-Fi setup"
    # OpenClaw's run control claims a final.
    h = rc._harness(consults=1, wait_secs=5)
    h.claims.taken_words = taken
    await rc._in(h, rc._final("cancel that"))
    await rc._in(h, RunControlResultFrame(request_id="r1", claimed=True))
    assert taken.why("cancel that") == "taken by OpenClaw's run control"
    # The barge-in judge lets the reply carry on over a backchannel.
    async with GateRig(_Judge({"Okay": CONTINUE}), wait=0.6) as rig:
        rig.gate.taken_words = taken
        await rig.task.queue_frames([jinterim("Okay"), jfinal("Okay.")])
        await asyncio.sleep(0.3)
        assert rig.out() == [("close", 3)], rig.out()
    assert taken.why("Okay.") == "judged a backchannel"
    # The barge-in guard drops too few words over the bot -- and records nothing for
    # words that start a turn.
    guard = PauseAwareMinWordsStrategy(min_words=3)
    guard.taken_words = taken
    await guard.process_frame(BotStartedSpeakingFrame())
    await guard.process_frame(jfinal("uh huh"))
    assert taken.why("uh huh") == "dropped by the barge-in guard"
    await guard.process_frame(jfinal("wait a second please"))
    assert taken.why("wait a second please") is None


# ---- a speculation ahead of any turn has an owner --------------------------------------

async def test_taken_words_stand_down_the_speculation_asked_on_them():
    ctx, agg, llm, spec, opened = specs._rig(parts=())
    taken = TakenWords()
    taken.listeners.append(lambda why: spec.cancel(f"words {why}"))
    assert await spec.start("stop the timer", "test", ahead_of_turn=True)
    await specs._settle()
    await taken.took("stop the timer", "run control")
    assert spec.text is None and spec.misses == 1


async def test_a_run_of_ours_stands_down_a_speculation_ahead_of_the_turn():
    ctx = render._context()
    llm = render._llm(render._Provider())
    llm.owed, _gate = render._book(ctx)
    spec = Speculator(llm=llm, aggregator=specs.FakeAggregator(ctx, turn_open=False))
    llm.speculator = spec

    async def open_stream(context):
        return specs.FakeStream()
    llm._open_stream = open_stream
    assert await spec.start("what time is it", "test", ahead_of_turn=True)
    await specs._settle()
    token = services._REPLY.set(owed.Reply(owed.RUN))
    try:
        await llm.get_chat_completions(ctx)
    finally:
        services._REPLY.reset(token)
    assert spec.text is None and spec.misses == 1, "a run left the speculation live"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        failed = []
        for fn in tests:
            try:
                await fn()
                print(f"  ok {fn.__name__}", flush=True)
            except Exception as e:  # noqa: BLE001 -- report every one
                failed.append(fn.__name__)
                print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}", flush=True)
        if failed:
            raise SystemExit(f"{len(failed)} failed: {failed}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
