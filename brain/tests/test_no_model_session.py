#
# Sessions work without a model (#277): with no key or no endpoint address, a session is
# still built -- the same builder, the LLM stage left out -- and runs speech recognition,
# the fixed-phrase processors (Wi-Fi setup) and the fixed lines. Every request for a reply
# gets one fixed, translated line, once per window (llm_error_speaker.py, NO MODEL AT ALL):
#
#   * the room, with spoken Wi-Fi setup on and the box offline: how to get it online --
#     and "set up Wi-Fi" starts setup mode, end to end, online or not;
#   * anywhere else: that the account for its answers is not set up;
#   * a phone call: a short neutral line heard in full, even over the caller's "hello?",
#     then the call ends (never setup instructions);
#   * a key written meanwhile: "I'm ready now", and the session ends -- through the
#     client's restart where it has one (the room opens the next one awake) -- so the next
#     one has the model.
#   No reaction to a client note, no room conversation kept from a session without a
#   model; /health's "answers" says "unconfigured" (or that the address isn't valid,
#   quietly), and the CLI says so.
#
# In process: the session's own pipeline from the Wi-Fi processor (or the user
# aggregator) down, the engine's synthesis stubbed. End to end: the real brain against the
# fake engine and the fake SIP gateway (a call, and a Talk client), with no key at all.
#
# All utterances are synthetic.
#
# Run: python test_no_model_session.py   (or via pytest test_suite.py)
#
import asyncio
import json
import os
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")

from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from tempdirs import tempdir  # noqa: E402

# No model: no endpoint, no key, and a HOME with no key file in it. (The helpers imported
# below set defaults for both; they are taken away again after.)
_HOME = tempdir("teaport-nomodel-home-")


def _no_model():
    os.environ.pop("LLM_BASE_URL", None)
    os.environ.pop("LLM_API_KEY", None)
    os.environ["HOME"] = _HOME


