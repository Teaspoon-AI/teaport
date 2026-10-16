#
# heard_context.py — reconcile the LLM's context with what the user ACTUALLY heard.
#
# pipecat commits the bot's FULL generated reply to the LLM context even when the
# TTS was cut off by a barge-in (TTSText is synthesized ahead of playout), so the
# model believes it conveyed things the user never heard — and then robotically
# acts on them. Two strategies, selected by HEARD_MODE:
#
#   "truncate" (default) — rewrite the cut assistant turn DOWN to the heard prefix
#       and drop the unheard tail. This makes the context look like an ordinary
#       transcript (everything in it really was said), which is exactly what
#       conversational LLMs are trained on — so "what did you just say?" is
#       answered by simply reading the context. Beat the note approach on every
#       model/harness we tried; ADDING grounding (notes) the model tends to ignore,
#       REMOVING the ungrounded tail it cannot.
#   "note" — leave the full reply and ADD a system note stating heard vs. unheard.
#       Purely additive (cannot corrupt the conversation). Kept as a fallback.
#
# Either way the TOOL RESULT stays in context, so "what did I miss / go on" can
# recover the undelivered content from authoritative data rather than memory.
#


from loguru import logger

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG
from teaport_brain.settings import setting
from teaport_brain.tts_text import has_speech

HEARD_MODE = setting("HEARD_MODE")  # truncate | note


