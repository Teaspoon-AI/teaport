#
# settings.setting() reads a knob through its config_schema.toml row: the row's type
# parses it, the row's default stands in when it is unset or unreadable, and nothing
# a hand-edited brain.env holds can raise out of an import.
# settings.parse() is also what the config UI validates with, so the page and the
# brain agree on what a value is.
#
# Run: python test_settings.py   (or via pytest test_suite.py)
#
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pipecat.utils.env import InvalidEnvVarValueError, env_truthy  # noqa: E402

from teaport_brain import settings  # noqa: E402
from teaport_brain.settings import ROWS, default_of, parse, setting  # noqa: E402


def test_unset_or_empty_is_the_schema_default():
    assert setting("TEAPORT_FOLLOWUP_QUIET_S", env={}) == 0.7
    assert setting("TEAPORT_FOLLOWUP_QUIET_S", env={"TEAPORT_FOLLOWUP_QUIET_S": "  "}) == 0.7
    assert setting("GATEWAY_PORT", env={}) == 7861


def test_a_set_value_is_parsed_by_the_row_type():
    assert setting("TEAPORT_FOLLOWUP_QUIET_S", env={"TEAPORT_FOLLOWUP_QUIET_S": " 1.25 "}) == 1.25
    assert setting("GATEWAY_PORT", env={"GATEWAY_PORT": "7999"}) == 7999
    assert setting("VAD_SAMPLE_RATE", env={"VAD_SAMPLE_RATE": "8000"}) == "8000"
    assert setting("HEARD_MODE", env={"HEARD_MODE": "NOTE"}) == "note"


def test_a_whole_number_default_of_a_float_row_is_a_float():
    # TOML writes `default = 60` for a float row as an int; every other read is a float.
    v = setting("TEAPORT_FOLLOWUP_MAX_WAIT_S", env={})
    assert v == 60.0 and isinstance(v, float)


def test_an_unreadable_value_falls_back_instead_of_raising():
    # The exact typos a bare float()/int() at import turned into a crash loop.
    for name, raw in (("TEAPORT_FOLLOWUP_QUIET_S", "0,7"), ("TEAPORT_FOLLOWUP_QUIET_S", "off"),
                      ("TEAPORT_FOLLOWUP_QUIET_S", "nan"), ("GATEWAY_PORT", "7861.0"),
                      ("VAD_SAMPLE_RATE", "22050")):
        assert setting(name, env={name: raw}) == default_of(ROWS[name]), (name, raw)


def test_a_code_side_default_overrides_the_schema():
    assert setting("TEAPORT_FOLLOWUP_QUIET_S", default=2.0, env={}) == 2.0
    assert setting("TEAPORT_FOLLOWUP_QUIET_S", default=2.0, env={"TEAPORT_FOLLOWUP_QUIET_S": "x"}) == 2.0


def test_a_name_without_a_row_fails_loudly():
    try:
        setting("TEAPORT_NO_SUCH_KNOB", env={})
    except KeyError:
        return
    raise AssertionError("an unschema'd read must not pass silently")


def test_every_schema_default_is_a_value_or_a_description():
    # A default in angle brackets describes one the code computes; its read passes
    # default=. Any other default must parse as its own row's type.
    for name, row in ROWS.items():
        if "<" in str(row.get("default", "")):
            continue
        default_of(row)  # raises on a default that is not a value


def test_a_described_default_is_never_returned_as_a_value():
    # These parse as their types (a path, a URL), so only the description rule stops a
    # read that forgot default= from dialing "ws://127.0.0.1:<BRAIN_PORT>/talk".
    for name in ("TEAPORT_THINKING_WAV", "LOCAL_AUDIO_URL", "ENGINE_TTS_STREAM_URL"):
        try:
            setting(name, env={})
        except ValueError as e:
            assert "must pass default=" in str(e), e
        else:
            raise AssertionError(f"{name}: its described default was returned as a value")
        assert setting(name, default="x", env={}) == "x"


