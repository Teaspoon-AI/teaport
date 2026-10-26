#
# Unit test: FollowupGate's "LLM mid-response" latch must always release.
#
# The gate sets _llm on LLMFullResponseStartFrame and used to clear it ONLY on
# LLMFullResponseEndFrame. But the end frame is not guaranteed to arrive at the
# gate: for a completion with no synthesizable text (a bare tool call — exactly
# the async openclaw_agent_consult shape, where no_inference() means no later completion
# comes either), the TTS service holds the end frame waiting for an audio context
# that empty text never creates; and an interruption discards a held one outright.
# Either way the gate read "mid-response" forever: every consult narrator line
# timed out its 6s gap wait and was skipped, and the consult follow-up (since #203,
# owed.py's run) burned its full 60s max_wait — total dead air in genuine silence, the
# exact condition the narrator exists to fill.
#
# The release points asserted here: a FunctionCallInProgressFrame (the completion
# is done producing speech; any audio it did produce is tracked via the bot
# speaking flags) and an InterruptionFrame (the in-flight response is dead by
# definition).
#
# Run: python test_followup_gate.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Refuse to run against the wrong pipecat (a bare `import pinned_pipecat` checked
# nothing: require_pinned() has to be CALLED). It imports the package first, for
# HF_HUB_OFFLINE.
from pinned_pipecat import require_pinned  # noqa: E402
require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    EndFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    BotStartedSpeakingFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from pipecat.utils.utils import obj_id  # noqa: E402

from teaport_brain.followup_gate import (  # noqa: E402
    FollowupGate, RunFrame, RunSilentFrame, TurnCommitMark,
)


def _gate(quiet_secs=0.01):
    """A FollowupGate with push_frame stubbed out (no pipeline downstream)."""
    gate = FollowupGate(quiet_secs=quiet_secs)

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    gate.push_frame = fake_push

    # The base FrameProcessor reacts to InterruptionFrame with task-manager
    # bookkeeping that a bare, unwired processor doesn't have; the gate's own
    # handling (what these tests assert) runs regardless, so stub the hook.
    async def _no_interruption_bookkeeping(*a, **k):
        pass
    gate._start_interruption = _no_interruption_bookkeeping
    return gate


async def _feed(gate, *frames):
    for f in frames:
        await gate.process_frame(f, FrameDirection.DOWNSTREAM)


def _tool_call(call_id="tc-1", name="openclaw_agent_consult"):
    """A call's in-progress frame, as the LLM broadcasts it for every tool here
    (cancel_on_interruption: pipecat's default)."""
    return FunctionCallInProgressFrame(
        function_name=name, tool_call_id=call_id, arguments={},
        cancel_on_interruption=True)


def _tool_result(run_llm=None, call_id="tc-1"):
    """The call's result. run_llm=False is tools.no_inference() -- the async
    placeholder nothing answers from; None means the answering completion follows."""
    return FunctionCallResultFrame(
        function_name="openclaw_agent_consult", tool_call_id=call_id, arguments={},
        result={"status": "queued"},
        properties=FunctionCallResultProperties(run_llm=False) if run_llm is False else None)


async def test_the_latch_latches():
    """Sanity for the tests below: a start frame with no release DOES hold the
    gate busy until max_wait — otherwise the release assertions prove nothing."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame())
    assert not await gate.wait_until_idle(max_wait=0.05), (
        "gate reported idle mid-response; the latch tests are vacuous")


async def test_end_frame_releases():
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    assert await gate.wait_until_idle(max_wait=0.5)


async def test_a_bare_tool_call_completion_releases_the_latch():
    """The stranded-end shape: start frame arrives, end frame never does (held by
    the TTS service for a text-less completion). The function call starting is
    the proof the completion is done speaking — the gate must read idle."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call())
    assert await gate.wait_until_idle(max_wait=0.5), (
        "gate still busy after a bare tool-call completion — the _llm latch is "
        "stuck and every narrator line will be skipped")


