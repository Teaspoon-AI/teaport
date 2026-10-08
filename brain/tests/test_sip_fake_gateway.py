#
# The phone path end to end, with no phone line: the REAL brain (teaport-brain, its
# SIP front-end finding the gateway's socket through TEAPORT_SIP_SOCKET, as the unit
# runs it) against the fake gateway (fake_sip_gateway.py) and a stand-in engine + LLM
# (fake_engine.py).
#
# The brain's services are the real ones -- stt.py, engine_tts.py, the OpenAI LLM
# service, VAD, Smart Turn, the session arbiter -- talking their real wire protocols to
# fake_engine; only what is at the far end of each socket is fake. So this covers what
# the unit tests around it stub out: the serializer and transport on a real SEQPACKET
# socket, the per-call bring-up and teardown, the greeting, a turn transcribed and
# answered, the STT slot handed back at hangup, and a brain restarted under a live call.
#
#   1. A call is answered and greeted, the caller's question is transcribed and
#      answered, and hanging up frees the engine's single STT slot.
#   2. A caller who hangs up in the middle of the reply: the brain stops talking, frees
#      the slot, and the next call on the same connection is greeted from scratch.
#   3. The brain is killed mid-call. The gateway keeps the call up and replays it to the
#      next brain (hello, then call.incoming + call.state with "replay": true, before any
#      audio), which resumes it with the apology rather than a fresh hello, and goes on
#      to answer the caller.
#
# Plus the fake gateway's own contract, with a bare socket for a brain (4-8): the
# brain-side tests above are only as good as the harness is faithful.
#
# Run: python test_sip_fake_gateway.py
#
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pinned_pipecat  # noqa: F401,E402  — refuse to pass against the wrong pipecat

from fake_engine import DEFAULT_TRANSCRIPT, FakeEngine  # noqa: E402
import fake_sip_gateway  # noqa: E402
from fake_sip_gateway import (  # noqa: E402
    BYTES_PER_FRAME, MSG_AUDIO_IN, MSG_AUDIO_OUT, MSG_CONTROL, FakeSipGateway, run_call)

QUESTION_WAV = os.path.join(BRAIN, "test", "question.wav")


def _short_tmpdir() -> str:
    d = tempfile.mkdtemp(prefix="fsg-")
    if len(d) > 90:  # AF_UNIX paths stop at 107 bytes
        shutil.rmtree(d)
        d = tempfile.mkdtemp(prefix="fsg-", dir="/tmp")
    return d


class Rig:
    """Fake engine + fake gateway in this process, teaport-brain as a child process --
    the unit's own entry point, whose SIP front-end finds the gateway's socket and asks
    the session arbiter for the engine per call -- so a restart can be a real SIGKILL."""

    def __init__(self, **gw_kw):
        self.dir = _short_tmpdir()
        self.sock = os.path.join(self.dir, "gw.sock")
        self.engine = FakeEngine()
        self.gw = FakeSipGateway(self.sock, **gw_kw)
        self.brains: list[subprocess.Popen] = []
        self.logs: list[str] = []

    async def __aenter__(self):
        await self.engine.start()
        await self.gw.start()
        return self

    async def __aexit__(self, exc_type, *exc):
        await self.gw.close()
        for p in self.brains:
            # teaport-brain outlives a gateway (it looks for the socket again), so it is
            # stopped as systemd stops it.
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        await self.engine.close()
        if exc_type is not None:
            for path in self.logs:
                with open(path, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-60:]
                print(f"--- {path} (tail) ---\n" + "".join(tail), file=sys.stderr)
        shutil.rmtree(self.dir, ignore_errors=True)

    def start_brain(self) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GATEWAY_TOKEN", "TEAPORT_AUDIO_DUMP")}
        env.update(self.engine.env_for())
        with socket.socket() as s:   # a free port for /talk, which nothing here uses
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env.update({
            "PYTHONPATH": BRAIN,
            "TEAPORT_SIP_SOCKET": self.sock,
            "BRAIN_PORT": str(port),
            "LOGURU_LEVEL": "INFO",
        })
        log = os.path.join(self.dir, f"brain-{len(self.brains) + 1}.log")
        self.logs.append(log)
        with open(log, "w") as out:
            p = subprocess.Popen([sys.executable, "-u", "-m", "teaport_brain.gateway_server",
                                  "--host", "127.0.0.1", "--port", str(port)],
                                 cwd=BRAIN, env=env, stdout=out, stderr=subprocess.STDOUT)
        self.brains.append(p)
        return p


