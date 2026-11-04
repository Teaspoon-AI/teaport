#
# Unit test (#246): an openclaw CLI run that outlives its timeout, or is cancelled (a
# barge-in), is killed whole: the process doing the work too, not only the direct child.
# On a host install openclaw.mjs respawns itself on Linux, and SIGKILL cannot be forwarded,
# so killing the parent alone left the respawned CLI running. The stub here does the
# same: the parent starts a child that ignores nothing and outlives it.
#
# The stubs respawn the way openclaw.mjs does off a terminal: the child in a session of
# its own (setsid, its detachForProcessTree), which a kill of the parent's process group
# does not reach either. One child also ignores SIGTERM: what is left after the grace is
# SIGKILLed.
#
# Run: python test_openclaw_cli_group.py
#
import asyncio
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import teaport_brain.openclaw_client as oc  # noqa: E402

# The parent, like openclaw.mjs's respawn: start the real work as a child in a session
# of its own, then wait on it. The child writes its pid and works for a minute ({trap}:
# what it does with a SIGTERM).
STUB = """#!/bin/sh
setsid sh -c '{trap} echo $$ > "{pidfile}"; exec sleep 60' &
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead for this purpose (killed, waiting for its reaper).
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class _Stub:
    def __init__(self, ignores_term: bool = False):
        self.trap = 'trap "" TERM;' if ignores_term else ""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.pidfile = pathlib.Path(self.dir.name, "child.pid")
        cli = pathlib.Path(self.dir.name, "openclaw")
        cli.write_text(STUB.format(pidfile=self.pidfile, trap=self.trap))
        cli.chmod(0o755)
        self.saved, oc.OPENCLAW_BIN = oc.OPENCLAW_BIN, str(cli)
        return self

    def __exit__(self, *exc):
        oc.OPENCLAW_BIN = self.saved
        pid = self.child()
        if pid is not None and _alive(pid):
            os.kill(pid, 9)            # never leave the stub's child behind
        self.dir.cleanup()

    def child(self):
        try:
            return int(self.pidfile.read_text())
        except (OSError, ValueError):
            return None

    async def started(self) -> int:
        deadline = time.monotonic() + 5
        while self.child() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert self.child() is not None, "the stub's child never started"
        return self.child()


async def _gone(pid: int, secs: float = 3.0) -> bool:
    deadline = time.monotonic() + secs
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return not _alive(pid)


async def test_a_timed_out_run_kills_the_respawned_cli_too():
    with _Stub() as stub:
        run = asyncio.create_task(oc._run_openclaw([], timeout=0.5, capture=True))
        child = await stub.started()
        try:
            await run
            raise AssertionError("the run did not time out")
        except asyncio.TimeoutError:
            pass
        assert await _gone(child), f"the respawned CLI (pid {child}) outlived the timeout"


async def test_a_cancelled_run_kills_the_respawned_cli_too():
    with _Stub() as stub:
        run = asyncio.create_task(oc._run_openclaw([], timeout=30, capture=True))
        child = await stub.started()
        run.cancel()
        try:
            await run
        except asyncio.CancelledError:
            pass
        assert run.cancelled()
        assert await _gone(child), f"the respawned CLI (pid {child}) outlived the cancel"


async def test_the_respawned_cli_is_in_a_group_of_its_own():
    # The premise: the stub's child is out of the CLI's group, as openclaw.mjs's is, so
    # a kill of that group alone (the first cut of this fix) would not reach it.
    with _Stub() as stub:
        run = asyncio.create_task(oc._run_openclaw([], timeout=30, capture=True))
        child = await stub.started()
        assert os.getpgid(child) == child, "the stub's child should lead its own group"
        run.cancel()
        try:
            await run
        except asyncio.CancelledError:
            pass
        assert await _gone(child)


async def test_a_cli_that_ignores_sigterm_is_killed_after_the_grace():
    with _Stub(ignores_term=True) as stub:
        run = asyncio.create_task(oc._run_openclaw([], timeout=0.3, capture=True))
        child = await stub.started()
        t0 = time.monotonic()
        try:
            await run
        except asyncio.TimeoutError:
            pass
        # Well inside the grace: the timeout returns without waiting out the teardown.
        assert time.monotonic() - t0 < 2.0, "the timeout waited for the CLI's teardown"
        await asyncio.sleep(0.5)
        assert _alive(child), "SIGTERM is ignored: still there inside the grace"
        assert await _gone(child, oc._END_GRACE_S + 2), \
            f"the CLI (pid {child}) outlived the grace's SIGKILL"


async def test_the_tree_is_walked_off_the_event_loop():
    # The walk reads every process's stat: at a barge-in it must not hold up the voice
    # pipeline. A slow walk here, and the cancel still returns at once.
    real = oc._tree

    def slow_tree(pid):
        time.sleep(0.5)
        return real(pid)

    with _Stub() as stub:
        oc._tree = slow_tree
        try:
            run = asyncio.create_task(oc._run_openclaw([], timeout=30, capture=True))
            child = await stub.started()
            t0 = time.monotonic()
            run.cancel()
            try:
                await run
            except asyncio.CancelledError:
                pass
            assert time.monotonic() - t0 < 0.3, "the cancel waited for the /proc walk"
            assert await _gone(child), f"the respawned CLI (pid {child}) outlived the cancel"
        finally:
            oc._tree = real


async def test_a_run_that_finishes_is_returned_as_before():
    with tempfile.TemporaryDirectory() as d:
        cli = pathlib.Path(d, "openclaw")
        cli.write_text("#!/bin/sh\necho out\necho err >&2\nexit 3\n")
        cli.chmod(0o755)
        saved, oc.OPENCLAW_BIN = oc.OPENCLAW_BIN, str(cli)
        try:
            rc, out, err = await oc._run_openclaw([], timeout=10, capture=True)
        finally:
            oc.OPENCLAW_BIN = saved
    assert (rc, out.strip(), err.strip()) == (3, b"out", b"err"), (rc, out, err)


async def test_the_cli_leads_its_own_process_group():
    # The brain's own group is not the CLI's: a kill of the CLI's group never reaches
    # the brain, and a signal to the brain's group (a terminal's Ctrl-C) not the CLI.
    with tempfile.TemporaryDirectory() as d:
        cli = pathlib.Path(d, "openclaw")
        cli.write_text("#!/bin/sh\nps -o pgid= -p $$\n")
        cli.chmod(0o755)
        saved, oc.OPENCLAW_BIN = oc.OPENCLAW_BIN, str(cli)
        try:
            rc, out, _ = await oc._run_openclaw([], timeout=10, capture=True)
        finally:
            oc.OPENCLAW_BIN = saved
    assert rc == 0 and int(out) != os.getpgrp(), (rc, out, os.getpgrp())


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            await fn()
            print(f"  ok {name}")
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
