#
# goodbye.py — the room said goodbye: it goes back to sleep (#221).
#
# In a room session with wake words, end_conversation is how the box goes back to sleep
# (tools.py; the local audio bridge performs it, local_audio.Restart). Its description
# says to call it straight away on "good night", "go to sleep" and the like, and to say
# the goodbye after. gpt-oss-120b does not always: 2026-10-24 05:53 the caller said
# "Okay, good night.", "Good night.", "You didn't go to sleep." and "Go to sleep.", the
# model answered each with a goodbye ("Good night—take care!", "Going to sleep now. Good
# night.") and called the tool only on the fifth turn. The worst of it: the box SAID it
# was going to sleep and went on listening, and the caller believed it.
#
# The backstop is on the model's own words. A reply that is only a goodbye -- "Good
# night!", "Going to sleep now.", "Bye, take care!" -- heard out to its end, in a turn
# that did not call end_conversation, sends the room to sleep the tool's own way: the
# brain calls end_conversation on the bridge itself (tools.call_client_tool) with
# "said": true, since its goodbye has already played, and the bridge's Restart ends the
# session once its speaker is quiet (local_audio.Restart, armed `said`). The wake gate
# keeps the conversation across it as for the model's own call (wake_gate.py).
#
# Narrow on purpose, since a wrong sleep costs the caller a wake word:
#   * the WHOLE reply is goodbye: each of its clauses is a goodbye or a sleep line
#     (_CORE) or the small talk around one (_AROUND: "okay", "thanks", "I'm here if you
#     need me"), with at least one goodbye, at most a name beside it ("Good night,
#     Sam!"), MAX_WORDS in all. A goodbye inside a longer answer ("Good night! Your
#     alarm is set for seven."), a quoted one ('"Good night" in Japanese is
#     "oyasumi".'), a question ("Going to bed already?") is none;
#   * heard to its end: a reply the caller cut is no goodbye they accepted;
#   * a reply to the caller's words (a commit or the answer after a tool), that called
#     no tool itself, in a turn with no end_conversation call (the model did it right:
#     its own call has armed the bridge);
#   * nothing owed: no consult running, no answer owed (owed.py), no tool call out, not
#     on hold for a phone call -- the room does not sleep on an answer still coming (nor
#     does the bridge's keep-alive, on a consult running) or one not heard yet;
#   * nothing since: the caller's next turn open or committed, or a completion under
#     way, means the conversation went on;
#   * once per session (the session ends on it).
#
# English only. The clauses are English, so a session whose voice speaks another
# language is left alone (inert): a model that says "Bonne nuit" without calling the
# tool still keeps the box awake until the keep-alive. Each language's own goodbyes, and
# the caller's own whole-turn "good night" ending the session (#221's option 1), would be
# per-catalog patterns as wifi_voice.PATTERNS has them; neither is here.
#
# Rooms only (agent_session.py builds it where end_conversation is offered: the room's
# bridge with wake words). A phone call has no sleep to go back to, and hanging one up
# is not this (#299).
#
import re
import unicodedata

from loguru import logger

from teaport_brain.conversation_log import quoted
from teaport_brain.owed import COMMIT, POST, is_caller_message
from teaport_brain.tools import call_client_tool

# The tool the backstop calls, and the one whose call in the turn stands it down.
TOOL = "end_conversation"
# A goodbye is short; a reply longer than this is an answer with a goodbye in it.
MAX_WORDS = 24

# ------------------------------------------------------------------ the goodbye

# A request to stay, or for an answer, is no goodbye.
_QUESTION = re.compile(r"[?？¿]")
# Quoted words are said about, not said: "\"Good night\" in Spanish is \"buenas noches\"".
_QUOTE = re.compile(r"[\"“”„«»]")
# Sentences, and the dashes that join two goodbyes ("Good night—take care!").
_SENTENCES = re.compile(r"[.!;:]+|\s[-‐‑‒]+\s|[–—―]+")
# Within one: its clauses.
_CLAUSES = re.compile(r",|\band\b")
# What stays of a clause: letters, digits, apostrophes, hyphens.
_NOT_WORD = re.compile(r"[^\w'\- ]+")