async def wait_until(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def user_texts(engine):
    """Every user message the LLM was asked to answer, newest request last."""
    return [m.get("content") for req in engine.llm_requests for m in req[-1:]
            if m.get("role") == "user"]


# --- 1-3. the real brain ----------------------------------------------------------

async def test_a_call_is_greeted_transcribed_and_answered():
    async with Rig() as rig:
        rig.start_brain()
        assert await rig.gw.wait_brain(60), "teaport-brain never connected to the gateway"
        rec = os.path.join(rig.dir, "bot.wav")
        s = await run_call(rig.gw, caller="+15551234567", wav=QUESTION_WAV,
                           hangup="done", hangup_after=60, record=rec)

        assert {"type": "hello", "proto": 0, "role": "brain"} in s["controls"], s["controls"]
        assert s["greeting_onset"] is not None and s["greeting_onset"] < 8, (
            f"no greeting within 8 s of answer: {s}")
        cue = user_texts(rig.engine)[0]
        assert cue.startswith("(") and "lost" not in cue, (
            f"the first completion was not the greeting cue: {cue!r}")
        assert rig.engine.stt_finals == [DEFAULT_TRANSCRIPT], rig.engine.stt_finals
        assert DEFAULT_TRANSCRIPT in user_texts(rig.engine), (
            f"the caller's words never reached the LLM: {user_texts(rig.engine)}")
        assert s["reply_onset"] is not None and s["reply_onset"] > s["wav_end"] - 0.5, s
        assert s["reply_latency"] < 4, f"answer took {s['reply_latency']} s: {s}"
        assert s["reply_end"] is not None, f"the answer never finished: {s}"
        assert s["ended_by"] == "caller", s
        assert s["violations"] == [] and s["playout_dropped"] == 0, s
        assert os.path.getsize(rec) > 44 + 16000 * 2, "nothing recorded"
        assert await wait_until(lambda: rig.engine.stt_active == 0), (
            "the engine's STT slot is still held after the hangup")


async def test_a_hangup_mid_reply_stops_the_bot_and_the_next_call_starts_fresh():
    async with Rig() as rig:
        brain = rig.start_brain()
        assert await rig.gw.wait_brain(60), "teaport-brain never connected to the gateway"
        s = await run_call(rig.gw, wav=QUESTION_WAV, hangup="mid-reply", mid_reply_s=0.8,
                           hangup_after=60)
        assert s["reply_onset"] is not None, s
        assert s.get("bot_talking_at_hangup") is True, f"hung up after the reply, not in it: {s}"
        assert s["ended_by"] == "caller", s

        # The brain stops sending for the dead call (a few in-flight frames at most).
        stray = rig.gw.stray_audio_out
        await asyncio.sleep(1.0)
        assert rig.gw.stray_audio_out - stray <= 5, (
            f"the brain kept playing to a hung-up call ({rig.gw.stray_audio_out} frames)")
        assert await wait_until(lambda: rig.engine.stt_active == 0), "STT slot still held"

        # Same connection, next caller: a fresh pipeline that greets from scratch.
        cues_before = len(user_texts(rig.engine))
        s2 = await run_call(rig.gw, caller="+15559876543", hangup_after=30)
        assert s2["greeting_onset"] is not None, f"the next call was not greeted: {s2}"
        cue = user_texts(rig.engine)[cues_before]
        assert cue.startswith("(") and "lost" not in cue, cue
        assert rig.gw.connects == 1 and brain.poll() is None, "the connection did not survive"
        assert rig.gw.violations == [], rig.gw.violations


async def test_a_brain_killed_mid_call_is_replayed_the_call_and_resumes_it():
    async with Rig() as rig:
        first = rig.start_brain()
        assert await rig.gw.wait_brain(60), "teaport-brain never connected to the gateway"
        call = await rig.gw.place_call("+15551234567")
        assert await call.wait_bot_speech(0, 20) is not None, "no greeting"
        assert await call.wait_bot_quiet(1.0, 20) is not None

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        assert await wait_until(lambda: rig.gw.brain_gone.is_set()), "EOF never seen"
        assert not call.ended.is_set(), "the gateway dropped the call with the brain"
        heard_before = len(call.bot_onsets)

        rig.start_brain()
        assert await rig.gw.wait_brain(60), "the restarted teaport-brain never reconnected"
        # The handshake, exactly: hello, then the call as it stands, all flagged.
        second = [m for n, m in rig.gw.sent if n == 2][:3]
        assert [m["type"] for m in second] == ["hello", "call.incoming", "call.state"], second
        assert all(m.get("replay") is True for m in second), second
        assert second[1]["call_id"] == call.call_id and second[1]["from"] == call.from_uri
        assert second[2]["state"] == "confirmed", second[2]

        # Resumed: the apology, not a hello from scratch.
        assert await call.wait_bot_speech(call.t, 30) is not None, (
            "the restarted brain never spoke to the replayed call")
        assert "lost" in user_texts(rig.engine)[-1], (
            f"the resumed call was greeted from scratch: {user_texts(rig.engine)[-1]!r}")
        assert len(call.bot_onsets) > heard_before
        await call.wait_bot_quiet(1.0, 20)

        # ...and it is an ordinary call from there: the caller is heard and answered.
        await call.play_wav(QUESTION_WAV)
        assert await call.wait_bot_speech(call.wav_end - 0.5, 30) is not None, (
            "the resumed call did not answer the caller")
        assert DEFAULT_TRANSCRIPT in user_texts(rig.engine)
        await call.hangup()
        assert await wait_until(lambda: rig.engine.stt_active == 0), "STT slot still held"
        assert rig.gw.violations == [], rig.gw.violations


# --- 4-8. the fake gateway itself, against a bare socket --------------------------

class _Client:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.s.connect(path)
        self.s.setblocking(False)

    async def recv(self, timeout=2.0):
        loop = asyncio.get_running_loop()
        data = await asyncio.wait_for(loop.sock_recv(self.s, 4096), timeout)
        if data and data[0] == MSG_CONTROL:
            return json.loads(data[1:])
        return data

    async def controls_until_audio(self, timeout=2.0):
        out = []
        while True:
            m = await self.recv(timeout)
            if isinstance(m, dict):
                out.append(m)
            elif m and m[0] == MSG_AUDIO_IN:
                assert len(m) == 1 + BYTES_PER_FRAME
                return out
            else:
                raise AssertionError(f"unexpected datagram {m[:8]!r}")

    def send(self, obj):
        self.s.send(bytes([MSG_CONTROL]) + json.dumps(obj).encode())

    def close(self):
        self.s.close()


async def test_the_fake_replays_a_live_call_before_any_audio():
    d = _short_tmpdir()
    try:
        async with FakeSipGateway(os.path.join(d, "gw.sock")) as gw:
            a = _Client(gw.path)
            assert await gw.wait_brain(2)
            call = await gw.place_call("+15550001111", ring_s=0)
            got = await a.controls_until_audio()
            assert [m["type"] for m in got] == ["hello", "call.incoming", "call.state",
                                                "call.state", "call.state"], got
            assert [m["state"] for m in got[2:]] == ["incoming", "connecting", "confirmed"]
            assert got[0]["replay"] is True and "replay" not in got[1]
            a.close()
            assert await wait_until(lambda: gw.brain_gone.is_set(), 2)
            assert not call.ended.is_set()

            b = _Client(gw.path)
            got = await b.controls_until_audio()
            assert [(m["type"], m.get("state"), m.get("replay")) for m in got] == [
                ("hello", None, True), ("call.incoming", None, True),
                ("call.state", "confirmed", True)], got
            b.send({"type": "call.hangup", "call_id": call.call_id})
            m = await b.recv()
            while not isinstance(m, dict):
                m = await b.recv()
            assert m == {"type": "call.state", "call_id": call.call_id,
                         "state": "disconnected"}, m
            assert call.ended_by == "brain"
            b.close()
            assert await wait_until(lambda: gw.brain_gone.is_set(), 2)

            # A call that has ended is not replayed.
            c = _Client(gw.path)
            assert (await c.recv())["type"] == "hello"
            try:
                extra = await c.recv(0.3)
            except asyncio.TimeoutError:
                extra = None
            assert extra is None, f"an ended call was replayed: {extra}"
            c.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_the_fake_as_an_older_gateway_does_not_replay():
    d = _short_tmpdir()
    try:
        async with FakeSipGateway(os.path.join(d, "gw.sock"), replay=False) as gw:
            a = _Client(gw.path)
            assert await gw.wait_brain(2)
            await gw.place_call(ring_s=0)
            await a.controls_until_audio()
            a.close()
            assert await wait_until(lambda: gw.brain_gone.is_set(), 2)
            b = _Client(gw.path)
            hello = await b.recv()
            assert hello["type"] == "hello" and "replay" not in hello, hello
            m = await b.recv()   # straight to audio: the call is stranded, as it was
            assert isinstance(m, bytes) and m[0] == MSG_AUDIO_IN, m
            b.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_the_fake_waits_for_call_answer_without_auto_answer():
    d = _short_tmpdir()
    try:
        async with FakeSipGateway(os.path.join(d, "gw.sock"), auto_answer=False) as gw:
            a = _Client(gw.path)
            assert await gw.wait_brain(2)
            placing = asyncio.ensure_future(gw.place_call(answer_timeout=5))
            for _ in range(3):   # hello, call.incoming, incoming
                m = await a.recv()
            assert m["state"] == "incoming", m
            await asyncio.sleep(0.3)
            assert not placing.done(), "answered without a call.answer"
            a.send({"type": "call.answer", "call_id": gw.call.call_id})
            call = await asyncio.wait_for(placing, 2)
            assert call.confirmed.is_set()
            await call.hangup()

            # A brain that declines the ringing call: it ends there, unanswered, at
            # once -- not as a TimeoutError once the caller would have given up.
            placing = asyncio.ensure_future(gw.place_call(answer_timeout=5))
            assert await wait_until(
                lambda: gw.call is not call and gw.call.state == "incoming", 2)
            a.send({"type": "call.hangup", "call_id": gw.call.call_id})
            declined = await asyncio.wait_for(placing, 2)
            assert declined.ended.is_set() and not declined.confirmed.is_set()
            assert declined.ended_by == "brain", declined.ended_by
            a.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_the_fake_flags_what_the_real_gateway_would_mangle():
    """The gateway plays one datagram as one 20 ms frame and truncates past 2048 B."""
    d = _short_tmpdir()
    try:
        async with FakeSipGateway(os.path.join(d, "gw.sock")) as gw:
            a = _Client(gw.path)
            assert await gw.wait_brain(2)
            await gw.place_call(ring_s=0)
            a.s.send(bytes([MSG_AUDIO_OUT]) + bytes(BYTES_PER_FRAME))
            a.s.send(bytes([MSG_AUDIO_OUT]) + bytes(64000))
            a.s.send(bytes([0x7f]))
            a.s.send(bytes([MSG_CONTROL]) + b"{not json")
            big = {"type": "hello", "pad": "x" * 3000}
            a.s.send(bytes([MSG_CONTROL]) + json.dumps(big).encode())
            assert await wait_until(lambda: len(gw.violations) == 5, 2), gw.violations
            v = gw.violations
            assert sum("exceeds MAX_FRAME_BYTES" in x for x in v) == 2, v  # audio + control
            assert sum("unknown tag 0x7f" in x for x in v) == 1, v
            assert sum("unparseable control" in x for x in v) == 2, v  # bad + truncated
            assert gw.call.audio_out_frames == 2
            a.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_the_fake_will_not_take_over_a_socket_in_use():
    """Not by its own path, and not through a symlink to it (/var/run is /run)."""
    d = _short_tmpdir()
    live = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        real = os.path.join(d, "real")
        os.mkdir(real)
        os.symlink(real, os.path.join(d, "alias"))
        live.bind(os.path.join(real, "gw.sock"))
        live.listen(1)
        for path in (os.path.join(real, "gw.sock"), os.path.join(d, "alias", "gw.sock")):
            try:
                await FakeSipGateway(path).start()
            except RuntimeError as e:
                assert "already listening" in str(e), e
            else:
                raise AssertionError(f"bound over a listening socket via {path}")
        live.close()

        # The live gateway's own path needs allow_live_path, whatever it is called.
        saved = fake_sip_gateway.LIVE_SOCKET
        fake_sip_gateway.LIVE_SOCKET = os.path.join(real, "live.sock")
        try:
            try:
                await FakeSipGateway(os.path.join(d, "alias", "live.sock")).start()
            except RuntimeError as e:
                assert "live gateway's socket" in str(e), e
            else:
                raise AssertionError("bound the live path through a symlink")
            gw = FakeSipGateway(os.path.join(d, "alias", "live.sock"), allow_live_path=True)
            await gw.start()
            await gw.close()
        finally:
            fake_sip_gateway.LIVE_SOCKET = saved
    finally:
        live.close()
        shutil.rmtree(d, ignore_errors=True)


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