async def test_an_interruption_releases_the_latch():
    """An interruption kills the in-flight response AND discards any end frame
    the TTS service was holding, so it must clear the latch itself."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), InterruptionFrame())
    assert await gate.wait_until_idle(max_wait=0.5), (
        "gate still busy after an interruption — the _llm latch is stuck")


# --- the budget: one deadline for the whole call ---

async def test_a_turn_free_wait_waits_out_a_synchronous_tool_call():
    """The two waits across a tool call whose result IS answered: the narrator
    reads idle the moment the call starts (its silence is the gap to fill), but
    a turn-free waiter must not -- a client note appended now would be read by the
    tool's own answering completion. The turn is over when that completion ends.
    Found by Followup.tla (LATCH = "clearedOnToolCall", NoInterjectMidTurn)."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call())
    assert await gate.wait_until_idle(max_wait=0.5), "narrator: gate busy during the call"
    assert not await gate.wait_until_idle(max_wait=0.05, turn_free=True), (
        "turn-free: the gate read turn-free during a tool call whose answer is pending")
    await _feed(gate, _tool_result(), LLMFullResponseStartFrame())
    assert not await gate.wait_until_idle(max_wait=0.05, turn_free=True)
    await _feed(gate, LLMFullResponseEndFrame())
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True), (
        "turn-free: still held after the answering completion ended")


async def test_a_no_inference_result_ends_the_turn():
    """The async openclaw_agent_consult shape: the placeholder result runs no inference, so
    nothing will answer from it -- the turn is over."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call(), _tool_result(run_llm=False))
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True), (
        "turn-free: held after a no-inference result -- the turn never closes")


async def test_an_interruption_ends_the_turn_too():
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call(), InterruptionFrame())
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)


async def test_max_wait_bounds_the_whole_call_not_just_the_idle_wait():
    """The debounce sleep used to be bounded by a `remaining` computed BEFORE the
    idle wait and reused after it, so a call could overrun max_wait by a whole
    quiet window -- measured at 1.65s for max_wait=1.0 with the real 0.7s window.
    With 0.25s of budget and idle arriving at 0.15s, only 0.1s remains: less than
    the window, so the answer is False, and it is given by the deadline."""
    gate = _gate(quiet_secs=0.3)
    await _feed(gate, LLMFullResponseStartFrame())          # busy

    async def release():
        await asyncio.sleep(0.15)
        await _feed(gate, LLMFullResponseEndFrame())
    task = asyncio.create_task(release())
    t0 = time.monotonic()
    ok = await gate.wait_until_idle(max_wait=0.25)
    dt = time.monotonic() - t0
    await task
    assert not ok, "a debounce cut to a fraction of the quiet window was reported as a window"
    assert dt < 0.3, f"wait_until_idle overran max_wait=0.25 by {dt - 0.25:.2f}s"


async def test_a_pause_shorter_than_the_quiet_window_is_not_a_window():
    """Idle from the start, but with a budget smaller than the window: the old
    code slept for the budget and returned True -- the narrator could then push
    its line into a 0.1s pause between the user's words."""
    gate = _gate(quiet_secs=0.3)
    assert not await gate.wait_until_idle(max_wait=0.1)
    assert await gate.wait_until_idle(max_wait=1.0)         # the same gate, given room



# --- watching a line to completion ------------------------------------------------
#
# say_last_line, resume's line and the call prompt watch a line they speak themselves
# (AgentSession, call_prompt.py): completion comes from the transport's own frames,
# which are prompt and mean what they say.


async def test_a_delivery_that_finishes_settles_as_not_interrupted():
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, TTSStartedFrame(), BotStartedSpeakingFrame())
    assert not d.done.is_set(), "settled while the bot was still speaking"
    await _feed(gate, BotStoppedSpeakingFrame())
    assert d.done.is_set(), "a line that played to the end never settled"
    assert not d.interrupted


async def test_a_delivery_that_is_cut_settles_as_interrupted():
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, TTSStartedFrame(), BotStartedSpeakingFrame(), InterruptionFrame())
    assert d.done.is_set() and d.interrupted, "a barged-over reply must read as cut"
    assert d.spoke, "the line's audio had started"


async def test_a_watcher_of_its_own_line_counts_any_audio():
    """say_last_line and the call prompt watch a line they speak themselves, often with
    append_to_context=False: for them any audio is the line."""
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, TTSStartedFrame(append_to_context=False), BotStartedSpeakingFrame(),
                BotStoppedSpeakingFrame())
    assert d.spoke and d.done.is_set() and not d.interrupted


async def test_a_delivery_cut_before_it_speaks_still_settles():
    """An interruption between queueing and the first audio: the caller heard none of
    it, and nothing later will start or stop it."""
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, InterruptionFrame())
    assert d.done.is_set() and d.interrupted


async def test_someone_elses_bot_turn_does_not_settle_our_watch():
    """BotStopped for a reply that is not ours (ours has not started) must not be read
    as our delivery finishing."""
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, BotStoppedSpeakingFrame())
    assert not d.done.is_set(), (
        "a BotStopped from a turn our line never started settled it anyway")


