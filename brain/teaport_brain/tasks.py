#
# tasks.py — ending a task from another one, without eating the caller's own cancellation.
#
# The usual way to stop a task and wait for it,
#
#     task.cancel()
#     try:
#         await task
#     except asyncio.CancelledError:
#         pass
#
# cannot tell the task's CancelledError from the caller's. If the caller is cancelled
# while it waits (the brain stopping, a call torn down), its cancellation lands in the
# same `except` and is dropped: the caller carries on as if nothing had asked it to stop.
# That is #284: a SIGTERM that reached the SIP front-end while it was waiting out the
# teardown of a call the brain had just ended was eaten in cancel_setup, the receive loop
# went back to reading the gateway's socket, and uvicorn's shutdown waited on it until
# systemd's SIGKILL.
#
# cancel_and_wait waits with asyncio.wait instead, which never raises the task's own
# outcome and never catches the caller's cancellation: that propagates as it should.
#
import asyncio
from typing import Optional


async def cancel_and_wait(task: Optional[asyncio.Task],
                          timeout: Optional[float] = None) -> Optional[BaseException]:
    """Cancel `task` and wait until it has finished. Returns the exception it ended with,
    if any other than its cancellation; asyncio.TimeoutError if it was still running
    after `timeout` seconds (it is left cancelled, to finish on its own). A cancellation
    of the CALLER while it waits is raised, never absorbed."""
    if task is None:
        return None
    task.cancel()
    return await wait_ended(task, timeout)


async def wait_ended(task: asyncio.Task,
                     timeout: Optional[float] = None) -> Optional[BaseException]:
    """Wait until `task` has finished, without cancelling it. Returns the exception it
    ended with, if any other than a cancellation; asyncio.TimeoutError if it is still
    running after `timeout` seconds. A cancellation of the CALLER is raised."""
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        return asyncio.TimeoutError()
    return None if task.cancelled() else task.exception()
