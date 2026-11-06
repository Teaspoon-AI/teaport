#
# Unit test: the consult narrator must stop before the answer is spoken.
#
# _consult_progress is a fixed countdown against dead air — "Still looking up ..." (or
# "Still on it.") at 9s, "This one's taking a bit longer." at 22s — spoken while a
# background openclaw_agent_consult runs. It used to be cancelled in the waiter's
# finally, which was harmless only while the follow-up injector returned as soon as it
# had queued the LLM run.
#
# When the injector waited for the delivered turn to FINISH SPEAKING (to retire the
# one-shot trigger it put in the context), the narrator ran on through the whole
# delivery. Live 2026-08-26 09:44: the consult started at :15.3, the shop list was spoken
# at :25.0, and "Almost there — hang tight." landed at :37.3 — twelve seconds after the
# user already had the answer.
#
# So the contract is an ORDER, not a cleanup: the narrator is cancelled before the
# outcome lands (owed.Owed.land), on every path out of the waiter.
#
# Run: python test_consult_progress.py
#
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from teaport_brain import tools  # noqa: E402
from teaport_brain import i18n  # noqa: E402

ANSWER = "Upper Crust Bakery, Neon Belly, Russell's Bakery."


class _LLM:
    """Records what the narrator says, and when, relative to the delivery."""

    def __init__(self):
        self.narrated = []
        self.delivering = False
        self.narrated_during_delivery = []

    async def push_frame(self, frame):
        if not isinstance(frame, _Speak):
            return                     # the client's "working" messages, not speech
        self.narrated.append(frame.text)
        if self.delivering:
            self.narrated_during_delivery.append(frame.text)


async def _fast_progress(llm, status_phrase=None, gate=None, tts=None):
    """The real narrator's shape, on a timescale a test can wait out. Accepts the
    status_phrase/gate kwargs _consult_and_land now passes, and ignores them — the
    ordering-vs-delivery contract these tests check is independent of them."""
    try:
        await asyncio.sleep(0.05)
        await llm.push_frame(_Speak("Still on it."))
        await asyncio.sleep(0.05)
        await llm.push_frame(_Speak("This one's taking a bit longer."))
    except asyncio.CancelledError:
        pass


class _Speak:
    def __init__(self, text):
        self.text = text


async def _run(consult_result):
    llm = _LLM()
    delivered = []

    class Owed:
        def land(self, request, text, key, *, failure=None, detail=""):
            # The landing (owed.Owed): from here the answer is the caller's, and the
            # narrator must not speak again -- watched for long enough for it to fire
            # twice if it is alive.
            llm.delivering = True
            delivered.append(text)

    fut = asyncio.get_running_loop().create_future()
    fut.set_result(consult_result)
    real_progress = tools._consult_progress
    tools._consult_progress = _fast_progress
    try:
        await tools._consult_and_land(
            "call-1", fut, "pastry shops on Burnet", Owed(), "tc-1", llm=llm)
        await asyncio.sleep(0.2)
    finally:
        tools._consult_progress = real_progress
    return llm, delivered


async def test_the_narrator_is_silent_once_the_answer_is_delivered():
    llm, delivered = await _run({"text": ANSWER})
    assert delivered == [ANSWER], delivered
    assert not llm.narrated_during_delivery, (
        f"the narrator spoke {llm.narrated_during_delivery!r} while the answer was being "
        f"delivered — the user hears 'Almost there' after already having the answer")


async def test_a_failed_consult_also_stops_the_narrator():
    """The failure path delivers too, and must not narrate over its own apology."""
    llm, delivered = await _run({"error": "the desktop agent did not answer"})
    assert delivered == [None], delivered
    assert not llm.narrated_during_delivery, llm.narrated_during_delivery


# --- the real _consult_progress: names the task, and fits into a gap ---

class _Gate:
    """Stub FollowupGate. `gap` is what wait_until_idle reports: True == a clear
    moment opened (speak), False == none within the window (skip)."""

    def __init__(self, gap, calls_out=False):
        self.gap = gap
        self.calls_out = calls_out
        self.waits = 0

    async def wait_until_idle(self, max_wait=None):
        self.waits += 1
        return self.gap


class _Recorder:
    def __init__(self):
        self.said = []

    async def push_frame(self, frame):
        self.said.append(getattr(frame, "text", ""))


