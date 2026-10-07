#
# teaport — Wi-Fi setup, the voice half (the setup network and page are wifi_setup.py).
#
# A box with no internet has no LLM either, so this half needs none: it listens for a
# fixed phrase ("set up Wi-Fi", "connect to Wi-Fi", ...) in the user's final transcript,
# asks them to confirm (the box goes offline while it runs), starts
# teaport-wifi-setup.service, and from then on speaks fixed sentences: the setup
# network's name and password, digit by digit, the page's address, and what became of
# the attempt, read from the unit's status.json. While it runs it keeps the user's words
# from the LLM and answers "repeat" and "cancel" itself.
#
# The online way in is the wifi_setup tool (tools.py): the model hands over to the same
# flow. Both exist only where that tool is active — TEAPORT_TOOL_WIFI_SETUP on, the
# client announced "local" (the local audio bridge: someone is at the box, not a remote
# Talk user or a phone caller who could cut the box off its own network), and the setup
# unit is installed.
#
# It speaks the session's language — its voice's, read live, so a switch_voice mid-session
# switches setup too — through the catalogs in i18n.py (the "start", "yes", ... phrases
# included); the phone page (wifi_setup.py) follows the phone's own language.
#
# A transcript it takes is not dropped: it goes on with its text emptied and marked
# (taken()), so the turn it opened still closes -- SetupTurnCloser, a stop strategy in the
# user aggregator, ends that turn with nothing to answer. Dropped, the turn's stop
# strategy would wait for words that never come (and for the segment close the final
# carries, endpointing.py), and the turn would hang open for the aggregator's 5 s
# inactivity timeout with barge-in dead -- the wedge stt.py describes for empty finals.
#
import asyncio
import dataclasses
import json
import math
import os
import re
import time
import unicodedata
from collections import deque

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.base_user_turn_stop_strategy import BaseUserTurnStopStrategy

from teaport_brain import i18n
from teaport_brain.i18n import N_

UNIT = "teaport-wifi-setup"
UNIT_FILE = f"/etc/systemd/system/{UNIT}.service"
# The root-owned config helper the installer puts in sudoers (config_ui.py uses it too).
APPLY_HELPER = "/usr/local/lib/teaport/config_apply.py"
SYSTEM_PYTHON = "/usr/bin/python3"
STATUS_PATH = "/run/teaport-wifi-setup/status.json"
POLL_SECS = 0.5
REMIND_SECS = 120.0
# No status this long after starting the unit: it did not start.
START_TIMEOUT_SECS = 60.0
# How often a running setup checks that its unit is still there. A unit that dies
# without a last word (killed for memory, by RuntimeMaxSec, by a stop from elsewhere)
# leaves its status file at whatever phase it had reached; without this the voice would
# wait on a dead setup forever, keeping every word from the LLM.
LIVENESS_SECS = 5.0
# Past the unit's own hard stop (RuntimeMaxSec=900 in teaport-wifi-setup.service), with
# room: no setup is still running by now, whatever systemctl says (or cannot say).
DEADLINE_SECS = 16 * 60.0
# An unanswered "do you want to set up Wi-Fi?" lapses: the next words, this long after
# the question, are ordinary conversation again, not a yes or a no to it.
CONFIRM_SECS = 30.0
# A yes is an answer, not a sentence that happens to hold "okay" or "please": at most
# this many words (CJK: about three characters to a word) and no question mark.
YES_MAX_WORDS = 4
# What the box says can come back through the microphone (the card's echo cancellation is
# good, not perfect: local_audio.py), and its lines are full of the words it listens for
# ("Say yes or no", "or cancel to stop"). A transcript that is a piece of a line it said,
# heard while it speaks or this soon after, is that echo.
ECHO_TAIL_SECS = 1.5
ECHO_LINES_SECS = 120.0
# The setup network's name is teaport-<hex>: the word is said as a word.
SSID_WORD = "teaport"

