#
# #221: the room says goodbye but does not sleep.
#
# 2026-10-24 05:53, a room session (wake words): to end it the caller said, in four
# turns, "Okay, good night.", "Good night.", "You didn't go to sleep." and "Go to
# sleep.". gpt-oss-120b answered each with a goodbye ("Good night—take care!", "Going to
# sleep now. Good night.") and did not call end_conversation, so the box SAID it was
# going to sleep and kept listening. It called the tool on the fifth turn.
#
# Now (goodbye.py): a reply that is only a goodbye, heard out, in a turn that did not
# call end_conversation, sends the room to sleep the tool's own way -- the brain calls
# end_conversation on the bridge with "said": true, and the bridge sleeps once its
# speaker is quiet (test_local_audio.py has that half). So the first of those four
# turns ends it.
#
# The goodbye itself (is_goodbye), the backstop's guards on fakes, and the session it
# runs in: build_agent_session's room session, run from the user aggregator down as
# test_owed_session.py runs Talk's -- the real LLM service, guard, TTS, a real output
# transport paced at real time, FollowupGate, the assistant aggregator, under the
# session's TranscriptLedger (whose reply end feeds the backstop). Scripted: the
# provider, the engine's synthesis, the stop verdict, the caller, and the bridge's
# answer to the call (consult_bridge, as the serializer routes it). The texts are
# invented in the shape of the 2026-10-24 ones.
#
# The sleep phrase (goodbye.SleepPhrase): "go to sleep" said by the caller sends the room
# to sleep itself, with no model asked -- on a fake voice, and in the room session from
# its fixed phrases down, with no model at all and with one that only errors. And the
# backstop sleeps on the caller's own sign-off when all that is owed is failures.
#
# Run: python test_goodbye.py   (or via the suite)
#
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from types import SimpleNamespace as NS  # noqa: E402

from openai.types.chat import ChatCompletionChunk  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStoppedSpeakingFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
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
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain import consult_bridge, goodbye, i18n, owed, tools, wifi_voice  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.followup_gate import FollowupGate  # noqa: E402
from teaport_brain.taken_turn import TakenTurnCloser  # noqa: E402
from teaport_brain.goodbye import BYE, SENT, SLEEP, GoodbyeSleep, asks, goodbye_kind, is_goodbye  # noqa: E402
from test_post_tool_under_speech import ScriptedStop, TalkTransport, _segments  # noqa: E402

STARTUP_TIMEOUT_S = 10.0
STEP_TIMEOUT_S = 10.0

# The four turns, in the 2026-10-24 shape: the caller's words, the model's goodbye.
TURNS = [("Okay, good night.", "Good night—take care!"),
         ("Good night.", "Good night!"),
         ("You didn't go to sleep.", "Going to sleep now. Good night."),
         ("Go to sleep.", "Okay, going to sleep. Good night!")]


# ------------------------------------------------------------------ the goodbye

GOODBYES = [t for _, t in TURNS] + [
    "Good night!", "Goodnight! 🌙", "**Good night.**", "Night night!", "Bye!",
    "Bye-bye for now!", "Goodbye!", "Sleep well!", "Sweet dreams.", "Take care!",
    "See you tomorrow!", "Talk to you later. Take care!", "Have a good night!",
    "Have a great night's sleep!", "Good night to you too!", "And a very good night to you!",
    "Good night – take care.", "Good night! Sleep well and sweet dreams.",
    "Alright, I'll go to sleep now. Good night!", "Okay. Going back to sleep.",
    "Sleeping now.", "Okay, I'll stop listening now. Good night.",
    "Thanks, good night!", "You're welcome, good night!",
    "It was nice talking with you. Good night!",
    "Good night! I'm here if you need me.",
    "Good night! Just say the wake word if you need me.",
    "Good night, Sam!", "Good night, Mary Ann!", "Good night, my friend.",
    "Sorry about that! Going to sleep now.", "You're right, good night!",
    # common sign-offs (review round 1)
    "Thanks for chatting—good night!",
    "Good night! If you need anything, just say my name.",
    "Good night! I'm always here if you need me.",
    "Good night! Feel free to call me anytime.",
    "Good night! Sleep soundly.", "Good night! Wishing you sweet dreams.",
    "Good night, and stay safe.",
]
NOT_GOODBYES = [
    # a goodbye inside a longer answer, or before one
    "Good night! Your alarm is set for seven.",
    "Sure, the weather tomorrow is sunny. Good night!",
    "Before you sleep, drink some water. Good night!",
    "I can't sleep, but good night!",
    "Good night, take care, and remember the meeting at nine.",
    "Here's a bedtime story: once upon a time there was a fox. Good night.",
    "Good night, Moon, said the little bunny in the great green room.",
    # mid-sentence, quoted, or asked
    "Good night is a song by the Beatles.",
    "\"Good night\" in Japanese is \"oyasumi\".",
    "Going to bed already?", "Good night?", "You should go to sleep.",
    "I'm not going to sleep.",
    # a short clause beside a goodbye is no name (review round 1)
    "Bye, reminder set!", "Good night, alarm set", "Good night, door locked.",
    "Good night, and lock up.", "Have a nice day, and drink water", "Good night, kids!",
    "Good night, Sam and Alex!", "Done, good night.",
    # "later" alone defers, it does not take leave
    "Not now, later.", "Sure, later.", "Later!",
    # the small talk around a goodbye, without one
    "Okay.", "Thanks!", "I'm here if you need me.", "I'm always here if you need me.",
    "Feel free to call me anytime.", "Stay safe!",
    # another language: English clauses only
    "Bonne nuit !", "Buenas noches.",
    "", "   ",
]


