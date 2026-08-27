# SPDX-License-Identifier: MIT
#
# teaport — offline fake teaport-sip gateway that drives MULTIPLE SEQUENTIAL CALLS
# on ONE persistent socket connection, to exercise the SIP brain's per-call pipeline
# lifecycle (Stage 2 part 1).
#
# The sibling of fake_gateway.py (one call, then close). This one keeps the SEQPACKET
# socket OPEN across N calls: for each call it sends call.incoming -> call.state
# confirmed, streams a spoken question WAV as 0x10 audio.in, collects the brain's
# 0x11 audio.out, then sends call.state disconnected and pauses. It only sends hello
# ONCE (the connection is persistent). The socket is closed after the LAST call.
#
# The point it proves: the persistent SipConnection + control dispatch SURVIVE while
# each call's pipeline is built fresh and torn down. If the brain closed the socket
# (or killed control dispatch) when a call's pipeline ended, calls #2 and #3 would
# never be received — this driver would hang and time out.
#
# It always binds a SEPARATE TEST socket (default /tmp/teaport-fakegw.sock), NEVER
# the live /tmp/teaport-sip.sock — it refuses that path outright.
#
# Usage:
#   python3 fake_gateway_multicall.py --socket /tmp/teaport-fakegw.sock \
#           --wav question_host_status.wav --out-prefix /tmp/sip-percall --calls 3

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

LIVE_SOCKET = "/tmp/teaport-sip.sock"  # NEVER bind this — it's the real gateway


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
    """Receive datagrams from the brain: record 0x11 audio.out into the CURRENT
    call's buffer, log 0x01 control. Runs for the whole connection lifetime."""
    while not state["stop"]:
        try:
            data = await asyncio.wait_for(loop.sock_recv(conn, MAX_FRAME_BYTES), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except (ConnectionResetError, OSError):
            break
        if not data:
            state["eof"] = True
            break
        tag, payload = data[0], data[1:]
        if tag == TAG_AUDIO_OUT:
            cur = state["cur"]
            if cur is not None:
                cur["audio"].extend(payload)
                cur["frames"] += 1
                cur["last_audio_t"] = time.monotonic()
        elif tag == TAG_CONTROL:
            try:
                print(f"[brain->gw] control {json.loads(payload)}")
            except Exception:
                print(f"[brain->gw] control <unparseable {len(payload)}B>")
        else:
            print(f"[brain->gw] unknown tag 0x{tag:02x} ({len(payload)}B)")


async def run_one_call(loop, conn, state, question, call_idx, args):
    """Drive a single call end-to-end on the already-open connection."""
    call_id = f"fakecall-{call_idx:04d}"
    call = {"audio": bytearray(), "frames": 0, "last_audio_t": 0.0, "id": call_id}
    state["cur"] = call
    print(f"\n===== CALL {call_idx} ({call_id}) =====")

    await send_control(loop, conn, {"type": "call.incoming", "call_id": call_id,
                                    "from": "sip:tester@fake", "to": "sip:100v@fake"})
    await send_control(loop, conn, {"type": "call.state", "call_id": call_id,
                                    "state": "confirmed"})

    # Let the greeting (LLMRunFrame on confirmed) play out before the question.
    print(f"waiting {args.greet_wait}s for the greeting...")
    await asyncio.sleep(args.greet_wait)
    greeting_frames = call["frames"]
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
    call["last_audio_t"] = time.monotonic()
    deadline = time.monotonic() + args.reply_cap
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        since = time.monotonic() - call["last_audio_t"]
        if call["frames"] > greeting_frames and since > args.reply_idle:
            break

    reply_frames = call["frames"] - greeting_frames
    print(f"reply idle — call {call_idx} audio.out frames={call['frames']} "
          f"(greeting~{greeting_frames}, reply~{reply_frames})")

    await send_control(loop, conn, {"type": "call.state", "call_id": call_id,
                                    "state": "disconnected"})

    # Write this call's reply audio to its own WAV.
    out_wav = f"{args.out_prefix}-call{call_idx}-audio-out.wav"
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WIRE_RATE)
        wf.writeframes(bytes(call["audio"]))
    print(f"wrote {out_wav} ({len(call['audio'])/2/WIRE_RATE:.2f}s)")

    state["cur"] = None  # audio between calls (there should be none) is dropped
    return {"idx": call_idx, "id": call_id, "total_frames": call["frames"],
            "greeting_frames": greeting_frames, "reply_frames": reply_frames}


