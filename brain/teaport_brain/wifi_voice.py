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

TRIGGER = re.compile(
    r"\b(?:set ?up|setup|configure|connect to|join|change|switch)\b(?: (?:the|a|my|your|to|new|another|different)){0,3} ?wi-?fi\b"
    r"|\bwi-?fi (?:set ?up|setup)\b", re.I)
YES = re.compile(r"\b(?:yes|yeah|yep|sure|okay|ok|go ahead|do it|please|start)\b", re.I)
NO = re.compile(r"\b(?:no|nope|don't|do not|never ?mind|cancel|stop)\b", re.I)
CANCEL = re.compile(r"\b(?:cancel|stop|quit|exit|abort|never ?mind)\b", re.I)
REPEAT = re.compile(r"\b(?:repeat|again|what was|what's the|password|network name|say that)\b", re.I)

_DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def available() -> bool:
    """The setup unit and the helper that starts it are installed."""
    return os.path.exists(UNIT_FILE) and os.path.exists(APPLY_HELPER)


_SYMBOLS = {"-": "dash", "_": "underscore", ".": "dot", " ": "space", "@": "at sign",
            "!": "exclamation mark", "#": "hash", "&": "ampersand", "*": "star"}


def spell(text: str) -> str:
    """Character by character, for the voice: 'teaport-9e35' -> 'teaport, dash, nine, E,
    three, five'. A lowercase word before the first dash is said as a word; in a string
    that mixes cases, uppercase letters are said as "capital X"."""
    head, sep, rest = text.partition("-")
    if sep and head.isalpha() and head.islower():
        words, chars = [head, "dash"], rest
    else:
        words, chars = [], text
    mixed = any(c.isupper() for c in chars) and any(c.islower() for c in chars)
    for c in chars:
        if c.isdigit():
            words.append(_DIGITS[int(c)])
        elif c.isalpha():
            words.append(f"capital {c.upper()}" if mixed and c.isupper() else c.upper())
        else:
            words.append(_SYMBOLS.get(c, c))
    return ", ".join(words)


def instructions(status: dict) -> str:
    ssid, password = status.get("ssid", ""), status.get("password", "")
    name = ssid.split("-", 1)[1] if ssid.startswith("teaport-") else ""
    url = f"teaport dash {spell(name).replace(', ', ' ')} dot local" if name else "ten dot forty-two dot zero dot one"
    return (f"On your phone, join the Wi-Fi network {spell(ssid)}. "
            f"The password is {spell(password)}. "
            f"A setup page should open by itself. If it doesn't, go to {url}.")


class WifiSetupVoice(FrameProcessor):
    """Between the user's transcript and the LLM. Idle: passes everything through,
    watching final transcripts for the setup phrase. Confirming or running: takes the
    user's final transcripts itself and speaks fixed sentences."""

    def __init__(self, run=None, status_path: str = STATUS_PATH, clock=time.time, **kw):
        super().__init__(**kw)
        self._run = run or _run_helper
        self._status_path = status_path
        self._clock = clock
        self.state = "idle"  # idle | confirm | running
        self._unclear = 0
        self._started_at = 0.0
        self._last: dict = {}
        self._poller: asyncio.Task | None = None

    # -- speaking
    async def say(self, text: str) -> None:
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    # -- the ways in
    async def begin(self) -> None:
        """Ask to confirm (the phrase, or the wifi_setup tool)."""
        if self.state == "running":
            await self.say("Wi-Fi setup is already running. " + (instructions(self._last)
                           if self._last.get("phase") == "ap_up" else ""))
            return
        self.state, self._unclear = "confirm", 0
        await self.say("Do you want to set up Wi-Fi? I'll go offline for a few minutes "
                       "while you do it on your phone. Say yes or no.")

    async def _start(self) -> None:
        self.state, self._started_at, self._last = "running", self._clock(), {}
        await self.say("Okay. Give me a moment to look for networks.")
        rc, err = await self._run("restart", UNIT)
        if rc != 0:
            logger.warning(f"wifi setup: could not start {UNIT}: {err}")
            self.state = "idle"
            await self.say("Sorry, I couldn't start Wi-Fi setup on this box.")
            return
        logger.info("wifi setup: started")
        self._poller = self._spawn(self._follow())

    async def _cancel(self) -> None:
        rc, err = await self._run("stop", UNIT)
        if rc != 0:
            logger.warning(f"wifi setup: could not stop {UNIT}: {err}")
        await self._stop_following()
        self.state = "idle"
        await self.say("Okay, I've stopped Wi-Fi setup and put things back as they were.")

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
                    await self.say("Wi-Fi setup didn't start. Please try again in a moment.")
                continue
            if (status["phase"], status.get("error")) == (self._last.get("phase"), self._last.get("error")):
                if status["phase"] == "ap_up" and now - reminded >= REMIND_SECS:
                    reminded = now
                    await self.say("I'm still waiting for you on the setup page. " + instructions(status))
                continue
            self._last, reminded = status, now
            await self._announce(status)

    async def _announce(self, status: dict) -> None:
        phase = status["phase"]
        logger.info(f"wifi setup: {phase}")
        if phase == "ap_up":
            lead = (f"That didn't work. " if status.get("error") else "")
            await self.say(lead + instructions(status))
        elif phase == "joining":
            await self.say(f"Got it. I'm joining {status.get('target', 'your network')}. "
                           "Switch your phone back to it.")
        elif phase == "failed":
            pass  # ap_up follows at once with the error and the instructions again
        elif phase == "connected":
            self.state = "idle"
            await self.say(f"I'm connected to {status.get('target', 'your network')}, "
                           "and I'm online again.")
        elif phase == "timeout":
            self.state = "idle"
            await self.say("Wi-Fi setup timed out, so I've put things back as they were.")
        elif phase == "error":
            self.state = "idle"
            await self.say(f"Wi-Fi setup stopped: {status.get('reason', 'something went wrong')}.")

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
        if self.state == "idle":
            if TRIGGER.search(text):
                await self.begin()
                return True
            return False
        if self.state == "confirm":
            if NO.search(text):
                self.state = "idle"
                await self.say("Okay, never mind.")
            elif YES.search(text):
                await self._start()
            else:
                self._unclear += 1
                if self._unclear >= 2:
                    self.state = "idle"
                    return False  # not about setup after all: the LLM takes it
                await self.say("Say yes to start Wi-Fi setup, or no.")
            return True
        # running
        if CANCEL.search(text):
            await self._cancel()
        elif REPEAT.search(text) and self._last.get("phase") == "ap_up":
            await self.say(instructions(self._last))
        else:
            await self.say("I'm in Wi-Fi setup. Say repeat to hear the details again, "
                           "or cancel to stop.")
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
