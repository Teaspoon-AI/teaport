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
