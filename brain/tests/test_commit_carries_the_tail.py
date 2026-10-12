#
# Unit test: every commit carries the audio after its speech end into the next segment
# (issue #154, stt.py CARRY THE TAIL).
#
# A room session replayed through the box's engine (2026-10-11) lost "You're" from
# "You're not supposed to know" to a barge-in commit drawn 0.14 s into the word: the
# hint claimed the word's onset as silence, so the committed segment never decoded it,
# and the next segment opened behind it. THE CALLER'S VOICE (#140) cannot see voice that
# close to the commit and does not cover the barge-in commit at all. So every commit,
# whatever sends it, sends the audio after its speech end again as the start of the next
# segment.
#
# Driven as test_commit_over_resumed_voice.py drives the service: the real service and
# the real analyzer (pipecat's VAD under the speech-onset mixin, Silero's confidence
# scripted per 32 ms chunk), with each kind of chunk carrying its own bytes so the wire
# shows which audio went where.
#
# What these pin, in order:
#
#   * a verdict's commit is followed on the wire by the audio since its speech end --
#     all its hint claims and the margin before it -- before any live audio;
#   * the barge-in commit over a word the caller has just begun carries that word's
#     onset into the next segment, ahead of the rest of it;
#   * voice since the stop that the carry re-sends no longer holds the hint back; where
#     the bound cuts the carry short of the claim, #140's rule stands;
#   * the bounds: never more than TEAPORT_STT_CARRY_MS, never back past the previous
#     commit or the session's start, and a commit that claims nothing carries nothing;
#   * the clocks: the send clock does not count the re-sent audio, the engine's report
#     does, and a report still maps onto the speech end exactly (SPEECH_DECODED);
#   * the wire's order: no carry while an append is on its way to the socket, and none
#     behind a live chunk that got out between the commit and its carry;
#   * off (0): the wire is as it was, and the hint keeps #140's rule.
#
# Run as a script: python test_commit_carries_the_tail.py (or via test_suite.py).
#
import asyncio
import base64
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinned_pipecat import require_pinned  # noqa: E402

require_pinned()

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    InputAudioRawFrame,
)

from stt_harness import WireRecorder  # noqa: E402
from test_commit_over_resumed_voice import (  # noqa: E402
    CATCHUP,
    CHUNK,
    DOWN,
    MARGIN,
    QUIET,
    SAMPLE_RATE,
    SPEECH,
    UP,
    VOICE,
    Journal,
    Line,
    ms,
    settle,
)

import teaport_brain.stt as stt_mod  # noqa: E402
from teaport_brain import speech_onset  # noqa: E402
from teaport_brain.endpointing import SpeechDecodedFrame  # noqa: E402

CARRY = 500
BYTES = {SPEECH: b"\x01\x10", VOICE: b"\x02\x20", QUIET: b"\x03\x00"}


def chunk(conf):
    return BYTES[conf] * CHUNK


@contextlib.contextmanager
def carry_ms(value):
    was = stt_mod._CARRY_MS
    stt_mod._CARRY_MS = value
    try:
        yield
    finally:
        stt_mod._CARRY_MS = was


class Tagged(Line):
    """Line, with each kind of chunk carrying its own bytes on the wire."""

    async def feed(self, chunks, conf, behind=False):
        for _ in range(chunks):
            f = InputAudioRawFrame(audio=chunk(conf), sample_rate=SAMPLE_RATE,
                                   num_channels=1)
            await self.stt.process_frame(f, DOWN)
            if behind:
                self.backlog.append((f.audio, conf))
            else:
                await self._analyze(f.audio, conf)

    def wire(self):
        return self.stt._websocket.sent

    def after_commit(self, k=0):
        """The messages behind the k-th commit, up to the next one."""
        sent = self.wire()
        at = [i for i, m in enumerate(sent) if m["type"] == "input_audio_buffer.commit"][k]
        out = []
        for m in sent[at + 1:]:
            if m["type"] == "input_audio_buffer.commit":
                break
            out.append(m)
        return out

    def before_commit(self, k=0) -> bytes:
        sent = self.wire()
        at = [i for i, m in enumerate(sent) if m["type"] == "input_audio_buffer.commit"][k]
        return b"".join(base64.b64decode(m["audio"]) for m in sent[:at]
                        if m["type"] == "input_audio_buffer.append")


def audio(m) -> bytes:
    assert m["type"] == "input_audio_buffer.append", m
    return base64.b64decode(m["audio"])


def samples(ms_):
    return ms_ * SAMPLE_RATE // 1000


# ---- what a commit carries ------------------------------------------------------------