from pipecat.frames.frames import (  # noqa: E402
    LLMRunFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.llm_service import LLMService  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from test_post_tool_under_speech import ScriptedStop, TalkTransport, _segments  # noqa: E402

_no_model()

from teaport_brain import i18n, llm_error_speaker as les, services  # noqa: E402
from teaport_brain import wifi_voice  # noqa: E402
from teaport_brain.agent_session import AgentSession, build_agent_session  # noqa: E402
from teaport_brain.llm_error_speaker import LLMErrorSpeaker  # noqa: E402
from teaport_brain.wifi_voice import WifiSetupVoice  # noqa: E402

EN = i18n.get("en")
STEP_S = 10.0


async def until(predicate, timeout=STEP_S):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a condition"
        await asyncio.sleep(0.01)


def _key_file(text: str | None) -> None:
    path = os.path.join(_HOME, ".config", "teaport", "llm_key")
    if text is None:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


# ---- what is missing ---------------------------------------------------------------------

def test_what_is_missing_is_said_and_make_llm_refuses_with_it():
    _no_model()
    assert services.model_missing() == "no endpoint address"
    assert services.answers_status() == {"state": "unconfigured",
                                         "reason": "no endpoint address"}
    try:
        services.make_llm()
        raise AssertionError("make_llm built a model with no endpoint")
    except services.NoModel:
        pass
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    try:
        assert services.model_missing() == "no key"
        try:
            services.make_llm()
            raise AssertionError("make_llm built a model with no key")
        except services.NoModel:
            pass
        # The key file is read as asked: one written meanwhile counts at once.
        _key_file("sk-synthetic-test")
        assert services.model_missing() is None
        assert services.answers_status()["state"] in ("untried", "ok")
    finally:
        _key_file(None)
        _no_model()


def test_an_address_that_is_not_one_is_said_so_and_quietly():
    """A malformed LLM_BASE_URL (ftp://...) is not "no address", and asking about it --
    every /health, every request a session without a model takes -- logs nothing."""
    from loguru import logger
    seen = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    os.environ["LLM_BASE_URL"] = "ftp://synthetic.invalid/v1"
    try:
        for _ in range(3):
            assert services.model_missing() == "the endpoint address isn't valid"
            assert services.answers_status()["reason"] == "the endpoint address isn't valid"
    finally:
        logger.remove(sink)
        _no_model()
    assert not [m for m in seen if "LLM_BASE_URL" in m], seen


# ---- the session is built, without the LLM stage ------------------------------------------

def test_a_session_builds_without_a_model_and_nothing_that_needs_one():
    _no_model()
    session = build_agent_session(TalkTransport(), front_end="talk", context_notes=True)
    assert session.llm is None
    procs = _processors(session)
    assert not any(isinstance(p, LLMService) for p in procs), "an LLM in a no-model session"
    speakers = [p for p in procs if isinstance(p, LLMErrorSpeaker)]
    assert len(speakers) == 1 and speakers[0].no_model, speakers
    assert speakers[0].end is not None, "nothing ends the session once a model can be made"
    assert session.call_prompt is None, "a call prompt with no model to ask it"
    assert session.owed.judge is None and session.owed.gist is None


def test_a_client_note_gets_no_reaction_without_a_model():
    """respond:true would queue a run, answered by the not-set-up line out of nowhere:
    with no model every note goes in as context only."""
    _no_model()
    session = build_agent_session(TalkTransport(), front_end="talk", context_notes=True)
    notes = session.client_notes
    notes.add_notice("Synthetic notice.", respond=True)
    assert notes._pending and notes._pending[-1].respond is False


def test_a_room_conversation_without_a_model_is_neither_kept_nor_continued():
    """Its turns went unanswered: saved, the next session with a model would answer them
    out of nowhere and skip its greeting. And one kept from before is left for that one."""
    from teaport_brain.wake_gate import MicConversation, WakeGate
    _no_model()
    store = MicConversation()
    store.save([{"role": "user", "content": "synthetic words from a session with a model"}])
    gate = WakeGate(["hey teaport"], asleep=True, store=store)
    session = build_agent_session(TalkTransport(), front_end="room", wake_gate=gate,
                                  client_features=frozenset({"local"}))
    assert gate.remembers is False

    async def run():
        async def push(_frame):
            pass
        await gate.final("hey teaport what time is it", push)
    asyncio.run(run())
    users = [m["content"] for m in session.context.get_messages() if m["role"] == "user"]
    assert "synthetic words from a session with a model" not in users, users
    session.context.add_message({"role": "user", "content": "synthetic unanswered turn"})
    gate.ended()
    assert store.take(3600) == [{"role": "user",
                                 "content": "synthetic words from a session with a model"}]


def _processors(session):
    inner = next(p for p in session.task.pipeline.processors if isinstance(p, Pipeline))
    return [p for p in inner.processors if not type(p).__name__.startswith("Pipeline")]


# ---- in process: the session's own pipeline, from the top of what is tested down -----------

class Rig:
    """The session's processors from `top` down, running; the engine's synthesis stubbed,
    the turn's stop scripted. `played` is every word the transport played."""

    def __init__(self, session, top):
        self.session = session
        procs = _processors(session)
        first = next(i for i, p in enumerate(procs) if isinstance(p, top))
        self.procs = procs[first:]
        self.user = next(p for p in self.procs if isinstance(p, LLMUserAggregator))
        self.assistant = next(p for p in self.procs if isinstance(p, LLMAssistantAggregator))
        self.speaker = next(p for p in self.procs if isinstance(p, LLMErrorSpeaker))
        controller = self.user._user_turn_controller
        self.stop = ScriptedStop()
        controller._user_turn_strategies = UserTurnStrategies(
            start=controller._user_turn_strategies.start, stop=[self.stop])
        session.tts._synth_text = _segments
        self.played: list[str] = []
        process = self.assistant.process_frame

        async def recording(frame, direction):
            await process(frame, direction)
            if isinstance(frame, TTSTextFrame):
                self.played.append(frame.text)
        self.assistant.process_frame = recording
        self.task = PipelineTask(Pipeline(self.procs), observers=[session.ledger],
                                 cancel_on_idle_timeout=False)
        self.speaker.end = self.task.stop_when_done
        self.started = asyncio.Event()

        @self.task.event_handler("on_pipeline_started")
        async def _started(_task, _frame):
            self.started.set()

    async def __aenter__(self):
        self._run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.started.wait(), STEP_S)
        return self

    async def __aexit__(self, *exc):
        if not self._run.done():
            await self.task.cancel()
        await self._run

    def heard(self) -> str:
        return _words(" ".join(self.played))

    async def final(self, text):
        """A final transcript, as the STT pushes one (no turn: Wi-Fi setup's words)."""
        f = TranscriptionFrame(text, "user", "t")
        f.finalized = True
        await self.task.queue_frames([f])

    async def say(self, text):
        """The user's turn: speech, its words, the verdict."""
        await self.task.queue_frames([VADUserStartedSpeakingFrame()])
        f = TranscriptionFrame(text, "user", "t")
        f.finalized = True
        await self.task.queue_frames([f, VADUserStoppedSpeakingFrame()])
        await until(lambda: self.user._user_turn_controller._user_turn)
        await self.stop.commit()


