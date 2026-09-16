#
# The config UI's JSON routes, against a temp /etc/teaport and a stubbed sudo.
#
# What has to hold:
#   - An env file round-trips: comments, blank lines and keys the schema does not
#     know stay where they were; a changed key is rewritten in place; None
#     removes it; a new key is appended; values with spaces/quotes are quoted the
#     way systemd reads them back.
#   - GET never returns a secret, even one sitting in the env file, and says
#     whether each is set.
#   - PUT validates per row (type, bounds, enum, json, installer tier) and
#     refuses the cross-row constraints unless forced; then writes through the
#     privileged hook with the whole new file, and secrets to their own file.
#   - Every route 401s without the token when GATEWAY_TOKEN is set.
#
# Run: python test_config_ui.py   (or via pytest)
#
import asyncio
import json
import os
import sys
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from teaport_brain import config_ui

BRAIN_ENV = """# hand-written comment
BRAIN_PORT=7861
LLM_BASE_URL=https://openrouter.ai/api/v1

ENDPOINT_STOP_SECS=0.2
LLM_EXTRA_BODY={"provider":{"order":["Groq"]}}
LLM_API_KEY=sk-should-never-leak
OPERATOR_ONLY=keep me
"""


def setup():
    etc = tempfile.mkdtemp(prefix="teaport-etc-")
    home = tempfile.mkdtemp(prefix="teaport-home-")
    with open(os.path.join(etc, "brain.env"), "w") as f:
        f.write(BRAIN_ENV)
    with open(os.path.join(etc, "engine.env"), "w") as f:
        f.write("KOKORO_RESERVE_FPT=12\n")
    config_ui.ETC_DIR = etc
    config_ui.SIP_CONF = os.path.join(home, "teaport-sip.conf")
    os.environ["HOME"] = home
    os.environ["GATEWAY_TOKEN"] = "t0k3n"
    os.environ.pop("LLM_API_KEY", None)

    calls = []

    async def fake_privileged(action, target, stdin=None):
        calls.append((action, target, stdin))
        if action == "write":
            with open(os.path.join(etc, target), "w") as f:
                f.write(stdin)

    config_ui._privileged = fake_privileged

    async def fake_states(units):
        return {u: {"state": "active", "active_since": 0.0} for u in units}

    config_ui._unit_states = fake_states

    # The secret file the schema names for LLM_API_KEY, relative to $HOME.
    rows = config_ui._rows()
    rows["LLM_API_KEY"]["file"] = os.path.join(home, "llm_key")
    rows["OPENCLAW_GATEWAY_TOKEN"]["file"] = os.path.join(home, "openclaw_token")

    app = FastAPI()
    app.include_router(config_ui.router)
    return TestClient(app), etc, home, calls


H = {"authorization": "Bearer t0k3n"}


def test_env_roundtrip():
    text = BRAIN_ENV
    out = config_ui.render_env(text, {
        "ENDPOINT_STOP_SECS": "0.5", "LLM_BASE_URL": None, "TTS_VOICE": "af_bella",
        "TEAPORT_LLM_GUARD_RECOVERY": "Let me say that again, plainly.",
    })
    assert "# hand-written comment\n" in out
    assert "OPERATOR_ONLY=keep me\n" in out, out
    assert "ENDPOINT_STOP_SECS=0.5\n" in out and "ENDPOINT_STOP_SECS=0.2" not in out
    assert "LLM_BASE_URL" not in out
    assert out.endswith('TTS_VOICE=af_bella\nTEAPORT_LLM_GUARD_RECOVERY="Let me say that again, plainly."\n'), out
    # the order of untouched lines is preserved
    assert out.index("BRAIN_PORT") < out.index("ENDPOINT_STOP_SECS") < out.index("OPERATOR_ONLY")
    parsed = config_ui.parse_env(out)
    assert parsed["TEAPORT_LLM_GUARD_RECOVERY"] == "Let me say that again, plainly."
    assert parsed["LLM_EXTRA_BODY"] == '{"provider":{"order":["Groq"]}}'
    # quoting: a value with a quote inside survives a round trip
    q = config_ui.quote('say "hi" # not a comment')
    assert config_ui.unquote(q) == 'say "hi" # not a comment', q
    assert config_ui.quote("plain") == "plain"
    # duplicate keys collapse to one
    dup = config_ui.render_env("A=1\nA=2\nB=3\n", {"A": "9"})
    assert dup == "A=9\nB=3\n", dup