EN = i18n.get("en")
PHRASE = "looking up pastry shops on Burnet Road"


def _ack(phrase, t=EN):
    """What the consult's acknowledgement says for a model that called it silently."""
    args = {"question": "Find good pastry shops on Burnet Road in Austin"}
    if phrase is not None:
        args[tools.CONSULT_STATUS] = phrase
    return tools._fallback_line("openclaw_agent_consult", args, t)


async def test_the_acknowledgement_and_first_line_say_the_models_phrase():
    """#120: one phrase the model wrote for the listener, said whole at both moments."""
    assert _ack(PHRASE) == "Sure — looking up pastry shops on Burnet Road.", _ack(PHRASE)
    assert tools._progress_line(PHRASE, 0, EN) == "Still looking up pastry shops on Burnet Road."
    # Only its edges are tidied: a capital (it opens the second half of a sentence),
    # wrapping quotes, spacing and a closing stop of its own.
    for written in ("Looking up pastry shops on Burnet Road.", f'"{PHRASE}"',
                    "  looking up  pastry shops\non Burnet Road  "):
        assert _ack(written) == "Sure — looking up pastry shops on Burnet Road.", written


async def test_no_phrase_gets_the_plain_lines():
    for missing in (None, "", "   ", 7, ["looking"], {"text": PHRASE}):
        assert _ack(missing) == "On it.", missing
        assert tools._progress_line(missing, 0, EN) == "Still on it.", missing
        tools._log_status_phrase(missing, None)     # and the consult's log line holds
    # A malformed call (arguments that are not an object) still gets its acknowledgement.
    assert tools._fallback_line("openclaw_agent_consult", "not a dict", EN) == "On it."


async def test_a_phrase_that_does_not_fit_is_never_cut():
    """The old topic echo cut the request at nine words or sixty characters and still
    ended mid-thought ("...pastry shops on Burnet Rd in"). Too long, not opening with an
    -ing word, not prose, or written for the agent: the plain lines, never a piece."""
    too_long = "looking up the best pastry shops and bakeries on Burnet Road in north Austin"
    assert len(too_long.split()) > tools._STATUS_MAX_WORDS
    too_wide = "checking" + " antidisestablishmentarianism" * 3   # few words, many letters
    assert len(too_wide.split()) <= tools._STATUS_MAX_WORDS < tools._STATUS_MAX_CHARS < len(too_wide)
    for bad in (too_long, too_wide,
                # the request's own register, from the box (2026-10-09/10)
                "Fetch the current top story headline from Hacker News.",
                "summarizing the latest news headlines for the user",
                "Please summarize the latest news headlines",
                # an -ing word that is no verb, and a sentence of its own
                "something about the pastry shop", "I'm looking that up",
                # not prose
                "opening https://news.example.com/x", '{"task": "summarize"}'):
        assert _ack(bad) == "On it.", (bad, _ack(bad))
        assert tools._progress_line(bad, 0, EN) == "Still on it.", bad
        assert tools._status_phrase(bad, "en")[0] == "", bad
    # One word over the cap is too long; the cap itself is said whole.
    at_cap = " ".join(["checking"] + ["the"] * (tools._STATUS_MAX_WORDS - 2) + ["menu"])
    assert _ack(at_cap) == f"Sure — {at_cap}.", _ack(at_cap)
    assert _ack(at_cap + " today") == "On it."


async def test_quotes_come_off_only_as_a_pair_around_the_whole_phrase():
    """Review of #329: a closing quote was stripped with the stop, leaving its opening
    one ('Sure — searching for "best tacos.'), which on Talk goes out as a caption."""
    for written, said in (
            ('Searching for "best tacos"', 'Sure — searching for "best tacos".'),
            ('looking up the "Hamilton"', 'Sure — looking up the "Hamilton".'),
            ('looking up "Hamilton" tickets.', 'Sure — looking up "Hamilton" tickets.'),
            ("Searching for 'best tacos'", "Sure — searching for 'best tacos'."),
            ('"looking up the weather."', "Sure — looking up the weather."),
            ('"looking up the weather".', "Sure — looking up the weather."),
            ("\u201clooking up the weather\u201d", "Sure — looking up the weather."),
            ("'checking Joe's calendar'", "Sure — checking Joe's calendar.")):
        assert _ack(written) == said, (written, _ack(written))
    # Two quotations, not one pair around it all: nothing comes off, and a quote mark
    # before the -ing word is no -ing word.
    assert _ack('"looking up" the "Hamilton"') == "On it."


