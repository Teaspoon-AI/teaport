#
# The call experience (issue #111), end to end: the REAL brain (teaport-brain, its SIP
# front-end and /talk in one process) against the fake gateway with auto_answer off
# (fake_sip_gateway.py, --no-auto-answer), a stand-in engine + LLM (fake_engine.py), and
# /talk clients that stream a microphone at real time like the OpenClaw relay and the
# local audio bridge do.
#
#   1. Ringing with a head start: the brain answers (call.answer) only after
#      SIP_ANSWER_AFTER_SECS, with the greeting already worded, and it plays at once.
#   2. A call during a Talk session: the agent asks in the session; the user says yes;
#      the Talk session goes on hold (its STT handed to the call, its socket and
#      context kept), the call is answered and greeted; a NEW Talk session meanwhile is
#      refused with the spoken busy line; after the hangup the Talk session is back
#      ("Sorry about that — where were we?").
#   3. The user says no: the call is never answered -- no call.answer, no busy line, no
#      hangup -- and rings until the caller gives up; the conversation goes on.
#   4. Nobody answers the question: the default (TEAPORT_CALL_PROMPT_DEFAULT=take)
#      picks up, and the conversation comes back after.
#   5. The same with the room mic (the local audio bridge's session): held, with the
#      bridge told ({"type": "hold"}), and resumed.
#   6. A gateway that answers by itself (auto_answer=true): nobody is asked about a
#      call already connected; it is taken, the conversation held and resumed.
#   7. A brain that connects while a call rings (replayed `early`) answers it.
#   And in 2: at DEBUG, the caller id never reaches the brain's journal; a second call
#   during the call is turned away by the gateway (486) and the first goes on.
#
# Run: python test_call_experience.py
#
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

import numpy as np  # noqa: E402
import soxr  # noqa: E402
import websockets  # noqa: E402

from fake_engine import GREETING_REPLY, TOOL_REPLY, FakeEngine  # noqa: E402
from fake_sip_gateway import FakeSipGateway, load_wav  # noqa: E402

QUESTION_WAV = os.path.join(BRAIN, "test", "question.wav")
ANSWER_AFTER = 1.5
CALLER = "+15551234567"
CALLER_SAID = "+1 555 123 4567"   # sip_server.caller_id, as the question says it
RELAY_RATE = 24000
FRAME_BYTES = RELAY_RATE * 2 // 50  # 20 ms of PCM16 mono at 24 kHz


def _short_tmpdir() -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="cx-")
    if len(d) > 90:  # AF_UNIX paths stop at 107 bytes
        shutil.rmtree(d)
        d = tempfile.mkdtemp(prefix="cx-", dir="/tmp")
    return d


def phone_answers(messages, offered):
    """The fake model's reading of a reply to "should I step away?": the tool call."""
    if "answer_phone_call" not in offered:
        return None
    last = messages[-1].get("content") or ""
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))
    if "take it" in last:
        return "answer_phone_call", {"take": True}
    if "let it ring" in last:
        return "answer_phone_call", {"take": False}
    return None


