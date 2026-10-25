#
# teaport — words something other than the model answers: a taken transcript, and the
# stop strategy that ends the turn it opened.
#
# Two parts of the brain take the user's final transcript away from the model: Wi-Fi
# setup, while it asks or runs (wifi_voice.py), and OpenClaw's spoken run control, for an
# utterance about a consult in flight that the relay acts on and answers itself
# (run_control.py, #230). A transcript taken is not dropped: it goes on with its text
# emptied and marked (taken()), so the turn it opened still closes -- TakenTurnCloser, a
# stop strategy in the user aggregator, ends that turn with nothing to answer. Dropped,
# the turn's stop strategy would wait for words that never come (and for the segment
# close the final carries, endpointing.py), and the turn would hang open for the
# aggregator's 5 s inactivity timeout with barge-in dead -- the wedge stt.py describes for
# empty finals.
#
import dataclasses

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.base_user_turn_stop_strategy import BaseUserTurnStopStrategy

_TAKEN = "teaport_taken"


def taken(frame: TranscriptionFrame) -> TranscriptionFrame:
    """The transcript, as it goes on once it was taken: a new frame of the same class
    (FinalTranscriptionFrame stays uninterruptible and keeps the VAD stop it answers, so
    the stop strategy still gets its segment close), with no words for the aggregator to
    commit, marked for TakenTurnCloser."""
    out = dataclasses.replace(frame, text="")
    setattr(out, _TAKEN, True)
    return out


def is_taken(frame: Frame) -> bool:
    return isinstance(frame, TranscriptionFrame) and getattr(frame, _TAKEN, False)


def handed_back(frame: TranscriptionFrame, text: str) -> TranscriptionFrame:
    """A transcript held and taken, given back to the model ahead of `frame`: same class
    and speaker, its own words, and no VAD stop stamp -- it closes no segment (its own was
    closed when it was taken), so it cannot end the turn it joins."""
    fields = {f.name for f in dataclasses.fields(frame)}
    return dataclasses.replace(frame, text=text, **({"stop_n": None} if "stop_n" in fields else {}))


class TakenTurnCloser(BaseUserTurnStopStrategy):
    """A user-turn stop strategy (agent_session.py lists it first): a taken transcript
    ends the turn it opened, with nothing to answer. The words of the turn's earlier
    segments are dropped with it ("hey can you... set up the Wi-Fi": the "hey can you" is
    not a question for the model). The strategies after it still see the frame, so the
    brain's own pays the segment close the frame carries."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = None  # the LLM user aggregator, set once it exists

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        await super().process_frame(frame)
        if is_taken(frame):
            if self.aggregator is not None:
                await self.aggregator.reset()  # its text so far: empty, no inference
            await self.trigger_user_turn_stopped()
        return ProcessFrameResult.CONTINUE