async def test_only_a_word_written_to_open_a_sentence_is_lowercased():
    """Review of #329: the first letter was lowercased whatever the word ("lOOKING"),
    and a name that ends in -ing passed as a gerund ("Sure — wyoming weather ...")."""
    assert _ack("Looking up NASA's launch") == "Sure — looking up NASA's launch."
    assert _ack("LOOKING UP THE WEATHER") == "Sure — LOOKING UP THE WEATHER."
    for name in ("Wyoming weather this weekend", "Beijing time right now"):
        assert _ack(name) == "On it.", name


async def test_a_verb_missing_its_object_gets_the_plain_lines():
    """Review of #329: "Looking up…" became "Sure — looking up.", which sounds whole.
    A verb with a particle that needs an object, and nothing after it, is a fragment."""
    for fragment in ("Looking up…", "looking up...", "Looking up.", "looking into",
                     "checking on", "working on", '"Looking up..."', "'looking up'"):
        assert _ack(fragment) == "On it.", fragment
        assert tools._status_phrase(fragment, "en") == ("", "ends mid-thought"), fragment
    # Round 2: a trailing "…" alone decides nothing (bare "checking" is said too), and a
    # phrasal verb that is whole without an object is said.
    for whole in ("checking", "checking…", "Looking it up…", "Checking traffic…",
                  "Checking flights...", "Booking it…", "Searching now…", "logging in",
                  "signing up", "following up", "backing up", "checking out",
                  "calling back", "holding on", "checking up on that", "looking into it"):
        phrase = tools._status_phrase(whole, "en")[0]
        assert phrase and _ack(whole) == f"Sure — {phrase}.", (whole, _ack(whole))


async def test_a_phrase_that_stops_mid_thought_gets_the_plain_lines():
    """Review of #329, round 3: a longer phrase that stops where a word should follow
    was said ("Sure — looking up flights to.", "Sure — looking up the.")."""
    for short in ("looking up flights to", "looking up the…", "looking up the...",
                  "looking up flights for", "reading the news for my", "checking up on",
                  "looking up flights in", "finding a table at", 'looking up "the',
                  "booking a room with a"):
        assert tools._status_phrase(short, "en") == ("", "ends mid-thought"), short
        assert _ack(short) == "On it.", short
        assert tools._progress_line(short, 0, EN) == "Still on it.", short
    # A pronoun ends a phrase whole, and so does a phrasal verb with its own particle.
    for whole in ("looking up flights to Denver", "looking into that", "looking into this",
                  "calling her", "checking in", "logging in", "holding on",
                  "looking it up", "checking on your order", "picking the kids up"):
        assert _ack(whole) == f"Sure — {whole}.", (whole, _ack(whole))


async def test_the_listener_named_in_the_third_person_is_refused():
    """Review of #329: round 1 refused "the user" only after a few verbs, so "informing
    the user" passed. Refused wherever the listener is named in the third person, the
    way an instruction to the agent names them; said where "user" or "caller" is part
    of a thing's name."""
    for agents in ("summarizing the latest news headlines for the user",
                   "summarizing the news for the users", "sending the email to the user",
                   "checking the user's calendar", "checking the users' accounts",
                   "checking with the caller's bank", "reading the messages to the caller",
                   "finding pastry shops near the user", "booking a table on behalf of the user",
                   "informing the user", "helping the user find a plumber",
                   "checking what the user asked", "updating the user", "emailing the user",
                   "texting the caller", "finding coffee near the caller", "looking up the user",
                   "asking the User to confirm"):
        assert tools._status_phrase(agents, "en") == ("", "not said to the listener"), agents
        assert _ack(agents) == "On it.", agents
    # Round 3: after a verb aimed at the listener, "the user" is the listener whatever
    # follows, even a word that could make it a thing's name.
    for agents in ("showing the user names of nearby cafes",
                   "giving the user list of options", "sending the user ID and password",
                   "telling the caller name of the shop", "emailing the users guide"):
        assert tools._status_phrase(agents, "en") == ("", "not said to the listener"), agents
    for listeners in ("checking the user manual for the router",
                      "looking up the caller ID for that number",
                      "reading the user guide", "checking the users forum"):
        assert _ack(listeners) == f"Sure — {listeners}.", listeners