def _msg_text(m) -> str:
    """Text of a context message whose content is a string or a list of parts."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _cancelled_rounds(msgs) -> set:
    """ids of the tool rounds among `msgs` that were cancelled whole: an assistant
    message whose every tool call has a tool result reading CANCELLED (pipecat's
    cancel_on_interruption), and those results."""
    msgs = [m for m in msgs if isinstance(m, dict)]
    results = {m.get("tool_call_id"): m for m in msgs
               if m.get("role") == "tool" and m.get("content") == "CANCELLED"}
    out = set()
    for m in msgs:
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        ids = [c.get("id") for c in calls or []]
        if ids and not _msg_text(m).strip() and all(i in results for i in ids):
            out.add(id(m))
            out.update(id(results[i]) for i in ids)
    return out


def _set_msg_text(m, text):
    if isinstance(m.get("content"), list):
        m["content"] = [{"type": "text", "text": text}]
    else:
        m["content"] = text


def _clean_prefix(heard: str) -> str:
    """Round the heard prefix back to the last COMPLETE clause / list item.

    A playout cut can land mid-item ("…aged parmesan, cold-pressed" out of
    "cold-pressed olive oil"). Answering "cold-pressed" is worse than answering
    the last item the user fully heard, so if the prefix ends mid-item (its last
    word isn't punctuated) we drop back to the last word that ended a clause or
    list item. Prose with no delimiters keeps the whole word-prefix.
    """
    words = (heard or "").strip().split()
    if not words:
        return ""
    if words[-1][-1] not in ",.;:":
        for i in range(len(words) - 1, -1, -1):
            if words[i][-1] in ",.;:":
                words = words[: i + 1]
                break
    return " ".join(words).rstrip(" ,;:.").strip()


def _is_reply(m) -> bool:
    """Whether a message can be a reply the ledger charted: an assistant message with
    something synthesizable in its text (tts_text.has_speech, the ledger's own test).
    An emoji reply -- a tool call riding on it or not -- is charted nothing, so the
    corrector neither counts it as a reply nor takes it for one."""
    return m.get("role") == "assistant" and has_speech(_msg_text(m))


def drop_from_context(context, doomed) -> None:
    """Remove `doomed` from `context`, matched by identity (the dicts are live, and two
    messages can read the same). Anything that removes messages the corrector may have
    seen goes through HeardContextCorrector.drop_messages instead, which calls this."""
    ids = {id(m) for m in doomed}
    context.set_messages([m for m in context.get_messages() if id(m) not in ids])


class HeardContextCorrector(FrameProcessor):
    """Make the LLM's context reflect only what the user actually heard."""

    # Class-level defaults as well: the hermetic tests build one with __new__.
    _merge = None
    _last_user = None

    def __init__(self, ledger, context, mode=None, merge=None):
        super().__init__()
        self._ledger = ledger
        self._context = context
        self._mode = mode or HEARD_MODE
        self._done = 0  # ledger events already reconciled
        # How long the context was at the previous reconcile. The cut turn's spoken
        # message can only be among the messages added since — see _truncate.
        self._mark = 0
        # reply_hold.TurnMerge: set when the reply to the previous user turn was dropped
        # before any of it played, so the turn committed now finishes that one. The
        # user message at the tail of the previous reconcile is the one it finishes.
        self._merge = merge
        self._last_user = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self._reconcile()
        await self.push_frame(frame, direction)

    def reconcile_for_snapshot(self):
        """The speculative reply's before_snapshot hook (speculate.py): the cut replies
        reconciled, as at a commit -- but this is not a commit, so a pending turn merge
        is left for the commit it belongs to (taking it here lost it). Called once
        CutReplyFence has let the cut reply into the context."""
        self._reconcile(commit=False)

    def _reconcile(self, commit: bool = True):
        start = self._mark
        batch = [u for u in self._ledger.events[self._done:] if u.speaker == "assistant"]
        cuts = [u for u in batch if u.interrupted and u.cut_short]
        if self._mode == "note":
            for u in cuts:
                self._add_note(u)
        else:
            # Every cut's message is found before any is rewritten: removing one moves
            # what "the newest" is for the next.
            for u, anchor in [(u, self._resolve(u, batch, start)) for u in cuts]:
                self._truncate(u, anchor)
        self._done = len(self._ledger.events)
        msgs = self._context.get_messages()
        if commit:
            # After the truncation: a reply cut at 0% that pipecat did commit sits
            # between the two user messages until it is removed above.
            if self._merge is not None and self._merge.take():
                self._merge_tail()
                msgs = self._context.get_messages()
            self._last_user = msgs[-1] if msgs and msgs[-1].get("role") == "user" else None
        self._mark = len(msgs)

    def _merge_tail(self):
        """Fold the user message just committed into the one before it.

        Asked for by reply_hold.ReplyHoldGate when the reply to the previous user turn
        was dropped before a word of it played: the caller was not answered, they kept
        talking, and what they said is the rest of that turn. Left as two messages the
        model reads a fragment and a separate continuation -- and has been seen to answer
        the fragment. Only when both are the caller's own words, and nothing but system
        messages sits between them (MemoryRecall adds its note just before the turn it
        recalled for): a tool call in between, or an injected order (SYSTEM_NOTICE_TAG),
        leaves them be. The system notes move ahead of the merged turn, so the context
        still ends on the caller's words.

        A tool round the dropped reply had started, and the interruption that dropped it
        cancelled (pipecat writes CANCELLED over its IN_PROGRESS), did nothing: it goes
        with the merge, and the model decides again on the whole turn. One that finished
        (a result, or the async consult's placeholder with the consult running on) is
        the turn acted on, and keeps the two apart (formal/UserTurn.tla, `Appended`)."""
        msgs = self._context.get_messages()
        prev, last = self._last_user, (msgs[-1] if msgs else None)
        idx = next((i for i, m in enumerate(msgs) if m is prev), None)
        between = msgs[idx + 1:-1] if idx is not None else []
        cancelled = _cancelled_rounds(between)
        if (idx is not None and last is not None and last is not prev
                and prev.get("role") == "user" and last.get("role") == "user"
                and all(m.get("role") == "system" or id(m) in cancelled for m in between)):
            first, rest = _msg_text(prev).strip(), _msg_text(last).strip()
            if (first and rest and not first.startswith(SYSTEM_NOTICE_TAG)
                    and not rest.startswith(SYSTEM_NOTICE_TAG)):
                _set_msg_text(prev, f"{first} {rest}")
                self.drop_messages([m for m in between if id(m) in cancelled] + [last])
                if cancelled:
                    logger.info("HeardCorrector[merge]: dropped the unplayed reply's "
                                "cancelled tool call(s) with it")
                notes = [m for m in self._context.get_messages()[idx + 1:]]
                if notes:
                    # The notes go AHEAD of the merged turn, so the context still ends on
                    # the caller's words (ClientNotes, for one, defers while it does not).
                    msgs = self._context.get_messages()
                    self._context.set_messages(msgs[:idx] + notes + [prev])
                logger.info(f"HeardCorrector[merge]: unanswered turn + its continuation "
                            f"-> one user turn …{_msg_text(prev)[-60:]!r}")
                return
        logger.info("HeardCorrector[merge]: the unanswered turn is not the caller's message "
                    "before this one; left as two turns")

    def drop_messages(self, doomed):
        """Remove `doomed` (matched by identity) from the context, keeping _mark true.

        _truncate walks back no further than _mark, the context length at the previous
        reconcile. A message removed from below that mark shifts everything after it
        left by one, so the first message added since, often the very reply that was
        cut, would fall outside the window and go uncorrected. Anything else that
        removes messages between reconciles must come through here (ClientNotes does,
        for its retention cap)."""
        ids = {id(m) for m in doomed}
        below = sum(1 for m in self._context.get_messages()[:self._mark] if id(m) in ids)
        drop_from_context(self._context, doomed)
        self._mark -= below

    def _resolve(self, u, batch, start):
        """The cut reply's own message in the context, or None: what _truncate rewrites.

        We CANNOT match by text: pipecat commits the TTS-spoken text (it sets the
        LLMTextFrame's append_to_context=False, so only the TTSTextFrame is
        appended), which differs from the ledger's raw-LLM `u.text` — and on an
        early barge-in the TTS text may be partial or never committed at all.
        So we locate it by POSITION: pipecat commits the replies' messages in the
        order their TTS contexts opened (Utterance.turn_seq), each one that
        `committed`, so the cut reply's message is as many spoken messages back from
        the newest as its batch has committed replies newer than it. Position also
        means order: the cut reply must be committed first, which
        endpointing.CutReplyFence ensures.

        The newest message alone, the first rule, deleted or rewrote another reply's
        (brain/formal/LedgerPlayout.tla, CutAnchorsOwn, NoPlayedReplyDeleted): a reply
        the engine failed on every clause is charted heard 0 but committed all the same
        (the TTS force-completes its text), so a reply played after it took its cut
        (lp_failed_position); a barge-in before a queued reply's first audio commits
        nothing of it, so the reply that played before took the cut (lp_position); and
        a reply cut partway with one committed after it (synthesized whole, queued
        behind it) had its heard prefix written over that one's text and kept its own
        full text (lp_guarded). A reply that committed nothing has nothing to correct.

        Positional anchoring is sound only while every spoken assistant message in the
        context is one the LEDGER also charted. Filler lines (the consult narrator, the
        tool acks, the pipeline's own notices) are charted by nothing, so they must
        never be committed either — which is why tools.py, llm_error_speaker.py and
        agent_session.py push every such line with append_to_context=False. Messages
        with nothing synthesizable (an emoji reply, charted nothing) are not counted.
        """
        if not u.committed:
            return None
        seqs = [e.turn_seq for e in batch]
        order = seqs if None not in seqs else list(range(len(batch)))
        mine = next(order[i] for i, e in enumerate(batch) if e is u)
        newer = sum(1 for i, e in enumerate(batch)
                    if e is not u and e.committed and order[i] > mine)
        msgs = self._context.get_messages()
        # Skip the triggering user turn (trailing user/system messages); whatever
        # remains at the tail is the newest bot turn.
        #
        # The walk stops at `start` — the context length at the previous reconcile —
        # because anything older belongs to an exchange that is already settled.
        # Without that bound, a cut that left no message lands on the PREVIOUS
        # exchange's assistant message — a reply the user did hear — and deletes it.
        # Live 2026-08-26 08:17: a background consult's answer was delivered and heard
        # in full, the user said "Thanks, Kettlebot.", the one-line reply to that was
        # barged over at 0% heard, and reconciling THAT cut removed the delivery. With
        # its own answer gone from the context the model recited the whole shop list
        # again, which the user saw as the reply repeating twice.
        #
        # Messages with nothing synthesizable are skipped too: an emoji reply is charted
        # nothing but committed (the TTS force-completes its text when its context ends,
        # at its End), so it can sit after the cut reply's message -- and the cut's heard
        # prefix overwrote it while the cut reply kept its full text (LedgerPlayout.tla,
        # HeardPrefixOnOwn, lp_unspeakable_tail).
        end = len(msgs)
        # (An assistant message with no text at all -- a bare tool call -- is not
        # skipped: the insert path below needs it.)
        def skipped(m):
            return (m.get("role") in ("user", "system")
                    or (m.get("role") == "assistant" and _msg_text(m).strip()
                        and not _is_reply(m)))
        while end > start and skipped(msgs[end - 1]):
            end -= 1
        if newer == 0:
            # The newest: a tool result there means pipecat dropped the reply's text
            # (_truncate inserts the heard prefix after it).
            return msgs[end - 1] if end > start else None
        spoken = [m for m in msgs[start:end] if _is_reply(m)]
        if len(spoken) <= newer:
            logger.warning(f"HeardCorrector[truncate]: {newer} newer replies but only "
                           f"{len(spoken)} spoken messages; cut turn left unchanged")
            return None
        return spoken[-1 - newer]

    def _truncate(self, u, anchor):
        """Reconcile the cut turn's message (`anchor`, from _resolve) to the heard
        prefix: overwrite it, remove it if nothing was heard, or insert the prefix
        after a tool result if pipecat committed none. Tool results are kept."""
        heard = _clean_prefix(u.heard_text)
        msgs = self._context.get_messages()
        if anchor is not None and anchor.get("role") == "assistant" and _msg_text(anchor).strip():
            # A spoken message was committed (possibly partial TTS text).
            if heard:
                _set_msg_text(anchor, heard)  # in-place; dict ref is shared
                logger.info(f"HeardCorrector[truncate]: spoken reply -> heard …{heard[-40:]!r}")
            elif anchor.get("tool_calls"):
                # Not the newest any more, it can be the call a tool result answers:
                # removing it would orphan the result.
                _set_msg_text(anchor, "")
                logger.info("HeardCorrector[truncate]: unheard reply's text cleared")
            else:
                self._context.set_messages([m for m in msgs if m is not anchor])
                logger.info("HeardCorrector[truncate]: removed unheard reply")
        elif anchor is not None and anchor.get("role") in ("tool", "assistant"):
            # Tool result / bare tool-call with no committed spoken text: pipecat
            # dropped the reply, so insert the heard prefix where it belongs.
            if heard:
                end = next(i for i, m in enumerate(msgs) if m is anchor) + 1
                self._context.set_messages(
                    msgs[:end] + [{"role": "assistant", "content": heard}] + msgs[end:])
                logger.info(f"HeardCorrector[truncate]: inserted heard reply …{heard[-40:]!r}")
        elif anchor is None:
            # Nothing was committed for the cut turn, so there is nothing to correct.
            # Not a warning: this is the ordinary shape of a reply barged over before
            # any of it was spoken.
            logger.info("HeardCorrector[truncate]: cut turn left no committed message")
        else:
            logger.warning("HeardCorrector[truncate]: no anchor for the cut turn "
                           f"(role={anchor.get('role')}); left unchanged")

    def _add_note(self, u):
        heard = (u.heard_text or "").strip()
        unheard = u.unheard_tail()
        if heard:
            note = (f'(The user interrupted your previous reply. They heard only: '
                    f'"{heard}". They did not hear the rest: "{unheard}".)')
        else:
            note = ("(The user interrupted your previous reply before any of it was "
                    "spoken aloud; they heard none of it.)")
        self._context.add_message({"role": "system", "content": note})
        logger.info(f"HeardCorrector[note]: noted cut (heard {len(heard)} chars)")