async def test_a_dropped_watch_is_forgotten():
    gate = _gate()
    d = gate.watch_delivery()
    gate.drop_delivery(d)
    await _feed(gate, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
    assert not d.done.is_set(), "a dropped watch still settled"


# --- the claim: one turn-free window, one waiter ---

async def _two_waiters(gate, **kw):
    a = asyncio.ensure_future(gate.wait_until_idle(turn_free=True, **kw))
    b = asyncio.ensure_future(gate.wait_until_idle(turn_free=True, **kw))
    return a, b


async def test_one_window_goes_to_one_waiter():
    """The consult follow-up and a client-note reaction waiting together used to both
    get the same quiet window: two LLMRunFrames, two completions, and the first one's
    text retired both one-shot triggers. The window is now claimed by the first waiter
    it is reported to, until that waiter's completion has started and ended (owed.py's
    run takes the same claim at its door)."""
    gate = _gate(quiet_secs=0.02)
    await _feed(gate, BotStartedSpeakingFrame())
    a, b = await _two_waiters(gate, max_wait=2.0)
    await asyncio.sleep(0.01)
    await _feed(gate, BotStoppedSpeakingFrame())
    done, _ = await asyncio.wait({a, b}, timeout=0.3, return_when=asyncio.FIRST_COMPLETED)
    assert len(done) == 1 and done.pop().result() is True
    await asyncio.sleep(0.1)
    assert sum(t.done() for t in (a, b)) == 1, "both waiters took the same window"
    # The winner's completion starts (the claim ends; the turn holds the window) and
    # ends: the other waiter gets the next window.
    await _feed(gate, LLMFullResponseStartFrame())
    await asyncio.sleep(0.1)
    assert sum(t.done() for t in (a, b)) == 1, "the window reopened mid-completion"
    await _feed(gate, LLMFullResponseEndFrame())
    assert await asyncio.wait_for(asyncio.gather(a, b), timeout=1.0) == [True, True]


async def test_a_claim_given_back_reopens_the_window():
    gate = _gate(quiet_secs=0.02)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)
    assert not gate.is_clear(), "the window was not claimed"
    assert not await gate.wait_until_idle(max_wait=0.1, turn_free=True)
    gate.release_claim()
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)


async def test_an_interruption_ends_a_claim():
    """A barge-in flushes the claimant's queued LLMRunFrame, so no completion will
    start for it: the claim cannot wait for one."""
    gate = _gate(quiet_secs=0.02)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)
    await _feed(gate, InterruptionFrame())
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)


async def test_a_forgotten_claim_expires():
    gate = FollowupGate(quiet_secs=0.02, claim_secs=0.1)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True), "the claim never expired"


async def test_the_narrator_wait_neither_claims_nor_heeds_a_claim():
    """The narrator speaks during a tool call's silence; the claim is the turn-free
    waiters'."""
    gate = _gate(quiet_secs=0.02)
    assert await gate.wait_until_idle(max_wait=0.5)
    assert gate.is_clear(), "a narrator wait claimed the window"
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)
    assert await gate.wait_until_idle(max_wait=0.5), "a claim held the narrator back"


async def _commit(gate):
    """A completion asked for, as TurnCommitMark (above the LLM) sees it."""
    mark = TurnCommitMark(gate)

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    mark.push_frame = fake_push
    await mark.process_frame(LLMContextFrame(context=LLMContext([])), FrameDirection.DOWNSTREAM)


async def test_a_committed_turn_holds_the_window_until_its_completion_starts():
    """Review of #74: the gate learned of a turn only at its LLMFullResponseStartFrame,
    so the moment between a commit and that frame read as turn-free, and a waiter
    could post after the user's unanswered words and queue a second completion."""
    gate = _gate(quiet_secs=0.02)
    await _commit(gate)
    assert not gate.is_clear()
    assert not await gate.wait_until_idle(max_wait=0.1, turn_free=True), \
        "a window was reported between the commit and its completion"
    # The narrator's wait is about speech, not turns: a commit does not hold it.
    assert await gate.wait_until_idle(max_wait=0.1)
    await _feed(gate, LLMFullResponseStartFrame())
    assert not await gate.wait_until_idle(max_wait=0.1, turn_free=True)
    await _feed(gate, LLMFullResponseEndFrame())
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)


# ---- the answer after a tool (#17): the push held while the caller's speech is pending

class _Top(FrameProcessor):
    """Above the gate: records what reaches it going up (the LLM's side)."""

    def __init__(self):
        super().__init__()
        self.up = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM:
            self.up.append(frame)
        await self.push_frame(frame, direction)