async def test_names_ending_in_ing_are_no_gerund():
    for name in ("Wyoming weather this weekend", "Beijing time right now",
                 "Lansing weather tomorrow", "Redding weather", "Fleming book reviews",
                 "Ewing theater tickets", "Corning stock", "Boeing stock price"):
        assert _ack(name) == "On it.", name
    assert _ack("Reading the news") == "Sure — reading the news."


async def test_the_second_line_claims_nothing():
    """It was "Almost there", said of consults nowhere near done: now the same honest
    line whatever the phrase."""
    for phrase in (PHRASE, None, "Fetch the news"):
        assert tools._progress_line(phrase, 1, EN) == "This one's taking a bit longer."


async def test_another_language_gets_the_plain_lines_not_the_phrase():
    """#290/#300: the -ing check is English, and the model's phrase in another session
    may be English or may not fit the frames, so neither is said there."""
    fr = i18n.get("fr")
    for phrase in (PHRASE, "je cherche des pâtisseries sur Burnet Road"):
        assert _ack(phrase, fr) == "Je m'en occupe.", _ack(phrase, fr)
        assert tools._progress_line(phrase, 0, fr) == "Je m'en occupe toujours."
        assert "—" not in tools._progress_line(phrase, 1, fr)
        assert tools._status_phrase(phrase, "fr") == ("", "a fr session")
    assert tools._progress_line(PHRASE, 1, fr) == "Cette demande prend un peu plus de temps."


async def test_agent_first_still_says_no_acknowledgement():
    saved = tools.AGENT_FIRST
    tools.AGENT_FIRST = True
    try:
        assert _ack(PHRASE) is None
    finally:
        tools.AGENT_FIRST = saved


async def test_the_narrator_speaks_the_voices_language_live():
    class _Voice:
        espeak_language = "en-us"
    voice, llm = _Voice(), _Recorder()
    saved = tools._PROGRESS_SCHEDULE
    tools._PROGRESS_SCHEDULE = (0.0, 0.0)
    try:
        await tools._consult_progress(llm, status_phrase=PHRASE, gate=None, tts=voice)
        voice.espeak_language = "es"        # a switch_voice between lines
        llm._teaport_progress_active = False
        await tools._consult_progress(llm, status_phrase=PHRASE, gate=None, tts=voice)
    finally:
        tools._PROGRESS_SCHEDULE = saved
    assert llm.said[:2] == ["Still looking up pastry shops on Burnet Road.",
                            "This one's taking a bit longer."], llm.said
    assert llm.said[2:] == ["Sigo en ello.", "Esto está tardando un poco más."], llm.said


async def test_the_consult_hands_its_phrase_to_the_narrator():
    """The handler reads the model's phrase off the call and the waiter gives it to
    the narrator; the question goes on to the agent without it (_consult_args)."""
    got = {}

    async def fake_land(call_id, fut, request, owed, tool_call_id, **kwargs):
        got.update(request=request, **kwargs)

    class _Owed:
        def dispatch(self, tool_call_id, request):
            return None

    class _Llm:
        tasks = []

        def create_task(self, coro):
            self.tasks.append(asyncio.get_running_loop().create_task(coro))

        async def push_frame(self, frame, direction=None):
            pass

    class _P:
        llm, tool_call_id = _Llm(), "tc-1"
        arguments = {"question": "Find pastry shops on Burnet Road",
                     tools.CONSULT_STATUS: PHRASE}

        async def result_callback(self, result, properties=None):
            pass

    real = tools._consult_and_land
    tools._consult_and_land = fake_land
    try:
        await tools._openclaw_agent_consult(_P(), owed=_Owed())
        await asyncio.gather(*_Llm.tasks)
    finally:
        tools._consult_and_land = real
    assert got["request"] == "Find pastry shops on Burnet Road", got
    assert got["status_phrase"] == PHRASE, got
    assert tools.CONSULT_STATUS not in tools._consult_message(
        tools._consult_args(_P.arguments))
    assert tools._consult_args(_P.arguments) == {"question": "Find pastry shops on Burnet Road"}

    seen = []

    async def recording_progress(llm, status_phrase=None, gate=None, tts=None):
        seen.append(status_phrase)
    fut = asyncio.get_running_loop().create_future()
    # Answered a moment later, once the narrator has started.
    asyncio.get_running_loop().call_later(0.05, fut.set_result, {"text": ANSWER})

    class _Land:
        def land(self, *a, **k):
            pass
    real_progress = tools._consult_progress
    tools._consult_progress = recording_progress
    try:
        await tools._consult_and_land("call-1", fut, "q", _Land(), "tc-1", llm=_LLM(),
                                      status_phrase=PHRASE)
    finally:
        tools._consult_progress = real_progress
    assert seen == [PHRASE], seen


