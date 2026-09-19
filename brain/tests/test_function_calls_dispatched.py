#
# Unit test: every function call — including a silent cancel_<tool> call — must tell
# llm_text_guard a call happened, before the turn's End frame is ever pushed.
#
# base_llm's own FunctionCallsStartedFrame excludes the built-in cancel_<tool> call from
# its broadcast (run_function_calls filters it out of user_visible_calls, since it is an
# internal mechanism, not a user-facing one). A completion whose ONLY call is a silent
# cancel therefore never sets llm_text_guard's _answered_otherwise, and if it also
# streamed some leading punctuation before the call, the guard mistakes the cancel for a
# vanished reply and speaks an apology over it.
#
# FunctionCallInProgressFrame — broadcast for every call including the cancel tool — was
# considered and rejected: base_llm schedules each call with create_task() and never
# awaits it before _process_context's `finally` pushes LLMFullResponseEndFrame, so that
# broadcast reliably arrives too late. BoundedOpenAILLMService.run_function_calls instead
# broadcasts FunctionCallsDispatchedFrame for every call, synchronously and fully awaited,
# BEFORE calling super() — so it is guaranteed to precede the End.
#
# Run: python test_function_calls_dispatched.py   (or via pytest)
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import FunctionCallFromLLM  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from teaport_brain.services import (  # noqa: E402
    BoundedOpenAILLMService,
    FunctionCallsDispatchedFrame,
)


def _llm():
    return BoundedOpenAILLMService(
        api_key="test", base_url="http://127.0.0.1:1",
        settings=BoundedOpenAILLMService.Settings(model="x"),
    )


def _call(name="cancel_ask_openclaw"):
    return FunctionCallFromLLM(context=None, tool_call_id="t1", function_name=name,
                                arguments={})


async def test_dispatched_frame_is_broadcast_for_a_cancel_only_call():
    """The exact gap: base_llm's FunctionCallsStartedFrame would stay silent for this
    call (it is the cancel tool), but our override must not."""
    llm = _llm()
    pushed = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))
    llm.push_frame = fake_push

    async def fake_super_run(function_calls):
        pass
    import teaport_brain.services as services_mod
    orig = services_mod.OpenAILLMService.run_function_calls
    services_mod.OpenAILLMService.run_function_calls = lambda self, fcs: fake_super_run(fcs)
    try:
        await llm.run_function_calls([_call()])
    finally:
        services_mod.OpenAILLMService.run_function_calls = orig

    dispatched = [f for f, _ in pushed if isinstance(f, FunctionCallsDispatchedFrame)]
    assert len(dispatched) == 2, "broadcast_frame must push one copy each direction"
    assert [c.function_name for c in dispatched[0].function_calls] == ["cancel_ask_openclaw"]


async def test_dispatched_frame_precedes_the_real_dispatch():
    """The whole point is ordering: the guard must see this before base_llm schedules
    (and, downstream, before _process_context's `finally` pushes the End)."""
    llm = _llm()
    order = []

    import teaport_brain.services as services_mod

    async def fake_broadcast(frame_cls, **kwargs):
        order.append("broadcast")

    async def fake_super_run(self, fcs):
        order.append("super")

    llm.broadcast_frame = fake_broadcast
    orig = services_mod.OpenAILLMService.run_function_calls
    services_mod.OpenAILLMService.run_function_calls = fake_super_run
    try:
        await llm.run_function_calls([_call()])
    finally:
        services_mod.OpenAILLMService.run_function_calls = orig

    assert order == ["broadcast", "super"], order


async def test_no_calls_means_no_broadcast():
    """Mirrors base_llm's own `if user_visible_calls:` guard: an empty list is not a
    call that happened, so there is nothing to tell the guard."""
    llm = _llm()
    called = []
    llm.broadcast_frame = lambda *a, **k: called.append(1) or asyncio.sleep(0)

    import teaport_brain.services as services_mod

    async def fake_super_run(self, fcs):
        pass
    orig = services_mod.OpenAILLMService.run_function_calls
    services_mod.OpenAILLMService.run_function_calls = fake_super_run
    try:
        await llm.run_function_calls([])
    finally:
        services_mod.OpenAILLMService.run_function_calls = orig
    assert not called


def main():
    sync = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    for fn in sync:
        fn()
        print(f"  ok {fn.__name__}")

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
