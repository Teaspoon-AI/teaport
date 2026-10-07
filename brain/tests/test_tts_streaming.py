#
# Unit test: EngineTTSService plays engine audio as it streams (issue #14).
#
# The engine sends a sentence as several audio.chunk messages while it synthesizes.
# The brain used to buffer every chunk to audio.done, so first audio waited for the
# LAST chunk (measured 370-830 ms per clause on the 2026-09-01 engine). Now each chunk
# is played on arrival and the seam trim runs incrementally. With teagram-engine#77's
# "chunk_timestamps" each chunk carries the words that start in it, placed just ahead
# of its audio; against an engine without it the words come in the trailing list and
# are placed at the sentence's end. Playout gaps move later words with the audio. No
# engine needed: _synth_text (or the socket under it) is stubbed.
#
# Run: python test_tts_streaming.py   (or via pytest)
#

import asyncio
import base64
import json
import os
import sys
import time

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
    only padding noise (or a soft onset) must not set it below the noise floor, or the
    noise counts as sound and the whole lead padding is kept (PR #81 reviews: lead_cut
    0.0 vs 0.25 s). The _SEAM_FLOOR keeps it out; onsets then land within a sample of
    the whole-buffer trim's (its threshold is 1% of the final peak, a hair higher)."""
    rng = np.random.default_rng(7)
    noise = lambda secs: (2e-4 * rng.standard_normal(int(secs * SR))).astype(np.float32)  # noqa: E731
    s = lambda secs: int(secs * SR)  # noqa: E731
    cases = {
        "soft 0.02 onset": np.concatenate([noise(0.3), _tone(0.2, amp=0.02), _tone(1.0),
                                           noise(0.4)]),
        "0.06 onset under 0.6 speech": np.concatenate([noise(0.3), _tone(0.15, amp=0.06),
                                                       _tone(1.0, amp=0.6), noise(0.4)]),
    }
    for label, sentence in cases.items():
        want, want_cut = _whole_trim(sentence)
        for name, cuts in {"first block all noise": [s(0.1)],
                           "first block noise + onset": [s(0.45)],
                           "engine-like": [s(0.98)],
                           "many small blocks": list(range(s(0.05), sentence.shape[0], s(0.05)))
                           }.items():
            got, cut = _streamed_trim(sentence, cuts)
            assert abs(cut - want_cut) <= 2 / SR, \
                f"{label} / {name}: lead_cut {cut:.4f} vs {want_cut:.4f}"
            assert abs(got.shape[0] - want.shape[0]) <= 2, \
                f"{label} / {name}: {got.shape[0]} samples vs whole-buffer {want.shape[0]}"
    # Soft speech only: the whole-buffer trim's 1%-of-peak threshold (0.0002) sits under
    # the padding noise and keeps all of it; the floor still finds the onset.
    quiet = np.concatenate([noise(0.3), _tone(0.5, amp=0.02), noise(0.4)])
    _, cut = _streamed_trim(quiet, [s(0.2), s(0.6)])
    assert abs(cut - 0.25) <= 2 / SR, f"soft-only sentence: lead_cut {cut:.4f}, want 0.25"
    print("  PASS a quiet or noisy first block does not set the trim threshold")


