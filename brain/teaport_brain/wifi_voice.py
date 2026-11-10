#
# teaport — Wi-Fi setup, the voice half (the setup network and page are wifi_setup.py).
#
# A box with no internet has no LLM either, so this half needs none: it listens for a
# fixed phrase ("set up Wi-Fi", "connect to Wi-Fi", ...) in the user's final transcript,
# asks them to confirm (the box goes offline while it runs), starts
# teaport-wifi-setup.service, and from then on speaks fixed sentences: where to find the
# setup network's name, password and address (the box's screen, or without one, spelled
# out), and what became of the attempt, read from the unit's status.json. While it runs it keeps the user's words
# from the LLM and answers "repeat" and "cancel" itself.
#
# The phrase only works while the box is offline (NetworkManager's connectivity check
# does not say "full": _box_online): online, the model is there, and "set up Wi-Fi"
# said in conversation goes to it like any other words -- it decides, and its way in is
# the wifi_setup tool (tools.py), which hands over to the same flow. A session with no
# model at all (#277: no key yet) has nobody to leave it to, so there the phrase works
# online too. Once setup is
# asking or running, its answers
# ("yes", "repeat", "cancel") are taken here however it began. Both exist only where
# that tool is active — TEAPORT_TOOL_WIFI_SETUP on, the
# client announced "local" (the local audio bridge: someone is at the box, not a remote
# Talk user or a phone caller who could cut the box off its own network), and the setup
# unit is installed.
#
# It speaks the session's language — its voice's, read live, so a switch_voice mid-session
# switches setup too — through the catalogs in i18n.py (the "start", "yes", ... phrases
# included); the phone page (wifi_setup.py) follows the phone's own language.
#
# A transcript it takes is not dropped: it goes on with its text emptied and marked
# (taken_turn.taken), and the turn it opened closes with nothing to answer
# (taken_turn.TakenTurnCloser, which says why that matters).
#
# The model hears none of this (say() keeps its lines out of the context), so when a
# setup ends, one English line saying how goes into its context through the session's
# notes (client_notes.py, add_notice): at the next turn boundary, never spoken, never a
# turn of its own (issue #97; _ended has who gets it). It names the network joined and
# never a password.
#
import asyncio
import contextvars
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
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from teaport_brain import display, i18n
from teaport_brain.tasks import cancel_and_wait
from teaport_brain.conversation_log import quoted
from teaport_brain.i18n import N_
from teaport_brain.wifi import AP_ADDRESS
from teaport_brain.wifi_setup import NO_WIFI_DEVICE, PAGE_FAILED
from teaport_brain.wifi_setup import hand_language as hand_screen_language
from teaport_brain.taken_turn import handed_back, is_taken, taken  # noqa: F401 (tests)

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
# How long the online check (_box_online) may take before the box counts as offline --
# the phrase waits on it: a D-Bus read of NM's settings, then a fresh connectivity check.
NM_PROPERTY_SECS = 1.0
ONLINE_CHECK_SECS = 3.0
# A yes is an answer, not a sentence that happens to hold "okay" or "please": at most
# this many words (CJK: about three characters to a word) and no question mark.
YES_MAX_WORDS = 4
# What the box says can come back through the microphone (the card's echo cancellation is
# good, not perfect: local_audio.py), and its lines are full of the words it listens for
# ("Say yes or no", "or cancel to stop"). A transcript that is a piece of a line it said,
# heard while it speaks or this soon after, is that echo.
ECHO_TAIL_SECS = 1.5
ECHO_LINES_SECS = 120.0
# A line said and then waited on (PhraseVoice.heard_out): how long its speech may take to
# start (the engine synthesises it first), and the longest it may play.
LINE_START_SECS = 5.0
LINE_MAX_SECS = 20.0
LINE_POLL_SECS = 0.05
# The setup network's name is teaport-<hex>: the word is said as a word.
SSID_WORD = "teaport"
# The first-boot setup (first_boot.py, begin_first_boot): the welcome, in place of the
# "Okay." that answers a yes; then, the usual setup-mode instructions once it is up.
WELCOME_TEXT = N_("Hi, I'm Teaport. Let's get me online. Give me a moment to look for "
                  "networks.")
# After a first-boot setup that left the box offline: it does not start again by itself
# until the next boot. The quoted words are the start phrase (tests/test_i18n.py).
AGAIN_TEXT = N_("You can say \"set up Wi-Fi\" any time to try again.")
# After "connected", how the AI account key typed on the setup page went (wifi_setup.py,
# #279): saved, or not (refused, or it could not be checked). The quoted words are the
# settings phrase (tests/test_i18n.py).
ACCOUNT_SAVED_TEXT = N_("Your AI account is connected.")
# The same, for an OpenRouter account with nothing to spend (ai_account.NO_ACCOUNT_CREDIT).
ACCOUNT_NO_CREDIT_TEXT = N_("Your AI account is connected, but it has no credit yet. Add some "
                            "on OpenRouter's website, and then I can answer.")
ACCOUNT_NOT_SAVED_TEXT = N_("I couldn't use the AI account key you entered, so I didn't "
                            "save it. Say \"open my settings\" to try again.")
# The same where "open my settings" is not usable (settings_access.usable).
ACCOUNT_NOT_SAVED_PLAIN_TEXT = N_("I couldn't use the AI account key you entered, so I didn't "
                                  "save it.")
# The first-boot setup, left because the box came online by another way (_leave_online).
ONLINE_NOW_TEXT = N_("I'm online now, so I've left setup mode.")
# While setup runs, the client hears it is under way (_keep_awake): the call id it is
# told by, and how long that holds unless it is said again.
WORKING_ID = "wifi-setup"
KEEP_AWAKE_SECS = REMIND_SECS + 60.0
# Pieces of the line the model gets when setup ends (_ended). English: it is context.
_PUT_BACK = "the box's Wi-Fi is back as it was before."
_UNCONFIRMED = "the user did not confirm, so it never started."