_THEN = r"(?: (?:now|then|again|too|for now|tonight|for tonight|for the night))*"
_NEED = r"(?: (?:if|when|whenever) you need (?:me|anything|something)(?: else)?)?"
# A goodbye, or the box saying it goes to sleep: one alternative per line.
_CORE = re.compile("|".join(f"(?:{p})" for p in (
    r"(?:a )?(?:very )?(?:good ?-?night|g'night)(?: to you(?: all)?| to (?:you )?all"
    r"| everyone| everybody| all| folks)?" + _THEN,
    r"night(?:[ -]night)?|nighty[ -]night|nite(?: nite)?",
    r"(?:good ?-?)?bye(?:[ -]bye)?" + _THEN + r"|farewell",
    r"(?:sleep (?:well|tight)|sweet dreams|rest (?:well|easy))" + _THEN,
    r"(?:i'm |i am |i'll be )?(?:going|heading|headed|off) (?:back )?to sleep" + _THEN,
    r"(?:i'll |i will |let me |time to |time for me to )?(?:go|head) (?:back )?to sleep"
    + _THEN,
    r"(?:i'll |i will )?sleep now|(?:i'm )?sleeping(?: now)?|(?:going )?back to sleep"
    r"(?: now| i go)?|off (?:i go|to sleep)(?: now)?|time (?:for me )?to sleep",
    r"(?:i'll |i will |i'm going to |i am going to )?stop listening" + _THEN
    + r"|(?:i'm |i am )?(?:no longer|done) listening" + _THEN,
    r"(?:i'm |i am )?signing off" + _THEN,
    r"(?:i hope you |hope you )?(?:have|enjoy) (?:a|the rest of your|your) (?:\w+ )?"
    r"(?:night|night's sleep|night's rest|evening|rest|sleep|day|weekend|morning|one)"
    + _THEN,
    r"(?:see|catch) (?:you|ya)(?: (?:later|soon|tomorrow|tomorrow morning|in the morning"
    r"|next time|around|then|again))*",
    r"(?:talk|speak) (?:to you |with you )?(?:later|soon|tomorrow|again)" + _THEN
    + r"|later|until (?:next time|tomorrow|later|then)|till (?:next time|tomorrow|then)",
    r"take care(?: of yourself)?" + _THEN,
)))
# What a goodbye comes with, and is nothing on its own.
_AROUND = re.compile("|".join(f"(?:{p})" for p in (
    r"ok(?:ay)?|okey dokey|alright|all right|sure(?: thing)?|of course|absolutely"
    r"|certainly|got it|you got it|gotcha|will do|no problem|no worries|sounds good"
    r"|perfect|great|understood|right|yes|yeah|yep|well|so|oh|ah|aw+|mhm|hmm",
    r"(?:thanks|thank you)(?: so much| very much| a lot)?(?: too| as well)?"
    r"(?: for (?:the|our|this|a) (?:\w+ )?(?:chat|talk|conversation|evening))?",
    r"you too|and you|same to you|likewise|you're welcome|anytime|any time|my pleasure",
    r"(?:oh )?(?:sorry|oops|my apologies|apologies)(?: about that| for that)?|my bad"
    r"|you're right",
    r"(?:i'm )?(?:glad|happy) (?:i could|to|i was able to) help"
    r"|(?:i'm )?(?:glad|happy) to be of (?:help|service)",
    r"(?:it was )?(?:nice|lovely|great|good|fun|a pleasure) (?:talking|chatting|speaking)"
    r" (?:to|with) you",
    r"(?:i'll be |i'm |i am )?(?:right )?here" + _NEED,
    r"(?:just )?(?:say|use) (?:my name|the wake word|my wake word|the word)" + _NEED,
    r"(?:just )?(?:wake me|call me|call on me|let me know)(?: up)?" + _NEED,
)))
# A name beside a goodbye ("Good night, Sam", "Sam, good night"): a word or two.
_NAME = re.compile(r"[^\W\d_]+(?: [^\W\d_]+)?")


