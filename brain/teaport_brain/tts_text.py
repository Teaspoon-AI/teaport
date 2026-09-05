#
# teaport demo — shared TTS text/pacing helpers (no model dependencies)
#

import difflib
import os
import re


def _wordbreak_max(piece: str, max_chars: int) -> list:
    """Break `piece` into word-bounded sub-pieces each <= max_chars. A single word
    longer than max_chars is hard-sliced (degenerate, but never exceeds the cap)."""
    if len(piece) <= max_chars:
        return [piece]
    pieces, cur = [], ""
    for w in piece.split():
        while len(w) > max_chars:  # a single mega-token: hard-slice it
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(w[:max_chars])
            w = w[max_chars:]
        if cur and len(cur) + 1 + len(w) > max_chars:
            pieces.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        pieces.append(cur)
    return pieces


# The unspeakable-character table, defined ONCE. llm_text_guard folds the streamed
# deltas with it (cleaning speech and committed history together) and _normalize_for_tts
# folds whatever reaches the synth. It lived in both files, which already disagreed on
# ellipses and would have drifted further the next time the model surfaced a new code
# point — the exact failure this table exists to fix. This module has no pipecat or
# model imports, so the guard can import from here and not the other way round.
#
# Explicit \uXXXX escapes ONLY: a literal character class once silently folded an
# ordinary space into itself, which is invisible in the source and survives review
# because the line looks right.
_NOBREAK_SPACES = re.compile("[\u00a0\u202f]")
_ZERO_WIDTH = re.compile("[\u200b\u2060\ufeff]")
_MD_BOLD = re.compile(r"\*{2,}")

# A run of three or more dots. Shared with llm_text_guard and raw_llm_capture, which
# count these as a collapse marker. MIN_DOT_RUN is exported alongside the pattern and
# BUILDS it, so the length can only be changed in one place: llm_text_guard scans
# character-by-character rather than by regex (it counts incrementally across frame
# boundaries) and previously hard-coded its own 3, so widening this pattern would have
# made the capture log and the guard disagree about how many markers a completion had.
MIN_DOT_RUN = 3
DOT_RUN = re.compile(r"\.{%d,}" % MIN_DOT_RUN)

# Sentence-repeat degeneracy: a completion that says the same sentence twice. Shared
# by raw_llm_capture (which logs the raw completion when it happens) and
# llm_text_guard (which drops the duplicate before it is spoken), for the same
# reason MIN_DOT_RUN lives here: two private copies of "what counts as a repeat"
# would drift, and then the capture log and the guard would disagree about the same
# completion.
#
# NEAR-equality, not equality. Live 2026-08-26 08:48 (single completion, single
# stream) the model generated its whole reply a second time and re-sampled the
# spelled-out numbers on the second pass — "four thousand five hundred eight" became
# "four five zero eight" — so the two copies measured 0.949 similar, and a verbatim
# comparison saw nothing. Unrelated sentences from the same session's replies
# measure far lower: the two most list-alike controls score 0.55 and 0.38, and a
# short echo like "I can do that." vs "I can do that for you now." scores 0.70.
# 0.9 splits the populations with margin on both sides. The 08:32 verbatim doubling
# scores 1.0 and is still caught.
#
# 15 chars filters out the short repeats that are ordinary speech ("No." / "No.").
MIN_REPEAT_CHARS = 15
REPEAT_SIMILARITY = 0.9
# Split AFTER sentence punctuation whether or not whitespace follows: the live
# 08:32 doubling arrived as "...right on Burnet.If you're undecided..." with no
# separator at all, and a split that required a following space kept both copies
# in one "sentence" that then matched nothing.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s*")


def is_sentence_repeat(a: str, b: str) -> bool:
    """True when two sentences are the same modulo re-sampled detail."""
    m = difflib.SequenceMatcher(None, a, b)
    # The quick ratios are cheap upper bounds; the full ratio only runs on pairs
    # they cannot already rule out, which keeps the pairwise scan off the hot
    # path's budget.
    return (m.real_quick_ratio() > REPEAT_SIMILARITY
            and m.quick_ratio() > REPEAT_SIMILARITY
            and m.ratio() > REPEAT_SIMILARITY)