class _Timed(_Recorder):
    def __init__(self):
        super().__init__()
        self.at = []

    async def push_frame(self, frame):
        await super().push_frame(frame)
        self.at.append(time.monotonic())


async def test_the_schedule_is_wall_clock_so_a_gap_wait_does_not_push_the_next_line():
    """_PROGRESS_SCHEDULE names the instants the lines are due, from the consult's
    start. Sleeping the schedule's entries one after another, each AFTER the previous
    line's gap wait, stacked every wait onto the next line: two 6s waits made a
    22s schedule a 34s one. Here the gate takes 0.3s to find a gap and the second
    line is due at 0.4s: it must fire at ~0.7s (its moment, then its own wait), not
    at 1.0s (the first line's wait, then 0.4s more, then its own)."""
    class SlowGate:
        calls_out = False

        async def wait_until_idle(self, max_wait=None):
            await asyncio.sleep(0.3)
            return True
    saved = (tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT)
    tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT = (0.0, 0.4), 1.0
    llm = _Timed()
    llm._teaport_progress_active = False
    t0 = time.monotonic()
    try:
        await tools._consult_progress(llm, status_phrase="anything", gate=SlowGate())
    finally:
        tools._PROGRESS_SCHEDULE, tools._PROGRESS_GAP_WAIT = saved
    assert len(llm.said) == 2, llm.said
    first, second = (t - t0 for t in llm.at)
    assert 0.25 < first < 0.5, first
    assert second < 0.85, f"the second line fired at {second:.2f}s: the gap wait stacked"


async def _drive_progress(gap, calls_out=False):
    saved = tools._PROGRESS_SCHEDULE
    tools._PROGRESS_SCHEDULE = (0.0, 0.0)  # no countdown; exercise the gating only
    gate = _Gate(gap, calls_out)
    llm = _Recorder()
    llm._teaport_progress_active = False
    try:
        await tools._consult_progress(llm, status_phrase="looking into the H-matrix question",
                                      gate=gate)
    finally:
        tools._PROGRESS_SCHEDULE = saved
    return llm, gate


async def test_the_narrator_speaks_when_a_gap_opens():
    llm, gate = await _drive_progress(gap=True)
    assert len(llm.said) == 2, llm.said
    assert "H-matrix question" in llm.said[0], llm.said[0]
    assert gate.waits == 2, "it should check for a gap before each line"


async def test_the_narrator_stays_quiet_when_there_is_no_gap():
    llm, gate = await _drive_progress(gap=False)
    assert llm.said == [], (
        f"the narrator talked over the user instead of skipping: {llm.said!r}")
    assert gate.waits == 2, "it still checks (and skips) each line"


async def test_the_narrator_stays_quiet_while_a_tool_call_waits():
    """A quiet line with a call out (openclaw_agent_control's wait reads idle: the call
    released the LLM flag): a line said then is audio the Talk client holds the
    control's result behind."""
    llm, gate = await _drive_progress(gap=True, calls_out=True)
    assert llm.said == [], llm.said


async def test_no_gate_falls_back_to_speaking_unconditionally():
    # The SIP path and any caller without a gate keep the original behaviour.
    saved = tools._PROGRESS_SCHEDULE
    tools._PROGRESS_SCHEDULE = (0.0, 0.0)
    llm = _Recorder()
    llm._teaport_progress_active = False
    try:
        await tools._consult_progress(llm, status_phrase="anything", gate=None)
    finally:
        tools._PROGRESS_SCHEDULE = saved
    assert len(llm.said) == 2, llm.said


def main():
    aio = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and asyncio.iscoroutinefunction(v)]

    async def run_aio():
        for fn in aio:
            await fn()
            print(f"  ok {fn.__name__}")
    asyncio.run(run_aio())


if __name__ == "__main__":
    main()
    print("ALL PASS")
