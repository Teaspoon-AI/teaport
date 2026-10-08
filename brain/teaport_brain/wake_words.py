#
# wake_words.py — wake phrases matched in transcripts: normalization and the cut.
#
# Shared by the brain's wake gate (wake_gate.py, which applies it to the STT's finals)
# and the local audio bridge (local_audio.py, which only parses the list to say what it
# listens for). No Pipecat import: the bridge stays small.
#
# Matching is on normalized text (NFKC, casefold, punctuation and symbols as spaces, runs
# of space collapsed), whole words, any script. No fuzziness: "teapot" does not wake a
# box listening for "teaport" unless the user lists it. A phrase in a script written
# without spaces (Chinese, Japanese, Thai, ...) is matched inside a run of text.
#
import unicodedata

# Scripts written without spaces between words: a phrase in them matches inside a run.
_NO_SPACE_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR")


def _unspaced(ch: str) -> bool:
    name = unicodedata.name(ch, "")
    return any(name.startswith(s) for s in _NO_SPACE_SCRIPTS)


def normalize(text: str) -> tuple[str, list[int]]:
    """`text` normalized for matching, and for each of its characters the index in
    NFKC(text) it came from: NFKC, casefold, every punctuation/symbol/control character a
    space, runs of space one, no space at the ends. Letters, marks and digits of every
    script are kept."""
    src = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(src):
        if ch.isspace() or unicodedata.category(ch)[0] in "PSCZ":
            if out and out[-1] != " ":
                out.append(" ")
                idx.append(i)
            continue
        for c in ch.casefold():
            out.append(c)
            idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def parse_phrases(raw: str) -> list[str]:
    """LOCAL_AUDIO_WAKE_WORDS (a comma list, any script) -> its normalized phrases, in
    order, empty ones and repeats dropped."""
    out: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").replace(";", ",").split(","):
        phrase = normalize(part)[0]
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def find_wake(text: str, phrases: list[str]) -> tuple[str, str] | None:
    """The first wake phrase in `text` (the earliest; the longest of those starting
    there), and what follows it, as said (NFKC, its leading punctuation dropped). None
    when no phrase is in it."""
    norm, idx = normalize(text)
    best = None
    for phrase in phrases:
        start = 0
        while (at := norm.find(phrase, start)) >= 0:
            end = at + len(phrase)
            if ((at == 0 or norm[at - 1] == " " or _unspaced(phrase[0]))
                    and (end == len(norm) or norm[end] == " " or _unspaced(phrase[-1]))):
                if best is None or at < best[0] or (at == best[0] and end > best[1]):
                    best = (at, end, phrase)
                break
            start = at + 1
    if best is None:
        return None
    at, end, phrase = best
    src = unicodedata.normalize("NFKC", text)
    rest = src[idx[end - 1] + 1:]
    # The punctuation the STT put after the phrase ("Hey Teaport, what's ..."): not words.
    i = 0
    while i < len(rest) and (rest[i].isspace() or unicodedata.category(rest[i])[0] in "PZ"):
        i += 1
    return phrase, rest[i:].strip()


def carry_tail(text: str, phrases: list[str]) -> str:
    """The end of a final that held no wake phrase, normalized, as much as a phrase SPLIT
    across two finals ("Hey." / "Teaport, what's ...") could have begun in: at most the
    longest phrase's length less one character, whole words, and at most its word count
    less one words (one run, for a phrase in a script without spaces). Empty when no
    phrase can be split."""
    norm = normalize(text)[0]
    if not phrases or not norm:
        return ""
    unspaced = any(_unspaced(c) for p in phrases for c in p)
    chars = max(len(p) for p in phrases) - 1
    words = max(max(len(p.split()) for p in phrases) - 1, 1 if unspaced else 0)
    if chars <= 0 or words <= 0:
        return ""
    tail = norm[-chars:]
    if len(norm) > chars and norm[-chars - 1] != " " and not _unspaced(tail[0]):
        tail = tail.split(" ", 1)[1] if " " in tail else ""  # no half word at its start
    return " ".join(tail.split()[-words:])


def join_carry(tail: str, text: str) -> str:
    """`tail` (carry_tail of the previous final) and the next final, as one text: no
    space between them where both sides are in a script written without spaces."""
    if not tail:
        return text
    head = normalize(text)[0][:1]
    sep = "" if head and _unspaced(tail[-1]) and _unspaced(head) else " "
    return tail + sep + text
