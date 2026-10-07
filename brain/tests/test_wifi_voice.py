"""Wi-Fi setup's voice half (wifi_voice.py), past the happy path test_wifi_setup.py walks:
a unit that dies or never reports, a stop that fails, a confirm question that lapses or
gets a sentence instead of an answer, questions about Wi-Fi that are not requests, the
box's own voice heard back, a barge-in in the middle of starting, the spelling of a
flash-time password -- and, against the real turn controller and the brain's own stop
strategy, that a transcript setup takes still closes the turn it opened."""
import asyncio
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.audio.turn.smart_turn.base_smart_turn import (  # noqa: E402
    BaseSmartTurn,
    SmartTurnParams,
)
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UninterruptibleFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_turn_controller import UserTurnController  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from turn_harness import running_controller  # noqa: E402

from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from teaport_brain import i18n  # noqa: E402
from teaport_brain.client_notes import ClientNotes  # noqa: E402
from teaport_brain.followup_gate import (  # noqa: E402
    SYSTEM_NOTICE_TAG, FollowupGate, FollowupTrigger,
)
from teaport_brain import wifi_setup as ws  # noqa: E402
from teaport_brain import wifi_voice as wv  # noqa: E402
from teaport_brain.endpointing import (  # noqa: E402
    ENDPOINT_STOP_SECS,
    INTERRUPT_MIN_WORDS,
    SMARTTURN_STOP_SECS,
    LateStartTurnStopStrategy,
)
from teaport_brain.stt import FinalTranscriptionFrame  # noqa: E402

EN = i18n.get("en")
DETAILS = {"ssid": "teaport-9e35", "password": "47190352", "error": ""}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Voice(wv.WifiSetupVoice):
    """The processor without a pipeline: what it pushes is collected (its speech among
    it, so say() itself runs), the unit helper and the liveness probe are fakes, and
    tasks are asyncio's. `gate`: {action: asyncio.Event} the helper waits on."""

    def __init__(self, path, results=None, alive=True, gate=None, online=False, **kw):
        self.calls, self.pushed = [], []
        self.alive, self.online = alive, online
        results, gate = results or {}, gate or {}

        async def run(action, unit):
            self.calls.append(action)
            if action in gate:
                await gate[action].wait()
            return results.get(action, (0, ""))

        async def is_alive():
            return self.alive

        async def is_online():
            return self.online

        super().__init__(run=run, status_path=path, alive=is_alive, online=is_online, **kw)

    @property
    def said(self):
        return [f.text for f in self.pushed if isinstance(f, TTSSpeakFrame)]

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    def _spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def _path():
    return os.path.join(tempfile.mkdtemp(), "status.json")


def _write(path, run_id="r1", at=None, **status):
    with open(path, "w") as f:
        json.dump({"run_id": run_id, "time": time.time() if at is None else at, **status}, f)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(wv, "POLL_SECS", 0.01)
    monkeypatch.setattr(wv, "LIVENESS_SECS", 0.05)


async def _running(v, path):
    """Asked, confirmed, started, the setup network up."""
    assert await v._heard("set up wifi") and await v._heard("yes")
    assert v.state == "running" and v.calls == ["restart"]
    _write(path, phase="ap_up", **DETAILS)
    await asyncio.sleep(0.05)
    assert any("four, seven" in s for s in v.said)


# ------------------------------------------------------------------ the unit's fate

def test_a_unit_that_dies_mid_setup_is_said_and_let_go(fast):
    """Killed with no last word (memory, RuntimeMaxSec, a stop from elsewhere): its file
    still says ap_up. The voice must not wait on it forever, keeping every word from the
    model and reminding the user of a network that is gone."""
    path = _path()

    async def run():
        v = Voice(path)
        await _running(v, path)
        v.alive = False
        await asyncio.sleep(0.2)
        assert v.state == "idle"
        assert v.said[-1] == "Wi-Fi setup stopped: something went wrong."
        assert not await v._heard("what time is it")      # the model has it again
    asyncio.run(run())


