#
# echo_guard.py — the room mic must not hear the bot's own voice as the user.
#
# The XVF3800 cancels the speaker's echo, and the bridge holds its suppressor at a set
# that takes the rest of the echo but lets the user through over the bot (xvf_echo.py,
# measured 2026-11-02). What echo leaks can still reach the STT as words: live
# 2026-10-10 at volume 80, with the suppressor then off, the bot's story came back as
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
# bot just said it. A single word is the bot's only if it is the whole transcript and
# long. Stop words are the exception the other way: a transcript that is ALL stop words
# ("Wait.", "Hold on.") cuts the reply at any length, paused or not (#216,
# barge_pause.STOP_WORDS), so when it is wholly a run of the bot's own words it is the
# bot's, however short -- and its interim is held like any other echo-like one.
#
# What it gets wrong: the user repeating the bot's last words straight back while it is
# still talking. That costs those words, not the turn -- and a "wait" the user says
# within LOOKBACK_SECS of the bot saying "wait" is taken as its echo: they say it again,
# or more than it.
#
# A barge-in pause moves nothing here. Each word is released the caption lead ahead of its
# audio, on the transport's voice clock (playout_pause.py, THE VOICE CLOCK), and dated by
# when that audio played, less the lead: the release time itself when nothing stops the
# voice in between, and that much later when a pause does -- a word released just before
# a pause is heard only after it. A word not yet heard is in no echo.
#
import collections
import time
import unicodedata

from loguru import logger
from pipecat.frames.frames import Frame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.barge_pause import fold_shh, is_stop_utterance
from teaport_brain.conversation_log import quoted
from teaport_brain.settings import setting
from teaport_brain.tts_text import CAPTION_LEAD_SECS
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

    def played(self, text: str, heard_at=None) -> None:
        """`text` was released to play: now, or -- `heard_at`, a callable -- whenever
        that says (on this clock; None while not yet)."""
        now = self.clock()
        stamp = now if heard_at is None else heard_at
        for w in _match_words(normalize(text)[0]):
            self._words.append((stamp, w))
        while self._words:
            t = self._time(self._words[0][0])
            if t is None or now - t <= KEEP_SECS:
                break
            self._words.popleft()

    @staticmethod
    def _time(stamp):
        return stamp() if callable(stamp) else stamp

    def last(self):
        """(when, word): the newest word the speaker has played, or None."""
        for stamp, w in reversed(self._words):
            t = self._time(stamp)
            if t is not None:
                return t, w
        return None

    def since(self, since: float) -> list[tuple[float, str]]:
        """(time, word) from LOOKBACK_SECS before `since` on; none when the bot played
        nothing in the ACTIVE_SECS before it (it was not talking when this speech began).
        A word not yet heard is in none of them."""
        words = []
        for stamp, w in self._words:
            t = self._time(stamp)
            if t is None:
                break                           # nor any after it: they play in order
            words.append((t, w))
        before = [t for t, _ in words if t <= since]
        if not before or since - before[-1] > ACTIVE_SECS:
            return []
        start = since - LOOKBACK_SECS
        return [(t, w) for t, w in words if t >= start]


def _match_words(norm: str) -> list[str]:
    """Normalized words as both sides of the match see them: any "shh" spelling folded
    the way the stop words fold it (barge_pause.fold_shh)."""
    return [fold_shh(w) for w in norm.split()]


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
    if whole and is_stop_utterance(" ".join(matched)):
        # All stop words, all the bot's: an echo that would cut the reply on its own.
        return True
    if len(matched) == 1:
        # One word: the bot's only when it is the whole transcript and long.
        return whole and len(matched[0]) >= 5
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
        pushed now, its first two words would already be a barge-in -- its first stop
        word too (#216)."""
        since = self.bot.clock() if since is None else since
        heard = self.bot.since(since)
        norm, idx = normalize(text)
        words = norm.split()
        if not heard or not words:
            return text
        bot = [w for _, w in heard]
        starts = [j for j, (t, _) in enumerate(heard) if t <= since - MIN_LAG_SECS]
        # An interim is held on less: one word after a fragment may still become a run.
        # Matched on the folded words ("shhh" as said, "shh" as played); `words` keeps
        # the normalized ones, whose lengths place the cut in `text` below.
        k, matched = echo_run(_match_words(norm), bot, starts,
                              behind_fragment=2 if final else 1)
        if not final and k and k == len(words):
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

    def tap(self, voice_clock=None) -> "PlayedWordsTap":
        return PlayedWordsTap(self.bot, voice_clock=voice_clock)


class PlayedWordsTap(FrameProcessor):
    """Below the output transport: each TTSTextFrame passes as its word plays, the
    caption lead ahead of its audio. With the transport's voice clock, each is dated by
    when its audio played, less the lead (the module header: a pause between the two)."""

    def __init__(self, bot: BotWords, voice_clock=None, **kwargs):
        super().__init__(**kwargs)
        self._bot = bot
        self._voice = voice_clock

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and direction == FrameDirection.DOWNSTREAM:
            self.note(frame)
        await self.push_frame(frame, direction)

    def note(self, frame: TTSTextFrame) -> None:
        """`frame`'s word was released to play: into the bot's words."""
        self._bot.played(frame.text, heard_at=self._heard_at(frame))

    def _heard_at(self, frame):
        voice = self._voice
        if voice is None or not voice.attached or not frame.pts:
            return None
        lead = CAPTION_LEAD_SECS
        audio_ns = frame.pts + round(lead * 1e9)
        # The pipeline clock (ns) to the guard's (s): both monotonic, a fixed offset.
        offset = self._bot.clock() - self.get_clock().get_time() / 1e9

        def heard_at():
            at = voice.when(audio_ns)
            return None if at is None else at / 1e9 - lead + offset
        return heard_at


def log_strip(kind: str, text: str, kept: str) -> None:
    if kept:
        logger.info(f"echo guard: {kind} began with the bot's own words; kept {quoted(kept)} "
                    f"of {quoted(text)}")
    else:
        logger.info(f"echo guard: {kind} {quoted(text)} is the bot's own words; dropped")
