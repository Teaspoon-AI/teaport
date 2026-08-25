# SPDX-License-Identifier: MIT
#
# teaport — synthesize a short SPOKEN-question WAV for the offline SIP test.
#
# The fake gateway must play REAL speech (a tone won't exercise STT). This drives
# the teaport engine's text-in TTS stream directly (the same protocol
# engine_tts._synth_text speaks), then resamples the engine's 24 kHz output down
# to the 16 kHz / mono / S16LE the SIP wire uses, and writes a WAV. Run it once on
# the Jetson (where the engine lives) to produce brain/test/question.wav; the
# committed WAV is then replayed by fake_gateway.py with no engine dependency.
#
# Usage: python -m teaport_brain... no — run directly:
#   ENGINE_TTS_STREAM_URL=ws://127.0.0.1:8000/v1/audio/speech/stream \
#   python3 make_question_wav.py --text "What is the capital of France?" \
#           --voice af_heart --out question.wav

import argparse
import asyncio
import base64
import json
import os
import wave

import numpy as np

STREAM_URL = os.getenv("ENGINE_TTS_STREAM_URL", "ws://127.0.0.1:8000/v1/audio/speech/stream")
ENGINE_RATE = 24000
WIRE_RATE = 16000


async def synth(text: str, voice: str) -> np.ndarray:
    """Return float32 [-1,1] mono audio at the engine's 24 kHz."""
    import websockets

    ws = await websockets.connect(STREAM_URL, max_size=None, open_timeout=10)
    try:
        # word_timestamps=True makes the engine deliver audio as base64 inside
        # audio.chunk JSON (matches engine_tts._synth_text); with it False the
        # audio would ride as raw binary frames instead — we collect those too.
        await ws.send(json.dumps({"type": "session.config", "voice": voice,
                                  "response_format": "pcm", "stream_audio": True,
                                  "word_timestamps": True}))
        await ws.send(json.dumps({"type": "input.text", "text": text}, ensure_ascii=False))
        await ws.send(json.dumps({"type": "input.done"}))
        pcm = b""
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(msg, (bytes, bytearray)):
                pcm += bytes(msg)
                continue
            data = json.loads(msg)
            mtype = data.get("type")
            if mtype == "audio.chunk":
                b64 = data.get("audio_b64") or ""
                if b64:
                    pcm += base64.b64decode(b64)
            elif mtype == "audio.done":
                if data.get("error"):
                    raise RuntimeError(f"engine audio.done error: {data.get('error')}")
            elif mtype == "session.done":
                break
            elif mtype == "error":
                raise RuntimeError(f"engine error: {data.get('message') or data.get('error')}")
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def resample_24k_to_16k(audio: np.ndarray) -> np.ndarray:
    """24000 -> 16000 (ratio 2/3). Prefer soxr (pipecat's resampler); fall back
    to linear interpolation so this stays runnable without soxr."""
    try:
        import soxr
        return soxr.resample(audio, ENGINE_RATE, WIRE_RATE)
    except Exception:
        n_out = int(round(len(audio) * WIRE_RATE / ENGINE_RATE))
        x_old = np.arange(len(audio))
        x_new = np.linspace(0, len(audio) - 1, n_out)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def write_wav(path: str, audio16k: np.ndarray):
    pcm = (np.clip(audio16k, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(WIRE_RATE)
        wf.writeframes(pcm)


async def main_async(args):
    audio24 = await synth(args.text, args.voice)
    audio16 = resample_24k_to_16k(audio24)
    write_wav(args.out, audio16)
    secs = len(audio16) / WIRE_RATE
    print(f"wrote {args.out}: {secs:.2f}s, {len(audio16)} samples @ {WIRE_RATE} Hz "
          f"(text={args.text!r}, voice={args.voice})")


def main():
    p = argparse.ArgumentParser(description="Synthesize a spoken-question WAV via the engine TTS")
    p.add_argument("--text", default="What is the capital of France, and is it a large city?")
    p.add_argument("--voice", default="af_heart")
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "question.wav"))
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