def _words(line: str) -> str:
    return wifi_voice._norm(line)  # words only: no case, no punctuation


async def _says(rig, line, timeout=STEP_S):
    await until(lambda: _words(line) in rig.heard(), timeout)


async def test_ordinary_speech_gets_the_line_once_per_window():
    _no_model()
    session = build_agent_session(TalkTransport(), front_end="talk")
    async with Rig(session, LLMUserAggregator) as rig:
        await rig.say("what is the weather like tomorrow")
        await _says(rig, les.NO_MODEL_TEXT)
        n = len(rig.played)
        await rig.say("and the day after that")
        await asyncio.sleep(1.0)
        assert len(rig.played) == n, f"said again inside the window: {rig.played[n:]}"
        # The window over (its clock moved back): said again.
        rig.speaker._said -= les._DEBOUNCE_SECS + 1
        await rig.say("tell me a joke")
        await until(lambda: rig.heard().count(_words(les.NO_MODEL_TEXT)) == 2)
        users = [m["content"] for m in session.context.get_messages() if m["role"] == "user"]
        assert users[-1] == "tell me a joke", users
        assert not [m for m in session.context.get_messages() if m["role"] == "assistant"], (
            "the fixed line went into the context")


async def test_the_line_is_said_in_the_voices_language():
    _no_model()
    session = build_agent_session(TalkTransport(), front_end="talk", language="es")
    async with Rig(session, LLMUserAggregator) as rig:
        await rig.say("hola que tal")
        await _says(rig, i18n.get("es")._(les.NO_MODEL_TEXT))


class _Unit:
    """The setup unit's helper and status file, faked; online or not as asked."""

    def __init__(self, online):
        self.calls = []
        self.online = online
        self.path = os.path.join(tempdir(), "status.json")

    async def run(self, action, unit):
        self.calls.append(action)
        return 0, ""

    async def is_online(self):
        return self.online

    async def alive(self):
        return True

    def write(self, **status):
        with open(self.path, "w") as f:
            json.dump({"run_id": "r1", "time": time.time(), **status}, f)


def _room_with_wifi(unit):
    wifi_voice_available = wifi_voice.available
    wifi_voice.available = lambda: True
    try:
        session = build_agent_session(TalkTransport(), front_end="room",
                                      client_features=frozenset({"local"}),
                                      context_notes=True)
    finally:
        wifi_voice.available = wifi_voice_available
    wifi = next(p for p in _processors(session) if isinstance(p, WifiSetupVoice))
    assert wifi.model is False
    wifi._run, wifi._online, wifi._alive = unit.run, unit.is_online, unit.alive
    wifi._status_path = unit.path
    return session, wifi