# A request said on its own (the settings and sleep phrases, request_heard): the
# English patterns' shared pieces. A short lead-in ("ok", "hey", "please"...) -- a phrase
# adds its own words to it --, a polite wrapper ("can you ...", #309), the request, words
# like "please" or "thanks" after it, and nothing else to the end of the utterance. The
# anchoring is what keeps out talk that only mentions the request ("how do I open my
# settings?"): a question mark is no reason to refuse one ("Can you open my settings?").
_LEAD_IN = r"um+|uh+|uhm|er+m?|eh|hm+|ok|okay|hey|so|now|please|teaport|well|alright|right"
_CAN_YOU = r"(?:can|could|would|will) you(?: please)? "
_THANKS = r"thanks a lot|thank you (?:so |very )?much|thanks|thank you|cheers|ok(?:ay)?|teaport"
_TO_THE_END = r"[\s.,!?;:…]*$"


def _lead_in(more: str = "") -> str:
    return r"^\W*(?:(?:" + _LEAD_IN + more + r")\W+){0,3}"


# What the user says, by meaning. The English patterns are the msgids of the "pattern"
# entries in each language's catalog (i18n.py), whose msgstr is that language's own
# pattern: a session listens for its language's words and always for the English ones.
PATTERNS = {
    # "set up / setting up / connect (me) to / switching (to) ... the wifi / a new network",
    # verb first: talk about Wi-Fi ("my wifi is slow", "what network am I on?") has none.
    # The noun must end the request ("... wifi.", "... wifi again", "... wifi on the box"),
    # not describe another noun: "set up wifi calling", "connect the wireless keyboard",
    # "join the network team", "switch the wifi off".
    "start": r"\b(?:set(?:s|ting)?[- ]?up|(?:re)?configur(?:e|es|ing)|(?:re)?connect(?:s|ing)?"
             r"|join(?:s|ing)?|chang(?:e|es|ing)|switch(?:es|ing)?)\b(?: (?:the|a|my|your|our|this"
             r"|that|to|onto|new|another|different|other|home|guest|me|us|it|yourself|the box|back))"
             r"{0,4} ?(?:(?:wi[-‐‑ ]?fi|wireless)(?: (?:network|connection))?|network|hotspot)s?"
             r"(?= *(?:[.,!?;:…]|$)| on \w| (?:and|so|but|because|since|then|now|please|again|for"
             r"|to|at|in|with|from|here|there|too|first|today|tonight|called|named|instead|as|if"
             r"|when|or|that|which|i|we|ok|okay|thanks|thank|right|quickly)\b)"
             r"|\bwi[-‐‑ ]?fi (?:set[- ]?up|setup)\b",
    "yes": r"\b(?:yes|yeah|yep|sure|okay|ok|go ahead|do it|please|start)\b",
    "no": r"\b(?:no|nope|don't|do not|never ?mind|cancel|stop)\b",
    "cancel": r"\b(?:cancel|stop|quit|exit|abort|never ?mind)\b",
    "repeat": r"\b(?:repeat|again|what was|what's the|password|network name|say that)\b",
    # "open my settings" (settings_access.SettingsPhrase, #279): a request to see the
    # box's settings, said as a request on its own (_lead_in, _CAN_YOU, _TO_THE_END), so
    # "on my phone, open the settings" is not it, and the phrase ends the sentence (or
    # "page", "please", "now" follow it). Every catalog's pattern is anchored the same
    # way, with its language's own wrappers.
    "settings": _lead_in() + r"(?:" + _CAN_YOU + r")?"
                r"(?:open|show|bring up|pull up|go to|get)(?: up)?(?: me)?(?: to)? "
                r"(?:(?:my|your|the|teaport's|teaport) )?settings(?:[\s,]+(?:page|please|now|for me|"
                + _THANKS + r")\b)*" + _TO_THE_END,
    # "go to sleep" (goodbye.SleepPhrase): the room goes back to sleep, said as a request
    # on its own, the same way -- "okay, go to sleep", "you can go back to sleep now",
    # "can you go to sleep?", "stop listening" -- and "no" or a goodbye may lead in ("No,
    # go to sleep.", "Good night, go to sleep."). Talk about sleep is not it: "how do I
    # make you go to sleep?", "I need to go to sleep", "don't go to sleep", "go to sleep
    # mode", "stop listening to me". (A turn that is only a goodbye -- "Good night." --
    # is the phrase too: goodbye.SleepPhrase hears it with the backstop's matcher.)
    "sleep": _lead_in(r"|no|yes|yeah|just|thanks|thank you|good ?night|bye")
             + r"(?:" + _CAN_YOU + r"|you can )?(?:please )?(?:just )?"
             r"(?:go (?:back )?to sleep|stop listening)"
             r"(?:[\s,]+(?:now|please|then|for now|again|" + _THANKS + r")\b)*" + _TO_THE_END,
}

# English words that make a "start" match a question or a complaint about Wi-Fi rather
# than a request to set up the box's: "how do I change my wifi password?", "my laptop
# can't connect to wifi", "set up wifi on my phone". Those go to the model, which can
# still hand over (the wifi_setup tool) if setup is what they want. English only, as the
# English start pattern is heard in every session; a language's own pattern is its
# catalog's to keep narrow.
_NOT_A_REQUEST = re.compile(
    r"\b(?:how|why|did|does|can['’]?t|cannot|couldn['’]?t|won['’]?t|doesn['’]?t|didn['’]?t"
    r"|isn['’]?t|wasn['’]?t|unable|trouble|problem)\b"
    r"|\b(?:wi[-‐‑ ]?fi|wireless|network|hotspot) (?:password|passcode|settings?|name|network name"
    r"|is|was|keeps)\b"
    r"|\bon (?:my|his|her|their|our|the|this) (?:phone|laptop|computer|tablet|tv|mac|pc"
    r"|ipad|iphone|kindle)\b", re.I)

