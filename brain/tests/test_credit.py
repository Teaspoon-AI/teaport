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


def _budget(limit, spend, remaining, **more):
    return {"limit_usd": limit, "spend_usd": spend, "remaining_usd": remaining,
            "reset_interval": "monthly", "resets_at": "2026-12-01T00:00:00Z",
            "scope": "key", "include_byok": False, **more}


def test_the_tighter_of_the_key_limit_and_its_effective_budget_decides():
    """OpenRouter's newer record carries effective_budget beside the key's own limit
    (shapes from a live read, made-up amounts): whichever has less left is judged, the
    half mark by its own usage."""
    j = lambda r: credit.judge(ai_account.key_record({"data": r}), half=0.5, low=1.0)  # noqa: E731
    key = {"limit": 5, "limit_remaining": 4.5, "usage": 0.5}  # roomy on its own
    # The budget is tighter: it decides, each state in turn.
    for b, state in ((_budget(10.0, 4.0, 4.0), credit.OK),     # 4 left of 10, 40% spent
                     (_budget(4.0, 2.5, 1.5), credit.HALF),    # 62% of the budget spent
                     (_budget(4.0, 3.2, 0.8), credit.LOW),
                     (_budget(4.0, 4.0, 0.0), credit.EMPTY)):
        got, numbers = j({**key, "effective_budget": b, "budgets": [b]})
        assert got == state and numbers == {"limit": b["limit_usd"], "remaining": b["remaining_usd"],
                                            "usage": b["spend_usd"]}, (b, got, numbers)
    # The key's own limit is tighter: it decides, the budget's roomier numbers aside.
    tight = {"limit": 5, "limit_remaining": 0.6, "usage": 4.4}
    got, numbers = j({**tight, "effective_budget": _budget(100.0, 1.0, 99.0)})
    assert got == credit.LOW and numbers == {"limit": 5.0, "remaining": 0.6, "usage": 4.4}
    half_key = {"limit": 5, "limit_remaining": 2.4, "usage": 2.6}
    assert j({**half_key, "effective_budget": _budget(10.0, 1.0, 9.0)})[0] == credit.HALF
    # The budget alone bounds a key with no limit of its own.
    assert j({"limit": None, "limit_remaining": None, "usage": 3,
              "effective_budget": _budget(2.0, 1.2, 0.8)})[0] == credit.LOW
    # No spend_usd: the budget's usage is what its limit and remainder imply.
    got, numbers = j({**key, "effective_budget": {"limit_usd": 4.0, "remaining_usd": 1.5}})
    assert got == credit.HALF and numbers["usage"] == 2.5


def test_only_the_old_fields_or_a_malformed_budget_judge_as_before():
    j = lambda r: credit.judge(ai_account.key_record({"data": r}), half=0.5, low=1.0)  # noqa: E731
    old = {"limit": 5, "limit_remaining": 2.5, "usage": 2.5}
    want = j(old)
    assert want == (credit.HALF, {"limit": 5.0, "remaining": 2.5, "usage": 2.5})
    # A budget limit of 0 or less is no budget set: the key's own limit decides.
    for unset in (0, 0.0, -1):
        assert j({**old, "effective_budget": _budget(unset, 0.0, 0.0)}) == want, unset
    for bad in (None, "4.0", [], {}, {"limit_usd": "4", "remaining_usd": "0.1"},
                {"limit_usd": 4.0}, {"remaining_usd": 0.1}, {"limit_usd": True, "remaining_usd": 0.1},
                {"limit_usd": 4.0, "remaining_usd": None, "spend_usd": 3.9}):
        assert j({**old, "effective_budget": bad, "budgets": "nonsense"}) == want, bad


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


def test_a_box_without_an_rtc_does_not_repeat_the_warning_after_ntp():
    """No RTC battery: the box boots at its last saved time, behind the real one, and NTP
    sets the clock later. A told time ahead of the clock is kept and not due; the day
    counts from when it was really said (#280 review)."""
    _box()
    clock = Clock()
    m, _ = _monitor({"limit": 5, "limit_remaining": 2.5, "usage": 2.5}, clock)
    _run(m.refresh())
    told_at = clock.now
    m.told(credit.HALF_TEXT)
    clock.now = told_at - 3 * 3600                   # reboot: the clock three hours behind
    assert m.notice() is None
    assert credit._load_notices()[m._notice_key(credit.HALF)] == told_at  # kept as it was
    clock.now = told_at + 3600                       # NTP: the real time, an hour after
    assert m.notice() is None
    clock.now = told_at + credit.REPEAT_SECS + 1     # a real day later: due again
    assert m.notice() == credit.HALF_TEXT
    # A time written by a clock that ran far ahead is put right, quietly.
    m.told(credit.HALF_TEXT)
    clock.now -= 30 * 24 * 3600
    assert m.notice() is None
    assert credit._load_notices()[m._notice_key(credit.HALF)] == clock.now


