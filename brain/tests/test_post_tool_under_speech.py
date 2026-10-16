#
# #17: the answer to a tool call, spoken over a caller who has started talking again.
#
# Live 2026-09-04 (pipecat 1.7, Talk): a web_search call, its "Looking up ..." filler
# playing, the tool's result landing during it. pipecat's LLMAssistantAggregator defers
# the post-tool completion to BotStoppedSpeaking, and gates that push -- and the one it
# makes at once when the bot is silent -- on its own `_user_speaking`, which is the user
# TURN (UserStartedSpeakingFrame), not the VAD. The turn starts only once the caller's
# words are transcribed, so the completion is asked for while they are already talking,
# against a context that does not have their words. The reply plays as their speech
# ends, their words then cut it, and their turn asks the model again: the stale answer
# heard over them, then the question answered a second time.
#
# The rig is the session itself: build_agent_session builds every processor, the real
# SIP output transport for the phone, a real BaseOutputTransport for Talk, and the test
# runs that pipeline from the user aggregator down -- the user aggregator with the
# session's own start strategy, the fence, the corrector, TurnCommitMark, the brain's
# BoundedOpenAILLMService, FollowupTrigger, the guard, EngineTTSService (its
# _hold_for_user_speech included), ReplyHoldGate on SIP, BargeInPauser on SIP, the
# transport, FollowupGate, the assistant aggregator (drop_overtaken_tool_calls) -- under
# the session's TranscriptLedger. Scripted: the LLM's stream (_open_stream: a tool call,
# then text), the engine's synthesis (_synth_text), the stop strategy's verdict (the
# test commits the turn), the caller (VAD edges and transcripts sent in above the user
# aggregator), and the Talk relay's socket.
# Every step waits on an event the code under test produces. The one bounded wait is
# for what must NOT happen: the stale reply's audio after the caller stops.
#
# Run: python test_post_tool_under_speech.py   (or via the suite)
#
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace as NS

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

import numpy as np  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import (  # noqa: E402
    BaseUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402
from pipecat.transports.base_output import BaseOutputTransport  # noqa: E402
from pipecat.transports.base_transport import TransportParams  # noqa: E402

from teaport_brain import tools  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.engine_tts import _SAMPLE_RATE  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402
from teaport_brain.sip_transport import SipGatewayTransport, make_sip_params  # noqa: E402
from test_service_unusable import _Stub  # noqa: E402
from test_sip_output_framing import _Wire  # noqa: E402

STARTUP_TIMEOUT_S = 10.0
STEP_TIMEOUT_S = 8.0
# The caller's words land this long after their VAD stop: the engine returns nothing for
# speech that began over the bot until the stop flushes it, and the final then takes
# ~0.5-0.7 s (reply_hold.RELEASE_SECS). Under TEAPORT_BARGE_PAUSE_RESUME_S (0.8), as
# live. Whatever plays in that gap -- the stale reply's first audio -- has no event of
# its own when it does NOT happen: this is that bounded wait, many times the fake
# engine's turnaround (~10 ms) and the TTS's path to the transport.
WORDS_LAG_S = 0.5
# How long the caller's resumed speech lasts, VAD start to stop: an utterance, not a
# blip (barge_pause resumes a pause on speech under 250 ms after 0.3 s).
SPEECH_S = 0.6
WORD_SECS = 0.3               # synthesized audio per word: a filler of 4 words is 1.2 s
QUESTION = "Can you look up the population of Lima?"
FILLER_ARGS = {"query": "Lima population"}
# What each completion after the tool call says first: the post-tool answer (asked
# without the caller's new words) or the answer to their turn (asked with them).
POST, TURN = "Post", "Turn"


async def _segments(text: str, eager: bool = True):
    """The engine's synthesis of one clause: a tone, WORD_SECS per word, word times."""
    words = text.split()
    secs = WORD_SECS * max(1, len(words))
    n = int(secs * _SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
    audio = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    await asyncio.sleep(0.01)
    timed = [(w, i * WORD_SECS) for i, w in enumerate(words)]
    yield "audio", audio, timed
    yield "end", timed


class ScriptedStop(BaseUserTurnStopStrategy):
    """The endpointing verdict, when the test says so."""

    async def commit(self):
        await self.trigger_user_turn_stopped()


class TalkOutput(BaseOutputTransport):
    """pipecat's real output transport, the socket write stubbed and paced at real time
    as the Talk relay's (FastAPIWebsocketOutputTransport._write_audio_sleep) is."""

    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True,
                                         audio_out_sample_rate=_SAMPLE_RATE))
        self._next_send = 0.0

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame) -> bool:
        interval = len(frame.audio) / (2 * frame.sample_rate * frame.num_channels)
        now = time.monotonic()
        wait = max(0.0, self._next_send - now)
        await asyncio.sleep(wait)
        self._next_send = (time.monotonic() if wait == 0 else self._next_send) + interval
        return True


