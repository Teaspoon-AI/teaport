#
# Unit test: every commit carries the audio from where its claim starts into the next
# segment (issue #154, stt.py CARRY THE TAIL).
#
# A room session replayed through the box's engine (2026-10-11) lost "You're" from
# "You're not supposed to know" to a barge-in commit drawn 0.14 s into the word: the
# hint claimed the word's onset as silence, so the committed segment never decoded it,
# and the next segment opened behind it. THE CALLER'S VOICE (#140) cannot see voice that
# close to the commit and does not cover the barge-in commit at all. So every commit,
# whatever sends it, sends the audio from where its claim starts again as the start of
# the next segment.
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
#     the bound keeps the carry from reaching the speech end, #140's rule stands and the
#     carry starts where the claim does, so the cough stays in the closed segment only;
#   * a commit held for the caller's pause keeps their voice in its own final;
#   * ONE RULE, on every commit path at Talk's and the phone's timings: the committed
#     segment decodes at most the margin past the carry's start, and what lies between
#     the two when the carry starts later holds no voice;
#   * the bounds: never more than TEAPORT_STT_CARRY_MS, never back past the previous
#     commit or the session's start, and a commit that claims nothing carries nothing;
#   * the clocks: the send clock does not count the re-sent audio, the engine's report
#     does -- from before the carry's send is awaited -- and a report still maps onto
#     the speech end exactly (SPEECH_DECODED);
#   * the wire: an append on its way lands before a commit and is carried with it, no
#     live chunk gets between a commit and its carry, a timer decides its commit on the
#     state after the append it waited for, and a session closed under a commit takes
#     its stamp and its carry with it;
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
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
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
    window = ms(6)                                 # the stop's 192 ms, nothing in transit
    assert line.hints() == [stt_mod._REPRIME_MS], (
        f"the window less the margin, which gives way at the re-prime floor; got "
        f"{line.hints()}")
    behind = line.after_commit()
    assert len(behind) == 1, "the carry, and nothing live yet"
    carried = audio(behind[0])
    assert carried == line.before_commit()[-2 * samples(window):], (
        "the carry is the audio since the speech end, as it was sent")
    assert line.stt._commits[0].carry_ms == window
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
        assert line.hints() == [stt_mod._REPRIME_MS], (
            f"the stop's window vouches for the re-prime; got {line.hints()}")
        carried = audio(line.after_commit()[0])
        assert carried.endswith(chunk(VOICE) * 2), "the word's onset opens the next segment"
        await line.feed(8, SPEECH)                 # "-re not supposed to know"
        nxt = b"".join(audio(m) for m in line.after_commit())
        assert nxt == carried + chunk(SPEECH) * 8, "onset, then the rest of the word"
        await line.stt._handle_message({"type": "transcription.done", "text": "No.",
                                        "reason": "commit"})
        assert f"carried {ms(6)}ms" in j.having("commit=bot-speaking")[0], j.lines


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


async def test_where_the_bound_cuts_the_carry_short_the_cough_stays_in_the_closed_segment():
    """A ceiling's commit after a cough (#140's test): the claim would reach back past
    the bound, so the hint counts from the cough's end, and the carry starts there too.
    The cough is decoded by the closed segment only; the margin past it by both."""
    line = Tagged()
    await line.utterance()
    await line.verdict(False)
    await line.feed(9, QUIET)
    await line.feed(2, VOICE)                      # a cough, no VAD start
    await line.feed(9, QUIET)
    await line.verdict(True, source="ceiling")
    assert line.hints() == [ms(9) - MARGIN], line.hints()
    assert audio(line.after_commit()[0]) == chunk(QUIET) * 9, (
        "the carry starts where the claim does: after the cough, not at the bound")


