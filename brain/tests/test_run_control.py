#
# Unit test: utterances OpenClaw's spoken run control takes (#230, run_control.py).
#
# While a consult runs on a Talk session, OpenClaw's relay acts on "cancel that", "how's
# it going?" and the like itself, and answers them through the provider. The brain's own
# model must not answer the same words too. What is asserted, and why each one matters:
#
#   * Nothing is held without a consult running, nor an empty or already-taken final:
#     every other turn ends exactly as before, with no wait.
#   * With one running, the final waits for the plugin's answer, and so does every STT
#     frame behind it, in order: a segment close that overtook its words would end the
#     turn without them.
#   * Claimed, the final goes on taken (emptied and marked), so TakenTurnCloser ends the
#     turn with nothing to answer, and the words reach the context as a notice.
#     Not claimed, or no answer in time, the frames go on unchanged.
#   * A stale answer changes nothing; the session's end lets what is held go first.
#   * The wire: the question goes out as {"type":"run_control"}, the answer comes back as
#     a RunControlResultFrame; a session built with run_control places it directly below
#     the transcript emitter with the closer in its stop strategies, and /talk builds it
#     only for a plugin that announced ?run_control=1.
#
# Run: python test_run_control.py   (hermetic)
#
import asyncio
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    EndFrame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.captions import UserTranscriptEmitter  # noqa: E402
from teaport_brain.endpointing import SegmentDoneFrame  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.run_control import RunControlClaims, RunControlResultFrame  # noqa: E402
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402
from teaport_brain.taken_turn import TakenTurnCloser, is_taken, taken  # noqa: E402


def _final(text, stop_n=1):
    return FinalTranscriptionFrame(text, "u", "t", None, finalized=True, stop_n=stop_n)


class _Notes:
    def __init__(self):
        self.notices = []

    def add_notice(self, text):
        self.notices.append(text)


def _harness(consults=1, wait_secs=0.3):
    running = {"n": consults}
    notes = _Notes()
    claims = RunControlClaims(lambda: running["n"], notes=notes, wait_secs=wait_secs)
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    claims.push_frame = push
    claims.create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    return SimpleNamespace(claims=claims, pushed=pushed, notes=notes, running=running)


async def _in(h, frame):
    await h.claims.process_frame(frame, FrameDirection.DOWNSTREAM)


def _questions(h):
    return [f.message for f in h.pushed if isinstance(f, OutputTransportMessageUrgentFrame)]


def _passed(h):
    return [f for f in h.pushed if not isinstance(f, OutputTransportMessageUrgentFrame)]


async def test_nothing_is_held_without_a_consult_running():
    h = _harness(consults=0)
    final = _final("cancel that")
    await _in(h, final)
    assert h.pushed == [final] and _questions(h) == []


async def test_an_empty_or_taken_final_is_not_asked_about():
    h = _harness()
    for frame in (_final("  "), taken(_final("set up wifi"))):
        await _in(h, frame)
    assert _questions(h) == [] and len(h.pushed) == 2


async def test_a_claimed_final_ends_its_turn_unanswered_and_leaves_a_notice():
    h = _harness()
    final, close = _final(" cancel that", stop_n=4), SegmentDoneFrame(stop_n=5)
    interim = InterimTranscriptionFrame("and", "u", "t")
    await _in(h, final)
    await _in(h, close)
    await _in(h, interim)
    assert _questions(h) == [{"type": "run_control", "id": "r1", "text": "cancel that"}]
    assert _passed(h) == [], "a frame went on while its final was being asked about"
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    out = _passed(h)
    assert len(out) == 3 and out[1:] == [close, interim], out
    assert is_taken(out[0]) and out[0].text == "" and out[0].stop_n == 4, out[0]
    assert type(out[0]) is FinalTranscriptionFrame, "the final lost its uninterruptible class"
    assert h.notes.notices == [
        'The user said "cancel that" about the request running in the background, and '
        "OpenClaw took it: it acts on it and answers them itself."], h.notes.notices
    assert h.claims.claimed == 1
    # The next final is asked about afresh.
    await _in(h, _final("what's the weather"))
    assert _questions(h)[-1]["id"] == "r2"
    await _in(h, RunControlResultFrame(request_id="r2", claimed=False))