def test_a_reply_that_is_only_a_goodbye_is_one():
    missed = [t for t in GOODBYES if not is_goodbye(t)]
    assert not missed, f"goodbyes not heard as one: {missed}"


def test_a_goodbye_inside_an_answer_quoted_or_asked_is_none():
    taken = [t for t in NOT_GOODBYES if is_goodbye(t)]
    assert not taken, f"heard as a goodbye: {taken}"


# Questions about goodbye words, answered with one (review round 3): no leave-taking.
ABOUT_GOODBYE_WORDS = [("What's another word for goodbye?", "Farewell!"),
                       ("What's a synonym for goodbye?", "Farewell."),
                       ("What's a more formal goodbye?", "Farewell."),
                       ("What's a nicer way to say goodbye?", "Take care!"),
                       ("How should I say good night to my kids?", "Sweet dreams!"),
                       ("Is it goodnight or good night?", "Good night."),
                       ("How do you spell goodbye?", "Goodbye.")]

# The box asked to go, as a question, and a plain goodbye back: #221's own shape.
ASKED_TO_GO = [("Can you go to sleep?", "Good night—take care!"),
               ("Could you go to sleep now?", "Good night!"),
               ("Why are you still awake?", "Sorry about that! Good night."),
               ("Can you stop listening?", "Okay. Goodbye!")]


def test_a_goodbye_that_says_the_box_sleeps_is_told_apart():
    for text in ("Going to sleep now. Good night.", "Okay, I'll stop listening now.",
                 "Sorry about that! Going to sleep now.", "Okay, going to sleep. Good night!"):
        assert goodbye_kind(text) == SLEEP, text
    for text in ("Good night—take care!", "Goodbye!", "See you!", "Sleep well!"):
        assert goodbye_kind(text) == BYE, text


def test_what_counts_as_the_caller_asking():
    for said in ("How do you say adiós in English?", "What's the opposite of hello?",
                 "Can you do that later?", "Can you remind me in 5 minutes?",
                 "can you remind me in five minutes", "Okay, what time is it",
                 "Is that all for tonight",
                 # a word asked for, in any form (review round 2)
                 "Translate adiós into English.", "Tell me the opposite of hello.",
                 "What's the word for adiós", "What does goodbye mean?",
                 "How do you say good night in French?",
                 # and without a question mark the STT may drop (second-family review)
                 "Repeat after me: good night.", "Name a word that means goodbye.",
                 "I need a word that means goodbye.", "Does adios mean goodbye"):
        assert asks(said), said
    # questions ABOUT goodbye words ask for a word (review round 3)
    for said, _ in ABOUT_GOODBYE_WORDS:
        assert asks(said), said
    # a question that asks the box to go is answered by a goodbye (review round 2), and
    # so is a turn that is only a goodbye, "?" or not (review round 3)
    for said, _ in ASKED_TO_GO:
        assert not asks(said), said
    for said in ("Okay, good night?", "Good night?", "Bye?",
                 "I just wanted to say good night.", "I mean, good night."):
        assert not asks(said), said
    for caller, _ in TURNS:                      # #221's four turns: none is a question
        assert not asks(caller), caller
    for said in ("Have a good night.", "Will do, good night.", "Thanks, that's all.",
                 "Bye for now.", "Stop listening."):
        assert not asks(said), said