def test_seam_trim_holds_only_trailing_quiet():
    """Quiet after the latest sound plays at once up to KEEP_TRAIL — it plays either
    way, so holding it would only add to a stall — and only the excess waits."""
    near = lambda got, want: abs(got - want) <= 5  # noqa: E731  (sine edges: a sample or two)
    keep = int(engine_tts._SEAM_KEEP_LEAD * SR)
    trail = int(engine_tts._SEAM_KEEP_TRAIL * SR)
    trim = _SeamTrim(SR)
    out = trim.feed(np.concatenate([_quiet(0.3), _tone(0.5), _quiet(0.2)]))
    assert near(out.shape[0], keep + int(0.5 * SR) + int(0.2 * SR)), \
        "the kept lead, the speech and the 0.2 s of quiet after it (under the trail) all play"
    out = trim.feed(_quiet(0.3))
    assert near(out.shape[0], trail - int(0.2 * SR)), "quiet plays up to the trail"
    out = trim.feed(_tone(0.2))
    assert near(out.shape[0], int(0.5 * SR) - trail + int(0.2 * SR)), \
        "sound after the gap releases the held excess with it (the pause is not cut)"
    assert trim.finish().shape[0] == 0, "nothing past the trail after the last sound"
    trim.feed(np.concatenate([_tone(0.2), _quiet(0.6)]))
    assert trim.finish().shape[0] == 0, "padding past the trail is dropped at the end"
    silent = _SeamTrim(SR)
    assert silent.feed(_quiet(0.2)).shape[0] == 0
    assert silent.finish().shape[0] == int(0.2 * SR), "an all-quiet sentence passes untrimmed"
    print("  PASS quiet plays up to the trail at once; only the excess is held")


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

    async def _synth_text(self, text, eager=True):
        try:
            await asyncio.sleep(0)  # the real stream awaits its engine connect first
            for ev in self.script[text]:
                if isinstance(ev, asyncio.Event):
                    await ev.wait()
                elif isinstance(ev, Exception):
                    raise ev
                elif ev[0] == "audio" and len(ev) == 2:
                    yield ev + (None,)  # no per-chunk list: an engine without #77
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
    print(f"  PASS first block yielded before the sentence ended; without per-chunk lists "
          f"(pre-#77 engine) the words follow at its end (lead cut {lead_cut:.2f}s)")


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


async def test_context_end_drops_only_its_own_tally():
    """The per-context end hook records where that context's audio ends and drops its
    tally only; a context queued behind keeps its own. A barge-in drops them all."""
    svc = _Recorder({})

    async def no_push(*a, **k):
        pass
    svc.push_frame = no_push
    svc._ctx_audio_secs.update({"a": 1.5, "b": 0.7})
    svc._initial_word_timestamp = 10_000_000_000
    await svc._maybe_reset_word_timestamps("a")
    assert svc._ctx_audio_secs == {"b": 0.7}, svc._ctx_audio_secs
    assert svc._prev_audio_end_ns == 10_000_000_000 + 1_500_000_000
    assert svc._initial_word_timestamp == -1, "the base still resets its baseline"
    svc._interrupted = True
    await svc.reset_word_timestamps()
    assert svc._ctx_audio_secs == {} and svc._prev_audio_end_ns == 0
    print("  PASS a context's end drops its own tally only; a barge-in drops all")


async def test_tallies_are_bounded():
    clauses = {f"Clause number {i}.": [("audio", SENTENCE), ("end", [])] for i in range(20)}
    svc = _Recorder(clauses)
    for i, text in enumerate(clauses):
        async for _ in svc.run_tts(text, f"ctx-{i}"):
            pass
    assert len(svc._ctx_audio_secs) <= engine_tts._MAX_CTX_TALLIES
    assert "ctx-19" in svc._ctx_audio_secs, "the newest context keeps its tally"
    print(f"  PASS tallies bounded at {engine_tts._MAX_CTX_TALLIES}")


async def test_words_go_out_ahead_of_their_own_chunk():
    """With per-chunk lists (teagram-engine#77) each chunk's words are handed over
    BEFORE its audio, so the caption lead and the barge-in flush work as they did when
    whole sentences were buffered; the trailing full list is then not placed again."""
    a, b = SENTENCE[:int(0.98 * SR)], SENTENCE[int(0.98 * SR):]
    full = [("Hello", 0.35), ("there", 0.8), ("friend.", 1.75)]
    clause = "Hello there friend."
    svc = _Recorder({clause: [("audio", a, full[:2]), ("audio", b, full[2:]), ("end", full)]})
    await svc.speak(clause)
    _, lead_cut = _whole_trim(SENTENCE)
    assert [n for n, _ in svc.placed] == [0, 1], \
        f"each chunk's words go out before that chunk's audio: {svc.placed}"
    got = [w for _, ws in svc.placed for w in ws]
    want = [(w, max(0.0, s - lead_cut - _CAPTION_LEAD_SECS)) for w, s in full]
    assert len(got) == len(want) and all(
        g[0] == w[0] and abs(g[1] - w[1]) < 1e-9 for g, w in zip(got, want)), got
    print("  PASS per-chunk words go out ahead of their chunk; the trailing list is not "
          "placed twice")


