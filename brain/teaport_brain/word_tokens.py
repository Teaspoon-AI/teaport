#
# word_tokens.py — the caller's words as the brain compares them, in one rule, so the
# barge-in guard's stop words (barge_pause) and a sliced turn's texts (stt.py TURN TEXT:
# its cut's margin, its correction, whether a barge-in has words) agree, in any script.
#
# A word is a run of letters, combining marks (Devanagari's vowel signs, Arabic's
# harakat), digits and apostrophes, casefolded; anything else separates words, and an
# apostrophe at either end is not part of one. A script written without spaces gives its
# run as one word. "don’t" is "don't"; "A.D., 1000" is "a", "d", "1000".
#
import unicodedata


def word_tokens(text: str) -> list:
    """The words of `text` (see the module header)."""
    out, word = [], []
    for ch in (text or "").casefold().replace("’", "'"):
        if ch.isalnum() or ch == "'" or unicodedata.category(ch)[0] == "M":
            word.append(ch)
        elif word:
            out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return [w for w in (w.strip("'") for w in out) if w]
