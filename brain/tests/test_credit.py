#
# The starter key's credit (#280): which key is the starter, what its record says, when
# the owner is told, and the stop when it is spent.
#
# What has to hold:
#   - The starter key is the one whose SHA-256 is in ~/.local/state/teaport/
#     starter_key_sha256; any other key is the owner's, and nothing is read or said.
#   - OpenRouter's key record is read in both shapes ("data" or the top level), with
#     limit minus usage when limit_remaining is missing; half used, $1 left and nothing
#     left are "half", "low" and "empty" (the thresholds are settings rows); a record
#     with no limit says nothing ("unknown").
#   - A read that fails is never alarming: the last known state stays, not fresh.
#   - Each warning is said once at the start of a conversation, again at most once a
#     day while it lasts (across restarts), never where "open my settings" is not heard,
#     and never for "empty", which stops the box instead: services.model_missing says
#     why, the no-model line is the trial-credit one, and a key the owner saves ends it.
#   - A refusal on the starter key with little left says the trial credit ran out.
#   - The notice is said first: before a Talk or room greeting, and when a wake starts a
#     new conversation (not one it continues).
#   - /health's "credit", `teaport status` and `teaport doctor`, and the Settings line.
#
# Synthetic keys and amounts only; no test talks to a provider.
#
# Run: python test_credit.py   (or via pytest)
#
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import ErrorFrame, LLMRunFrame, TTSSpeakFrame  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.utils.errors import ErrorCategory  # noqa: E402

import appliance  # noqa: E402
from tempdirs import tempdir  # noqa: E402
from teaport_brain import ai_account, credit, i18n, llm_error_speaker, services  # noqa: E402
from teaport_brain.wake_gate import MicConversation, WakeGate  # noqa: E402
# The in-process session harness (it sets its own defaults, which _box() replaces).
from test_post_tool_under_speech import TalkTransport  # noqa: E402
from teaport_brain.agent_session import build_agent_session  # noqa: E402

STARTER = "sk-or-v1-starter-0000000000000000000000000000000000000000000000000000"
OWN = "sk-or-v1-owners-1111111111111111111111111111111111111111111111111111111"
BASE = "https://openrouter.ai/api/v1"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Fetch:
    """ai_account.check_key, faked: the record to answer with, or None for no answer."""

    def __init__(self, record=None):
        self.record, self.calls = record, 0

    def __call__(self, key, base_url, timeout=None):
        self.calls += 1
        if self.record is None:
            return ai_account.Check(ai_account.UNKNOWN, ai_account.UNREACHABLE)
        return ai_account.Check(ai_account.WORKS, data=ai_account.key_record(dict(self.record)))


def _box(key=STARTER, marker=True):
    """A HOME with the key in use and (marker) the starter's marker."""
    home = tempdir("credit-home-")
    os.environ["HOME"] = home
    os.environ["LLM_BASE_URL"] = BASE
    os.environ.pop("LLM_API_KEY", None)
    if key:
        ai_account.store_key(key)
    if marker:
        os.makedirs(os.path.join(home, ".local", "state", "teaport"), exist_ok=True)
        with open(os.path.join(home, ".local", "state", "teaport", credit.MARKER), "w") as f:
            f.write(hashlib.sha256(STARTER.encode()).hexdigest() + "\n")
    return home


def _monitor(record, clock=None):
    fetch = Fetch(record)
    m = credit.Monitor(clock=clock or Clock(), fetch=fetch)
    return m, fetch


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the record

def test_both_record_shapes_and_the_thresholds():
    for wrap in (lambda r: {"data": r}, lambda r: r):
        assert credit.judge(ai_account.key_record(wrap({"limit": 5, "limit_remaining": 4.0, "usage": 1.0})))[0] == credit.OK
    j = lambda **r: credit.judge(r, half=0.5, low=1.0)[0]  # noqa: E731
    assert j(limit=5, limit_remaining=2.5, usage=2.5) == credit.HALF
    assert j(limit=5, limit_remaining=2.6, usage=2.4) == credit.OK
    assert j(limit=5, limit_remaining=1.0, usage=4.0) == credit.LOW
    assert j(limit=5, limit_remaining=0, usage=5) == credit.EMPTY
    assert j(limit=5, usage=4.5) == credit.LOW          # limit_remaining missing: limit - usage
    assert j(limit=5, usage=7) == credit.EMPTY
    assert j(limit=None, limit_remaining=None, usage=3) == credit.UNKNOWN  # no limit: nothing to judge
    assert j(limit="5", limit_remaining="1") == credit.UNKNOWN            # not numbers
    # The rows' defaults: half, and a dollar.
    assert (credit.HALF_USED, credit.LOW_USD) == (0.5, 1.0)