class _GateRun:
    """[top] -> gate, running; the answer's push is sent up into the gate as the
    assistant aggregator below it sends it."""

    def __init__(self, release=0.1, cap=5.0):
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.task import PipelineTask
        self.gate = FollowupGate(quiet_secs=0.01, answer_release_secs=release,
                                 answer_max_secs=cap)
        self.top = _Top()
        self.task = PipelineTask(Pipeline([self.top, self.gate]), cancel_on_idle_timeout=False)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_t, _f):
            self.started.set()

    async def __aenter__(self):
        from pipecat.pipeline.runner import PipelineRunner
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), 10)
        return self

    async def __aexit__(self, *exc):
        if not self._run.done():
            await self.task.cancel()
        await self._run

    async def down(self, *frames):
        """Send frames down and wait until the gate has handled them."""
        mark = TTSStoppedFrame()                 # any frame the gate passes on
        seen = asyncio.Event()
        process = self.gate.process_frame

        async def watching(frame, direction):
            await process(frame, direction)
            if frame is mark:
                seen.set()
        self.gate.process_frame = watching
        await self.task.queue_frames([*frames, mark])
        await asyncio.wait_for(seen.wait(), 5)
        self.gate.process_frame = process

    async def answer(self):
        frame = LLMContextFrame(LLMContext([{"role": "user", "content": "q"}]))
        await self.gate.queue_frame(frame, FrameDirection.UPSTREAM)
        return frame

    async def until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return predicate()


async def test_the_answer_passes_at_once_with_nobody_talking():
    async with _GateRun() as r:
        frame = await r.answer()
        assert await r.until(lambda: frame in r.top.up), "held with nobody talking"
        assert r.gate.answers_held == 0


async def test_the_answer_waits_out_speech_that_has_no_words():
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame())
        frame = await r.answer()
        await r.down(VADUserStoppedSpeakingFrame())
        stopped = time.monotonic()
        assert await r.until(lambda: frame in r.top.up), "a cough's answer never went on"
        assert time.monotonic() - stopped >= 0.15, "released before the words could come"
        assert r.gate.answers_held == 1


async def test_the_answer_is_dropped_when_the_words_start_a_turn():
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame())
        frame = await r.answer()
        await r.down(VADUserStoppedSpeakingFrame(), UserStartedSpeakingFrame(),
                     InterruptionFrame())
        assert not await r.until(lambda: frame in r.top.up, timeout=0.5), (
            "the answer went on after the caller's turn started")
        assert r.gate.answers_dropped == 1


async def test_an_answer_pushed_into_an_open_turn_is_dropped():
    async with _GateRun() as r:
        await r.down(UserStartedSpeakingFrame())
        frame = await r.answer()
        assert not await r.until(lambda: frame in r.top.up, timeout=0.3)
        assert r.gate.answers_dropped == 1


async def test_a_session_end_drops_a_held_answer():
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame())
        frame = await r.answer()
        await r.until(lambda: r.gate.answers_held == 1)
        # What the gate holds the moment the End has passed it -- before cleanup, which
        # would drop it anyway: in between, its cap could still send it on.
        at_end = []
        process = r.gate.process_frame

        async def watching(f, direction):
            await process(f, direction)
            if isinstance(f, EndFrame):
                at_end.append(r.gate._held_answer)
        r.gate.process_frame = watching
        await r.task.queue_frames([EndFrame()])
        await asyncio.wait_for(r._run, 5)
        assert at_end == [None], "the End passed with the answer still held"
        assert frame not in r.top.up, "a held answer went on into a closing session"


async def test_the_cap_sends_the_answer_on_while_the_caller_keeps_talking():
    # The cap is never under the release window (the gate raises it to that); the
    # caller never stops, so only the cap can end this hold.
    async with _GateRun(release=0.3, cap=0.5) as r:
        await r.down(VADUserStartedSpeakingFrame())
        frame = await r.answer()
        assert await r.until(lambda: frame in r.top.up), "the cap never sent it on"
        assert r.gate.answers_released == 1 and r.gate._held_answer is None