def test_flags_read_exactly_as_pipecat_reads_them():
    # One truth table for the whole brain.env: setting() and pipecat's env_truthy (its
    # PIPECAT_* flags live in the same file) must agree on every word either one knows.
    key = "TEAPORT_SETTINGS_TEST_FLAG"
    row = {"name": key, "type": "flag"}
    words = settings.FLAG_TRUE | settings.FLAG_FALSE | {"maybe", "2", "enabled"}
    try:
        for raw in sorted(words) + ["TRUE", " On "]:
            os.environ[key] = raw
            try:
                want = env_truthy(key)
            except InvalidEnvVarValueError:
                want = None
            assert parse(row, raw.strip())[0] is want, raw
    finally:
        os.environ.pop(key, None)


def test_an_accepts_flag_reads_an_off_word_as_off_without_a_warning():
    # LEDGER_TRACE: only 1 turns it on, but 0 is the natural way to write off, so it is
    # not "unreadable". true/yes/on still warn: those are the ones a reader expects on.
    row = {"type": "flag", "accepts": ["1"]}
    assert parse(row, "0") == (False, None)
    assert parse(row, "off") == (False, None)
    assert parse(row, "true")[1] is not None


def test_a_bad_json_value_is_not_echoed_to_the_journal():
    seen = []
    sink = settings.logger.add(seen.append, level="WARNING")
    try:
        assert setting("LLM_EXTRA_BODY", env={"LLM_EXTRA_BODY": '{"api_key": "sk-SECRET",'}) is None
    finally:
        settings.logger.remove(sink)
    assert seen and all("sk-SECRET" not in str(m) for m in seen), seen
    assert "not valid JSON" in str(seen[0])


def test_parse_judges_text_by_type():
    ok = [({"type": "int"}, "-3", -3), ({"type": "float"}, "1e-3", 0.001),
          ({"type": "flag"}, "Yes", True), ({"type": "flag"}, "off", False),
          ({"type": "flag", "accepts": ["1"]}, "1", True),
          ({"type": "enum", "values": ["a", ""]}, "A", "a"),
          ({"type": "json"}, '{"a": 1}', {"a": 1}),
          ({"type": "url"}, "https://x", "https://x"), ({"type": "ws_url"}, "ws://x", "ws://x"),
          ({"type": "snowflake"}, "123", "123"), ({"type": "string"}, "anything", "anything")]
    for row, text, want in ok:
        assert parse(row, text) == (want, None), (row, text)
    bad = [({"type": "int"}, "2.5"), ({"type": "float"}, "inf"), ({"type": "flag"}, "maybe"),
           ({"type": "flag", "accepts": ["1"]}, "true"), ({"type": "enum", "values": ["a"]}, "b"),
           ({"type": "json"}, "[1]"), ({"type": "json"}, "{nope"), ({"type": "url"}, "ftp://x"),
           ({"type": "ws_url"}, "http://x"), ({"type": "snowflake"}, "12a")]
    for row, text in bad:
        value, problem = parse(row, text)
        assert value is None and problem, (row, text)


def test_bounds_are_a_separate_question():
    row = {"type": "float", "min": 0, "max": 1}
    assert settings.out_of_bounds(row, 0.5) is None
    assert "at least" in settings.out_of_bounds(row, -0.1)
    assert "at most" in settings.out_of_bounds(row, 1.5)
    # setting() reads an out-of-range value as written: runtime bounds are not enforced yet.
    assert setting("VAD_CONFIDENCE", env={"VAD_CONFIDENCE": "1.5"}) == 1.5


