#
# Unit test: LLMTextGuard's containment contract and the fold table it shares with
# the TTS normalizer.
#
# The failure this guards against is gpt-oss-120b collapsing mid-completion into runs
# of ellipses and no-break unicode (live captures 2026-08-12, up to 2340 chars in one
# completion). Three properties have to hold together, and each has already been got
# wrong once:
#
#   The fold table must cover the WHOLE no-break family, not just U+00A0 — U+202F,
#   U+200B and U+2011 were all in the captures and all reached the engine verbatim.
#   It is written with \uXXXX escapes because a literal class once folded an ordinary
#   space into itself, which is invisible in source and survives review.
#
#   The trip test must fire on a collapse and NOT on ordinary speech. "Well... okay
#   then." and a reply with two dramatic pauses are the calibration floor: if a change
#   makes either of them trip, healthy replies get cut off mid-sentence.
#
#   A trip must still leave the user something. Swallowing alone turned a degenerate
#   turn into silence, because the surviving prefix is usually punctuation and
#   split_clauses_ramp drops it as unsynthesizable.
#
# Run: python test_llm_text_guard.py   (or via pytest)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.env import env_flag  # noqa: E402
from teaport_brain.llm_text_guard import (  # noqa: E402
    RECOVERY_TEXT,
    DegeneracyCounter,
    LLMTextGuard,
    fold_degenerate_chars,
    is_degenerate,
)
from teaport_brain.raw_llm_capture import RawLLMCapture  # noqa: E402
from teaport_brain.tts_text import fold_unspeakable, split_clauses_ramp  # noqa: E402

# \uXXXX escapes, never the literal characters: five of these seven are invisible,
# and a test whose fixtures a formatter can silently rewrite into ordinary spaces
# would pass while asserting nothing.
NBSP, NNBSP, ZWSP, WJ, BOM, NBHY, ELL = (
    "\u00a0", "\u202f", "\u200b", "\u2060", "\ufeff", "\u2011", "\u2026")

DOWN = FrameDirection.DOWNSTREAM


# ---------------------------------------------------------------- fold table

def test_folds_the_whole_nobreak_family():
    # U+00A0 was the only one folded before; the other four reached the engine
    # inside otherwise ordinary words.
    assert fold_unspeakable(f"voice{NBSP}id") == "voice id"
    assert fold_unspeakable(f"voice{NNBSP}id") == "voice id"
    assert fold_unspeakable(f"voice{ZWSP}id") == "voiceid"
    assert fold_unspeakable(f"voice{WJ}id") == "voiceid"
    assert fold_unspeakable(f"voice{BOM}id") == "voiceid"
    assert fold_unspeakable(f"voice{NBHY}id") == "voice-id"


def test_strips_markdown_bold():
    # raw_llm_capture calls ** "always a defect"; the system prompt forbids markdown,
    # so it is never speech. Diagnosing it without folding it is half a fix.
    assert fold_unspeakable("**Sure** thing") == "Sure thing"
    assert fold_degenerate_chars("**Sure**") == "Sure"


def test_healthy_text_passes_untouched():
    healthy = "Sure, the forecast is 18 degrees and clear."
    assert fold_unspeakable(healthy) == healthy
    assert fold_degenerate_chars(healthy) == healthy


def test_collapses_runs():
    assert fold_degenerate_chars(f"a{ELL}{ELL}{ELL}b") == f"a{ELL} b"
    assert fold_degenerate_chars("a....b") == "a...b"
    # A single ellipsis is ordinary punctuation and must survive the fold.
    assert fold_degenerate_chars(f"a{ELL}b") == f"a{ELL}b"


# ------------------------------------------------------------- the trip test

DEGENERATE = [
    f"Sure{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}",
    "I can help... ... ... ... ...",
    f"{ELL}{NBSP}{NBSP}\n\n{ELL}{NBSP}{ELL}{NBSP}{ELL}{NBSP}{ELL}",
    "Let me check that.... hmm.... well.... so.... anyway....",
]

