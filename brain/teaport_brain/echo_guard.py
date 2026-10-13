#
# echo_guard.py — the room mic must not hear the bot's own voice as the user.
#
# The XVF3800 cancels the speaker's echo, and its suppressor is off so the user is heard
# over the bot, unless the bridge measures the echo within LOCAL_AUDIO_ECHO_MARGIN_DB of
# the user's voice (xvf_echo.py). What echo is left can still reach the STT as words:
# live 2026-10-10 at volume 80 with the suppressor off, the bot's story came back as
# user turns ("A curious", "In a quiet orchard of magic", "The tiny pistachio") and the
# bot kept interrupting itself; at volume 65 in a controlled take, the bot's words were
# prefixed to the user's ("This began with a camera. Is it a horned owl?").
#
# So on the room mic's sessions (a client that says it is at the box, ?features=local)
# the STT asks the guard before it pushes any transcript, as it asks the wake gate:
#
#   * BotWords records every word the speaker PLAYED, as it plays: the tap sits below
#     the output transport, which releases each TTSTextFrame at its word's playout time
#     and drops the ones a barge-in cut (captions.CaptionTap relies on the same).
#   * strip() takes off the front of a transcript the longest run of words that follows
#     what the bot said, in order. The rest is the user's. A transcript that is ALL the
#     bot's words is nothing: an interim is not pushed (it cannot start a barge-in), a
#     final closes its segment without words.
#
# When. An echo reaches the STT a fixed while after its word played: the speaker's lead,
# the STT's delay and its decode -- 1.4-1.7 s from a word's playout to its first interim
# on that session (the trace of "A curious", "In quiet"). So the guard decides per
# SEGMENT, from when its first words arrived (`since`), not from when the final lands
# (seconds later): the segment can be the bot's only if the bot was still playing
# ACTIVE_SECS before it (an echo of its very last word arrives ~1.5 s after it), and a
# run counts only if its first word played in the LOOKBACK_SECS before and at least
# MIN_LAG_SECS before. A user who answers once the bot has finished is
# never matched against what it said ("the blue one" after "...or the blue one?"), and
# an old phrase is never matched at all.
#
# What. A run follows the bot word for word, with slips: a bot word the STT missed, a
# word it misheard or inserted -- one slip once two words match, one more per
# WORDS_PER_SLIP after; and a first word clipped to a fragment ("ny pistachio"). Only a
# run long enough not to be a coincidence is taken, counting matched words only
# (MIN_RUN_CHARS letters or MIN_RUN_WORDS words): "is it a" is not the bot's because the
# bot just said it. A single word is the bot's only if it is the whole transcript, long,
# and not a stop word ("wait" stops the bot, whatever the bot said).
#
# What it gets wrong: the user repeating the bot's last words straight back while it is
# still talking. That costs those words, not the turn.
#
import collections
import time
import unicodedata

from loguru import logger
from pipecat.frames.frames import Frame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.barge_pause import is_stop_utterance
from teaport_brain.settings import setting
from teaport_brain.wake_words import normalize

ENABLED = setting("TEAPORT_ECHO_GUARD")

# A segment can be the bot's only if the bot played a word at most this long before its
# first words arrived...
ACTIVE_SECS = 2.0
# ...and a run's first word must have played at most this long before them...
LOOKBACK_SECS = 3.0
# ...and at least this long before (no echo is faster than the STT's own delay).
MIN_LAG_SECS = 0.3
# Played words kept: a segment can span the whole stretch of the bot it heard.
KEEP_SECS = 60.0
# A run taken as the bot's: at least this many matched letters or matched words.
MIN_RUN_CHARS = 8
MIN_RUN_WORDS = 4
# One slip is allowed once two words match, and one more per this many matched.
WORDS_PER_SLIP = 4