def fold_unspeakable(text: str) -> str:
    """Fold characters that have no phonemes but reach the synth verbatim.

    U+202F (narrow no-break space), U+2011 (non-breaking hyphen) and U+200B
    (zero-width space) were all present in live degenerate completions (2026-08-12),
    inside otherwise ordinary words, and none was folded. "**" is markdown that the
    system prompt forbids, so it is never speech and is stripped rather than folded —
    raw_llm_capture counts it as a degeneracy signal for the same reason.

    U+2019 is here for a different reason and must not be dropped as cosmetic: it has a
    perfectly good phoneme, but the ENGINE rewrites it to a plain apostrophe in the word
    timestamps it returns, while the text we handed it keeps the curly one. The caption
    sequencer matches those word-for-word against the slot built from our text, so the
    first word containing one misses — and because it walks the slot in order, every word
    after it misses too. The whole reply is then emitted as passthrough and the assistant
    bubble never renders, while the audio plays perfectly.

    Verified against the live engine 2026-08-25: of the typographic characters a model
    actually emits, it normalizes exactly U+2019 -> ' and U+2011 -> -, and echoes curly
    double quotes and em dashes back unchanged. Only the two it rewrites need folding,
    and folding them costs nothing: the engine was going to speak them that way anyway.

    This bit whenever a reply contained an apostrophe, so it arrived with the switch to a
    model that writes typographically."""
    text = _NOBREAK_SPACES.sub(" ", text)     # no-break spaces -> space
    text = _ZERO_WIDTH.sub("", text)          # zero-width chars -> removed
    text = text.replace("\u2011", "-")        # non-breaking hyphen -> hyphen
    text = text.replace("\u2019", "'")        # curly apostrophe -> straight
    text = _MD_BOLD.sub("", text)             # markdown bold -> removed
    return text


def _normalize_for_tts(text: str) -> str:
    """Strip punctuation that has no phonemes but derails the synth. The LLM sometimes
    emits ellipses (unicode U+2026 or "...") and non-breaking spaces (U+00A0); the
    sentence splitter isolates those into punctuation-only chunks, and the engine TTS then
    fails the whole clause ("did not receive a valid HTTP response") and emits 0.0s
    audio. Fold the unspeakable family -> plain equivalents and ellipses/dot-runs -> a
    comma pause."""
    text = fold_unspeakable(text)
    # A whole run of ellipses/dot-runs -> ONE comma pause. The run must be matched as a
    # unit: "a... ... b" folded per-item gives "a, , b", and the trailing \s* is what
    # absorbs the space llm_text_guard's own ellipsis-run fold leaves behind, so text
    # that passed through both becomes ", " and not ",  ".
    text = re.sub(r"(?:(?:\u2026|\.{2,})\s*)+", ", ", text)
    return text


# Any script's letters/digits, minus underscore -- see the note at the end of
# split_clauses_ramp for why not [A-Za-z0-9].
_SPEECH = re.compile(r"[^\W_]")

# Clause boundaries inside an over-long sentence (split_clauses_ramp, soft_max): a
# clause mark followed by whitespace, or a full-width one (CJK writes no space after
# it). The whitespace requirement is what keeps "1,000" and "10:10" whole.
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:—–])\s+|(?<=[，、；：])")
_NO_SPACE_AFTER = "，、；：。！？"

# A web address as a model writes it into prose. Spoken aloud it is "nvd dot nist dot
# gov slash vuln slash …" for ten seconds (live 2026-09-04: a five-item consult delivery
# read five of them) — the persona now names sources instead, and the delivery text
# has these removed before the model sees it (agent_session). Trailing sentence
# punctuation is not part of the address.
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+?(?=[.,;:!?)]*(?:\s|$))")


def strip_urls_for_speech(text: str) -> str:
    """Remove web addresses from text that will be spoken, tidying the space they
    leave (" at https://x.y, and" -> " at, and" is still a sentence the model can
    rephrase; an address alone in parentheses goes with its parentheses)."""
    if "http" not in text and "www." not in text:
        return text
    text = re.sub(r"\(\s*" + _URL.pattern + r"\s*\)", "", text)
    text = _URL.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" ([.,;:!?])", r"\1", text)
    return text


def has_speech(text: str) -> bool:
    """Whether the engine TTS will synthesize anything for `text`: after the same
    normalization run_tts applies, at least one letter or digit in any script.
    This is THE predicate for "nothing synthesizable": split_clauses_ramp drops
    every chunk failing it and run_tts then yields no audio and no context. The
    ledger applies it to a response's text before queuing the response as an
    expected TTS context -- one that will never open must not sit at the queue's
    head for the next context to claim (brain/formal/LedgerPlayout.tla,
    lp_wrongText_unspeakable). One regex, so the two cannot drift."""
    return bool(_SPEECH.search(_normalize_for_tts(text or "")))


