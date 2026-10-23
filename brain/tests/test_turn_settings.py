#
# One brain process serves the phone and Talk (issue #58), and their turn-taking was
# tuned apart in blind A/Bs: each front-end's sessions take their own SIP_*/TALK_*
# override of the shared knob, read when the session is built (endpointing.turn_settings).
# The box's own mic is a third front-end, "room", with ROOM_* of its own (issue #159):
# a stop and a ceiling with schema defaults, and no TALK_* reaching it.
# Silero's rate is per front-end too (issue #215): the phone's audio is 8 kHz G.711 the
# gateway upsampled, so SIP_VAD_SAMPLE_RATE defaults to 8000, while /talk and the room
# mic are wideband and take the shared VAD_SAMPLE_RATE (16000).
#
# Run: python test_turn_settings.py   (or via pytest test_suite.py)
#
import asyncio
import os
import subprocess
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

from teaport_brain import endpointing, settings  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402
from teaport_brain.endpointing import NarrowbandSileroMixin  # noqa: E402
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


def test_the_room_has_its_own_values_and_not_talks():
    # The box's brain.env sets TALK_* 0.2 / 0.6; a room session must not take them.
    room = endpointing.turn_settings("room")
    with _Env(OVERRIDES):
        assert endpointing.turn_settings("room") == room
    rows = settings.ROWS
    assert (room.endpoint_stop_secs, room.smartturn_stop_secs) == (
        rows["ROOM_ENDPOINT_STOP_SECS"]["default"], rows["ROOM_SMARTTURN_STOP_SECS"]["default"])
    shared = endpointing.TurnSettings()
    assert (room.smartturn_complete_threshold, room.interrupt_min_words) == (
        shared.smartturn_complete_threshold, shared.interrupt_min_words)
    with _Env({"ROOM_ENDPOINT_STOP_SECS": "0.3", "ROOM_INTERRUPT_MIN_WORDS": "3",
               "ROOM_SMARTTURN_STOP_SECS": "soon"}):
        r = endpointing.turn_settings("room")
    assert (r.endpoint_stop_secs, r.interrupt_min_words) == (0.3, 3)
    assert r.smartturn_stop_secs == room.smartturn_stop_secs   # a typo: the room's default


def test_every_front_end_names_each_knob_once_in_field_order():
    # The one table (FRONT_ENDS): a front-end that read its knobs in another order would
    # put a stop into the ceiling with nothing to say so.
    fields = ("ENDPOINT_STOP_SECS", "SMARTTURN_STOP_SECS", "SMARTTURN_COMPLETE_THRESHOLD",
              "INTERRUPT_MIN_WORDS", "VAD_SAMPLE_RATE")
    for name, read in endpointing.FRONT_ENDS.items():
        seen = []
        real = endpointing.setting
        endpointing.setting = lambda n, **k: seen.append(n)
        try:
            read()
        finally:
            endpointing.setting = real
        assert seen == [name.upper() + "_" + f for f in fields], (name, seen)


def test_an_unknown_front_end_raises():
    try:
        endpointing.turn_settings("rooom")
    except KeyError:
        return
    raise AssertionError("a misspelt front-end must not build on the shared values")


def test_a_typo_falls_back_to_the_shared_value():
    with _Env({"SIP_ENDPOINT_STOP_SECS": "0,5", "TALK_INTERRUPT_MIN_WORDS": " "}):
        assert endpointing.turn_settings("sip").endpoint_stop_secs == endpointing.ENDPOINT_STOP_SECS
        assert endpointing.turn_settings("talk").interrupt_min_words == endpointing.INTERRUPT_MIN_WORDS
    # The phone's rate has a schema default of its own: a typo falls back to it, not to
    # the shared 16000.
    with _Env({"SIP_VAD_SAMPLE_RATE": "800", "TALK_VAD_SAMPLE_RATE": "8k"}):
        assert endpointing.turn_settings("sip").vad_sample_rate == 8000
        assert endpointing.turn_settings("talk").vad_sample_rate == endpointing.VAD_SAMPLE_RATE


def test_the_phone_runs_silero_at_8k_and_talk_and_the_room_at_16k_by_default():
    assert endpointing.VAD_SAMPLE_RATE == 16000      # the shared value, unset
    assert settings.ROWS["SIP_VAD_SAMPLE_RATE"]["default"] == "8000"
    rates = {f: endpointing.turn_settings(f).vad_sample_rate for f in endpointing.FRONT_ENDS}
    assert rates == {"sip": 8000, "talk": 16000, "room": 16000}, rates
    assert all(type(r) is int for r in rates.values()), rates
    assert endpointing.turn_settings(None).vad_sample_rate == 16000