async def test_a_room_session_with_no_model_starts_setup_mode_on_the_phrase_online_or_not():
    """Acceptance: no key file, no LLM_BASE_URL; a room session builds, and "set up Wi-Fi"
    starts setup mode, end to end -- online too: there is no model to leave it to."""
    _no_model()
    was = wifi_voice.POLL_SECS
    wifi_voice.POLL_SECS = 0.02
    try:
        for online in (False, True):
            unit = _Unit(online)
            session, wifi = _room_with_wifi(unit)
            async with Rig(session, WifiSetupVoice) as rig:
                await rig.final("set up wifi")
                await _says(rig, EN._("Do you want to set up Wi-Fi? I'll go into setup mode "
                                      "and be offline for a few minutes while you do it on "
                                      "your phone. Say yes or no."))
                await rig.final("yes")
                await until(lambda: unit.calls == ["restart"])
                assert wifi.state == "running"
                unit.write(phase="ap_up", ssid="teaport-ab12", password="12345678",
                           screen=True, qr=True, url="http://10.42.0.1")
                await _says(rig, EN._("I'm in setup mode."))
                users = [m for m in session.context.get_messages() if m["role"] == "user"]
                assert users == [], f"setup's words reached the context: {users}"
                assert _words(les.NO_MODEL_WIFI_TEXT) not in rig.heard()
                await rig.final("cancel")
                await until(lambda: unit.calls == ["restart", "stop"])
    finally:
        wifi_voice.POLL_SECS = was


async def test_the_room_offline_is_told_how_to_get_online_and_its_echo_is_not_a_request():
    _no_model()
    unit = _Unit(online=False)
    session, wifi = _room_with_wifi(unit)
    async with Rig(session, WifiSetupVoice) as rig:
        await rig.say("what time is it")
        await _says(rig, les.NO_MODEL_WIFI_TEXT)
        # The line holds the start phrase: heard back while it plays, it is the box's own
        # voice, not a request (wifi_voice._echo, fed by the speaker).
        wifi._bot_speaking = True
        assert await wifi._heard('Say "set up Wi-Fi" to get me online.')
        assert wifi.state == "idle" and unit.calls == []


async def test_the_room_online_is_told_the_account_is_not_set_up():
    _no_model()
    session, _wifi = _room_with_wifi(_Unit(online=True))
    async with Rig(session, LLMUserAggregator) as rig:
        await rig.say("what time is it")
        await _says(rig, les.NO_MODEL_TEXT)


async def test_a_key_written_meanwhile_is_found_and_the_session_ends_for_the_next():
    _no_model()
    session = build_agent_session(TalkTransport(), front_end="talk")
    async with Rig(session, LLMUserAggregator) as rig:
        os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
        _key_file("sk-synthetic-test")
        try:
            await rig.say("are you there")
            # A Talk client opens the next session itself: it is told to.
            await _says(rig, les.NO_MODEL_READY_NEW_TEXT)
            await asyncio.wait_for(rig._run, STEP_S)   # ended: the next session has it
            assert _words(les.NO_MODEL_TEXT) not in rig.heard()
        finally:
            _key_file(None)
            _no_model()
    # And the next session is built with the model.
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    _key_file("sk-synthetic-test")
    try:
        assert build_agent_session(TalkTransport(), front_end="talk").llm is not None
    finally:
        _key_file(None)
        _no_model()


# ---- the greeting --------------------------------------------------------------------------

class _Task:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


async def test_a_greeting_asks_for_the_line_and_no_greeting_is_prepared():
    from pipecat.processors.aggregators.llm_context import LLMContext
    stt = type("STT", (), {"stt_available": True, "slot_busy": False})()
    s = AgentSession(task=_Task(), context=LLMContext([]), stt=stt, tts=None, llm=None,
                     ledger=None, followup_gate=None)
    await s.greet()
    assert [type(f) for f in s.task.queued] == [LLMRunFrame] and not s.should_end
    assert await s.prepare_greeting() is None


# ---- the speaker on its own ----------------------------------------------------------------