def test_a_unit_stopped_from_outside_says_it_put_things_back(fast):
    """systemctl stop / the config page / a restart: the unit runs its restore path on
    SIGTERM and writes "stopped" (wifi_setup.py). The voice takes that as the end."""
    path = _path()

    async def run():
        v = Voice(path)
        await _running(v, path)
        _write(path, phase="stopped")
        v.alive = False
        await asyncio.sleep(0.2)
        assert v.state == "idle"
        assert v.said[-1] == "Okay, I've stopped Wi-Fi setup and put things back as they were."
    asyncio.run(run())


def test_its_last_words_win_over_its_absence(fast):
    path = _path()

    async def run():
        v = Voice(path)
        await _running(v, path)
        _write(path, phase="connected", target="home")
        v.alive = False
        await asyncio.sleep(0.2)
        assert v.state == "idle" and "online again" in v.said[-1]
        assert not any("went wrong" in s for s in v.said)
    asyncio.run(run())


def test_no_setup_outlives_the_units_hard_stop(fast, monkeypatch):
    """systemctl cannot say (None): the deadline past RuntimeMaxSec still ends it."""
    monkeypatch.setattr(wv, "DEADLINE_SECS", 0.2)
    path = _path()

    async def run():
        v = Voice(path, alive=None)
        await _running(v, path)
        await asyncio.sleep(0.3)
        assert v.state == "idle" and "went wrong" in v.said[-1] and v.calls == ["restart", "stop"]
    asyncio.run(run())


def test_this_runs_news_is_told_by_its_content_not_the_clock(fast):
    """The file from before is ignored, whatever its time says; a new run's file counts
    even when its time is EARLIER (the clock jumps when NTP syncs on joining)."""
    path = _path()
    _write(path, run_id="before", at=2e9, phase="connected", target="stale")

    async def run():
        v = Voice(path)
        await v._heard("set up wifi")
        await v._heard("yes")
        await asyncio.sleep(0.05)
        assert not any("stale" in s for s in v.said)
        _write(path, run_id="now", at=1e9, phase="connected", target="home")
        await asyncio.sleep(0.05)
        assert v.state == "idle" and "connected to home" in v.said[-1]
    asyncio.run(run())


def test_a_setup_that_never_reports_is_stopped_not_just_abandoned(fast, monkeypatch):
    """No status: maybe it never started, maybe it runs where the brain cannot read it.
    It is stopped either way, so the box is not left offline with nobody following."""
    monkeypatch.setattr(wv, "START_TIMEOUT_SECS", 0.1)

    async def run():
        v = Voice(_path())
        await v._heard("set up wifi")
        await v._heard("yes")
        await asyncio.sleep(0.25)
        return v
    v = asyncio.run(run())
    assert v.state == "idle" and v.calls == ["restart", "stop"]
    assert v.said[-1].startswith("Wi-Fi setup didn't start")


def test_a_failed_stop_is_not_reported_as_stopped(fast):
    path = _path()

    async def run():
        v = Voice(path, results={"stop": (1, "sudo: a password is required")})
        await _running(v, path)
        assert await v._heard("cancel")
        assert v.state == "running" and v.said[-1].startswith("I'm in Wi-Fi setup")
        assert not any("stopped Wi-Fi setup" in s for s in v.said)
        _write(path, phase="connected", target="home")   # still followed
        await asyncio.sleep(0.05)
        assert "online again" in v.said[-1]
    asyncio.run(run())


def test_a_cancel_that_crosses_the_end_claims_nothing(fast):
    path = _path()
    gate = {"stop": asyncio.Event}

    async def run():
        gate["stop"] = asyncio.Event()
        v = Voice(path, gate=gate)
        await _running(v, path)
        cancel = asyncio.create_task(v._heard("cancel"))
        await asyncio.sleep(0.02)
        _write(path, phase="connected", target="home")
        await asyncio.sleep(0.05)
        gate["stop"].set()
        await cancel
        assert v.state == "idle" and "online again" in v.said[-1]
        assert not any("put things back" in s for s in v.said)
    asyncio.run(run())