def test_a_session_built_at_boot_waits_briefly_for_the_first_read():
    """A box that boots with its trial credit spent: the first session waits (bounded)
    for the first read and is built with no model (#280 review)."""
    _box()

    async def run(delay, timeout):
        m, fetch = _monitor({"limit": 5, "limit_remaining": 0, "usage": 5})
        real_fetch = fetch.__call__

        def slow(key, base_url, timeout=None):
            import time as tm
            tm.sleep(delay)
            return real_fetch(key, base_url)

        m._fetch = slow
        real = credit.MONITOR
        credit.MONITOR = m
        task = asyncio.ensure_future(m.run())
        try:
            await asyncio.sleep(0)
            t0 = asyncio.get_running_loop().time()
            await m.settled(timeout)
            waited = asyncio.get_running_loop().time() - t0
            return m.spent(), waited
        finally:
            credit.MONITOR = real
            task.cancel()

    spent, waited = _run(run(0.2, 3.0))
    assert spent and waited < 2.0
    spent, waited = _run(run(2.0, 0.3))              # a stalled read: going on without it
    assert not spent and waited < 1.0
    # Off the starter key nothing is waited for.
    ai_account.store_key(OWN)
    _, waited = _run(run(2.0, 3.0))
    assert waited < 0.1


def test_the_credit_running_out_mid_conversation_ends_the_session_for_a_model_less_next():
    """A 402 on the starter key in the owner's conversation: the trial-credit line, the
    credit counted as spent, and the session ended the way #277's "I'm ready now" ends
    one -- so the next is built with no model (#280 review)."""
    from test_llm_account_error import ACCOUNT_CASES, _llm, _raise_through, _spoken
    _box()
    m, _ = _monitor({"limit": 5, "limit_remaining": 0.9, "usage": 4.1})
    real = credit.MONITOR
    credit.MONITOR = m

    async def run():
        await m.refresh()
        llm, pushed = _llm()
        frame = await _raise_through(llm, pushed, ACCOUNT_CASES["402 no credit"]())
        speaker = llm_error_speaker.LLMErrorSpeaker(settings=True, owner=True)
        ended = []

        async def end():
            ended.append(1)

        speaker.end = end
        said = await _spoken(speaker, frame)
        return said, ended

    try:
        said, ended = _run(run())
        assert said == [credit.EMPTY_TEXT] and ended == [1], (said, ended)
        assert m.spent()
        try:
            services.make_llm()
            raise AssertionError("a model after the trial credit ran out")
        except services.NoModel:
            pass
    finally:
        credit.MONITOR = real


def test_asking_while_spent_reads_again_and_so_does_opening_settings():
    _box()
    m, _ = _monitor({"limit": 5, "limit_remaining": 0, "usage": 5})
    real = credit.MONITOR
    credit.MONITOR = m
    poked = []
    m.poke = lambda: poked.append(1)
    try:
        _run(m.refresh())
        speaker = llm_error_speaker.LLMErrorSpeaker(no_model=True, owner=True,
                                                    why=services.model_missing)
        assert _run(speaker._no_model_line()) == credit.EMPTY_PLAIN_TEXT and poked == [1]
        from teaport_brain import pairing, settings_access

        class Voice:
            t, said = i18n.get("en"), []

            async def say(self, text):
                self.said.append(text)

        host, pairing.FRONTDOOR_HOST = pairing.FRONTDOOR_HOST, ""
        try:
            _run(settings_access.SettingsPhrase().open(Voice()))
        finally:
            pairing.FRONTDOOR_HOST = host
        assert poked == [1, 1]  # "open my settings": the owner is at the account
    finally:
        credit.MONITOR = real


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
        # $0.90 left and a revoked key or a policy 403: not "the trial credit ran out" --
        # the account line, naming the phrase (the owner's surface).
        assert owner()._notice(refused, 1000.0) == llm_error_speaker.ACCOUNT_SETTINGS_TEXT
        # The phone and a bridge never hear about the owner's trial credit: that the
        # account is out of credit, without the phrase.
        assert llm_error_speaker.LLMErrorSpeaker()._notice(quota, 1000.0) == llm_error_speaker.CREDIT_TEXT
        fetch.record = {"limit": 5, "limit_remaining": 0, "usage": 5}
        _run(m.refresh())
        assert owner()._notice(refused, 1000.0) == credit.EMPTY_TEXT     # nothing left
        ai_account.store_key(OWN)  # the owner's own account out of credit: its own line
        assert owner()._notice(quota, 1000.0) == llm_error_speaker.CREDIT_SETTINGS_TEXT
        assert owner()._notice(refused, 1000.0) == llm_error_speaker.ACCOUNT_SETTINGS_TEXT
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


