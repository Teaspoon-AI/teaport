#
# turn_text.py — a turn's final words, where they differ from the words it was asked on.
#
# With TEAPORT_STT_TURN_TEXT=slice (stt.py, TURN TEXT) a turn goes downstream at its
# commit's preview as PROVISIONAL text -- the words the engine had sent, then its preview
# of the rest -- and the reply starts on it, as early as it does on today's done. The
# turn's FINAL text is known ~0.5 s later, once the engine's bound passes the commit. On
# the measured corpus the two agreed on 97 % of turns; where they do not, the STT pushes a
# TurnTextFinalFrame and TurnTextCorrector, below the user aggregator, applies it:
#
#   amended    The turn has not been committed yet (the caller resumed, the strategy is
#              still waiting): its words in the aggregator are replaced, and the turn is
#              asked on the final. Nothing was asked on the provisional words, so nothing
#              is lost. (A speculative reply asked on them misses at the commit, as it
#              should: the words changed.)
#   corrected  It was committed: the user message holding it -- the first committed
#              after the words were pushed (or, where the reply hold folded that one
#              into the turn before, the newest) -- is rewritten in place, whole words
#              only, and the reply -- asked, maybe playing, maybe done -- is KEPT. Every
#              later request reads the caller's real words. The live speculation's
#              snapshot, if one holds that message, is rewritten the same way, so the
#              next commit is not a ctx-changed miss over a correction the policy keeps
#              anyway. A final with no words never empties a message.
#   unplaced   The words are in neither: the provisional final never reached the
#              aggregator's words. Something took it on the way -- Wi-Fi setup or
#              OpenClaw's run control (taken_turn.taken), the barge-in judge letting the
#              reply carry on over it, the barge-in guard dropping it as too few words
#              to cut the bot -- each of which TakenWords records, and the line says
#              which; anything else is logged as having fit nowhere. The words are NOT
#              pushed again: they were decided once, under the state they came in, and a
#              second decision under another (the pause over, the run's answer in) once
#              turned a judged backchannel into a barge-in and asked the relay twice.
#              The one re-push there is is the STT's own, for a provisional that pushed
#              no words at all (stt.py, _settle_turn).
#
# No restart of a reply already asked. Measured (~/rbf/RESULTS2.md, spike 2b, 288 turns):
# the final beats the provisional by 3 word errors in ~3000 words (259 vs 256), and the
# provisional beats today's done (268), so the reply is already on words at least as good
# as today's; a restart would cost those turns a whole time-to-first-token, and once any of
# the reply has left the LLM it is a barge-in in all but name (the ledger's 0 % cut, its
# tool calls cancelled, owed.py's records re-rendered and its turn counted twice). Each
# correction logs what the final added and dropped and whether the reply had started
# playing -- "final adds" separately from changes, since a trailing "stop" or "no" the
# preview missed is the one case a restart or a follow-up could ever pay for -- so the
# live log says whether one is needed.
#
# The rewrite is in place: no message is added, removed or moved, so the positional
# windows of HeardContextCorrector and the reply hold's merge, CutReplyFence's commits
# and owed.py's bookkeeping (it reads the context at a render, and nothing here renders)
# see the same context they would have, with other words in one user message.
#
# The correction is one of endpointing.STT_FRAMES, so a processor that holds the STT's
# frames above the aggregator (run control's claim, the barge-in judge) holds it behind
# the final it corrects; and commits are recorded as the aggregator writes them
# (on_user_turn_message_added), not when their frame passes here, so one the aggregator
# makes while the correction is on its way (the ceiling, the safety net, the watchdog --
# its own task) is found.
#
import time
from collections import deque

from loguru import logger

from pipecat.frames.frames import BotStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor

from teaport_brain.endpointing import TurnTextFinalFrame
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG
from teaport_brain.heard_context import _set_msg_text, msg_text

__all__ = ["TurnTextCorrector", "TurnTextFinalFrame"]

# The commits a correction may look in: the latest few, each with when it passed and
# the user message it carried. The final follows its provisional by ~0.5 s.
_COMMITS_KEPT = 8


def _last_whole(text: str, part: str) -> int:
    """Where `part` last occurs in `text` as whole words (its ends at the text's ends or
    at whitespace), or -1: a turn's "no" is never the end of "I know"."""
    end = len(text)
    while (k := text.rfind(part, 0, end)) >= 0:
        after = k + len(part)
        if (k == 0 or text[k - 1].isspace()) and (after == len(text) or text[after].isspace()):
            return k
        end = k + len(part) - 1
    return -1