def test_an_interrupted_start_still_starts_and_is_followed(fast):
    """A barge-in cancels the frame being processed. Half a start -- the unit asked to
    start and nobody following it -- must not be what that leaves."""
    path = _path()

    async def run():
        gate = {"restart": asyncio.Event()}
        v = Voice(path, gate=gate)
        await v._heard("set up wifi")
        heard = asyncio.create_task(v._heard("yes"))
        await asyncio.sleep(0.02)
        heard.cancel()
        await asyncio.sleep(0.01)
        gate["restart"].set()
        await asyncio.sleep(0.02)
        assert v.state == "running" and v._poller is not None and not v._poller.done()
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.05)
        assert any("four, seven" in s for s in v.said)
        v._poller.cancel()
    asyncio.run(run())


# ------------------------------------------------------------------ the question

@pytest.mark.parametrize("text", [
    "Okay, so what's the weather tomorrow?", "Please tell me a joke about cats",
    "Sure, but first, what time is it?", "okay let me think about it for a bit"])
def test_a_sentence_with_a_yes_word_in_it_is_not_a_yes(text):
    async def run():
        v = Voice(_path())
        await v._heard("set up wifi")
        assert await v._heard(text)
        return v
    v = asyncio.run(run())
    assert v.state == "confirm" and v.calls == []
    assert v.said[-1] == "Say yes to start Wi-Fi setup, or no."


@pytest.mark.parametrize("text", ["yes", "Yes please.", "Okay, go ahead", "sure", "yeah do it"])
def test_an_answer_is_a_yes(text, fast):
    async def run():
        v = Voice(_path())
        await v._heard("set up wifi")
        await v._heard(text)
        v._poller.cancel()
        return v
    assert asyncio.run(run()).calls == ["restart"]


def test_cjk_sentences_are_sized_by_characters():
    zh, ja = i18n.get("zh"), i18n.get("ja")
    assert wv.is_yes("好的，开始吧", zh) and wv.is_yes("はい、お願いします", ja)
    assert not wv.is_yes("好的，那你先告诉我明天的天气情况吧", zh)
    assert not wv.is_yes("好的，那明天天气怎么样？", zh)


def test_an_unanswered_question_lapses():
    clock = Clock()

    async def run():
        v = Voice(_path(), clock=clock)
        await v._heard("set up wifi")
        clock.now += wv.CONFIRM_SECS + 1
        return v, await v._heard("yes")
    v, took = asyncio.run(run())
    assert not took and v.state == "idle" and v.calls == []


@pytest.mark.parametrize("text,hit", [
    ("How do I change my wifi password?", False), ("My laptop can't connect to wifi.", False),
    ("My laptop can’t connect to wifi.", False), ("Can you set up wifi on my phone?", False),
    ("why won't my tv join the wifi", False),
    ("set up wifi", True), ("Can you set up the Wi-Fi?", True), ("connect to wi-fi please", True),
    ("change my wifi", True), ("switch to a different wifi", True)])
def test_requests_are_heard_and_questions_are_not(text, hit):
    assert wv.heard("start", text, EN) is hit


def _final(text, stop_n=None):
    return FinalTranscriptionFrame(text, "u", "t", None, finalized=True, stop_n=stop_n)


def test_words_held_while_asking_go_back_to_the_model_when_no_answer_comes():
    """The phrase, then two answers that are neither yes nor no: whatever it was, it was
    not setup, and the model gets all of it -- not just the last words."""
    async def run():
        v = Voice(_path())
        for text, n in (("connect to wifi", 1), ("hmm, I meant my laptop", 2)):
            await v.process_frame(_final(text, n), FrameDirection.DOWNSTREAM)
        last = _final("what should I do", 3)
        await v.process_frame(last, FrameDirection.DOWNSTREAM)
        return v, last
    v, last = asyncio.run(run())
    frames = [f for f in v.pushed if isinstance(f, TranscriptionFrame)]
    assert [f.text for f in frames] == [
        "", "", "connect to wifi", "hmm, I meant my laptop", "what should I do"]
    assert all(wv.is_taken(f) for f in frames[:2]) and not any(wv.is_taken(f) for f in frames[2:])
    assert [f.stop_n for f in frames[2:]] == [None, None, 3]   # only the real final closes
    assert frames[-1] is last and v.state == "idle"