async def test_the_cap_drops_the_answer_into_an_open_turn():
    # The caller's turn has started (its interruption not here yet): its commit reads
    # the result, so the cap must not ask for a second completion.
    # The cap is never under the release window (the gate raises it to that); the
    # caller never stops, so only the cap can end this hold.
    async with _GateRun(release=0.3, cap=0.5) as r:
        await r.down(VADUserStartedSpeakingFrame())
        frame = await r.answer()
        await r.until(lambda: r.gate.answers_held == 1)
        await r.down(UserStartedSpeakingFrame())
        assert await r.until(lambda: r.gate._held_answer is None), "still held past the cap"
        assert frame not in r.top.up, "the cap asked for the answer inside an open turn"
        assert r.gate.answers_dropped == 1 and r.gate.answers_released == 0


async def test_the_answer_waits_out_the_gap_between_the_vad_stop_and_the_words():
    # The result lands after the caller's VAD stop: their words are still to come.
    async with _GateRun(release=0.3) as r:
        await r.down(VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame())
        frame = await r.answer()
        assert await r.until(lambda: r.gate.answers_held == 1), (
            "sent on between the VAD stop and the words")
        await r.down(UserStartedSpeakingFrame(), InterruptionFrame())
        assert not await r.until(lambda: frame in r.top.up, timeout=0.6)
        assert r.gate._held_answer is None and r.gate.answers_dropped == 1


async def test_a_turn_of_two_segments_holds_nothing_once_committed():
    # "What's the time (300 ms) in Paris": the turn starts on the first segment, the
    # second VAD start comes inside the open turn (no turn start of its own), and the
    # commit takes its words. A tool's answer after that is asked at once.
    async with _GateRun(release=5.0) as r:
        await r.down(VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
                     VADUserStoppedSpeakingFrame(), VADUserStartedSpeakingFrame(),
                     VADUserStoppedSpeakingFrame())
        r.gate.words_committed(obj_id())       # the fence, at the commit
        await r.down(UserStoppedSpeakingFrame())
        frame = await r.answer()
        assert await r.until(lambda: frame in r.top.up, timeout=0.5), (
            "held with nobody talking: the commit had taken the speech's words")
        assert r.gate.answers_held == 0


async def test_speech_begun_after_the_commit_stays_pending():
    # The caller resumes inside the commit's await: those VAD frames are made after the
    # commit, and can reach the gate before it hears of the commit. Their words are not
    # in it: an answer must still wait for them.
    async with _GateRun(release=5.0) as r:
        await r.down(UserStartedSpeakingFrame())
        marker = obj_id()
        await r.down(VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame())
        r.gate.words_committed(marker)
        await r.down(UserStoppedSpeakingFrame())
        frame = await r.answer()
        assert await r.until(lambda: r.gate.answers_held == 1), "not held"
        assert frame not in r.top.up


async def test_speech_still_going_at_the_commit_stays_pending():
    # pipecat guards only the finalize on the caller speaking, not the commit: one made
    # while they still talk does not have the words they are saying (the gate as it was
    # in 0db54f6 let go of them here: the answer played before the words).
    async with _GateRun(release=5.0) as r:
        # A turn before it: live, the gate has a VAD stop in its history.
        await r.down(VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
                     VADUserStoppedSpeakingFrame())
        r.gate.words_committed(obj_id())
        await r.down(UserStoppedSpeakingFrame())
        assert not r.gate._speech_pending
        await r.down(VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame())
        r.gate.words_committed(obj_id())       # the commit, the VAD still speaking
        await r.down(VADUserStoppedSpeakingFrame(), UserStoppedSpeakingFrame())
        frame = await r.answer()
        assert await r.until(lambda: r.gate.answers_held == 1), (
            "sent on while the words of speech the commit did not take are on their way")
        assert frame not in r.top.up


async def test_the_re_applied_stops_commit_leaves_its_flush_pending():
    # A turn left open by a refused finalize; the caller's resumed span. The engine
    # closed a segment mid-span (final B1 is in the aggregation), the caller went on,
    # and at their VAD stop keep_barge_in_reachable re-applies the stop at once -- in
    # that stop's own handling, before the engine's flush of it (B2) lands. That
    # commit, through the real fence, carries B1 only: the stop strategy still owes the
    # stop its close, so the gate is told nothing and keeps waiting for B2. ff033c9
    # told it, and an answer landing before B2 played ahead of it.
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from test_barge_commit_order import IN_SR, Scripted, _Aggregator

    from teaport_brain.endpointing import CutReplyFence, LateStartTurnStopStrategy
    strategy = LateStartTurnStopStrategy(
        turn_analyzer=Scripted(sample_rate=IN_SR, params=SmartTurnParams(stop_secs=1.0)))
    async with _GateRun(release=5.0) as r:
        fence = CutReplyFence()
        agg = _Aggregator(words=["B1"])
        fence.hold_commits(agg)
        fence.on_words_committed = r.gate.words_committed
        fence.close_owed = strategy.close_owed
        await r.down(VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame())
        # The strategy counted the stop (it stamps its closes): its close is owed.
        strategy._stamped, strategy._stops, strategy._owed = True, 1, 1
        assert strategy.close_owed()
        await agg.push_aggregation()           # the re-applied stop's commit of B1
        assert agg.pushed == 1
        assert r.gate._speech_pending, "the gate let go of speech whose flush is owed"
        frame = await r.answer()
        assert await r.until(lambda: r.gate.answers_held == 1), "the answer was not held"
        # B2 lands (the close is paid) and is committed: now the speech is done.
        strategy._note_close(1)
        assert not strategy.close_owed()
        agg._aggregation = ["B2"]
        await agg.push_aggregation()
        assert not r.gate._speech_pending, "a commit with the close paid did not clear it"
        assert frame not in r.top.up