# What the user says, by meaning. The English patterns are the msgids of the "pattern"
# entries in each language's catalog (i18n.py), whose msgstr is that language's own
# pattern: a session listens for its language's words and always for the English ones.
PATTERNS = {
    "start": r"\b(?:set ?up|setup|configure|connect to|join|change|switch)\b(?: (?:the|a|my|your|to|new|another|different)){0,3} ?wi-?fi\b|\bwi-?fi (?:set ?up|setup)\b",
    "yes": r"\b(?:yes|yeah|yep|sure|okay|ok|go ahead|do it|please|start)\b",
    "no": r"\b(?:no|nope|don't|do not|never ?mind|cancel|stop)\b",
    "cancel": r"\b(?:cancel|stop|quit|exit|abort|never ?mind)\b",
    "repeat": r"\b(?:repeat|again|what was|what's the|password|network name|say that)\b",
}

# English words that make a "start" match a question or a complaint about Wi-Fi rather
# than a request to set up the box's: "how do I change my wifi password?", "my laptop
# can't connect to wifi", "set up wifi on my phone". Those go to the model, which can
# still hand over (the wifi_setup tool) if setup is what they want. English only, as the
# English start pattern is heard in every session; a language's own pattern is its
# catalog's to keep narrow.
_NOT_A_REQUEST = re.compile(
    r"\b(?:how|why|can['’]?t|cannot|couldn['’]?t|won['’]?t|doesn['’]?t|didn['’]?t|isn['’]?t"
    r"|wasn['’]?t|unable|trouble|problem)\b"
    r"|\bwi-?fi (?:password|passcode|settings?|name|network name|is|was|keeps)\b"
    r"|\bon (?:my|his|her|their|our|the|this) (?:phone|laptop|computer|tablet|tv|mac|pc"
    r"|ipad|iphone|kindle)\b", re.I)

_TASHKEEL = re.compile("[ً-ٰٟـ]")
_QUESTION = re.compile("[?？؟]")
# Scripts written without spaces between words (Han, kana): sized by characters.
_UNSPACED = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def heard(meaning: str, text: str, t: i18n.T) -> bool:
    """Did the user say `meaning` ("start", "yes", ...) in English or in t's language?
    Except "no": in another language English's "no" is often an ordinary word (Portuguese
    "no celular", Italian "come no" = of course), and a false "no" throws the setup away,
    so there only the language's own "no" counts (every catalog has its word for it)."""
    # Arabic short vowels, shadda and tatweel are optional in writing and an STT may or
    # may not emit them: match without them (no other catalog's script uses these).
    text = _TASHKEEL.sub("", text)
    if meaning == "start" and _NOT_A_REQUEST.search(text):
        return False
    english = PATTERNS[meaning]
    own = t.p("pattern", english)
    patterns = {own} if meaning == "no" and t.lang != i18n.SOURCE_LANG else {english, own}
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error:
            logger.warning(f"wifi setup: bad {t.lang} pattern for {meaning!r} — fix its .po entry")
    return False


def words(text: str) -> int:
    """About how many words `text` holds, in any of the catalogs' scripts: punctuation
    and symbols split, a run of Han or kana counts a word per three characters (a
    Japanese or Chinese answer has no spaces to count)."""
    spaced = "".join(" " if unicodedata.category(c)[0] in "PSZ" else c for c in text)
    n = 0
    for token in spaced.split():
        unspaced = len(_UNSPACED.findall(token))
        n += max(1, math.ceil(unspaced / 3)) if unspaced else 1
    return n


def is_yes(text: str, t: i18n.T) -> bool:
    """A yes to "do you want to set up Wi-Fi?": the language's (or English's) yes, said as
    an answer -- short and not a question. "Okay, so what's the weather tomorrow?" and
    "Please tell me a joke" hold a yes word and are not one; taking them for a yes would
    take the box offline."""
    return (heard("yes", text, t) and not _QUESTION.search(text)
            and words(text) <= YES_MAX_WORDS)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def available() -> bool:
    """The setup unit and the helper that starts it are installed."""
    return os.path.exists(UNIT_FILE) and os.path.exists(APPLY_HELPER)


# Characters that are neither digits nor letters, by the name a voice says (catalog
# context "spell"). wifi_setup.PASSWORD_SYMBOLS is this set's keys: a flash-time password
# may use no other symbol.
_SYMBOLS = {"-": "dash", "_": "underscore", ".": "dot", " ": "space", "@": "at sign",
            "!": "exclamation mark", "#": "hash", "&": "ampersand", "*": "star"}