def test_an_enum_whose_values_include_empty_reads_a_set_empty_value_as_itself():
    # LLM_REASONING_EFFORT="" means "send no effort", and the config UI writes it.
    assert setting("LLM_REASONING_EFFORT", env={"LLM_REASONING_EFFORT": ""}) == ""
    assert setting("LLM_REASONING_EFFORT", env={}) == "low"
    # Empty is still "not set" for an enum without "" among its values.
    assert setting("HEARD_MODE", env={"HEARD_MODE": ""}) == "truncate"


def test_a_row_with_an_empty_field_reads_a_set_empty_value_as_itself():
    # TEAPORT_SIP_SOCKET="" turns the SIP front-end off (#127); unset is the default socket.
    from teaport_brain.sip_transport import DEFAULT_UDS_PATH
    assert setting("TEAPORT_SIP_SOCKET", env={}) == DEFAULT_UDS_PATH
    assert setting("TEAPORT_SIP_SOCKET", env={"TEAPORT_SIP_SOCKET": ""}) == ""
    assert setting("TEAPORT_SIP_SOCKET", env={"TEAPORT_SIP_SOCKET": "  "}) == ""
    assert setting("TEAPORT_SIP_SOCKET", env={"TEAPORT_SIP_SOCKET": "/tmp/x.sock"}) == "/tmp/x.sock"
    assert settings.empty_is_value(settings.ROWS["TEAPORT_SIP_SOCKET"])
    assert settings.empty_is_value(settings.ROWS["LLM_REASONING_EFFORT"])
    assert not settings.empty_is_value(settings.ROWS["TTS_VOICE"])


def test_normalize_is_the_text_a_value_reads_as():
    # The config page writes exactly this text (#136), so it reads back unchanged.
    sock, voice, key = ROWS["TEAPORT_SIP_SOCKET"], ROWS["TTS_VOICE"], ROWS["GATEWAY_TOKEN"]
    assert settings.normalize(voice, None) is None
    assert settings.normalize(voice, " \t ") is None      # blank is "not set" ...
    assert settings.normalize(sock, " \t ") == ""         # ... or "" where empty is a value
    assert settings.normalize(sock, None) is None
    assert settings.normalize(voice, " af_bella\n") == "af_bella"
    assert settings.normalize(key, " two words ") == "two words"  # a secret too
    assert settings.normalize(key, "   ") is None
    # parse() takes normalized text: its patterns match whole, not up to a newline.
    assert parse(ROWS["TEAPORT_URL"], "ws://x\n")[1] is not None


def test_paths_expand_the_home_directory():
    home = os.path.expanduser("~")
    assert setting("TEAPORT_PERSONA_FILE", env={}) == os.path.join(home, ".config/teaport/persona.md")
    assert setting("ENGINE_LOG", env={"ENGINE_LOG": "~/x.log"}) == os.path.join(home, "x.log")


def test_a_secret_is_read_by_the_same_rule_from_its_env_var_or_its_file():
    # Every reader of a secret strips it (wifi_setup.setup_password, the OpenClaw token,
    # the Discord bridge, the SIP gateway's conf parser, /talk's bearer header), and the
    # config page writes its file stripped: the env copy reads the same as the file.
    from tempdirs import tempdir
    assert setting("WIFI_SETUP_PASSWORD", env={"WIFI_SETUP_PASSWORD": " two words "}) == "two words"
    assert setting("GATEWAY_TOKEN", env={"GATEWAY_TOKEN": "   "}) == ""
    assert setting("LLM_API_KEY", env={}) is None
    row = ROWS["LLM_API_KEY"]
    home, old = tempdir("teaport-secret-"), row["file"]
    row["file"] = os.path.join(home, "llm_key")
    try:
        assert settings.secret_file("LLM_API_KEY") is None  # no file
        for text, want in (("\tsk-x \n", "sk-x"), ("  \n", None)):
            with open(row["file"], "w") as f:
                f.write(text)
            assert settings.secret_file("LLM_API_KEY") == want, text
            assert setting("LLM_API_KEY", env={"LLM_API_KEY": text}) == want, text
        with open(row["file"], "wb") as f:  # not UTF-8, and then a directory: unreadable
            f.write(b"\xffsk-x\n")             # as root too; warned, not raised
        assert settings.secret_file("LLM_API_KEY") is None
        os.remove(row["file"])
        os.mkdir(row["file"])
        assert settings.secret_file("LLM_API_KEY") is None
    finally:
        row["file"] = old
    assert settings.secret_file("GATEWAY_TOKEN") is None  # a secret with no file