_TASHKEEL = re.compile("[ً-ٰٟـ]")
_QUESTION = re.compile("[?？؟]")
# Scripts written without spaces between words (Han, kana): sized by characters.
_UNSPACED = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def _first_boot():
    """first_boot.py, imported where it is used: it imports this module."""
    from teaport_brain import first_boot
    return first_boot


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


# A request said on its own is short: the pattern already holds the utterance to a
# lead-in, a polite wrapper, the request and a few trailing words; this is a backstop,
# with room for the longest of those ("Teaport, est-ce que tu peux ouvrir les réglages
# s'il te plaît ?": 13 words by words(), which splits on hyphens and apostrophes).
REQUEST_MAX_WORDS = 16


def request_heard(meaning: str, text: str, t: i18n.T) -> bool:
    """Is `text` the fixed request `meaning` -- "settings" (settings_access.SettingsPhrase),
    "sleep" (goodbye.SleepPhrase) -- in English or t's language, said as a request of its
    own? Its pattern (PATTERNS, and each catalog's) holds the request to the whole
    utterance: a short lead-in, a polite wrapper ("can you ...", #309), the request, and
    nothing after it but punctuation or a word like "please". So a question mark is no
    reason to refuse it ("Can you open my settings?" is a request), and a sentence that
    only mentions it ("how do I open my settings?", "open my settings on the phone") still
    is not it. At most REQUEST_MAX_WORDS words."""
    return heard(meaning, text, t) and words(text) <= REQUEST_MAX_WORDS


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


def spoken_address(host: str, t: i18n.T) -> str:
    """A host name or address as a voice reads it out, label by label with "dot" between:
    a label of letters alone as a word ("local"), digits one by one ("one zero"), anything
    else spelled (spell(): "teaport dash nine E three five"). The setup page's address
    (instructions) and the Settings address (settings_access.py) are read this way."""
    said = []
    for label in host.split("."):
        if label.isascii() and label.isalpha():
            said.append(label)
        elif label.isascii() and label.isdigit():
            said.append(" ".join(t.p("digit", d) for d in label))
        else:
            said.append(" ".join(spell(label, t).split(t.p("spell", ", "))))
    return f" {t.p('spell', 'dot')} ".join(said)


def instructions(status: dict, t: i18n.T, spelled: bool = False) -> str:
    """What the user does next, in the order they do it. With the box's display up
    (wifi_setup.py shows "Network ...", "Password ..." and an address, and a code that
    joins when the avatar draws one), the voice points at the screen and offers the
    spelled details as a fallback; without it, or `spelled` (a "repeat"), it spells the
    network, the password and the address. Nothing here counts on the phone opening the
    page by itself (a captive-portal check that not every phone makes)."""
    ssid, password = status.get("ssid", ""), status.get("password", "")
    if not spelled and status.get("screen"):
        if status.get("qr"):
            # The code joins by the real name, whatever the font can draw.
            return t._("Scan the code on my screen with your phone's camera to join my "
                       "setup network. Then, in your phone's browser, go to the address "
                       "on my screen and pick your Wi-Fi. Say repeat if you'd like me to "
                       "read the details.")
        # Without a code the screen's network line is the name only where the screen
        # draws it as it is (display's rule, with what the unit asked of the avatar:
        # "unicode"); a folded one would read wrong, so it is spelled instead.
        if display.screen_text(ssid, status.get("unicode")) == ssid:
            return t._("On your phone, join the Wi-Fi network shown on my screen, using "
                       "the password shown there. Then, in your phone's browser, go to "
                       "the address on my screen and pick your Wi-Fi. Say repeat if you'd "
                       "like me to read the details.")
    # Exactly the address the screen shows and the status carries (wifi_setup.page_url):
    # the .local name, or the bare address digit by digit ("one zero dot four two ...").
    host = (status.get("url") or "").removeprefix("http://")
    name = ssid.split("-", 1)[1] if ssid.startswith(SSID_WORD + "-") else ""
    if not (name and host == f"{ssid}.local") and not re.fullmatch(r"[0-9.]+", host):
        host = AP_ADDRESS
    url = spoken_address(host, t)
    return t._("On your phone, join the Wi-Fi network {ssid}. The password is {password}. "
               "Then open your browser, go to {url}, and pick your Wi-Fi.").format(
        ssid=spell(ssid, t), password=spell(password, t), url=url)


class _Answer:
    """The answer to one transcript a phrase is hearing (PhraseVoice._heard): open until
    its first line is said, or the hearing is over."""

    __slots__ = ("voice", "open")

    def __init__(self, voice):
        self.voice, self.open = voice, True


# The answer being given in this task, and in the tasks it starts (asyncio copies the
# context into each): a line said elsewhere -- the setup poller's news, a line said again
# after a cut, started before or outside the hearing -- answers nothing (PhraseVoice.say).
_ANSWERING: contextvars.ContextVar = contextvars.ContextVar("teaport_phrase_answer",
                                                            default=None)