async def serve(args):
    if os.path.abspath(args.socket) == os.path.abspath(LIVE_SOCKET):
        raise SystemExit(f"refusing to bind the LIVE gateway socket {LIVE_SOCKET}")
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    question = read_wav_16k_mono(args.wav)
    n_q_frames = (len(question) + BYTES_PER_FRAME - 1) // BYTES_PER_FRAME
    print(f"loaded question WAV: {len(question)} bytes "
          f"({len(question)/2/WIRE_RATE:.2f}s, {n_q_frames} frames of 20ms)")
    print(f"driving {args.calls} SEQUENTIAL calls on ONE persistent socket")

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(args.socket)
    srv.listen(1)
    srv.setblocking(False)
    print(f"fake gateway listening on {args.socket} — waiting for the brain to connect")

    loop = asyncio.get_event_loop()
    conn, _ = await loop.sock_accept(srv)
    conn.setblocking(False)
    print("brain connected")

    state = {"cur": None, "stop": False, "eof": False}
    rec_task = asyncio.create_task(recorder(loop, conn, state))

    # hello ONCE — the connection is persistent across all calls.
    await send_control(loop, conn, {"type": "hello", "proto": 0, "role": "gateway",
                                    "codec": "s16le", "rate": WIRE_RATE,
                                    "channels": 1, "ptime_ms": 20})

    results = []
    for call_idx in range(1, args.calls + 1):
        if state["eof"]:
            print("brain closed the socket unexpectedly — aborting")
            break
        results.append(await run_one_call(loop, conn, state, question, call_idx, args))
        if call_idx < args.calls:
            print(f"--- inter-call pause {args.between}s (connection stays open) ---")
            await asyncio.sleep(args.between)

    await asyncio.sleep(0.3)
    state["stop"] = True
    rec_task.cancel()
    try:
        conn.close()
    except Exception:
        pass
    try:
        srv.close()
        os.unlink(args.socket)
    except Exception:
        pass

    print("\n" + "=" * 64)
    print(f"RESULT: drove {len(results)}/{args.calls} calls on one persistent socket")
    for r in results:
        print(f"  call {r['idx']} ({r['id']}): total={r['total_frames']}f "
              f"greeting~{r['greeting_frames']}f reply~{r['reply_frames']}f")
    print("=" * 64)

    # Every call must have received a non-trivial reply (>~0.3 s past the greeting).
    if len(results) != args.calls:
        raise SystemExit(f"FAIL: only {len(results)}/{args.calls} calls completed — "
                         "the connection did NOT survive between calls")
    weak = [r["idx"] for r in results if r["reply_frames"] < 15]
    if weak:
        raise SystemExit(f"FAIL: calls {weak} had too little reply audio.out")
    print(f"PASS: all {args.calls} calls received + answered on one persistent socket")


def main():
    here = os.path.dirname(__file__)
    p = argparse.ArgumentParser(description="Offline fake teaport-sip gateway — "
                                            "multiple sequential calls (per-call test)")
    p.add_argument("--socket", default="/tmp/teaport-fakegw.sock")
    p.add_argument("--wav", default=os.path.join(here, "question_host_status.wav"))
    p.add_argument("--out-prefix", default="/tmp/sip-percall")
    p.add_argument("--calls", type=int, default=3)
    p.add_argument("--greet-wait", type=float, default=14.0,
                   help="seconds to let the greeting play before the question")
    p.add_argument("--reply-idle", type=float, default=3.0,
                   help="silence gap that marks a reply complete")
    p.add_argument("--reply-cap", type=float, default=75.0,
                   help="overall cap waiting for a reply")
    p.add_argument("--between", type=float, default=3.0,
                   help="pause between calls (connection stays open)")
    asyncio.run(serve(p.parse_args()))


if __name__ == "__main__":
    main()