async def test_a_final_not_claimed_goes_on_unchanged():
    h = _harness()
    final, close = _final("what's the weather tomorrow?"), SegmentDoneFrame(stop_n=2)
    await _in(h, final)
    await _in(h, close)
    await _in(h, RunControlResultFrame(request_id="r1", claimed=False))
    assert _passed(h) == [final, close] and h.notes.notices == []
    assert h.claims.passed == 1


async def test_no_answer_in_time_lets_the_turn_go_on():
    h = _harness(wait_secs=0.05)
    final = _final("cancel that")
    await _in(h, final)
    await asyncio.sleep(0.15)
    assert _passed(h) == [final] and h.claims.unanswered == 1
    # A late answer to it changes nothing.
    await _in(h, RunControlResultFrame(request_id="r1", claimed=True))
    assert _passed(h) == [final] and h.notes.notices == []


async def test_a_stale_answer_is_ignored():
    h = _harness()
    final = _final("never mind")
    await _in(h, final)
    await _in(h, RunControlResultFrame(request_id="r9", claimed=True))
    assert _passed(h) == []
    await _in(h, RunControlResultFrame(request_id="r1", claimed=False))
    assert _passed(h) == [final]


async def test_the_sessions_end_lets_what_is_held_go_first():
    h = _harness()
    final = _final("how's it going")
    await _in(h, final)
    end = EndFrame()
    await _in(h, end)
    assert _passed(h) == [final, end]


async def test_the_serializer_turns_the_answer_into_a_frame():
    s = TeaportGatewaySerializer()
    f = await s.deserialize(json.dumps({"type": "run_control_result", "id": "r3",
                                        "claimed": True}))
    assert isinstance(f, RunControlResultFrame) and (f.request_id, f.claimed) == ("r3", True)
    f = await s.deserialize(json.dumps({"type": "run_control_result", "id": 4,
                                        "claimed": "yes"}))
    assert (f.request_id, f.claimed) == ("4", False), "only a true claims"


async def test_the_session_places_it_below_the_emitter_with_the_closer():
    from teaport_brain.agent_session import build_agent_session
    from test_client_notes import _Transport

    def inner(session):
        return list(next(p for p in session.task.pipeline.processors
                         if isinstance(p, Pipeline)).processors)

    def stops(procs):
        agg = next(p for p in procs if isinstance(p, LLMUserAggregator))
        return agg._user_turn_controller.user_turn_strategies.stop

    plain = inner(build_agent_session(_Transport(), context_notes=True))
    assert not any(isinstance(p, RunControlClaims) for p in plain)
    assert not any(isinstance(s, TakenTurnCloser) for s in stops(plain))

    session = build_agent_session(_Transport(), context_notes=True, run_control=True)
    procs = inner(session)
    at = next(i for i, p in enumerate(procs) if isinstance(p, RunControlClaims))
    assert isinstance(procs[at - 1], UserTranscriptEmitter), "not directly below the emitter"
    agg = next(i for i, p in enumerate(procs) if isinstance(p, LLMUserAggregator))
    assert at < agg
    assert procs[at].notes is session.client_notes is not None
    assert isinstance(stops(procs)[0], TakenTurnCloser), "the closer must come first"
    # It counts the consults the session's tools count (tools._consult_and_land).
    session.followup_gate.consults_running = 1
    assert procs[at]._consults() == 1


async def test_talk_builds_it_only_for_a_plugin_that_answers():
    from test_client_notes import _run_talk

    _, built = await _run_talk(query={"run_control": "1"})
    assert built.get("run_control") is True
    _, built = await _run_talk()
    assert built.get("run_control") is False


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
