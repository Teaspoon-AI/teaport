#
# Typing on phone calls (#242): which sessions get the thinking sound's bed. Talk and the
# room type through a long agent consult unless TEAPORT_THINKING_SOUND=0, the kill switch
# for every front-end. A phone call types only with SIP_THINKING_SOUND on (off by
# default) and no half-duplex gate on the call (SIP_HALF_DUPLEX: the caller could not stop
# it), or in agent-first (TEAPORT_AGENT_FIRST), which keeps its typing whatever the other
# two say, as on main: thinking_sound.enabled_for, the gate agent_session builds the bed
# by. Turning calls on changes no routing: a call's consults stay background ones.
#
# And a call's bed types for no face. The OLED face types along to the room bridge's
# session (local_audio.py announces "typing" in its ?features=); sip_server builds a call
# with no client features, so its bed sends no key presses, and the SIP serializer would
# put none on the gateway's wire anyway. (That sip_server builds a call so, and hands its
# half-duplex gate the call's own bed, is test_sip_call_lifecycle's: it needs a
# SEQPACKET socket, which macOS has not.)
#
# What the bed does on a call once built -- waiting for quiet, stopping when the caller
# talks, muting the caller through the half-duplex gate -- is test_thinking_sound.py's.
# How the typing sounds at 8 kHz, and whether its echo eats a caller's barge-in with the
# gateway's AEC on, needs a live call.
#
# Run: python test_thinking_sound_on_calls.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

os.environ.pop("TEAPORT_THINKING_WAV", None)   # live typing (a wav has no key presses)
for k, v in (("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime"),
             ("LLM_BASE_URL", "http://127.0.0.1:9/v1"), ("LLM_API_KEY", "not-a-real-key")):
    os.environ.setdefault(k, v)