# Below the cut threshold ON PURPOSE. A mild run is not a collapse: the fold still
# strips it to comma pauses and the clause splitter drops what is left, so it costs a
# beat of silence rather than a truncated turn. Cutting this aggressively cost more
# than it saved — three dot runs also fires on ordinary speech quoting a log line
# ("Encoder... Decoder... Marlin..."), and a false cut interrupts a healthy answer.
# These pin the boundary: folded, not cut.
FOLDED_NOT_CUT = [
    "I can help... ... ... ...",                     # four runs
    "Let me check that.... hmm.... well.... so....",  # four runs
    "The log said Encoder... Decoder... Marlin... and then it stopped.",
]

HEALTHY = [
    "Well... okay then.",
    "I waited... and waited... then left.",     # two dramatic pauses
    f"Hmm{ELL} interesting{ELL} let me check.",  # two ellipses in ordinary prose
    "Sure, the forecast is 18 degrees and clear.",
    "The file is at /tmp/a...b if you want it.",
]


def test_trips_on_every_capture_shape():
    for text in DEGENERATE:
        assert is_degenerate(text), f"should trip: {text!r}"


def test_mild_runs_are_folded_but_not_cut():
    """The boundary between "clean it up" and "stop the turn"."""
    for text in FOLDED_NOT_CUT:
        assert not is_degenerate(text), f"should NOT cut: {text!r}"
        folded = fold_degenerate_chars(text)
        assert "...." not in folded, f"long run should be folded: {text!r}"


def test_leaves_healthy_speech_alone():
    for text in HEALTHY:
        assert not is_degenerate(text), f"should NOT trip: {text!r}"


def test_incremental_matches_oneshot():
    # The processor feeds deltas; is_degenerate() scans whole strings. They must
    # agree, including when a dot run is split across a frame boundary.
    for text in DEGENERATE + HEALTHY:
        for size in (1, 2, 3, 7):
            c = DegeneracyCounter()
            for i in range(0, len(text), size):
                c.feed(text[i:i + size])
            assert c.tripped == is_degenerate(text), (text, size)


def test_dot_run_split_across_frames_counts_once():
    c = DegeneracyCounter()
    c.feed("..")
    c.feed(".")
    assert c.dot_runs == 1        # not 0 (boundary lost) and not 2 (double counted)
    c2 = DegeneracyCounter()
    c2.feed("....")
    assert c2.dot_runs == 1       # a longer run is still ONE marker


# ------------------------------------------------------------ the processor

class Guard:
    def __init__(self):
        self.g = LLMTextGuard()
        self.out = []
        out = self.out

        async def fake_push(frame, direction=DOWN):
            out.append(frame)
        self.g.push_frame = fake_push

    async def feed(self, frame):
        await self.g.process_frame(frame, DOWN)

    async def text(self, s):
        await self.feed(LLMTextFrame(text=s))

    def spoken(self):
        return "".join(f.text for f in self.out if isinstance(f, LLMTextFrame))

    def kinds(self):
        return [type(f).__name__ for f in self.out]


async def test_healthy_completion_flows_through_folded():
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text(f"The voice{NBSP}id ")
    await h.text("is af_heart.")
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "The voice id is af_heart."
    assert h.kinds() == ["LLMFullResponseStartFrame", "LLMTextFrame",
                         "LLMTextFrame", "LLMFullResponseEndFrame"]


async def test_trip_swallows_tail_and_speaks_recovery():
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text("Sure, one moment. ")
    await h.text(f"{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}")     # crosses the threshold
    await h.text(f"{ELL}{NBSP}{ELL}{NBSP}" * 40)        # the 2340-char tail
    await h.feed(LLMFullResponseEndFrame())
    # The healthy prefix survives, the collapse does not, and the turn still says
    # something a user can respond to.
    assert h.spoken() == "Sure, one moment. " + RECOVERY_TEXT
    # Exactly one recovery line, however long the tail runs.
    assert h.spoken().count(RECOVERY_TEXT) == 1
    # Start/End still bracket the turn so downstream aggregators close it.
    assert h.kinds()[0] == "LLMFullResponseStartFrame"
    assert h.kinds()[-1] == "LLMFullResponseEndFrame"