def test_get_masks_secrets_and_requires_token():
    client, etc, home, calls = setup()
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.put("/api/config", json={}).status_code == 401
    assert client.post("/api/restart", json={}).status_code == 401
    r = client.get("/api/config", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "sk-should-never-leak" not in r.text
    assert "LLM_API_KEY" not in body["values"]["brain_env"]
    assert body["values"]["brain_env"]["ENDPOINT_STOP_SECS"] == "0.2"
    assert body["values"]["brain_env"]["OPERATOR_ONLY"] == "keep me"
    assert body["secrets"]["LLM_API_KEY"] is False  # env unset, file absent
    assert body["auth"] is True
    assert body["services"]["teaport-brain"] == "active"
    assert body["pending"] == {"teaport-brain": ["brain_env"], "teaport-sip-brain": ["brain_env"],
                               "teaport-engine": ["engine_env"]}, body["pending"]
    assert config_ui.parse_env(open(os.path.join(etc, "brain.env")).read())["OPERATOR_ONLY"] == "keep me"
    # ?token= works too (what the page uses on first open)
    assert client.get("/api/config?token=t0k3n").status_code == 200
    assert client.get("/config", headers=H).headers["content-type"].startswith("text/html")


def test_put_validation():
    client, etc, home, calls = setup()
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {
        "ENDPOINT_STOP_SECS": "abc",          # not a number
        "TTS_CLAUSE_GROWTH": "2.5",           # above max
        "VAD_SAMPLE_RATE": "22050",           # not in enum
        "LLM_EXTRA_BODY": "[1,2]",            # json but not an object
        "TEAPORT_LLM_TEXT_GUARD": "maybe",    # not a flag word
        "GATEWAY_TOKEN": "x",                 # installer tier
        "KOKORO_RESERVE_FPT": "6",            # wrong store
        "LLM_API_KEY": None,                  # secrets cannot be cleared
        "LLM_BASE_URL": "ftp://nope",         # wrong scheme
    }})
    assert r.status_code == 400, r.text
    errs = r.json()["errors"]
    assert set(errs) == {"ENDPOINT_STOP_SECS", "TTS_CLAUSE_GROWTH", "VAD_SAMPLE_RATE", "LLM_EXTRA_BODY",
                         "TEAPORT_LLM_TEXT_GUARD", "GATEWAY_TOKEN", "KOKORO_RESERVE_FPT", "LLM_API_KEY",
                         "LLM_BASE_URL"}, errs
    assert "installer" in errs["GATEWAY_TOKEN"]
    assert calls == []
    # constraints: ask_openclaw must exceed ack + native
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {
        "TEAPORT_ASK_OPENCLAW_TIMEOUT": "30"}})
    assert r.status_code == 400 and r.json()["constraints"], r.text
    assert calls == []
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {
        "TEAPORT_ASK_OPENCLAW_TIMEOUT": "30"}, "force": True})
    assert r.status_code == 200 and r.json()["warnings"], r.text
    assert calls[-1][0:2] == ("write", "brain.env")
    # LEDGER_TRACE only takes "1"
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {"LEDGER_TRACE": "true"}})
    assert r.status_code == 400 and "LEDGER_TRACE" in r.json()["errors"]
    # an explicit empty is allowed only where the code distinguishes it
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {"LLM_REASONING_EFFORT": ""}})
    assert r.status_code == 200, r.text
    assert 'LLM_REASONING_EFFORT=""\n' in open(os.path.join(etc, "brain.env")).read()
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {"TTS_VOICE": ""}})
    assert r.status_code == 400 and "TTS_VOICE" in r.json()["errors"]


def test_put_writes_env_and_secrets():
    client, etc, home, calls = setup()
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {
        "ENDPOINT_STOP_SECS": "0.5", "LLM_BASE_URL": None, "TTS_VOICE": "af_bella",
        "LLM_API_KEY": "sk-new-key", "TEAPORT_LLM_TEXT_GUARD": "off",
    }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written"] == ["ENDPOINT_STOP_SECS", "LLM_BASE_URL", "TEAPORT_LLM_TEXT_GUARD", "TTS_VOICE"]
    assert body["secrets"] == ["LLM_API_KEY"]
    assert len(calls) == 1 and calls[0][:2] == ("write", "brain.env")
    new = open(os.path.join(etc, "brain.env")).read()
    assert "# hand-written comment" in new and "OPERATOR_ONLY=keep me" in new
    assert "LLM_BASE_URL" not in new
    assert "LLM_API_KEY=sk-should-never-leak" in new  # untouched: the env copy is not ours to edit
    parsed = config_ui.parse_env(new)
    assert parsed["ENDPOINT_STOP_SECS"] == "0.5" and parsed["TTS_VOICE"] == "af_bella"
    assert parsed["TEAPORT_LLM_TEXT_GUARD"] == "off"
    key_path = os.path.join(home, "llm_key")
    assert open(key_path).read() == "sk-new-key\n"
    assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"
    assert "sk-new-key" not in new
    # now GET reports the secret as set, still without the value
    r = client.get("/api/config", headers=H)
    assert r.json()["secrets"]["LLM_API_KEY"] is True and "sk-new-key" not in r.text
    # no-op write (same values) does not call sudo
    r = client.put("/api/config", headers=H, json={"store": "brain_env", "values": {"TTS_VOICE": "af_bella"}})
    assert r.status_code == 200 and len(calls) == 1
    # restart
    r = client.post("/api/restart", headers=H, json={"unit": "teaport-brain"})
    assert r.status_code == 200 and calls[-1] == ("restart", "teaport-brain", None)
    r = client.post("/api/restart", headers=H, json={"unit": "sshd"})
    assert r.status_code == 400


def test_apply_helper_rejects_junk():
    from teaport_brain import config_apply
    for bad in ("rm -rf /\n", "A=1\n$(reboot)\n"):
        try:
            config_apply.write("brain.env", bad)
        except SystemExit as e:
            assert "not KEY=value" in str(e), e
        else:
            raise AssertionError(f"accepted {bad!r}")
    for name in ("../shadow", "sudoers", "brain.env.bak"):
        try:
            config_apply.write(name, "A=1\n")
        except SystemExit:
            pass
        else:
            raise AssertionError(f"accepted file {name!r}")
    try:
        config_apply.restart("sshd")
    except SystemExit:
        pass
    else:
        raise AssertionError("accepted unit sshd")


def main() -> int:
    for fn in (test_env_roundtrip, test_get_masks_secrets_and_requires_token, test_put_validation,
               test_put_writes_env_and_secrets, test_apply_helper_rejects_junk):
        fn()
        print("ok", fn.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
