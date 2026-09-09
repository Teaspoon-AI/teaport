#
# Unit test: a segment whose VAD stop never arrives is committed anyway.
#
# TeaportSTTService has exactly one path to a final — a commit sent when
# VADUserStoppedSpeakingFrame arrives — so a missed VAD stop loses the turn outright,
# and silently. The turn starts from an interim (MinWordsUserTurnStartStrategy), owns
# no text, and TurnAnalyzerUserTurnStopStrategy cannot fire without text, so it hangs
# until the aggregator's 5s user_turn_stop_timeout force-stops it with an empty
# aggregation: SILENT TURN reached=['nothing'], the model never asked.
#
# Live 2026-09-09 17:17:43 on the AWS box, mid-Hamlet. The caller said "Stop there";
# the interim started the turn and cut the bot correctly at heard~39%; no final ever
# arrived; the turn force-stopped at 5.006s ("User stopped speaking (strategy: None)").
# The barge-in worked perfectly and the answer was still lost. The engine's own buffer
# auto-commit would have closed the segment around 15s, long past the 5s window.
#
# This is NOT the 2026-08-25 shape (test_final_survives_turn_start.py), where the final
# was produced and then flushed by the interruption. That fix holds. Here the final was
# never produced at all — a different route to the same silence.
#
# The backstop is armed off INTERIM ACTIVITY and nothing else. Arming it off a turn-stop
# frame is the coupling stt.process_frame's comment rejects as a deadlock: that frame
# only fires once the turn is judged complete, which itself waits on this final.
#
# Run: python test_stranded_segment_commit.py   (or via the suite)
#

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain.stt import TeaportSTTService  # noqa: E402

# Short enough to keep the suite quick; the production value is 1.5s.
QUIET = 0.05


class Recorder(TeaportSTTService):
    """Real _handle_message and real backstop; commits and frames captured.

    create_task/cancel_task are pipecat FrameProcessor helpers that need a task
    manager from setup(); outside a pipeline they are plain asyncio tasks.
    """

    def __init__(self):
        super().__init__(url="ws://127.0.0.1:1/none")
        self.pushed = []
        self.commits = 0

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)

    async def stop_processing_metrics(self):
        pass

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)

    def cancel_task(self, task, timeout=None):
        task.cancel()

    async def _send_commit(self, final: bool = True):
        self.commits += 1


async def _delta(stt, text):
    await stt._handle_message({"type": "transcription.delta", "delta": text})


async def test_a_stranded_segment_is_committed_by_the_backstop():
    stt = Recorder()
    await _delta(stt, "Stop there")
    assert stt.commits == 0, "must not commit while the segment is still fresh"
    await asyncio.sleep(QUIET * 3)
    assert stt.commits == 1, (
        "a segment that streamed interims and then went quiet with no VAD stop was "
        "never committed — this is the turn the model is never asked about")


async def test_a_still_talking_user_never_trips_it():
    stt = Recorder()
    # Interims arriving faster than the quiet window, as while someone is speaking.
    for _ in range(6):
        await _delta(stt, "and ")
        await asyncio.sleep(QUIET / 2)
    assert stt.commits == 0, (
        "the backstop fired mid-utterance; every delta must re-arm it, or it becomes "
        "a source of the mid-phrase cutting it exists alongside")
    # ...and the other half of the same claim: once they DO stop, it still fires. Without
    # this the test passes on a backstop that is simply broken, and it also leaves a timer
    # running into whatever test comes next.
    await asyncio.sleep(QUIET * 3)
    assert stt.commits == 1, "re-arming must delay the backstop, not disable it"


async def test_a_final_disarms_it():
    stt = Recorder()
    await _delta(stt, "Stop there")
    await stt._handle_message({"type": "transcription.done", "text": "Stop there"})
    await asyncio.sleep(QUIET * 3)
    assert stt.commits == 0, "the segment already closed; committing again is a double commit"


async def test_the_vad_stop_disarms_it():
    # The normal path must win the race and leave nothing armed behind it, or a healthy
    # turn pays a second commit ~1.5s later against a segment that is already closed.
    stt = Recorder()
    await _delta(stt, "Stop there")
    stt._cancel_stranded_commit()          # what process_frame does on the VAD stop
    await stt._send_commit(final=True)     # ...before committing itself
    await asyncio.sleep(QUIET * 3)
    assert stt.commits == 1, f"expected exactly the VAD commit, got {stt.commits}"


async def test_a_disconnect_disarms_it():
    stt = Recorder()
    await _delta(stt, "Stop there")
    await stt._disconnect_websocket()
    await asyncio.sleep(QUIET * 3)
    assert stt.commits == 0, "the socket is gone; there is nothing left to commit to"


def main():
    stt_mod._STRANDED_INTERIM_SECS = QUIET

    async def run_all():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                await fn()
                print(f"  ok {name}")

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
    print("ALL PASS")