async def test_recovery_speaks_even_when_nothing_healthy_preceded():
    # The captures collapse within ~50 chars, so "keep the healthy prefix" can keep
    # nothing. Silence with no signal was the old outcome.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text(f"{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}{NBSP}{NBSP}")
    await h.feed(LLMFullResponseEndFrame())
    # lstrip: RECOVERY_TEXT carries a deliberate leading space so it reads as its own
    # sentence after a healthy prefix. With nothing forwarded there is no prefix, and a
    # whitespace-opening frame is exactly the phantom caption slot _LEADING_PUNCT exists
    # to prevent — so the strip applies to the recovery line too.
    assert h.spoken() == RECOVERY_TEXT.lstrip()
    assert split_clauses_ramp(h.spoken()), "recovery line must be synthesizable"


async def test_log_counters_reset_on_end_but_the_trip_stays_latched():
    # pipecat's audio-context watchdog can push a premature End mid-reply, splitting a
    # response into segments with no Start. Resetting only on Start let _swallowed
    # carry over, and the next trip log reported more swallowed chars than the
    # response contained — so the per-segment ACCOUNTING resets here.
    #
    # _tripped and _forwarded deliberately do NOT: an End is not proof the completion
    # ended, and clearing them re-armed the guard against the rest of the same collapse.
    # See test_premature_end_does_not_speak_a_second_recovery_line.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text(f"{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}")
    await h.text("swallowed tail")
    assert h.g._swallowed > 0
    await h.feed(LLMFullResponseEndFrame())
    assert h.g._swallowed == 0          # accounting: reset
    assert h.g._tripped                 # verdict: latched
    # A real new response clears everything.
    await h.feed(LLMFullResponseStartFrame())
    assert h.g._forwarded == 0 and not h.g._tripped


async def test_premature_end_does_not_speak_a_second_recovery_line():
    """The regression the End-reset caused: one collapse, two apologies.

    engine_tts's audio-context watchdog pushes an End mid-reply. If that clears the
    trip, the remaining deltas of the SAME degenerate completion flow again, re-cross
    the threshold, and the user hears the recovery line twice."""
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text("Sure. ")
    await h.text(f"{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}")   # trips
    await h.feed(LLMFullResponseEndFrame())            # premature, mid-collapse
    await h.text(f"{ELL}{ELL}{ELL}{ELL}{ELL}{ELL}")   # same completion, still junk
    await h.text(f"{ELL}{NBSP}" * 40)
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken().count(RECOVERY_TEXT.strip()) == 1, h.spoken()


async def test_a_solid_punctuation_run_trips_on_volume():
    """One unbroken run is ONE marker, so the marker counts alone never saw the worst
    capture on record — 2340 characters of punctuation arriving as a single run."""
    assert is_degenerate("." * 2340)
    assert is_degenerate(ELL * 60)
    # And it still reaches the user as a sentence rather than as silence.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for _ in range(300):
        await h.text(".")
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken().strip() == RECOVERY_TEXT.strip()


async def test_empty_text_frames_are_never_forwarded():
    """An empty frame opens a word-timestamp slot that synthesizes nothing, and every
    real word after it then misses its slot — the caption bubble never assembles."""
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text("Hello there")
    await h.text("**")          # folds to empty mid-reply
    await h.text("")            # arrives empty
    await h.text(" world.")
    await h.feed(LLMFullResponseEndFrame())
    assert all(f.text for f in h.out if isinstance(f, LLMTextFrame)), h.spoken()
    assert h.spoken() == "Hello there world."


async def test_runs_split_across_deltas_still_fold():
    """Guard output must not depend on where the provider chunks the stream."""
    for chunks in ([f"a{ELL}", f"{ELL}b"], ["a**", "*b"], ["a**", "b"], ["a..", "..b"]):
        h = Guard()
        await h.feed(LLMFullResponseStartFrame())
        for c in chunks:
            await h.text(c)
        await h.feed(LLMFullResponseEndFrame())
        got = h.spoken()
        assert "**" not in got and "...." not in got and f"{ELL}{ELL}" not in got, (chunks, got)


