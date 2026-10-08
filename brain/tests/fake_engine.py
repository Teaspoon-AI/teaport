# SPDX-License-Identifier: MIT
#
# teaport — a stand-in speech engine and LLM, for running the REAL brain with no GPU.
#
# One aiohttp server on one port, speaking the three wire protocols the brain's
# services use, so stt.py, engine_tts.py and the OpenAI LLM service run unmodified:
#
#   ws   /v1/realtime               the engine's STT (stt.py's header has the protocol).
#                                   One session at a time, a second is refused 503, as
#                                   the engine's single STT slot does. It cannot
#                                   recognise anything: a segment with speech in it
#                                   (by energy) transcribes as the next scripted
#                                   transcript, streamed as deltas while the speech
#                                   lasts and closed by transcription.done on commit.
#   ws   /v1/audio/speech/stream    the engine's text-in TTS: per sentence audio.start,
#                                   one audio.chunk of a tone (0.22 s a word, 24 kHz)
#                                   with word timestamps, the trailing word list,
#                                   audio.done; then session.done.
#   POST /v1/chat/completions       an OpenAI-compatible chat endpoint, streamed. It
#                                   greets on the brain's greeting cue, apologises on
#                                   the resume cue, and otherwise answers with `reply`
#                                   -- or with a tool call, when `tool_call` (a function
#                                   of the messages and the offered tool names) says so;
#                                   after a tool's result it says TOOL_REPLY.
#
# Everything it is asked is kept for assertions: stt_finals, tts_texts, llm_requests,
# stt_sessions / stt_active.
#
# Point a brain at it with the env from env_for() (TEAPORT_URL, ENGINE_TTS_URL,
# LLM_BASE_URL, ...), set BEFORE teaport_brain.services is imported. By hand:
#     python tests/fake_engine.py --port 18000      # prints the env to export
#
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import re
import struct
import sys
import time

from aiohttp import WSMsgType, web

DEFAULT_TRANSCRIPT = "What is the capital of France, and is it a large city?"
DEFAULT_REPLY = ("Paris is the capital of France, and yes, it is a big city with "
                 "more than two million people living in it.")
GREETING_REPLY = "Hello, you have reached the test line. How can I help?"
RESUME_REPLY = "Sorry, you lost me for a moment. Where were we?"
TOOL_REPLY = "Okay."

_STT_RATE = 16000
_TTS_RATE = 24000
_WORD_S = 0.22
_SPEECH_RMS = 300.0      # int16 RMS of caller audio that counts as speech
_DELTA_EVERY_S = 0.25    # speech per streamed word while the caller talks


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    s = struct.unpack(f"<{n}h", pcm[:2 * n])
    return math.sqrt(sum(x * x for x in s) / n)


def _tone(seconds: float) -> bytes:
    """A 330 Hz tone with 10 ms fades, 24 kHz S16LE: audible as the bot talking."""
    n = max(1, int(seconds * _TTS_RATE))
    fade = int(0.01 * _TTS_RATE)
    out = []
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade) if fade else 1.0
        out.append(int(8000 * env * math.sin(2 * math.pi * 330 * i / _TTS_RATE)))
    return struct.pack(f"<{n}h", *out)


