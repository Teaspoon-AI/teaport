#
# Unit test: RawLLMCapture must trip on a completion that repeats a whole sentence.
#
# The punctuation-collapse detector cannot see this failure — the text is perfectly
# well-formed, it is just emitted twice.
#
# Live 2026-08-26 08:32: one completion, one stream, and the deltas carried "If you're
# undecided, try Upper Crust for a classic buttery croissant; it's a reliable spot right
# on Burnet." twice, back to back with no separator. The TTS was handed both copies and
# spoke 13.5s of audio for a one-sentence reply. Replaying that exact context at the
# provider 32 times never reproduced it, so this detector exists to catch the next one
# with the model's own bytes attached.
#
# And a repeat need only be NEAR-verbatim: live 2026-08-26 08:48 one completion carried
# the whole reply twice at generation pace with the numbers re-sampled on the second
# pass ("four thousand five hundred eight" vs "four five zero eight" for the same
# address) — 0.949 similar, so the original exact-equality detector stayed silent and
# the raw bytes of that occurrence were lost. The detector must fire on that shape too.
#
# Run: python test_raw_llm_capture.py
#
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teaport_brain.raw_llm_capture import _repeated_sentence  # noqa: E402

SENTENCE = ("If you're undecided, try Upper Crust for a classic buttery croissant; "
            "it's a reliable spot right on Burnet.")

# The 2026-08-26 08:48 completion, reconstructed verbatim from the ledger's TRACE
# LLMTextFrame lines (54 deltas -> two copies separated by "\n"). Only the number
# words differ between the copies.
NEAR_DUP = (
    "About those pastry shops near Burnet Road: Upper Crust Bakery at four thousand "
    "five hundred eight Burnet, La Pâtisserie at seven three zero one Burnet "
    "suite one‑zero‑two, Russell’s Bakery and Genuine Joe Coffee just "
    "off Burnet at two thousand one West Anderson, and Sugarwolf Bakery downtown at "
    "four hundred one West Fourth Street.\n"
    "About those pastry shops near Burnet Road: Upper Crust Bakery at four five zero "
    "eight Burnet, La Pâtisserie at seven three zero one Burnet suite "
    "one‑zero‑two, Russell’s Bakery and Genuine Joe Coffee just off "
    "Burnet at two thousand one West Anderson, and Sugarwolf Bakery downtown at four "
    "zero one West Fourth Street."
)


def test_the_live_doubling_is_detected():
    # Back to back with no separator, exactly as the deltas arrived.
    assert _repeated_sentence(SENTENCE + SENTENCE) == SENTENCE
    print("  PASS live doubling -> detected")


def test_a_separated_repeat_is_still_detected():
    assert _repeated_sentence(f"{SENTENCE} Anything else? {SENTENCE}") == SENTENCE
    print("  PASS separated repeat -> detected")


def test_the_live_near_duplicate_is_detected():
    # The 08:48 shape: same reply generated twice, numbers re-worded. Exact
    # equality misses it; the fuzzy match must not.
    assert _repeated_sentence(NEAR_DUP)
    print("  PASS live near-duplicate -> detected")


def test_similar_but_distinct_sentences_stay_quiet():
    # List-shaped replies legitimately rhyme; measured 0.55 on this pair, well
    # under the 0.9 bar. A trip here would flood the log on every itinerary.
    assert not _repeated_sentence(
        "Upper Crust is at four five zero eight Burnet. "
        "La Pâtisserie is at seven three zero one Burnet.")
    assert not _repeated_sentence("I can do that. I can do that for you now.")
    print("  PASS similar-but-distinct -> quiet")


def test_an_ordinary_reply_does_not_trip():
    assert not _repeated_sentence(
        "I like a deep teal. Let me know if you need anything else.")
    print("  PASS ordinary reply -> quiet")


def test_short_repeats_are_ordinary_speech():
    """"No. No." is emphasis, not degeneracy — the log must not fill with it."""
    assert not _repeated_sentence("No. No. Yes, that one.")
    print("  PASS short repeats -> quiet")


if __name__ == "__main__":
    for fn in [test_the_live_doubling_is_detected,
               test_a_separated_repeat_is_still_detected,
               test_the_live_near_duplicate_is_detected,
               test_similar_but_distinct_sentences_stay_quiet,
               test_an_ordinary_reply_does_not_trip,
               test_short_repeats_are_ordinary_speech]:
        fn()
    print("ALL PASS")