async def test_a_playout_gap_moves_later_words_and_is_logged():
    """Word times count emitted audio. When the next block comes after the audio so far
    has played out, everything after it starts late by the gap: the gap joins the
    context's tally so later words move with it (PR #81 review), and a gap inside a
    sentence is logged. The first audio of a context is not a gap."""
    from loguru import logger
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        gate = asyncio.Event()
        a, b = SENTENCE[:int(0.98 * SR)], SENTENCE[int(0.98 * SR):]
        clause = "Gap in the middle."
        svc = _Recorder({clause: [("audio", a, [("Gap", 0.35)]), gate,
                                  ("audio", b, [("middle.", 1.75)]), ("end", [])]})
        svc._play_end = time.monotonic() - 5.0   # long idle before the reply: not a gap
        task = asyncio.create_task(svc.speak(clause))
        await asyncio.sleep(0.05)
        svc._play_end = time.monotonic() - 0.5   # block 1 has played out 0.5 s ago
        gate.set()
        await asyncio.wait_for(task, 2.0)
        _, lead_cut = _whole_trim(SENTENCE)
        (_, [(_, t1)]), (_, [(_, t2)]) = svc.placed
        assert abs(t1 - max(0.0, 0.35 - lead_cut - _CAPTION_LEAD_SECS)) < 1e-6, t1
        assert 0.45 < t2 - (1.75 - lead_cut - _CAPTION_LEAD_SECS) < 0.6, \
            f"the word after a ~0.5 s gap moved {t2 - (1.75 - lead_cut - _CAPTION_LEAD_SECS):.3f}s"
        assert sum("TTS stream underrun" in ln for ln in lines) == 1, lines
    finally:
        logger.remove(sink)
    print("  PASS a playout gap moves the words after it, and a mid-sentence one is logged")