class _Gate:
    def __init__(self):
        self.calls = []

    def request_rendered(self):
        self.calls.append("rendered")

    def release_claim(self):
        self.calls.append("released")


class _Speaker(LLMErrorSpeaker):
    def __init__(self, **kw):
        super().__init__(no_model=True, **kw)
        self.said, self.pushed = [], []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)
        if isinstance(frame, TTSSpeakFrame):
            self.said.append(frame.text)

    def _spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)


async def _ask(speaker):
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    await speaker.process_frame(LLMContextFrame(context=LLMContext([])),
                                FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.05)


async def test_a_request_releases_the_window_as_a_model_would():
    gate = _Gate()
    speaker = _Speaker(gate=gate, lang_fn=lambda: "en-us")
    await _ask(speaker)
    assert speaker.said == [les.NO_MODEL_TEXT], speaker.said
    assert gate.calls == ["rendered", "released"], "the window left claimed"


def test_the_callers_line_holds_no_setup_instructions():
    for lang in i18n.catalogs():
        line = i18n.get(lang)._(les.NO_MODEL_CALL_TEXT)
        assert "Wi-Fi" not in line and "wifi" not in line.lower(), (lang, line)


async def test_the_ready_line_restarts_through_a_client_that_can_and_ends_otherwise():
    """The room's bridge restarts a session itself (restart_session): told before the
    line, it ends the session once the line has played and opens the next one awake,
    listening -- "ask me again" is true. A Talk client cannot: the session ends, and the
    line says to start a new conversation."""
    from pipecat.frames.frames import OutputTransportMessageUrgentFrame
    for restarts in (True, False):
        ended = []

        async def end():
            ended.append(True)
        speaker = _Speaker(model_back=lambda: True, end=end, restarts=restarts)
        await _ask(speaker)
        await _ask(speaker)
        if restarts:
            assert speaker.said == [les.NO_MODEL_READY_TEXT] and ended == [], speaker.said
            first = speaker.pushed[0]
            assert isinstance(first, OutputTransportMessageUrgentFrame)
            assert first.message["type"] == "client_tool"
            assert first.message["name"] == "restart_session", first.message
        else:
            assert speaker.said == [les.NO_MODEL_READY_NEW_TEXT] and ended == [True]
            assert not any(isinstance(f, OutputTransportMessageUrgentFrame)
                           for f in speaker.pushed)


def test_the_room_session_asks_its_bridge_to_restart_and_talk_does_not():
    _no_model()
    room = build_agent_session(TalkTransport(), front_end="room",
                               client_features=frozenset({"local", "restart"}))
    talk = build_agent_session(TalkTransport(), front_end="talk")
    speaker = lambda s: next(p for p in _processors(s) if isinstance(p, LLMErrorSpeaker))  # noqa: E731
    assert speaker(room)._restarts and not speaker(talk)._restarts


def test_no_spoken_line_names_the_machinery():
    import re
    for text in (les.NO_MODEL_TEXT, les.NO_MODEL_WIFI_TEXT, les.NO_MODEL_CALL_TEXT,
                 les.NO_MODEL_READY_TEXT, les.NO_MODEL_READY_NEW_TEXT):
        assert not re.search(r"\b(LLM|API|model|key|endpoint|settings|\d{3})\b", text,
                             re.I), text


# ---- /health and the CLI -------------------------------------------------------------------

async def test_health_says_no_model_is_configured_and_the_cli_shows_it():
    _no_model()
    from teaport_brain import gateway_server as gs
    answers = (await gs.health())["answers"]
    assert answers == {"state": "unconfigured", "reason": "no endpoint address"}, answers
    from test_llm_account_error import _doctor_answers, _status_answers
    served = {"ok": True, "answers": answers}
    shown = _status_answers(served)
    assert shown.startswith("not set up (no endpoint address)"), shown
    # The env file where TEAPORT_ETC puts it (the CLI test sets it to a temp etc/).
    assert "/etc/brain.env" in shown and "/etc/teaport/" not in shown, shown
    assert "llm_key" in shown, shown
    doctor = _doctor_answers(served)
    assert len(doctor) == 1 and doctor[0].startswith("FAIL answers: not set up"), doctor


