#
# Unit test: LLMTextGuard's repeat cut — a completion that says its reply twice must
# reach the TTS and the context exactly once.
#
# The failure is the model's, measured live twice on 2026-08-26, both times inside a
# SINGLE provider stream (one "Generating chat", one "response stream open", one
# "completion finished"; the deltas below are reconstructed verbatim from the ledger's
# id-deduplicated TRACE LLMTextFrame lines):
#
#   08:32:32 — the one-sentence reply "If you're undecided, try Upper Crust..."
#   streamed once (t=122.88-123.05) and then again (t=123.06-123.12), verbatim, same
#   token boundaries, with no separator ("...on Burnet.If you're undecided..."). One
#   engine TTS call got both copies concatenated and synthesized 13.5s of audio for a
#   one-sentence reply.
#
#   08:48:16 — the whole delivery paragraph streamed twice at generation pace (copy
#   one 08:48:13.0-15.6, a 1.0s pause, copy two 08:48:16.6-19.1) with the spelled-out
#   numbers RE-SAMPLED on the second pass: "four thousand five hundred eight" became
#   "four five zero eight", "four hundred one" became "four zero one". Two engine TTS
#   calls, 42.9s of audio, and the doubled text committed to history. The re-sampled
#   numbers are what prove the copy was generated upstream — no frame replay in this
#   process can re-word a number.
#
# The guard now holds each sentence's frames until the sentence closes (costless: the
# TTS aggregator only synthesizes whole sentences anyway) and drops a closing sentence
# that near-repeats one already flushed in the same response, swallowing the rest.
# tts_text.is_sentence_repeat is the shared match (0.949 measured between the 08:48
# copies; 0.55 and 0.38 for the most alike DISTINCT sentences of the same session).
#
# Run: python test_repeat_cut.py
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.llm_text_guard import RECOVERY_TEXT, LLMTextGuard  # noqa: E402

DOWN = FrameDirection.DOWNSTREAM


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


# The 08:32 completion, delta for delta (the ledger TRACE shows the repeat reused the
# same token boundaries: 'undec'+'ided', 'Cr'+'ust', 'cro'+'issant').
COPY_0832 = ["If", " you", "’re", " undec", "ided", ",", " try", " Upper",
             " Cr", "ust", " for", " a", " classic", " buttery", " cro", "issant",
             ";", " it", "’s", " a", " reliable", " spot", " right", " on",
             " Burn", "et", "."]
SPOKEN_0832 = ("If you're undecided, try Upper Crust for a classic buttery "
               "croissant; it's a reliable spot right on Burnet.")

# The 08:48 completion in coarser slices; the copy boundary is exactly as live —
# copy one's "." delta, then "\nAbout" opening copy two.
COPY1_0848 = ["About those pastry shops near Burnet Road: Upper Crust Bakery at ",
              "four thousand five hundred eight", " Burnet, La Pâtisserie at ",
              "seven three zero one Burnet suite one‑zero‑two, ",
              "Russell’s Bakery and Genuine Joe Coffee just off Burnet at two ",
              "thousand one West Anderson, and Sugarwolf Bakery downtown at ",
              "four hundred one", " West Fourth Street", "."]
COPY2_0848 = ["\nAbout", " those pastry shops near Burnet Road: Upper Crust Bakery at ",
              "four five zero eight", " Burnet, La Pâtisserie at ",
              "seven three zero one Burnet suite one‑zero‑two, ",
              "Russell’s Bakery and Genuine Joe Coffee just off Burnet at two ",
              "thousand one West Anderson, and Sugarwolf Bakery downtown at ",
              "four zero one", " West Fourth Street", "."]


async def test_the_verbatim_doubling_is_spoken_once():
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for t in COPY_0832 + COPY_0832:
        await h.text(t)
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == SPOKEN_0832, h.spoken()
    assert h.spoken().count("undecided") == 1


async def test_the_resampled_doubling_is_spoken_once():
    # The 08:48 shape: near-verbatim, numbers re-worded. This is the copy that
    # proves provider origin, and the one an exact-equality comparison missed.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for t in COPY1_0848 + COPY2_0848:
        await h.text(t)
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken().count("About those pastry shops") == 1, h.spoken()
    assert "four thousand five hundred eight" in h.spoken()  # copy one survives
    assert "four five zero eight" not in h.spoken()          # copy two does not
    # The cut is silent — the flushed prefix is the complete healthy reply, so a
    # "lost my train of thought" apology after it would be a lie.
    assert RECOVERY_TEXT.strip() not in h.spoken()


async def test_a_duplicate_cut_short_by_the_token_cap_is_still_dropped():
    # If max_tokens ends the response mid-second-copy there is no closing ".", but
    # the End flush must still recognise the near-complete duplicate.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    for t in COPY_0832 + COPY_0832[:-3]:  # repeat ends "...right on", no terminator
        await h.text(t)
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == SPOKEN_0832, h.spoken()


async def test_similar_but_distinct_sentences_both_flow():
    # List-shaped replies legitimately rhyme (0.55 measured on this pair). Both
    # sentences must be spoken, or every itinerary loses its second half.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    await h.text("Upper Crust is at four five zero eight Burnet. ")
    await h.text("La Pâtisserie is at seven three zero one Burnet.")
    await h.feed(LLMFullResponseEndFrame())
    assert "Upper Crust" in h.spoken() and "La Pâtisserie" in h.spoken()


async def test_sentences_are_held_until_they_close():
    # The mechanism that makes the drop possible: no delta may be pushed before its
    # sentence closes. Costless, because pipecat's TTS aggregator only hands a
    # WHOLE sentence to run_tts anyway — nothing downstream ran earlier than this.
    h = Guard()
    await h.feed(LLMFullResponseStartFrame())
    sent = [LLMTextFrame(text="It is a lovely"), LLMTextFrame(text=" day today")]
    for f in sent:
        await h.feed(f)
    assert not [f for f in h.out if isinstance(f, LLMTextFrame)], (
        "mid-sentence deltas must be held — flushing early makes the duplicate "
        "unsuppressible")
    await h.text(". And tomorrow too.")
    await h.feed(LLMFullResponseEndFrame())
    forwarded = [f for f in h.out if isinstance(f, LLMTextFrame)]
    # Pass-through text keeps its frame identity (the ledger de-dups on it).
    assert [f.id for f in forwarded[:2]] == [f.id for f in sent]
    assert h.spoken() == "It is a lovely day today. And tomorrow too."


async def test_interruption_drops_the_held_sentence():
    # A barge-in cancels the completion, but base_llm still pushes its End. The End
    # flush must not hand the TTS a held sentence the user just cut off (the
    # stale-speech bug class).
    h = Guard()

    async def noop():  # the bare harness has no task manager to interrupt
        pass
    h.g._start_interruption = noop
    await h.feed(LLMFullResponseStartFrame())
    await h.text("Something the user is about to interru")
    await h.feed(InterruptionFrame())
    await h.feed(LLMFullResponseEndFrame())
    assert h.spoken() == "", h.spoken()


def main():
    async def run_aio():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_"):
                await fn()
                print(f"  ok {name}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