# ---- the run (#203): owed.py's own request, through the same door ------------------
#
# brain/formal/Owed.tla: Ask, RunGo, Cap; HoldNoLoss, NoRunUnderSpeech, NoRunMidTurn.
# (Whether its records are still owed is asked where it renders: test_owed_render.py.)

CTX = LLMContext([{"role": "user", "content": "q"}])


class _Reports:
    """What the door reports, as owed.Owed wires it."""

    def __init__(self, gate):
        self.dropped, self.turns = [], 0
        gate.on_run_dropped = self.dropped.append
        gate.on_caller_turn = self._turn

    def _turn(self):
        self.turns += 1


def _runs(r):
    return [f for f in r.top.up if isinstance(f, RunFrame)]


async def test_a_run_goes_up_to_the_llm_and_claims_the_window():
    async with _GateRun() as r:
        r.gate.ask(["k1"], "run", CTX)
        assert await r.until(lambda: _runs(r)), "the run never went"
        run = _runs(r)[0]
        assert run.keys == ("k1",) and run.purpose == "run" and run.context is CTX
        # The gate takes the claim itself: TurnCommitMark never sees a frame pushed up.
        assert not r.gate.is_clear(), "the run went without claiming the window"
        await r.down(LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
        assert r.gate.is_clear()


async def test_a_run_waits_out_caller_speech_with_no_words():
    """#198's hold, at the VAD level (ow_turnLevel: a turn-level window alone goes into
    speech whose words have not made a turn yet, #197)."""
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame())
        r.gate.ask(["k1"], "run", CTX)
        assert not await r.until(lambda: _runs(r), timeout=0.3), "a run went under speech"
        await r.down(VADUserStoppedSpeakingFrame())
        stopped = time.monotonic()
        assert await r.until(lambda: _runs(r)), "a cough's run never went"
        assert time.monotonic() - stopped >= 0.15, "went before the words could come"


async def test_a_run_reaching_the_hold_cap_is_dropped_not_sent():
    """Room TV noise holds the VAD up: the post-tool answer is released at the cap, a
    run is DROPPED (Owed.tla Cap) and the re-offer armed (on_run_dropped)."""
    async with _GateRun(release=0.2, cap=0.4) as r:
        reports = _Reports(r.gate)
        await r.down(VADUserStartedSpeakingFrame())
        r.gate.ask(["k1"], "run", CTX)
        assert await r.until(lambda: reports.dropped), "the cap never dropped it"
        assert not _runs(r), "the cap sent the run on"
        assert reports.dropped == [["k1"]]
        assert r.gate._run is None and r.gate.runs_dropped == 1