class TalkTransport:
    def __init__(self):
        self._input = _Stub(name="talk-in")
        self._output = TalkOutput()

    def input(self):
        return self._input

    def output(self):
        return self._output


def _chunk(content=None, tool_calls=None):
    return NS(usage=None, model=None, choices=[NS(finish_reason=None, delta=NS(
        content=content, tool_calls=tool_calls))])


class Rig:
    """One session's pipeline, from the user aggregator down, running."""

    def __init__(self, front_end: str, tool: str = "web_search"):
        self.front_end = front_end
        self.wire = None
        if front_end == "sip":
            self.wire = _Wire()
            transport = SipGatewayTransport(self.wire.connection, make_sip_params())
            session = build_agent_session(transport, reply_hold_enabled=True,
                                          front_end="sip")
        else:
            transport = TalkTransport()
            session = build_agent_session(transport, front_end=front_end)
        self.session = session
        inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
        procs = inner.processors
        first = next(i for i, p in enumerate(procs) if isinstance(p, LLMUserAggregator))
        # Its own processors only: the list ends with the inner pipeline's sink.
        self.procs = [p for p in procs[first:] if not type(p).__name__.startswith("Pipeline")]
        self.user = procs[first]
        self.assistant = next(p for p in self.procs if isinstance(p, LLMAssistantAggregator))
        self.gate = next(p for p in self.procs if isinstance(p, FollowupGate))
        self.output = transport.output()
        # The verdict is the test's: the session's start strategy stays, its stop is
        # scripted (set before the StartFrame, when the controller wires them).
        controller = self.user._user_turn_controller
        self.stop = ScriptedStop()
        controller._user_turn_strategies = UserTurnStrategies(
            start=controller._user_turn_strategies.start, stop=[self.stop])
        # The engine.
        session.tts._synth_text = _segments
        # The LLM: completion 1 calls web_search, every later one answers in words.
        self.llm = session.llm
        self.asked: list[list[dict]] = []
        self.completions = 0

        async def open_stream(context):
            self.asked.append([dict(m) for m in context.get_messages()])
            self.completions += 1
            n = self.completions
            if n == 1:
                async def stream():
                    yield _chunk(tool_calls=[NS(index=0, id="call_17", function=NS(
                        name=tool, arguments=json.dumps(
                            FILLER_ARGS if tool == "web_search" else {})))])
            else:
                turn = any(m.get("role") == "user" and "metro" in str(m.get("content"))
                           for m in self.asked[-1])
                tag = TURN if turn else POST
                async def stream():
                    yield _chunk(content=f"{tag} answer: Lima has ten million people.")
            return stream()
        self.llm._open_stream = open_stream
        # The tool, through the real wrapper (web_search's filler line, "Looking up ...";
        # get_current_time has none): its result is reported when the test releases it.
        self.release_result = asyncio.Event()

        async def handler(params):
            self._fire("tool called")
            await self.release_result.wait()
            await params.result_callback({"result": "Lima: about 10 million people"})
            self._fire("result sent")
        self.llm.register_function(tool, tools._wrap(tool, handler, lambda: "en-us"))
        # What the assistant aggregator handled, and what played (the words the
        # transport releases on its clock, below it).
        self.events: dict = {}
        self.played: list[str] = []
        self.agg_seen: list[str] = []
        self.at_result = None
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            name = type(frame).__name__
            self.agg_seen.append(name)
            self._fire(name)
            if isinstance(frame, FunctionCallResultFrame):
                # What pipecat decided there: deferred to the bot's stop, or pushed now
                # unless the user's TURN is open.
                self.at_result = dict(
                    deferred=self.assistant._push_context_on_bot_stopped_speaking,
                    user_turn_open=self.assistant._user_speaking)
            if isinstance(frame, TTSTextFrame):
                self.played.append(frame.text)
                self._fire("word:" + frame.text)
        self.assistant.process_frame = recording
        self.task = PipelineTask(Pipeline(self.procs), observers=[session.ledger],
                                 cancel_on_idle_timeout=False)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    def _fire(self, key):
        ev = self.events.setdefault(key, asyncio.Event())
        ev.set()

    def event(self, key):
        return self.events.setdefault(key, asyncio.Event())

    async def wait_for(self, key, timeout=STEP_TIMEOUT_S):
        await asyncio.wait_for(self.event(key).wait(), timeout)

    def clear(self, key):
        self.event(key).clear()

    async def __aenter__(self):
        if self.wire is not None:
            self.wire.start()
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STARTUP_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc):
        self.release_result.set()
        await self.task.cancel()
        await self._run
        if self.wire is not None:
            await self.wire.stop()

    async def send(self, *frames):
        await self.task.queue_frames(list(frames))

    async def say(self, text, final=True):
        f = (TranscriptionFrame if final else InterimTranscriptionFrame)(text, "caller", "t")
        if final:
            f.finalized = True
        await self.send(f)

    # -- the conversation

    async def ask_the_question(self):
        """The caller's question, committed; the model calls web_search; the filler
        starts playing."""
        await self.send(VADUserStartedSpeakingFrame())
        await self.say(QUESTION)
        await self.send(VADUserStoppedSpeakingFrame())
        await self.wait_for("UserStartedSpeakingFrame")
        await self.stop.commit()
        await self.wait_for("BotStartedSpeakingFrame")          # the filler
        assert self.completions == 1

    def assert_nothing_held(self, where=""):
        """Every answer the gate held went somewhere: dropped for a turn, or on. One
        still held at the end would play a cap later, into whatever came next."""
        assert self.gate._held_answer is None, f"{where}: an answer is still held"
        assert self.gate.answers_held <= self.gate.answers_dropped + self.gate.answers_released, (
            f"{where}: held {self.gate.answers_held}, dropped {self.gate.answers_dropped}, "
            f"released {self.gate.answers_released}")

    def answers_played(self):
        return [t for t in (POST, TURN) if any(w.startswith(t) for w in self.played)]

    def asked_after_the_result(self):
        return [m for m in self.asked
                if any(x.get("role") == "tool" and "10 million" in str(x.get("content"))
                       for x in m)]