def _clauses(sentence: str) -> list:
    out = []
    for c in _CLAUSES.split(sentence):
        c = " ".join(_NOT_WORD.sub(" ", c).replace("_", " ").split()).strip("-' ")
        if c:
            out.append(c)
    return out


def is_goodbye(text: str) -> bool:
    """Is `text`, a whole reply, only a goodbye or a going-to-sleep line (the module
    note)? English."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace("’", "'").replace("‘", "'")
    if _QUESTION.search(t) or _QUOTE.search(t):
        return False
    # Emoji, symbols, markdown: none of it is said.
    t = "".join(" " if unicodedata.category(c)[0] in "SC" or c in "*#`" else c for c in t)
    if len(t.split()) > MAX_WORDS:
        return False
    said = False
    for sentence in _SENTENCES.split(t):
        clauses = _clauses(sentence)
        cores = [bool(_CORE.fullmatch(c)) for c in clauses]
        named = False
        for i, c in enumerate(clauses):
            if cores[i] or _AROUND.fullmatch(c):
                continue
            beside = (i > 0 and cores[i - 1]) or (i + 1 < len(clauses) and cores[i + 1])
            if named or not beside or not _NAME.fullmatch(c):
                return False
            named = True
        said = said or any(cores)
    return said


def english(code) -> bool:
    """Does a session whose voice speaks `code` (engine_tts's espeak code: "en-us",
    "en-gb", "fr-fr", ...) speak English?"""
    return re.split(r"[-_]", (code or "").strip().lower())[0] == "en"


# ------------------------------------------------------------------ the backstop

def _turn_calls(messages: list, name: str):
    """Whether the turn being answered -- every message since the caller's last words --
    called `name`; None when the caller has said nothing."""
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or ():
                if isinstance(c, dict) and (c.get("function") or {}).get("name") == name:
                    return True
        elif is_caller_message(m):
            return False
    return None


class GoodbyeSleep:
    """The backstop (the module note), fed each reply's end by the session's transcript
    ledger (TranscriptLedger.on_reply_end, after owed.Owed.settle). `context`: the
    session's LLMContext; `owed`: its owed.Owed; `gate`: its FollowupGate; `lang_fn`: the
    voice's language, read live (a switch_voice switches it); `llm`: the session's LLM
    service, whose push reaches the client and whose tasks are the session's."""

    def __init__(self, *, context, owed, gate, lang_fn, llm):
        self._context, self._owed, self._gate = context, owed, gate
        self._lang_fn, self._llm = lang_fn, llm
        self.sent = False

    def reply_end(self, reply, heard, *, cut: bool, audio: bool, text=None, rest=None) -> None:
        if self.sent or reply is None or reply.kind not in (COMMIT, POST):
            return
        if cut or not audio or not (heard or "").strip() or not text:
            return
        if not english(self._lang_fn()) or not is_goodbye(text):
            return
        if _turn_calls(self._context.get_messages(), TOOL) is not False:
            # The model called it (its own call has armed the bridge), or the caller said
            # nothing this answers (a greeting, a note's reaction).
            return
        why = self._stays_awake(reply)
        if why is not None:
            logger.info(f"GOODBYE: {quoted(text)} without {TOOL}, but {why} -- the room "
                        "stays awake")
            return
        self.sent = True
        logger.info(f"GOODBYE: {quoted(text)} heard out, and {TOOL} was not called -- "
                    "sending the room back to sleep (#221)")
        self._llm.create_task(call_client_tool(self._llm, TOOL, {}, extra={"said": True}),
                              "goodbye-sleep")

    def _stays_awake(self, reply):
        """Why a goodbye heard out, in a turn that did not call the tool, does not send
        the room to sleep, or None."""
        if reply.calls:
            return "it called a tool, whose answer is still to come"
        gate = self._gate
        if gate.consults_running or self._owed.owed():
            return "an answer is still owed"
        if gate.calls_out:
            return "a tool call is out"
        if gate.held:
            return "the conversation is on hold for a phone call"
        if gate.caller_request_pending or gate.turn_in_flight:
            return "the conversation went on"
        return None