async def test_a_commit_held_for_the_callers_voice_keeps_it_in_its_own_final():
    """The word in the inference's backlog that #140 holds the boundary for: the turn
    waits for it, so it is this segment's, even though the carry could reach it. The
    claim counts from the voice's end and the carry holds only the pause after it."""
    line = Tagged()
    await line.utterance()                         # 6 chunks since the speech end
    await line.feed(4, VOICE, behind=True)         # the next word, unheard at the verdict
    await line.verdict(True)
    await line.catch_up()
    await settle()
    assert line.commits() == [], "held for the caller's pause"
    pause = 0
    while not line.commits() and pause < 20:
        await line.feed(1, QUIET)
        await settle()
        pause += 1
    assert len(line.commits()) == 1 and line.stt._commits[0].for_voice
    assert 6 + 4 + pause <= 18, "inside the carry's reach of the speech end, the case at issue"
    assert line.hints() == [ms(pause) - MARGIN], (
        f"the claim counts from the voice's end; got {line.hints()}")
    carried = audio(line.after_commit()[0])
    assert carried == chunk(QUIET) * pause, "the carry holds the pause, not the word"


# ---- one rule, every path ---------------------------------------------------------------
#
# At the timings the brain runs with -- Talk: a 0.2 s stop, a 0.6 s ceiling; the phone:
# 0.5 s and 1.0 s -- every commit path: the committed segment decodes up to its hint,
# the next one opens at the carry's start, and the two may overlap by the margin and no
# more, with no voice in the overlap and none in a stretch between them.

TALK = (0.2, 0.6)
PHONE = (0.5, 1.0)


def secs(s_):
    return round(s_ * SAMPLE_RATE / CHUNK)


def voiced(b: bytes) -> bool:
    return any(b[j:j + 2] in (BYTES[VOICE], BYTES[SPEECH]) for j in range(0, len(b), 2))