# ---- end to end: the real brain, no key at all ---------------------------------------------

def _brain_env(engine):
    env = engine.env_for()
    env.update({"LLM_BASE_URL": "", "LLM_API_KEY": "", "HOME": _HOME})
    return env


async def test_end_to_end_a_caller_hears_the_short_line_and_is_hung_up():
    from fake_sip_gateway import run_call
    from test_sip_fake_gateway import Rig as SipRig
    from test_sip_fake_gateway import QUESTION_WAV
    from fake_engine import _WORD_S
    # Its whole length at the fake engine's pace: what the caller must hear before the
    # hangup (less a frame or two of slack).
    full = sum(len(s.split()) * _WORD_S for s in les.NO_MODEL_CALL_TEXT.split(". "))
    # A caller who waits, and one who talks from the moment the call is answered ("hello?
    # hello?"): the line must play out in full over them, not be cut and hung up on.
    for wav in (None, QUESTION_WAV):
        rig = SipRig()
        async with rig:
            rig.engine.env_for = (lambda env=_brain_env(rig.engine): env)
            brain = rig.start_brain()
            try:
                connected = await rig.gw.wait_brain(60)
                summary = (await run_call(rig.gw, wav=wav, wait_greeting=False,
                                          hangup="brain", hangup_after=40)
                           if connected else {})
            finally:
                # The brain first, while the gateway still reads: its reader then ends on
                # the brain's EOF.
                brain.kill()
                await asyncio.to_thread(brain.wait, 15)
        assert connected, "teaport-brain never connected to the gateway"
        assert summary.get("ended_by") == "brain", f"the brain did not hang up: {summary}"
        assert " ".join(rig.engine.tts_texts) == les.NO_MODEL_CALL_TEXT, rig.engine.tts_texts
        assert summary["bot_speech_s"] >= full - 0.1, (wav, summary["bot_speech_s"], full)
        assert rig.engine.llm_requests == []


async def test_end_to_end_a_talk_client_is_greeted_with_the_line_and_health_says_why():
    from test_call_experience import Rig as CxRig, TalkClient
    rig = CxRig(**{"LLM_BASE_URL": "", "LLM_API_KEY": "", "HOME": _HOME})
    async with rig:
        talk = await TalkClient(rig.port).connect()
        await until(lambda: any(m.get("type") == "ready" for m in talk.msgs), 30)
        await until(lambda: les.NO_MODEL_TEXT in " ".join(rig.engine.tts_texts), 20)
        with urllib.request.urlopen(f"http://127.0.0.1:{rig.port}/health", timeout=5) as r:
            answers = json.load(r)["answers"]
        assert answers["state"] == "unconfigured", answers
        assert rig.engine.llm_requests == []
        await talk.close()


async def test_end_to_end_with_a_front_door_the_line_says_open_my_settings():
    """#279: a box with a front door hears "open my settings" on Talk, so its no-model
    line says to; the phone line stays as it was (sip_server, the test above)."""
    from test_call_experience import Rig as CxRig, TalkClient
    rig = CxRig(**{"LLM_BASE_URL": "", "LLM_API_KEY": "", "HOME": _HOME,
                   "TEAPORT_FRONTDOOR_HOST": "teaport-9e35.local",
                   "TEAPORT_SETTINGS_PORT": "8443"})
    async with rig:
        talk = await TalkClient(rig.port).connect()
        await until(lambda: les.NO_MODEL_SETTINGS_TEXT in " ".join(rig.engine.tts_texts), 20)
        assert rig.engine.llm_requests == []
        await talk.close()


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    for name, fn in tests:
        t0 = time.monotonic()
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        print(f"  ok {name} ({time.monotonic() - t0:.1f}s)", flush=True)
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("ALL PASS")