async def test_a_verdict_commit_carries_all_its_hint_claims_and_the_margin():
    line = Tagged()
    await line.utterance()
    await line.verdict(True)
    assert len(line.commits()) == 1
    claim = ms(6) - MARGIN
    assert line.hints() == [claim], line.hints()
    behind = line.after_commit()
    assert len(behind) == 1, "the carry, and nothing live yet"
    carried = audio(behind[0])
    assert carried == line.before_commit()[-2 * samples(claim + MARGIN):], (
        "the carry is the audio since the speech end, as it was sent")
    assert line.stt._commits[0].carry_ms == claim + MARGIN
    await line.feed(3, SPEECH)
    behind = line.after_commit()
    assert [audio(m) for m in behind[1:]] == [chunk(SPEECH)] * 3, (
        "the live stream goes on behind the carry")


async def test_the_barge_in_commit_carries_the_word_it_cut():
    """'No, that's good. | You're not supposed to know': the caller's 'You're' begins
    inside the stop's window, quiet to the VAD, and the stop over the bot's reply
    commits at once. The hint claims the onset as silence; the carry re-sends it."""
    with Journal() as j:
        line = Tagged()
        await line.stt.process_frame(BotStartedSpeakingFrame(), UP)
        await line.feed(20, SPEECH)                # "No, that's good."
        await line.feed(4, QUIET)
        await line.feed(2, VOICE)                  # "You'-"
        assert not line.speaking, "stopped: six chunks under the VAD's 0.7"
        assert len(line.commits()) == 1, "the barge-in flush, at once"
        claim = ms(6) - MARGIN
        assert line.hints() == [claim], line.hints()
        carried = audio(line.after_commit()[0])
        assert carried.endswith(chunk(VOICE) * 2), "the word's onset opens the next segment"
        await line.feed(8, SPEECH)                 # "-re not supposed to know"
        nxt = b"".join(audio(m) for m in line.after_commit())
        assert nxt == carried + chunk(SPEECH) * 8, "onset, then the rest of the word"
        await line.stt._handle_message({"type": "transcription.done", "text": "No.",
                                        "reason": "commit"})
        assert f"carried {claim + MARGIN}ms" in j.having("commit=bot-speaking")[0], j.lines


async def a_click_after_the_stop(line):
    """A voiced chunk the analyzer hears after the stop, short of an onset: the verdict
    commits at once (THE CALLER'S VOICE), and #140's hint counts from the chunk."""
    await line.utterance()
    await line.feed(2, QUIET)
    await line.feed(1, VOICE)
    await line.feed(1, QUIET)
    await line.verdict(True)
    assert len(line.commits()) == 1


async def test_voice_the_carry_resends_does_not_hold_the_hint_back():
    line = Tagged()
    await a_click_after_the_stop(line)
    claim = ms(10) - MARGIN
    assert line.hints() == [claim], (
        f"the claim counts from the stop's speech end, the voice is carried; got "
        f"{line.hints()}")
    assert chunk(VOICE) in audio(line.after_commit()[0]), "and the voice is in the carry"


async def test_where_the_bound_cuts_the_carry_short_the_hint_keeps_the_voice_rule():
    """A ceiling's commit after a cough (#140's test): the claim reaches back past the
    bound, so the cough is not all carried, and the hint counts from its end."""
    line = Tagged()
    await line.utterance()
    await line.verdict(False)
    await line.feed(9, QUIET)
    await line.feed(2, VOICE)                      # a cough, no VAD start
    await line.feed(9, QUIET)
    await line.verdict(True, source="ceiling")
    assert line.hints() == [ms(9) - MARGIN], line.hints()
    assert len(audio(line.after_commit()[0])) == 2 * samples(CARRY), "the bound"


# ---- the bounds -----------------------------------------------------------------------

def ended(line, back):
    """Place a speech end `back` samples behind what has been sent, as a VAD stop does."""
    line.stt._speech_end = line.stt._sent_samples - back


async def test_a_carry_never_reaches_back_past_the_previous_commit():
    line = Tagged()
    await line.feed(30, QUIET)
    ended(line, 30 * CHUNK)
    await line.stt._send_commit(why="tail")        # the claim reaches past the bound
    assert len(audio(line.after_commit(0)[0])) == 2 * samples(CARRY)
    await line.feed(3, QUIET)
    ended(line, 30 * CHUNK)
    await line.stt._send_commit(why="tail")
    assert audio(line.after_commit(1)[0]) == chunk(QUIET) * 3, (
        "only what was sent since the last commit")
    ended(line, 30 * CHUNK)
    await line.stt._send_commit(why="tail")
    assert line.after_commit(2) == [], "nothing sent since: nothing to carry"


async def test_a_commit_that_claims_nothing_carries_nothing():
    """The backstop has no speech end: its segment decodes its whole tail, and a carry
    would hand the next segment the same words again."""
    line = Tagged()
    await line.feed(30, SPEECH)
    await line.stt._send_commit(why="backstop")
    assert line.hints() == [None] and line.after_commit() == []


