#
# Unit test: the consult narrator must stop before the answer is spoken.
#
# _consult_progress is a fixed countdown against dead air — "Still working on it." at 9s,
# "Almost there — hang tight." at 22s — spoken while a background ask_openclaw runs. It
# used to be cancelled in _consult_and_followup's finally, which was harmless only
# because the follow-up injector returned as soon as it had queued the LLM run.
#
# The injector now waits for the delivered turn to FINISH SPEAKING, so it can retire the
# one-shot trigger it puts in the context. That left the narrator running for the whole
# delivery. Live 2026-08-26 09:44: the consult started at :15.3, the shop list was spoken
# at :25.0, and "Almost there — hang tight." landed at :37.3 — twelve seconds after the
# user already had the answer.
#
# So the contract is an ORDER, not a cleanup: the narrator is cancelled before the
# outcome is handed to the injector, on every path out of the waiter.
#
# Run: python test_consult_progress.py
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import tools  # noqa: E402

ANSWER = "Upper Crust Bakery, Neon Belly, Russell's Bakery."


class _LLM:
    """Records what the narrator says, and when, relative to the delivery."""

    def __init__(self):
        self.narrated = []
        self.delivering = False
        self.narrated_during_delivery = []

    async def push_frame(self, frame):
        text = getattr(frame, "text", "")
        self.narrated.append(text)
        if self.delivering:
            self.narrated_during_delivery.append(text)


async def _fast_progress(llm):
    """The real narrator's shape, on a timescale a test can wait out."""
    try:
        await asyncio.sleep(0.05)
        await llm.push_frame(_Speak("Still working on it."))
        await asyncio.sleep(0.05)
        await llm.push_frame(_Speak("Almost there — hang tight."))
    except asyncio.CancelledError:
        pass


class _Speak:
    def __init__(self, text):
        self.text = text


async def _run(consult_result):
    llm = _LLM()
    delivered = []

    async def followup(request, text, tool_call_id=None):
        # Stands in for speak_followup, which now blocks until the reply has been
        # spoken in full — long enough for the narrator to fire twice if it is alive.
        llm.delivering = True
        delivered.append(text)
        await asyncio.sleep(0.2)
        llm.delivering = False

    fut = asyncio.get_running_loop().create_future()
    fut.set_result(consult_result)
    real_progress = tools._consult_progress
    tools._consult_progress = _fast_progress
    try:
        await tools._consult_and_followup(
            "call-1", fut, "pastry shops on Burnet", followup, "tc-1", llm=llm)
    finally:
        tools._consult_progress = real_progress
    return llm, delivered


async def test_the_narrator_is_silent_once_the_answer_is_delivered():
    llm, delivered = await _run({"text": ANSWER})
    assert delivered == [ANSWER], delivered
    assert not llm.narrated_during_delivery, (
        f"the narrator spoke {llm.narrated_during_delivery!r} while the answer was being "
        f"delivered — the user hears 'Almost there' after already having the answer")


async def test_a_failed_consult_also_stops_the_narrator():
    """The failure path delivers too, and must not narrate over its own apology."""
    llm, delivered = await _run({"error": "the desktop agent did not answer"})
    assert delivered == [None], delivered
    assert not llm.narrated_during_delivery, llm.narrated_during_delivery


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
