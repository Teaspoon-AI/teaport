#
# teaport — local audio bridge (USB mic array + speaker -> the brain's /talk)
#
# Talks to the agent through a sound card on the box itself, with no browser, phone or
# OpenClaw client in between. Built for the Seeed ReSpeaker XVF3800 (USB mic array with
# a 3.5 mm speaker jack and hardware echo cancellation), but any ALSA card that does
# 16 kHz stereo S16_LE both ways works.
#
#   mic --arecord--> THIS --binary PCM16 24k mono--> ws://127.0.0.1:7861/talk (brain)
#   speaker <--aplay-- THIS <--binary PCM16 24k mono-- brain (TTS)
#
# It is a /talk client exactly like the OpenClaw plugin and the Discord bridge (the wire
# format is gateway_serializer.py's), so the brain is unchanged. It shares /talk's
# single-session slot: starting it evicts a dashboard Talk session and vice versa.
#
# Two hardware facts shape it:
#   * The XVF3800 only streams capture while a playback stream is open on the same
#     card (arecord alone dies with "Input/output error" and tegra-xusb logs buffer
#     overruns). So playback never stops: when the brain is silent we feed the card
#     digital silence at the real-time rate, which also keeps the card's AEC clocked.
#   * It speaks 16 kHz / 2 channels only. Channel 0 is the beamformed, echo-cancelled
#     conversation mix; channel 1 is the ASR-tuned beam. LOCAL_AUDIO_CAPTURE_CHANNEL
#     picks one.
#
# arecord/aplay (alsa-utils) rather than a Python audio binding: no new dependency in
# the brain venv, and a dead card is a dead child process we can see and restart.
#
# Usage:  python -m teaport_brain.local_audio            (env below)
#
import asyncio
import json
import os
import signal
import sys
import time

import numpy as np
import soxr
from loguru import logger

DEVICE_RATE = 16000
DEVICE_CHANNELS = 2
RELAY_RATE = 24000
CHUNK_MS = 20
# How far ahead of the wall clock we let the card's queue run. Bounds how much speech
# is still buffered, and so still audible, after a barge-in "clear".
LEAD_SECS = 0.12

DEVICE = os.getenv("LOCAL_AUDIO_DEVICE", "hw:CARD=Array,DEV=0")
CAPTURE_CHANNEL = int(os.getenv("LOCAL_AUDIO_CAPTURE_CHANNEL", "0"))
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
URL = os.getenv("LOCAL_AUDIO_URL") or f"ws://127.0.0.1:{os.getenv('BRAIN_PORT', '7861')}/talk"
RECONNECT_SECS = 3.0


def capture_to_relay(raw: bytes, resampler: "soxr.ResampleStream", channel: int) -> bytes:
    """One chunk of device capture (16 kHz stereo S16_LE) -> relay audio (24 kHz mono)."""
    pcm = np.frombuffer(raw, dtype="<i2")
    pcm = pcm[: len(pcm) - len(pcm) % DEVICE_CHANNELS].reshape(-1, DEVICE_CHANNELS)
    mono = np.ascontiguousarray(pcm[:, channel])
    return resampler.resample_chunk(mono).astype("<i2").tobytes()


def relay_to_device(raw: bytes, resampler: "soxr.ResampleStream") -> bytes:
    """One chunk of brain audio (24 kHz mono S16_LE) -> device playback (16 kHz stereo)."""
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2")
    mono = resampler.resample_chunk(pcm).astype("<i2")
    return np.repeat(mono[:, None], DEVICE_CHANNELS, axis=1).tobytes()


