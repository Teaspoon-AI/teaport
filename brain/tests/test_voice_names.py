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

from teaport_brain import engine_tts  # noqa: E402
from teaport_brain.engine_tts import ENGINE_VOICES, LANG_NAMES, VOICE_NAMES  # noqa: E402
from teaport_brain import tools  # noqa: E402

# Every voice but the zm_* ones: the stable channel's model, which predates them.
OLD_MODEL = frozenset(v for vs in ENGINE_VOICES.values() for v in vs
                      if not v.startswith("zm_"))
ALL = frozenset(v for vs in ENGINE_VOICES.values() for v in vs)


class _Served:
    """engine_tts's cached engine answer set to `served` for the block (None: the
    engine could not be asked)."""

    def __init__(self, served):
        self.served = served

    def __enter__(self):
        self.saved = engine_tts._served_voices
        engine_tts._served_voices = self.served
        if self.served is None:
            # Not asked: a bare test box has no engine, and one that does must not
            # decide what this test sees.
            self.ask = engine_tts.served_voices
            engine_tts.served_voices = lambda timeout=1.0: None
        return self

    def __exit__(self, *exc):
        engine_tts._served_voices = self.saved
        if self.served is None:
            engine_tts.served_voices = self.ask


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
        lang = next(k for k, vs in ENGINE_VOICES.items() if voice in vs)
        return {"ok": True, "voice": voice, "language": lang,
                "language_name": LANG_NAMES[lang]}


def test_every_named_voice_exists():
    accepted = {v for vs in ENGINE_VOICES.values() for v in vs}
    assert set(VOICE_NAMES) <= accepted, sorted(set(VOICE_NAMES) - accepted)


def test_every_mandarin_voice_has_a_name():
    assert set(ENGINE_VOICES["cmn"]) <= set(VOICE_NAMES), \
        sorted(set(ENGINE_VOICES["cmn"]) - set(VOICE_NAMES))


def test_the_male_mandarin_voices_are_offered():
    assert {"zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang"} <= set(ENGINE_VOICES["cmn"])


def test_list_voices_hands_over_the_names():
    with _Served(ALL):
        p = _Params()
        asyncio.run(tools._list_voices(p, tts=_TTS()))
    assert p.result["voice_names"]["zm_yunxi"] == "云希"
    assert "Mandarin" in p.result["note"]


def test_switch_voice_names_a_named_voice():
    with _Served(ALL):
        p = _Params({"voice": "zm_yunxi"})
        asyncio.run(tools._switch_voice(p, tts=_TTS()))
    assert p.result["ok"] and "云希" in p.result["note"], p.result


def test_switch_voice_note_unchanged_for_an_unnamed_voice():
    with _Served(ALL):
        p = _Params({"voice": "af_nova"})
        asyncio.run(tools._switch_voice(p, tts=_TTS()))
    assert p.result["note"] == ("You now speak with af_nova. From now on reply only in "
                                "English (US)."), p.result


# An engine whose model lacks the zm_* voices (the stable channel's): they are not
# offered, listed, named or accepted, so a switch to one cannot leave the session mute.
def test_an_old_model_withholds_the_voices_it_lacks():
    with _Served(OLD_MODEL):
        assert "zm_yunxi" not in tools.offered_voices()["cmn"]
        p = _Params()
        asyncio.run(tools._list_voices(p, tts=_TTS()))
        assert "zm_yunxi" not in p.result["voices_by_language"]["Chinese (Mandarin)"]
        assert "zm_yunxi" not in p.result["voice_names"]
        assert p.result["voice_names"]["zf_xiaobei"] == "晓北"
        p = _Params({"voice": "zm_yunxi"})
        asyncio.run(tools._switch_voice(p, tts=_TTS()))
        assert p.result["ok"] is False, p.result
        enum = next(t for t in tools.build_tools_schema(tools.ToolContext(has_tts=True))
                    .standard_tools if t.name == "switch_voice").properties["voice"]["enum"]
        assert set(enum) == OLD_MODEL, sorted(set(enum) ^ OLD_MODEL)


def test_an_engine_that_cannot_be_asked_withholds_nothing():
    with _Served(None):
        assert tools.offered_voices() == ENGINE_VOICES
        enum = next(t for t in tools.build_tools_schema(tools.ToolContext(has_tts=True))
                    .standard_tools if t.name == "switch_voice").properties["voice"]["enum"]
        assert set(enum) == ALL


def test_a_language_the_model_lacks_entirely_is_dropped():
    assert "cmn" not in engine_tts.offered_voices(frozenset({"af_heart"}))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
