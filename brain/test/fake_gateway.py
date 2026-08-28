# SPDX-License-Identifier: MIT
#
# teaport — offline fake teaport-sip gateway for testing the SIP brain (M2).
#
# Stands in for the real C++/GPL teaport-sip gateway so the brain
# (teaport_brain.sip_server) can be exercised end-to-end WITHOUT a live phone
# call. Speaks teaport-sip protocol v0 (see teaport_brain.sip_serializer):
# AF_UNIX SOCK_SEQPACKET *server* (the gateway is the server; the brain connects).
#
# It always binds a SEPARATE TEST socket (default /tmp/teaport-fakegw.sock), NEVER
# the live /run/teaport/teaport-sip.sock — it refuses that path outright so a fat-finger
# can't disturb the real gateway's NetSapiens registration.
#
# Kept as its own file rather than folded into fake_gateway_multicall.py --calls 1
# (which it nearly is), because two acceptance run logs — RUNLOG-sip-offline.md and
# RUNLOG-sip-tools.md — quote the literal `fake_gateway.py ... --out-wav ...` command
# they were produced by. Those are records of runs that happened, not docs to keep
# current, so a rename would make them cite a command that never existed. The two
# also differ in what they record: this one keeps every audio.out frame from connect
# onward, the multicall driver drops anything arriving outside a call window.
#
# Sequence it drives:
#   brain connects  ->  we send hello, call.incoming, call.state=confirmed
#   wait --greet-wait s (collect any greeting audio.out)
#   stream question.wav as 0x10 frames at 20 ms cadence (640 B each)
#   collect the brain's 0x11 audio.out until it goes idle (--reply-idle s)
#   send call.state=disconnected, close
#   write all received audio.out to a WAV; print stats
#
# Usage:
#   python3 fake_gateway.py --socket /tmp/teaport-fakegw.sock \
#           --wav question.wav --out-wav /tmp/sip-audio-out.wav

import argparse
import asyncio
import json
import os
import socket
import time
import wave

TAG_CONTROL = 0x01
TAG_AUDIO_IN = 0x10   # gateway -> brain
TAG_AUDIO_OUT = 0x11  # brain -> gateway
BYTES_PER_FRAME = 640
FRAME_SECS = 0.02
WIRE_RATE = 16000
MAX_FRAME_BYTES = 2048

LIVE_SOCKET = "/run/teaport/teaport-sip.sock"  # NEVER bind this — it's the real gateway


def read_wav_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1, "question WAV must be mono"
        assert wf.getsampwidth() == 2, "question WAV must be S16LE"
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if rate != WIRE_RATE:
        raise SystemExit(f"question WAV is {rate} Hz; the SIP wire needs {WIRE_RATE} Hz "
                         f"(regenerate with make_question_wav.py)")
    return pcm


async def send_control(loop, conn, msg: dict):
    await loop.sock_sendall(conn, bytes([TAG_CONTROL]) + json.dumps(msg).encode("utf-8"))
    print(f"[gw->brain] control {msg}")


async def send_audio(loop, conn, pcm640: bytes):
    await loop.sock_sendall(conn, bytes([TAG_AUDIO_IN]) + pcm640)