class PhraseVoice(FrameProcessor):
    """The session's fixed phrases, between the user's transcript and the LLM: what the
    box hears and answers itself, without the model, in the session's language (catalog
    "pattern" entries, heard()). It says its lines itself (say(): out of the context,
    never through the LLM) and takes the box's own voice heard back for nothing (_echo).
    A transcript a phrase takes goes on emptied and marked (taken_turn).

    `phrases`: the phrases heard while nothing else holds the user's words, each an
    object with `async heard(voice, text) -> bool` (True: it took them) -- "open my
    settings" (settings_access.SettingsPhrase, #279), the room's "go to sleep"
    (goodbye.SleepPhrase). A subclass that holds the words for
    a while (WifiSetupVoice, while it asks or runs) says so with idle(), and answers the
    rest in _own()."""

    TAKEN_BY = "taken by a fixed phrase"

    def __init__(self, lang_fn=lambda: "en-us", clock=time.monotonic, phrases=(), **kw):
        super().__init__(**kw)
        # taken_turn.TakenWords, where the session keeps one (agent_session).
        self.taken_words = None
        self._lang_fn = lang_fn  # the session's TTS language now ('en-us', 'es', ...)
        self._clock = clock
        self.phrases = list(phrases)
        self._returning: list[str] = []
        self._bot_speaking = False
        self._quiet_since = -math.inf
        self._said: deque = deque(maxlen=8)  # (when, normalized line)
        self.says = 0  # lines say() has started: whether a step got as far as speaking
        self.bot_starts = 0  # the box's speech started so far (heard_out)
        self.cuts = 0        # ... and cut by the user while it played (heard_out)
        self._start_cuts = 0  # cuts when the box's speech last started
        # The room's echo guard's record of what the speaker played (echo_guard.BotWords),
        # where the session has one (agent_session): the model's words, for _echo.
        self.bot_words = None
        # The user has said something in this session (a final heard here, the box's
        # own voice aside): a phrase may read whether these are their first words.
        self.caller_spoke = False
        self._first = None    # their first words, normalized (first_words)
        # AgentSession.say_in_place, where the session wires it: an answer to the user's
        # words said while the bot is replying stops that reply first (say()).
        self.say_in_place = None

    @property
    def t(self) -> i18n.T:
        return i18n.for_espeak(self._lang_fn())

    # -- speaking
    async def say(self, text: str) -> None:
        """Say `text`. The first line of an answer to the user's words (_heard), said
        while the bot is replying -- speaking, paused over those very words, or about to
        -- goes in place of that reply, which stops as for a barge-in (say_in_place):
        the user asked for something else. Queued behind it instead, the barge-in pauser
        found no turn in words a phrase took and resumed the reply, and the answer waited
        for the rest of it (#389). One place for every phrase: "go to sleep", "open my
        settings", Wi-Fi setup's. Only a line said by the hearing itself (or a task it
        started, _ANSWERING): one said meanwhile by anything else answers nothing."""
        self.says += 1
        self.note_said(text)
        answer = _ANSWERING.get()
        answering = answer is not None and answer.open and answer.voice is self
        if answering:
            answer.open = False
        if (answering and self.say_in_place is not None
                and await self.say_in_place(text, LINE_START_SECS + LINE_MAX_SECS)):
            return
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    def note_said(self, text: str) -> None:
        """`text` is being said: heard back, it is the box's own voice (_echo). say()'s,
        and the no-model line's, which holds a phrase (llm_error_speaker.py)."""
        self._said.append((self._clock(), _norm(text)))

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

    def tail_echo(self, text: str) -> bool:
        """Might `text` be the box's own last word heard back: one word, the last of its
        newest line or of anything the speaker played (bot_words: the model's reply),
        heard while it speaks or just after (ECHO_TAIL_SECS)? "…es a las ocho. Chao."
        comes back as "Chao". Too short to call echo and drop (_echo's runs: "No." over
        "Say yes or no." is the user's answer, as the room's echo guard keeps one short
        word), but no ground to act on alone: the sleep phrase does not sleep on it."""
        now = self._clock()
        if not (self._bot_speaking or now - self._quiet_since <= ECHO_TAIL_SECS):
            return False
        word = _norm(text)
        if not word or len(word.split()) != 1:
            return False
        if self._said:
            when, line = self._said[-1]
            if now - when <= ECHO_LINES_SECS and line.split()[-1:] == [word]:
                return True
        last = self.bot_words.last() if self.bot_words is not None else None
        return last is not None and _norm(last[1]) == word

    async def heard_out(self, starts: int) -> bool:
        """Until a line say() started has played to its end (`starts`: bot_starts read
        before it was said). True once it has; False when the user cut it -- an
        interruption once its speech had started (one before, of a reply the user's own
        words cut, is not the line's). A line whose speech never starts (no voice, or
        queued behind speech still playing) is waited on LINE_START_SECS, and one that
        plays on past LINE_MAX_SECS is taken as over."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LINE_START_SECS
        while self.bot_starts <= starts and loop.time() < deadline:
            await asyncio.sleep(LINE_POLL_SECS)
        cuts = self._start_cuts if self.bot_starts > starts else self.cuts
        deadline = loop.time() + LINE_MAX_SECS
        while self._bot_speaking and loop.time() < deadline:
            await asyncio.sleep(LINE_POLL_SECS)
        return self.cuts == cuts

    def _spawn(self, coro) -> asyncio.Task:
        return self.create_task(coro)  # pipecat's task manager (tests use asyncio's)

    @property
    def settings_usable(self) -> bool:
        """Is "open my settings" usable here (settings_access.usable): a line may tell the
        user to say it."""
        from teaport_brain import settings_access
        return settings_access.usable(self.phrases)

    # -- hearing
    def idle(self) -> bool:
        """Nothing holds the user's words: the phrases are listened for."""
        return True

    async def _heard(self, text: str) -> bool:
        """Handle a final transcript; True if a phrase took it. Words to give the model
        ahead of it, if any, are left in _returning."""
        self._returning = []
        if self._echo(text):
            logger.debug(f"phrases: {quoted(text[:40])} is the box's own voice — ignored")
            return True
        try:
            return await self._hear(text)
        finally:
            if text.strip():
                # Their first words, while that turn is the latest: the same words said
                # again later are no hello.
                first = not self.caller_spoke
                self.caller_spoke, self._first = True, _norm(text) if first else None

    def first_words(self, said: str) -> bool:
        """Are `said` (the user's words, as committed) their first in this session, and
        still their latest?"""
        return self._first is not None and _norm(said) == self._first

    async def _hear(self, text: str) -> bool:
        answer = _Answer(self)
        reset = _ANSWERING.set(answer)
        try:
            if self.idle():
                for phrase in self.phrases:
                    if await phrase.heard(self, text):
                        return True
            return await self._own(text)
        finally:
            answer.open = False
            _ANSWERING.reset(reset)

    async def _own(self, text: str) -> bool:
        """A subclass's own phrases and answers: none here."""
        return False

    async def _ending(self) -> None:
        """The session ends (EndFrame, CancelFrame): what a subclass stops."""

    # -- the frames
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self.bot_starts += 1
            self._start_cuts = self.cuts
        elif isinstance(frame, InterruptionFrame):
            self.cuts += self._bot_speaking
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking, self._quiet_since = False, self._clock()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._ending()
        elif (isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM
              and not is_taken(frame)):
            # Not dropped mid-decision by the interruption a phrase's own answer sends
            # (say_in_place, from the pipeline's top): the STT's finals never are
            # (stt.FinalTranscriptionFrame); any other is held to the same while here.
            was, frame.interruptible = frame.interruptible, False
            try:
                took = await self._heard(frame.text or "")
            finally:
                frame.interruptible = was
            if took:
                if self.taken_words is not None:
                    await self.taken_words.took(frame.text, self.TAKEN_BY)
                await self.push_frame(taken(frame), direction)  # ours: no words go on
                return
            for text in self._returning:
                await self.push_frame(handed_back(frame, text), direction)
            self._returning = []
        await self.push_frame(frame, direction)