def test_a_no_gives_nothing_back():
    """'Set up Wi-Fi' handed to the model after a no would only hand over again."""
    async def run():
        v = Voice(_path())
        await v.process_frame(_final("set up wifi", 1), FrameDirection.DOWNSTREAM)
        await v.process_frame(_final("no thanks", 2), FrameDirection.DOWNSTREAM)
        return v
    v = asyncio.run(run())
    assert [f.text for f in v.pushed if isinstance(f, TranscriptionFrame)] == ["", ""]
    assert v.state == "idle" and v.said[-1] == "Okay, never mind."


def test_online_the_phrase_is_the_models():
    """Online, "set up Wi-Fi" in conversation goes on untouched: the model decides (it
    has the wifi_setup tool). The phrase is the way in only for a box with no internet."""
    async def run():
        v = Voice(_path(), online=True)
        frame = _final("can you set up the wifi later", 1)
        await v.process_frame(frame, FrameDirection.DOWNSTREAM)
        return v, frame
    v, frame = asyncio.run(run())
    assert v.pushed == [frame] and not wv.is_taken(frame)
    assert v.said == [] and v.calls == [] and v.state == "idle"


def test_online_a_setup_the_model_began_still_takes_its_answers(fast):
    """The tool's way in: begin() asks, and the yes, the repeat and the cancel are setup's
    even though the box is online (it is the model that started it)."""
    async def run():
        path = _path()
        v = Voice(path, online=True)
        await v.begin()
        assert v.state == "confirm"
        assert await v._heard("yes") and v.state == "running" and v.calls == ["restart"]
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.05)
        assert await v._heard("repeat that")
        assert await v._heard("cancel")
        return v
    v = asyncio.run(run())
    assert v.calls == ["restart", "stop"] and v.state == "idle"


def _tool(directory, name, script):
    """A fake executable `name` in `directory` (put on PATH by the test)."""
    exe = directory / name
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)


@pytest.mark.parametrize("answer, online", [
    ("full", True), ("limited", False), ("portal", False), ("none", False),
    ("unknown", False), (None, False)])
def test_online_means_full_connectivity(answer, online, monkeypatch, tmp_path):
    _tool(tmp_path, "busctl", "echo 'b true'\n")
    if answer is not None:   # None: no nmcli at all
        # Only a fresh check counts: the cached answer can be minutes old.
        _tool(tmp_path, "nmcli", f'[ "$*" = "networking connectivity check" ] && echo {answer}\n')
    monkeypatch.setenv("PATH", str(tmp_path))
    assert asyncio.run(wv._box_online()) is online


@pytest.mark.parametrize("busctl", [
    "echo 'b false'\n", "echo 'Failed to get property' >&2; exit 1\n", None])
def test_without_nms_connectivity_check_full_means_nothing(busctl, monkeypatch, tmp_path):
    # No check configured: NM says "full" whenever there is a default route.
    if busctl is not None:   # None: no busctl at all
        _tool(tmp_path, "busctl", busctl)
    _tool(tmp_path, "nmcli", "echo full\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert asyncio.run(wv._box_online()) is False


@pytest.mark.parametrize("slow", ["busctl", "nmcli"])
def test_a_check_that_hangs_is_offline_in_time_and_killed(slow, monkeypatch, tmp_path):
    # A dead link can stall the check: the phrase must still work, and soon.
    monkeypatch.setattr(wv, "NM_PROPERTY_SECS", 0.3)
    monkeypatch.setattr(wv, "ONLINE_CHECK_SECS", 0.3)
    pid = tmp_path / "pid"
    hang = f"echo $$ > {pid}\nexec sleep 30\n"
    _tool(tmp_path, "busctl", hang if slow == "busctl" else "echo 'b true'\n")
    _tool(tmp_path, "nmcli", hang if slow == "nmcli" else "echo full\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")   # the fakes find sleep
    started = time.monotonic()
    assert asyncio.run(wv._box_online()) is False
    assert time.monotonic() - started < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)   # killed and reaped, not left running