def test_a_goodbye_longer_than_a_line_is_an_answer():
    assert not is_goodbye(" ".join(["Good night!"] * (goodbye.MAX_WORDS // 2 + 1)))


def test_only_an_english_voice_counts():
    assert goodbye.english("en-us") and goodbye.english("en-gb") and goodbye.english("EN")
    for code in ("fr-fr", "es", "pt-br", "it", "hi", "ja", "cmn", "", None):
        assert not goodbye.english(code), code


# ------------------------------------------------------------------ the backstop, on fakes

class _Gate:
    """FollowupGate's reads, set by the test. `speech`: an Event set when the caller's
    speech is over (speech_over), `cap` whether the hold ran to its cap instead."""

    def __init__(self, **kw):
        self.consults_running = 0
        self.calls_out = self.held = False
        self.caller_request_pending = self.turn_in_flight = False
        self.caller_speaking = False
        self.speech = None
        self.cap = False
        self.ended = False
        self.__dict__.update(kw)

    async def speech_over(self):
        if self.speech is not None:
            await self.speech.wait()
        return not (self.cap or self.ended)


class _Owed:
    """owed.Owed's owed(): records with a `failure` (None: a real answer to tell)."""

    def __init__(self, records=()):
        self.records = [NS(failure=r if r in _FAILURES else None) for r in records]

    def owed(self):
        return list(self.records)


class _Context:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self):
        return self.messages


class _LLM:
    def __init__(self):
        self.pushed, self.tasks = [], []

    async def push_frame(self, frame, *a, **kw):
        self.pushed.append(frame)

    def create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task


def _turn(caller, called=None):
    """A conversation that has just had `caller`'s turn (and, `called`, a tool call)."""
    msgs = [{"role": "system", "content": "You are teaport."},
            {"role": "user", "content": "What's the time?"},
            {"role": "assistant", "content": "It's ten past eleven."},
            {"role": "user", "content": caller}]
    if called:
        msgs += [{"role": "assistant", "content": None, "tool_calls": [
                     {"id": "fc_1", "type": "function",
                      "function": {"name": called, "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "fc_1", "content": json.dumps({"ok": True})}]
    return msgs


def _reply(kind=owed.COMMIT, calls=0):
    r = owed.Reply(kind)
    r.calls = calls
    return r


def _sent(text, *, messages=None, reply=None, gate=None, book=None, lang="en-us",
          heard=None, cut=False, audio=True, again=None):
    """Feed one reply end to a backstop, answer the call as the bridge would, and return
    the messages it sent (and the backstop)."""
    async def run():
        llm = _LLM()
        sleep = GoodbyeSleep(context=_Context(messages or _turn(TURNS[0][0])),
                             owed=book or _Owed(), gate=gate or _Gate(),
                             lang_fn=lambda: lang, llm=llm)
        for t in [text] + ([again] if again else []):
            sleep.reply_end(reply or _reply(), t if heard is None else heard, cut=cut,
                            audio=audio, text=t)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if llm.pushed or not llm.tasks:
                break
        for f in llm.pushed:
            assert consult_bridge.resolve(f.message["call_id"],
                                          {"ok": True, "note": "The box goes back to sleep now."})
        await asyncio.gather(*llm.tasks)
        return [f.message for f in llm.pushed], sleep
    return asyncio.run(run())


def test_221_each_goodbye_of_the_four_turns_sends_the_room_to_sleep():
    for caller, said in TURNS:
        sent, sleep = _sent(said, messages=_turn(caller))
        assert len(sent) == 1 and sleep.sent, (caller, said, sent)
        m = sent[0]
        assert m["type"] == "client_tool" and m["name"] == "end_conversation", m
        assert m["args"] == {} and m["said"] is True, m
        assert m["call_id"].startswith(tools.CLIENT_CALL_PREFIX), m


def test_the_room_is_sent_to_sleep_once_per_session():
    sent, _ = _sent(TURNS[0][1], again=TURNS[2][1])
    assert len(sent) == 1, sent


def test_a_turn_that_called_end_conversation_sends_nothing_more():
    """The model did it right: its own call armed the bridge, and its goodbye is the
    answer to the call's result."""
    sent, sleep = _sent("Good night!", messages=_turn("Go to sleep.", called="end_conversation"),
                        reply=_reply(owed.POST))
    assert sent == [] and not sleep.sent


def test_the_answer_after_another_tool_can_be_the_goodbye():
    sent, _ = _sent("Okay, good night!", messages=_turn("Quieter, and good night.",
                                                        called="set_volume"),
                    reply=_reply(owed.POST))
    assert len(sent) == 1, sent


def test_what_keeps_the_room_awake():
    cases = {
        "a goodbye inside an answer": dict(text="Good night! Your alarm is set for seven."),
        "the caller cut it": dict(cut=True, heard="Good night—"),
        "it never played": dict(audio=False, heard=""),
        "a consult still running": dict(gate=_Gate(consults_running=1)),
        "an answer still owed": dict(book=_Owed(["the weather"])),
        "a failure owed, the caller no sign-off": dict(
            book=_Owed(["error"]), messages=_turn("You didn't go to sleep.")),
        "an answer and a failure owed": dict(book=_Owed(["error", "the weather"])),
        "a tool call out": dict(gate=_Gate(calls_out=True)),
        "on hold for a phone call": dict(gate=_Gate(held=True)),
        "the caller's next turn": dict(gate=_Gate(caller_request_pending=True)),
        "a completion under way": dict(gate=_Gate(turn_in_flight=True)),
        "it called a tool as well": dict(reply=_reply(calls=1)),
        "a run of ours": dict(reply=_reply(owed.RUN)),
        "a re-offer": dict(reply=_reply(owed.REOFFER)),
        "nothing the caller said": dict(messages=[{"role": "user", "content": "(The user woke you.)"}]),
        "a goodbye that answers a question": dict(
            messages=_turn("What's the opposite of hello?"), text="Goodbye!"),
        "a French voice": dict(lang="fr-fr"),
        "a Spanish voice": dict(lang="es"),
    }
    for why, kw in cases.items():
        text = kw.pop("text", TURNS[0][1])
        sent, sleep = _sent(text, **kw)
        assert sent == [] and not sleep.sent, f"{why}: the room was sent to sleep"
        assert sleep.decisions and sleep.decisions[-1] != SENT, (why, sleep.decisions)


_FAILURES = ("error", "timeout", "empty", "unknown")


def test_only_failures_owed_and_the_caller_signed_off_sleeps():
    """A consult that ended in an error is still owed, and the caller says "Okay, go to
    sleep." / "Good night.": the failure is nothing to wait for, and they asked to go.
    It stays owed for the next wake (the room's sleep keeps the records)."""
    for caller in ("Okay, go to sleep.", "No, go to sleep.", "Okay, good night.", "Bye!",
                   "Can you go to sleep?"):
        sent, sleep = _sent("Goodbye.", messages=_turn(caller), book=_Owed(["error"]))
        assert len(sent) == 1 and sleep.decisions[-1] == SENT, (caller, sleep.decisions)
        assert sent[0]["said"] is True
    # Still running, or a real answer beside the failure: the room stays awake.
    for kw in (dict(book=_Owed(["error"]), gate=_Gate(consults_running=1)),
               dict(book=_Owed(["error", "the weather"]))):
        sent, sleep = _sent("Goodbye.", messages=_turn("Okay, go to sleep."), **kw)
        assert sent == [] and sleep.decisions[-1] == "an answer is still owed", kw


def test_what_signs_off():
    en = i18n.get("en")
    for said in ("Okay, go to sleep.", "No, go to sleep.", "you can go back to sleep now",
                 "Okay, good night.", "Good night!", "Bye.", "Thanks, good night."):
        assert goodbye.signed_off(said, en), said
    for said in ("You didn't go to sleep.", "What's the weather?", "Good night?",
                 "How do I make you go to sleep?", "Thanks.", ""):
        assert not goodbye.signed_off(said, en), said
    assert goodbye.signed_off("Vete a dormir.", i18n.get("es"))   # the phrase, its language


def test_a_goodbye_answering_a_question_is_no_leave_taking():
    """Review rounds 1 and 2: the goodbye is the answer, not a leave-taking."""
    for caller, said in (("How do you say adiós in English?", "Goodbye."),
                         ("What's the opposite of hello?", "Goodbye!"),
                         ("Can you remind me in 5 minutes?", "See you!"),
                         ("Translate adiós into English.", "Goodbye."),
                         ("Tell me the opposite of hello.", "Goodbye!"),
                         *ABOUT_GOODBYE_WORDS):
        sent, sleep = _sent(said, messages=_turn(caller))
        assert sent == [] and sleep.decisions[-1] == "it answers a question", (caller, said)


def test_a_question_that_asks_the_box_to_go_sleeps_on_a_plain_goodbye():
    """Review round 2: "Can you go to sleep?" -- "Good night—take care!" is #221's own
    reply shape; the question asks for the goodbye, it does not ask for a word."""
    for caller, said in ASKED_TO_GO:
        sent, sleep = _sent(said, messages=_turn(caller))
        assert len(sent) == 1 and sleep.decisions[-1] == SENT, (caller, said, sleep.decisions)


def test_a_question_answered_with_going_to_sleep_still_sleeps():
    """Saying it goes to sleep and listening on is #221's harm itself, asked or not."""
    for caller, said in (("Can you go to sleep?", "Going to sleep now. Good night."),
                         ("Could you stop listening?", "Okay, I'll stop listening now.")):
        sent, sleep = _sent(said, messages=_turn(caller))
        assert len(sent) == 1 and sleep.decisions[-1] == SENT, (caller, said, sleep.decisions)


def _held(gate, then, messages=None):
    """A goodbye heard out while the caller is talking (`gate`): the sleep is held; `then`
    (gate, messages) happens; the caller's speech is over. The messages sent, and the
    backstop."""
    async def run():
        llm = _LLM()
        msgs = messages or _turn(TURNS[0][0])
        sleep = GoodbyeSleep(context=_Context(msgs), owed=_Owed(), gate=gate,
                             lang_fn=lambda: "en-us", llm=llm)
        gate.speech = asyncio.Event()
        sleep.reply_end(_reply(), TURNS[0][1], cut=False, audio=True, text=TURNS[0][1])
        assert sleep.holding and list(sleep.decisions) == ["held: the caller is talking"]
        assert not llm.pushed, "the room was sent to sleep under the caller's speech"
        await asyncio.sleep(0.01)
        then(gate, msgs)
        gate.speech.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not sleep.holding and (llm.pushed or sleep.decisions[-1] != SENT):
                break
        for f in llm.pushed:
            consult_bridge.resolve(f.message["call_id"], {"ok": True})
        await asyncio.gather(*llm.tasks)
        return [f.message for f in llm.pushed], sleep
    return asyncio.run(run())


def test_the_caller_talking_over_the_goodbye_holds_the_sleep():
    """Review round 1: "oh wait, one more thing" over the goodbye's end, no turn yet (its
    words not transcribed): the sleep waits for their speech, then is decided again."""
    def their_words_took_a_turn(gate, msgs):
        gate.caller_speaking, gate.caller_request_pending = False, True

    def their_words_were_committed(gate, msgs):
        gate.caller_speaking = False
        msgs.append({"role": "user", "content": "Oh wait, one more thing."})

    def they_talk_on(gate, msgs):
        gate.cap = True

    def they_start_again(gate, msgs):
        pass                                     # still caller_speaking after the clear

    def the_session_ends(gate, msgs):
        gate.ended = True

    for then, why in ((their_words_took_a_turn, "the conversation went on"),
                      (their_words_were_committed, "the conversation went on"),
                      (they_talk_on, "the caller was still talking at the hold's cap"),
                      (they_start_again, "the caller is talking again"),
                      (the_session_ends, "the session ended")):
        sent, sleep = _held(_Gate(caller_speaking=True), then)
        assert sent == [] and not sleep.sent, then.__name__
        assert sleep.decisions[-1] == why, (then.__name__, list(sleep.decisions))


def test_a_cough_over_the_goodbye_only_delays_the_sleep():
    """No words came of it (or the room's own echo): once the speech is over, it sleeps."""
    def no_words(gate, msgs):
        gate.caller_speaking = False

    sent, sleep = _held(_Gate(caller_speaking=True), no_words)
    assert len(sent) == 1 and sent[0]["said"] is True and sleep.decisions[-1] == SENT


# ------------------------------------------------------------------ the session

def _chunk(text=None, tool=None):
    delta = {"role": "assistant"}
    if text is not None:
        delta["content"] = text
    if tool is not None:
        delta["tool_calls"] = [{"index": 0, "id": f"call-{tool}", "type": "function",
                                "function": {"name": tool, "arguments": "{}"}}]
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c

    async def close(self):
        pass


class Provider:
    """The openai client's chat.completions: each reply is text, or ("call", name).
    `fails`: every request raises it instead (a provider out of credit)."""

    def __init__(self, replies, fails=None):
        self.requests: list = []
        self.replies = list(replies)
        self.fails = fails

    async def create(self, **params):
        self.requests.append(params)
        if self.fails is not None:
            raise self.fails
        reply = self.replies.pop(0) if self.replies else "Okay."
        if isinstance(reply, tuple):
            return _Stream([_chunk(tool=reply[1])])
        return _Stream([_chunk(text=reply)])


class Rig:
    """A room session with wake words (?features=sleep), from the user aggregator down.
    The client's side: every message the transport sends is kept (`sent`, with how many
    replies had stopped playing by then), and a client_tool call is answered at once, as
    the bridge answers it."""

    def __init__(self, replies, front_end="room", features=("sleep",), top=LLMUserAggregator,
                 fails=None, transport=None):
        transport = transport or TalkTransport()
        session = build_agent_session(transport, front_end=front_end,
                                      client_features=frozenset(features))
        self.session = session
        self.owed = session.owed
        self.owed.judge = self.owed.gist = self.owed.decline = None
        inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
        procs = inner.processors
        first = next(i for i, p in enumerate(procs) if isinstance(p, top))
        self.procs = [p for p in procs[first:] if not type(p).__name__.startswith("Pipeline")]
        self.user = next(p for p in self.procs if isinstance(p, LLMUserAggregator))
        self.assistant = next(p for p in self.procs if isinstance(p, LLMAssistantAggregator))
        self.gate = next(p for p in self.procs if isinstance(p, FollowupGate))
        controller = self.user._user_turn_controller
        self.stop = ScriptedStop()
        # The scripted verdict, after the closer of a turn a fixed phrase took.
        closers = [s for s in controller._user_turn_strategies.stop
                   if isinstance(s, TakenTurnCloser)]
        controller._user_turn_strategies = UserTurnStrategies(
            start=controller._user_turn_strategies.start, stop=[*closers, self.stop])
        session.tts._synth_text = _segments
        self.provider = Provider(replies, fails)
        if session.llm is not None:
            session.llm._client = NS(chat=NS(completions=self.provider))
            session.llm._warm_closed = True
        self.played: list = []
        self.bot_stops = 0
        self.sent: list = []
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            if isinstance(frame, TTSTextFrame):
                self.played.append(frame.text)
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.bot_stops += 1
        self.assistant.process_frame = recording
        output = transport.output()

        async def send_message(frame):
            m = frame.message
            self.sent.append((dict(m), self.bot_stops, list(self.played)))
            if m.get("type") == "client_tool":
                note = ("The box goes back to sleep now." if m.get("said") else
                        "Now say one short goodbye; the box goes back to sleep once it has played.")
                consult_bridge.resolve(m["call_id"], {"ok": True, "note": note})
        output.send_message = send_message
        self.task = PipelineTask(Pipeline(self.procs), observers=[session.ledger],
                                 cancel_on_idle_timeout=False)
        session.task = self.task   # the session's own lines go in here (own_line)
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STARTUP_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc):
        await self.task.cancel()
        await self._run

    async def final(self, text):
        """A final transcript, as the STT pushes one: what a fixed phrase hears."""
        f = TranscriptionFrame(text, "caller", "t")
        f.finalized = True
        await self.task.queue_frames([f])

    def calls(self):
        return [(m, stops, played) for m, stops, played in self.sent
                if m.get("type") == "client_tool"]

    async def say(self, text):
        stops = self.bot_stops
        await self.task.queue_frames([VADUserStartedSpeakingFrame()])
        f = TranscriptionFrame(text, "caller", "t")
        f.finalized = True
        await self.task.queue_frames([f, VADUserStoppedSpeakingFrame()])
        await until(lambda: self.user._user_turn_controller._user_turn)
        await self.stop.commit()
        return stops

    async def played_out(self, since):
        await until(lambda: self.bot_stops > since)
        await until(self.owed.is_settled)


async def until(predicate, timeout=STEP_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.01)


async def decided(rig, n=1):
    """The backstop's decision on the n-th reply it was fed (made at each reply's end:
    the ledger's settle feed), once it is final -- not a hold still waiting."""
    sleep = rig.session.goodbye_sleep
    await until(lambda: len(sleep.decisions) >= n and not sleep.holding)
    return sleep.decisions[-1]


async def test_221_the_first_goodbye_sends_the_room_to_sleep_once_it_has_played():
    """The caller's first "good night" and the model's goodbye without the tool: the
    brain calls end_conversation on the bridge, once the goodbye has played out."""
    caller, said = TURNS[0]
    async with Rig([said]) as rig:
        assert any(t["function"]["name"] == "end_conversation"
                   for t in (await _first_request(rig, caller)).get("tools") or ()), (
            "the room session was not offered end_conversation")
        assert await decided(rig) == SENT
        await until(lambda: rig.calls())
        (m, stops, played), = rig.calls()
        assert m["name"] == "end_conversation" and m["said"] is True and m["args"] == {}, m
        assert stops >= 1, "the call went out before the goodbye had played"
        assert " ".join(played) == said, f"the call went out before all of it played: {played}"
        assert rig.session.goodbye_sleep.sent


async def test_the_models_own_call_is_the_only_one():
    """The model did it right: end_conversation, then the goodbye. Its own call is the
    one the bridge gets (no "said": the goodbye is still to come), and the backstop
    stands down on the goodbye."""
    async with Rig([("call", "end_conversation"), "Good night!"]) as rig:
        await rig.say("Go to sleep.")
        sleep = rig.session.goodbye_sleep
        await until(lambda: "its turn called end_conversation" in sleep.decisions)
        assert SENT not in sleep.decisions and not sleep.sent, list(sleep.decisions)
        calls = rig.calls()
        assert len(calls) == 1, calls
        assert calls[0][0]["name"] == "end_conversation" and "said" not in calls[0][0]


async def test_a_goodbye_inside_an_answer_keeps_the_room_awake():
    async with Rig(["Good night! Your alarm is set for seven."]) as rig:
        await rig.say("Set an alarm for seven, and good night.")
        assert await decided(rig) == "not a goodbye"
        assert not rig.session.goodbye_sleep.sent and rig.calls() == []


async def test_a_goodbye_that_answers_a_question_keeps_the_room_awake():
    async with Rig(["Goodbye!"]) as rig:
        await rig.say("What's the opposite of hello?")
        assert await decided(rig) == "it answers a question"
        assert not rig.session.goodbye_sleep.sent and rig.calls() == []


async def _talking_over_the_goodbye(rig):
    """The caller's goodbye turn, and their speech starting over the model's goodbye
    (its VAD start, no words yet). Returns once the backstop holds the sleep."""
    caller, said = TURNS[0]
    await rig.say(caller)
    await until(lambda: rig.played)                  # the goodbye is playing
    await rig.task.queue_frames([VADUserStartedSpeakingFrame()])
    await until(lambda: rig.gate.caller_speaking)
    sleep = rig.session.goodbye_sleep
    await until(lambda: sleep.decisions)
    assert list(sleep.decisions) == ["held: the caller is talking"], list(sleep.decisions)
    assert sleep.holding and rig.calls() == [], "slept under the caller's speech"


async def test_the_caller_talking_over_the_goodbye_keeps_the_room_awake():
    """Review round 1: "oh wait, one more thing" over the goodbye's end. No turn has
    opened at its end (the words are not transcribed yet), so a turn-level guard slept
    under them. The sleep is held; their words take the turn; the room stays awake."""
    async with Rig([TURNS[0][1], "Sure, what is it?"]) as rig:
        await _talking_over_the_goodbye(rig)
        f = TranscriptionFrame("Oh wait, one more thing.", "caller", "t")
        f.finalized = True
        await rig.task.queue_frames([f, VADUserStoppedSpeakingFrame()])
        await until(lambda: rig.user._user_turn_controller._user_turn)
        await rig.stop.commit()
        sleep = rig.session.goodbye_sleep
        await until(lambda: not sleep.holding)
        assert sleep.decisions[-1] == "the conversation went on", list(sleep.decisions)
        assert not sleep.sent and rig.calls() == []
        await until(lambda: len(rig.provider.requests) >= 2)   # and their turn is answered


async def test_a_sound_over_the_goodbye_with_no_words_only_delays_the_sleep():
    """A cough, the room: speech the VAD heard, no words. Once the release window has
    passed with none, the held sleep goes."""
    async with Rig([TURNS[0][1]]) as rig:
        await _talking_over_the_goodbye(rig)
        await rig.task.queue_frames([VADUserStoppedSpeakingFrame()])
        await until(lambda: rig.calls())
        (m, _, _), = rig.calls()
        assert m["said"] is True and rig.session.goodbye_sleep.decisions[-1] == SENT


async def test_a_hold_ends_with_the_session_and_sends_nothing():
    """Review round 2: the session torn down while the sleep is held. The hold's wait ends
    with it (FollowupGate.speech_over), not at its cap after it: no dangling task, and
    nothing sent."""
    from loguru import logger
    seen: list = []
    sink = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        rig = Rig([TURNS[0][1]])
        async with rig:
            await _talking_over_the_goodbye(rig)
            started = time.monotonic()
        sleep = rig.session.goodbye_sleep
        await until(lambda: not sleep.holding, timeout=2.0)
        assert time.monotonic() - started < rig.gate._answer_max_secs, "held to its cap"
        assert sleep.decisions[-1] == "the session ended", list(sleep.decisions)
        assert not sleep.sent and rig.calls() == []
    finally:
        logger.remove(sink)
    dangling = [m for m in seen if "dangling" in m and "goodbye" in m]
    assert not dangling, dangling


async def test_the_gates_speech_wait_ends_with_the_session():
    gate = FollowupGate(answer_max_secs=30.0)
    gate._vad = gate._speech_pending = True
    gate._speech_changed()
    assert gate.caller_speaking and not gate.ended
    wait = asyncio.create_task(gate.speech_over())
    await asyncio.sleep(0.05)
    assert not wait.done()
    gate._end_interruption_waits()               # what End, Stop, Cancel and cleanup do
    assert await asyncio.wait_for(wait, 1.0) is False and gate.ended
    assert await gate.speech_over() is False     # and after it, at once


async def test_a_talk_session_or_a_room_without_wake_words_has_no_backstop():
    """Only where end_conversation is offered: the room's bridge with wake words."""
    for front_end, features in (("talk", ("sleep",)), ("room", ())):
        rig = Rig(["Good night!"], front_end=front_end, features=features)
        assert rig.session.goodbye_sleep is None, (front_end, features)
        assert rig.session.ledger.on_reply_end == rig.owed.settle, (front_end, features)
    assert Rig(["Good night!"]).session.goodbye_sleep is not None


# ------------------------------------------------------------------ the sleep phrase

class _Voice(wifi_voice.PhraseVoice):
    """The fixed phrases' processor without a pipeline: what it pushes is kept, and the
    box's speech is the test's to start and stop."""

    def __init__(self, lang="en-us"):
        super().__init__(lang_fn=lambda: lang, phrases=[goodbye.SleepPhrase()])
        self.pushed: list = []

    @property
    def sleep(self):
        return self.phrases[0]

    def said(self):
        return [f.text for f in self.pushed if isinstance(f, TTSSpeakFrame)]

    def sent(self):
        return [f.message for f in self.pushed
                if isinstance(f, OutputTransportMessageUrgentFrame)]

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)
        if isinstance(frame, OutputTransportMessageUrgentFrame):
            consult_bridge.resolve(frame.message["call_id"], {"ok": True})

    def _spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)

    async def speak(self, *, cut=False):
        """The line plays: the box's speech starts, (is cut,) and ends."""
        await asyncio.sleep(0.01)
        self.bot_starts += 1
        self._start_cuts = self.cuts
        self._bot_speaking = True
        if cut:
            self.cuts += 1
        await asyncio.sleep(0.1)
        self._bot_speaking, self._quiet_since = False, self._clock()


def test_the_phrase_says_its_line_and_sleeps_once_it_has_played():
    async def run():
        v = _Voice()
        assert await v._heard("Okay, go to sleep.")
        assert v.said() == [goodbye.SLEEP_TEXT] and v.sent() == [], "slept before the line"
        await asyncio.sleep(0.05)
        assert v.sent() == [], "slept before the line played"
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        (m,) = v.sent()
        assert m["type"] == "client_tool" and m["name"] == "end_conversation", m
        assert m["args"] == {} and m["said"] is True, m
        assert v.sleep.sent and list(v.sleep.decisions) == [SENT]
        # Said again: taken, nothing more.
        assert await v._heard("go to sleep") and len(v.sent()) == 1 and len(v.said()) == 1
    asyncio.run(run())


def test_the_phrase_in_the_voices_language():
    async def run():
        v = _Voice("es")
        assert await v._heard("Vale, vete a dormir.")
        assert v.said() == [i18n.get("es")._(goodbye.SLEEP_TEXT)]
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
    asyncio.run(run())


def test_talk_about_sleep_is_not_the_phrase():
    async def run():
        v = _Voice()
        for talk in ("How do I make you go to sleep?", "I need to go to sleep",
                     "Don't go to sleep.", "Tell the kids to go to sleep.",
                     "go to sleep and then remind me at seven"):
            assert not await v._heard(talk), talk
        assert v.said() == [] and v.sent() == []
    asyncio.run(run())


def test_the_caller_cutting_the_line_or_going_on_keeps_the_room_awake():
    async def run():
        v = _Voice()
        assert await v._heard("go to sleep")
        await v.speak(cut=True)
        await until(lambda: v.sleep.decisions, timeout=2.0)
        assert list(v.sleep.decisions) == ["the caller cut the line"] and v.sent() == []
        assert not v.sleep.sent
        # Words of theirs before the line ends: the conversation goes on (to the model).
        v = _Voice()
        assert await v._heard("go to sleep")
        assert not await v._heard("oh wait, what's the weather tomorrow?")
        await v.speak()
        await asyncio.sleep(0.1)
        assert list(v.sleep.decisions) == ["the caller went on"] and v.sent() == []
        # And asked again after, it goes.
        assert await v._heard("okay, go to sleep now")
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        # An interruption before the line's speech started cut the reply the caller's
        # words talked over, not the line: it sleeps.
        v = _Voice()
        assert await v._heard("go to sleep")
        v.cuts += 1
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
    asyncio.run(run())


def test_asked_again_over_the_line_the_room_still_sleeps():
    """#389: "go to sleep" again while the line plays is the same request, not the
    caller going on: a cut of the line that follows it (the repeat's own words, judged
    over the line) says the line again, and the room sleeps. Only other words keep it
    awake."""
    async def run():
        # Asked again, then the line is cut: said again, and it sleeps.
        v = _Voice()
        assert await v._heard("go to sleep")
        assert await v._heard("Go to sleep.")
        await v.speak(cut=True)
        await until(lambda: len(v.said()) == 2, timeout=2.0)
        assert v.said() == [goodbye.SLEEP_TEXT] * 2 and v.sent() == []
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        assert v.sleep.sent and list(v.sleep.decisions) == [SENT]
        # The repeat's words cut the line before their final came: asked again after the
        # cut, it starts over, and sleeps.
        v = _Voice()
        assert await v._heard("go to sleep")
        await v.speak(cut=True)
        await until(lambda: v.sleep.decisions, timeout=2.0)
        assert await v._heard("go to sleep")
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        # Asked again, then other words before the line ends: the conversation goes on.
        v = _Voice()
        assert await v._heard("go to sleep") and await v._heard("go to sleep")
        assert not await v._heard("actually, one more thing")
        await v.speak(cut=True)
        await asyncio.sleep(0.2)
        assert v.sent() == [] and list(v.sleep.decisions) == ["the caller went on"]
        assert len(v.said()) == 1
    asyncio.run(run())


def test_an_answer_over_a_reply_goes_in_its_place():
    """#389: a phrase's answer to words heard while the bot replies goes in place of the
    reply (say_in_place: the session stops it as for a barge-in), not queued behind it.
    Only the answer's first line, and only where there is something to stop."""
    async def run():
        placed, replying = [], [True]

        async def in_place(text, max_secs):
            placed.append(text)
            return replying[0]
        v = _Voice()
        v.say_in_place = in_place
        assert await v._heard("go to sleep")
        assert placed == [goodbye.SLEEP_TEXT] and v.said() == [], "queued behind the reply"
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        # Nothing to stop: said as usual.
        placed.clear()
        replying[0] = False
        v = _Voice()
        v.say_in_place = in_place
        assert await v._heard("go to sleep")
        assert placed == [goodbye.SLEEP_TEXT] and v.said() == [goodbye.SLEEP_TEXT]
        # A line that answers nothing just heard (the line said again after a cut) is
        # never put in place of anything.
        placed.clear()
        replying[0] = True
        assert await v._heard("go to sleep")              # the repeat
        await v.speak(cut=True)
        await until(lambda: len(v.said()) == 2, timeout=2.0)
        assert placed == []
    asyncio.run(run())


def _without_a_model():
    """The environment of a box with no model (test_no_model_session.py): no endpoint,
    no key, a HOME with no key file. Returns what to put back."""
    from tempdirs import tempdir
    was = {k: os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "HOME")}
    os.environ.pop("LLM_BASE_URL", None)
    os.environ.pop("LLM_API_KEY", None)
    os.environ["HOME"] = tempdir("teaport-goodbye-nomodel-")
    return was


def _put_back(was):
    for k, v in was.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


async def _sleeps_on_the_phrase(rig, phrase="Okay, go to sleep."):
    """The caller's phrase in the room session: the line plays out, then the bridge is
    asked to sleep, "said": true -- and no model is asked anything."""
    await rig.final(phrase)
    await until(lambda: rig.calls())
    (m, stops, played), = rig.calls()
    assert m["name"] == "end_conversation" and m["said"] is True and m["args"] == {}, m
    assert stops >= 1, "the call went out before the line had played"
    assert wifi_voice._norm(" ".join(played)) == wifi_voice._norm(goodbye.SLEEP_TEXT), played
    assert rig.provider.requests == [], "a model was asked"
    assert rig.session.sleep_phrase.sent


async def test_the_phrase_sleeps_a_room_with_no_model():
    """No key, no endpoint: the session runs without an LLM, and "go to sleep" still
    sends the room to sleep (it used to wait out the keep-alive)."""
    was = _without_a_model()
    try:
        rig = Rig([], top=wifi_voice.PhraseVoice)
    finally:
        _put_back(was)
    assert rig.session.llm is None and rig.session.sleep_phrase is not None
    async with rig:
        await _sleeps_on_the_phrase(rig)


async def test_the_phrase_sleeps_a_room_whose_model_only_errors():
    """A provider out of credit (every request fails): "No, go to sleep." is not a
    request for a reply at all."""
    async with Rig([], top=wifi_voice.PhraseVoice,
                   fails=RuntimeError("402: insufficient credit")) as rig:
        await _sleeps_on_the_phrase(rig, "No, go to sleep.")


async def test_the_phrase_wins_over_an_owed_failure_which_stays_owed():
    """A failed consult still owed: the phrase sleeps all the same, and the record is
    still owed, for the next wake (the room's sleep saves it, wake_gate.py)."""
    async with Rig([], top=wifi_voice.PhraseVoice) as rig:
        rig.owed.restore([{"key": "call-1", "request": "book a table",
                           "outcome": owed.failure_outcome("error", "402")[1],
                           "failure": "error"}])
        assert [r.failure for r in rig.owed.owed()] == ["error"]
        await _sleeps_on_the_phrase(rig)
        assert [r.key for r in rig.owed.owed()] == ["call-1"]
        assert [d["key"] for d in rig.owed.snapshot()] == ["call-1"]


# A long reply: about 14 s of synthesized speech (WORD_SECS a word).
STORY = " ".join(["Once upon a time a small fox walked through the quiet woods."] * 4)


async def test_389_go_to_sleep_over_a_paused_reply_stops_it_and_sleeps():
    """#389: "go to sleep" over a story the barge-in pauser paused for those very words.
    The phrase takes the words, so the pauser finds no turn in them -- it used to resume
    the story, with the sleep line queued behind the rest of it. Now the story is cut as
    for a barge-in, the line plays at once, and the room sleeps once it has played."""
    from test_talk_barge_pause import _Socket, _talk_transport
    async with Rig([STORY], top=wifi_voice.PhraseVoice,
                   transport=_talk_transport(_Socket())) as rig:
        pauser = rig.session.barge_pauser
        assert pauser is not None, "no barge-in pauser on a pausable transport"
        await rig.say("Tell me a story.")
        await until(lambda: len(rig.played) >= 3)            # the story is playing
        await pauser.on_onset(True)                           # the caller talks over it
        assert pauser.paused
        await pauser.on_onset(False)
        await rig.final("Go to sleep.")
        await until(lambda: rig.calls(), timeout=6.0)
        (m, stops, played), = rig.calls()
        assert m["name"] == "end_conversation" and m["said"] is True and m["args"] == {}, m
        story = wifi_voice._norm(STORY)
        heard = wifi_voice._norm(" ".join(played))
        assert heard.endswith(wifi_voice._norm(goodbye.SLEEP_TEXT)), played
        told = heard[:-len(wifi_voice._norm(goodbye.SLEEP_TEXT))].strip()
        assert told and story.startswith(told) and len(told) < len(story) / 2, (
            f"the story was not cut: {told!r}")
        assert len(rig.provider.requests) == 1, "a model was asked about the phrase"
        assert rig.session.sleep_phrase.sent
        assert list(rig.session.sleep_phrase.decisions) == [SENT]


async def test_the_backstop_sleeps_on_a_sign_off_with_only_a_failure_owed():
    """The model says only a goodbye, and the one record owed is a failure: the caller
    signed off, so the room sleeps."""
    async with Rig(["Goodbye."]) as rig:
        rig.owed.restore([{"key": "call-1", "request": "book a table",
                           "outcome": owed.failure_outcome("error", "402")[1],
                           "failure": "error"}])
        await rig.say("Okay, good night.")
        assert await decided(rig) == SENT
        await until(lambda: rig.calls())
        assert rig.calls()[0][0]["said"] is True


async def test_no_sleep_phrase_off_the_room():
    """Talk has no sleep, a phone call ends by hanging up, and a room without wake words
    (no client:sleep) has none to go back to: the phrase is the room's alone."""
    for front_end, features in (("talk", ("sleep",)), ("room", ()), ("sip", ())):
        session = build_agent_session(TalkTransport(), front_end=front_end,
                                      client_features=frozenset(features))
        assert session.sleep_phrase is None, (front_end, features)
        assert not any(isinstance(p, goodbye.SleepPhrase)
                       for p in getattr(session.phrases, "phrases", ())), (front_end, features)
    session = build_agent_session(TalkTransport(), front_end="room",
                                  client_features=frozenset({"sleep"}))
    assert session.sleep_phrase in session.phrases.phrases


async def _first_request(rig, caller):
    await rig.say(caller)
    await until(lambda: rig.provider.requests)
    return rig.provider.requests[0]


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v) and not asyncio.iscoroutinefunction(v)]
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
