#
# Unit test: a service pipecat has written off ends the session — and only those that
# should.
#
# pipecat 1.8.0 introduced is_usable: a permanent error clears it FOR GOOD, and the
# services then refuse work themselves — an STT that gave up reconnecting is handed no
# more audio and will not reconnect, a TTS written off is handed no more text, an output
# transport that timed out on a write drops every write after it. At 1.7.0 none of that
# existed and every one of these outages self-healed. Nothing in pipecat ENDS anything
# for it either: PipelineTask's processor_unusable_policy defaults to CONTINUE, so the
# session runs on — deaf, mute, or unable to reach the caller — for as long as the user
# stays on it, with one logger.error to show for it.
#
# Three properties, and the third is why this file is not just the policy set to END:
#
#   1. The transient paths stay transient. EngineTTSService yields an ErrorFrame and no
#      audio when the engine OOMs or restarts mid-reply; pipecat 1.8.0 counts three of
#      those in a row as permanent. Disabled (0), so it self-heals as it did at 1.7.0.
#   2. The permanent ones end the session, for the three processors whose loss IS the
#      loss of the session: STT, TTS, output transport.
#   3. The LLM is NOT one of them. pipecat calls 400/401/403/404/422 permanent
#      (utils/errors.py), which includes a context that outgrew the model's window —
#      and LLMErrorSpeaker exists precisely so that is SPOKEN and survived. A
#      pipeline-wide END policy would hang the caller up on it instead.
#
# Run: python test_service_unusable.py   (hermetic; nothing connects at build time)
#
import asyncio
import os
import sys

# Before teaport_brain.services is imported anywhere: it reads TEAPORT_URL at module
# scope, and make_llm refuses to build without LLM_BASE_URL. Nothing connects while a
# session is merely built, so these only have to be syntactically real.
os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_API_KEY", "not-a-real-key")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.pipeline.worker import ProcessorUnusablePolicy  # noqa: E402
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402

from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.stt import CONNECT_BUDGET_S  # noqa: E402


class _Stub(FrameProcessor):
    """A processor standing in for a transport side. Never linked into anything."""


class _Transport:
    """The minimum build_agent_session needs of a transport: an input and an output.

    output() must return the SAME object every time — the factory uses it three times
    (pipeline, ledger, unusable handler) and a fresh one each call would leave the
    handler watching a processor no pipeline contains."""

    def __init__(self):
        self._input = _Stub(name="stub-in")
        self._output = _Stub(name="stub-out")

    def input(self):
        return self._input

    def output(self):
        return self._output


async def _build():
    transport = _Transport()
    return transport, build_agent_session(transport)


async def _ends_the_session(session, processor) -> bool:
    """Write `processor` off the way a permanent error does; did the session end?"""
    ended = []

    async def spy():
        ended.append(True)

    original = session.task.stop_when_done
    session.task.stop_when_done = spy
    try:
        await processor.set_usable(False)
        # on_usable_changed is not a sync handler, so pipecat runs it as its own task.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ended:
                break
    finally:
        session.task.stop_when_done = original
    return bool(ended)


async def test_a_silent_tts_context_does_not_write_the_voice_off():
    """The engine failing one reply must not mute the bot for the rest of the call.

    pipecat's counter is aimed at a provider that accepts requests and stays silent (an
    unknown voice ID). Ours are per-utterance: "nothing synthesizable", and every clause
    failing on a CUDA OOM or an engine restart — both clear on their own, and both
    already report themselves. Three in a row would otherwise cost is_usable, and
    make_tts builds a fresh service only per SESSION."""
    _, session = await _build()
    assert session.tts._max_consecutive_zero_audio_contexts == 0, (
        "EngineTTSService is back on pipecat's zero-audio counter: three failed replies "
        "in a row now mute the bot permanently, where 1.7.0 recovered on the next "
        "successful clause")


async def test_the_pipeline_policy_stays_continue_so_an_llm_error_is_spoken():
    """END here would be pipeline-WIDE, and pipecat calls an LLM 400 permanent."""
    _, session = await _build()
    assert session.task._processor_unusable_policy is ProcessorUnusablePolicy.CONTINUE, (
        "the unusable policy went pipeline-wide: a permanent LLM error (400/401/403/"
        "404/422 — a context that outgrew the window is one) would now end the session "
        "instead of reaching LLMErrorSpeaker, which exists to speak it and carry on")


async def test_the_start_budget_covers_the_stt_connect():
    """1.8.0 bounded the StartFrame's trip at 20s and TEARS THE PIPELINE DOWN past it.

    stt.py connects to the engine inside that StartFrame on purpose, and returns rather
    than raises on failure so the brain can SPEAK "I can't hear you". At the default
    that design's own worst case is a dead session with nothing spoken."""
    _, session = await _build()
    for name, value in (("start", session.task._start_timeout_secs),
                        ("setup", session.task._setup_timeout_secs)):
        assert value > CONNECT_BUDGET_S, (
            f"{name}_timeout_secs is {value}s, inside the STT's own "
            f"{CONNECT_BUDGET_S}s connect budget — a slow engine would be torn down "
            f"mid-connect instead of warning the user")


async def test_a_written_off_stt_tts_or_transport_ends_the_session():
    """Deaf, mute, or unable to reach the caller: each one ends it."""
    transport, session = await _build()
    for what, processor in (("STT", session.stt),
                            ("TTS", session.tts),
                            ("output transport", transport.output())):
        assert await _ends_the_session(session, processor), (
            f"the {what} was written off and the session carried on — pipecat hands it "
            f"no more work, so the user is left in a session that cannot recover")


async def test_a_written_off_llm_does_not_end_the_session():
    """The counterpart, and the reason the policy is not END."""
    _, session = await _build()
    assert not await _ends_the_session(session, session.llm), (
        "a permanent LLM error ended the session; LLMErrorSpeaker speaks it and the "
        "conversation continues — that is the behaviour a pipeline-wide END policy "
        "would take away")


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