# ------------------------------------------------- TTS-side integration

def test_punctuation_only_is_dropped_and_cjk_survives():
    # The `or [text]` fallback in run_tts is gone, so this filter is now the only
    # thing standing between a punctuation-only reply and a 500 per clause.
    assert split_clauses_ramp(f"{ELL}{NBSP}{NBSP}\n\n") == []
    assert split_clauses_ramp("...") == []
    # The reason the fallback existed: an ASCII-only test dropped these entirely.
    for text in ("こんにちは。", "你好。",
                 "नमस्ते।"):
        assert split_clauses_ramp(text), f"must survive: {text!r}"


def test_guard_output_survives_the_tts_normalizer():
    # Both stages fold; text through both must not come out with a doubled space.
    folded = fold_degenerate_chars(f"a{ELL}{ELL}{ELL}b")
    assert ",  " not in " ".join(split_clauses_ramp(folded + " Done."))


# ---------------------------------------------------------------- env flags

def test_env_flag_truth_table():
    key = "TEAPORT_TEST_FLAG"
    try:
        for raw, want in [("0", False), ("false", False), ("no", False),
                          ("off", False), ("1", True), ("true", True),
                          ("yes", True), ("ON", True)]:
            os.environ[key] = raw
            assert env_flag(key, True) is want, raw
        # An empty value is "not set", NOT "disabled". A bare `TEAPORT_LLM_TEXT_GUARD=`
        # line in brain.env used to silently remove the containment.
        os.environ[key] = ""
        assert env_flag(key, True) is True
        os.environ[key] = "  "
        assert env_flag(key, True) is True
        # Unrecognized falls back to the default rather than guessing.
        os.environ[key] = "maybe"
        assert env_flag(key, True) is True
        del os.environ[key]
        assert env_flag(key, True) is True
        assert env_flag(key, False) is False
    finally:
        os.environ.pop(key, None)


def test_max_tokens_follows_reasoning_effort():
    from teaport_brain import services
    saved = (os.environ.get("LLM_REASONING_EFFORT"), os.environ.get("LLM_MAX_TOKENS"))
    try:
        os.environ.pop("LLM_MAX_TOKENS", None)
        # A cap sized for "low" starves "high": the reasoning alone exhausts it and
        # the completion returns finish_reason=length with no visible content.
        for effort, floor in [("low", 1024), ("medium", 2048), ("high", 4096)]:
            os.environ["LLM_REASONING_EFFORT"] = effort
            assert services._max_tokens() >= floor, effort
        os.environ["LLM_REASONING_EFFORT"] = "high"
        assert services._max_tokens() > services._MAX_TOKENS_BY_EFFORT["low"]
        # 0 means uncapped, not 1024 — an un-metered endpoint needs a way out.
        os.environ["LLM_MAX_TOKENS"] = "0"
        assert services._max_tokens() is None
        # A typo warns and falls back rather than crash-looping the service.
        os.environ["LLM_MAX_TOKENS"] = "lots"
        assert isinstance(services._max_tokens(), int)
    finally:
        for k, v in zip(("LLM_REASONING_EFFORT", "LLM_MAX_TOKENS"), saved):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ------------------------------------------------------- after_tool flag

class Capture:
    def __init__(self):
        self.c = RawLLMCapture()

        async def fake_push(frame, direction=DOWN):
            pass
        self.c.push_frame = fake_push

    async def feed(self, frame):
        await self.c.process_frame(frame, DOWN)


def result_frame(run_llm=None):
    props = FunctionCallResultProperties(run_llm=run_llm) if run_llm is not None else None
    return FunctionCallResultFrame(function_name="ask_openclaw", tool_call_id="t1",
                                   arguments={}, result={"ok": True}, properties=props)