async def recorder(loop, conn, state: dict):
    """Receive datagrams from the brain: record 0x11 audio.out, log 0x01 control."""
    while not state["stop"]:
        try:
            data = await asyncio.wait_for(loop.sock_recv(conn, MAX_FRAME_BYTES), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except (ConnectionResetError, OSError):
            break
        if not data:
            break
        tag, payload = data[0], data[1:]
        if tag == TAG_AUDIO_OUT:
            state["audio"].extend(payload)
            state["frames"] += 1
            state["last_audio_t"] = time.monotonic()
        elif tag == TAG_CONTROL:
            try:
                print(f"[brain->gw] control {json.loads(payload)}")
            except Exception:
                print(f"[brain->gw] control <unparseable {len(payload)}B>")
        else:
            print(f"[brain->gw] unknown tag 0x{tag:02x} ({len(payload)}B)")


async def serve(args):
    if os.path.abspath(args.socket) == os.path.abspath(LIVE_SOCKET):
        raise SystemExit(f"refusing to bind the LIVE gateway socket {LIVE_SOCKET}")
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    question = read_wav_16k_mono(args.wav)
    n_q_frames = (len(question) + BYTES_PER_FRAME - 1) // BYTES_PER_FRAME
    print(f"loaded question WAV: {len(question)} bytes "
          f"({len(question)/2/WIRE_RATE:.2f}s, {n_q_frames} frames of 20ms)")

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(args.socket)
    srv.listen(1)
    srv.setblocking(False)
    print(f"fake gateway listening on {args.socket} — waiting for the brain to connect")

    loop = asyncio.get_event_loop()
    conn, _ = await loop.sock_accept(srv)
    conn.setblocking(False)
    print("brain connected")

    state = {"audio": bytearray(), "frames": 0, "last_audio_t": 0.0, "stop": False}
    rec_task = asyncio.create_task(recorder(loop, conn, state))

    call_id = "fakecall-0001"
    await send_control(loop, conn, {"type": "hello", "proto": 0, "role": "gateway",
                                    "codec": "s16le", "rate": WIRE_RATE,
                                    "channels": 1, "ptime_ms": 20})
    await send_control(loop, conn, {"type": "call.incoming", "call_id": call_id,
                                    "from": "sip:tester@fake", "to": "sip:100v@fake"})
    await send_control(loop, conn, {"type": "call.state", "call_id": call_id, "state": "confirmed"})

    # Let the greeting (LLMRunFrame on confirmed) play out before the question.
    print(f"waiting {args.greet_wait}s for the greeting...")
    await asyncio.sleep(args.greet_wait)
    greeting_frames = state["frames"]
    print(f"greeting audio.out so far: {greeting_frames} frames")

    # Stream the spoken question at real-time 20 ms cadence.
    print("streaming the question...")
    next_t = time.monotonic()
    for i in range(0, len(question), BYTES_PER_FRAME):
        chunk = question[i:i + BYTES_PER_FRAME]
        if len(chunk) < BYTES_PER_FRAME:
            chunk = chunk + b"\x00" * (BYTES_PER_FRAME - len(chunk))
        await send_audio(loop, conn, chunk)
        next_t += FRAME_SECS
        await asyncio.sleep(max(0.0, next_t - time.monotonic()))
    print("question sent — waiting for the reply...")

    # Collect the reply until audio.out goes idle for --reply-idle (overall cap).
    state["last_audio_t"] = time.monotonic()
    deadline = time.monotonic() + args.reply_cap
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        since = time.monotonic() - state["last_audio_t"]
        if state["frames"] > greeting_frames and since > args.reply_idle:
            break

    reply_frames = state["frames"] - greeting_frames
    print(f"reply idle — total audio.out frames={state['frames']} "
          f"(greeting~{greeting_frames}, reply~{reply_frames})")

    await send_control(loop, conn, {"type": "call.state", "call_id": call_id, "state": "disconnected"})
    await asyncio.sleep(0.3)
    state["stop"] = True
    rec_task.cancel()

    # Write everything we heard back from the brain.
    with wave.open(args.out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WIRE_RATE)
        wf.writeframes(bytes(state["audio"]))
    secs = len(state["audio"]) / 2 / WIRE_RATE

    try:
        conn.close()
    except Exception:
        pass
    try:
        srv.close()
        os.unlink(args.socket)
    except Exception:
        pass

    print("=" * 60)
    print(f"RESULT audio.out: {state['frames']} frames, {len(state['audio'])} bytes, {secs:.2f}s")
    print(f"RESULT wrote     : {args.out_wav}")
    print("=" * 60)
    # Non-trivial reply gate: more than ~0.3 s of playout past the greeting.
    if reply_frames < 15:
        raise SystemExit(f"FAIL: reply audio.out too small ({reply_frames} frames)")
    print("PASS: non-trivial audio.out reply received")


def main():
    here = os.path.dirname(__file__)
    p = argparse.ArgumentParser(description="Offline fake teaport-sip gateway (M2 test)")
    p.add_argument("--socket", default="/tmp/teaport-fakegw.sock")
    p.add_argument("--wav", default=os.path.join(here, "question.wav"))
    p.add_argument("--out-wav", default="/tmp/sip-audio-out.wav")
    p.add_argument("--greet-wait", type=float, default=6.0,
                   help="seconds to let the greeting play before the question")
    p.add_argument("--reply-idle", type=float, default=2.0,
                   help="silence gap that marks the reply complete")
    p.add_argument("--reply-cap", type=float, default=40.0,
                   help="overall cap waiting for the reply")
    asyncio.run(serve(p.parse_args()))


if __name__ == "__main__":
    main()