class WifiSetupVoice(PhraseVoice):
    """Spoken Wi-Fi setup. Idle: passes everything through, watching final transcripts
    for the setup phrase (and the session's other phrases). Confirming or running: takes
    the user's final transcripts itself and speaks fixed sentences."""

    TAKEN_BY = "taken by Wi-Fi setup"

    def __init__(self, run=None, status_path: str = STATUS_PATH, clock=time.monotonic,
                 lang_fn=lambda: "en-us", alive=None, online=None, model: bool = True,
                 on_network=None, hand_language=None, **kw):
        super().__init__(lang_fn=lang_fn, clock=clock, **kw)
        # How the unit learns the voice's language for its screens (wifi_setup).
        self._hand_language = hand_language or hand_screen_language
        # The session has a model (#277). Without one the phrase works online too: the
        # rule that leaves it to the model online has no model to leave it to.
        self.model = model
        self._run = run or _run_helper
        self._alive = alive or _unit_active
        self._online = online or _box_online
        self._on_network = on_network or _box_on_network
        self._status_path = status_path
        self.state = "idle"  # idle | confirm | running
        self._unclear = 0
        self._asked = 0.0  # when the confirm question was put
        self._lapser: asyncio.Task | None = None  # lapses it (_question_lapses)
        # The words setup took while asking (the phrase, unclear answers): given back to
        # the model if the answer never becomes a yes or a no.
        self._held: list[str] = []
        self._last: dict = {}
        self._before: tuple | None = None  # (run_id, time) of the file before this run
        self._why = ""  # the last failed attempt's reason (an English msgid)
        # The user has heard "I'm in setup mode" this run: an error before that is a
        # failure to go into it, not a leaving.
        self._in_mode = False
        self._poller: asyncio.Task | None = None
        # Where the model hears how setup ended: the session's ClientNotes, set once it
        # exists (agent_session.py); None, and the model is told nothing.
        self.notes = None
        self._by_model = False  # this setup began with the wifi_setup tool
        self._cancelling = False  # the user's cancel is stopping the unit (_cancel)
        # This setup is the first-boot one (first_boot.py): it began with the welcome, and
        # an end that leaves the box offline says how to start it again.
        self._first_boot = False
        self._kept = -math.inf  # when the bridge was last told setup is under way

    async def box_on_network(self) -> bool:
        """Is the box on a network a phone can reach it on, internet or not (Settings,
        settings_access.py)? Wrong on the safe side: anything but a clear "no network"
        counts as on one (_on_network)."""
        return await self._on_network()

    async def box_online(self) -> bool:
        """Is the box on the internet (_box_online, or the check it was given)?"""
        return await self._online()

    # -- the ways in
    async def begin(self, by_model: bool = False) -> None:
        """Ask to confirm (the phrase, or the wifi_setup tool: `by_model`)."""
        if self.state == "running":
            self._by_model = self._by_model or by_model  # it asked: it hears the end
            t = self.t
            await self.say(t._("I'm already in setup mode.") + (
                " " + instructions(self._last, t) if self._last.get("phase") == "ap_up" else ""))
            return
        self.state, self._unclear, self._held = "confirm", 0, []
        self._asked, self._by_model = self._clock(), by_model
        # The question lapses by itself: nobody may say anything after it (they walked
        # off), and until then the room counts as busy with setup (gateway_server).
        if self._lapser is not None:
            self._lapser.cancel()
        self._lapser = self._spawn(self._question_lapses(self._asked))
        await self.say(self.t._("Do you want to set up Wi-Fi? I'll go into setup mode and be "
                                "offline for a few minutes while you do it on your phone. "
                                "Say yes or no."))

    def _lapse(self) -> None:
        """The confirm question went unanswered: back to conversation."""
        logger.info("wifi setup: the question went unanswered — back to conversation")
        self.state, self._held = "idle", []
        self._ended(_UNCONFIRMED)

    async def _question_lapses(self, asked: float) -> None:
        """CONFIRM_SECS after the question asked at `asked`: unanswered, it lapses."""
        await asyncio.sleep(CONFIRM_SECS)
        if self.state == "confirm" and self._asked == asked:
            self._lapse()

    async def begin_first_boot(self) -> bool:
        """Go into setup mode unasked, with the welcome instead of the question: a new box
        that is offline at boot (first_boot.py). No confirmation: it is offline already, so
        there is nothing to lose. False when setup is already asking or running."""
        if self.state != "idle":
            return False
        self._first_boot, self._by_model = True, False
        await self._shielded(self._start(welcome=True))
        return True

    async def _say_end(self, line: str, retry: bool = True) -> None:
        """A setup's last line. After the first-boot setup, while the box is still new
        (never online: it is offline), how to start it again -- it does not start again by
        itself before the next boot. Not when trying again cannot help (`retry` False: no
        Wi-Fi device, the unit would not start) or the line says so already."""
        if self._first_boot and retry and _first_boot().is_new():
            line += " " + self.t._(AGAIN_TEXT)
        self._first_boot = False
        await self.say(line)

    async def _leave_online(self) -> None:
        """The first-boot setup, and the box came online meanwhile (Ethernet plugged in, a
        network back: first_boot's watcher saw it): setup mode is left at once -- the
        setup network would only keep the box off a network it no longer needs."""
        logger.info("wifi setup: the box is online now — leaving the first-boot setup")
        await self._stop_unit()
        self.state, self._first_boot = "idle", False
        self._ended(f"the box came online by itself; {_PUT_BACK}", online=True)
        await self.say(self.t._(ONLINE_NOW_TEXT))

    async def _keep_awake(self, on: bool) -> None:
        """Tell the client that setup is under way (or over), the way an answer still owed
        is told ({"type": "working"}): the local audio bridge's conversation does not go to
        sleep on the minutes the user spends on their phone, which would stop this voice
        following the setup. "typing": false -- nothing to type through (thinking_sound.py
        leaves it alone). Refreshed from _follow, well inside KEEP_AWAKE_SECS."""
        message = {"type": "working", "call_id": WORKING_ID, "typing": False}
        message.update({"secs": KEEP_AWAKE_SECS} if on else {"done": True})
        self._kept = self._clock() if on else -math.inf
        try:
            await self.push_frame(OutputTransportMessageUrgentFrame(message=message))
        except Exception as e:  # noqa: BLE001 — a pipeline already ending
            logger.debug(f"wifi setup: could not tell the client ({e!r})")

    async def _shielded(self, coro) -> None:
        """Run a unit operation to its end even if the task that asked for it is
        cancelled -- a session ending under it, a pipeline torn down mid-frame (a final
        is not dropped by an interruption any more: process_frame holds it while the
        phrases decide) -- since half a start (the unit asked to start, nobody following
        it) or half a cancel is worse than either."""
        await asyncio.shield(self._spawn(coro))

    async def _start(self, welcome: bool = False) -> None:
        self.state, self._last, self._why = "running", {}, ""
        self._in_mode = False  # set by the first ap_up announcement
        # What the status file says before this run: any other content is this run's.
        # By content, not by clock: the clock jumps when the box comes online (NTP).
        old = self._read_file()
        self._before = (old.get("run_id"), old.get("time")) if old else None
        # Any setup spends this boot's first-boot try: one the owner started is not
        # followed by one nobody asked for.
        _first_boot().setup_started()
        await self.say(self.t._(WELCOME_TEXT) if welcome
                       else self.t._("Okay. Give me a moment to look for networks."))
        # The box's screen begins in the voice's language (#207): the unit reads it from
        # here, as nothing else reaches it through config_apply.
        try:
            await asyncio.to_thread(self._hand_language, self.t.lang)
        except OSError as e:
            logger.warning(f"wifi setup: could not hand the screens their language: {e}")
        rc, err = await self._run("restart", UNIT)
        if rc != 0:
            logger.warning(f"wifi setup: could not start {UNIT}: {err}")
            self.state = "idle"
            self._ended("it could not start; nothing changed.")
            await self._say_end(self.t._("Sorry, I couldn't go into setup mode."), retry=False)
            return
        logger.info("wifi setup: started" + (" (first boot)" if welcome else ""))
        await self._keep_awake(True)
        self._poller = self._spawn(self._follow())

    async def _cancel(self) -> None:
        # The unit writes "stopped" on SIGTERM, before the stop returns: a poll in that
        # gap announces it, and it is the user's cancel, not a stop from outside.
        self._cancelling = True
        try:
            rc, err = await self._run("stop", UNIT)
        finally:
            self._cancelling = False
        if self.state != "running":
            return  # it ended on its own meanwhile, and has said so
        if rc != 0:
            # Still running, as far as anyone knows: keep following it, and say only
            # what is true -- setup is on, and cancel is the way out.
            logger.warning(f"wifi setup: could not stop {UNIT}: {err}")
            if self._in_mode:
                await self.say(self.t._("I'm in setup mode. Say repeat to hear the details, "
                                        "or cancel to leave setup mode."))
            else:
                await self.say(self.t._("I couldn't stop yet. Say cancel again in a moment."))
            return
        await self._stop_following()
        self.state = "idle"
        self._ended(f"the user cancelled it; {_PUT_BACK}")
        await self._say_end(self._put_back_line(cancelled=True))

    async def _stop_following(self) -> None:
        if self._poller and self._poller is not asyncio.current_task():
            await cancel_and_wait(self._poller)   # how the poller ended is not ours to raise
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
        try:
            await self._following()
        finally:
            await self._keep_awake(False)

    async def _following(self) -> None:
        started = reminded = checked = self._clock()
        seen = False  # a status of this run has been read: the unit is up
        while self.state == "running":
            await asyncio.sleep(POLL_SECS)
            status = self._read()
            now = self._clock()
            if now - self._kept >= KEEP_AWAKE_SECS / 3:
                await self._keep_awake(True)
            # The first-boot setup, and the box is online by another way meanwhile -- not
            # while it joins the network picked on the phone: that is this setup's own
            # success, which "connected" announces.
            if (self._first_boot and self._last.get("phase") in (None, "ap_up", "failed")
                    and not _first_boot().is_new()):
                await self._leave_online()
                return
            if status is not None:
                seen = True
                if self._news(status):
                    reminded = now
                    await self._announce(status)
                    continue
                if status["phase"] == "ap_up" and now - reminded >= REMIND_SECS:
                    reminded = now
                    t = self.t
                    await self.say(t._("I'm still in setup mode, waiting for you.") + " "
                                   + instructions(status, t))
            elif now - started > START_TIMEOUT_SECS:
                # Nothing from it: it did not start -- or runs where we cannot read it.
                # Either way stop it, so the box is not offline with nobody following.
                logger.warning(f"wifi setup: no status after {START_TIMEOUT_SECS:.0f}s")
                await self._stop_unit()
                self.state = "idle"
                self._ended("it did not start; nothing changed.")
                await self._say_end(
                    self.t._("I couldn't go into setup mode. Please try again in a moment."),
                    retry=False)  # it says how already
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
        self._ended("it stopped without saying how (something went wrong).")
        await self._say_end(self._stopped_line(t._("something went wrong")))

    def _ended(self, outcome: str, online: bool = False) -> None:
        """Tell the model how this setup ended: "Wi-Fi setup ended: <outcome>" into its
        context (ClientNotes.add_notice). Only where the model is in the conversation: it
        started this setup (the wifi_setup tool), or the setup left the box `online`.

        A setup the phrase started (the box had no internet: the phrase works only then)
        that ends any other way leaves the box as it was, offline, and adds nothing --
        not even queued for later: no model can read it now, this one never heard of the
        setup (its words were setup's), and by the time the box is online again the line
        would be stale news out of nowhere. `outcome` is English (the context is) and
        never holds a password."""
        if self.notes is None or not (self._by_model or online):
            return
        self.notes.add_notice(f"Wi-Fi setup ended: {outcome}")

    async def _offer_account(self, line: str) -> bool:
        """Online, with no key typed on the setup page: offer to connect the owner's
        OpenRouter account where Settings is usable here and the box has no key of the
        owner's (openrouter_connect.offer_after_setup, owner decision on #282). True when
        it was said, with `line` first -- or once anything was said before it broke, so
        the line is never said twice."""
        from teaport_brain import openrouter_connect
        before = self.says
        try:
            return await openrouter_connect.offer_after_setup(self, line)
        except Exception as e:  # noqa: BLE001 -- the connected line is said regardless
            logger.warning(f"wifi setup: the account offer failed ({e!r})")
            return self.says != before

    async def _announce(self, status: dict) -> None:
        phase, t = status["phase"], self.t
        network = status.get("target") or t._("your network")
        logger.info(f"wifi setup: {phase}")
        if phase == "ap_up":
            # The reasons are English msgids from wifi.py / wifi_setup.py.
            lead = (t._("That didn't work: {reason}.").format(reason=t._(self._why)) + " "
                    if status.get("error") and self._why else "")
            # The first time: say what state the box is in (a retry has the reason).
            await self.say((lead or t._("I'm in setup mode.") + " ") + instructions(status, t))
            self._in_mode = True
        elif phase == "joining":
            await self.say(t._("Got it. I'm joining {network}. Switch your phone back to it.")
                           .format(network=network))
        elif phase == "failed":
            pass  # ap_up follows at once with the reason and the instructions again
        elif phase == "connected":
            self.state = "idle"
            target = status.get("target")
            account = status.get("account") if isinstance(status.get("account"), dict) else {}
            saved = account.get("result") == "saved"
            self._ended((f"connected to {json.dumps(target, ensure_ascii=False)}"
                         if target else "connected") + "; the box is online." + (
                " The owner's AI account key from the setup page was saved"
                + (" (its OpenRouter account has no credit yet)." if account.get("no_credit")
                   else ".") if saved else
                f" The AI account key typed on the setup page was not saved "
                f"({account.get('result')}: {account.get('reason') or 'no reason'})."
                if account else ""), online=True)
            # Online once: the box is no longer new (first_boot.py).
            self._first_boot = False
            _first_boot().reached_online("Wi-Fi setup connected")
            line = t._("I'm connected to {network}, and I'm online again.").format(network=network)
            if account:
                line += " " + t._((ACCOUNT_NO_CREDIT_TEXT if account.get("no_credit")
                                   else ACCOUNT_SAVED_TEXT) if saved
                                  else ACCOUNT_NOT_SAVED_TEXT if self.settings_usable
                                  else ACCOUNT_NOT_SAVED_PLAIN_TEXT)
            elif await self._offer_account(line):
                return  # said, with the Settings code to connect the account (#282)
            await self.say(line)
        elif phase == "timeout":
            self.state = "idle"
            # The reasons are English msgids (wifi.py / wifi_setup.py): no password.
            self._ended(f"it timed out; {_PUT_BACK}" + (
                f" The last attempt to join failed: {self._why}." if self._why else ""))
            await self._say_end(t._("Nothing connected in time, so I've left setup mode and put "
                                    "things back as they were."))
        elif phase == "stopped":
            # The unit was stopped from outside (systemctl, the config page, a restart)
            # and ran its restore path on the way out (wifi_setup.py, SIGTERM).
            self.state = "idle"
            self._ended(f"the user cancelled it; {_PUT_BACK}" if self._cancelling else
                        f"it was stopped from outside before it finished; {_PUT_BACK}")
            await self._say_end(self._put_back_line(cancelled=self._cancelling))
        elif phase == "error":
            self.state = "idle"
            self._ended(f"it failed: {status.get('reason') or 'something went wrong'}.")
            if status.get("reason") == PAGE_FAILED:
                # Only ever before setup mode, and nothing a user can act on but a retry.
                await self._say_end(
                    t._("I couldn't go into setup mode. Please try again in a moment."),
                    retry=False)  # it says how already
            else:
                await self._say_end(self._stopped_line(
                    t._(status.get("reason") or N_("something went wrong"))),
                    retry=status.get("reason") != NO_WIFI_DEVICE)

    def _put_back_line(self, cancelled: bool) -> str:
        """A stop -- the user's spoken cancel, or from outside (the Settings page, a
        restart): setup mode is left only once the user heard the box go into it."""
        t = self.t
        if self._in_mode:
            return t._("Okay, I've left setup mode and put things back as they were.")
        if cancelled:
            return t._("Okay, I've cancelled that. Everything is as it was.")
        return t._("Setup mode was turned off. Everything is as it was.")

    def _stopped_line(self, reason: str) -> str:
        """An error's line: a leaving once the user has heard the box is in setup mode,
        before that a failure to go into it (no Wi-Fi device, the network or the page
        would not start). `reason` is already in the user's language."""
        t = self.t
        if self._in_mode:
            return t._("I've left setup mode: {reason}.").format(reason=reason)
        return t._("Sorry, I couldn't go into setup mode: {reason}.").format(reason=reason)

    # -- the frames
    async def _ending(self) -> None:
        # The unit outlives the session; only the following stops.
        if self._lapser is not None:
            self._lapser.cancel()
        await self._stop_following()

    def idle(self) -> bool:
        if self.state == "confirm" and self._clock() - self._asked > CONFIRM_SECS:
            self._lapse()  # its timer has not run yet (a busy loop): the words decide
        return self.state == "idle"

    async def _own(self, text: str) -> bool:
        """Setup's own: the start phrase while idle, the answer to its question, and
        "cancel" / "repeat" while it runs."""
        t = self.t
        if self.idle():
            if not heard("start", text, t):
                return False
            if self.model and await self._online():
                logger.info("wifi setup: the phrase, but the box is online — the model's call")
                return False
            await self.begin()
            self._held = [text]
            return True
        if self.state == "confirm":
            if heard("no", text, t):
                # A no to the question asked: nothing goes back to the model, which
                # would only hand over again ("set up Wi-Fi" is a request for the tool).
                self.state, self._held = "idle", []
                self._ended("the user said no, so it never started.")
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
                    self._ended(_UNCONFIRMED)
                    return False
                self._held.append(text)
                await self.say(t._("Say yes to go into setup mode, or no."))
            return True
        # running
        if heard("cancel", text, t):
            await self._shielded(self._cancel())
        elif heard("repeat", text, t) and self._last.get("phase") == "ap_up":
            await self.say(instructions(self._last, t, spelled=True))  # the details, read out
        elif self._in_mode:
            await self.say(t._("I'm in setup mode. Say repeat to hear the details, "
                               "or cancel to leave setup mode."))
        else:
            await self.say(t._("One moment, I'm getting setup mode ready. Say cancel to stop."))
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


