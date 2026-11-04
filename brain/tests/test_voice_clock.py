#
# test_voice_clock.py — playout_pause.VoiceClock: the pipeline clock less every moment
# the voice stood still (playout_pause.py, THE VOICE CLOCK; formal/VoiceClock.tla).
#
# Hermetic: a pipeline clock the test moves by hand, and the event loop for the waits.
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain.playout_pause import VoiceClock  # noqa: E402

MS = 1_000_000


class Pipeline:
    """The pipeline clock (ns), moved by hand."""
    t = 5_000 * MS

    def get_time(self):
        return self.t


def _clock():
    base, voice = Pipeline(), VoiceClock()
    voice.attach(base)
    return base, voice


def test_it_runs_with_the_pipeline_clock_until_a_stop():
    base, voice = _clock()
    assert voice.now_ns() == base.t
    base.t += 300 * MS
    assert voice.now_ns() == base.t and not voice.standing


def test_a_stop_takes_hold_once_what_was_sent_has_played():
    # The pause's lag: what the transport sent before it still plays.
    base, voice = _clock()
    t0 = base.t
    voice.stop(after_secs=0.04)
    base.t += 20 * MS
    assert voice.now_ns() == t0 + 20 * MS and not voice.standing, "the lag still plays"
    base.t += 500 * MS
    assert voice.now_ns() == t0 + 40 * MS and voice.standing
    voice.start()
    assert voice.now_ns() == t0 + 40 * MS, "it goes on from where it stood"
    base.t += 100 * MS
    assert voice.now_ns() == t0 + 140 * MS


def test_a_resume_inside_the_lag_never_stood_still():
    base, voice = _clock()
    t0 = base.t
    voice.stop(after_secs=0.04)
    base.t += 10 * MS
    voice.start()
    base.t += 100 * MS
    assert voice.now_ns() == t0 + 110 * MS


def test_stops_add_up_and_a_second_stop_or_start_changes_nothing():
    base, voice = _clock()
    t0 = base.t
    for _ in range(2):
        voice.stop()
        voice.stop()                                  # the first one holds
        base.t += 250 * MS
        voice.start()
        voice.start()
        base.t += 50 * MS
    assert voice.now_ns() == t0 + 100 * MS


def test_an_instant_just_before_a_start_reads_where_the_voice_stood():
    # An observer's timestamp (the ledger's) can be a moment older than the start.
    base, voice = _clock()
    t0 = base.t
    base.t += 100 * MS
    voice.stop()
    base.t += 400 * MS
    voice.start()
    assert voice.at(t0 + 450 * MS) == t0 + 100 * MS, "inside the stop: where it stood"
    assert voice.at(t0 + 50 * MS) == t0 + 50 * MS, "before the stop: as it ran"
    assert voice.at(base.t) == t0 + 100 * MS


def test_an_instant_behind_two_stops_still_maps_right():
    # Review nit 2: an observer that has fallen behind more than one pause.
    base, voice = _clock()
    t0 = base.t
    for stop, start in ((1000, 1500), (2000, 2300)):
        base.t = t0 + stop * MS
        voice.stop()
        base.t = t0 + start * MS
        voice.start()
    assert voice.at(t0 + 1200 * MS) == t0 + 1000 * MS, "inside the first stop"
    assert voice.at(t0 + 900 * MS) == t0 + 900 * MS, "before both"
    assert voice.at(t0 + 1800 * MS) == t0 + 1300 * MS, "between them"
    assert voice.at(t0 + 2100 * MS) == t0 + 1500 * MS, "inside the second"
    assert voice.now_ns() == t0 + 1500 * MS


def test_when_maps_a_voice_instant_back_to_the_pipeline_clock():
    base, voice = _clock()
    t0 = base.t
    base.t = t0 + 1000 * MS
    voice.stop()
    base.t = t0 + 1500 * MS
    voice.start()
    base.t = t0 + 2000 * MS
    assert voice.when(t0 + 800 * MS) == t0 + 800 * MS, "before the stop: as it ran"
    assert voice.when(t0 + 1000 * MS) == t0 + 1000 * MS, "the instant it stopped"
    assert voice.when(t0 + 1200 * MS) == t0 + 1700 * MS, "after it: the stop later"
    assert voice.when(t0 + 1600 * MS) is None, "not reached yet: not played"