def test_the_wake_notice_task_ends_with_the_session_and_a_hold():
    """The notice a wake starts is cancelled when the session ends or a call puts it on
    hold; a held session is not unmuted by it, nor greeted (#280 review)."""
    from teaport_brain.agent_session import AgentSession

    async def run(how):
        gate = WakeGate(["hey box"], store=MicConversation(), greeting="(greet)")
        gate.context = LLMContext([])
        playing = asyncio.Event()

        async def start_with_notice():
            playing.set()
            await asyncio.sleep(30)               # the line playing

        gate.notice_due = lambda: True
        gate.start_with_notice = start_with_notice

        async def push(frame):
            pass

        await gate.wake(push, "hey box")
        await playing.wait()
        (task,) = gate._starting
        if how == "ended":
            gate.ended()
        else:
            s = AgentSession(task=FakeTask(), context=LLMContext([]), stt=None, tts=None,
                             llm=None, ledger=None, followup_gate=None)
            s.wake_gate = gate
            gate.cancel_start()  # what hold() does first
        await asyncio.sleep(0)
        return task.cancelled() or task.done(), gate._starting

    for how in ("ended", "hold"):
        done, left = _run(run(how))
        assert done and not left, how

    # A call that holds the session while the notice plays: no unmute, no greeting.
    class Mute:
        muted = True

    s = AgentSession(task=FakeTask(), context=LLMContext([]), stt=None, tts=None, llm=None,
                     ledger=None, followup_gate=None, input_mute=Mute())
    s.start_notice = Notice()

    async def say_whole(line, max_secs, *, name, append_to_context=False):
        s.held = True                              # the call came while it played
        return False

    s._say_whole = say_whole
    _run(s.start_with_notice())
    assert s.input_mute.muted is True and s.task.frames == []


def _held_session(notice_playing: asyncio.Event, notice_done: asyncio.Event):
    """A session whose start notice plays until notice_done, with the real hold()."""
    from teaport_brain.agent_session import AgentSession

    class Mute:
        muted = False

    class Gate:
        def set_held(self, on):
            pass

    class STT:
        stt_available, slot_busy = True, None

        async def hold(self):
            await asyncio.sleep(0)   # hold() awaits here, as the real STT does

    s = AgentSession(task=FakeTask(), context=LLMContext([]), stt=STT(), tts=None, llm=None,
                     ledger=None, followup_gate=Gate(), input_mute=Mute())
    s.start_notice = Notice()

    async def say_whole(line, max_secs, *, name, append_to_context=False):
        s.input_mute.muted = True
        notice_playing.set()
        await notice_done.wait()     # cut by the hold's own line, or cancelled
        return False

    async def tell(message):
        pass

    s._say_whole = say_whole
    s._tell_client = tell
    s._keep_alive_on_hold = lambda: asyncio.sleep(0)
    return s


def test_a_hold_while_the_start_notice_plays_leaves_the_session_unheard_and_ungreeted():
    """The real hold(), while the notice plays: on the wake path (its task cancelled) and
    on greet()'s (a Talk connect, the room awake). Either way the held session stays
    muted and no greeting goes in (#280 review)."""
    async def wake_path(line):
        playing, done = asyncio.Event(), asyncio.Event()
        s = _held_session(playing, done)
        gate = WakeGate(["hey box"], store=MicConversation(), greeting="(greet)")
        gate.context = LLMContext([])
        gate.notice_due = lambda: True
        gate.start_with_notice = s.start_with_notice
        s.wake_gate = gate

        async def push(frame):
            pass

        await gate.wake(push, "hey box")
        await playing.wait()
        await s.hold(line, 1.0)
        await asyncio.sleep(0.01)
        return s

    async def greet_path(line):
        playing, done = asyncio.Event(), asyncio.Event()
        s = _held_session(playing, done)
        greeting = asyncio.ensure_future(s.greet())
        await playing.wait()
        hold = asyncio.ensure_future(s.hold(line, 1.0))
        await asyncio.sleep(0)
        done.set()                   # the hold's line cuts the notice
        await hold
        await greeting
        return s

    async def say_last_line(self, line, max_secs, *, append_to_context=False):
        self.input_mute.muted = True
        return True

    from teaport_brain.agent_session import AgentSession
    real = AgentSession.say_last_line
    AgentSession.say_last_line = say_last_line
    try:
        for path in (wake_path, greet_path):
            for line in (None, "I'll step away for a moment."):
                s = _run(path(line))
                assert s.held and s.input_mute.muted, (path.__name__, line)
                assert not [f for f in s.task.frames if isinstance(f, LLMRunFrame)], path.__name__
                assert s.start_notice.told_n == 0  # cut short: due again next time
    finally:
        AgentSession.say_last_line = real


