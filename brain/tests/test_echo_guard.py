#
# The room mic's echo guard (echo_guard.py): the bot's own words, heard back through the
# box's speaker, are taken off the front of a transcript before the STT pushes it, and a
# transcript made only of them is nothing -- while the user's own words, said over the
# bot, go through. The cases are the live ones of 2026-10-10 (volume 80: the story came
# back as user turns; volume 65: the bot's words prefixed to the user's question).
#
# Run: python test_echo_guard.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from pipecat.frames.frames import (  # noqa: E402
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402

from teaport_brain import echo_guard  # noqa: E402
from teaport_brain.endpointing import SegmentDoneFrame  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

STORY = ("Once upon a time, in a quiet orchard of magic, there lived a tiny pistachio "
         "named Pip. The tiny pistachio rolled under the old oak tree.")
CAMERA = "This began with a camera that someone had left in the attic."
# From a word's playout to the first interim of its echo: 1.4-1.7 s on the box's
# room-mic trace of 2026-10-10 ("A curious", "In quiet").
LAG = 1.5
GAP = 0.3  # one played word every GAP seconds


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _guard(*played, at=0.0):
    """A guard whose bot played `played`, one word every GAP seconds from `at`."""
    clock = Clock()
    clock.t = at
    g = echo_guard.EchoGuard(clock=clock)
    for w in " ".join(played).split():
        g.bot.played(w)
        clock.t += GAP
    return g, clock


def _echo_of(word_index, at=0.0):
    """When an echo's first words arrive, for one beginning at the word played
    `word_index`-th (counting from 0)."""
    return at + word_index * GAP + LAG


# ---------------------------------------------------------------- what is taken

def test_the_bots_words_heard_back_are_nothing():
    g, clock = _guard(STORY)
    assert g.strip("In a quiet orchard of magic", final=True, since=_echo_of(4)) == ""
    assert g.strip("The tiny pistachio", final=True, since=_echo_of(16)) == ""
    assert g.strip("Once upon a time in a quiet orchard of magic there lived", final=True,
                   since=_echo_of(0)) == ""


def test_the_bots_words_in_front_of_the_users_are_taken_off():
    g, clock = _guard(CAMERA)
    assert g.strip("This began with a camera. Is it a horned owl?", final=True,
                   since=_echo_of(0)) == "Is it a horned owl?"
    # Its opening punctuation stays the user's.
    assert g.strip("This began with a camera. ¿Qué es eso?", final=True,
                   since=_echo_of(0)) == "¿Qué es eso?"


def test_a_final_that_lands_long_after_the_bot_stopped_is_still_stripped():
    # The bot's last words, then the user's question; the final lands seconds later.
    g, clock = _guard("The tiny pistachio found its home.")
    since = _echo_of(0)
    clock.t = since + 5.0
    assert g.strip("The tiny pistachio found its home. What kind of tree is a pistachio?",
                   final=True, since=since) == "What kind of tree is a pistachio?"


def test_an_answer_that_quotes_the_bot_after_it_finished_is_the_users():
    g, clock = _guard("Do you want the red one or the blue one?")
    # The user answers 1.5 s after the bot's last word; their words arrive ~0.7 s later.
    since = clock.t - GAP + 1.5 + 0.7
    assert g.strip("The blue one.", final=True, since=since) == "The blue one."


def test_the_users_own_words_over_the_bot_go_through():
    g, clock = _guard(STORY)
    for said in ("Is that a type of dragonfly?", "What time is it right now?",
                 "Wait, I thought Ila was the mouse.", "Stop."):
        assert g.strip(said, final=True, since=_echo_of(10)) == said, said


def test_a_few_common_words_the_bot_also_said_are_not_its():
    g, clock = _guard("So is it a good idea to plant it in the spring?")
    assert g.strip("Is it a horned owl?", final=True, since=_echo_of(1)) == "Is it a horned owl?"
    # A misheard word counts for nothing: "the ... today" is two short matches.
    g, clock = _guard("The answer today was no.")
    assert g.strip("The weather today is nice.", final=True,
                   since=_echo_of(0)) == "The weather today is nice."


def test_a_word_misheard_missed_inserted_or_clipped_still_follows_the_bot():
    g, clock = _guard(STORY)
    since = _echo_of(16)
    assert g.strip("The tiny pistachio rolled under the cold oak tree", True, since) == ""
    assert g.strip("The tiny rolled under the old oak tree", True, since) == ""
    assert g.strip("The tiny little pistachio rolled under the old oak tree", True, since) == ""
    assert g.strip("ny pistachio rolled under the old oak tree", True, _echo_of(17)) == ""
    # ...but not a run of slips: that is somebody else talking.
    said = "The tiny dog ran over the green hill fast"
    assert g.strip(said, True, since) == said