class TurnTextCorrector(FrameProcessor):
    """Apply a sliced turn's final where it differs from its provisional text (see the
    module header). Placed below the user aggregator, above SegmentDoneSink
    (agent_session), only in slice mode. `taken_words`: the session's
    taken_turn.TakenWords, which says where unplaced words went."""

    def __init__(self, aggregator, *, speculator=None, taken_words=None):
        super().__init__()
        self._agg = aggregator
        self.speculator = speculator
        self.taken_words = taken_words
        # The latest commits, (when the aggregator wrote it, the user message), oldest
        # first: where a committed turn's words are looked for. And the bot's latest
        # start, for the line's "had the reply started playing".
        self._commits: deque = deque(maxlen=_COMMITS_KEPT)
        self._spoke_at = 0.0
        aggregator.add_event_handler("on_user_turn_message_added", self._message_added)
        self.amended = 0
        self.corrected = 0
        self.unplaced = 0
        # Where the latest unplaced words went (TakenWords), or None: fit nowhere.
        self.last_unplaced = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TurnTextFinalFrame):
            await self._apply(frame)
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self._spoke_at = time.monotonic()
        await self.push_frame(frame, direction)

    async def _message_added(self, _aggregator, message):
        """The aggregator has written a user turn: the message the event names -- the
        newest user message reading its content and not yet on record. Matched by
        content, not taken as the newest when this runs: the handler is a task of its
        own, and two commits can land before the first one's runs."""
        content = getattr(message, "content", None)
        recorded = {id(m) for _, m in self._commits}
        users = [m for m in self._agg.context.get_messages()
                 if isinstance(m, dict) and m.get("role") == "user" and id(m) not in recorded]
        found = next((m for m in reversed(users) if msg_text(m) == content), None)
        if found is None and users:
            found = users[-1]          # rewritten since (the reply hold's merge): newest
        if found is not None:
            self.committed(found)

    def committed(self, message: dict):
        """A commit wrote the user message `message`, now."""
        self._commits.append((time.monotonic(), message))

    def _tally(self) -> str:
        return f"amended={self.amended} corrected={self.corrected} unplaced={self.unplaced}"

    async def _apply(self, frame: TurnTextFinalFrame):
        what = (f"[TURN] VAD stop {frame.stop_n}: final {frame.kind} "
                f"added={list(frame.added)} dropped={list(frame.dropped)}")
        if self._amend(frame):
            self.amended += 1
            logger.info(f"{what} -> amended: not yet asked, the turn will be asked on the "
                        f"final ({self._tally()})")
            return
        placed = self._correct(frame)
        if placed is not None:
            self.corrected += 1
            logger.info(f"{what} -> corrected in the context ({placed}); the reply is "
                        f"kept ({self._tally()})")
            return
        self.unplaced += 1
        why = self.taken_words.why(frame.asked) if self.taken_words is not None else None
        self.last_unplaced = why
        if why is not None:
            logger.info(f"{what} -> unplaced: the asked words were {why}; they stay so "
                        f"({self._tally()})")
        else:
            logger.warning(f"{what} -> unplaced: the asked words fit nowhere (not taken, "
                           f"judged or dropped that this session knows of) -- logged only "
                           f"({self._tally()})")

    def _amend(self, frame: TurnTextFinalFrame) -> bool:
        """The turn's words still in the aggregator (not committed): replace them -- or,
        where the final has none (a word only the preview heard), take them out. Reaches
        for `_aggregation` as speculate.Speculator.candidate does."""
        asked = frame.asked.strip()
        parts = self._agg._aggregation
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].text.strip() == asked:
                if frame.final.strip():
                    parts[i].text = frame.final
                else:
                    del parts[i]
                return True
        return False

    def _correct(self, frame: TurnTextFinalFrame):
        """The turn's words in a committed user message: rewrite them in place, in the
        live speculation's snapshot too. The message is the first one committed after
        the words were pushed that holds them as whole words, and still in the context.
        Returns how the reply stood, for the line, or None if no message holds them."""
        asked = frame.asked.strip()
        if not asked:
            return None
        msgs = self._agg.context.get_messages()
        where = {id(m): i for i, m in enumerate(msgs)}
        newest = next((m for m in reversed(msgs)
                       if isinstance(m, dict) and m.get("role") == "user"
                       and not msg_text(m).startswith(SYSTEM_NOTICE_TAG)), None)
        recorded = {id(m) for _, m in self._commits}
        after = [(at, m) for at, m in self._commits if at >= frame.pushed_at]
        looks = [(at, m) for at, m in after if id(m) in where]
        if (newest is not None and all(m is not newest for _, m in looks)
                and (id(newest) not in recorded or len(looks) < len(after))):
            # Looked in last, the newest caller message: a commit not on record yet (the
            # aggregator wrote it, and its event's handler runs as a task of its own), or,
            # where a commit since has left the context, the turn before it, into which
            # the reply hold folded it (HeardContextCorrector's merge). Never one on
            # record from before the words were pushed: a turn already answered.
            looks.append((float("inf"), newest))
        for n, (at, m) in enumerate(looks):
            i = where[id(m)]
            old = msg_text(m)
            k = _last_whole(old, asked)
            if k < 0 or old.startswith(SYSTEM_NOTICE_TAG):
                continue
            new = " ".join((old[:k] + " " + frame.final + " " + old[k + len(asked):]).split())
            if not new:
                # The final has no words, and the message had nothing else: kept as it
                # was asked rather than emptied (an empty user message is no turn).
                return "kept: its final has no words"
            _set_msg_text(m, new)
            if self.speculator is not None:
                self.speculator.amend(i, old, new)
            if m is not newest:
                return "a turn already answered"
            since = at if at != float("inf") else frame.pushed_at
            return ("its reply had started playing" if self._spoke_at > since
                    else "its reply had not started playing")
        return None
