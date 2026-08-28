#
# test_remember_tool.py — validate the `remember` tool end-to-end through the REAL
# pipeline (no audio): inject a "remember that X" transcript, run
# [ inject -> user aggregator -> LLM(+tools) -> capture ], and confirm the LLM CALLS
# `remember`, whose handler writes the fact to today's shared daily memory note.
#
# Asserts only the DETERMINISTIC core (tool fired + fact written to the note). The
# spoken confirmation and *semantic* recall are reported but not asserted — they depend
# on the cloud LLM responding and the ~7 s background reindex, which are timing/throttle
# dependent (recall is validated separately via the gateway). Writes to an ISOLATED tmp
# memory dir so it never pollutes the real store. Run on the box. Usage: python test_remember_tool.py
#
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appliance  # noqa: E402

# Isolate the write BEFORE importing openclaw_client (it reads MEMORY_DIR at import).
_TMP_MEM = tempfile.mkdtemp(prefix="teaport-remember-test-")
os.environ["OPENCLAW_MEMORY_DIR"] = _TMP_MEM

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

from pipecat.frames.frames import (  # noqa: E402
    Frame,
    LLMRunFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.utils.time import time_now_iso8601  # noqa: E402

from teaport_brain import tools as tools_mod  # noqa: E402
from teaport_brain.persona import build_system_prompt  # noqa: E402
from teaport_brain.services import make_llm  # noqa: E402

FIRED = []


class Injector(FrameProcessor):
    def __init__(self, text):
        super().__init__()
        self._t = text

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.create_task(self._emit())

    async def _emit(self):
        await asyncio.sleep(0.2)
        await self.push_frame(TranscriptionFrame(self._t, "user", time_now_iso8601(), None))
        await self.push_frame(LLMRunFrame())


class Capture(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.reply = ""
        self.done = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.reply += frame.text
            if self.reply.strip():
                self.create_task(self._settle())
        await self.push_frame(frame, direction)

    async def _settle(self):
        await asyncio.sleep(1.5)
        self.done.set()


async def main() -> int:
    # Before make_llm(), which raises on an unset base URL — that is a missing
    # dependency, not a failure of what this test checks.
    appliance.require_env("LLM_BASE_URL", "a live OpenAI-compatible LLM")
    llm = make_llm()
    tools_mod.register_tools(llm)
    orig = tools_mod._remember

    async def traced(params):
        FIRED.append((params.function_name, (params.arguments or {}).get("fact", "")))
        await orig(params)

    llm.register_function("remember", traced)

    context = LLMContext(
        [{"role": "system", "content": build_system_prompt("")}],
        tools=tools_mod.build_tools_schema(),
    )
    aggr = LLMContextAggregatorPair(context)
    cap = Capture()
    task = PipelineTask(Pipeline([
        Injector("Please remember that my favorite hobby is rock climbing in the Dolomites."),
        aggr.user(), llm, cap, aggr.assistant(),
    ]))
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        await asyncio.wait_for(cap.done.wait(), timeout=40)
    except asyncio.TimeoutError:
        pass
    finally:
        await task.cancel()
        try:
            await asyncio.wait_for(run, timeout=5)
        except Exception:
            pass
    await asyncio.sleep(0.3)  # let the synchronous write settle

    fired = any(name == "remember" and "rock climbing" in (fact or "").lower()
                for name, fact in FIRED)
    notes = "".join(open(os.path.join(_TMP_MEM, f)).read()
                    for f in os.listdir(_TMP_MEM)) if os.path.isdir(_TMP_MEM) else ""
    written = "rock climbing" in notes.lower()

    print(f"remember fired (with fact): {fired}  calls={FIRED}")
    print(f"fact written to daily note: {written}")
    print(f"note line: {notes.strip()[:90]!r}")
    print(f"spoken reply (best-effort, throttle-dependent): {cap.reply.strip()[:90]!r}")
    ok = fired and written
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        import shutil
        shutil.rmtree(_TMP_MEM, ignore_errors=True)
    sys.exit(rc)
