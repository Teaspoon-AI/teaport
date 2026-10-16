#
# Unit test: FollowupGate's "LLM mid-response" latch must always release.
#
# The gate sets _llm on LLMFullResponseStartFrame and used to clear it ONLY on
# LLMFullResponseEndFrame. But the end frame is not guaranteed to arrive at the
# gate: for a completion with no synthesizable text (a bare tool call — exactly
# the async ask_openclaw shape, where no_inference() means no later completion
# comes either), the TTS service holds the end frame waiting for an audio context
# that empty text never creates; and an interruption discards a held one outright.
# Either way the gate read "mid-response" forever: every consult narrator line
# timed out its 6s gap wait and was skipped, and the follow-up injector burned its
# full 60s max_wait — total dead air in genuine silence, the exact condition the
# narrator exists to fill.
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
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    BotStartedSpeakingFrame,
    OutputAudioRawFrame,
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

from teaport_brain.followup_gate import FollowupGate, TurnCommitMark  # noqa: E402


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


def _tool_call():
    return FunctionCallInProgressFrame(
        function_name="ask_openclaw", tool_call_id="tc-1", arguments={})


def _tool_result(run_llm=None):
    """The call's result. run_llm=False is tools.no_inference() -- the async
    placeholder nothing answers from; None means the answering completion follows."""
    return FunctionCallResultFrame(
        function_name="ask_openclaw", tool_call_id="tc-1", arguments={},
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

async def test_the_injector_waits_out_a_synchronous_tool_call():
    """The two waits across a tool call whose result IS answered: the narrator
    reads idle the moment the call starts (its silence is the gap to fill), but
    the follow-up injector must not -- appended now, its trigger would be read by
    the tool's own answering completion. The turn is over when that completion
    ends. Found by Followup.tla (LATCH = "clearedOnToolCall", NoInterjectMidTurn)."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call())
    assert await gate.wait_until_idle(max_wait=0.5), "narrator: gate busy during the call"
    assert not await gate.wait_until_idle(max_wait=0.05, turn_free=True), (
        "injector: the gate read turn-free during a tool call whose answer is pending")
    await _feed(gate, _tool_result(), LLMFullResponseStartFrame())
    assert not await gate.wait_until_idle(max_wait=0.05, turn_free=True)
    await _feed(gate, LLMFullResponseEndFrame())
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True), (
        "injector: still held after the answering completion ended")


async def test_a_no_inference_result_ends_the_turn():
    """The async ask_openclaw shape: the placeholder result runs no inference, so
    nothing will answer from it -- the turn is over and the injector may speak."""
    gate = _gate()
    await _feed(gate, LLMFullResponseStartFrame(), _tool_call(), _tool_result(run_llm=False))
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True), (
        "injector: held after a no-inference result -- the turn never closes")


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



# --- watching a delivery to completion -----------------------------------------
#
# The follow-up injector needs to know whether the caller got the answer. It used to
# ask the ledger, which charts a CUT immediately but does not chart a reply that simply
# FINISHED until the bot next speaks: _window_closed advances playout from a layout
# anchored on BotStartedSpeaking, ~0.2s after the audio really starts, so the estimate
# overshoots the end and the last fraction is never accounted.
#
# Live 2026-09-10 17:37: a 27.9s delivery played in full and the injector sat on the
# ledger for its whole 45s timeout before reporting "unknown". Completion now comes from
# the transport's own frames, which are prompt and mean what they say.


async def test_a_delivery_that_finishes_settles_as_not_interrupted():
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, TTSStartedFrame(), BotStartedSpeakingFrame())
    assert not d.done.is_set(), "settled while the bot was still speaking"
    await _feed(gate, BotStoppedSpeakingFrame())
    assert d.done.is_set(), (
        "a reply that played to the end never settled — the injector waits out its "
        "whole timeout and calls every quiet delivery unverified")
    assert not d.interrupted


async def test_a_delivery_that_is_cut_settles_as_interrupted():
    gate = _gate()
    d = gate.watch_delivery()
    await _feed(gate, TTSStartedFrame(), BotStartedSpeakingFrame(), InterruptionFrame())
    assert d.done.is_set() and d.interrupted, "a barged-over reply must read as cut"
    assert d.spoke, "our reply's audio had started"


async def test_a_filler_line_does_not_speak_for_the_reply():
    """brain/formal/Followup.tla, fu_heard_no_ownAudio_loss. The follow-up's completion
    calls a tool first: the tool-ack line plays (append_to_context=False) and the
    transport reports it like any audio. That must neither mark our reply spoken nor
    settle it as played -- the answer has not made a sound yet."""
    gate = _gate()
    d = gate.watch_delivery(replies_only=True)
    await _feed(gate, TTSStartedFrame(append_to_context=False), BotStartedSpeakingFrame(),
                BotStoppedSpeakingFrame())
    assert not d.spoke and not d.done.is_set(), (
        "a filler line settled the follow-up as played before the answer had any audio")
    await _feed(gate, TTSStartedFrame(), BotStartedSpeakingFrame(), _audio(),
                TTSStoppedFrame(), BotStoppedSpeakingFrame())
    assert d.spoke and d.done.is_set() and not d.interrupted


def _audio():
    return OutputAudioRawFrame(audio=b"\0\0" * 160, sample_rate=16000, num_channels=1)


async def test_a_reply_context_that_never_plays_is_not_spoken():
    """brain/formal/Followup.tla, fu_heard_ownStart_loss. pipecat opens a TTS context for
    any non-blank text: an emoji-only reply, or one whose every clause the engine failed,
    opens one and never plays. Its TTSStartedFrame alone must not count as the follow-up
    having spoken -- that rode the heard timeout to "delivered"."""
    gate = _gate()
    d = gate.watch_delivery(replies_only=True)
    await _feed(gate, TTSStartedFrame(), TTSStoppedFrame())
    assert not d.spoke, "a reply context with no audio counted as the answer playing"
    await _feed(gate, TTSStartedFrame(append_to_context=False), _audio(), TTSStoppedFrame())
    assert not d.spoke, "a filler's audio counted as the reply's"
    await _feed(gate, TTSStartedFrame(), _audio())
    assert d.spoke


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
        "a BotStopped from a turn our reply never started settled it anyway — the "
        "injector would call an undelivered answer delivered")


async def test_a_dropped_watch_is_forgotten():
    gate = _gate()
    d = gate.watch_delivery()
    gate.drop_delivery(d)
    await _feed(gate, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
    assert not d.done.is_set(), "a dropped watch still settled"


# --- the claim: one turn-free window, one injector ---

async def _two_waiters(gate, **kw):
    a = asyncio.ensure_future(gate.wait_until_idle(turn_free=True, **kw))
    b = asyncio.ensure_future(gate.wait_until_idle(turn_free=True, **kw))
    return a, b


async def test_one_window_goes_to_one_injector():
    """The consult follow-up and a client-note reaction waiting together used to both
    get the same quiet window: two LLMRunFrames, two completions, and the first one's
    text retired both one-shot triggers. The window is now claimed by the first waiter
    it is reported to, until that waiter's completion has started and ended."""
    gate = _gate(quiet_secs=0.02)
    await _feed(gate, BotStartedSpeakingFrame())
    a, b = await _two_waiters(gate, max_wait=2.0)
    await asyncio.sleep(0.01)
    await _feed(gate, BotStoppedSpeakingFrame())
    done, _ = await asyncio.wait({a, b}, timeout=0.3, return_when=asyncio.FIRST_COMPLETED)
    assert len(done) == 1 and done.pop().result() is True
    await asyncio.sleep(0.1)
    assert sum(t.done() for t in (a, b)) == 1, "both injectors took the same window"
    # The winner's completion starts (the claim ends; the turn holds the window) and
    # ends: the other injector gets the next window.
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
    """The narrator speaks during a tool call's silence; the claim is the injectors'."""
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
    so the moment between a commit and that frame read as turn-free, and an injector
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


async def test_a_waiter_that_ran_out_waits_out_a_claimed_window():
    """Review of #74: the consult follow-up goes ahead when its wait runs out. If a
    window is claimed right then, it now waits for the claimant's completion to start
    and end, rather than queue a second completion into the same window."""
    gate = _gate(quiet_secs=0.02)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)  # someone's claim
    waiter = asyncio.ensure_future(gate.wait_out_claim(max_wait=2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done(), "went ahead into a claimed window"
    await _feed(gate, LLMFullResponseStartFrame())  # the claimant's completion
    await asyncio.sleep(0.05)
    assert not waiter.done(), "went ahead while the claimant's completion ran"
    await _feed(gate, LLMFullResponseEndFrame())
    await asyncio.wait_for(waiter, timeout=0.5)
    # Nothing claimed and no completion running: no wait, however much speech.
    await _feed(gate, BotStartedSpeakingFrame())
    await asyncio.wait_for(gate.wait_out_claim(max_wait=2.0), timeout=0.05)


async def test_a_waiter_that_ran_out_waits_out_a_completion_already_running():
    """Second review of #74: a claim ends when its completion STARTS. A waiter that
    gives up while that completion runs must still wait for it to end: its first text
    would retire the waiter's freshly armed trigger unread."""
    gate = _gate(quiet_secs=0.02)
    assert await gate.wait_until_idle(max_wait=0.5, turn_free=True)  # someone's claim
    await _feed(gate, LLMFullResponseStartFrame())  # ...its completion started: claim gone
    waiter = asyncio.ensure_future(gate.wait_out_claim(max_wait=2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done(), "went ahead while a completion was running"
    await _feed(gate, LLMFullResponseEndFrame())
    await asyncio.wait_for(waiter, timeout=0.5)


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
        await r.task.queue_frames([EndFrame()])
        await asyncio.wait_for(r._run, 5)
        assert frame not in r.top.up, "a held answer went on into a closing session"


def main():
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