async def _until(predicate, timeout=STEP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.005)


async def _resume_over(rig: Rig, shape: str, words_before_stop: bool):
    """The caller starts talking again around the tool result, then their words come:
    WORDS_LAG_S after their VAD stop (`words_before_stop` False), or while they talk.

    The result is queued behind the filler's audio at the output transport, so the
    assistant aggregator sees it once the filler has played. Shapes:
      "paused"     they start mid-filler: on SIP the barge-in pauser pauses the filler,
                   and the result waits behind it.
      "filler_end" the filler plays out under their speech (on SIP they start inside
                   its last 0.5 s, where the pauser does not pause), and the result
                   lands while they are still talking.
      "after"      the filler has played out, they start, the result lands.
      "after_stop" the filler has played out, they speak and stop, and the result
                   lands between their VAD stop and their words (always words after
                   the stop) -- what the TTS's speech hold cannot see at all."""
    await rig.ask_the_question()
    if shape == "after_stop":
        await rig.wait_for("BotStoppedSpeakingFrame")
        rig.vad_started = time.monotonic()
        await rig.send(VADUserStartedSpeakingFrame())
        await asyncio.sleep(max(0.0, rig.vad_started + SPEECH_S - time.monotonic()))
        await rig.send(VADUserStoppedSpeakingFrame())
        rig.clear("UserStartedSpeakingFrame")
        rig.release_result.set()
        await rig.wait_for("FunctionCallResultFrame")
        try:
            await rig.wait_for("word:" + POST, timeout=WORDS_LAG_S)
        except asyncio.TimeoutError:
            pass
        await rig.say("what about the metro area of Lima")
        await rig.wait_for("UserStartedSpeakingFrame")
        await rig.stop.commit()
        await rig.wait_for("word:" + TURN)
        return
    if shape == "after":
        await rig.wait_for("BotStoppedSpeakingFrame")
    elif shape == "filler_end" and rig.front_end == "sip":
        await _until(lambda: 0 < (rig.output.playout_tail_secs() or 0) <= 0.4)
    rig.vad_started = time.monotonic()
    await rig.send(VADUserStartedSpeakingFrame())
    rig.release_result.set()
    await rig.wait_for("result sent")
    if shape != "paused":
        await rig.wait_for("FunctionCallResultFrame")
    rig.clear("UserStartedSpeakingFrame")
    await asyncio.sleep(max(0.0, rig.vad_started + SPEECH_S - time.monotonic()))
    if words_before_stop:
        await rig.say("what about", final=False)
        await rig.wait_for("UserStartedSpeakingFrame")
        await rig.send(VADUserStoppedSpeakingFrame())
    else:
        await rig.send(VADUserStoppedSpeakingFrame())
        try:
            await rig.wait_for("word:" + POST, timeout=WORDS_LAG_S)
        except asyncio.TimeoutError:
            pass
    await rig.say("what about the metro area of Lima")
    await rig.wait_for("UserStartedSpeakingFrame")
    await rig.stop.commit()
    await rig.wait_for("word:" + TURN)


