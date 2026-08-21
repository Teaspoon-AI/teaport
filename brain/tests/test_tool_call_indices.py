#
# Unit test: a tool call must be dispatched whichever way the endpoint numbers it.
#
# base_llm coalesces streamed tool-call deltas assuming `tool_call.index` counts calls
# WITHIN the response (0 for the first, 1 for the second). It starts at func_idx=0,
# treats any other index as "a new call started" — flushing the accumulator and resetting
# function_name — and then gates the ENTIRE dispatch on that last function_name being
# non-empty.
#
# The endpoint teaport talks to numbers them by position in the TOOLS ARRAY instead.
# `remember` is the sixth of nine tools, so its deltas arrive as index=5, every one trips
# the "new call" branch, function_name ends empty, and run_function_calls() is never
# reached. The parsed call is discarded with no error, no warning and no log line: the
# model asks for a tool and the pipeline simply does not run it. A tool at array index 0
# would work and every other tool would not.
#
# The two cases below are the two conventions seen live, replayed through the real
# accumulator logic rather than a paraphrase of it.
#
# Run: on the appliance only — see pinned_pipecat.py.
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain.services import _sequential_tool_call_indices  # noqa: E402


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, name=None, arguments=None, id=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.content = None


class _Choice:
    def __init__(self, tool_calls):
        self.delta = _Delta(tool_calls)
        self.finish_reason = None


class _Chunk:
    def __init__(self, tool_calls):
        self.choices = [_Choice(tool_calls)]


async def _renumber(deltas):
    """Push (index, name) deltas through the wrapper; return the indices it emits."""

    async def stream():
        for index, name in deltas:
            yield _Chunk([_ToolCall(index, name=name)])

    out = []
    async for chunk in _sequential_tool_call_indices(stream()):
        out += [tc.index for tc in chunk.choices[0].delta.tool_calls]
    return out


def _dispatches(deltas):
    """base_llm's coalescing, verbatim in behaviour: does it reach run_function_calls?"""
    func_idx, function_name = 0, ""
    for index, name in deltas:
        if index != func_idx:
            function_name = ""
            func_idx += 1
        if name:
            function_name += name
    return bool(function_name)


# `remember` is the sixth of nine tools; one call, three deltas.
BY_ARRAY_POSITION = [(5, "remember"), (5, None), (5, None)]
SEQUENTIAL = [(0, "remember"), (0, None), (0, None)]
TWO_CALLS_BY_POSITION = [(5, "remember"), (5, None), (8, "switch_voice"), (8, None)]


def test_the_accumulator_really_does_drop_array_indexed_calls():
    """The premise. If this ever fails, base_llm was fixed and the wrapper can go."""
    assert _dispatches(SEQUENTIAL), "sequential numbering should always have worked"
    assert not _dispatches(BY_ARRAY_POSITION), (
        "array-position numbering no longer breaks dispatch — drop the wrapper"
    )


async def test_array_positions_become_sequential():
    assert await _renumber(BY_ARRAY_POSITION) == [0, 0, 0]
    assert _dispatches(list(zip(await _renumber(BY_ARRAY_POSITION),
                                [n for _, n in BY_ARRAY_POSITION])))


async def test_sequential_numbering_is_left_alone():
    """Identity for a well-behaved provider, so this costs nothing when unneeded."""
    assert await _renumber(SEQUENTIAL) == [0, 0, 0]


async def test_two_calls_keep_their_boundary():
    """Renumbering must not merge distinct calls into one."""
    assert await _renumber(TWO_CALLS_BY_POSITION) == [0, 0, 1, 1]


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
