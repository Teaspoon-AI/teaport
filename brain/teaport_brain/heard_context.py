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

import os

from loguru import logger

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

HEARD_MODE = os.getenv("HEARD_MODE", "truncate")  # truncate | note


def _msg_text(m) -> str:
    """Text of a context message whose content is a string or a list of parts."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


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


class HeardContextCorrector(FrameProcessor):
    """Make the LLM's context reflect only what the user actually heard."""

    def __init__(self, ledger, context, mode=None):
        super().__init__()
        self._ledger = ledger
        self._context = context
        self._mode = mode or HEARD_MODE
        self._done = 0  # ledger events already reconciled
        # How long the context was at the previous reconcile. The cut turn's spoken
        # message can only be among the messages added since — see _truncate.
        self._mark = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self._reconcile()
        await self.push_frame(frame, direction)

    def _reconcile(self):
        start = self._mark
        for u in self._ledger.events[self._done:]:
            if not (u.interrupted and u.cut_short):
                continue
            if self._mode == "note":
                self._add_note(u)
            else:
                self._truncate(u, start)
        self._done = len(self._ledger.events)
        self._mark = len(self._context.get_messages())

    def _truncate(self, u, start=0):
        """Reconcile the cut turn's spoken message to the heard prefix.

        We CANNOT match by text: pipecat commits the TTS-spoken text (it sets the
        LLMTextFrame's append_to_context=False, so only the TTSTextFrame is
        appended), which differs from the ledger's raw-LLM `u.text` — and on an
        early barge-in the TTS text may be partial or never committed at all.
        So we locate the cut turn by POSITION (it is the bot's most recent turn)
        and overwrite its spoken message with the accurate heard prefix from the
        ledger — or insert one if pipecat committed none. Tool results are kept.

        Positional anchoring is sound only while every spoken assistant message
        in the context is one the LEDGER also charted. Filler lines (the consult
        narrator, the tool acks, the pipeline's own notices) are charted by
        nothing, so they must never be committed either — which is why tools.py,
        llm_error_speaker.py and agent_session.py push every such line with
        append_to_context=False. A filler committed as the tail assistant message
        would BE the anchor here, and the cut reply's heard prefix would delete
        or overwrite the filler line instead of the reply.
        """
        heard = _clean_prefix(u.heard_text)
        msgs = self._context.get_messages()
        # Skip the triggering user turn (trailing user/system messages); whatever
        # remains at the tail is the cut bot turn.
        #
        # The walk stops at `start` — the context length at the previous reconcile —
        # because anything older belongs to an exchange that is already settled. When
        # NOTHING was heard, pipecat commits no spoken text for the cut turn at all
        # (it appends the TTSTextFrame, and none was ever pushed), so without that
        # bound the walk crosses the intervening user turn and lands on the PREVIOUS
        # assistant message — a reply the user did hear — and deletes it.
        #
        # Live 2026-08-26 08:17: a background consult's answer was delivered and heard
        # in full, the user said "Thanks, Kettlebot.", the one-line reply to that was
        # barged over at 0% heard, and reconciling THAT cut removed the delivery. With
        # its own answer gone from the context the model recited the whole shop list
        # again, which the user saw as the reply repeating twice.
        end = len(msgs)
        while end > start and msgs[end - 1].get("role") in ("user", "system"):
            end -= 1
        anchor = msgs[end - 1] if end > start else None

        if anchor is not None and anchor.get("role") == "assistant" and _msg_text(anchor).strip():
            # A spoken message was committed (possibly partial TTS text).
            if heard:
                _set_msg_text(anchor, heard)  # in-place; dict ref is shared
                logger.info(f"HeardCorrector[truncate]: spoken reply -> heard …{heard[-40:]!r}")
            else:
                self._context.set_messages([m for m in msgs if m is not anchor])
                logger.info("HeardCorrector[truncate]: removed unheard reply")
        elif anchor is not None and anchor.get("role") in ("tool", "assistant"):
            # Tool result / bare tool-call with no committed spoken text: pipecat
            # dropped the reply, so insert the heard prefix where it belongs.
            if heard:
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