# Which shapes each front-end has: the barge-in pauser exists only on SIP, so only there
# can the caller start mid-filler and have it paused; elsewhere the filler plays out
# under them ("filler_end").
SHAPES = [("sip", "paused"), ("sip", "filler_end"), ("sip", "after"), ("sip", "after_stop"),
          ("talk", "filler_end"), ("talk", "after"), ("talk", "after_stop"),
          ("room", "after")]


async def _check_one_answer(front_end, shape, words_before_stop):
    async with Rig(front_end) as rig:
        await _resume_over(rig, shape, words_before_stop)
        where = f"{front_end}/{shape}/words {'before' if words_before_stop else 'after'} the stop"
        assert POST not in rig.answers_played(), (
            f"{where}: the answer asked under the caller's speech played before their "
            f"words (played: {rig.answers_played()})")
        assert TURN in rig.answers_played(), f"{where}: the caller's own turn went unanswered"
        asked = rig.asked_after_the_result()
        assert len(asked) == 1, (
            f"{where}: the tool's result was answered {len(asked)} times, not once")
        rig.assert_nothing_held(where)


async def test_an_answer_due_while_the_caller_talks_is_answered_once_after_their_words():
    """#17, every front-end and shape: the caller starts talking again around the tool
    result, and their words come after their VAD stop (the common case: speech that
    began over the bot is transcribed only once the stop flushes it) or while they talk.
    Nothing asked under their speech plays, and the result is answered once -- by their
    own turn, which reads it in the context."""
    for front_end, shape in SHAPES:
        for words_before_stop in ((False,) if shape == "after_stop" else (False, True)):
            await _check_one_answer(front_end, shape, words_before_stop)
            print(f"    ok {front_end}/{shape}/words_before_stop={words_before_stop}")