def test_a_start_inside_the_lag_leaves_no_stop_behind():
    # Review round 2, N1: the voice never reached the stop it would have taken. A later
    # stop before that instant must map on its own.
    base, voice = _clock()
    t0 = base.t
    voice.stop(after_secs=0.5)                      # due at +500 ms...
    base.t = t0 + 100 * MS
    voice.start()                                   # ...never reached
    base.t = t0 + 200 * MS
    voice.stop()                                    # a stop that takes hold at +200
    base.t = t0 + 700 * MS
    voice.start()
    base.t = t0 + 1000 * MS
    assert voice.when(t0 + 300 * MS) == t0 + 800 * MS, voice.when(t0 + 300 * MS)
    assert voice.at(t0 + 400 * MS) == t0 + 200 * MS


def test_it_reads_zero_until_it_is_attached():
    voice = VoiceClock()
    assert not voice.attached and voice.now_ns() == 0
    voice.stop()
    voice.start()


class _LoopClock:
    """The pipeline clock on the event loop's own time (ns from its making)."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.t0 = self.loop.time()

    def get_time(self):
        return int((self.loop.time() - self.t0) * 1e9)


def _loop_voice():
    voice = VoiceClock()
    voice.attach(_LoopClock())
    return voice


async def test_a_wait_runs_through_a_stop():
    voice = _loop_voice()
    w = asyncio.create_task(voice.wait_until(200 * MS))
    await asyncio.sleep(0.1)
    voice.stop()
    await asyncio.sleep(0.3)
    voice.start()
    t_start = asyncio.get_running_loop().time()
    await asyncio.wait_for(w, 0.5)
    rest = asyncio.get_running_loop().time() - t_start
    assert 0.07 <= rest <= 0.16, f"{rest:.3f}s after the start: the 0.1 s still to go"


async def test_a_wait_stops_for_a_stop_that_comes_while_it_sleeps():
    # The wait sleeps until the pts on the running clock; a stop meanwhile must wake it,
    # or it would release the word at the old time, while the voice stands still.
    voice = _loop_voice()
    w = asyncio.create_task(voice.wait_until(200 * MS))
    await asyncio.sleep(0.1)
    voice.stop()
    await asyncio.sleep(0.25)
    assert not w.done(), "a word was released while the voice stood still"
    voice.start()
    await asyncio.wait_for(w, 0.3)


async def test_a_stop_ahead_wakes_the_wait_at_the_stop():
    # A pause's lag: the stop is due a moment ahead. A word due after it must not be
    # released at its old time.
    voice = _loop_voice()
    voice.stop(after_secs=0.05)
    w = asyncio.create_task(voice.wait_until(150 * MS))
    await asyncio.sleep(0.3)
    assert not w.done(), "released past the stop"
    voice.start()
    await asyncio.sleep(0.05)
    assert not w.done(), "released before the rest of its time on the voice clock"
    await asyncio.wait_for(w, 0.2)


async def test_a_word_whose_audio_is_past_a_stop_waits_for_the_start():
    # Review should-fix 1: every word is stamped the caption lead ahead of its audio. One
    # due just before a stop has its audio after it: it waits for the start, though its
    # pts comes first -- nobody hears it before then (the echo guard stamps it then).
    voice = _loop_voice()
    voice.stop(after_secs=0.1)
    seam = asyncio.create_task(voice.wait_until(50 * MS, lead_ns=200 * MS))
    early = asyncio.create_task(voice.wait_until(30 * MS, lead_ns=50 * MS))
    await asyncio.sleep(0.3)
    assert early.done(), "a word whose audio plays before the stop is released at its pts"
    assert not seam.done(), "released before the stop, its audio after it"
    voice.start()
    await asyncio.wait_for(seam, 0.05)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run_aio():
        for fn in tests:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
