#
# One brain process serves the phone and Talk (issue #58), and their turn-taking was
# tuned apart in blind A/Bs: each front-end's sessions take their own SIP_*/TALK_*
# override of the shared knob, read when the session is built (endpointing.turn_settings).
#
# Run: python test_turn_settings.py   (or via pytest test_suite.py)
#
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from teaport_brain import endpointing  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402

OVERRIDES = {"TALK_ENDPOINT_STOP_SECS": "0.2", "TALK_SMARTTURN_STOP_SECS": "0.6",
             "SIP_INTERRUPT_MIN_WORDS": "3", "SIP_SMARTTURN_COMPLETE_THRESHOLD": "0.4"}


class _Env:
    def __init__(self, values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_each_front_end_takes_its_own_override_else_the_shared_value():
    shared = endpointing.TurnSettings()
    with _Env(OVERRIDES):
        talk, sip = endpointing.turn_settings("talk"), endpointing.turn_settings("sip")
    assert (talk.endpoint_stop_secs, talk.smartturn_stop_secs) == (0.2, 0.6)
    assert talk.interrupt_min_words == shared.interrupt_min_words
    assert sip.endpoint_stop_secs == shared.endpoint_stop_secs
    assert (sip.interrupt_min_words, sip.smartturn_complete_threshold) == (3, 0.4)
    assert endpointing.turn_settings(None) == shared


def test_a_typo_falls_back_to_the_shared_value():
    with _Env({"SIP_ENDPOINT_STOP_SECS": "0,5", "TALK_INTERRUPT_MIN_WORDS": " "}):
        assert endpointing.turn_settings("sip").endpoint_stop_secs == endpointing.ENDPOINT_STOP_SECS
        assert endpointing.turn_settings("talk").interrupt_min_words == endpointing.INTERRUPT_MIN_WORDS


def test_a_call_and_a_talk_session_in_one_process_are_built_with_their_own_values():
    def build(front_end):
        t = FastAPIWebsocketTransport(websocket=SimpleNamespace(), params=FastAPIWebsocketParams(
            serializer=TeaportGatewaySerializer()))
        return build_agent_session(t, front_end=front_end)

    async def run():
        with _Env(OVERRIDES):
            return build("sip"), build("talk")
    sip, talk = asyncio.run(run())
    assert talk.vad_analyzer.params.stop_secs == 0.2
    assert sip.vad_analyzer.params.stop_secs == endpointing.ENDPOINT_STOP_SECS
    assert (talk.stt.smartturn_stop_secs, sip.stt.smartturn_stop_secs) == (0.6, endpointing.SMARTTURN_STOP_SECS)
    assert sip.turns.interrupt_min_words == 3 and talk.turns.interrupt_min_words == endpointing.INTERRUPT_MIN_WORDS


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
