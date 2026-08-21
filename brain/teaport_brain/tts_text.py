#
# teaport demo — shared TTS text/pacing helpers (no model dependencies)
#

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
# count these as a collapse marker.
DOT_RUN = re.compile(r"\.{3,}")


def fold_unspeakable(text: str) -> str:
    """Fold characters that have no phonemes but reach the synth verbatim.

    U+202F (narrow no-break space), U+2011 (non-breaking hyphen) and U+200B
    (zero-width space) were all present in live degenerate completions (2026-08-12),
    inside otherwise ordinary words, and none was folded. "**" is markdown that the
    system prompt forbids, so it is never speech and is stripped rather than folded —
    raw_llm_capture counts it as a degeneracy signal for the same reason."""
    text = _NOBREAK_SPACES.sub(" ", text)     # no-break spaces -> space
    text = _ZERO_WIDTH.sub("", text)          # zero-width chars -> removed
    text = text.replace("\u2011", "-")        # non-breaking hyphen -> hyphen
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


def split_clauses_ramp(text: str, first_max: int = 32, growth: float = 1.5,
                       cap: int = 200, hard_max: int = 350) -> list:
    """Ramp-up chunking for streaming TTS, splitting ONLY at sentence boundaries.

    A chunk is one or more WHOLE sentences — never split mid-sentence. Earlier this
    cut at clause punctuation (commas/colons) and word-broke a long opening clause,
    but every chunk is synthesized as an independent TTS utterance, so a
    sub-sentence boundary made the boundary word get utterance-final prosody (an
    unnatural emphasis/fall) instead of mid-phrase continuation. Sentence boundaries
    are genuine prosodic pauses, so chunking there has no audible seam.

    A small first chunk still gates first-audio; each later chunk may grow up to
    `growth`x the previous chunk's length, accumulating whole sentences and stretching
    to the next sentence boundary. (growth < 1/RTF avoids mid-reply stalls; on the
    GPU backend RTF is tiny so this is moot, but the ramp is harmless.)

    A single sentence with no internal '.!?' (e.g. the Tale of Two Cities run-on) would
    otherwise become one chunk that overflows the engine's max utterance length (~512
    tokens) and CRASHES the synth, aborting the whole reply. So as a last resort a chunk
    longer than `hard_max` chars is word-broken — a mid-sentence prosody seam beats a
    dropped reply. hard_max is in chars, kept well under the token limit."""
    text = _normalize_for_tts(text)
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out, cur, limit = [], "", first_max
    for p in parts:
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            limit = min(cap, max(first_max, int(round(len(cur) * growth))))
            cur = p
        else:
            cur = f"{cur} {p}".strip()
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
    out = [c for c in out if re.search(r"[^\W_]", c)]
    return out


# Caption UX lead, shared by engine_tts.py (schedules each word's caption pts EARLY
# by this much) and transcript_ledger.py (backs the same lead OUT of heard-word
# accounting on a barge-in). One constant so the two can't drift.
CAPTION_LEAD_SECS = float(os.getenv("TTS_CAPTION_LEAD_SECS", "0.2"))