def test_a_front_ends_own_rate_wins():
    with _Env({"SIP_VAD_SAMPLE_RATE": "16000", "TALK_VAD_SAMPLE_RATE": "8000",
               "ROOM_VAD_SAMPLE_RATE": "8000"}):
        rates = {f: endpointing.turn_settings(f).vad_sample_rate for f in endpointing.FRONT_ENDS}
    assert rates == {"sip": 16000, "talk": 8000, "room": 8000}, rates
    with _Env({"TALK_VAD_SAMPLE_RATE": "8000"}):   # Talk's does not reach the room
        assert endpointing.turn_settings("room").vad_sample_rate == 16000


def test_the_shared_rate_reaches_talk_and_the_room_but_not_the_phone():
    # VAD_SAMPLE_RATE is read at import (TurnSettings' default), so in a process of its own.
    env = {k: v for k, v in os.environ.items() if not k.endswith("VAD_SAMPLE_RATE")}
    env["VAD_SAMPLE_RATE"] = "16000"
    probe = ("from teaport_brain import endpointing as e; "
             "print(*(e.turn_settings(f).vad_sample_rate for f in ('sip', 'talk', 'room')))")
    def rates(**extra):
        out = subprocess.run([sys.executable, "-c", probe], env={**env, **extra},
                             capture_output=True, text=True, check=True).stdout
        return out.split()[-3:]
    assert rates(VAD_SAMPLE_RATE="8000") == ["8000", "8000", "8000"]
    # The phone has a default of its own, so the shared value does not reach it.
    assert rates(VAD_SAMPLE_RATE="8000", SIP_VAD_SAMPLE_RATE="16000") == ["16000", "8000", "8000"]


def test_a_call_and_a_talk_session_in_one_process_are_built_with_their_own_values():
    def build(front_end):
        t = FastAPIWebsocketTransport(websocket=SimpleNamespace(), params=FastAPIWebsocketParams(
            serializer=TeaportGatewaySerializer()))
        return build_agent_session(t, front_end=front_end)

    async def run():
        with _Env(OVERRIDES):
            return build("sip"), build("talk"), build("room")
    sip, talk, room = asyncio.run(run())
    assert talk.vad_analyzer.params.stop_secs == 0.2
    expect = endpointing.turn_settings("room")
    assert room.vad_analyzer.params.stop_secs == expect.endpoint_stop_secs
    assert room.stt.smartturn_stop_secs == expect.smartturn_stop_secs
    assert room.turns == expect
    assert sip.vad_analyzer.params.stop_secs == endpointing.ENDPOINT_STOP_SECS
    assert (talk.stt.smartturn_stop_secs, sip.stt.smartturn_stop_secs) == (0.6, endpointing.SMARTTURN_STOP_SECS)
    # The STT reads the session's own analyzer for the caller's voice (issue #140).
    for session in (sip, talk):
        assert session.stt.vad_analyzer is session.vad_analyzer
        assert session.vad_analyzer.voice_marks().received == 0
    assert sip.turns.interrupt_min_words == 3 and talk.turns.interrupt_min_words == endpointing.INTERRUPT_MIN_WORDS
    # Each session's analyzer runs at its front-end's rate (issue #215): the phone's
    # decimates to 8 kHz, Talk's and the room's run on the wideband audio as it is.
    assert isinstance(sip.vad_analyzer, NarrowbandSileroMixin) and sip.turns.vad_sample_rate == 8000
    for session in (talk, room):
        assert not isinstance(session.vad_analyzer, NarrowbandSileroMixin)
        assert session.turns.vad_sample_rate == 16000


def test_a_session_is_built_at_its_front_ends_own_rate():
    t = FastAPIWebsocketTransport(websocket=SimpleNamespace(), params=FastAPIWebsocketParams(
        serializer=TeaportGatewaySerializer()))

    async def run():
        with _Env({"SIP_VAD_SAMPLE_RATE": "16000", "ROOM_VAD_SAMPLE_RATE": "8000"}):
            return build_agent_session(t, front_end="sip"), build_agent_session(t, front_end="room")
    sip, room = asyncio.run(run())
    assert not isinstance(sip.vad_analyzer, NarrowbandSileroMixin)
    assert isinstance(room.vad_analyzer, NarrowbandSileroMixin)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