def spell(text: str, t: i18n.T) -> str:
    """Character by character, in t's language: 'teaport-9e35' -> 'teaport, dash, nine,
    E, three, five'. The setup network's word (SSID_WORD, before its dash) is said as a
    word; nothing else is, so a password is always spelled out. If the text holds any
    uppercase letter, each one is said as "capital X" (a password is case-sensitive, and
    one in capitals alone typed in lowercase does not work)."""
    head, sep, rest = text.partition("-")
    if sep and head == SSID_WORD:
        words_, chars = [head, t.p("spell", "dash")], rest
    else:
        words_, chars = [], text
    capitals = any(c.isupper() for c in chars)
    for c in chars:
        if c.isdigit() and c.isascii():
            words_.append(t.p("digit", c))
        elif c.isalpha() and c.isascii():
            # A letter by its name. Optional catalog context "letter" (A-Z): a language
            # whose letter names collide with its digit words names them its own way
            # (Korean says E like 2); without an entry the voice gets the letter itself.
            name = t.p("letter", c.upper())
            words_.append(t.p("spell", "capital {letter}").format(letter=name)
                          if capitals and c.isupper() else name)
        elif c.isalpha():
            words_.append(c)
        elif c in _SYMBOLS:
            words_.append(t.p("spell", _SYMBOLS[c]))
        else:
            words_.append(c)
    return t.p("spell", ", ").join(words_)


def instructions(status: dict, t: i18n.T) -> str:
    ssid, password = status.get("ssid", ""), status.get("password", "")
    name = ssid.split("-", 1)[1] if ssid.startswith(SSID_WORD + "-") else ""
    if name:
        parts = spell(name, t).split(t.p("spell", ", "))
        url = " ".join([SSID_WORD, t.p("spell", "dash"), *parts, t.p("spell", "dot"), "local"])
    else:
        # The setup network's address, digit by digit: "one zero dot four two dot ...".
        url = f" {t.p('spell', 'dot')} ".join(
            " ".join(t.p("digit", d) for d in part) for part in "10.42.0.1".split("."))
    said = t._("On your phone, join the Wi-Fi network {ssid}. The password is {password}. "
               "A setup page should open by itself. If it doesn't, go to {url}.").format(
        ssid=spell(ssid, t), password=spell(password, t), url=url)
    # The box's display has them too, and a code that joins (wifi_setup.py, display.py).
    if status.get("qr"):
        said += " " + t._("You can also scan the code on my screen to join.")
    elif status.get("screen"):
        said += " " + t._("You can also read these details on my screen.")
    return said


# ------------------------------------------------------------------ taken transcripts

_TAKEN = "teaport_wifi_setup_took"


def taken(frame: TranscriptionFrame) -> TranscriptionFrame:
    """The transcript, as it goes on once setup has taken it: a new frame of the same
    class (FinalTranscriptionFrame stays uninterruptible and keeps the VAD stop it
    answers, so the stop strategy still gets its segment close), with no words for the
    aggregator to commit, marked for SetupTurnCloser."""
    out = dataclasses.replace(frame, text="")
    setattr(out, _TAKEN, True)
    return out


def is_taken(frame: Frame) -> bool:
    return isinstance(frame, TranscriptionFrame) and getattr(frame, _TAKEN, False)


def handed_back(frame: TranscriptionFrame, text: str) -> TranscriptionFrame:
    """A transcript setup held, given back to the model ahead of `frame`: same class and
    speaker, its own words, and no VAD stop stamp -- it closes no segment (its own was
    closed when it was taken), so it cannot end the turn it joins."""
    fields = {f.name for f in dataclasses.fields(frame)}
    return dataclasses.replace(frame, text=text, **({"stop_n": None} if "stop_n" in fields else {}))


