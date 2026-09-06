#
# Unit test: two voice-overlay rules the 2026-09-04 runbook proved necessary.
#
# Live S9 (21:10): the user read "as soon as the ack ends, start" aloud while an
# ask_openclaw consult was pending, and the model FABRICATED the answer -- "The top
# five Hacker News stories right now are: 'OpenAI launches new model GPT-4 Turbo' –
# https://news.ycombinator.com/item?id=12345678" -- three seconds before the real one
# arrived. Six words of it were spoken before the next fragment cut it.
#
# Live S8/S9 (21:05, 21:10): consult deliveries read web addresses aloud ("at nvd dot
# nist dot gov slash vuln slash …"), which is where a 30-second single sentence comes
# from. The rule names sources instead; the delivery text has the addresses removed
# before the model sees them (test_followup_injection).
#
# The prompt is tuned text that must not be reworded casually; these tests pin only
# that the two rules are present in the composed system prompt, so a rewrite that
# drops one fails here rather than in a room.
#
# Run: python test_persona_rules.py   (or via the suite)
#

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import teaport_brain  # noqa: E402, F401
from teaport_brain.persona import VOICE_OVERLAY, build_system_prompt  # noqa: E402


def test_a_pending_consult_answer_must_not_be_guessed():
    prompt = build_system_prompt("You are a test persona.")
    assert "never guess or make one up" in prompt, "the pending-consult rule is missing"
    assert "still in progress" in prompt
    assert "'start'" in prompt, "the live trigger word is named so the rule covers it"


def test_web_addresses_are_never_read_aloud():
    assert "Never read out a web address or URL" in VOICE_OVERLAY
    assert "name the source instead" in VOICE_OVERLAY


def test_the_overlay_still_follows_the_persona():
    prompt = build_system_prompt("IDENTITY LINE")
    assert prompt.startswith("IDENTITY LINE\n\n")
    assert prompt.endswith(VOICE_OVERLAY)


def main():
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print(f"  ok {k}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
