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
import asyncio
import json
import os
import re
import time

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

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


_TASHKEEL = re.compile("[\u064b-\u065f\u0670\u0640]")


def heard(meaning: str, text: str, t: i18n.T) -> bool:
    """Did the user say `meaning` ("start", "yes", ...) in English or in t's language?
    Except "no": in another language English's "no" is often an ordinary word (Portuguese
    "no celular", Italian "come no" = of course), and a false "no" throws the setup away,
    so there only the language's own "no" counts (every catalog has its word for it)."""
    # Arabic short vowels, shadda and tatweel are optional in writing and an STT may or
    # may not emit them: match without them (no other catalog's script uses these).
    text = _TASHKEEL.sub("", text)
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


def available() -> bool:
    """The setup unit and the helper that starts it are installed."""
    return os.path.exists(UNIT_FILE) and os.path.exists(APPLY_HELPER)


# Characters that are neither digits nor letters, by the name a voice says (catalog
# context "spell").
_SYMBOLS = {"-": "dash", "_": "underscore", ".": "dot", " ": "space", "@": "at sign",
            "!": "exclamation mark", "#": "hash", "&": "ampersand", "*": "star"}


def spell(text: str, t: i18n.T) -> str:
    """Character by character, in t's language: 'teaport-9e35' -> 'teaport, dash, nine,
    E, three, five'. A lowercase word before the first dash is said as a word; in a
    string that mixes cases, uppercase letters are said as "capital X"."""
    head, sep, rest = text.partition("-")
    if sep and head.isalpha() and head.islower():
        words, chars = [head, t.p("spell", "dash")], rest
    else:
        words, chars = [], text
    mixed = any(c.isupper() for c in chars) and any(c.islower() for c in chars)
    for c in chars:
        if c.isdigit() and c.isascii():
            words.append(t.p("digit", c))
        elif c.isalpha() and c.isascii():
            # A letter by its name. Optional catalog context "letter" (A-Z): a language
            # whose letter names collide with its digit words names them its own way
            # (Korean says E like 2); without an entry the voice gets the letter itself.
            name = t.p("letter", c.upper())
            words.append(t.p("spell", "capital {letter}").format(letter=name)
                         if mixed and c.isupper() else name)
        elif c.isalpha():
            words.append(c)
        elif c in _SYMBOLS:
            words.append(t.p("spell", _SYMBOLS[c]))
        else:
            words.append(c)
    return t.p("spell", ", ").join(words)


def instructions(status: dict, t: i18n.T) -> str:
    ssid, password = status.get("ssid", ""), status.get("password", "")
    name = ssid.split("-", 1)[1] if ssid.startswith("teaport-") else ""
    if name:
        parts = spell(name, t).split(t.p("spell", ", "))
        url = " ".join(["teaport", t.p("spell", "dash"), *parts, t.p("spell", "dot"), "local"])
    else:
        # The setup network's address, digit by digit: "one zero dot four two dot ...".
        url = f" {t.p('spell', 'dot')} ".join(
            " ".join(t.p("digit", d) for d in part) for part in "10.42.0.1".split("."))
    return t._("On your phone, join the Wi-Fi network {ssid}. The password is {password}. "
               "A setup page should open by itself. If it doesn't, go to {url}.").format(
        ssid=spell(ssid, t), password=spell(password, t), url=url)


class WifiSetupVoice(FrameProcessor):
    """Between the user's transcript and the LLM. Idle: passes everything through,
    watching final transcripts for the setup phrase. Confirming or running: takes the
    user's final transcripts itself and speaks fixed sentences."""

    def __init__(self, run=None, status_path: str = STATUS_PATH, clock=time.time,
                 lang_fn=lambda: "en-us", **kw):
        super().__init__(**kw)
        self._lang_fn = lang_fn  # the session's TTS language now ('en-us', 'es', ...)
        self._run = run or _run_helper
        self._status_path = status_path
        self._clock = clock
        self.state = "idle"  # idle | confirm | running
        self._unclear = 0
        self._started_at = 0.0
        self._last: dict = {}
        self._why = ""  # the last failed attempt's reason (an English msgid)
        self._poller: asyncio.Task | None = None

    @property
    def t(self) -> i18n.T:
        return i18n.for_espeak(self._lang_fn())

    # -- speaking
    async def say(self, text: str) -> None:
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    # -- the ways in
    async def begin(self) -> None:
        """Ask to confirm (the phrase, or the wifi_setup tool)."""
        if self.state == "running":
            t = self.t
            await self.say(t._("Wi-Fi setup is already running.") + (
                " " + instructions(self._last, t) if self._last.get("phase") == "ap_up" else ""))
            return
        self.state, self._unclear = "confirm", 0
        await self.say(self.t._("Do you want to set up Wi-Fi? I'll go offline for a few "
                                "minutes while you do it on your phone. Say yes or no."))

    async def _start(self) -> None:
        self.state, self._started_at, self._last = "running", self._clock(), {}
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
        if rc != 0:
            logger.warning(f"wifi setup: could not stop {UNIT}: {err}")
        await self._stop_following()
        self.state = "idle"
        await self.say(self.t._("Okay, I've stopped Wi-Fi setup and put things back as they were."))

    def _spawn(self, coro) -> asyncio.Task:
        return self.create_task(coro)  # pipecat's task manager (tests use asyncio's)

    async def _stop_following(self) -> None:
        if self._poller:
            self._poller.cancel()
            try:
                await self._poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._poller = None

    # -- following the unit
    def _read(self) -> dict | None:
        try:
            with open(self._status_path) as f:
                status = json.load(f)
        except (OSError, ValueError):
            return None
        # A file from an earlier run is not this run's news.
        return status if status.get("time", 0) >= self._started_at else None

    async def _follow(self) -> None:
        reminded = self._clock()
        while self.state == "running":
            await asyncio.sleep(POLL_SECS)
            status = self._read()
            now = self._clock()
            if status is None:
                if now - self._started_at > START_TIMEOUT_SECS:
                    self.state = "idle"
                    await self.say(self.t._("Wi-Fi setup didn't start. Please try again in a moment."))
                continue
            if (status["phase"], status.get("error")) == (self._last.get("phase"), self._last.get("error")):
                if status["phase"] == "ap_up" and now - reminded >= REMIND_SECS:
                    reminded = now
                    t = self.t
                    await self.say(t._("I'm still waiting for you on the setup page.") + " "
                                   + instructions(status, t))
                continue
            if status["phase"] == "failed":
                self._why = status.get("reason", "")
            self._last, reminded = status, now
            await self._announce(status)

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
        elif phase == "error":
            self.state = "idle"
            await self.say(t._("Wi-Fi setup stopped: {reason}.").format(
                reason=t._(status.get("reason") or N_("something went wrong"))))

    # -- the frames
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            # The unit outlives the session; only the following stops.
            await self._stop_following()
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            if await self._heard(frame.text or ""):
                return  # ours: the LLM never sees it
        await self.push_frame(frame, direction)

    async def _heard(self, text: str) -> bool:
        t = self.t
        if self.state == "idle":
            if heard("start", text, t):
                await self.begin()
                return True
            return False
        if self.state == "confirm":
            if heard("no", text, t):
                self.state = "idle"
                await self.say(t._("Okay, never mind."))
            elif heard("yes", text, t):
                await self._start()
            else:
                self._unclear += 1
                if self._unclear >= 2:
                    self.state = "idle"
                    return False  # not about setup after all: the LLM takes it
                await self.say(t._("Say yes to start Wi-Fi setup, or no."))
            return True
        # running
        if heard("cancel", text, t):
            await self._cancel()
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