class SetupTurnCloser(BaseUserTurnStopStrategy):
    """A user-turn stop strategy (agent_session.py lists it first): a transcript Wi-Fi
    setup took ends the turn it opened, with nothing to answer. The words of the turn's
    earlier segments are dropped with it ("hey can you... set up the Wi-Fi": the "hey
    can you" is not a question for the model). The strategies after it still see the
    frame, so the brain's own pays the segment close the frame carries."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aggregator = None  # the LLM user aggregator, set once it exists

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        await super().process_frame(frame)
        if is_taken(frame):
            if self.aggregator is not None:
                await self.aggregator.reset()  # its text so far: empty, no inference
            await self.trigger_user_turn_stopped()
        return ProcessFrameResult.CONTINUE


class WifiSetupVoice(FrameProcessor):
    """Between the user's transcript and the LLM. Idle: passes everything through,
    watching final transcripts for the setup phrase. Confirming or running: takes the
    user's final transcripts itself and speaks fixed sentences."""

    def __init__(self, run=None, status_path: str = STATUS_PATH, clock=time.monotonic,
                 lang_fn=lambda: "en-us", alive=None, **kw):
        super().__init__(**kw)
        self._lang_fn = lang_fn  # the session's TTS language now ('en-us', 'es', ...)
        self._run = run or _run_helper
        self._alive = alive or _unit_active
        self._status_path = status_path
        self._clock = clock
        self.state = "idle"  # idle | confirm | running
        self._unclear = 0
        self._asked = 0.0  # when the confirm question was put
        # The words setup took while asking (the phrase, unclear answers): given back to
        # the model if the answer never becomes a yes or a no.
        self._held: list[str] = []
        self._returning: list[str] = []
        self._last: dict = {}
        self._before: tuple | None = None  # (run_id, time) of the file before this run
        self._why = ""  # the last failed attempt's reason (an English msgid)
        self._poller: asyncio.Task | None = None
        self._bot_speaking = False
        self._quiet_since = -math.inf
        self._said: deque = deque(maxlen=8)  # (when, normalized line)

    @property
    def t(self) -> i18n.T:
        return i18n.for_espeak(self._lang_fn())

    # -- speaking
    async def say(self, text: str) -> None:
        self._said.append((self._clock(), _norm(text)))
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    def _echo(self, text: str) -> bool:
        """Is `text` the box's own voice, heard back: while it speaks or just after, a
        run of at least three words (four characters, unspaced) from a line it said?
        One word is not enough -- "cancel" is a line's word and the user's answer."""
        now = self._clock()
        if not (self._bot_speaking or now - self._quiet_since <= ECHO_TAIL_SECS):
            return False
        piece = _norm(text)
        if not piece:
            return False
        if len(piece.split()) >= 3:
            hit = lambda line: f" {piece} " in f" {line} "  # noqa: E731
        elif _UNSPACED.search(piece) and len(piece.replace(" ", "")) >= 4:
            hit = lambda line: piece.replace(" ", "") in line.replace(" ", "")  # noqa: E731
        else:
            return False
        return any(now - when <= ECHO_LINES_SECS and hit(line) for when, line in self._said)

    # -- the ways in
    async def begin(self) -> None:
        """Ask to confirm (the phrase, or the wifi_setup tool)."""
        if self.state == "running":
            t = self.t
            await self.say(t._("Wi-Fi setup is already running.") + (
                " " + instructions(self._last, t) if self._last.get("phase") == "ap_up" else ""))
            return
        self.state, self._unclear, self._held = "confirm", 0, []
        self._asked = self._clock()
        await self.say(self.t._("Do you want to set up Wi-Fi? I'll go offline for a few "
                                "minutes while you do it on your phone. Say yes or no."))

    async def _shielded(self, coro) -> None:
        """Run a unit operation to its end even if the frame that asked for it is
        interrupted: a barge-in cancels the frame being processed, and half a start (the
        unit asked to start, nobody following it) or half a cancel is worse than either."""
        await asyncio.shield(self._spawn(coro))

    async def _start(self) -> None:
        self.state, self._last, self._why = "running", {}, ""
        # What the status file says before this run: any other content is this run's.
        # By content, not by clock: the clock jumps when the box comes online (NTP).
        old = self._read_file()
        self._before = (old.get("run_id"), old.get("time")) if old else None
        await self.say(self.t._("Okay. Give me a moment to look for networks."))
        rc, err = await self._run("restart", UNIT)
        if rc != 0:
            logger.warning(f"wifi setup: could not start {UNIT}: {err}")
            self.state = "idle"
            await self.say(self.t._("Sorry, I couldn't start Wi-Fi setup on this box."))
            return
        logger.info("wifi setup: started")
        self._poller = self._spawn(self._follow())

    async def _cancel(self) -> None:
        rc, err = await self._run("stop", UNIT)
        if self.state != "running":
            return  # it ended on its own meanwhile, and has said so
        if rc != 0:
            # Still running, as far as anyone knows: keep following it, and say only
            # what is true -- setup is on, and cancel is the way out.
            logger.warning(f"wifi setup: could not stop {UNIT}: {err}")
            await self.say(self.t._("I'm in Wi-Fi setup. Say repeat to hear the details "
                                    "again, or cancel to stop."))
            return
        await self._stop_following()
        self.state = "idle"
        await self.say(self.t._("Okay, I've stopped Wi-Fi setup and put things back as they were."))

    def _spawn(self, coro) -> asyncio.Task:
        return self.create_task(coro)  # pipecat's task manager (tests use asyncio's)

    async def _stop_following(self) -> None:
        if self._poller and self._poller is not asyncio.current_task():
            self._poller.cancel()
            try:
                await self._poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poller = None

    # -- following the unit
    def _read_file(self) -> dict | None:
        try:
            with open(self._status_path) as f:
                status = json.load(f)
        except (OSError, ValueError):
            return None
        return status if isinstance(status, dict) and "phase" in status else None

    def _read(self) -> dict | None:
        """This run's status, or None: no file yet, or still the one from before."""
        status = self._read_file()
        if status is None or (status.get("run_id"), status.get("time")) == self._before:
            return None
        return status

    async def _follow(self) -> None:
        started = reminded = checked = self._clock()
        seen = False  # a status of this run has been read: the unit is up
        while self.state == "running":
            await asyncio.sleep(POLL_SECS)
            status = self._read()
            now = self._clock()
            if status is not None:
                seen = True
                if self._news(status):
                    reminded = now
                    await self._announce(status)
                    continue
                if status["phase"] == "ap_up" and now - reminded >= REMIND_SECS:
                    reminded = now
                    t = self.t
                    await self.say(t._("I'm still waiting for you on the setup page.") + " "
                                   + instructions(status, t))
            elif now - started > START_TIMEOUT_SECS:
                # Nothing from it: it did not start -- or runs where we cannot read it.
                # Either way stop it, so the box is not offline with nobody following.
                logger.warning(f"wifi setup: no status after {START_TIMEOUT_SECS:.0f}s")
                await self._stop_unit()
                self.state = "idle"
                await self.say(self.t._("Wi-Fi setup didn't start. Please try again in a moment."))
                return
            if now - started > DEADLINE_SECS:
                logger.warning("wifi setup: still running past the unit's own hard stop")
                await self._stop_unit()
                await self._lost()
                return
            if seen and now - checked >= LIVENESS_SECS:
                checked = now
                if await self._alive() is False:
                    # Its last words may have landed since the read above.
                    status = self._read()
                    if status is not None and self._news(status):
                        await self._announce(status)
                    if self.state == "running":
                        logger.warning(f"wifi setup: {UNIT} is gone, last phase "
                                       f"{self._last.get('phase')!r}")
                        await self._lost()
                    return

    def _news(self, status: dict) -> bool:
        """A phase (or failure) not yet announced; remembered as announced."""
        if (status["phase"], status.get("error")) == (self._last.get("phase"), self._last.get("error")):
            return False
        if status["phase"] == "failed":
            self._why = status.get("reason", "")
        self._last = status
        return True

    async def _stop_unit(self) -> None:
        rc, err = await self._run("stop", UNIT)
        if rc != 0:
            logger.warning(f"wifi setup: could not stop {UNIT}: {err}")

    async def _lost(self) -> None:
        """The unit ended without saying how."""
        self.state = "idle"
        t = self.t
        await self.say(t._("Wi-Fi setup stopped: {reason}.").format(
            reason=t._("something went wrong")))

    async def _announce(self, status: dict) -> None:
        phase, t = status["phase"], self.t
        network = status.get("target") or t._("your network")
        logger.info(f"wifi setup: {phase}")
        if phase == "ap_up":
            # The reasons are English msgids from wifi.py / wifi_setup.py.
            lead = (t._("That didn't work: {reason}.").format(reason=t._(self._why)) + " "
                    if status.get("error") and self._why else "")
            await self.say(lead + instructions(status, t))
        elif phase == "joining":
            await self.say(t._("Got it. I'm joining {network}. Switch your phone back to it.")
                           .format(network=network))
        elif phase == "failed":
            pass  # ap_up follows at once with the reason and the instructions again
        elif phase == "connected":
            self.state = "idle"
            await self.say(t._("I'm connected to {network}, and I'm online again.")
                           .format(network=network))
        elif phase == "timeout":
            self.state = "idle"
            await self.say(t._("Wi-Fi setup timed out, so I've put things back as they were."))
        elif phase == "stopped":
            # The unit was stopped from outside (systemctl, the config page, a restart)
            # and ran its restore path on the way out (wifi_setup.py, SIGTERM).
            self.state = "idle"
            await self.say(t._("Okay, I've stopped Wi-Fi setup and put things back as they were."))
        elif phase == "error":
            self.state = "idle"
            await self.say(t._("Wi-Fi setup stopped: {reason}.").format(
                reason=t._(status.get("reason") or N_("something went wrong"))))

    # -- the frames
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking, self._quiet_since = False, self._clock()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            # The unit outlives the session; only the following stops.
            await self._stop_following()
        elif (isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM
              and not is_taken(frame)):
            if await self._heard(frame.text or ""):
                await self.push_frame(taken(frame), direction)  # ours: no words go on
                return
            for text in self._returning:
                await self.push_frame(handed_back(frame, text), direction)
            self._returning = []
        await self.push_frame(frame, direction)

    async def _heard(self, text: str) -> bool:
        """Handle a final transcript; True if setup took it. Words to give the model
        ahead of it, if any, are left in _returning."""
        t = self.t
        self._returning = []
        if self._echo(text):
            logger.debug(f"wifi setup: {text[:40]!r} is the box's own voice — ignored")
            return True
        if self.state == "confirm" and self._clock() - self._asked > CONFIRM_SECS:
            logger.info("wifi setup: the question went unanswered — back to conversation")
            self.state, self._held = "idle", []
        if self.state == "idle":
            if heard("start", text, t):
                await self.begin()
                self._held = [text]
                return True
            return False
        if self.state == "confirm":
            if heard("no", text, t):
                # A no to the question asked: nothing goes back to the model, which
                # would only hand over again ("set up Wi-Fi" is a request for the tool).
                self.state, self._held = "idle", []
                await self.say(t._("Okay, never mind."))
            elif is_yes(text, t):
                self._held = []
                await self._shielded(self._start())
            else:
                self._unclear += 1
                if self._unclear >= 2:
                    # Not about setup after all: the model takes it, with what setup
                    # held back -- the words that started this and the answers since.
                    self.state, self._returning, self._held = "idle", self._held, []
                    return False
                self._held.append(text)
                await self.say(t._("Say yes to start Wi-Fi setup, or no."))
            return True
        # running
        if heard("cancel", text, t):
            await self._shielded(self._cancel())
        elif heard("repeat", text, t) and self._last.get("phase") == "ap_up":
            await self.say(instructions(self._last, t))
        else:
            await self.say(t._("I'm in Wi-Fi setup. Say repeat to hear the details again, "
                               "or cancel to stop."))
        return True


async def _run_helper(action: str, unit: str) -> tuple[int, str]:
    """config_apply <action> <unit> under sudo — the sudoers line the installer writes."""
    try:
        p = await asyncio.create_subprocess_exec(
            "sudo", "-n", SYSTEM_PYTHON, APPLY_HELPER, action, unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), 20)
        return p.returncode, (err or out).decode(errors="replace").strip()
    except (OSError, asyncio.TimeoutError) as e:
        return 1, repr(e)


async def _unit_active() -> bool | None:
    """Is the setup unit still up? `systemctl is-active` (no root needed): False for
    inactive or failed, None when systemctl cannot say -- not taken as dead."""
    try:
        p = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", UNIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
    except (OSError, asyncio.TimeoutError):
        return None
    state = out.decode(errors="replace").strip()
    if state in ("inactive", "failed"):
        return False
    return True if state else None