class Rig:
    """Fake engine + fake gateway (auto_answer off) in this process, teaport-brain as a
    child process with /talk on a free port."""

    def __init__(self, transcripts=None, *, auto_answer=False, start_brain=True, **env):
        self.dir = _short_tmpdir()
        self.sock = os.path.join(self.dir, "gw.sock")
        self.engine = FakeEngine(transcripts=transcripts, tool_call=phone_answers)
        self.gw = FakeSipGateway(self.sock, auto_answer=auto_answer)
        self.env = env
        self.brain = None
        self.log = os.path.join(self.dir, "brain.log")
        self.port = None
        self._start_brain = start_brain

    async def __aenter__(self):
        await self.engine.start()
        await self.gw.start()
        if self._start_brain:
            await self.start_brain()
        return self

    async def start_brain(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GATEWAY_TOKEN", "TEAPORT_AUDIO_DUMP")}
        env.update(self.engine.env_for())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env.update({"PYTHONPATH": BRAIN, "TEAPORT_SIP_SOCKET": self.sock,
                    "BRAIN_PORT": str(self.port), "LOGURU_LEVEL": "INFO",
                    "SIP_ANSWER_AFTER_SECS": str(ANSWER_AFTER),
                    "TEAPORT_CALL_PROMPT_SECS": "20", "TEAPORT_CALL_PROMPT_DEFAULT": "take"})
        env.update(self.env)
        with open(self.log, "w") as out:
            self.brain = subprocess.Popen(
                [sys.executable, "-u", "-m", "teaport_brain.gateway_server",
                 "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=BRAIN, env=env, stdout=out, stderr=subprocess.STDOUT)
        assert await self.gw.wait_brain(60), "teaport-brain never connected to the gateway"

    async def __aexit__(self, exc_type, *exc):
        await self.gw.close()
        if self.brain is not None:
            if self.brain.poll() is None:
                self.brain.terminate()
            try:
                self.brain.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.brain.kill()
                self.brain.wait()
        await self.engine.close()
        if os.getenv("CX_KEEP_LOG"):  # by hand: keep the brain's journal to read
            shutil.copy(self.log, os.getenv("CX_KEEP_LOG"))
        if exc_type is not None:
            with open(self.log, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-120:]
            print(f"--- {self.log} (tail) ---\n" + "".join(tail), file=sys.stderr)
        shutil.rmtree(self.dir, ignore_errors=True)

    def answers_sent(self):
        return [m for _, m in self.gw.controls if m.get("type") == "call.answer"]


_SPEECH = None


def speech() -> bytes:
    """Something said into a /talk microphone: real speech (the VAD has to hear it),
    24 kHz mono. What it transcribes as is the fake engine's script."""
    global _SPEECH
    if _SPEECH is None:
        pcm = np.frombuffer(load_wav(QUESTION_WAV), dtype="<i2")
        _SPEECH = soxr.resample(pcm, 16000, RELAY_RATE).astype("<i2").tobytes()
    return _SPEECH


class TalkClient:
    """A /talk client: its microphone streams 20 ms frames at real time (silence unless
    it says something), and it keeps everything the brain sends it."""

    def __init__(self, port: int, **query):
        self.url = f"ws://127.0.0.1:{port}/talk?" + urllib.parse.urlencode(query, safe=",")
        self.msgs: list[dict] = []
        self.audio = 0
        self.close_code = None
        self._mic = bytearray()
        self._tasks = []
        self.ws = None

    async def connect(self):
        self.ws = await websockets.connect(self.url, max_size=None)
        self._tasks = [asyncio.ensure_future(self._send()), asyncio.ensure_future(self._recv())]
        return self

    async def _send(self):
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        try:
            while True:
                if self._mic:
                    frame = bytes(self._mic[:FRAME_BYTES]).ljust(FRAME_BYTES, b"\0")
                    del self._mic[:FRAME_BYTES]
                else:
                    frame = bytes(FRAME_BYTES)
                await self.ws.send(frame)
                next_t += 0.02
                await asyncio.sleep(max(0.0, next_t - loop.time()))
        except websockets.ConnectionClosed:
            pass

    async def _recv(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    self.audio += len(m)
                else:
                    self.msgs.append(json.loads(m))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_code = self.ws.close_code if self.ws.close_code is not None else -1

    def say(self):
        self._mic += speech()

    def said(self, role="assistant"):
        return [m.get("text") for m in self.msgs if m.get("type") == "transcript"
                and m.get("role") == role and m.get("final")]

    def holds(self):
        return [m.get("on") for m in self.msgs if m.get("type") == "hold"]

    @property
    def closed(self):
        return self.close_code is not None

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


async def wait_until(pred, timeout=10.0, step=0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


def asked(client):
    return [t for t in client.said() if "calling me" in t]


async def _greeted_talk(rig, **query) -> TalkClient:
    c = await TalkClient(rig.port, **query).connect()
    assert await wait_until(lambda: any(m.get("type") == "ready" for m in c.msgs), 30), (
        f"the Talk session never became ready: {c.msgs}")
    assert await wait_until(lambda: GREETING_REPLY in c.said(), 15), c.msgs
    return c


# --- 1. ringing with a head start -----------------------------------------------------

async def test_the_brain_lets_it_ring_then_answers_with_the_greeting_ready():
    async with Rig() as rig:
        t0 = time.monotonic()
        call = await rig.gw.place_call(CALLER, answer_timeout=30)
        answered_after = time.monotonic() - t0
        assert call.confirmed.is_set(), f"never answered (ended by {call.ended_by})"
        assert len(rig.answers_sent()) == 1, rig.gw.controls
        assert answered_after >= ANSWER_AFTER - 0.1, (
            f"answered {answered_after:.2f} s after the call came in: no rings first")
        # The greeting was worded while it rang (the out-of-band completion, before the
        # answer), and plays at once.
        assert rig.engine.llm_requests, "nothing was asked of the model while it rang"
        onset = await call.wait_bot_speech(0.0, 10)
        # Ready-made: TTS alone stands between the answer and it (~0.2 s here), well
        # inside the completion it would otherwise wait for. Room for a loaded runner.
        assert onset is not None and onset < 3.0, f"greeting {onset} s after the answer"
        assert GREETING_REPLY in " ".join(rig.engine.tts_texts), rig.engine.tts_texts
        assert len(rig.engine.llm_requests) == 1, (
            "the greeting was asked for again after the answer")
        await call.hangup()
        assert await wait_until(lambda: rig.engine.stt_active == 0), "STT slot still held"
        assert rig.gw.violations == [], rig.gw.violations


# --- 2-4. a call during a Talk session ---------------------------------------------------

CALLER_FORMS = ("15551234567", "5551234567", "555 123 4567")


async def test_a_talk_user_who_says_yes_is_held_and_comes_back_after_the_call():
    # At DEBUG, as the unit runs (nothing sets LOGURU_LEVEL): the caller is said, and
    # must still never reach the journal.
    async with Rig(transcripts=["Sure, take it."], LOGURU_LEVEL="DEBUG") as rig:
        talk = await _greeted_talk(rig, client="openclaw:a")
        placing = asyncio.ensure_future(rig.gw.place_call(CALLER, answer_timeout=60))
        assert await wait_until(lambda: asked(talk), 15), f"never asked: {talk.said()}"
        assert CALLER_SAID in asked(talk)[0], asked(talk)
        assert rig.answers_sent() == [], "the call was answered before the user said yes"

        talk.say()
        call = await asyncio.wait_for(placing, 30)
        assert call.confirmed.is_set(), f"never answered (ended by {call.ended_by})"
        assert ("answer_phone_call", {"take": True}) in rig.engine.tool_calls
        assert any("back in a moment" in t for t in talk.said()), talk.said()
        assert talk.holds() == [True], talk.msgs
        assert not talk.closed, "the Talk session was ended, not held"
        assert await call.wait_bot_speech(0.0, 10) is not None, "the caller was not greeted"
        assert rig.engine.stt_active == 1, "the call and the held session both hold STT"

        # A second call meanwhile is turned away by the gateway (486); this one goes on.
        second = await rig.gw.place_call("+15550002222", answer_timeout=5)
        assert second.busy and call.active

        # A new Talk session during the call: told the agent is on a call.
        other = await TalkClient(rig.port, client="openclaw:b").connect()
        assert await wait_until(lambda: other.closed, 20), "the second session was let in"
        assert other.close_code == 4004, other.close_code
        assert any("on a phone call" in t for t in other.said()), other.msgs
        assert other.audio > 0, "the busy line was captioned but not heard"
        await other.close()

        await call.hangup()
        assert await wait_until(lambda: talk.holds() == [True, False], 15), talk.msgs
        assert await wait_until(
            lambda: any("where were we" in t for t in talk.said()), 15), talk.said()
        assert await wait_until(lambda: rig.engine.stt_active == 1, 10), (
            "the Talk session did not take the engine back")
        assert not talk.closed
        await talk.close()
        assert rig.gw.violations == [], rig.gw.violations
        with open(rig.log, encoding="utf-8", errors="replace") as f:
            journal = f.read()
        assert "| DEBUG" in journal, "the brain did not log at DEBUG"
        assert "<caller>" in journal, "the question never reached the journal masked"
        leaks = [form for form in CALLER_FORMS if form in journal]
        assert not leaks, f"the caller id reached the journal: {leaks}"
        # Nor a piece of it: a preview cut mid-number ("… +1 555 123 45…").
        number = "15551234567"
        for line in journal.splitlines():
            digits = re.sub(r"\D", "", line.split(" - ", 1)[-1])
            pieces = [number[k:k + 6] for k in range(len(number) - 5)
                      if number[k:k + 6] in digits]
            assert not pieces, f"part of the caller id reached the journal: {line}"


async def test_a_call_the_gateway_answers_itself_is_taken_without_asking():
    """auto_answer=true (a conf from before #111): the caller is connected at once, so
    the conversation is not asked -- the caller would sit on a silent line through the
    question -- the call is taken, and the conversation held and given back."""
    async with Rig(auto_answer=True) as rig:
        talk = await _greeted_talk(rig, client="openclaw:a")
        call = await rig.gw.place_call(CALLER, ring_s=0.2)
        assert await call.wait_bot_speech(0.0, 15) is not None, "the caller was not greeted"
        assert asked(talk) == [], f"asked about a call already answered: {talk.said()}"
        assert any("back in a moment" in t for t in talk.said()), talk.said()
        assert talk.holds() == [True] and not talk.closed
        await call.hangup()
        assert await wait_until(
            lambda: any("where were we" in t for t in talk.said()), 15), talk.said()
        await talk.close()


async def test_a_brain_that_connects_while_a_call_rings_answers_it():
    """The gateway replays a ringing call as `early` (180 Ringing sent): the brain that
    connects then rings it on and answers it."""
    async with Rig(start_brain=False) as rig:
        placing = asyncio.ensure_future(rig.gw.place_call(CALLER, answer_timeout=90))
        assert await wait_until(lambda: rig.gw.call is not None
                                and rig.gw.call.state == "early", 5)
        await rig.start_brain()
        replay = [m for n, m in rig.gw.sent if n == 1][:3]
        assert [(m["type"], m.get("state")) for m in replay] == [
            ("hello", None), ("call.incoming", None), ("call.state", "early")], replay
        call = await asyncio.wait_for(placing, 60)
        assert call.confirmed.is_set() and len(rig.answers_sent()) == 1
        assert await call.wait_bot_speech(0.0, 10) is not None
        await call.hangup()


async def test_a_talk_user_who_says_no_leaves_the_call_ringing_until_the_caller_gives_up():
    async with Rig(transcripts=["No, let it ring."]) as rig:
        talk = await _greeted_talk(rig, client="openclaw:a")
        placing = asyncio.ensure_future(rig.gw.place_call(CALLER, answer_timeout=ANSWER_AFTER + 8))
        assert await wait_until(lambda: asked(talk), 15), f"never asked: {talk.said()}"
        talk.say()
        assert await wait_until(
            lambda: ("answer_phone_call", {"take": False}) in rig.engine.tool_calls, 20)
        assert await wait_until(lambda: TOOL_REPLY in talk.said(), 10), talk.said()

        call = await asyncio.wait_for(placing, ANSWER_AFTER + 15)
        assert not call.confirmed.is_set() and call.ended_by == "caller", (
            f"the call was not left ringing: confirmed={call.confirmed.is_set()} "
            f"ended_by={call.ended_by}")
        assert rig.answers_sent() == [], "answered after the user said not to"
        assert not any(m.get("type") == "call.hangup" for _, m in rig.gw.controls), (
            "the caller was hung up on")
        assert rig.gw.stray_audio_out == 0, "something was played to the unanswered call"
        assert talk.holds() == [] and not talk.closed
        assert rig.engine.stt_active == 1                 # still the Talk session's
        await talk.close()


async def test_nobody_answering_the_question_takes_the_call_by_default():
    async with Rig(TEAPORT_CALL_PROMPT_SECS="1.5") as rig:
        talk = await _greeted_talk(rig, client="openclaw:a")
        call = await rig.gw.place_call(CALLER, answer_timeout=40)
        assert asked(talk), talk.said()
        assert call.confirmed.is_set(), f"never answered (ended by {call.ended_by})"
        assert rig.engine.tool_calls == []                # nobody said anything
        assert talk.holds() == [True] and not talk.closed
        assert await call.wait_bot_speech(0.0, 10) is not None
        await call.hangup()
        assert await wait_until(
            lambda: any("where were we" in t for t in talk.said()), 15), talk.said()
        await talk.close()


# --- 5. the room mic ----------------------------------------------------------------

async def test_the_room_conversation_is_held_for_the_call_and_resumed():
    async with Rig(TEAPORT_CALL_PROMPT_SECS="1.5") as rig:
        # The local audio bridge's session without wake words: asleep until someone in
        # the room has spoken, so it speaks first.
        room = await TalkClient(rig.port, features="volume,restart,local",
                                client="local-audio", keepalive="45").connect()
        assert await wait_until(lambda: any(m.get("type") == "ready" for m in room.msgs), 30)
        room.say()
        assert await wait_until(lambda: room.said("user"), 20), room.msgs
        assert await wait_until(lambda: any("Paris" in t for t in room.said()), 20), room.said()

        call = await rig.gw.place_call(CALLER, answer_timeout=40)
        assert asked(room), f"the room was not asked: {room.said()}"
        assert call.confirmed.is_set()
        assert room.holds() == [True] and not room.closed
        await call.hangup()
        assert await wait_until(lambda: room.holds() == [True, False], 15), room.msgs
        assert await wait_until(
            lambda: any("where were we" in t for t in room.said()), 15), room.said()
        assert not room.closed
        await room.close()


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run():
        for fn in aio:
            t0 = time.monotonic()
            await fn()
            print(f"  ok {fn.__name__} ({time.monotonic() - t0:.1f}s)", flush=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
