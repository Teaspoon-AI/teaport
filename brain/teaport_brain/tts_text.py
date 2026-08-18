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


def _normalize_for_tts(text: str) -> str:
    """Strip punctuation that has no phonemes but derails the synth. The LLM sometimes
    emits ellipses (unicode U+2026 or "...") and non-breaking spaces (U+00A0); the
    sentence splitter isolates those into punctuation-only chunks, and the engine TTS then
    fails the whole clause ("did not receive a valid HTTP response") and emits 0.0s
    audio. Fold nbsp -> space and ellipses/dot-runs -> a comma pause. \\u escapes keep
    this source pure-ASCII."""
    text = text.replace(" ", " ")               # non-breaking space -> space
    text = re.sub(r"[…]+|\.{2,}", ", ", text)    # …  or  ...  -> comma pause
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
    # [^\W_] (letters/digits in ANY script, minus underscore), not [A-Za-z0-9]: the ASCII
    # class matches nothing in Japanese, Mandarin or Hindi, so EVERY reply in those
    # languages was dropped here. They reach the engine today only because run_tts falls
    # back to `or [text]` when this returns empty — i.e. three of the nine shipped voice
    # languages have been riding an error path, and get no clause ramping at all.
    # Verified on the deployed brain: Japanese, Mandarin and Hindi go from dropped to
    # kept, while "..", "\u2026\xa0\xa0\n\n?" and friends are still correctly dropped.
    out = [c for c in out if re.search(r"[^\W_]", c)]
    return out


# Caption UX lead, shared by engine_tts.py (schedules each word's caption pts EARLY
# by this much) and transcript_ledger.py (backs the same lead OUT of heard-word
# accounting on a barge-in). One constant so the two can't drift.
CAPTION_LEAD_SECS = float(os.getenv("TTS_CAPTION_LEAD_SECS", "0.2"))
