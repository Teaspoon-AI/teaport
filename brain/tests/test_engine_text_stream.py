#!/usr/bin/env python3
"""
test_engine_text_stream.py — exercise the engine's text-in TTS stream
(/v1/audio/speech/stream), the path EngineTTSService uses (text-in).
Mirrors engine_tts.py `_synth_text`. Needs a LIVE engine (deps: websockets, numpy).

  ENGINE_TTS_STREAM_URL=ws://127.0.0.1:8000/v1/audio/speech/stream python3 test_engine_text_stream.py
"""
import asyncio
import base64
import json
import os
import sys

import numpy as np
import websockets

URL = os.getenv("ENGINE_TTS_STREAM_URL", "ws://127.0.0.1:8000/v1/audio/speech/stream")
SR = 24000


async def synth(text, voice="af_heart"):
    """Same protocol as EngineTTSService._synth_text. -> [(audio, [(word, start_s)])]."""
    ws = await websockets.connect(URL, max_size=None, open_timeout=5)
    try:
        await ws.send(json.dumps({"type": "session.config", "voice": voice,
                                  "response_format": "pcm", "stream_audio": True,
                                  "word_timestamps": True}))
        await ws.send(json.dumps({"type": "input.text", "text": text}, ensure_ascii=False))
        await ws.send(json.dumps({"type": "input.done"}))
        segs, pcm, words = [], b"", []
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=20)
            if isinstance(msg, (bytes, bytearray)):
                continue
            d = json.loads(msg)
            t = d.get("type")
            if t == "audio.start":
                pcm, words = b"", []
            elif t == "audio.chunk":
                if d.get("audio_b64"):
                    pcm += base64.b64decode(d["audio_b64"])
                if d.get("timestamps"):
                    words = [(w["word"], w["start_ms"] / 1000.0) for w in d["timestamps"]]
            elif t == "audio.done":
                if d.get("error"):
                    raise RuntimeError(f"sentence {d.get('sentence_index')} failed")
                if pcm:
                    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    if a.shape[0]:
                        segs.append((a, words))
                pcm, words = b"", []
            elif t == "session.done":
                break
            elif t == "error":
                raise RuntimeError(d.get("message") or d.get("error"))
        return segs
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def main():
    fails = 0
    # (text, caption words that must be present, caption words that must be ABSENT,
    # min #sentences). run_tts sends ONE clause per call (the brain's clause-ramp gates
    # first-audio), so inputs are single sentences or sub-sentence fragments. Numbers
    # expand in the SPEECH but the engine folds word_timestamps back to the source token,
    # so the caption shows "2026", not "twenty".
    cases = [
        ("Hello there, how are you doing today?", [], [], 1),
        ("It is 2026 and I have $5.99.", ["2026", "$5.99."], ["twenty"], 1),
        ("The quick brown fox", [], [], 1),   # a ramp fragment (no terminal punct)
    ]
    for text, must_have, must_not, min_sents in cases:
        try:
            segs = await synth(text)
        except Exception as e:  # noqa: BLE001
            print(f"[{text[:40]!r}] -> EXCEPTION {e!r}")
            fails += 1
            continue
        total_s = sum(a.shape[0] for a, _ in segs) / SR
        allwords = [w for _, ws in segs for (w, _) in ws]
        joined = " ".join(allwords)
        print(f"[{text[:44]!r}] -> {len(segs)} sentence(s), {total_s:.2f}s, "
              f"{len(allwords)} words: {joined[:80]!r}")
        # per-segment monotonic starts within a segment
        for _, ws in segs:
            starts = [s for _, s in ws]
            if starts != sorted(starts):
                print("  FAIL: non-monotonic word starts")
                fails += 1
                break
        if total_s <= 0:
            print("  FAIL: no audio")
            fails += 1
        if not allwords:
            print("  FAIL: no word_timestamps")
            fails += 1
        if len(segs) < min_sents:
            print(f"  FAIL: expected >= {min_sents} sentence(s)")
            fails += 1
        for m in must_have:  # source token must survive as a caption word (fold)
            if m not in allwords:
                print(f"  FAIL: expected caption word {m!r} (fold to source token)")
                fails += 1
        for m in must_not:   # spoken expansion must NOT leak into the caption
            if m in allwords:
                print(f"  FAIL: {m!r} leaked into the caption (expansion not folded)")
                fails += 1
    print("OK" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