def test_a_check_cancelled_mid_way_leaves_no_child(monkeypatch, tmp_path):
    # A barge-in cancels the frame being processed, and the check with it.
    pid = tmp_path / "pid"
    _tool(tmp_path, "busctl", "echo 'b true'\n")
    _tool(tmp_path, "nmcli", f"echo $$ > {pid}\nexec sleep 30\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    async def run():
        task = asyncio.ensure_future(wv._box_online())
        while not pid.exists():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(asyncio.wait_for(run(), 5))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


# ------------------------------------------------------------------ the box's own voice

def test_its_own_voice_heard_back_is_not_an_answer():
    clock = Clock()

    async def run():
        v = Voice(_path(), clock=clock)
        await v._heard("set up wifi")
        await v.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        # The tail of the question, through the microphone: not a "no".
        assert await v._heard("Say yes or no.")
        assert v.state == "confirm" and v._unclear == 0 and len(v.said) == 1
        await v.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        clock.now += 0.5
        assert await v._heard("say yes or no") and v.state == "confirm"   # still the tail
        clock.now += wv.ECHO_TAIL_SECS
        # Long after: the user's own words, whatever they quote.
        assert await v._heard("Wait, you'll go offline for a few minutes?")
        assert v._unclear == 1
        return v
    asyncio.run(run())


def test_a_one_word_answer_over_its_voice_counts(fast):
    async def run():
        v = Voice(_path())
        await v._heard("set up wifi")
        await v.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await v._heard("yes")
        v._poller.cancel()
        return v
    assert asyncio.run(run()).calls == ["restart"]


# ------------------------------------------------------------------ spelling

def test_capitals_are_said_whenever_there_are_any():
    assert wv.spell("ABCD1234", EN) == "capital A, capital B, capital C, capital D, one, two, three, four"
    assert wv.spell("Tito2017", EN) == "capital T, I, T, O, two, zero, one, seven"
    assert wv.spell("abcd", EN) == "A, B, C, D"


def test_only_the_setup_networks_word_is_said_as_a_word():
    assert wv.spell("teaport-9e35", EN) == "teaport, dash, nine, E, three, five"
    assert wv.spell("kitten-42", EN) == "K, I, T, T, E, N, dash, four, two"


def test_a_flash_time_password_is_one_the_voice_can_spell():
    assert set(ws.PASSWORD_SYMBOLS) == set(wv._SYMBOLS)
    for good in ("Tea-Port.42", "my wifi 2024", "a_b@c!d#e&f*"):
        assert ws.setup_password(good) == good
    for bad in ("abc,defgh1", "pass/word1", "100%secure", "naïve1234", "quote'1234"):
        pw = ws.setup_password(bad)
        assert pw.isdigit() and len(pw) == ws.PASSWORD_DIGITS, bad


# ------------------------------------------------------------------ the turn it took

def test_a_taken_transcript_keeps_its_kind_and_its_stamp():
    final = _final("set up wifi", 3)
    out = wv.taken(final)
    assert type(out) is FinalTranscriptionFrame and isinstance(out, UninterruptibleFrame)
    assert out.text == "" and out.stop_n == 3 and out.finalized and wv.is_taken(out)
    assert out.id != final.id and not wv.is_taken(final) and final.text == "set up wifi"


SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK = b"\x00\x02" * int(SAMPLE_RATE * CHUNK_MS / 1000)


class Complete(BaseSmartTurn):
    def _predict_endpoint(self, audio_array):
        return {"prediction": 1, "probability": 0.98}


class Aggregator:
    """What the closer needs of the LLM user aggregator: its text so far, dropped."""

    def __init__(self):
        self.resets = 0

    async def reset(self):
        self.resets += 1


async def _turn(with_closer):
    """The brain's start strategy, its stop strategy (and the closer ahead of it, as
    agent_session lists them) in a real controller: "set up wifi" opens a turn on its
    interim, VAD stops, and its final arrives -- taken by setup. Returns the turn
    starts and stops seen, and the aggregator's resets."""
    analyzer = Complete(sample_rate=SAMPLE_RATE, params=SmartTurnParams(stop_secs=SMARTTURN_STOP_SECS))
    analyzer.set_sample_rate(SAMPLE_RATE)
    closer = wv.SetupTurnCloser()
    closer.aggregator = Aggregator()
    controller = UserTurnController(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=INTERRUPT_MIN_WORDS)],
            stop=([closer] if with_closer else []) + [LateStartTurnStopStrategy(turn_analyzer=analyzer)]),
        user_turn_stop_timeout=3600)  # the 5 s force-stop is what must not be needed
    starts, stops = [], []

    async def ignore(*args, **kwargs):
        pass

    for event in ("on_push_frame", "on_broadcast_frame", "on_reset_aggregation",
                  "on_user_turn_inference_triggered", "on_user_turn_stop_timeout"):
        controller.add_event_handler(event, ignore)

    async def on_started(*_):
        starts.append(time.monotonic())

    async def on_stopped(*_):
        stops.append(time.monotonic())

    controller.add_event_handler("on_user_turn_started", on_started)
    controller.add_event_handler("on_user_turn_stopped", on_stopped)

    async def audio(ms):
        for _ in range(ms // CHUNK_MS):
            await controller.process_frame(
                InputAudioRawFrame(audio=CHUNK, sample_rate=SAMPLE_RATE, num_channels=1))
            await asyncio.sleep(CHUNK_MS / 1000)

    async with running_controller(controller):
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await audio(300)
        await controller.process_frame(InterimTranscriptionFrame("set up wifi", "u", "t", None))
        await audio(300)
        await controller.process_frame(VADUserStoppedSpeakingFrame(stop_secs=ENDPOINT_STOP_SECS))
        await audio(100)
        await controller.process_frame(wv.taken(_final("set up wifi", 1)))
        await audio(600)
        after = len(stops)
        # The next words open a turn of their own: barge-in is reachable.
        await controller.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await controller.process_frame(InterimTranscriptionFrame("say it again", "u", "t", None))
        return len(starts), after, closer.aggregator.resets


def test_a_taken_transcript_closes_the_turn_it_opened():
    starts, stops, resets = asyncio.run(_turn(with_closer=True))
    assert stops == 1 and starts == 2 and resets == 1


def test_without_the_closer_the_turn_hangs_open():
    """Why the closer exists: the emptied final pays the stop strategy's segment debt but
    gives it no words, and a turn without words does not end -- nor can a new one start."""
    starts, stops, _ = asyncio.run(_turn(with_closer=False))
    assert stops == 0 and starts == 1


# ------------------------------------------------------------------ telling the model

# The user's own network password, as typed on the setup page. The voice never has it
# (status.json does not carry it); it is planted in a status below to prove that a
# field the note does not name never reaches it.
USER_PASSWORD = "hunter2-lounge"


class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


def _notes(quiet_secs=0.02):
    """The session's ClientNotes over a real context, gate and trigger, off-pipeline (as
    test_client_notes builds it)."""
    context = LLMContext([{"role": "system", "content": "persona"},
                          {"role": "user", "content": "Can you set up the Wi-Fi?"}])
    # A budget longer than the debounce: shorter, the gate's answer is an instant False.
    gate = FollowupGate(quiet_secs=quiet_secs, max_wait=quiet_secs + 0.3, claim_secs=0.3)
    notes = ClientNotes(context, gate, FollowupTrigger())
    notes.task = _Task()

    async def ignore(frame, direction=FrameDirection.DOWNSTREAM):
        pass
    notes.push_frame = gate.push_frame = ignore
    notes.create_task = lambda coro, *a, **k: asyncio.get_running_loop().create_task(coro)
    return notes, context


def _notices(context):
    return [m["content"] for m in context.get_messages()
            if str(m.get("content", "")).startswith(SYSTEM_NOTICE_TAG)]


async def _settle(notes):
    for _ in range(25):
        await asyncio.sleep(0.02)
        if not notes._pending and (notes._drainer is None or notes._drainer.done()):
            return


async def _ask_did_it_work(notes, context):
    """The user's next turn: its completion takes whatever notice is still pending."""
    context.add_message({"role": "user", "content": "Did it work?"})
    await notes.process_frame(LLMContextFrame(context=context), FrameDirection.DOWNSTREAM)


async def _ends(path, outcome):
    """Write the unit's statuses from ap_up to `outcome`'s last word."""
    if outcome == "connected":
        _write(path, phase="joining", target="#guinealounge")
        await asyncio.sleep(0.03)
        # A password field planted: the note names the network, never a field it is not
        # built from.
        _write(path, phase="connected", target="#guinealounge", password=USER_PASSWORD)
    elif outcome == "timeout":
        _write(path, phase="failed", target="#guinealounge",
               reason="the password was not accepted")
        await asyncio.sleep(0.03)
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.03)
        _write(path, phase="timeout")
    elif outcome in ("stopped", "error"):
        _write(path, phase=outcome, **({"reason": "the setup page could not start"}
                                       if outcome == "error" else {}))


