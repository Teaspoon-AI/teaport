#
# mood_tag.py -- let the model say how a reply feels, for the client's face.
#
# The OLED avatar takes {"mood": NAME} (teaport-oled-avatar, "Moods"). Left to itself it
# guesses from the reply text with VADER, which tells happy from angry and little else.
# The model knows. So a session whose client announced ?features=mood tells the model it
# MAY begin a reply with a tag, [mood: sad], when the reply carries a clear feeling --
# most replies carry none and leave it off (the avatar relaxes a mood by itself).
#
# MoodTag sits upstream of LLMTextGuard and does one thing: it takes that tag off the
# front of a response -- before the guard, the TTS, the captions and the committed
# context see a character of it -- and sends the client {"type": "mood", "mood": name}.
# The tag counts only as the very first thing of a completion: held until it closes,
# or until what came is plainly not one (then the held deltas go on untouched). A tag
# with a name the avatar does not know is stripped and sent nowhere.
#
import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    FunctionCallsStartedFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.face_mood import LEAD_WORDS, lead_words
from teaport_brain.llm_text_guard import RECOVERY_TEXT
from teaport_brain.services import FunctionCallsDispatchedFrame
from teaport_brain.settings import setting

# TEAPORT_FACE_MOOD: "tag" lets the model tag a reply's mood (this module); "text" and "off"
# do not, and the client is told which (gateway_server's hello): the local audio bridge
# scores the words itself (face_mood.py) for "tag" and "text", and not at all for "off".
MODE = setting("TEAPORT_FACE_MOOD")
ENABLED = MODE == "tag"

# What the model may name: the avatar's nine emotions, and its two plain faces. (The
# avatar knows aliases too; the model is shown, and held to, these.)
MOODS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised",
         "calm", "concerned", "curious", "neutral")

PARAGRAPH = (
    "You have a small face that shows a mood. When your reply carries a clear feeling, "
    "begin it with a tag naming that mood, like [mood: happy]: one of "
    + ", ".join(MOODS) + ". It is never spoken. Most replies have no clear feeling, "
    "and then you write no tag at all; use one only when it would be honest, and "
    "never more than one, always at the very start."
)

_TAG = re.compile(r"\s*\[mood\s*:\s*([A-Za-z ]{1,24}?)\s*\]\s*", re.IGNORECASE)
_OPENER = "[mood"
_MAX_TAG_CHARS = 40       # an opener still unclosed this far in is not a tag


def parse_leading_tag(text: str):
    """text -> ("tag", name or None, rest) for a leading tag, ("plain",) when it plainly
    is not one, ("more",) when it could still become one."""
    head = text.lstrip()
    if not head:
        return ("more",)
    cut = head[:len(_OPENER)].lower()
    if not _OPENER.startswith(cut):
        return ("plain",)
    m = _TAG.match(text)
    if m:
        name = m.group(1).strip().lower()
        return ("tag", name if name in MOODS else None, text[m.end():])
    if len(head) < len(_OPENER) or (len(head) <= _MAX_TAG_CHARS and "]" not in head):
        return ("more",)
    return ("plain",)


class MoodTag(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._reset()

    def _reset(self):
        self._deciding = True      # still looking at the first deltas of this completion
        self._held = []            # (frame, its text) held while deciding
        # A tag took the whole reply, and nothing since (a tool call, a word) has answered:
        # at the End that is dead air, so it is apologised for as the guard does.
        self._bare = False
        # A tag was read and its mood waits for the reply's first words: the face matches
        # a mood to the reply it hears by them. (mood, the text that followed the tag)
        self._mood = None

    async def _send_mood(self, direction):
        """The tagged mood, with the first words of the reply it is for."""
        name, text = self._mood
        self._mood = None
        words = lead_words(text)
        if words:
            logger.info(f"mood: {name}")
            await self.push_frame(OutputTransportMessageUrgentFrame(
                message={"type": "mood", "mood": name, "starts": words}), direction)

    async def _release(self, direction):
        held, self._held, self._deciding = self._held, [], False
        for frame, text in held:
            frame.text = text
            await self.push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
        elif isinstance(frame, InterruptionFrame):
            self._held, self._deciding, self._bare, self._mood = [], False, False, None
        elif isinstance(frame, LLMTextFrame) and self._deciding:
            if frame.text:
                self._held.append((frame, frame.text))
            verdict = parse_leading_tag("".join(t for _, t in self._held))
            if verdict[0] == "more":
                return
            if verdict[0] == "plain":
                await self._release(direction)
                return
            _, name, rest = verdict
            self._held, self._deciding = [], False
            if name:
                self._mood = (name, rest)
            else:
                logger.info("mood tag with an unknown name, stripped")
            if rest.strip():       # the rest rides the delta that closed the tag
                frame.text = rest
                await self.push_frame(frame, direction)
                if self._mood and len(lead_words(rest)) >= LEAD_WORDS:
                    await self._send_mood(direction)
            else:
                self._bare = True
            return
        elif isinstance(frame, LLMTextFrame) and frame.text.strip():
            self._bare = False
            if self._mood:
                self._mood = (self._mood[0], self._mood[1] + frame.text)
                if len(lead_words(self._mood[1])) >= LEAD_WORDS:
                    await self.push_frame(frame, direction)
                    await self._send_mood(direction)
                    return
        elif isinstance(frame, (FunctionCallsStartedFrame, FunctionCallsDispatchedFrame)):
            self._bare = False     # the call is the answer
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._release(direction)
            if self._mood:       # a short reply: the words it had
                await self._send_mood(direction)
            if self._bare:
                self._bare = False
                logger.warning("mood: a reply of only a tag; speaking the recovery line")
                await self.push_frame(LLMTextFrame(text=RECOVERY_TEXT), direction)
        await self.push_frame(frame, direction)