def test_a_refusal_keeps_the_credit_spent_until_a_real_top_up():
    """OpenRouter can refuse (402) with a few cents still showing: the mark stays spent --
    across a restart too -- through reads that show the same, and lifts only when one
    shows LOW_USD more than at the refusal (#280 review)."""
    _box()
    m, fetch = _monitor({"limit": 5, "limit_remaining": 0.30, "usage": 4.70})
    _run(m.refresh())
    assert m.state() == credit.LOW
    m.refused_for_credit()
    assert m.spent()
    for _ in range(3):                                # the flap: a read, "low" again
        _run(m.refresh())
        assert m.spent(), m.status()
    restarted, fetch2 = _monitor({"limit": 5, "limit_remaining": 0.30, "usage": 4.70})
    _run(restarted.refresh())
    assert restarted.spent()                          # remembered across a restart
    fetch2.record = {"limit": 5, "limit_remaining": 0.9, "usage": 4.1}
    _run(restarted.refresh())
    assert restarted.spent()                          # not a real top-up
    fetch2.record = {"limit": 10, "limit_remaining": 5.3, "usage": 4.7}
    _run(restarted.refresh())
    assert restarted.state() == credit.OK             # topped up: counts again
    fetch2.record = {"limit": 10, "limit_remaining": 5.2, "usage": 4.8}
    _run(restarted.refresh())
    assert restarted.state() == credit.OK             # and the mark is gone


def test_a_refusal_with_credit_left_on_the_key_is_the_accounts_and_passes():
    """A key's limit is a cap; the money is the account's. The vendor's balance behind the
    starter keys runs dry: a 402 with $3 left on this key. The session ends as for any
    such refusal, but nothing sticky is kept: once the account is refilled, the next read
    shows the key as it is and the box answers again -- restart or not (#280 review)."""
    _box()
    m, fetch = _monitor({"limit": 5, "limit_remaining": 3.0, "usage": 2.0})
    _run(m.refresh())
    assert m.state() == credit.OK
    m.refused_for_credit()
    assert m.spent()                                  # until the next read
    assert not any(k.endswith(":refused") for k in credit._load_notices())
    _run(m.refresh())                                 # the vendor refilled the account
    assert m.state() == credit.OK
    restarted, _ = _monitor({"limit": 5, "limit_remaining": 3.0, "usage": 2.0})
    _run(restarted.refresh())
    assert restarted.state() == credit.OK
    # No read yet when it was refused: nothing to judge by, nothing kept either.
    fresh, _ = _monitor({"limit": 5, "limit_remaining": 3.0, "usage": 2.0})
    fresh.refused_for_credit()
    assert fresh.spent() and not any(k.endswith(":refused") for k in credit._load_notices())
    _run(fresh.refresh())
    assert fresh.state() == credit.OK


def test_with_the_low_warning_off_a_refusal_at_a_cent_still_stays_spent():
    """TEAPORT_STARTER_LOW_USD=0 turns the low warning off; the sticky cut-off and the
    top-up margin still have a floor, STICKY_FLOOR_USD (#280 review)."""
    _box()
    real = credit.LOW_USD
    credit.LOW_USD = 0.0
    try:
        m, fetch = _monitor({"limit": 5, "limit_remaining": 0.012, "usage": 4.988})
        _run(m.refresh())
        m.refused_for_credit()
        for record in ({"limit": 5, "limit_remaining": 0.012, "usage": 4.988},
                       {"limit": 5, "limit_remaining": 0.05, "usage": 4.95}):
            fetch.record = record
            _run(m.refresh())
            assert m.spent(), record                  # no flap
        fetch.record = {"limit": 10, "limit_remaining": 8.0, "usage": 2.0}
        _run(m.refresh())
        assert m.state() == credit.OK                 # a real top-up
    finally:
        credit.LOW_USD = real


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