async def test_a_run_and_a_held_answer_do_not_displace_each_other():
    """HoldNoLoss (ow_oneSlot is the single slot, where the second replaced the first).
    The answer goes first; the run waits for its turn to end."""
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame(), LLMFullResponseStartFrame(), _tool_call())
        answer = await r.answer()
        await r.until(lambda: r.gate.answers_held == 1)
        r.gate.ask(["k1"], "run", CTX)
        await r.down(VADUserStoppedSpeakingFrame())
        assert await r.until(lambda: answer in r.top.up), "the held answer was lost"
        assert not await r.until(lambda: _runs(r), timeout=0.3), (
            "the run went before the answer it queued behind had been answered")
        await r.down(_tool_result(), LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
        assert await r.until(lambda: _runs(r)), "the run was lost behind the answer"


async def test_a_caller_turn_drops_the_waiting_run():
    """Owed.tla TurnStart: the turn's own request carries the note, and the re-offer is
    armed (on_caller_turn)."""
    async with _GateRun(release=0.3) as r:
        reports = _Reports(r.gate)
        await r.down(VADUserStartedSpeakingFrame())
        r.gate.ask(["k1"], "run", CTX)
        await r.down(UserStartedSpeakingFrame(), InterruptionFrame(),
                     VADUserStoppedSpeakingFrame(), UserStoppedSpeakingFrame())
        assert not await r.until(lambda: _runs(r), timeout=0.6), "the run went after the turn"
        assert reports.turns == 1 and r.gate._run is None


async def test_a_run_waits_out_a_tool_round():
    """NoRunMidTurn: the VAD alone would let it go between a sync call and its answer
    (ow_vadLevel)."""
    async with _GateRun() as r:
        await r.down(LLMFullResponseStartFrame(), _tool_call())
        r.gate.ask(["k1"], "run", CTX)
        assert not await r.until(lambda: _runs(r), timeout=0.3), "went into a tool round"
        await r.down(_tool_result(), LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
        assert await r.until(lambda: _runs(r))


async def test_a_run_waits_out_a_tool_call_put_out_with_a_line():
    """Owed.tla endClearsTool: "Let me check." + get_weather -- the completion's End
    reaches the gate while the call is out. The turn half alone (_turn) reads it over;
    the tool round holds the window until the answering completion starts."""
    async with _GateRun() as r:
        await r.down(LLMFullResponseStartFrame(), _tool_call(), BotStartedSpeakingFrame(),
                     BotStoppedSpeakingFrame(), LLMFullResponseEndFrame())
        r.gate.ask(["k1"], "run", CTX)
        assert not await r.until(lambda: _runs(r), timeout=0.3), (
            "the run went between a call put out with a line and its answer")
        await r.down(_tool_result(), LLMFullResponseStartFrame())
        assert not await r.until(lambda: _runs(r), timeout=0.2), "went into the answer"
        await r.down(LLMFullResponseEndFrame())
        assert await r.until(lambda: _runs(r))


async def test_a_cancelled_calls_late_in_progress_frame_opens_no_round():
    """Owed.tla staleFlag (OwedResolves): "Let me check." + get_weather, the caller barges
    in during the line and their turn commits no words. The call's in-progress frame is
    uninterruptible -- it waits behind the line's audio -- and reaches the gate after the
    interruption and the call's cancel. As one flag it shut the door and the lull for
    good; as an id already cancelled it is ignored, and the re-offer comes."""
    from teaport_brain.owed import Owed
    async with _GateRun(release=0.1) as r:
        owed = Owed(CTX, gate=r.gate, reoffer_quiet_s=0.3)
        await r.down(LLMFullResponseStartFrame(), BotStartedSpeakingFrame())   # the line
        owed.land("Fetch the top story.", "Tesla Roadster", "call-hn")         # run waits
        from pipecat.frames.frames import FunctionCallCancelFrame
        await r.down(VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
                     InterruptionFrame(),
                     FunctionCallCancelFrame(function_name="get_weather", tool_call_id="tc-w"),
                     _tool_call("tc-w", "get_weather"),          # stale, after the cut
                     BotStoppedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
                     UserStoppedSpeakingFrame())                 # no words: no completion
        assert r.gate.is_clear(), "a cancelled call's late in-progress frame shut the door"
        assert await r.until(lambda: [f for f in _runs(r) if f.purpose == "reoffer"]), (
            "the lull never fired")
        r.gate.ask(["call-hn"], "run", CTX)
        assert await r.until(lambda: len(_runs(r)) == 2), "the door did not reopen"


async def test_two_calls_out_end_the_turn_only_when_both_are_answered():
    """openclaw_agent_consult (its placeholder runs no inference) beside a sync tool in one
    completion: the placeholder's result does not end the turn or the round while the
    sync tool runs."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call("tc-a"),
                _tool_call("tc-b", "get_weather"), _tool_result(run_llm=False, call_id="tc-a"))
    assert not gate.is_clear(), "the placeholder's result ended a turn with a call still out"
    await _feed(gate, _tool_result(call_id="tc-b"))
    assert not gate.is_clear(), "the sync tool's answering completion has not run"
    await _feed(gate, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    assert gate.is_clear()


async def test_a_no_inference_result_ends_the_tool_round():
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call(), LLMFullResponseEndFrame())
    assert not gate.is_clear(), "a call out read as a clear window"
    await _feed(gate, _tool_result(run_llm=False))
    assert gate.is_clear()


async def test_a_run_waits_out_a_commit_not_yet_answered():
    async with _GateRun() as r:
        await _commit(r.gate)
        assert r.gate.caller_request_pending, "a commit not yet rendered is pending"
        r.gate.ask(["k1"], "run", CTX)
        assert not await r.until(lambda: _runs(r), timeout=0.3), (
            "went between a commit and its completion")
        r.gate.request_rendered()
        assert not r.gate.caller_request_pending
        await r.down(LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
        assert await r.until(lambda: _runs(r))


async def test_a_second_ask_joins_the_waiting_run():
    async with _GateRun(release=0.2) as r:
        await r.down(VADUserStartedSpeakingFrame())
        r.gate.ask(["k1"], "reoffer", CTX)
        r.gate.ask(["k2"], "run", CTX)
        await r.down(VADUserStoppedSpeakingFrame())
        assert await r.until(lambda: _runs(r))
        assert len(_runs(r)) == 1
        assert _runs(r)[0].keys == ("k1", "k2") and _runs(r)[0].purpose == "run"


async def test_a_silent_run_ends_its_turn():
    """A re-offer that called stay_silent (or came back empty) leaves its End held at the
    TTS: RunSilentFrame ends the turn at the gate instead."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame())
    assert not gate.is_clear()
    await _feed(gate, RunSilentFrame())
    assert gate.is_clear(), "the silent run's turn stayed open"


async def test_quiet_for_counts_only_a_quiet_line():
    """owed.py's lull (Owed.tla Quiet): caller speech, pending words, a turn or a run
    waiting all count as not quiet, and quiet counts from when they ended."""
    async with _GateRun(release=0.2) as r:
        await asyncio.sleep(0.05)
        assert r.gate.quiet_for() >= 0.04
        await r.down(VADUserStartedSpeakingFrame())
        assert r.gate.quiet_for() == 0.0, "caller speech counted as quiet"
        await r.down(VADUserStoppedSpeakingFrame())
        assert r.gate.quiet_for() == 0.0, "speech still pending counted as quiet"
        assert await r.until(lambda: r.gate.quiet_for() > 0.0), "never quiet again"
        assert r.gate.quiet_for() < 0.2, "quiet counted from before the speech"
        await r.down(LLMFullResponseStartFrame())
        assert r.gate.quiet_for() == 0.0, "a reply in flight counted as quiet"


async def test_caller_request_pending_tracks_the_turn_and_the_unrendered_commit():
    gate = _gate()
    assert not gate.caller_request_pending
    await _feed(gate, UserStartedSpeakingFrame())
    assert gate.caller_request_pending, "an open turn is a request still to be rendered"
    await _feed(gate, UserStoppedSpeakingFrame())
    await _commit(gate)
    assert gate.caller_request_pending
    await _feed(gate, InterruptionFrame())
    assert not gate.caller_request_pending, "a flushed commit stayed pending"


def test_every_run_frame_the_brain_sends_enters_above_the_user_aggregator():
    """FollowupGate holds every context frame that comes UP past it as the answer to a
    tool's result. The assistant aggregator below it also sends one up for an
    LLMRunFrame or LLMMessages*Frame(run_llm) that reaches it -- so none may: each the
    brain makes is queued on the PipelineTask (it enters at the top) or pushed by the STT
    (wake_gate's greeting, through stt.py's own push_frame), and the user aggregator
    takes it there (test_pipecat_premises.py, check_run_frames_stop_at_the_user_
    aggregator). A new sender anywhere else must be added here, and must not be below
    the user aggregator."""
    import re
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teaport_brain")
    made = re.compile(r"\b(LLMRunFrame|LLMMessages(Append|Update|Transform)Frame)\(")
    # Queued on the PipelineTask, nothing else: a processor's own queue_frame (below the
    # user aggregator) would send it straight past the gate as a tool's answer.
    queued_at_top = re.compile(r"\btask\.queue_frames?\(")
    allowed = {("wake_gate.py", "await push(LLMRunFrame())")}
    stray = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(root, name), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                code = line.split("#", 1)[0]
                if not made.search(code):
                    continue
                if queued_at_top.search(code):
                    continue
                if (name, code.strip()) in allowed:
                    continue
                stray.append(f"{name}:{n}: {line.strip()}")
    assert not stray, ("run frames sent from somewhere FollowupGate cannot tell from a "
                       f"tool's answer: {stray}")
    with open(os.path.join(root, "stt.py"), encoding="utf-8") as f:
        assert "self.wake_gate.final(text, self.push_frame)" in f.read(), (
            "wake_gate's push is no longer the STT's own push_frame")


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