def on_the_wire(line):
    """The live audio sent, and for each commit: the live samples before it, its hint,
    the samples it carried, and its stamp."""
    live = bytearray()
    out = []
    stamps = list(line.stt._commits)
    sent = line.wire()
    i = 0
    while i < len(sent):
        m = sent[i]
        if m["type"] == "input_audio_buffer.commit":
            stamp = stamps[len(out)]
            carry = b""
            if stamp.carry_ms:
                i += 1
                carry = audio(sent[i])
                assert bytes(live[len(live) - len(carry):]) == carry, (
                    "a carry is the live audio right behind its commit")
            out.append((len(live) // 2, m.get("trailing_silence_ms"), carry, stamp))
        elif m["type"] == "input_audio_buffer.append":
            live += audio(m)
        i += 1
    return bytes(live), out


def check_one_rule(line, label):
    live, commits = on_the_wire(line)
    assert commits, f"{label}: no commit"
    margin = samples(MARGIN)
    for at, hint, carry, stamp in commits:
        what = f"{label}, commit={stamp.why}"
        decoded_to = at - (samples(hint) if hint else 0)
        opens_at = at - len(carry) // 2
        assert decoded_to - opens_at <= margin, (
            f"{what}: {(decoded_to - opens_at) * 1000 // SAMPLE_RATE} ms decoded by both "
            f"segments (hint {hint}, carried {stamp.carry_ms} ms)")
        assert not voiced(live[2 * opens_at:2 * decoded_to]), (
            f"{what}: voice decoded by both segments")
        assert not voiced(live[2 * decoded_to:2 * opens_at]), (
            f"{what}: voice decoded by neither segment")
        if stamp.for_voice:
            assert not voiced(carry), f"{what}: the voice it was held for, carried away"
        if stamp.why in ("bot-speaking", "vad-stop", "tail"):
            # Committed at the stop: its window alone must vouch for the pause the
            # engine re-primes the next segment on (the barge-in's above all).
            assert hint is not None and hint >= stt_mod._REPRIME_MS, (
                f"{what}: hint {hint} ms, under the re-prime floor")


async def the_verdict(line):
    await line.utterance()
    await line.feed(secs(0.15), QUIET)             # Smart Turn's inference
    await line.verdict(True)


async def the_verdict_after_a_click(line):
    await line.utterance()
    await line.feed(2, QUIET)
    await line.feed(1, VOICE)
    await line.feed(1, QUIET)
    await line.verdict(True)


async def the_verdict_held_for_the_callers_pause(line):
    await line.utterance()
    await line.feed(4, VOICE, behind=True)
    await line.verdict(True)
    await line.catch_up()
    await settle()
    n = 0
    while not line.commits() and n < 60:
        await line.feed(1, QUIET)
        await settle()
        n += 1


async def the_ceiling_after_a_cough(line):
    await line.utterance()
    await line.verdict(False)
    hold = secs(line.ceiling)
    await line.feed(hold // 2, QUIET)
    await line.feed(2, VOICE)
    await line.feed(hold - hold // 2 - 2, QUIET)
    await line.verdict(True, source="ceiling")


async def the_ceiling_over_voice_that_never_pauses(line):
    await line.utterance()
    await line.verdict(False)
    await line.feed(secs(line.ceiling) - 2, QUIET)
    await line.feed(2, VOICE)
    await line.verdict(True, source="ceiling")
    waited = 0.0
    while not line.commits() and waited < line.ceiling + 0.3:
        await line.feed(1, VOICE)
        await asyncio.sleep(0.02)
        waited += 0.02


async def the_barge_in(line):
    await line.stt.process_frame(BotStartedSpeakingFrame(), UP)
    await line.feed(20, SPEECH)
    await line.feed(line.vad._vad_stop_frames - 2, QUIET)
    await line.feed(2, VOICE)                      # the word begins inside the stop's window
    assert len(line.commits()) == 1
    await line.feed(8, SPEECH)


async def the_vad_stop(line):
    line.stt._commit_on = "vad-stop"
    await line.utterance()
    await line.feed(3, VOICE)


async def the_tail(line):
    await line.feed(20, SPEECH)
    line.stt._closed_unasked = True                # the engine closed it on its own
    await line.feed(line.vad._vad_stop_frames, QUIET)
    assert len(line.commits()) == 1


async def the_expired_hold(line):
    # The audio of the production wait (the ceiling and the expiry's slack), sent at
    # once; the timer's own wait shortened so the test need not sit through it.
    line.stt.smartturn_stop_secs = 0.02
    await line.utterance()
    hold = secs(line.ceiling + 1.0)
    await line.feed(hold - 6, QUIET)
    await line.feed(2, VOICE)                      # a cough near the end
    await line.feed(4, QUIET)
    await asyncio.sleep(0.1)


async def the_backstop(line):
    await line.feed(20, SPEECH)
    await line.stt._handle_message({"type": "transcription.delta", "delta": "the"})
    await asyncio.sleep(0.1)


PATHS = [the_verdict, the_verdict_after_a_click, the_verdict_held_for_the_callers_pause,
         the_ceiling_after_a_cough, the_ceiling_over_voice_that_never_pauses, the_barge_in,
         the_vad_stop, the_tail, the_expired_hold, the_backstop]


class Timed(Tagged):
    def __init__(self, stop, ceiling):
        super().__init__(stop_secs=stop)
        self.ceiling = ceiling
        self.stt.smartturn_stop_secs = ceiling


async def test_one_rule_on_every_commit_path_at_talk_and_phone_timings():
    slack, stranded = stt_mod._HOLD_SLACK_SECS, stt_mod._STRANDED_INTERIM_SECS
    stt_mod._HOLD_SLACK_SECS, stt_mod._STRANDED_INTERIM_SECS = 0.0, 0.05
    try:
        whys = set()
        for name, (stop, ceiling) in (("talk", TALK), ("phone", PHONE)):
            for path in PATHS:
                line = Timed(stop, ceiling)
                await path(line)
                check_one_rule(line, f"{name}: {path.__name__}")
                whys |= {c.why for c in line.stt._commits}
                for t in (line.stt._hold_task, line.stt._stranded_task):
                    if t is not None:
                        t.cancel()
        assert whys >= {"verdict", "ceiling", "bot-speaking", "vad-stop", "tail",
                        "hold-expired", "backstop"}, whys
    finally:
        stt_mod._HOLD_SLACK_SECS, stt_mod._STRANDED_INTERIM_SECS = slack, stranded


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
    # Nor does one whose claim the margin swallows: its segment decodes to the commit,
    # and the stretch a carry could send is decoded there already.
    await line.feed(2, QUIET)
    ended(line, 2 * CHUNK)
    await line.stt._send_commit(why="tail")
    assert line.hints() == [None, None] and line.after_commit(1) == []


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


# ---- the wire ------------------------------------------------------------------------

class Gated(WireRecorder):
    """A socket whose appends wait at a gate (back-pressure), whose commits yield, and
    which runs `on_commit` while a commit drains."""

    def __init__(self):
        super().__init__()
        self.gate = None
        self.yield_on_commit = False
        self.on_commit = None
        self.on_gate = None

    async def send(self, raw):
        await super().send(raw)
        kind = self.sent[-1]["type"]
        if kind == "input_audio_buffer.append" and self.gate is not None:
            await self.gate.wait()
            if self.on_gate is not None:
                hook, self.on_gate = self.on_gate, None
                await hook()
        if kind == "input_audio_buffer.commit":
            if self.yield_on_commit:
                for _ in range(3):
                    await asyncio.sleep(0)
            if self.on_commit is not None:
                hook, self.on_commit = self.on_commit, None
                await hook()


async def test_the_engine_clock_moves_back_before_the_carry_drains():
    """A report the receive task handles while the carry's send waits on the socket
    must read early, never late: the origin moves before that send is awaited."""
    line = Tagged()
    wire = line.stt._websocket = Gated()
    await line.utterance()
    wire.gate = asyncio.Event()
    verdict = asyncio.ensure_future(line.verdict(True))
    await settle()
    assert line.wire()[-1]["type"] == "input_audio_buffer.append", "the carry, draining"
    carried = len(audio(line.wire()[-1])) // 2
    assert line.stt._session_origin == -carried, (
        "moved back by the carry before its send is through")
    wire.gate.set()
    await verdict


async def test_a_commit_waits_for_an_append_on_its_way_and_carries_it():
    """A timer's commit while run_stt's send waits on back-pressure: the chunk is on the
    wire already, so the commit goes behind it, placed with it on the send clock, and
    carries it."""
    line = Tagged()
    wire = line.stt._websocket = Gated()
    await line.feed(20, QUIET)
    wire.gate = asyncio.Event()
    stuck = asyncio.ensure_future(line.feed(1, VOICE))
    await settle()
    asyncio.get_running_loop().call_later(0.05, wire.gate.set)
    ended(line, 10 * CHUNK)                        # placed before the chunk on its way
    await line.stt._send_commit(why="tail")
    await stuck
    assert [m["type"] for m in line.wire()[-3:]] == [
        "input_audio_buffer.append", "input_audio_buffer.commit",
        "input_audio_buffer.append"], "the chunk on its way, the commit, its carry"
    assert line.hints() == [ms(11) - MARGIN], line.hints()
    assert audio(line.after_commit()[0]).endswith(chunk(VOICE)), "and the carry holds it"


async def test_no_live_chunk_gets_between_a_commit_and_its_carry():
    """A live chunk that comes in while the commit's send drains waits for the carry:
    the claimed voice (the click) is in the next segment, and the live audio follows."""
    with Journal("WARNING") as j:
        line = Tagged()
        wire = line.stt._websocket = Gated()
        await line.utterance()
        await line.feed(2, QUIET)
        await line.feed(1, VOICE)
        await line.feed(1, QUIET)
        wire.yield_on_commit = True
        live = asyncio.ensure_future(line.feed(1, SPEECH))
        await line.verdict(True)
        await live
        assert line.hints() == [ms(10) - MARGIN], "the click is claimed, as carried"
        behind = line.after_commit()
        assert len(behind) == 2 and chunk(VOICE) in audio(behind[0]), (
            "the carry, with the click, right behind the commit")
        assert audio(behind[1]) == chunk(SPEECH), "then the live chunk"
        assert not j.lines, j.lines


async def test_a_timer_decides_its_commit_on_the_state_after_the_append_it_waited_for():
    """The hold expires while run_stt's send waits on back-pressure, and the caller's
    VAD start is handled before the expiry gets the wire: the expiry must see it and
    leave the resumed segment open."""
    line = Tagged()
    wire = line.stt._websocket = Gated()
    line.stt.smartturn_stop_secs = 0.02
    slack = stt_mod._HOLD_SLACK_SECS
    stt_mod._HOLD_SLACK_SECS = 0.0
    try:
        await line.utterance()
        assert line.stt._commit_pending
        wire.gate = asyncio.Event()

        async def the_caller_resumes():
            await line.stt.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), UP)

        wire.on_gate = the_caller_resumes
        stuck = asyncio.ensure_future(line.feed(1, QUIET))
        await asyncio.sleep(0.08)                  # the expiry is due, and waits
        assert line.commits() == []
        wire.gate.set()
        await stuck
        await settle()
        assert line.commits() == [], "the caller resumed: no commit"
    finally:
        stt_mod._HOLD_SLACK_SECS = slack


@contextlib.contextmanager
def timers(hold=None, stranded=None):
    """The hold's expiry and the backstop, shortened so a test need not sit them out."""
    was = stt_mod._HOLD_SLACK_SECS, stt_mod._STRANDED_INTERIM_SECS
    if hold is not None:
        stt_mod._HOLD_SLACK_SECS = hold
    if stranded is not None:
        stt_mod._STRANDED_INTERIM_SECS = stranded
    try:
        yield
    finally:
        stt_mod._HOLD_SLACK_SECS, stt_mod._STRANDED_INTERIM_SECS = was


async def a_timer_waits_on_a_drain(line, wire, fire_secs, then=None):
    """An append stuck on back-pressure (it holds the wire), and a timer that fires
    behind it: returns the input task, to be let through by setting wire.gate. `then`:
    the frame that task handles next, as soon as the append is through and before it
    yields, as the pipeline's input task goes on to its next queued frame."""
    wire.gate = asyncio.Event()

    async def the_input_task():
        await line.feed(1, QUIET, behind=True)
        if then is not None:
            await then()

    stuck = asyncio.ensure_future(the_input_task())
    await asyncio.sleep(fire_secs + 0.06)          # the timer has fired, and waits
    assert line.commits() == [], "nothing gets past the append on its way"
    return stuck


async def test_the_expiry_and_a_late_verdict_raced_across_a_drain_commit_once():
    """The hold expires while an append drains; the ceiling's verdict, late, is the
    next frame the input task handles. One of the two answers the stop -- the expiry,
    which got the wire first -- and the verdict is logged late, not lost. (No analyzer
    marks: the verdict commits at once, with no catch-up to wait out.)"""
    with timers(hold=0.0), Journal("WARNING") as j:
        line = Tagged(wired=False)
        wire = line.stt._websocket = Gated()
        line.stt.smartturn_stop_secs = 0.02
        await line.utterance()
        await line.verdict(False)
        stuck = await a_timer_waits_on_a_drain(
            line, wire, 0.02, then=lambda: line.verdict(True, source="ceiling"))
        wire.gate.set()
        await stuck
        await settle()
        assert [c.why for c in line.stt._commits] == ["hold-expired"], (
            f"one commit for the stop; got {[c.why for c in line.stt._commits]}")
        assert j.having("late, not lost"), j.lines


async def test_a_pause_wait_raced_against_a_drain_and_a_reconnect_commits_nothing():
    """The caller pauses while an append drains, so the wait for their pause decides to
    commit and waits for the wire. The session closes and a new one opens before it
    gets it: the close cancels the wait (its slot is still the wait's), and nothing
    goes out on the new session."""
    line = Tagged()
    wire = line.stt._websocket = Gated()
    wire.state = None
    await line.utterance()
    await line.feed(4, VOICE, behind=True)
    await line.verdict(True)
    await line.catch_up()
    await settle()
    await line.feed(7, QUIET, behind=True)         # the pause, not yet heard
    wire.gate = asyncio.Event()
    stuck = asyncio.ensure_future(line.feed(1, QUIET, behind=True))
    await settle()
    await line.catch_up()                          # heard: the wait decides to commit
    await settle()
    assert line.commits() == [], "the wait is waiting for the wire"
    fresh = await reconnect(line)
    wire.gate.set()
    await stuck
    await settle()
    assert line.commits() == [] and wire.commits() == [], (
        f"no commit on either session; got {fresh.types()}")


async def reconnect(line):
    """The session closes and a new one opens, as the receive task's reconnect does."""
    fresh = WireRecorder()

    async def connect(url):
        return fresh

    real = stt_mod.websockets.connect
    stt_mod.websockets.connect = connect
    try:
        await line.stt._disconnect_websocket()
        await line.stt._connect_websocket()
    finally:
        stt_mod.websockets.connect = real
    return fresh


async def test_an_expiry_raced_against_a_drain_and_a_reconnect_commits_nothing():
    """The same race for the hold's expiry: its slot stays its own until it has the
    wire, so the close cancels it."""
    with timers(hold=0.0):
        line = Tagged()
        wire = line.stt._websocket = Gated()
        wire.state = None
        line.stt.smartturn_stop_secs = 0.02
        await line.utterance()
        stuck = await a_timer_waits_on_a_drain(line, wire, 0.02)
        fresh = await reconnect(line)
        wire.gate.set()
        await stuck
        await settle()
        assert line.commits() == [] and wire.commits() == [], (
            f"no commit on either session; got {fresh.types()}")


async def test_a_backstop_raced_against_a_drain_stands_down_for_a_fresh_interim():
    """The backstop fires while an append drains, and an interim lands before it gets
    the wire: the engine is still streaming, the quiet it fired on is broken, and the
    re-arm cancels it there (its slot is still its own)."""
    with timers(stranded=0.02):
        line = Tagged()
        wire = line.stt._websocket = Gated()
        await line.feed(20, SPEECH)
        await line.stt._handle_message({"type": "transcription.delta", "delta": "I"})
        stuck = await a_timer_waits_on_a_drain(line, wire, 0.02)
        stt_mod._STRANDED_INTERIM_SECS = 5.0       # the re-armed one, out of the way
        await line.stt._handle_message({"type": "transcription.delta", "delta": " mean"})
        wire.gate.set()
        await stuck
        await settle()
        assert line.commits() == [], "the backstop that fired was cancelled by the re-arm"
        line.stt._stranded_task.cancel()


async def test_a_backstop_raced_against_a_stop_carries_nothing():
    """The backstop fires while an append drains, and the VAD's stop over the bot's
    reply is the next frame handled. The backstop gets the wire first and commits what
    it was armed for: no speech end is placed yet, so it claims and carries nothing.
    The stop then places its own and commits the flush."""
    with timers(stranded=0.02):
        line = Tagged()
        wire = line.stt._websocket = Gated()
        await line.stt.process_frame(BotStartedSpeakingFrame(), UP)
        await line.feed(20, SPEECH)
        await line.stt._handle_message({"type": "transcription.delta", "delta": "No,"})
        stuck = await a_timer_waits_on_a_drain(
            line, wire, 0.02, then=lambda: line.stt.process_frame(
                VADUserStoppedSpeakingFrame(stop_secs=0.2), UP))
        wire.gate.set()
        await stuck
        await settle()
        assert [c.why for c in line.stt._commits] == ["backstop", "bot-speaking"], (
            [c.why for c in line.stt._commits])
        assert line.hints()[0] is None and line.stt._commits[0].carry_ms == 0, (
            "the backstop claims and carries nothing")


async def test_a_session_closed_under_a_commit_takes_its_stamp_and_carry():
    with Journal("WARNING") as j:
        line = Tagged()
        wire = line.stt._websocket = Gated()
        await line.utterance()
        fresh = WireRecorder()

        async def reconnect():
            line.stt._websocket = fresh
            line.stt._commits.clear()

        wire.on_commit = reconnect
        await line.verdict(True)
        assert wire.types()[-1] == "input_audio_buffer.commit", "no carry on the old one"
        assert fresh.sent == [], "nor on the new one"
        assert not line.stt._commits, "and no stamp for a done that will not come"
        assert j.having("closed under a commit"), j.lines


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
