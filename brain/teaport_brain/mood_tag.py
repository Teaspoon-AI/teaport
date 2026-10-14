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
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_MOODS")

# What the model may name: the avatar's eight emotions, and its two plain faces. (The
# avatar knows aliases too; the model is shown, and held to, these.)
MOODS = ("happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised",
         "calm", "curious", "neutral")

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
            self._held, self._deciding = [], False
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
                logger.info(f"mood: {name}")
                await self.push_frame(OutputTransportMessageUrgentFrame(
                    message={"type": "mood", "mood": name}), direction)
            else:
                logger.info("mood tag with an unknown name, stripped")
            if rest:       # the rest rides the delta that closed the tag
                frame.text = rest
                await self.push_frame(frame, direction)
            return
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._release(direction)
        await self.push_frame(frame, direction)
