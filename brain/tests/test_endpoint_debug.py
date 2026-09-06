#
# Unit test: the endpoint-debug chips never land while the bot is speaking.
#
# The chips ride the wire as plain assistant transcript messages (role=assistant,
# final=true) — the only transcript shape the OpenClaw relay accepts — and the
# official Talk UI keeps ONE open assistant entry: an assistant final arriving
# while the reply's captions are streaming is APPENDED into the open reply bubble
# and closes it early (ui realtime-talk-conversation.ts,
# mergeAssistantTranscriptText). Observed live: "🎙️ VAD: speech stopped" from a
# backchannel spoken over the bot ("mm-hm" that never barges) rendered INSIDE the
# agent's reply bubble. Since teaport runs against the official OpenClaw build
# (not a fork), the fix is on our side of the wire: a chip due during bot playout
# is withheld — dropped, not delayed, because a flush on BotStopped races the
# reply's caption final to the transport and can still merge. The journal log
# lines are never withheld.
#
# Run: python test_endpoint_debug.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Refuse to run against the wrong pipecat (a bare `import pinned_pipecat` checked
# nothing: require_pinned() has to be CALLED). It imports the package first, for
# HF_HUB_OFFLINE.
from pinned_pipecat import require_pinned  # noqa: E402
require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.endpoint_debug import EndpointDebug  # noqa: E402


def _tap(stage):
    """An EndpointDebug tap with push_frame stubbed; returns (tap, chips, pushed).
    `chips` collects the text of every transcript bubble the tap emitted."""
    tap = EndpointDebug({}, stage)
    chips = []
    pushed = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)
        if isinstance(frame, OutputTransportMessageUrgentFrame):
            chips.append(frame.message.get("text", ""))
    tap.push_frame = fake_push
    return tap, chips, pushed


async def _feed(tap, *frames, direction=FrameDirection.DOWNSTREAM):
    for f in frames:
        await tap.process_frame(f, direction)


async def test_chips_emit_between_turns():
    """The intended rendering: nobody else is talking, each chip is its own bubble."""
    tap, chips, _ = _tap("in")
    await _feed(tap, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
                UserStoppedSpeakingFrame())
    assert len(chips) == 2, chips
    assert chips[0] == "🎙️ VAD: speech stopped", chips
    assert chips[1].startswith("⏱️ turn committed"), chips


async def test_chips_withheld_while_bot_speaks():
    """A backchannel over the bot's reply: its VAD-stop/commit chips would be
    appended into the open reply bubble by the Talk UI, so they must not go out."""
    tap, chips, _ = _tap("in")
    # Bot frames reach the input-side tap traveling UPSTREAM from transport.output.
    await tap.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await _feed(tap, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
                UserStoppedSpeakingFrame())
    assert chips == [], (
        f"chips {chips!r} emitted during bot playout — the Talk UI folds these "
        f"into the agent's open reply bubble")


async def test_chips_resume_after_bot_stops():
    tap, chips, _ = _tap("in")
    await tap.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await _feed(tap, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame())
    await tap.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
    await _feed(tap, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame())
    assert chips == ["🎙️ VAD: speech stopped"], chips


async def test_first_audio_chip_withheld_when_chained_onto_playout():
    """The out tap: first audio of a turn chained onto a still-playing reply
    arrives mid-playout — same folding hazard, same withholding. (The normal
    case, synthesis before playout, still chips.)"""
    tap, chips, _ = _tap("out")
    tap._m["commit"] = 1.0
    await tap.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await _feed(tap, TTSAudioRawFrame(b"\x00\x00" * 240, 24000, 1))
    assert chips == [], chips
    # A fresh turn in silence still gets its chip.
    tap2, chips2, _ = _tap("out")
    tap2._m["commit"] = 1.0
    await _feed(tap2, TTSAudioRawFrame(b"\x00\x00" * 240, 24000, 1))
    assert len(chips2) == 1 and chips2[0].startswith("🔊 first audio"), chips2


async def test_frames_are_forwarded():
    """The tap is an observer: everything it sees must still go downstream."""
    tap, _, pushed = _tap("in")
    frames = [VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
              UserStoppedSpeakingFrame()]
    await _feed(tap, *frames)
    forwarded = [f for f in pushed
                 if not isinstance(f, OutputTransportMessageUrgentFrame)]
    assert forwarded == frames, forwarded


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