async def test_after_tool_latched_at_start_not_at_the_result():
    # pipecat pushes End BEFORE the scheduled FunctionCallResultFrame, so clearing the
    # flag on End made it survive to the next completion only by scheduling luck.
    h = Capture()
    await h.feed(LLMFullResponseStartFrame())
    await h.feed(LLMFullResponseEndFrame())      # the tool-call turn ends first...
    await h.feed(result_frame())                 # ...then its result arrives
    await h.feed(LLMFullResponseStartFrame())    # the completion reacting to it
    assert h.c._after_tool is True


async def test_after_tool_not_leaked_by_a_suppressed_result():
    # run_llm=False means NO completion follows, so the flag must not wait around and
    # attach itself to an unrelated later turn.
    h = Capture()
    await h.feed(result_frame(run_llm=False))
    await h.feed(LLMFullResponseStartFrame())
    assert h.c._after_tool is False


async def test_after_tool_cleared_by_a_new_user_turn():
    h = Capture()
    await h.feed(result_frame())
    await h.feed(UserStartedSpeakingFrame())     # stale: no completion ever came
    await h.feed(LLMFullResponseStartFrame())
    assert h.c._after_tool is False


async def test_a_reply_opening_with_an_ellipsis_loses_no_words():
    """The leading dots must go, and nothing else may.

    They arrive split across deltas, exactly as pipecat streams them, because that is
    what produced the empty '..' and '.' TTS slots that made every spoken word miss its
    slot and cost the user the whole caption bubble.
    """
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for delta in (".", ".", ". ", "it was ", "the worst of times."):
        await h.feed(LLMTextFrame(text=delta))
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "it was the worst of times."


async def test_a_reply_opening_with_a_unicode_ellipsis_is_stripped_too():
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.feed(LLMTextFrame(text="\u2026 right, where were we?"))
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "right, where were we?"


async def test_punctuation_inside_a_reply_is_left_alone():
    """Only the opening is stripped: mid-reply a '.' ends a sentence, and dropping it
    would merge two sentences into one TTS chunk."""
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for delta in ("Yes", ".", " ", "It is done", "."):
        await h.feed(LLMTextFrame(text=delta))
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "Yes. It is done."


async def test_each_response_strips_its_own_opening():
    """_forwarded resets per response, so a second reply is stripped like the first."""
    h = Guard()
    for _ in range(2):
        await h.feed(LLMFullResponseStartFrame())
        await h.feed(LLMTextFrame(text="... here we go."))
        await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "here we go.here we go."


async def test_pass_through_text_keeps_its_frame_identity():
    """A delta that merely passes through must keep the frame id it arrived with.

    TranscriptLedger is a BaseObserver: it sees every push of every frame and
    de-duplicates on frame IDENTITY. Each LLMTextFrame is pushed twice on the way down,
    once by the LLM service and once by this guard, and the ledger charts it once only
    because both pushes carry the same object. Emitting a fresh frame gives the second
    push an id the ledger has not seen, so the delta is charted TWICE — which corrupts
    the denominator of the heard fraction, makes HeardContextCorrector drop replies the
    user actually heard, and leaves the model re-answering questions it had answered.

    Live 2026-08-25: "I like teal." was charted as "I like tealI. like teal.", 11 of 12
    assistant turns doubled.
    """
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    sent = [LLMTextFrame(text="I"), LLMTextFrame(text=" like teal.")]
    for frame in sent:
        await h.feed(frame)
    await h.feed(LLMFullResponseEndFrame())
    forwarded = [f for f in h.out if isinstance(f, LLMTextFrame)]
    assert [f.id for f in forwarded] == [f.id for f in sent], (
        "guard emitted new frame ids; the ledger will chart these deltas twice"
    )
    assert h.spoken() == "I like teal."


async def test_the_recovery_line_is_allowed_a_new_frame():
    """It is genuinely new text, so it SHOULD be charted — unlike pass-through deltas."""
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    sent = LLMTextFrame(text="." * 40)
    await h.feed(sent)
    await h.feed(LLMFullResponseEndFrame())
    forwarded = [f for f in h.out if isinstance(f, LLMTextFrame)]
    assert forwarded, "the recovery line must still be spoken"
    assert forwarded[0].id != sent.id, "recovery text is new; it needs its own frame"


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