def test_one_word_is_the_bots_only_when_long_and_not_a_stop():
    g, clock = _guard("Sorry. Wait for it. Okay.")
    assert g.strip("Sorry.", final=True, since=_echo_of(0)) == ""
    assert g.strip("Okay.", final=True, since=_echo_of(4)) == "Okay."   # short: the user's too
    assert g.strip("Wait.", final=True, since=_echo_of(1)) == "Wait."   # a stop word stops the bot


def test_nothing_is_taken_once_the_bot_has_been_quiet():
    g, clock = _guard(STORY)
    since = clock.t + echo_guard.ACTIVE_SECS + 0.1
    assert g.strip("The old oak tree", final=True, since=since) == "The old oak tree"


def test_an_old_phrase_is_not_matched():
    g, clock = _guard(STORY, "and then some more words go on for quite a while now")
    said = "Once upon a time in a quiet orchard"   # played ~8 s before
    assert g.strip(said, final=True, since=clock.t) == said


# ---------------------------------------------------------------- interims

def test_an_interim_that_follows_the_bot_waits_and_the_users_words_go():
    g, clock = _guard("Is that what you wanted to know about fireflies?")
    since = _echo_of(0)
    assert g.strip("Is", final=False, since=since) == ""          # could be either: wait
    assert g.strip("Is that", final=False, since=since) == ""
    assert g.strip("Is that a type", final=False, since=since) == "Is that a type"
    assert g.strip("Wait", final=False, since=since) == "Wait"   # a stop word never waits


def test_an_echo_never_reaches_two_words():
    # The barge-in counts words in interims: an echo held back cannot cut the bot.
    g, clock = _guard(STORY)
    for said, at in (("in a quiet orchard of magic there lived", 4),
                     ("ny pistachio rolled under the old oak", 17)):
        words = said.split()
        for n in range(1, len(words) + 1):
            got = g.strip(" ".join(words[:n]), final=False, since=_echo_of(at))
            assert len(got.split()) < 2, (said, n, got)


# ---------------------------------------------------------------- the tap

async def _tap_records_played_words():
    clock = Clock()
    g = echo_guard.EchoGuard(clock=clock)
    tap = g.tap()
    pushed = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)
    tap.push_frame = push

    async def base(self, frame, direction):
        pass
    orig = FrameProcessor.process_frame
    FrameProcessor.process_frame = base
    try:
        for w in "In a quiet orchard".split():
            await tap.process_frame(TTSTextFrame(w, aggregated_by="word"), FrameDirection.DOWNSTREAM)
    finally:
        FrameProcessor.process_frame = orig
    assert len(pushed) == 4                            # passed on, every one
    assert [w for _, w in g.bot.since(clock.t)] == ["in", "a", "quiet", "orchard"]


def test_the_tap_records_what_played_and_passes_it_on():
    asyncio.run(_tap_records_played_words())


# ---------------------------------------------------------------- in the STT

class Recorder(TeaportSTTService):
    """Real _handle_message, frames captured instead of pushed."""

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass


async def _say(stt, text, final=None):
    for word in text.split():
        await stt._handle_message({"type": "transcription.delta", "delta": word + " "})
    await stt._handle_message({"type": "transcription.done",
                               "text": text if final is None else final})


def _texts(frames):
    return [f.text for f in frames if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))]


async def _in_the_stt():
    stt = Recorder()
    g, clock = _guard(STORY)
    stt.echo_guard = g
    clock.t = _echo_of(4)                              # its first words arrive now
    await _say(stt, "In a quiet orchard of magic")
    assert _texts(stt.pushed) == []                    # no interim, no final
    assert isinstance(stt.pushed[-1], SegmentDoneFrame)  # the segment still closes
    stt.pushed.clear()
    t0 = clock.t
    for w in CAMERA.split():
        g.bot.played(w)
        clock.t += GAP
    clock.t = t0 + LAG
    await _say(stt, "This began with a camera. Is it a horned owl?")
    texts = _texts(stt.pushed)
    assert texts[-1] == "Is it a horned owl?", texts
    assert all(not t.lower().startswith("this") for t in texts), texts
    assert stt.pushed[-1].result["text"] == "Is it a horned owl?"
    stt.pushed.clear()
    await _say(stt, "What time is it right now?")
    assert _texts(stt.pushed)[-1] == "What time is it right now?"


def test_the_stt_pushes_only_the_users_words():
    asyncio.run(_in_the_stt())


async def _no_guard():
    stt = Recorder()
    await _say(stt, "In a quiet orchard of magic")
    assert _texts(stt.pushed)[-1] == "In a quiet orchard of magic"


def test_without_a_guard_every_word_goes():
    asyncio.run(_no_guard())


def test_only_the_room_mic_gets_it():
    import inspect
    from teaport_brain import agent_session
    src = inspect.getsource(agent_session.build_agent_session)
    assert 'echo_guard.ENABLED and "local" in client_features' in src
    assert "guard.tap() if guard is not None" in src


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