def test_whole_numbers_are_ascii_digits():
    # "\u0665" (Arabic-Indic five) passed Python's \d and int(); atoi, the bridge's
    # Number() and the engine read it as no number at all.
    assert setting("GATEWAY_PORT", env={"GATEWAY_PORT": "\u0667\u0668\u0666\u0661"}) == 7861
    assert parse({"type": "snowflake"}, "\u0661\u0662")[1] is not None


def test_canonical_is_the_brains_reading_in_one_spelling():
    # What the config page shows and writes (#136 follow-up): the file's spelling is not
    # what the brain reads, so a page that matched on it showed TRUNCATE as no option and
    # yes as off.
    cases = [("HEARD_MODE", "TRUNCATE", "truncate"), ("HEARD_MODE", " Note ", "note"),
             ("TEAPORT_LLM_TEXT_GUARD", "yes", "1"), ("TEAPORT_LLM_TEXT_GUARD", "On", "1"),
             ("TEAPORT_LLM_TEXT_GUARD", "NO", "0"), ("LEDGER_TRACE", "1", "1"),
             ("LEDGER_TRACE", "off", "0"), ("GATEWAY_PORT", "+07999", "7999"),
             ("GATEWAY_PORT", "-0", "0"), ("ENDPOINT_STOP_SECS", "0.50", "0.5"),
             ("ENDPOINT_STOP_SECS", "1.0", "1"), ("ENDPOINT_STOP_SECS", "1_000", "1000"),
             ("ENDPOINT_STOP_SECS", "1e-7", "1e-07"), ("ENDPOINT_STOP_SECS", "-0.0", "-0"),
             ("LLM_EXTRA_BODY", '{ "a": [1, 2] }', '{"a":[1,2]}'),
             ("LLM_EXTRA_BODY", '{"s": "\u2028"}', '{"s":"\\u2028"}'),
             ("TTS_VOICE", " af_bella ", "af_bella"), ("TEAPORT_PERSONA_FILE", "~/p.md", "~/p.md"),
             ("LLM_REASONING_EFFORT", "  ", ""), ("TEAPORT_SIP_SOCKET", "", "")]
    for name, given, want in cases:
        assert settings.canonical(ROWS[name], given) == (want, None), (name, given)
    assert settings.canonical(ROWS["TTS_VOICE"], "  ") == (None, None)  # not set
    assert settings.canonical(ROWS["TTS_VOICE"], None) == (None, None)
    for name, given in (("HEARD_MODE", "loud"), ("GATEWAY_PORT", "7861.0"),
                        ("TEAPORT_LLM_TEXT_GUARD", "maybe"), ("LEDGER_TRACE", "true")):
        text, why = settings.canonical(ROWS[name], given)
        assert text is None and why == parse(ROWS[name], given)[1], (name, given)
    # The one spelling reads back as the same value, and is its own spelling, for every row.
    samples = ["1", "0", "-3", "+5", " 0.25 ", "1e3", "yes", "OFF", "low", "NOTE", "truncate",
               "8000", '{"k": 1}', "ws://h:1/x", "https://h", "123", "~/x", "text", ""]
    for row in ROWS.values():
        for given in samples:
            text, why = settings.canonical(row, given)
            if not text:
                continue
            assert parse(row, text)[0] == parse(row, settings.normalize(row, given))[0], (row["name"], given)
            assert settings.canonical(row, text) == (text, None), (row["name"], given, text)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