class Playback:
    """Real-time paced feed to the card, silence when the brain has nothing to say.

    `write` is the sink (aplay's stdin in production). The clock is injectable so the
    pacing and the barge-in flush can be tested without sleeping.
    """

    def __init__(self, write, clock=time.monotonic):
        self._write = write
        self._clock = clock
        self._pending = bytearray()
        self._resampler = soxr.ResampleStream(RELAY_RATE, DEVICE_RATE, 1, dtype="int16")
        self._chunk_bytes = DEVICE_RATE * CHUNK_MS // 1000 * DEVICE_CHANNELS * 2
        self._start: float | None = None
        self._written_secs = 0.0

    def push(self, relay_audio: bytes) -> None:
        self._pending += relay_to_device(relay_audio, self._resampler)

    def clear(self) -> None:
        """Barge-in: drop what has not been handed to the card yet."""
        self._pending.clear()
        # A fresh resampler: the old one holds the tail of the interrupted speech.
        self._resampler = soxr.ResampleStream(RELAY_RATE, DEVICE_RATE, 1, dtype="int16")

    @property
    def speaking(self) -> bool:
        return bool(self._pending)

    def pump(self) -> None:
        """Hand the card as many chunks as keep it LEAD_SECS ahead of the clock."""
        now = self._clock()
        if self._start is None:
            self._start = now
        while self._written_secs - (now - self._start) < LEAD_SECS:
            if len(self._pending) >= self._chunk_bytes:
                chunk = bytes(self._pending[: self._chunk_bytes])
                del self._pending[: self._chunk_bytes]
            else:
                # Short tail of a reply, or nothing: pad with silence so the card's
                # clock never stops (the capture side dies without it).
                chunk = bytes(self._pending).ljust(self._chunk_bytes, b"\0")
                self._pending.clear()
            self._write(chunk)
            self._written_secs += CHUNK_MS / 1000


async def _spawn(*argv: str, **kw) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*argv, **kw)


async def run_session() -> None:
    """One /talk connection with the card held for exactly as long as it lasts."""
    import websockets

    url = URL + (("&" if "?" in URL else "?") + "token=" + GATEWAY_TOKEN if GATEWAY_TOKEN else "")
    fmt = ["-f", "S16_LE", "-r", str(DEVICE_RATE), "-c", str(DEVICE_CHANNELS), "-D", DEVICE, "-q"]
    period_us, buffer_us = CHUNK_MS * 1000, int(LEAD_SECS * 1e6) + CHUNK_MS * 2000
    async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
        logger.info(f"connected to {URL}")
        # Playback first: capture only runs once a playback stream is open.
        aplay = await _spawn(
            "aplay", *fmt, f"--period-time={period_us}", f"--buffer-time={buffer_us}", "-t", "raw",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        play = Playback(aplay.stdin.write)
        play.pump()  # prime the card before arecord opens
        arecord = await _spawn(
            "arecord", *fmt, "-t", "raw",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        procs = (aplay, arecord)

        async def feed_playback():
            while True:
                play.pump()
                await aplay.stdin.drain()
                await asyncio.sleep(CHUNK_MS / 2000)

        async def send_mic():
            resampler = soxr.ResampleStream(DEVICE_RATE, RELAY_RATE, 1, dtype="int16")
            frame = DEVICE_RATE * CHUNK_MS // 1000 * DEVICE_CHANNELS * 2
            while True:
                raw = await arecord.stdout.readexactly(frame)
                await ws.send(capture_to_relay(raw, resampler, CAPTURE_CHANNEL))

        async def receive():
            async for msg in ws:
                if isinstance(msg, bytes):
                    play.push(msg)
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                kind = m.get("type")
                if kind == "clear":
                    play.clear()
                elif kind == "ready":
                    logger.info("session ready")
                elif kind == "transcript" and m.get("final"):
                    logger.info(f"{m.get('role')}: {m.get('text')}")

        async def watch(p: asyncio.subprocess.Process):
            rc = await p.wait()
            err = (await p.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(f"audio process exited ({rc}): {err or 'no output'}")

        tasks = [asyncio.create_task(c) for c in (
            feed_playback(), send_mic(), receive(), watch(aplay), watch(arecord))]
        try:
            await next(iter(asyncio.as_completed(tasks)))
        finally:
            for t in tasks:
                t.cancel()
            for p in procs:
                if p.returncode is None:
                    p.terminate()
            await asyncio.gather(*tasks, *(p.wait() for p in procs), return_exceptions=True)


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    logger.info(f"local audio bridge: device={DEVICE} channel={CAPTURE_CHANNEL} url={URL}")
    while not stop.is_set():
        session = asyncio.create_task(run_session())
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait({session, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not session.done():
            session.cancel()
        try:
            await session
            logger.warning("session ended")
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001 — any failure (brain down, card gone): retry
            logger.warning(f"session failed: {e!r}")
        if not stop.is_set():
            await asyncio.sleep(RECONNECT_SECS)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