def test_only_the_marked_key_is_the_starter():
    _box(key=OWN)
    m, fetch = _monitor({"limit": 5, "limit_remaining": 0.5, "usage": 4.5})
    _run(m.refresh())
    assert fetch.calls == 0 and m.status() == {"state": credit.OWN}
    _box(key=STARTER, marker=False)
    _run(m.refresh())
    assert fetch.calls == 0 and m.state() == credit.OWN
    _box(key=None)
    assert m.status() == {"state": credit.NONE}
    _box()
    assert m.status() == {"state": credit.UNKNOWN, "fresh": False}  # not read yet
    _run(m.refresh())
    s = m.status()
    assert fetch.calls == 1 and s["state"] == credit.LOW and s["remaining"] == 0.5 and s["fresh"]
    os.environ["LLM_BASE_URL"] = "https://api.example.invalid/v1"  # not OpenRouter: not asked
    _run(m.refresh())
    assert fetch.calls == 1


def test_a_failed_read_keeps_what_was_known():
    _box()
    m, fetch = _monitor({"limit": 5, "limit_remaining": 2.0, "usage": 3.0})
    _run(m.refresh())
    assert m.state() == credit.HALF
    fetch.record = None
    _run(m.refresh())
    s = m.status()
    assert s["state"] == credit.HALF and s["fresh"] is False and s["remaining"] == 2.0
    fresh, _ = _monitor(None)
    _run(fresh.refresh())
    assert fresh.state() == credit.UNKNOWN  # never known, and a failure is not a warning


# ------------------------------------------------------------------ telling the owner

def test_each_warning_is_said_once_heard_then_at_most_daily_and_survives_a_restart():
    _box()
    clock = Clock()
    m, fetch = _monitor({"limit": 5, "limit_remaining": 2.5, "usage": 2.5}, clock)
    _run(m.refresh())
    assert m.notice(settings=False) is None          # its line names a phrase not heard here
    assert m.notice() == credit.HALF_TEXT
    assert m.notice() == credit.HALF_TEXT            # not heard yet (cut short): still due
    m.told(credit.HALF_TEXT)
    assert m.notice() is None
    clock.now += 3600
    restarted, _ = _monitor({"limit": 5, "limit_remaining": 2.5, "usage": 2.5}, clock)
    _run(restarted.refresh())
    assert restarted.notice() is None                # told an hour ago, before the restart
    clock.now += credit.REPEAT_SECS
    assert restarted.notice() == credit.HALF_TEXT    # still half used a day later
    fetch.record = {"limit": 5, "limit_remaining": 0.8, "usage": 4.2}
    _run(m.refresh())
    assert m.notice() == credit.LOW_TEXT             # a new state: said at once
    fetch.record = {"limit": 5, "limit_remaining": 0, "usage": 5}
    _run(m.refresh())
    assert m.notice() is None                        # empty is a stop, not a notice
    ai_account.store_key(OWN)
    assert m.notice() is None and m.state() == credit.OWN


def test_a_told_time_in_the_future_is_reset_not_taken_as_expired():
    """A box that booted with a wrong clock (no RTC battery, before NTP): told "tomorrow"
    is now, and quiet."""
    _box()
    clock = Clock()
    m, _ = _monitor({"limit": 5, "limit_remaining": 2.5, "usage": 2.5}, clock)
    _run(m.refresh())
    m.told(credit.HALF_TEXT)
    clock.now -= 30 * 24 * 3600                      # the clock jumps back a month
    assert m.notice() is None
    clock.now += 3600                                # an hour later on the new clock
    assert m.notice() is None
    clock.now += credit.REPEAT_SECS
    assert m.notice() == credit.HALF_TEXT


def test_the_marker_takes_the_documented_recipe_and_a_hashed_key_file():
    for text in (STARTER, STARTER + "\n"):
        home = _box(marker=False)
        os.makedirs(os.path.join(home, ".local", "state", "teaport"), exist_ok=True)
        with open(os.path.join(home, ".local", "state", "teaport", credit.MARKER), "w") as f:
            f.write(hashlib.sha256(text.encode()).hexdigest() + "\n")
        assert credit.on_starter(STARTER), repr(text)
    assert not credit.on_starter(OWN)