def test_a_moved_pipecat_hook_fails_the_session_build():
    from pipecat.services.tts_service import TTSService
    engine_tts._require_context_end_hook()          # today's pipecat: fine
    saved = TTSService._maybe_reset_word_timestamps
    try:
        del TTSService._maybe_reset_word_timestamps
        try:
            engine_tts._require_context_end_hook()
        except AttributeError:
            pass
        else:
            raise AssertionError("a missing _maybe_reset_word_timestamps went unnoticed")
    finally:
        TTSService._maybe_reset_word_timestamps = saved
    # An install without .py sources can't run the call check: skip it, don't fail.
    import inspect
    saved_src = inspect.getsource

    def no_source(obj):
        raise OSError("could not get source code")
    inspect.getsource = no_source
    try:
        engine_tts._require_context_end_hook()
    finally:
        inspect.getsource = saved_src
    print("  PASS the private pipecat hook is checked at session build (skipped, not "
          "failed, without sources)")


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
    got = np.concatenate([e[1] for e in events if e[0] == "audio"])
    assert np.array_equal(np.round(got * 32768).astype(np.int16),
                          np.frombuffer(pcm, dtype=np.int16)), "odd-byte chunks reassemble"
    assert events[-1] == ("end", [("Hi", 0.1)]) and ws.closed
    assert all(e[2] is None for e in events if e[0] == "audio"), "no per-chunk list: None"
    assert ws.sent[0]["eager"] is engine_tts._STREAM_AUDIO
    assert ws.sent[0]["chunk_timestamps"] is True

    # Per-chunk lists (#77) ride their audio; [] is "no word starts here".
    t2 = [{"word": "there", "start_ms": 900, "end_ms": 1200}]
    events, _ = await _events([{"type": "audio.start"}, _chunk(pcm, ts=ts), _chunk(pcm, ts=[]),
                               _chunk(pcm, ts=t2), _chunk(ts=ts + t2), {"type": "audio.done"},
                               {"type": "session.done"}])
    assert [e[2] for e in events if e[0] == "audio"] == [[("Hi", 0.1)], [], [("there", 0.9)]]
    assert events[-1] == ("end", [("Hi", 0.1), ("there", 0.9)])

    # A sentence left without its audio.done is closed (no words) when the next starts,
    # and one still open at session.done is closed there.
    events, _ = await _events([{"type": "audio.start"}, _chunk(pcm), {"type": "audio.start"},
                               _chunk(pcm, i=1), _chunk(ts=ts, i=1), {"type": "audio.done"},
                               {"type": "audio.start"}, _chunk(pcm, i=2),
                               {"type": "session.done"}])
    assert [e[0] if e[0] == "audio" else e for e in events] == \
        ["audio", ("end", []), "audio", ("end", [("Hi", 0.1)]), "audio", ("end", [])], events

    # Engine-side failures, and only those, are _EngineError.
    for bad in ({"type": "error", "message": "unknown voice"},
                {"type": "audio.done", "error": True}, "not json",
                {"type": "audio.chunk", "audio_b64": 12345, "timestamps": None}):
        try:
            await _events([{"type": "audio.start"}, bad])
        except _EngineError:
            continue
        raise AssertionError(f"{bad!r} did not raise _EngineError")

    # Malformed word timing only loses the words: the audio still plays.
    for bad_ts in ([{"word": "Hi", "start_ms": None}], ["Hi"], "abc", {"word": "Hi"}):
        events, _ = await _events([{"type": "audio.start"}, _chunk(pcm, ts=bad_ts),
                                   _chunk(ts=bad_ts), {"type": "audio.done"},
                                   {"type": "session.done"}])
        assert [e[0] for e in events] == ["audio", "end"], (bad_ts, events)
        assert events[0][2] is None and events[1] == ("end", []), (bad_ts, events)

    # Streaming off: each sentence's audio is one block, right before its words, and
    # the engine is asked to skip its eager prefix.
    saved, engine_tts._STREAM_AUDIO = engine_tts._STREAM_AUDIO, False
    try:
        events, ws = await _events([{"type": "audio.start"}, _chunk(pcm[:1000], ts=ts),
                                    _chunk(pcm[1000:], ts=[]), _chunk(ts=ts),
                                    {"type": "audio.done"}, {"type": "session.done"}])
    finally:
        engine_tts._STREAM_AUDIO = saved
    assert [e[0] for e in events] == ["audio", "end"] and events[0][1].shape[0] == 1000
    assert events[0][2] == [("Hi", 0.1)], "a buffered sentence keeps its chunks' words"
    assert ws.sent[0]["eager"] is False
    print("  PASS _synth_text: per-chunk words, odd-byte chunks, missing audio.done, "
          "engine errors, malformed timestamps keep the audio, TTS_STREAM_AUDIO=0")


def test_tts_streaming():
    test_seam_trim_matches_whole_buffer_trim()
    test_seam_trim_ignores_a_quiet_first_block()
    test_seam_trim_holds_only_trailing_quiet()
    test_a_moved_pipecat_hook_fails_the_session_build()

    async def main():
        await test_first_block_plays_before_the_sentence_ends()
        await test_later_words_time_from_where_their_audio_began()
        await test_a_failed_clause_keeps_what_it_played_and_the_reply_goes_on()
        await test_a_brain_bug_is_not_logged_as_an_engine_failure()
        await test_abandoning_the_reply_closes_the_stream_at_once()
        await test_synth_text_protocol_edges()
        await test_context_end_drops_only_its_own_tally()
        await test_tallies_are_bounded()
        await test_words_go_out_ahead_of_their_own_chunk()
        await test_a_playout_gap_moves_later_words_and_is_logged()
    asyncio.run(main())


if __name__ == "__main__":
    test_tts_streaming()
    print("ALL PASS")
