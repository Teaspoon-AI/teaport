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
import time

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


async def _fast_progress(llm, request=None, gate=None):
    """The real narrator's shape, on a timescale a test can wait out. Accepts the
    request/gate kwargs _consult_and_followup now passes, and ignores them — the
    ordering-vs-delivery contract these tests check is independent of them."""
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


# --- the real _consult_progress: names the topic, and fits into a gap ---

class _Gate:
    """Stub FollowupGate. `gap` is what wait_until_idle reports: True == a clear
    moment opened (speak), False == none within the window (skip)."""

    def __init__(self, gap):
        self.gap = gap
        self.waits = 0

    async def wait_until_idle(self, max_wait=None):
        self.waits += 1
        return self.gap


class _Recorder:
    def __init__(self):
        self.said = []

    async def push_frame(self, frame):
        self.said.append(getattr(frame, "text", ""))


async def test_the_progress_line_names_the_user_request():
    # The request's own opening phrase, so a late line still has a referent.
    line = tools._progress_line("Find good pastry shops on Burnet Road in Austin", 0)
    assert "pastry shops on Burnet Road" in line, line
    assert line.startswith("Still working on that"), line
    # An empty request falls back to the generic line rather than a dangling "that —".
    assert tools._progress_line("", 0) == "Still working on it."
    assert "—" not in tools._progress_line("   ", 0)


async def test_the_topic_echo_cuts_at_a_boundary_and_skips_non_prose():
    """A fixed nine-word slice landed on a determiner ("...pastry shop in.") and had
    no character cap, so a URL or a JSON argument -- one token, however long -- was
    read aloud in full. The cut is at a clause boundary or the caps, never on a
    function word, and anything that is not prose yields the generic line."""
    T = tools._topic_phrase
    assert T("find me a really good pastry shop in the old town") == "find me a really good pastry shop"
    assert T("Find good pastry shops on Burnet Road, and check their hours") == \
        "Find good pastry shops on Burnet Road"
    assert T('{"task":"summarize","url":"https://news.example.com/x","depth":3}') == ""
    assert T("summarize https://news.example.com/2026/09/02/a-really-long-slug-here") == ""
    long = "compare " + " ".join(["antidisestablishmentarianism"] * 6)
    assert 0 < len(T(long)) <= tools._PROGRESS_TOPIC_CHARS, T(long)
    assert tools._progress_line('{"task": 1}', 0) == "Still working on it."


class _Timed(_Recorder):
    def __init__(self):
        super().__init__()
        self.at = []

    async def push_frame(self, frame):
        await super().push_frame(frame)
        self.at.append(time.monotonic())


async def test_the_schedule_is_wall_clock_so_a_gap_wait_does_not_push_the_next_line():
    """_PROGRESS_SCHEDULE names the instants the lines are due, from the consult's
    start. Sleeping the schedule's entries one after another, each AFTER the previous
    line's gap wait, stacked every wait onto the next line: two 6s waits made a
    22s schedule a 34s one. Here the gate takes 0.3s to find a gap and the second
    line is due at 0.4s: it must fire at ~0.7s (its moment, then its own wait), not
    at 1.0s (the first line's wait, then 0.4s more, then its own)."""
    class SlowGate:
        async def wait_until_idle(self, max_wait=None):
            await asyncio.sleep(0.3)
            return True
    saved = (tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT)
    tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT = (0.0, 0.4), 1.0
    llm = _Timed()
    llm._teaport_progress_active = False
    t0 = time.monotonic()
    try:
        await tools._consult_progress(llm, request="anything", gate=SlowGate())
    finally:
        tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT = saved
    assert len(llm.said) == 2, llm.said
    first, second = (t - t0 for t in llm.at)
    assert 0.25 < first < 0.5, first
    assert second < 0.85, f"the second line fired at {second:.2f}s: the gap wait stacked"


async def _drive_progress(gap):
    saved = tools._PROGRESS_SCHEDULE
    tools._PROGRESS_SCHEDULE = (0.0, 0.0)  # no countdown; exercise the gating only
    gate = _Gate(gap)
    llm = _Recorder()
    llm._teaport_progress_active = False
    try:
        await tools._consult_progress(llm, request="look into the H-matrix question", gate=gate)
    finally:
        tools._PROGRESS_SCHEDULE = saved
    return llm, gate


async def test_the_narrator_speaks_when_a_gap_opens():
    llm, gate = await _drive_progress(gap=True)
    assert len(llm.said) == 2, llm.said
    assert "H-matrix question" in llm.said[0], llm.said[0]
    assert gate.waits == 2, "it should check for a gap before each line"


async def test_the_narrator_stays_quiet_when_there_is_no_gap():
    llm, gate = await _drive_progress(gap=False)
    assert llm.said == [], (
        f"the narrator talked over the user instead of skipping: {llm.said!r}")
    assert gate.waits == 2, "it still checks (and skips) each line"


async def test_no_gate_falls_back_to_speaking_unconditionally():
    # The SIP path and any caller without a gate keep the original behaviour.
    saved = tools._PROGRESS_SCHEDULE
    tools._PROGRESS_SCHEDULE = (0.0, 0.0)
    llm = _Recorder()
    llm._teaport_progress_active = False
    try:
        await tools._consult_progress(llm, request="anything", gate=None)
    finally:
        tools._PROGRESS_SCHEDULE = saved
    assert len(llm.said) == 2, llm.said


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
