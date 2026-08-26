#
# Unit test: what a tool advertises has to be what it accepts.
#
# switch_voice validates its argument against ENGINE_VOICES and rejects anything else.
# The schema used to advertise three EXAMPLE ids instead of the valid set, which left the
# model to either guess the id or spend a turn on list_voices finding it — and both went
# wrong. Measured 2026-08-23 across three models and four ordinary requests, only 1 of 12
# reached switch_voice with a valid id first time: gemma-4-31b-it guessed 'nova', 'Liam'
# and 'en_gb_emma' (all rejected, so the user heard "I couldn't find a voice called
# Liam"), while gpt-oss-120b and qwen3.8-27b avoided guessing by calling list_voices
# first, costing a round trip before anything was spoken. With the enum in place it was
# 11 of 12, direct.
#
# The enum therefore has to keep matching the handler's table exactly. Advertise an id
# the engine will not accept and the model picks it confidently and fails; omit one and
# that voice silently becomes unreachable.
#
# Run: on the appliance only — see pinned_pipecat.py.
#
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from teaport_brain.engine_tts import ENGINE_VOICES  # noqa: E402
from teaport_brain.tools import build_tools_schema  # noqa: E402


def _voice_param():
    for tool in build_tools_schema().standard_tools:
        if tool.name == "switch_voice":
            return tool.properties["voice"]
    raise AssertionError("switch_voice is no longer advertised")


def test_the_advertised_voices_are_exactly_the_accepted_ones():
    advertised = set(_voice_param()["enum"])
    accepted = {v for voices in ENGINE_VOICES.values() for v in voices}
    assert advertised == accepted, (
        f"schema and handler disagree: only advertised {sorted(advertised - accepted)}, "
        f"only accepted {sorted(accepted - advertised)}"
    )


def test_the_examples_in_the_description_are_real_ids():
    """The description names af_nova and am_liam; if they are renamed it must follow."""
    description = _voice_param()["description"]
    accepted = {v for voices in ENGINE_VOICES.values() for v in voices}
    for quoted in ("af_nova", "am_liam"):
        if quoted in description:
            assert quoted in accepted, f"description cites {quoted}, which no longer exists"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and not asyncio.iscoroutinefunction(fn):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
