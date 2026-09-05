#
# Unit test: split_clauses_ramp's long-sentence split, and strip_urls_for_speech.
#
# The engine synthesizes a chunk whole before any of it can play, so a long sentence
# IS the first-audio wait. Live 2026-09-04 on the appliance: a 236-char consult answer
# (18.8 s of audio) began 4.6 s after the model finished it; a ~30 s one took 6.6 s,
# and the 8 s of silence that made read as "it's done" -- the user talked over the
# delivery and it was retired at 10% heard. gpt-oss writes list answers as one
# comma-chained sentence, so this is the common consult shape, not an edge. Above
# soft_max a sentence is now split at its clause boundaries and the pieces ride the
# ramp; up to soft_max a sentence keeps its prosody untouched.
#
# Run: python test_clause_ramp.py   (or via the suite)
#

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
from teaport_brain.tts_text import split_clauses_ramp, strip_urls_for_speech  # noqa: E402

# The live 21:05 delivery, one sentence, 21.5 s of audio (URLs already stripped).
LIST_SENTENCE = (
    "About that Hacker News list: the top five stories right now are an exploited "
    "Chromium sandbox RCE, a formal proof of Fermat's Last Theorem, a new OpenAI agent "
    "message board, the Artificial Analysis Intelligence Index version four point two, "
    "and a European static-site hosting service called Statichost.")
# The live 20:53 forecast: 136 chars, 8.5 s, one sentence.
FORECAST = ("Tomorrow in Austin looks mostly sunny, with highs around seventy-four degrees "
            "and lows near fifty-four, and only a slight chance of rain.")


def _rejoined(chunks):
    return " ".join(chunks)


def test_a_long_sentence_is_split_at_its_clauses_and_the_first_chunk_is_small():
    chunks = split_clauses_ramp(LIST_SENTENCE)
    assert len(chunks) >= 3, chunks
    assert len(chunks[0]) <= 60, f"first chunk gates first-audio: {chunks[0]!r}"
    assert chunks[0].endswith((",", ":")), f"split at a clause mark, not mid-phrase: {chunks[0]!r}"
    assert _rejoined(chunks) == LIST_SENTENCE, "nothing lost or reordered"
    assert all(len(c) <= 200 for c in chunks), "later chunks still obey the ramp cap"
    print(f"  PASS {len(LIST_SENTENCE)}-char list sentence -> {len(chunks)} chunks, "
          f"first {len(chunks[0])} chars")


def test_the_forecast_shape_gets_a_short_first_chunk():
    chunks = split_clauses_ramp(FORECAST)
    assert chunks[0] == "Tomorrow in Austin looks mostly sunny,", chunks
    assert _rejoined(chunks) == FORECAST
    print(f"  PASS forecast -> {[len(c) for c in chunks]}")


def test_a_sentence_up_to_soft_max_is_never_split():
    s = "It is known for its vibrant culinary scene, especially its famous ceviche."  # 74 chars
    assert len(s) <= 80
    assert split_clauses_ramp(s) == [s], split_clauses_ramp(s)
    # Just over it, a sentence IS split (live 22:22: 112 chars under the old 120 cap
    # was one 6.4 s call, 1.7 s to first audio).
    long = ("Expect Thursday in Austin to be hot, around ninety degrees, mostly sunny with "
            "a small chance of afternoon showers.")
    assert len(long) > 80
    chunks = split_clauses_ramp(long)
    assert len(chunks) >= 2 and chunks[0] == "Expect Thursday in Austin to be hot,", chunks
    # ...and soft_max=0 switches the clause split off entirely.
    assert split_clauses_ramp(LIST_SENTENCE, soft_max=0) == [LIST_SENTENCE]
    print("  PASS sentences up to 80 chars keep their prosody; longer ones split; soft_max=0 disables")


def test_sentence_boundaries_still_come_first():
    text = "Four. A lemon is yellow."
    assert split_clauses_ramp(text) == [text], "two short sentences pack as before"
    two = "Lima sits on the Pacific coast and is the capital city of Peru. It is known for ceviche."
    chunks = split_clauses_ramp(two)
    assert chunks[0] == "Lima sits on the Pacific coast and is the capital city of Peru.", chunks
    print("  PASS sentence packing unchanged")


def test_numbers_and_times_are_not_clause_boundaries():
    s = ("The budget came to 1,250,000 dollars in the end, which the board approved at "
         "10:30 on Tuesday, after a very long and very detailed and very tiring debate "
         "about the parking garage.")
    assert len(s) > 120
    chunks = split_clauses_ramp(s)
    assert any("1,250,000" in c for c in chunks), chunks
    assert any("10:30" in c for c in chunks), chunks
    assert _rejoined(chunks) == s
    print("  PASS 1,250,000 and 10:30 stay whole")


def test_a_run_on_without_any_clause_mark_still_word_breaks_at_hard_max():
    s = " ".join(["word"] * 100)   # 499 chars, no punctuation at all
    chunks = split_clauses_ramp(s, hard_max=350)
    assert len(chunks) >= 2 and all(len(c) <= 350 for c in chunks), [len(c) for c in chunks]
    print("  PASS hard_max word-break unchanged")


def test_cjk_long_sentence_splits_at_full_width_commas():
    s = ("今日はとても良い天気で、公園には多くの人が集まり、子供たちは走り回り、" * 4)
    assert len(s) > 120
    chunks = split_clauses_ramp(s)
    assert len(chunks) >= 2, chunks
    assert "".join(chunks) == s, "CJK rejoins with no spaces"
    print("  PASS CJK splits at 、")


def test_strip_urls_for_speech():
    cases = [
        ("Actively exploited sandbox RCE at https://nvd.nist.gov/vuln/detail/CVE-2026-85046, "
         "a formal proof at https://www.anthropic.com/research/fermat.",
         "Actively exploited sandbox RCE at, a formal proof at."),
        ("Statichost.eu (https://www.statichost.eu) is European.", "Statichost.eu is European."),
        ("See www.example.com for details.", "See for details."),
        ("No addresses here, 3.5 percent and e.g. this.", "No addresses here, 3.5 percent and e.g. this."),
        ("", ""),
    ]
    for src, want in cases:
        got = strip_urls_for_speech(src)
        assert got == want, f"{src!r} -> {got!r} (want {want!r})"
    print("  PASS URLs stripped for speech")


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()


if __name__ == "__main__":
    main()
    print("ALL PASS")