async def _output(*cmd: str, timeout: float) -> str | None:
    """`cmd`'s stdout, stripped; None when it cannot run or takes longer than `timeout`.
    One that overruns (or whose caller is cancelled, by a barge-in) is killed, not left
    behind."""
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return None
    finally:
        if p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            await p.wait()


async def connectivity(timeout: float | None = None) -> str | None:
    """NetworkManager's connectivity, freshly checked: "full", or what it says instead
    ("limited", "portal", "none", "unknown"; "" when the check could not be run or took
    longer than `timeout`, by default ONLINE_CHECK_SECS). None when NM has no connectivity
    check configured, so nobody can tell (first_boot.py does nothing then).

    The check must be configured (Ubuntu's network-manager-config-connectivity-ubuntu,
    which install.sh puts there): without it NM says "full" whenever there is a default
    route, internet or not. A fresh check, not the cached answer, which can be minutes
    old after the internet drops."""
    enabled = await _output(
        "busctl", "get-property", "org.freedesktop.NetworkManager",
        "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager",
        "ConnectivityCheckEnabled", timeout=NM_PROPERTY_SECS)
    if enabled != "b true":
        return None
    return await _output("nmcli", "networking", "connectivity", "check",
                         timeout=ONLINE_CHECK_SECS if timeout is None else timeout) or ""


async def _box_online() -> bool:
    """Is the box on the internet? Asked only when the phrase was heard, and wrong on
    the safe side: anything but a fresh "full" leaves the phrase working (no check
    configured, a slow or dead link included)."""
    return await connectivity() == "full"


# NetworkManager's overall state (NMState): CONNECTED_LOCAL and up (50 local, 60 site,
# 70 global) have an address on some network.
NM_CONNECTED_LOCAL = 50


async def _box_on_network() -> bool:
    """Is the box on any network (NetworkManager's State at least CONNECTED_LOCAL)? Only
    a clear answer below it is "no": NM not answering counts as on one."""
    state = await _output(
        "busctl", "get-property", "org.freedesktop.NetworkManager",
        "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager", "State",
        timeout=NM_PROPERTY_SECS)
    parts = (state or "").split()
    if len(parts) == 2 and parts[0] == "u" and parts[1].isdigit():
        return int(parts[1]) >= NM_CONNECTED_LOCAL
    return True


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
