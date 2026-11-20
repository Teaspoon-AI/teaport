"""Display names for voices whose ids would be read out as letters (teaport#415).

list_voices hands the model VOICE_NAMES and tells it to say the name; switch_voice's note
names the voice by it. A name for an id the engine does not have would be offered for a
voice switch_voice then refuses, so every named id must be an accepted one, and every
Mandarin voice must have a name (an unnamed one is spoken as its id in a Mandarin reply).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teaport_brain.engine_tts import ENGINE_VOICES, VOICE_NAMES  # noqa: E402
from teaport_brain import tools  # noqa: E402


class _Params:
    def __init__(self, arguments=None):
        self.arguments = arguments or {}
        self.result = None

    async def result_callback(self, result):
        self.result = result


class _TTS:
    _voice = "zf_xiaoyi"
    espeak_language = "cmn"

    def set_voice(self, voice):
        return {"ok": True, "voice": voice, "language": "cmn",
                "language_name": "Chinese (Mandarin)"}


def test_every_named_voice_exists():
    accepted = {v for vs in ENGINE_VOICES.values() for v in vs}
    assert set(VOICE_NAMES) <= accepted, sorted(set(VOICE_NAMES) - accepted)


def test_every_mandarin_voice_has_a_name():
    assert set(ENGINE_VOICES["cmn"]) <= set(VOICE_NAMES), \
        sorted(set(ENGINE_VOICES["cmn"]) - set(VOICE_NAMES))


def test_the_male_mandarin_voices_are_offered():
    assert {"zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang"} <= set(ENGINE_VOICES["cmn"])


def test_list_voices_hands_over_the_names():
    p = _Params()
    asyncio.run(tools._list_voices(p, tts=_TTS()))
    assert p.result["voice_names"]["zm_yunxi"] == "云希"
    assert "never the id" in p.result["note"]


def test_switch_voice_names_a_named_voice():
    p = _Params({"voice": "zm_yunxi"})
    asyncio.run(tools._switch_voice(p, tts=_TTS()))
    assert p.result["ok"] and "云希" in p.result["note"], p.result


def test_switch_voice_note_unchanged_for_an_unnamed_voice():
    p = _Params({"voice": "af_nova"})
    asyncio.run(tools._switch_voice(p, tts=_TTS()))
    assert p.result["note"].startswith("You now speak with af_nova. "), p.result


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
