#
# Unit test (#284): tasks.cancel_and_wait ends a task without eating the caller's own
# cancellation -- the `task.cancel(); try: await task; except CancelledError: pass`
# idiom it replaces dropped a stop of the caller that landed while it waited.
#
# Run: python test_tasks.py
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from teaport_brain.tasks import cancel_and_wait, wait_ended  # noqa: E402


async def _slow_to_stop(started: asyncio.Event, secs: float):
    """A task whose teardown takes a while: it finishes `secs` after it is cancelled."""
    started.set()
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await asyncio.sleep(secs)
        raise


async def test_a_cancellation_of_the_caller_while_it_waits_is_raised():
    started = asyncio.Event()
    inner = asyncio.create_task(_slow_to_stop(started, 0.5))
    await started.wait()
    went_on = []

    async def caller():
        await cancel_and_wait(inner)
        went_on.append(True)           # must never run: the caller was cancelled

    outer = asyncio.create_task(caller())
    await asyncio.sleep(0.05)          # outer is now waiting out inner's teardown
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    assert outer.cancelled(), "the caller's cancellation was eaten"
    assert not went_on


async def test_the_old_idiom_ate_it():
    # The premise: the idiom cancel_and_wait replaces does eat it (so the test above
    # tests something).
    started = asyncio.Event()
    inner = asyncio.create_task(_slow_to_stop(started, 0.5))
    await started.wait()

    async def caller():
        inner.cancel()
        try:
            await inner
        except asyncio.CancelledError:
            pass
        return "went on"

    outer = asyncio.create_task(caller())
    await asyncio.sleep(0.05)
    outer.cancel()
    assert await outer == "went on"


async def test_the_tasks_own_end_is_returned_not_raised():
    async def fails_on_cancel():
        try:
            await asyncio.sleep(3600)
        finally:
            raise RuntimeError("teardown failed")

    t = asyncio.create_task(fails_on_cancel())
    await asyncio.sleep(0)
    ended = await cancel_and_wait(t)
    assert isinstance(ended, RuntimeError), ended

    started = asyncio.Event()
    t = asyncio.create_task(_slow_to_stop(started, 0.0))
    await started.wait()
    assert await cancel_and_wait(t) is None, "a plain cancellation ends with nothing"
    assert await cancel_and_wait(None) is None


async def test_a_task_outliving_the_timeout_says_so():
    started = asyncio.Event()
    t = asyncio.create_task(_slow_to_stop(started, 1.0))
    await started.wait()
    ended = await cancel_and_wait(t, timeout=0.1)
    assert isinstance(ended, asyncio.TimeoutError), ended
    assert not t.done() and t.cancelling(), "left cancelled, to finish on its own"
    assert await wait_ended(t, timeout=5.0) is None


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            await fn()
            print(f"  ok {name}")
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
