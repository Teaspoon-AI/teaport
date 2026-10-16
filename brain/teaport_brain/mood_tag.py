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

# The face a leading emoji names. Each is one code point that needs no variation selector,
# so it cannot arrive split across deltas; a model is steadier with an emoji than with
# brackets, and one that fails to parse is silent where a bracketed tag would be spoken.
EMOJI = {"😊": "happy", "😠": "angry", "😢": "sad", "😨": "afraid", "🤢": "disgusted",
         "😔": "melancholic", "😮": "surprised", "😌": "calm", "😟": "concerned",
         "🤔": "curious", "😐": "neutral"}

PARAGRAPH = (
    "You have a small face that shows a mood. When your reply carries a feeling, or you "
    "are asked to show or act one out, begin the reply with exactly one emoji naming it, "
    "as its very first character. This is the one exception to the rule against emojis: "
    "the face reads it and the voice never does. "
    "The emojis: " + ", ".join(f"{e} {m}" for e, m in EMOJI.items())
    + ". Use 😟 when someone shares bad news or you sympathise. The emoji is never "
    "spoken and is not part of your words; write nothing else around it, and only one. "
    "A reply with no clear feeling gets no emoji."
)

# What may sit between an emoji tag and the words: spaces, and the invisible marks that
# follow an emoji (a variation selector, a skin tone). A joiner makes it another emoji, a
# sequence, which is not one of ours.
_SKIP = re.compile("^[\\s\ufe0f\U0001F3FB-\U0001F3FF]+")

_TAG = re.compile(r"\s*\[\s*mood\s*:\s*([A-Za-z ]{1,24}?)\s*\]\s*", re.IGNORECASE)
_OPENER = "[mood"
_MAX_TAG_CHARS = 40       # an opener still unclosed this far in is not a tag


def parse_leading_tag(text: str, final: bool = False):
    """text -> ("tag", name or None, rest) for a leading tag, ("plain",) when it plainly
    is not one, ("more",) when it could still become one. `final`: the reply is over, so
    nothing more is coming (an emoji alone, or an unfinished second tag, is decided now)."""
    head = text.lstrip()
    if not head:
        return ("more",)
    if head[0] in EMOJI:         # an emoji tag: the mood, and the words after it
        name, rest = EMOJI[head[0]], head[1:]
        while True:              # a second tag, of either kind, is dropped: the first wins
            rest = _SKIP.sub("", rest)
            if rest.startswith("\u200d"):
                return ("plain",)    # a sequence (😮‍💨): another emoji, not this one
            if rest and rest[0] in EMOJI:
                rest = rest[1:]
            elif (m := _TAG.match(rest)):
                rest = rest[m.end():]
            else:
                break
        # The words have not come yet, or a second tag is half in: what follows the emoji
        # (a modifier, a joiner, "[mood") is decided by the next delta, not this one.
        if not final and (not rest or (rest.startswith("[") and "]" not in rest
                                       and len(rest) <= _MAX_TAG_CHARS)):
            return ("more",)
        return ("tag", name, rest)
    # ("[ mood: angry ]" is read as a tag too: a model spaces its brackets now and then)
    opener = "[" + head[1:].lstrip() if head.startswith("[") else head
    cut = opener[:len(_OPENER)].lower()
    if not _OPENER.startswith(cut):
        return ("plain",)
    m = _TAG.match(text)
    if m:
        name = m.group(1).strip().lower()
        return ("tag", name if name in MOODS else None, text[m.end():])
    if len(head) <= _MAX_TAG_CHARS and (len(opener) < len(_OPENER) or "]" not in head):
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

    async def _settle(self, direction):
        """The reply stops here (its end, or a tool call): an emoji tag still waiting for
        words has none coming, and is a tag of its own."""
        if not (self._deciding and self._held):
            return
        verdict = parse_leading_tag("".join(t for _, t in self._held), final=True)
        if verdict[0] != "tag":
            return
        _, name, rest = verdict
        self._held, self._deciding = [], False
        if name:
            self._mood = (name, rest)
        if rest.strip():
            await self.push_frame(LLMTextFrame(text=rest), direction)
        else:
            self._bare = True

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
            await self._settle(direction)
            self._bare = False     # the call is the answer
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._settle(direction)
            await self._release(direction)
            if self._mood:       # a short reply: the words it had
                await self._send_mood(direction)
            if self._bare:
                self._bare = False
                logger.warning("mood: a reply of only a tag; speaking the recovery line")
                await self.push_frame(LLMTextFrame(text=RECOVERY_TEXT), direction)
        await self.push_frame(frame, direction)