class FakeEngine:
    def __init__(self, *, transcripts: list[str] | None = None, reply: str = DEFAULT_REPLY,
                 tool_call=None, host: str = "127.0.0.1", port: int = 0):
        self.transcripts = list(transcripts or [DEFAULT_TRANSCRIPT])
        self.reply = reply
        # (messages, tool names) -> (name, arguments) to call a tool, or None to answer.
        self.tool_call = tool_call
        self.tool_calls: list[tuple[str, dict]] = []
        self.host = host
        self.port = port
        self.stt_sessions = 0
        self.stt_active = 0
        self.stt_refused = 0
        self.stt_finals: list[str] = []
        self.tts_texts: list[str] = []
        self.llm_requests: list[list[dict]] = []
        self._runner: web.AppRunner | None = None
        self._next = 0

    # -- lifecycle -------------------------------------------------------------------

    async def start(self):
        app = web.Application()
        app.router.add_get("/v1/realtime", self._stt)
        app.router.add_get("/v1/audio/speech/stream", self._tts)
        app.router.add_post("/v1/chat/completions", self._chat)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def env_for(self) -> dict:
        """The brain env that points every service here."""
        base = f"{self.host}:{self.port}"
        return {
            "TEAPORT_URL": f"ws://{base}/v1/realtime",
            "ENGINE_TTS_URL": f"ws://{base}/v1/tts",
            "LLM_BASE_URL": f"http://{base}/v1",
            "LLM_API_KEY": "fake",
            "LLM_MODEL": "fake",
            # No OpenClaw gateway behind this brain: no recall/consult round-trips.
            "TEAPORT_AGENT": "none",
        }

    # -- STT -------------------------------------------------------------------------

    def _transcript(self) -> str:
        text = self.transcripts[min(self._next, len(self.transcripts) - 1)]
        self._next += 1
        return text

    async def _stt(self, request):
        if self.stt_active:
            self.stt_refused += 1
            return web.Response(status=503, text="STT session busy")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.stt_sessions += 1
        self.stt_active += 1
        seg_speech, words, streamed = 0.0, None, 0
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype = data.get("type")
                if mtype == "session.update":
                    await ws.send_json({"type": "session.created",
                                        "id": f"sess_fake{self.stt_sessions}",
                                        "created": int(time.time())})
                elif mtype == "input_audio_buffer.append":
                    pcm = base64.b64decode(data.get("audio", ""))
                    if _rms(pcm) > _SPEECH_RMS:
                        if words is None:
                            words = self._transcript().split()
                        seg_speech += len(pcm) / 2 / _STT_RATE
                        while (streamed < len(words) - 1
                               and seg_speech >= (streamed + 1) * _DELTA_EVERY_S):
                            await ws.send_json({"type": "transcription.delta", "delta":
                                                (" " if streamed else "") + words[streamed]})
                            streamed += 1
                elif mtype == "input_audio_buffer.commit":
                    text = ""
                    if words is not None:
                        rest = words[streamed:]
                        if rest:
                            await ws.send_json({"type": "transcription.delta", "delta":
                                                (" " if streamed else "") + " ".join(rest)})
                        text = " ".join(words)
                        self.stt_finals.append(text)
                    await ws.send_json({"type": "transcription.done", "text": text,
                                        "reason": "commit"})
                    seg_speech, words, streamed = 0.0, None, 0
        except ConnectionResetError:
            pass  # the brain went away mid-message
        finally:
            self.stt_active -= 1
        return ws

    # -- TTS -------------------------------------------------------------------------

    async def _tts(self, request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        text = ""
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype = data.get("type")
                if mtype == "input.text":
                    text += data.get("text", "")
                elif mtype == "input.done":
                    self.tts_texts.append(text)
                    await self._synthesize(ws, text)
                    break
            await ws.close()
        except ConnectionResetError:
            pass  # the brain hung up mid-synthesis (a barge-in, a hangup)
        return ws

    async def _synthesize(self, ws, text: str):
        sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
        for i, sentence in enumerate(sentences):
            words = sentence.split()
            stamps = [{"word": w, "start_ms": int(k * _WORD_S * 1000),
                       "end_ms": int((k + 1) * _WORD_S * 1000)}
                      for k, w in enumerate(words)]
            audio = _tone(max(0.3, len(words) * _WORD_S))
            await ws.send_json({"type": "audio.start", "sentence_index": i})
            await ws.send_json({"type": "audio.chunk", "sentence_index": i,
                                "audio_b64": base64.b64encode(audio).decode(),
                                "timestamps": stamps})
            await ws.send_json({"type": "audio.chunk", "sentence_index": i,
                                "audio_b64": "", "timestamps": stamps})
            await ws.send_json({"type": "audio.done", "sentence_index": i})
        await ws.send_json({"type": "session.done"})

    # -- LLM -------------------------------------------------------------------------

    def _answer(self, messages: list[dict]) -> str:
        if messages and messages[-1].get("role") == "tool":
            return TOOL_REPLY
        last = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = last.get("content") or ""
        if isinstance(content, list):  # content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content.startswith("(") and "lost" in content:
            return RESUME_REPLY
        if content.startswith("("):
            return GREETING_REPLY
        return self.reply

    async def _chat(self, request):
        body = await request.json()
        messages = body.get("messages") or []
        self.llm_requests.append(messages)
        answer = self._answer(messages)
        created = int(time.time())

        def chunk(delta, finish=None):
            return {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                    "created": created, "model": body.get("model", "fake"),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        offered = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        call = (self.tool_call(messages, offered)
                if self.tool_call is not None and body.get("stream")
                and messages and messages[-1].get("role") == "user" else None)
        if call is not None:
            name, args = call
            self.tool_calls.append((name, args))
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for c in (chunk({"role": "assistant", "content": None, "tool_calls": [
                            {"index": 0, "id": f"call_{len(self.tool_calls)}",
                             "type": "function",
                             "function": {"name": name, "arguments": ""}}]}),
                      chunk({"tool_calls": [{"index": 0, "function": {
                          "arguments": json.dumps(args)}}]}),
                      chunk({}, "tool_calls")):
                await resp.write(f"data: {json.dumps(c)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        if not body.get("stream"):
            return web.json_response({
                "id": "chatcmpl-fake", "object": "chat.completion", "created": created,
                "model": body.get("model", "fake"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}]})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(obj):
            await resp.write(f"data: {json.dumps(obj)}\n\n".encode())

        try:
            await send(chunk({"role": "assistant", "content": ""}))
            for i, w in enumerate(answer.split(" ")):
                await send(chunk({"content": (" " if i else "") + w}))
            await send(chunk({}, "stop"))
            if (body.get("stream_options") or {}).get("include_usage"):
                n = len(answer.split())
                await send({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                            "created": created, "model": body.get("model", "fake"),
                            "choices": [], "usage": {"prompt_tokens": 1,
                                                     "completion_tokens": n,
                                                     "total_tokens": n + 1}})
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except ConnectionResetError:
            pass  # the brain cancelled the completion
        return resp


async def _serve(args):
    engine = await FakeEngine(transcripts=args.transcript or None, reply=args.reply,
                              host=args.host, port=args.port).start()
    for k, v in engine.env_for().items():
        print(f"export {k}={v}", flush=True)
    print(f"# fake engine + LLM on {engine.host}:{engine.port}; Ctrl-C to stop",
          file=sys.stderr, flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await engine.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fake speech engine + LLM for the brain")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18000)
    p.add_argument("--transcript", action="append",
                   help="what each speech segment transcribes as (repeatable, in order)")
    p.add_argument("--reply", default=DEFAULT_REPLY, help="the LLM's answer to a user turn")
    try:
        asyncio.run(_serve(p.parse_args()))
    except KeyboardInterrupt:
        pass