def test_pokes_are_one_read_a_minute():
    _box()

    async def run():
        m, fetch = _monitor({"limit": 5, "limit_remaining": 4.0, "usage": 1.0})
        real = credit.MIN_READ_SECS
        credit.MIN_READ_SECS = 1.0
        task = asyncio.ensure_future(m.run())
        try:
            await asyncio.sleep(0.05)
            assert fetch.calls == 1                  # the first read, at once
            for _ in range(20):                      # a burst of reconnects
                m.poke()
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            assert fetch.calls == 1                  # still inside the minute
            await asyncio.sleep(0.8)
            assert fetch.calls == 2                  # then one read for the whole burst
        finally:
            credit.MIN_READ_SECS = real
            task.cancel()
    _run(run())


def test_spent_makes_no_model_on_any_surface_until_an_own_key_is_saved():
    _box()
    m, fetch = _monitor({"data": {"limit": 5, "limit_remaining": 0.001, "usage": 4.999}})
    real = credit.MONITOR
    credit.MONITOR = m
    try:
        _run(m.refresh())
        try:
            services.make_llm()
            raise AssertionError("make_llm built a model on a spent trial credit")
        except services.NoModel as e:
            assert str(e) == credit.SPENT_REASON
        assert services.model_missing() == credit.SPENT_REASON
        assert services.answers_status() == {"state": "unconfigured", "reason": credit.SPENT_REASON}
        # Sessions are built with no model -- the phone's too, which then says its
        # no-model line and hangs up (sip_server, test_no_model_session).
        for front_end in ("sip", "talk", "room"):
            assert build_agent_session(TalkTransport(), front_end=front_end).llm is None, front_end
        # The owner hears why; anyone else the usual no-model line.
        owner = llm_error_speaker.LLMErrorSpeaker(no_model=True, settings=True, owner=True,
                                                  why=services.model_missing)
        assert _run(owner._no_model_line()) == credit.EMPTY_TEXT
        plain = llm_error_speaker.LLMErrorSpeaker(no_model=True, owner=True, why=services.model_missing)
        assert _run(plain._no_model_line()) == credit.EMPTY_PLAIN_TEXT
        bridge = llm_error_speaker.LLMErrorSpeaker(no_model=True, why=services.model_missing)
        assert _run(bridge._no_model_line()) == llm_error_speaker.NO_MODEL_TEXT
        # The owner's key: a model again (the session's "I'm ready now" path), and quiet.
        ai_account.store_key(OWN)
        assert services.model_missing() is None and m.state() == credit.OWN
        assert services.make_llm() is not None
    finally:
        credit.MONITOR = real


def test_the_trial_credit_line_names_the_right_cause_and_only_to_the_owner():
    _box()
    m, fetch = _monitor({"limit": 5, "limit_remaining": 0.9, "usage": 4.1})
    real = credit.MONITOR
    credit.MONITOR = m
    try:
        _run(m.refresh())
        quota = ErrorFrame(error="x", exception=None, category=ErrorCategory.QUOTA)
        refused = ErrorFrame(error="x", exception=None, category=ErrorCategory.AUTHORIZATION)
        owner = lambda: llm_error_speaker.LLMErrorSpeaker(settings=True, owner=True)  # noqa: E731
        assert owner()._notice(quota, 1000.0) == credit.EMPTY_TEXT       # the provider says so
        # $0.90 left and a revoked key or a policy 403: not "the trial credit ran out".
        assert owner()._notice(refused, 1000.0) == llm_error_speaker.ACCOUNT_TEXT
        # The phone and a bridge never hear about the owner's trial credit.
        assert llm_error_speaker.LLMErrorSpeaker()._notice(quota, 1000.0) == llm_error_speaker.ACCOUNT_TEXT
        fetch.record = {"limit": 5, "limit_remaining": 0, "usage": 5}
        _run(m.refresh())
        assert owner()._notice(refused, 1000.0) == credit.EMPTY_TEXT     # nothing left
        ai_account.store_key(OWN)  # the owner's own account refused: the usual line
        assert owner()._notice(quota, 1000.0) == llm_error_speaker.ACCOUNT_TEXT
        # An account refusal reads the credit again (soon: pokes are spaced).
        poked = []
        m.poke = lambda: poked.append(1)
        import httpx
        quota.exception = httpx.HTTPStatusError("402", request=httpx.Request("POST", BASE),
                                                response=httpx.Response(402))
        llm_error_speaker.AnswerHealth().failed(quota)
        assert poked
    finally:
        credit.MONITOR = real