def split_clauses_ramp(text: str, first_max: int = 32, growth: float = 1.5,
                       cap: int = 200, hard_max: int = 350, soft_max: int = 80) -> list:
    """Ramp-up chunking for streaming TTS: at sentence boundaries, and inside a
    sentence only when the sentence is long.

    A chunk is normally one or more WHOLE sentences. Earlier this cut every clause
    (commas/colons) and word-broke a long opening clause, but every chunk is
    synthesized as an independent TTS utterance, so a sub-sentence boundary makes the
    boundary word get utterance-final prosody (an unnatural emphasis/fall) instead of
    mid-phrase continuation. Sentence boundaries are genuine prosodic pauses, so
    chunking there has no audible seam — and a sentence up to `soft_max` chars is
    still never split.

    Above `soft_max` the trade flips. The engine synthesizes a chunk whole before
    any of it can play, so a long sentence IS the first-audio latency: measured live
    2026-09-04, a 236-char sentence (18.8 s of audio) began 4.6 s after the model
    finished it, a ~30 s one 6.6 s — 8 s of silence that the user read as "it's
    done" and talked over. gpt-oss writes list answers as one comma-chained sentence,
    so this is the common shape for a consult delivery, not an edge. A sentence over
    `soft_max` is therefore split at its clause boundaries (", ; : — –", and the
    CJK "，、；："), and the pieces ride the same ramp as sentences do — a comma seam
    every few seconds inside a run-on list reads as the list's own rhythm. A comma
    with no space after it ("1,000") is not a boundary. `soft_max=0` disables this.
    80, not 120: at 120 a 112-char forecast sentence still went out as one call --
    6.4 s of audio, 1.7 s of synthesis before its first word (live 22:22) -- and 80
    chars is about five seconds of speech, the longest wait worth keeping for the
    sake of an unbroken sentence.

    A small first chunk gates first-audio; each later chunk may grow up to `growth`x
    the previous chunk's length, accumulating whole pieces and stretching to the next
    boundary. (growth < 1/RTF avoids mid-reply stalls; on the GPU backend RTF is tiny
    so this is moot, but the ramp is harmless.)

    A single sentence with no internal punctuation at all (e.g. the Tale of Two Cities
    run-on) would otherwise become one chunk that overflows the engine's max utterance
    length (~512 tokens) and CRASHES the synth, aborting the whole reply. So as a last
    resort a chunk longer than `hard_max` chars is word-broken — a mid-word-group seam
    beats a dropped reply. hard_max is in chars, kept well under the token limit."""
    text = _normalize_for_tts(text)
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    if soft_max:
        parts = [piece for p in parts
                 for piece in (_CLAUSE_SPLIT.split(p) if len(p) > soft_max else [p])]
    out, cur, limit = [], "", first_max
    for p in parts:
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            limit = min(cap, max(first_max, int(round(len(cur) * growth))))
            cur = p
        else:
            # Pieces rejoin with the space the split consumed -- none after a
            # full-width mark, where the source had none either.
            sep = "" if cur and cur[-1] in _NO_SPACE_AFTER else " "
            cur = f"{cur}{sep}{p}".strip()
    if cur:
        out.append(cur)
    if hard_max:
        out = [sub for chunk in out for sub in _wordbreak_max(chunk, hard_max)]
    # Drop chunks with nothing synthesizable (pure punctuation/whitespace) — the engine TTS
    # fails them ("did not receive a valid HTTP response") and yields 0.0s "audio".
    #
    # [^\W_] (any script's letters/digits, minus underscore) rather than [A-Za-z0-9]:
    # the ASCII class matched nothing in Japanese, Mandarin or Hindi, so EVERY reply in
    # those languages was dropped here and survived only because run_tts fell back to
    # `or [text]`. That fallback then also resurrected the punctuation-only junk this
    # line had just deliberately dropped, handing the engine raw "…\xa0\xa0\n\n" and
    # earning a 500 per clause. Getting the test right is what LET the fallback go:
    # run_tts no longer has one, so an empty list here means "nothing to speak" and is
    # honoured rather than overridden. Keep the two facts together — restoring the
    # fallback without widening this test brings the 500-per-clause bug straight back.
    out = [c for c in out if _SPEECH.search(c)]     # has_speech, per chunk
    return out


# Caption UX lead, shared by engine_tts.py (schedules each word's caption pts EARLY
# by this much) and transcript_ledger.py (backs the same lead OUT of heard-word
# accounting on a barge-in). One constant so the two can't drift.
CAPTION_LEAD_SECS = float(os.getenv("TTS_CAPTION_LEAD_SECS", "0.2"))