NOTICE = SYSTEM_NOTICE_TAG + "\nWi-Fi setup ended: "
ENDINGS = {
    "connected": 'connected to "#guinealounge"; the box is online.',
    "timeout": "it timed out; the box's Wi-Fi is back as it was before. The last attempt "
               "to join failed: the password was not accepted.",
    "stopped": "it was stopped from outside before it finished; the box's Wi-Fi is back as "
               "it was before.",
    "error": "it failed: the setup page could not start.",
}


@pytest.mark.parametrize("outcome", sorted(ENDINGS))
def test_the_model_hears_how_a_setup_it_started_ended(outcome, fast):
    """Issue #97: the tool hands over and the voice speaks everything after, out of the
    context. Each ending puts one line in, with the network's name and no password."""
    path = _path()

    async def run():
        notes, context = _notes(quiet_secs=5.0)  # no quiet moment: the next turn takes it
        v = Voice(path, online=True)
        v.notes = notes
        await v.begin(by_model=True)
        assert await v._heard("yes")
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.05)
        assert _notices(context) == [], "a notice before the end"
        await _ends(path, outcome)
        if outcome in ("stopped", "error"):
            v.alive = False
        await asyncio.sleep(0.2)
        assert v.state == "idle"
        await _ask_did_it_work(notes, context)
        return v, notes, context

    v, notes, context = asyncio.run(run())
    msgs = [m["content"] for m in context.get_messages()]
    assert msgs[-1] == "Did it work?", "the notice became the turn the model answers"
    assert msgs[-2] == NOTICE + ENDINGS[outcome]
    assert _notices(context) == [msgs[-2]], "one line per ending"
    for secret in (DETAILS["password"], USER_PASSWORD):
        assert secret not in "\n".join(msgs)
    assert notes.task.queued == [], "the notice ran a turn of its own"
    assert not any("ended" in s for s in v.said), "the notice was spoken"