async def test_with_nobody_talking_the_answer_plays_at_once():
    """Nothing over-suppresses: no caller speech, and the answer is asked the moment the
    result lands -- not after any release window -- and plays."""
    for front_end in ("sip", "talk"):
        async with Rig(front_end) as rig:
            await rig.ask_the_question()
            await rig.wait_for("BotStoppedSpeakingFrame")       # the filler is over
            rig.release_result.set()
            await rig.wait_for("FunctionCallResultFrame")
            landed = time.monotonic()
            await _until(lambda: rig.completions == 2)
            asked_after = time.monotonic() - landed
            await rig.wait_for("word:" + POST)
            assert asked_after < 0.2, (
                f"{front_end}: the answer waited {asked_after:.2f}s with nobody talking")
            assert len(rig.asked_after_the_result()) == 1
            assert rig.gate.answers_held == 0
            rig.assert_nothing_held(front_end)


async def test_a_wordless_sound_lets_the_answer_play_once_it_is_over():
    """A cough while the result lands: no words, so no turn will read the result. The
    answer is held for the caller's words and then plays, once."""
    for front_end in ("sip", "talk"):
        async with Rig(front_end) as rig:
            await rig.ask_the_question()
            await rig.wait_for("BotStoppedSpeakingFrame")
            rig.vad_started = time.monotonic()
            await rig.send(VADUserStartedSpeakingFrame())
            rig.release_result.set()
            await rig.wait_for("FunctionCallResultFrame")
            await asyncio.sleep(max(0.0, rig.vad_started + SPEECH_S - time.monotonic()))
            await rig.send(VADUserStoppedSpeakingFrame())
            stopped = time.monotonic()
            await rig.wait_for("word:" + POST)
            assert len(rig.asked_after_the_result()) == 1
            assert rig.gate.answers_released == 1
            rig.assert_nothing_held(front_end)
            print(f"    {front_end}: the answer played {time.monotonic() - stopped:.2f}s "
                  "after the cough ended")


async def test_a_commit_made_while_the_caller_still_talks_does_not_take_their_rest():
    """pipecat guards only the finalize on the caller speaking, not the commit. One made
    while they are still talking (a turn opened on their first words, the strategy
    concluding before their VAD stop) does not have the rest of what they say. Its reply
    calls a tool with no filler, the finalize refused is re-applied at their VAD stop,
    and the result lands then: the answer must wait for the rest of their words, which
    start a turn of their own. On 0db54f6 the gate let go of that speech at the commit
    (its VAD START was older), and the answer played before the words."""
    for front_end in ("talk", "sip"):
        async with Rig(front_end, tool="get_current_time") as rig:
            started = time.monotonic()
            await rig.send(VADUserStartedSpeakingFrame())
            await rig.say(QUESTION)
            await rig.wait_for("UserStartedSpeakingFrame")
            await rig.stop.commit()                       # the VAD still hears them
            await rig.wait_for("tool called")
            await asyncio.sleep(max(0.0, started + SPEECH_S - time.monotonic()))
            rig.clear("UserStartedSpeakingFrame")
            await rig.send(VADUserStoppedSpeakingFrame())
            stopped = time.monotonic()
            await rig.wait_for("UserStoppedSpeakingFrame")   # the re-applied finalize
            # The result lands now. (On SIP the reply gate, armed by the commit with the
            # caller speaking, holds the tool round itself until their words or its
            # release; the words come first, WORDS_LAG_S after the stop, as live.)
            rig.release_result.set()
            try:
                await rig.wait_for("word:" + POST,
                                   timeout=max(0.0, stopped + WORDS_LAG_S - time.monotonic()))
            except asyncio.TimeoutError:
                pass
            await rig.say("what about the metro area of Lima")
            await rig.wait_for("UserStartedSpeakingFrame")
            await rig.stop.commit()
            await rig.wait_for("word:" + TURN)
            assert POST not in rig.answers_played(), (
                f"{front_end}: the answer played before the rest of the caller's words "
                f"(played: {rig.answers_played()})")
            # Once on Talk; on SIP not at all: the reply gate drops the held reply's tool
            # round with it, and the turn is answered whole (NoLateToolRound).
            assert len(rig.asked_after_the_result()) == (1 if front_end == "talk" else 0), (
                f"{front_end}: the result was answered "
                f"{len(rig.asked_after_the_result())} times")
            assert TURN in rig.answers_played(), f"{front_end}: the turn went unanswered"
            rig.assert_nothing_held(front_end)


def main():
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