class BotWords:
    """The words the speaker played, normalized, with when they played."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._words: collections.deque = collections.deque()

    def played(self, text: str) -> None:
        now = self.clock()
        for w in normalize(text)[0].split():
            self._words.append((now, w))
        while self._words and now - self._words[0][0] > KEEP_SECS:
            self._words.popleft()

    def since(self, since: float) -> list[tuple[float, str]]:
        """(time, word) from LOOKBACK_SECS before `since` on; none when the bot played
        nothing in the ACTIVE_SECS before it (it was not talking when this speech began)."""
        before = [t for t, _ in self._words if t <= since]
        if not before or since - before[-1] > ACTIVE_SECS:
            return []
        start = since - LOOKBACK_SECS
        return [(t, w) for t, w in self._words if t >= start]


def echo_run(words: list[str], bot: list[str], starts,
             behind_fragment: int = 2) -> tuple[int, list[str]]:
    """The longest run of `words`, from the first, that follows `bot` in order from one
    of `starts` (indices into `bot`): how many words it covers, and the ones that
    matched. The run ends on a matched word. A clipped first word counts only with
    `behind_fragment` matched words after it."""
    best = (0, [])
    for start in starts:
        # The first word as said, or clipped to a fragment the bot's word does not match.
        for skip in (0, 1):
            if skip >= len(words) or words[skip] != bot[start]:
                continue
            i, j, matched, slips = skip, start, [], 0
            covered = 0
            while i < len(words) and j < len(bot):
                if words[i] == bot[j]:
                    matched.append(words[i])
                    i, j = i + 1, j + 1
                    covered = i
                    continue
                if len(matched) < 2 or slips >= 1 + (len(matched) - 2) // WORDS_PER_SLIP:
                    break
                slips += 1
                if j + 1 < len(bot) and words[i] == bot[j + 1]:
                    j += 1                      # a bot word the STT missed
                elif i + 1 < len(words) and words[i + 1] == bot[j]:
                    i += 1                      # a word the STT inserted
                else:
                    i, j = i + 1, j + 1         # one it misheard
            if skip and len(matched) < behind_fragment:
                continue                        # a fragment needs a run behind it
            if covered > best[0]:
                best = (covered, matched)
        if best[0] == len(words):
            break
    return best


def _taken(matched: list[str], whole: bool) -> bool:
    if len(matched) == 1:
        # One word: the bot's only when it is the whole transcript, long, and not a stop.
        return whole and len(matched[0]) >= 5 and not is_stop_utterance(matched[0])
    return len(matched) >= MIN_RUN_WORDS or sum(len(w) for w in matched) >= MIN_RUN_CHARS


class EchoGuard:
    """The STT's question: what of this transcript is the user's (strip)."""

    def __init__(self, clock=time.monotonic):
        self.bot = BotWords(clock)

    def strip(self, text: str, final: bool, since: float | None = None) -> str:
        """`text` less a leading run of the bot's words (as said: NFKC, from the user's
        first word, its opening punctuation kept); "" when it is all the bot's; `text`
        itself otherwise. `since`: when the segment's first words arrived (now if None).

        An interim that so far follows the bot word for word is "" however short: it is
        the start of an echo or of the user's words, and the next interim tells which --
        pushed now, its first two words would already be a barge-in. A stop word never
        waits."""
        since = self.bot.clock() if since is None else since
        heard = self.bot.since(since)
        norm, idx = normalize(text)
        words = norm.split()
        if not heard or not words:
            return text
        bot = [w for _, w in heard]
        starts = [j for j, (t, _) in enumerate(heard) if t <= since - MIN_LAG_SECS]
        # An interim is held on less: one word after a fragment may still become a run.
        k, matched = echo_run(words, bot, starts, behind_fragment=2 if final else 1)
        if not final and k and k == len(words) and not is_stop_utterance(text):
            return ""
        if k == 0 or not _taken(matched, whole=k == len(words)):
            return text
        if k == len(words):
            return ""
        # Where word k starts in NFKC(text): its first character's index in `norm`...
        at = idx[sum(len(w) + 1 for w in words[:k])]
        src = unicodedata.normalize("NFKC", text)
        # ...and the opening punctuation that belongs to it ("¿Qué", "(that", '"Why').
        while at > 0 and (unicodedata.category(src[at - 1]) in ("Ps", "Pi")
                          or src[at - 1] in "¿¡\"'"):
            at -= 1
        return src[at:].strip()

    def tap(self) -> "PlayedWordsTap":
        return PlayedWordsTap(self.bot)


class PlayedWordsTap(FrameProcessor):
    """Below the output transport: each TTSTextFrame passes as its word plays."""

    def __init__(self, bot: BotWords, **kwargs):
        super().__init__(**kwargs)
        self._bot = bot

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._bot.played(frame.text)
        await self.push_frame(frame, direction)


def log_strip(kind: str, text: str, kept: str) -> None:
    if kept:
        logger.info(f"echo guard: {kind} began with the bot's own words; kept {kept!r} "
                    f"of {text!r}")
    else:
        logger.info(f"echo guard: {kind} {text!r} is the bot's own words; dropped")
