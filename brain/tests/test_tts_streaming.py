#
# Unit test: EngineTTSService plays engine audio as it streams (issue #14).
#
# The engine sends a sentence as several audio.chunk messages while it synthesizes,
# then the word timestamps in a trailing empty chunk. The brain used to buffer every
# chunk to audio.done, so first audio waited for the LAST chunk (measured 370-830 ms
# per clause on the 2026-09-01 engine). Now each chunk is played on arrival, the seam
# trim runs incrementally, and the words are placed once the sentence ends, timed
# from where its audio began. No engine needed: _synth_text is stubbed.
#
# Run: python test_tts_streaming.py   (or via pytest)
#

import asyncio
import base64
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the PACKAGE (not just a submodule) before pipecat: teaport_brain/__init__.py
# sets HF_HUB_OFFLINE, and that only guards imports that come after it runs.
import teaport_brain  # noqa: E402, F401

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame  # noqa: E402

from teaport_brain import engine_tts  # noqa: E402
from teaport_brain.engine_tts import (  # noqa: E402
    _CAPTION_LEAD_SECS,
    _SAMPLE_RATE as SR,
    EngineTTSService,
    _EngineError,
    _SeamTrim,
)


def _tone(secs, amp=0.3):
    t = np.arange(int(secs * SR), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def _quiet(secs):
    return np.zeros(int(secs * SR), dtype=np.float32)


# Engine-shaped sentence: lead padding, speech, a word gap, speech, trail padding.
SENTENCE = np.concatenate([_quiet(0.3), _tone(1.0), _quiet(0.4), _tone(0.5), _quiet(0.4)])


def _whole_trim(audio):
    """The pre-#14 whole-buffer trim (_trim_seam_silence), the reference the
    incremental one must reproduce: keep KEEP_LEAD before the first sound and
    KEEP_TRAIL after the last, sound being above 1% of the peak."""
    peak = float(np.max(np.abs(audio)))
    nz = np.nonzero(np.abs(audio) > 0.01 * peak)[0]
    start = max(0, int(nz[0] - engine_tts._SEAM_KEEP_LEAD * SR))
    end = min(audio.shape[0], int(nz[-1] + 1 + engine_tts._SEAM_KEEP_TRAIL * SR))
    return audio[start:end], start / SR


def _streamed_trim(audio, cuts):
    trim, out = _SeamTrim(SR), []
    for block in np.split(audio, cuts):
        out.append(trim.feed(block))
    out.append(trim.finish())
    return np.concatenate(out), trim.lead_cut


def test_seam_trim_matches_whole_buffer_trim():
    want, want_cut = _whole_trim(SENTENCE)
    s = lambda secs: int(secs * SR)  # noqa: E731
    splits = {
        "one block": [],
        "engine-like (first ~1 s, then the rest)": [s(0.98)],
        "cut inside the lead padding": [s(0.1), s(0.98)],
        "cut inside the word gap": [s(1.5)],
        "cut inside the trail padding": [s(2.3)],
        "many small blocks": list(range(s(0.07), SENTENCE.shape[0], s(0.07))),
    }
    for name, cuts in splits.items():
        got, cut = _streamed_trim(SENTENCE, cuts)
        assert got.shape == want.shape and np.array_equal(got, want), \
            f"{name}: streamed trim {got.shape[0]} samples vs whole-buffer {want.shape[0]}"
        assert abs(cut - want_cut) < 1e-9, f"{name}: lead_cut {cut} vs {want_cut}"
    print("  PASS incremental seam trim == whole-buffer trim at every block split")


def test_seam_trim_ignores_a_quiet_first_block():
    """The trim's "sound" threshold is 1% of the peak heard so far. A first block that is
    only padding noise (or a soft onset) must not set it: the noise would count as sound
    and the whole lead padding would be kept (PR #81 review: lead_cut 0.0 vs 0.25 s)."""
    rng = np.random.default_rng(7)
    noise = lambda secs: (2e-4 * rng.standard_normal(int(secs * SR))).astype(np.float32)  # noqa: E731
    sentence = np.concatenate([noise(0.3), _tone(0.2, amp=0.02), _tone(1.0), noise(0.4)])
    want, want_cut = _whole_trim(sentence)
    s = lambda secs: int(secs * SR)  # noqa: E731
    for name, cuts in {"first block all noise": [s(0.1)],
                       "first block noise + soft onset": [s(0.45)],
                       "engine-like": [s(0.98)],
                       "many small blocks": list(range(s(0.05), sentence.shape[0], s(0.05)))
                       }.items():
        got, cut = _streamed_trim(sentence, cuts)
        assert abs(cut - want_cut) < 1e-9, f"{name}: lead_cut {cut:.3f} vs {want_cut:.3f}"
        assert np.array_equal(got, want), \
            f"{name}: streamed trim {got.shape[0]} samples vs whole-buffer {want.shape[0]}"
    quiet = np.concatenate([noise(0.3), _tone(0.5, amp=0.02), noise(0.4)])  # never loud
    want, want_cut = _whole_trim(quiet)
    got, cut = _streamed_trim(quiet, [s(0.2), s(0.6)])
    assert abs(cut - want_cut) < 1e-9 and np.array_equal(got, want), \
        "a sentence that never gets loud is trimmed whole at its end"
    print("  PASS a quiet or noisy first block does not set the trim threshold")


def test_seam_trim_holds_only_trailing_quiet():
    # A sine's edge sits a sample or two off the tone boundary (zero crossings), hence ~.
    near = lambda got, want: abs(got - want) <= 5  # noqa: E731
    trim = _SeamTrim(SR)
    first = np.concatenate([_quiet(0.3), _tone(0.5), _quiet(0.2)])
    out = trim.feed(first)
    keep = int(engine_tts._SEAM_KEEP_LEAD * SR)
    assert near(out.shape[0], keep + _tone(0.5).shape[0]), \
        "plays the kept lead + the speech at once, holds only the quiet after it"
    assert trim.feed(_quiet(0.3)).shape[0] == 0, "more quiet: still held"
    assert near(trim.feed(_tone(0.2)).shape[0], int(0.5 * SR) + _tone(0.2).shape[0]), \
        "sound after the gap releases the held gap with it (the pause is not cut)"
    assert trim.finish().shape[0] <= 5, "nothing held after trailing sound"
    silent = _SeamTrim(SR)
    assert silent.feed(_quiet(0.2)).shape[0] == 0
    assert silent.finish().shape[0] == int(0.2 * SR), "an all-quiet sentence passes untrimmed"
    print("  PASS only trailing quiet is held back")


class _Recorder(EngineTTSService):
    """run_tts driven directly: word timestamps recorded with the moment they were
    placed (how many audio frames had already been yielded)."""

    def __init__(self, script):
        super().__init__(voice="af_heart")
        self._sample_rate = SR        # what the pipeline's StartFrame would set
        self.script = script          # clause text -> list of events / Events to await
        self.placed = []              # (frames yielded so far, [(word, t)])
        self.frames_out = 0
        self.closed = []              # clause texts whose stream was closed

    async def start_tts_usage_metrics(self, text):
        pass

    async def add_word_timestamps(self, word_times, context_id=None, **kw):
        self.placed.append((self.frames_out, list(word_times)))

    async def _synth_text(self, text):
        try:
            await asyncio.sleep(0)  # the real stream awaits its engine connect first
            for ev in self.script[text]:
                if isinstance(ev, asyncio.Event):
                    await ev.wait()
                elif isinstance(ev, Exception):
                    raise ev
                else:
                    yield ev
        finally:
            self.closed.append(text)

    async def speak(self, text, on_frame=None):
        out = []
        async for f in self.run_tts(text, "ctx-1"):
            out.append(f)
            if isinstance(f, TTSAudioRawFrame):
                self.frames_out += 1
            if on_frame:
                await on_frame(f)
        return out


async def test_first_block_plays_before_the_sentence_ends():
    released = asyncio.Event()
    a, b = SENTENCE[:int(0.98 * SR)], SENTENCE[int(0.98 * SR):]
    clause = "Hello there friend."
    svc = _Recorder({clause: [("audio", a), released, ("audio", b),
                              ("end", [("Hello", 0.3), ("there", 0.8), ("friend.", 1.7)])]})
    got_first = asyncio.Event()

    async def on_frame(f):
        if isinstance(f, TTSAudioRawFrame):
            got_first.set()

    task = asyncio.create_task(svc.speak(clause, on_frame))
    await asyncio.wait_for(got_first.wait(), 2.0)
    assert not released.is_set() and not svc.placed, \
        "the first block must play while the engine is still synthesizing the sentence"
    released.set()
    frames = await asyncio.wait_for(task, 2.0)
    audio = np.concatenate([np.frombuffer(f.audio, dtype=np.int16) for f in frames
                            if isinstance(f, TTSAudioRawFrame)])
    want, lead_cut = _whole_trim(SENTENCE)
    assert audio.shape[0] == want.shape[0], "the streamed clause is the whole trimmed sentence"
    assert len(svc.placed) == 1
    after, words = svc.placed[0]
    assert after >= 1, "words are placed after the audio they describe has been yielded"
    expect = [("Hello", 0.3), ("there", 0.8), ("friend.", 1.7)]
    assert len(words) == len(expect), f"placed {words}"
    for (w, t), (ew, es) in zip(words, expect):
        assert w == ew and abs(t - max(0.0, es - lead_cut - _CAPTION_LEAD_SECS)) < 1e-9, \
            f"{w}: {t} — word times shift back by the lead trim and the caption lead"
    assert svc.closed == [clause]
    print(f"  PASS first block yielded before the sentence ended; words placed after "
          f"(lead cut {lead_cut:.2f}s)")


async def test_later_words_time_from_where_their_audio_began():
    """Two sentences in one clause and a second run_tts: each sentence's words are
    based at the reply audio already emitted, not at its own arrival."""
    one, two = "One. Two.", "Three."
    w = [("x", 1.0)]  # late enough that the caption lead never clamps it at 0
    svc = _Recorder({one: [("audio", SENTENCE), ("end", w), ("audio", SENTENCE), ("end", w)],
                     two: [("audio", SENTENCE[:int(1.0 * SR)]),
                           ("audio", SENTENCE[int(1.0 * SR):]), ("end", w)]})
    await svc.speak(one)
    await svc.speak(two)
    trimmed, lead_cut = _whole_trim(SENTENCE)
    dur = trimmed.shape[0] / SR
    bases = [t + _CAPTION_LEAD_SECS - (1.0 - lead_cut) for _, [(_, t)] in svc.placed]
    assert len(bases) == 3, f"every sentence places its words: {svc.placed}"
    for got, want in zip(bases, [0.0, dur, 2 * dur]):
        assert abs(got - want) < 1e-6, f"sentence based at {got:.4f}s, want {want:.4f}s"
    print("  PASS each sentence's words are based at the audio emitted before it")


async def test_a_failed_clause_keeps_what_it_played_and_the_reply_goes_on():
    clause, nxt = "It broke midway.", "Next one."
    svc = _Recorder({clause: [("audio", SENTENCE[:int(0.98 * SR)]), _EngineError("engine")],
                     nxt: [("audio", SENTENCE), ("end", [("Next", 0.3)])]})
    out = await svc.speak(clause)
    assert any(isinstance(f, TTSAudioRawFrame) for f in out), "the played block stays played"
    assert not any(isinstance(f, ErrorFrame) for f in out), \
        "audio went out, so this is not the all-clauses-failed dead air"
    assert svc.closed == [clause] and not svc.placed
    await svc.speak(nxt)
    assert len(svc.placed) == 1
    print("  PASS a clause failing mid-stream keeps its audio; no ErrorFrame")


async def test_a_brain_bug_is_not_logged_as_an_engine_failure():
    """Only the engine stream's failures skip a clause. A bug raised while placing words
    (brain or pipecat) must surface, not be logged as an engine synth error."""
    class Broken(_Recorder):
        async def add_word_timestamps(self, word_times, context_id=None, **kw):
            raise ValueError("word sequencer bug")
    clause = "Hello there friend."
    svc = Broken({clause: [("audio", SENTENCE), ("end", [("Hello", 0.3)])]})
    try:
        await svc.speak(clause)
    except ValueError:
        pass
    else:
        raise AssertionError("a ValueError from add_word_timestamps was swallowed")
    assert svc.closed == [clause]
    print("  PASS a brain-side error propagates; only _EngineError skips a clause")


async def test_abandoning_the_reply_closes_the_stream_at_once():
    """The engine has two stream slots. A reply abandoned mid-clause (barge-in) must
    close its stream right away, not whenever the generator is collected."""
    never = asyncio.Event()
    clause = "Long clause here."
    svc = _Recorder({clause: [("audio", SENTENCE[:int(0.98 * SR)]), never]})
    gen = svc.run_tts(clause, "ctx-1")
    first = await gen.__anext__()
    assert isinstance(first, TTSAudioRawFrame)
    await gen.aclose()
    assert svc.closed == [clause], "the clause's stream closed with the reply"

    svc2 = _Recorder({clause: [("audio", SENTENCE[:int(0.98 * SR)]), never]})
    task = asyncio.create_task(svc2.speak(clause))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert svc2.closed == [clause], "a cancelled run_tts (barge-in) closes the stream"
    print("  PASS an abandoned or cancelled reply closes its stream immediately")


class _FakeWS:
    """The engine's speech stream, scripted: records what the brain sends, replays
    `messages` (dicts become JSON text frames)."""

    def __init__(self, messages):
        self.sent, self._msgs, self.closed = [], list(messages), False

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        m = self._msgs.pop(0)
        return json.dumps(m) if isinstance(m, dict) else m

    async def close(self):
        self.closed = True


def _chunk(raw=b"", ts=None, i=0):
    return {"type": "audio.chunk", "sentence_index": i,
            "audio_b64": base64.b64encode(raw).decode(), "timestamps": ts}


async def _events(messages):
    svc = EngineTTSService(voice="af_heart")
    ws = _FakeWS(messages)

    async def connect():
        return ws
    svc._connect_stream = connect
    events = [ev async for ev in svc._synth_text("x")]
    return events, ws


async def test_synth_text_protocol_edges():
    pcm = (np.arange(-500, 500, dtype=np.int16) * 30).tobytes()
    ts = [{"word": "Hi", "start_ms": 100, "end_ms": 300}]
    # Chunks split mid-sample: the odd byte carries over instead of failing to decode.
    events, ws = await _events([{"type": "audio.start"}, _chunk(pcm[:333]), _chunk(pcm[333:801]),
                                _chunk(pcm[801:]), _chunk(ts=ts), {"type": "audio.done"},
                                {"type": "session.done"}])
    got = np.concatenate([p for k, p in events if k == "audio"])
    assert np.array_equal(np.round(got * 32768).astype(np.int16),
                          np.frombuffer(pcm, dtype=np.int16)), "odd-byte chunks reassemble"
    assert events[-1] == ("end", [("Hi", 0.1)]) and ws.closed
    assert ws.sent[0]["eager"] is engine_tts._STREAM_AUDIO

    # A sentence left without its audio.done is closed (no words) when the next starts,
    # and one still open at session.done is closed there.
    events, _ = await _events([{"type": "audio.start"}, _chunk(pcm), {"type": "audio.start"},
                               _chunk(pcm, i=1), _chunk(ts=ts, i=1), {"type": "audio.done"},
                               {"type": "audio.start"}, _chunk(pcm, i=2),
                               {"type": "session.done"}])
    assert [k if k == "audio" else (k, p) for k, p in events] == \
        ["audio", ("end", []), "audio", ("end", [("Hi", 0.1)]), "audio", ("end", [])], events

    # Engine-side failures, and only those, are _EngineError.
    for bad in ({"type": "error", "message": "unknown voice"},
                {"type": "audio.done", "error": True}, "not json"):
        try:
            await _events([{"type": "audio.start"}, bad])
        except _EngineError:
            continue
        raise AssertionError(f"{bad!r} did not raise _EngineError")

    # Streaming off: each sentence's audio is one block, right before its words, and
    # the engine is asked to skip its eager prefix.
    engine_tts._STREAM_AUDIO = False
    try:
        events, ws = await _events([{"type": "audio.start"}, _chunk(pcm[:1000]),
                                    _chunk(pcm[1000:]), _chunk(ts=ts), {"type": "audio.done"},
                                    {"type": "session.done"}])
    finally:
        engine_tts._STREAM_AUDIO = True
    assert [k for k, _ in events] == ["audio", "end"] and events[0][1].shape[0] == 1000
    assert ws.sent[0]["eager"] is False
    print("  PASS _synth_text: odd-byte chunks, missing audio.done, engine errors, "
          "TTS_STREAM_AUDIO=0")


def test_tts_streaming():
    test_seam_trim_matches_whole_buffer_trim()
    test_seam_trim_ignores_a_quiet_first_block()
    test_seam_trim_holds_only_trailing_quiet()

    async def main():
        await test_first_block_plays_before_the_sentence_ends()
        await test_later_words_time_from_where_their_audio_began()
        await test_a_failed_clause_keeps_what_it_played_and_the_reply_goes_on()
        await test_a_brain_bug_is_not_logged_as_an_engine_failure()
        await test_abandoning_the_reply_closes_the_stream_at_once()
        await test_synth_text_protocol_edges()
    asyncio.run(main())


if __name__ == "__main__":
    test_tts_streaming()
    print("ALL PASS")
