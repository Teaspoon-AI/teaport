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
# Run: python test_raw_llm_capture.py
#
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teaport_brain.raw_llm_capture import _repeated_sentence  # noqa: E402

SENTENCE = ("If you're undecided, try Upper Crust for a classic buttery croissant; "
            "it's a reliable spot right on Burnet.")


def test_the_live_doubling_is_detected():
    # Back to back with no separator, exactly as the deltas arrived.
    assert _repeated_sentence(SENTENCE + SENTENCE) == SENTENCE
    print("  PASS live doubling -> detected")


def test_a_separated_repeat_is_still_detected():
    assert _repeated_sentence(f"{SENTENCE} Anything else? {SENTENCE}") == SENTENCE
    print("  PASS separated repeat -> detected")


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
               test_an_ordinary_reply_does_not_trip,
               test_short_repeats_are_ordinary_speech]:
        fn()
    print("ALL PASS")