from pipecat.frames.frames import (  # noqa: E402
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams  # noqa: E402

from teaport_brain import agent_session, settings  # noqa: E402
from teaport_brain import thinking_sound as ts  # noqa: E402
from teaport_brain.gateway_serializer import TeaportGatewaySerializer  # noqa: E402
from teaport_brain.sip_serializer import SipProtocolSerializer  # noqa: E402
from teaport_brain.talk_transport import TalkWebsocketTransport  # noqa: E402

FRONT_ENDS = ("sip", "talk", "room", None)
# (front-end, agent-first, half-duplex): every case. Only a call has a half-duplex gate,
# but the gate is asked the same for every front-end.
CASES = [(f, af, hd) for f in FRONT_ENDS for af in (False, True) for hd in (False, True)]
EVERYWHERE = dict.fromkeys(CASES, True)
NOWHERE = dict.fromkeys(CASES, False)


class _Switches:
    """thinking_sound's two switches, and agent_session's agent-first, as given."""

    def __init__(self, enabled, sip, agent_first=False):
        self.values = {(ts, "ENABLED"): enabled, (ts, "SIP_ENABLED"): sip,
                       (agent_session, "AGENT_FIRST"): agent_first}

    def __enter__(self):
        self.saved = {k: getattr(*k) for k in self.values}
        for (mod, name), v in self.values.items():
            setattr(mod, name, v)

    def __exit__(self, *exc):
        for (mod, name), v in self.saved.items():
            setattr(mod, name, v)


def _table():
    return {(f, af, hd): ts.enabled_for(f, af, hd) for f, af, hd in CASES}


def test_the_setting_is_a_flag_off_by_default_beside_the_kill_switch():
    row, shared = settings.ROWS["SIP_THINKING_SOUND"], settings.ROWS["TEAPORT_THINKING_SOUND"]
    assert (row["type"], row["default"], row["read"]) == ("flag", False, "startup"), row
    assert (row["group"], row["store"]) == (shared["group"], shared["store"])
    assert settings.setting("SIP_THINKING_SOUND", env={}) is False
    # The kill switch's help says what the new flag does, and the flag's says the switch
    # still turns it off.
    assert "SIP_THINKING_SOUND" in shared["help"], shared["help"]
    assert "TEAPORT_THINKING_SOUND=0" in row["help"], row["help"]
    assert "SIP_HALF_DUPLEX" in row["help"], row["help"]       # (and that it does nothing there)


def test_the_gate():
    with _Switches(enabled=True, sip=False):                 # the defaults
        assert _table() == {**EVERYWHERE, ("sip", False, False): False,
                            ("sip", False, True): False}     # agent-first types on
    with _Switches(enabled=True, sip=True):                  # no half-duplex call, though
        assert _table() == {**EVERYWHERE, ("sip", False, True): False}
    for sip in (False, True):                                # agent-first on a half-duplex
        with _Switches(enabled=True, sip=sip):               # call: as on main, typed
            assert ts.enabled_for("sip", True, True) is True
    for sip in (False, True):                                # the kill switch, calls included
        with _Switches(enabled=False, sip=sip):
            assert _table() == NOWHERE


# The switches are read at import, as the brain reads them when it starts: in a process
# of their own, the module is re-imported under each env in turn, neither switch carried
# over from the env before it (or from this shell).
_PROBE = (
    "import importlib, json, os, sys\n"
    "from teaport_brain import thinking_sound as ts\n"
    "out = []\n"
    "for env in json.loads(sys.argv[1]):\n"
    "    for k in ('TEAPORT_THINKING_SOUND', 'SIP_THINKING_SOUND'):\n"
    "        os.environ.pop(k, None)\n"
    "    os.environ.update(env)\n"
    "    importlib.reload(ts)\n"
    "    out.append([ts.enabled_for(f, af) for f in ('sip', 'talk', 'room') for af in (0, 1)])\n"
    "print(json.dumps(out))\n")


def test_the_switches_are_read_from_the_env_as_the_brain_starts():
    envs = [{}, {"SIP_THINKING_SOUND": "1"}, {"SIP_THINKING_SOUND": "On"},
            {"SIP_THINKING_SOUND": "off"}, {"SIP_THINKING_SOUND": "maybe"},   # a typo: off
            {"SIP_THINKING_SOUND": "yes", "TEAPORT_THINKING_SOUND": "0"},
            {"TEAPORT_THINKING_SOUND": "no"}]
    p = subprocess.run([sys.executable, "-c", _PROBE, json.dumps(envs)], env=dict(os.environ),
                       capture_output=True, text=True, check=True)
    got = json.loads(p.stdout.strip().splitlines()[-1])
    #        sip  sip+af  talk  talk+af  room  room+af
    off_on_calls = [False, True, True, True, True, True]
    on, nowhere = [True] * 6, [False] * 6
    assert got == [off_on_calls, on, on, off_on_calls, off_on_calls, nowhere, nowhere], got
    assert "SIP_THINKING_SOUND='maybe'" in p.stderr, p.stderr   # (warned, not silently off)


def _build(front_end, client_features=frozenset(), half_duplex=False):
    t = TalkWebsocketTransport(websocket=SimpleNamespace(), params=FastAPIWebsocketParams(
        serializer=TeaportGatewaySerializer()))
    return agent_session.build_agent_session(t, front_end=front_end,
                                             client_features=client_features,
                                             half_duplex=half_duplex)


def test_a_call_gets_the_bed_the_gate_says_and_types_for_no_face():
    # Built as sip_server builds a call: front_end "sip", no client features, and
    # whether the call has a half-duplex gate (test_sip_call_lifecycle's
    # test_a_call_is_built_for_no_face_...). Keys: (enabled, sip, agent-first, half-duplex).
    cases = ((True, False, False, False), (True, True, False, False),
             (True, False, True, False), (True, True, True, False),
             (True, True, False, True), (True, False, True, True), (True, True, True, True),
             (False, True, True, False))

    async def run():
        beds = {}
        for enabled, sip, agent_first, half_duplex in cases:
            with _Switches(enabled, sip, agent_first):
                beds[enabled, sip, agent_first, half_duplex] = _build(
                    "sip", half_duplex=half_duplex).thinking_sound
        with _Switches(enabled=True, sip=True):              # the room, for contrast
            room = _build("room", frozenset({"typing"})).thinking_sound
        return beds, room
    beds, room = asyncio.run(run())
    typed = {k for k, bed in beds.items() if bed is not None}
    assert typed == {(True, True, False, False),             # the flag, on a full-duplex call
                     (True, False, True, False), (True, True, True, False),   # agent-first,
                     (True, False, True, True), (True, True, True, True)}, typed  # half-duplex too
    for key in typed:
        bed = beds[key]
        assert isinstance(bed, ts.ThinkingSound), key
        assert bed._keys is False, key                       # no face types for a call
        assert bed._async is (not key[2]), key               # the routing is agent-first's alone
    assert room._keys is True                                # the room's face does type


def test_no_typing_message_leaves_a_call():
    # Were a call's bed ever told to type for a face, the serializer puts on the
    # gateway's wire only the control types its protocol knows.
    async def run():
        ser = SipProtocolSerializer()
        out = []
        for m in ({"type": "typing", "on": True, "first_key_ms": 1500},
                  {"type": "keys", "anchor": "audio", "keys": [["e", 3]]},
                  {"type": "typing", "on": False, "done": True}):
            for cls in (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame):
                out.append(await ser.serialize(cls(message=m)))
        return out
    assert asyncio.run(run()) == [None] * 6


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