async def test_a_carry_never_reaches_back_into_the_last_session():
    line = Tagged()
    await line.feed(30, QUIET)
    fresh = WireRecorder()

    async def connect(url):
        return fresh

    real = stt_mod.websockets.connect
    stt_mod.websockets.connect = connect
    try:
        line.stt._websocket = None
        await line.stt._connect_websocket()
    finally:
        stt_mod.websockets.connect = real
    assert line.stt._websocket is fresh
    await line.feed(2, QUIET)
    ended(line, 30 * CHUNK)
    await line.stt._send_commit(why="tail")
    assert audio(line.after_commit(0)[0]) == chunk(QUIET) * 2


# ---- the clocks ----------------------------------------------------------------------

async def test_the_report_still_maps_onto_the_speech_end_after_a_carry():
    """The engine's clock counts the re-sent audio, the send clock does not. A report of
    the next stop's speech end plus the margin, on the engine's clock, must say so --
    and one a sample short of it must not, as it would if the carry went uncounted."""
    line = Tagged()
    line.stt._reports = True
    await line.utterance()
    await line.verdict(True)
    sent = line.stt._sent_samples
    carried = samples(line.stt._commits[0].carry_ms)
    assert line.stt._session_origin == -carried, "moved back by what was re-sent"
    await line.feed(20, SPEECH)
    assert line.speaking
    await line.feed(6, QUIET)
    assert line.stt._sent_samples == sent + 26 * CHUNK, "the send clock: live audio only"
    target = line.stt._speech_end + samples(stt_mod._REPORT_MARGIN_MS)
    wire_ms = (target + carried) * 1000 // SAMPLE_RATE
    await line.stt._handle_message({"type": "transcription.progress",
                                    "decoded_through_ms": wire_ms - 1})
    assert not [f for f in line.stt.pushed if isinstance(f, SpeechDecodedFrame)], (
        "short of the speech end on the engine's clock")
    await line.stt._handle_message({"type": "transcription.progress",
                                    "decoded_through_ms": wire_ms})
    assert [f.stop_n for f in line.stt.pushed if isinstance(f, SpeechDecodedFrame)] == [2]


# ---- the wire's order ----------------------------------------------------------------

class Gated(WireRecorder):
    """A socket whose appends wait at a gate (back-pressure), or whose commits yield."""

    def __init__(self):
        super().__init__()
        self.gate = None
        self.yield_on_commit = False

    async def send(self, raw):
        await super().send(raw)
        kind = self.sent[-1]["type"]
        if kind == "input_audio_buffer.append" and self.gate is not None:
            await self.gate.wait()
        if kind == "input_audio_buffer.commit" and self.yield_on_commit:
            for _ in range(3):
                await asyncio.sleep(0)


async def test_no_carry_while_an_append_is_on_its_way():
    line = Tagged()
    wire = line.stt._websocket = Gated()
    await line.feed(20, QUIET)
    wire.gate = asyncio.Event()
    stuck = asyncio.ensure_future(line.feed(1, QUIET))
    await settle()
    assert line.stt._appending == 1
    asyncio.get_running_loop().call_later(0.05, wire.gate.set)
    ended(line, 10 * CHUNK)
    await line.stt._send_commit(why="tail")
    await stuck
    assert [m["type"] for m in line.after_commit()] == [], (
        "where the commit falls is not known: no carry")


async def test_a_live_chunk_ahead_of_the_carry_drops_it():
    with Journal() as j:
        line = Tagged()
        wire = line.stt._websocket = Gated()
        await line.feed(20, QUIET)
        wire.yield_on_commit = True
        live = asyncio.ensure_future(line.feed(1, SPEECH))
        ended(line, 10 * CHUNK)
        await line.stt._send_commit(why="tail")
        await live
        assert [audio(m) for m in line.after_commit()] == [chunk(SPEECH)], (
            "the live chunk went first; a carry behind it would put old audio after new")
        assert j.having("opens at the commit"), j.lines


# ---- off ------------------------------------------------------------------------------

async def test_off_the_wire_is_as_it_was():
    with carry_ms(0):
        line = Tagged()
        await a_click_after_the_stop(line)
        assert line.after_commit() == [], "no carry"
        assert line.hints() == [None], (
            f"#140's rule: nothing to claim after the voice; got {line.hints()}")
        assert line.stt._session_origin == 0
        assert not line.stt._tail, "nothing kept"


def main():
    stt_mod._HINT_MARGIN_MS = MARGIN
    stt_mod.STT_SILENCE_HINT = True
    stt_mod._VOICE_CATCHUP_SECS = CATCHUP
    stt_mod._CARRY_MS = CARRY
    assert speech_onset.ONSET_MIN_MS == 128, "these scripts assume a 4-chunk onset"
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    async def run():
        for fn in tests:
            await fn()
            print(f"  ok {fn.__name__}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
    print("ALL PASS")
