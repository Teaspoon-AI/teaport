#
# Unit test: structural markdown never reaches the synth, and a list speaks as speech.
#
# Live 2026-09-09, one 18-minute SIP call on the AWS box (the first clean long call --
# WebRTC AEC was finally in the gateway, so none of it is echo): 19 of the 220 texts
# handed to run_tts opened with a bullet. A whole restaurant menu and a six-item summary
# of the call itself went out one list item per utterance, each beginning with a hyphen,
# and a numbered recital arrived as "Your last three spoken inputs, verbatim:\n\n1. ...".
# The persona had forbidden markdown since the file was split, and the model wrote it
# anyway -- so the synth-side normalizer strips what arrives, exactly as it already does
# for "**" (tts_text.fold_unspeakable), and persona.py owns the other half: keeping the
# SHAPE conversational, which no strip can do.
#
# The strip is line-anchored and therefore lives in _normalize_for_tts rather than the
# shared fold_unspeakable table -- llm_text_guard folds per delta, where a line anchor
# would depend on the provider's chunking. These cases run through split_clauses_ramp,
# which is the path run_tts actually takes.
#
# Run: python test_spoken_markdown.py   (or via the suite)
#

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
from teaport_brain.tts_text import has_speech, split_clauses_ramp  # noqa: E402

# Verbatim from the call log, the two shapes that were spoken.
# The third item keeps the live en dash: it is mid-line punctuation and a clause seam,
# so it must survive while the line-leading hyphen-minus does not.
MENU = ("- Cupcakes (vanilla, chocolate, seasonal flavors)  \n"
        "- Daily soups (typically a rotating selection)  \n"
        "- Pastries & breads – cookies, lemon squares, brownies")
RECITAL = ("Your last three spoken inputs, verbatim:\n\n"
           "1. “Shut up. Shut up.”\n"
           "2. “Say that again.”")


def _spoken(text):
    return " ".join(split_clauses_ramp(text))


def test_a_bullet_never_reaches_the_synth():
    spoken = _spoken(MENU)
    assert not any(c.lstrip().startswith(("-", "*", "•"))
                   for c in split_clauses_ramp(MENU)), \
        f"a chunk still opens with the hyphen the engine had to pronounce: {spoken!r}"
    assert spoken.startswith("Cupcakes"), spoken
    assert "Daily soups" in spoken and "Pastries" in spoken, "no item is lost"
    assert "breads – cookies" in spoken, "the mid-line en dash is punctuation, not a marker"


def test_an_item_seam_becomes_the_pause_the_bullet_carried():
    spoken = _spoken(MENU)
    # "(seasonal flavors)" closes with a bracket, not punctuation, so the seam earns a
    # comma; a line already ending in "." would not get a second mark.
    assert "flavors), Daily soups" in spoken, spoken
    assert ".," not in spoken and ", ," not in spoken, f"doubled pause: {spoken!r}"


def test_an_ordered_list_loses_its_numbers_and_keeps_its_words():
    spoken = _spoken(RECITAL)
    assert "1." not in spoken and "2." not in spoken, spoken
    assert "verbatim: “Shut up" in spoken, f"the colon already pauses: {spoken!r}"
    assert "Say that again" in spoken, "no item is lost"


def test_a_heading_is_not_spoken_as_a_hash():
    assert _spoken("## Lunch\nSandwiches and soup.") == "Lunch, Sandwiches and soup."


def test_a_marker_only_chunk_is_dropped_rather_than_synthesized():
    # split_clauses_ramp drops what has nothing synthesizable; run_tts has no fallback,
    # so a chunk that is only a bullet must not survive as an empty utterance.
    assert split_clauses_ramp("- ") == []
    assert not has_speech("- ")


def test_mid_sentence_punctuation_survives_the_strip():
    # The en dash is a clause seam _CLAUSE_SPLIT depends on, and a hyphen inside a word
    # is speech. Only a LINE-LEADING hyphen-minus is a marker.
    keep = "Pastries – cookies, made-to-order sandwiches, and fresh-baked bread."
    assert _spoken(keep) == keep
    assert _spoken("Ten - twelve minutes.") == "Ten - twelve minutes."


def test_a_decimal_at_a_line_start_is_not_an_ordered_list():
    # "3." only opens a list when whitespace follows it; "3.5" is a number.
    assert _spoken("3.5 percent of the total.") == "3.5 percent of the total."


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all spoken-markdown tests passed")