def test_the_notice_waits_for_a_quiet_moment_and_runs_no_turn(fast):
    """The note-path's other moment: nobody speaks, the notice is appended at a quiet
    moment -- and nothing answers it until the user does."""
    path = _path()

    async def run():
        notes, context = _notes()
        v = Voice(path, online=True)
        v.notes = notes
        await v.begin(by_model=True)
        assert await v._heard("yes")
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.05)
        await _ends(path, "connected")
        await asyncio.sleep(0.2)
        await _settle(notes)
        return notes, context

    notes, context = asyncio.run(run())
    assert context.get_messages()[-1]["content"] == NOTICE + ENDINGS["connected"]
    assert notes.task.queued == []


@pytest.mark.parametrize("answers, ending", [
    (["cancel"], "the user cancelled it; the box's Wi-Fi is back as it was before."),
    (["no"], "the user said no, so it never started."),
    (["what's the weather", "tell me a joke"], "the user did not confirm, so it never started."),
])
def test_a_setup_the_model_started_that_never_ran_or_was_cancelled_is_told(answers, ending, fast):
    """The model was told setup "is asking the user to confirm": a no, a cancel or a
    question that never gets an answer end it too."""
    path = _path()

    async def run():
        notes, context = _notes(quiet_secs=5.0)
        v = Voice(path, online=True)
        v.notes = notes
        await v.begin(by_model=True)
        if answers == ["cancel"]:
            assert await v._heard("yes")
            _write(path, phase="ap_up", **DETAILS)
            await asyncio.sleep(0.05)
        for text in answers:
            await v._heard(text)
        assert v.state == "idle"
        await _ask_did_it_work(notes, context)
        return context

    context = asyncio.run(run())
    assert _notices(context) == [NOTICE + ending]