# ------------------------------------------------------------------ the conversation start

class FakeTask:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, frames):
        self.frames += list(frames)


class Notice:
    def __init__(self):
        self.told_n = 0

    def line(self):
        return "Quick note: a made-up notice."

    def told(self):
        self.told_n += 1


def test_the_notice_is_heard_in_full_before_the_greeting_and_counted_only_once_played():
    from teaport_brain.agent_session import AgentSession

    class STT:
        stt_available, slot_busy = True, None

    for played in (True, False):
        s = AgentSession(task=FakeTask(), context=LLMContext([]), stt=STT(), tts=None, llm=None,
                         ledger=None, followup_gate=None)
        s.start_notice = notice = Notice()
        said = []

        async def say_whole(line, max_secs, *, name, append_to_context=False, s=s, played=played):
            said.append((line, len(s.task.frames)))  # before the greeting is queued
            return played

        s._say_whole = say_whole
        _run(s.greet())
        assert said == [("Quick note: a made-up notice.", 0)]
        assert isinstance(s.task.frames[-1], LLMRunFrame)
        assert notice.told_n == (1 if played else 0)   # cut short: said again next time
    # A wake that starts a new conversation says it first, from a task, then greets.
    pushed, started = [], []

    async def push(frame):
        pushed.append(frame)

    async def start_with_notice():
        started.append(1)

    async def wake(store):
        gate = WakeGate(["hey box"], store=store, greeting="(greet)")
        gate.context = LLMContext([])
        gate.notice_due = lambda: True
        gate.start_with_notice = start_with_notice
        await gate.wake(push, "hey box")
        await asyncio.sleep(0.01)

    _run(wake(MicConversation()))
    assert started == [1] and not [f for f in pushed if isinstance(f, LLMRunFrame)]
    store = MicConversation()
    store.save([{"role": "user", "content": "made-up earlier turn"}], [])
    started.clear()
    _run(wake(store))                                 # a conversation continued: no notice
    assert started == []


# ------------------------------------------------------------------ where it shows

def test_health_status_doctor_and_the_settings_line():
    _box()
    m, _ = _monitor({"limit": 5, "limit_remaining": 0.87, "usage": 4.13})
    real = credit.MONITOR
    credit.MONITOR = m
    try:
        _run(m.refresh())
        from teaport_brain import gateway_server
        shown = _run(gateway_server.health())["credit"]
        assert shown["state"] == credit.LOW and shown["remaining"] == 0.87 and shown["fresh"]
        es = i18n.get("es")
        assert m.page_line(es) == es._(credit.PAGE_LINES[credit.LOW]).format(amount="$0.87")
        assert "$0.87" in m.page_line(i18n.get("en"))
        from test_llm_account_error import _cli
        status = [ln for ln in _cli("cmd_status", {"ok": True, "credit": shown}).splitlines()
                  if "trial credit (brain)" in ln]
        assert len(status) == 1 and "almost used up, $0.87 left of $5.00" in status[0], status
        doctor = [ln for ln in _cli("cmd_doctor", {"ok": True, "credit": shown}).splitlines()
                  if "trial credit:" in ln]
        assert len(doctor) == 1 and doctor[0].lstrip().startswith("!"), doctor
        spent = {"state": "empty", "limit": 5, "remaining": 0, "usage": 5, "fresh": True}
        doctor = [ln for ln in _cli("cmd_doctor", {"ok": True, "credit": spent}).splitlines()
                  if "trial credit:" in ln]
        assert doctor[0].lstrip().startswith("✗") and "open my settings" in doctor[0], doctor
        answers = {"state": "unconfigured", "reason": credit.SPENT_REASON}
        line = [ln for ln in _cli("cmd_status", {"ok": True, "answers": answers}).splitlines()
                if "answers (brain)" in ln][0]
        assert "trial credit has run out" in line and "open my settings" in line, line
        # An own key: nothing in status at all.
        assert "trial credit" not in _cli("cmd_status", {"ok": True, "credit": {"state": "own"}})
    finally:
        credit.MONITOR = real


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