def test_a_lapsed_question_is_told_ahead_of_the_words_that_follow(fast):
    async def run():
        notes, context = _notes(quiet_secs=5.0)
        clock = Clock()
        v = Voice(_path(), online=True, clock=clock)
        v.notes = notes
        await v.begin(by_model=True)
        clock.now += wv.CONFIRM_SECS + 1
        assert not await v._heard("what time is it")  # the model's again
        await _ask_did_it_work(notes, context)
        return context

    context = asyncio.run(run())
    assert _notices(context) == [NOTICE + "the user did not confirm, so it never started."]


@pytest.mark.parametrize("outcome", ["timeout", "stopped", "error"])
def test_an_offline_phrase_setup_that_leaves_the_box_offline_adds_nothing(outcome, fast):
    """The phrase works only with no internet; ending any way but connected leaves the
    box offline: no model can read the line, and later it would be stale news."""
    path = _path()

    async def run():
        notes, context = _notes(quiet_secs=5.0)
        v = Voice(path)  # offline
        v.notes = notes
        await _running(v, path)
        await _ends(path, outcome)
        if outcome != "timeout":
            v.alive = False
        await asyncio.sleep(0.2)
        assert v.state == "idle"
        await _ask_did_it_work(notes, context)
        return notes, context

    notes, context = asyncio.run(run())
    assert _notices(context) == [] and notes._pending == []


def test_an_offline_phrase_setup_that_connects_is_told(fast):
    """...but connected, the box is online and the model is back: it hears it."""
    path = _path()

    async def run():
        notes, context = _notes(quiet_secs=5.0)
        v = Voice(path)
        v.notes = notes
        await _running(v, path)
        await _ends(path, "connected")
        await asyncio.sleep(0.2)
        await _ask_did_it_work(notes, context)
        return context

    assert _notices(asyncio.run(run())) == [NOTICE + ENDINGS["connected"]]


def test_without_notes_there_is_no_one_to_tell(fast):
    """A session without the notes path (no LLM context to reach): setup still ends
    cleanly and says so aloud."""
    path = _path()

    async def run():
        v = Voice(path, online=True)
        assert v.notes is None
        await v.begin(by_model=True)
        assert await v._heard("yes")
        _write(path, phase="ap_up", **DETAILS)
        await asyncio.sleep(0.05)
        await _ends(path, "connected")
        await asyncio.sleep(0.2)
        return v

    v = asyncio.run(run())
    assert v.state == "idle" and "online again" in v.said[-1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
