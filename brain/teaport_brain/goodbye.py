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
#   * the WHOLE reply is goodbye: each of its clauses is a goodbye (_BYE), a line that
#     says the box goes to sleep (_SLEEP) or the small talk around one (_AROUND: "okay",
#     "thanks", "I'm here if you need me"), with at least one of the first two, MAX_WORDS
#     in all. Beside a goodbye, after a comma, a NAME may stand ("Good night, Sam!"):
#     a word or two each written with a capital, or a word for anyone ("my friend") --
#     never any other short clause ("Good night, alarm set.", "Bye, and lock up."). A
#     goodbye inside a longer answer ("Good night! Your alarm is set for seven."), a
#     quoted one ('"Good night" in Japanese is "oyasumi".'), a question ("Going to bed
#     already?") is none;
#   * heard to its end: a reply the caller cut is no goodbye they accepted;
#   * a reply to the caller's words (a commit or the answer after a tool), that called
#     no tool itself, in a turn with no end_conversation call (the model did it right:
#     its own call has armed the bridge);
#   * not the answer to a question: "What's the opposite of hello?" -- "Goodbye!" (the
#     caller's words end in "?", start with a wh-word or an auxiliary and a subject, or
#     ask for a word: "Translate adiós into English.", "What's another word for
#     goodbye?" -- asks()). A question that asks the box to go, though, is answered by
#     a goodbye ("Can you go to sleep?" -- "Good night—take care!", #221's own shape:
#     _ASKS_TO_GO), as is a turn that is itself only a goodbye ("Okay, good night?").
#     And a reply that says the box goes to sleep sleeps whatever was asked: saying so
#     and listening on is #221's harm;
#   * nothing owed: no consult running, no answer owed (owed.py), no tool call out, not
#     on hold for a phone call -- the room does not sleep on an answer still coming (nor
#     does the bridge's keep-alive, on a consult running) or one not heard yet (the
#     owner's decision, 2026-11-05). Except where all that is owed is failures (a
#     consult that ended in an error: record.failure) and the caller's own turn signed
#     off -- the sleep phrase, or a turn that is only a goodbye ("Okay, good night.",
#     signed_off): a failure is nothing to wait for, and they asked to go. It stays owed
#     for the next wake (the room's sleep keeps the records, wake_gate.py);
#   * nothing since: the caller's next turn open or committed, or a completion under
#     way, means the conversation went on;
#   * the caller not talking. A turn starts only on their words (barge_pause's min
#     words), so "oh wait, one more thing" over the goodbye's end has opened none yet:
#     at the VAD level -- their speech, or too short a while since it for its words to
#     have come -- the sleep is HELD, as the run's door holds a run (FollowupGate
#     .speech_over: the #198 hold and its cap, TEAPORT_REPLY_HOLD_MAX_S), and decided
#     again once it is over. Still talking at the cap, the room stays awake.
#     A turn-level guard alone sleeps under their words (formal/Owed.tla's ow_turnLevel:
#     NoRunUnderSpeech);
#   * once per session (the session ends on it).
#
# In every language (#320). The clause tables are per-catalog patterns (PATTERNS), as
# wifi_voice.PATTERNS has them: a session hears English and its voice's language. Each
# language's are its own words, not a translation: English, Spanish and Italian (the
# release languages) have theirs; the other catalogs carry the English pattern until
# someone who speaks the language writes one, so their sessions hear English goodbyes
# alone (a model's "Bonne nuit" there still waits for the keep-alive). A word that greets
# as well as parts -- Spanish "buenas noches" (good evening), Italian "ciao" -- is a goodbye
# in a reply, but not as the caller's first words (PATTERNS["greets"], SleepPhrase).
#
# The caller's own goodbye (#221's option 1). A turn that is only a goodbye -- "Good
# night.", "Okay, bye!", "Hasta mañana.", "Buonanotte!" -- is the sleep phrase too
# (SleepPhrase, THE SLEEP PHRASE below), matched by goodbye_kind as the model's is, and
# stood down as the backstop is while an answer is owed (owed_reason).
#
# Rooms only (agent_session.py builds it where end_conversation is offered: the room's
# bridge with wake words). A phone call has no sleep to go back to, and hanging one up
# is not this (#299).
#
# THE SLEEP PHRASE. Both of the above need the model: to call the tool, or to say the
# goodbye the backstop hears. A model that cannot answer (no key, an account out of
# credit, a provider down) left the room awake on "go to sleep" until the keep-alive,
# answering each with the error line. So "go to sleep" itself is a fixed phrase, as
# "open my settings" is (settings_access.py): heard by the session's PhraseVoice ahead
# of the model, in English and the voice's language (each catalog's "sleep" pattern,
# wifi_voice.request_heard: a request on its own -- "okay, go to sleep", "you can go
# back to sleep now", "can you go to sleep?" -- never a question about it or a sentence
# that mentions it). SleepPhrase says one short line (SLEEP_TEXT), waits for it to play
# out, and sends the room to sleep the backstop's way: end_conversation on the bridge,
# "said": true. No model is asked, so it works with none, and it wins over anything
# owed -- the caller asked -- which stays owed for the next wake. So does "stop
# listening". A turn that is only a goodbye is heard the same way, except that it stands
# down while an answer is still owed (above). Not if the caller cut the line or went on
# talking before it ended: that is the conversation going on. Where the backstop is:
# rooms with wake words, end_conversation on (agent_session.py).
#
import asyncio
import collections
import functools
import re
import unicodedata

from loguru import logger

from teaport_brain import i18n, wifi_voice
from teaport_brain.conversation_log import quoted
from teaport_brain.heard_context import msg_text
from teaport_brain.i18n import N_
from teaport_brain.owed import COMMIT, POST, is_caller_message
from teaport_brain.tools import call_client_tool

# The tool the backstop calls, and the one whose call in the turn stands it down.
TOOL = "end_conversation"
# A goodbye is short; a reply longer than this is an answer with a goodbye in it.
MAX_WORDS = 24
# What goodbye_kind finds: a goodbye, or a line that says the box goes to sleep.
BYE, SLEEP = "bye", "sleep"
# The backstop's decision on a reply that sends the room to sleep (GoodbyeSleep.decisions).
SENT = "sent"

# ------------------------------------------------------------------ the goodbye

# A request to stay, or for an answer, is no goodbye.
_QUESTION = re.compile(r"[?？¿]")
# Quoted words are said about, not said: "\"Good night\" in Spanish is \"buenas noches\"".
_QUOTE = re.compile(r"[\"“”„«»]")
# Sentences, and the dashes that join two goodbyes ("Good night—take care!").
_SENTENCES = re.compile(r"[.!;:]+|\s[-‐‑‒]+\s|[–—―]+")
# Within one: its clauses, and what joins them (a name never follows "and"): the comma,
# and the language's "and" (PATTERNS["and"]).
# What stays of a clause: letters, digits, apostrophes, hyphens.
_NOT_WORD = re.compile(r"[^\w'\- ]+")

_THEN = r"(?: (?:now|then|again|too|for now|tonight|for tonight|for the night))*"
_NEED = r"(?: (?:if|when|whenever) you need (?:me|anything|something)(?: else)?)?"


def _either(*patterns) -> str:
    return "|".join(f"(?:{p})" for p in patterns)


# A goodbye: one alternative per line.
_BYE = _either(
    r"(?:a )?(?:very )?(?:good ?-?night|g'night)(?: to you(?: all)?| to (?:you )?all"
    r"| everyone| everybody| all| folks)?" + _THEN,
    r"night(?:[ -]night)?|nighty[ -]night|nite(?: nite)?",
    r"(?:good ?-?)?bye(?:[ -]bye)?" + _THEN + r"|farewell",
    r"(?:sleep (?:well|tight|soundly)|(?:wishing you |i wish you )?sweet dreams"
    r"|rest (?:well|easy))" + _THEN,
    r"(?:i hope you |hope you )?(?:have|enjoy) (?:a|the rest of your|your) (?:\w+ )?"
    r"(?:night|night's sleep|night's rest|evening|rest|sleep|day|weekend|morning|one)"
    + _THEN,
    r"(?:see|catch) (?:you|ya)(?: (?:later|soon|tomorrow|tomorrow morning|in the morning"
    r"|next time|around|then|again))*",
    # Not a bare "later": "Can you do that later?" -- "Sure, later."
    r"(?:talk|speak) (?:to you |with you )?(?:later|soon|tomorrow|again)" + _THEN
    + r"|until (?:next time|tomorrow|later|then)|till (?:next time|tomorrow|then)",
    r"take care(?: of yourself)?" + _THEN,
)
# The box says it goes to sleep, or stops listening.
_SLEEP = _either(
    r"(?:i'm |i am |i'll be )?(?:going|heading|headed|off) (?:back )?to sleep" + _THEN,
    r"(?:i'll |i will |let me |time to |time for me to )?(?:go|head) (?:back )?to sleep"
    + _THEN,
    r"(?:i'll |i will )?sleep now|(?:i'm )?sleeping(?: now)?|(?:going )?back to sleep"
    r"(?: now| i go)?|off (?:i go|to sleep)(?: now)?|time (?:for me )?to sleep",
    r"(?:i'll |i will |i'm going to |i am going to )?stop listening" + _THEN
    + r"|(?:i'm |i am )?(?:no longer|done) listening" + _THEN,
    r"(?:i'm |i am )?signing off" + _THEN,
)
# What a goodbye comes with, and is nothing on its own.
_AROUND = _either(
    r"ok(?:ay)?|okey dokey|alright|all right|sure(?: thing)?|of course|absolutely"
    r"|certainly|got it|you got it|gotcha|will do|no problem|no worries|sounds good"
    r"|perfect|great|understood|right|yes|yeah|yep|well|so|oh|ah|aw+|mhm|hmm",
    r"(?:thanks|thank you)(?: so much| very much| a lot)?(?: too| as well)?"
    r"(?: for (?:the|our|this|a) (?:\w+ )?(?:chat|talk|conversation|evening)"
    r"| for (?:chatting|talking)(?: with me)?)?",
    r"you too|and you|same to you|likewise|you're welcome|anytime|any time|my pleasure",
    # Only beside a goodbye: alone, it answers a storm warning.
    r"stay safe",
    r"(?:oh )?(?:sorry|oops|my apologies|apologies)(?: about that| for that)?|my bad"
    r"|you're right",
    r"(?:i'm )?(?:glad|happy) (?:i could|to|i was able to) help"
    r"|(?:i'm )?(?:glad|happy) to be of (?:help|service)",
    r"(?:it was )?(?:nice|lovely|great|good|fun|a pleasure) (?:talking|chatting|speaking)"
    r" (?:to|with) you",
    r"(?:i'll be |i'm |i am )?(?:always |right )*here" + _NEED,
    r"if you need (?:me|anything|something)(?: else)?",
    r"(?:just )?(?:say|use) (?:my name|the wake word|my wake word|the word)" + _NEED,
    r"(?:just )?(?:wake me|call me|call on me|let me know)(?: up)?" + _NEED,
    r"feel free to (?:call|wake|ask) (?:me|on me)(?: up)?"
    r"(?: (?:any ?time|again|whenever(?: you like| you want)?|later))?",
)
# Who a goodbye is said to, beside it: a word for anyone, or a name (_named).
# ("teaport": the box's name, as the STT writes it, lowercase.)
_ANYONE = r"(?:my )?(?:friend|dear)|buddy|pal|you two|you all|y'all|teaport"
_WORD = re.compile(r"[^\W\d_]+")


def _named(said: str) -> bool:
    """A name as written: a word or two, each of letters and with a capital first
    ("Sam", "Mary Ann"), never a short clause ("alarm set", "Door locked")."""
    words = said.split()
    return 1 <= len(words) <= 2 and all(_WORD.fullmatch(w) and w[0].isupper() for w in words)


# The caller asked something: a wh-word or an auxiliary and its subject first ("can you
# do that later", "how do you say adiós"), whatever the punctuation (a question mark is
# asks()'s own test). Not "have"/"will" alone: "Have a good night.", "Will do, good
# night." are sign-offs.
_ASKS = (r"^\W*(?:(?:okay|ok|so|and|but|well|hey|oh|um+|uh+|hmm+)\W+)*"
         r"(?:(?:what|what's|whats|who|who's|whom|whose|which|when|where|why|how)\b"
         r"|(?:can|could|will|would|shall|should|may|might|do|does|did|is|are|was|were|am"
         r"|can't|won't|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|couldn't|wouldn't"
         r"|shouldn't)\s+(?:i|you|we|they|he|she|it|that|this|there|your|my|the)\b)")
# ...or for a word, in any form: "Translate adiós into English.", "Tell me the opposite
# of hello." -- a goodbye is then the answer, whatever else they said.
# Not a bare "to say": "I just wanted to say good night." is a sign-off.
_ASKS_FOR_A_WORD = (
    r"\btranslat\w*|\bin english\b|\bword for\b|\bsynonym\w*|\bopposite of\b"
    r"|\bmeaning of\b|\bway to say\b|\bspell\w*|\brepeat after me\b|\bname a word\b"
    r"|\bwords? that means?\b|\b(?:does|do|did) \S+ mean\b"
    r"|\bhow (?:do|would|should|can|could) (?:you|i|we) say\b"
    r"|\bwhat (?:does|did|do) .{1,40}\bmean\b"
    r"|\b(?:is it|which is(?: right| correct)?|should i (?:say|write))\b[^.!?]*\bor\b")
# ...unless the question asks the box itself to go: to sleep, to stop listening, to be
# quiet ("Can you go to sleep?", "Why are you still awake?", "Could you stop
# listening?"). A goodbye is its answer. Not a goodbye word anywhere in it: "What's
# another word for goodbye?" is about the word (a turn that is only a goodbye is
# asks()'s own case).
_ASKS_TO_GO = (
    r"\b(?:go(?:ing)? (?:back )?to (?:sleep|bed)|sleep now|stop listening|still (?:awake|up"
    r"|listening)|that's (?:all|it)|that is (?:all|it)|be quiet|shut up)\b")

# Each language's words for all of the above, by meaning (#320). The English patterns are
# the msgids of "pattern" entries in each catalog (i18n.py), whose msgstr is that
# language's own, written for it rather than translated: what its speakers say, and its
# model writes, in both registers. A session hears its language's and always English's,
# as wifi_voice.PATTERNS does. A catalog whose msgstr is the English pattern itself has
# none of its own yet (only en, es and it have theirs): its sessions match English alone.
# Matched against a clause folded to lower case, with its punctuation gone (_clauses):
# "bye", "sleep", "around" and "anyone" must be the whole clause; "and" joins two;
# "asks", "asks_for_a_word" and "asks_to_go" are searched in the caller's words.
PATTERNS = {
    "bye": _BYE,
    "sleep": _SLEEP,
    "around": _AROUND,
    # A clause that greets as well as parts -- Spanish "buenas noches", Italian "ciao":
    # a goodbye, but not as the caller's first words, where it is hello (SleepPhrase).
    # English has none: it matches nothing.
    "greets": r"(?!)",
    "anyone": _ANYONE,
    "and": r"and",
    "asks": _ASKS,
    "asks_for_a_word": _ASKS_FOR_A_WORD,
    "asks_to_go": _ASKS_TO_GO,
}


class _Words:
    """PATTERNS compiled for one language: its own and English's, either matching."""

    def __init__(self, t: i18n.T):
        def both(meaning):
            english = PATTERNS[meaning]
            own = t.p("pattern", english)
            if own != english:
                try:
                    return re.compile(f"(?:{english})|(?:{own})", re.I)
                except re.error:
                    logger.warning(f"goodbye: bad {t.lang} pattern for {meaning!r} -- "
                                   "fix its .po entry; English alone meanwhile")
            return re.compile(english, re.I)
        self.bye, self.sleep, self.around = both("bye"), both("sleep"), both("around")
        self.greets = both("greets")
        self.anyone = both("anyone")
        self.clauses = re.compile(r"(,|\b(?:" + both("and").pattern + r")\b)", re.I)
        self.asks, self.for_a_word = both("asks"), both("asks_for_a_word")
        self.to_go = both("asks_to_go")


@functools.cache
def _words(lang: str) -> _Words:
    return _Words(i18n.get(lang))


def _t(t) -> i18n.T:
    return t if t is not None else i18n.get(i18n.SOURCE_LANG)


def _clauses(sentence: str, words: _Words) -> list:
    """`sentence`'s clauses: (as written, folded, whether "and" joined it on)."""
    out, after_and = [], False
    for i, part in enumerate(words.clauses.split(sentence)):
        if i % 2:
            after_and = part.strip() != ","
            continue
        said = " ".join(_NOT_WORD.sub(" ", part).replace("_", " ").split()).strip("-' ")
        if said:
            out.append((said, said.casefold(), after_and))
    return out


def goodbye_kind(text: str, t: i18n.T | None = None, *, greets_part: bool = True):
    """What `text`, a whole reply, is (the module note): SLEEP when it is only a goodbye
    and says the box goes to sleep, BYE when it is only a goodbye, else None. In English
    and in t's language (PATTERNS). `greets_part`: a clause that greets as well as parts
    ("buenas noches") is a goodbye -- not for the caller's first words, where it is
    their hello and only small talk beside a real goodbye."""
    words = _words(_t(t).lang)
    s = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'")
    if _QUESTION.search(s) or _QUOTE.search(s):
        return None
    # Emoji, symbols, markdown: none of it is said.
    s = "".join(" " if unicodedata.category(c)[0] in "SC" or c in "*#`" else c for c in s)
    if len(s.split()) > MAX_WORDS:
        return None
    kinds = set()
    for sentence in _SENTENCES.split(s):
        clauses = _clauses(sentence, words)
        said = [SLEEP if words.sleep.fullmatch(f) else BYE if words.bye.fullmatch(f)
                or greets_part and words.greets.fullmatch(f) else None
                for _, f, _ in clauses]
        named = False
        for i, (written, folded, after_and) in enumerate(clauses):
            if said[i] or words.around.fullmatch(folded) or words.greets.fullmatch(folded):
                continue
            # A name: after a comma, right after a goodbye, once.
            if (named or after_and or i == 0 or not said[i - 1]
                    or not (words.anyone.fullmatch(folded) or _named(written))):
                return None
            named = True
        kinds.update(k for k in said if k)
    return SLEEP if SLEEP in kinds else BYE if kinds else None


def is_goodbye(text: str, t: i18n.T | None = None) -> bool:
    """Is `text`, a whole reply, only a goodbye or a going-to-sleep line?"""
    return goodbye_kind(text, t) is not None


def asks(said: str, t: i18n.T | None = None) -> bool:
    """Did the caller ask a question in `said` (their words, as committed) that a goodbye
    would answer as a word, not as a leave-taking? In English and in t's language."""
    words = _words(_t(t).lang)
    said = (said or "").replace("’", "'")
    if words.for_a_word.search(said):
        return True
    if words.to_go.search(said) or goodbye_kind(_QUESTION.sub(".", said), t):
        return False                       # the box asked to go, or a goodbye of theirs
    return bool(_QUESTION.search(said) or words.asks.search(said))


def signed_off(said: str, t: i18n.T) -> bool:
    """Did the caller's own turn (`said`, their words as committed) ask the box to go:
    the sleep phrase (SleepPhrase), or a turn that is only a goodbye or a going-to-sleep
    line ("Okay, good night.", "Bye!" -- not "Good night?", a question)? In English and
    in t's language."""
    return (wifi_voice.request_heard("sleep", said or "", t)
            or goodbye_kind(said, t) is not None)


def owed_reason(gate, owed, *, signed: bool):
    """Why the room must not sleep yet for what it still owes the caller, or None: a
    consult running, an answer owed (unless all that is owed is failures and the caller
    `signed` off: a failure is nothing to wait for), a tool call out, the conversation on
    hold for a phone call. The backstop's (GoodbyeSleep) and the caller's own goodbye's
    (SleepPhrase) stand-downs alike."""
    if gate.consults_running:
        return "an answer is still owed"
    book = owed.owed()
    if book and not (signed and all(r.failure for r in book)):
        return "an answer is still owed"
    if gate.calls_out:
        return "a tool call is out"
    if gate.held:
        return "the conversation is on hold for a phone call"
    return None


# ------------------------------------------------------------------ the backstop

def _turn(messages: list, name: str):
    """The turn being answered -- every message since the caller's last words: (whether
    it called `name`, those words); (False, None) when the caller has said nothing."""
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or ():
                if isinstance(c, dict) and (c.get("function") or {}).get("name") == name:
                    return True, None
        elif is_caller_message(m):
            return False, msg_text(m).strip()
    return False, None


def _callers(messages: list) -> int:
    """How many messages the caller has said."""
    return sum(1 for m in messages if is_caller_message(m))


class GoodbyeSleep:
    """The backstop (the module note), fed each reply's end by the session's transcript
    ledger (TranscriptLedger.on_reply_end, after owed.Owed.settle). `context`: the
    session's LLMContext; `owed`: its owed.Owed; `gate`: its FollowupGate; `lang_fn`: the
    voice's language, read live (a switch_voice switches it); `llm`: the session's LLM
    service, whose push reaches the client and whose tasks are the session's.

    `decisions`: what it made of each reply it was fed, newest last -- SENT, or why not
    (the session tests read it, and a box's journal has the goodbye's ones)."""

    def __init__(self, *, context, owed, gate, lang_fn, llm):
        self._context, self._owed, self._gate = context, owed, gate
        self._lang_fn, self._llm = lang_fn, llm
        self.sent = False          # the room was sent to sleep (once per session)
        self.holding = False       # a goodbye's sleep waits for the caller's speech
        self.decisions: collections.deque = collections.deque(maxlen=32)

    def reply_end(self, reply, heard, *, cut: bool, audio: bool, text=None, rest=None) -> None:
        why, kind = self._not_a_goodbye(reply, heard, cut=cut, audio=audio, text=text)
        if why is not None:
            self.decisions.append(why)
            return
        why = self._stays_awake(reply, kind)
        if why is None and self._gate.caller_speaking:
            self.holding = True
            self.decisions.append("held: the caller is talking")
            logger.info(f"GOODBYE: {quoted(text)} without {TOOL}, but the caller is talking "
                        "-- the sleep waits until their speech is over")
            callers = _callers(self._context.get_messages())
            self._llm.create_task(self._after_speech(reply, kind, text, callers),
                                  "goodbye-hold")
            return
        self._decide(why, text)

    async def _after_speech(self, reply, kind, text, callers: int) -> None:
        """The held sleep, decided again once the caller's speech is over (their words
        took no turn: a cough, the room) -- or not, at the hold's cap."""
        try:
            over = await self._gate.speech_over()
        finally:
            self.holding = False
        if not over:
            why = ("the session ended" if self._gate.ended else
                   "the caller was still talking at the hold's cap")
        elif _callers(self._context.get_messages()) != callers:
            why = "the conversation went on"       # their words were committed meanwhile
        elif self._gate.caller_speaking:
            why = "the caller is talking again"
        else:
            why = self._stays_awake(reply, kind)
        self._decide(why, text)

    def _decide(self, why, text) -> None:
        self.decisions.append(why or SENT)
        if why is not None:
            logger.info(f"GOODBYE: {quoted(text)} without {TOOL}, but {why} -- the room "
                        "stays awake")
            return
        self.sent = True
        logger.info(f"GOODBYE: {quoted(text)} heard out, and {TOOL} was not called -- "
                    "sending the room back to sleep (#221)")
        self._llm.create_task(call_client_tool(self._llm, TOOL, {}, extra={"said": True}),
                              "goodbye-sleep")

    def _not_a_goodbye(self, reply, heard, *, cut, audio, text):
        """(why the reply is none of the backstop's business, None) or (None, its kind):
        not a goodbye, or one the model's own call answers. Not logged."""
        if self.sent or self.holding:
            return "decided already", None
        if reply is None or reply.kind not in (COMMIT, POST):
            return "not a reply to the caller", None
        if cut or not audio or not (heard or "").strip() or not text:
            return "not heard out", None
        kind = goodbye_kind(text, i18n.for_espeak(self._lang_fn()))
        if kind is None:
            return "not a goodbye", None
        called, said = _turn(self._context.get_messages(), TOOL)
        if called:
            return f"its turn called {TOOL}", None     # its own call armed the bridge
        if said is None:
            return "the caller said nothing it answers", None   # a greeting, a reaction
        return None, kind

    def _stays_awake(self, reply, kind):
        """Why a goodbye heard out, in a turn that did not call the tool, does not send
        the room to sleep, or None. Logged."""
        called, said = _turn(self._context.get_messages(), TOOL)
        if called or said is None:
            return "the conversation went on"
        t = i18n.for_espeak(self._lang_fn())
        if kind != SLEEP and asks(said, t):
            return "it answers a question"
        if reply.calls:
            return "it called a tool, whose answer is still to come"
        gate = self._gate
        why = owed_reason(gate, self._owed, signed=signed_off(said, t))
        if why is not None:
            return why
        if gate.caller_request_pending or gate.turn_in_flight:
            return "the conversation went on"
        return None


# ------------------------------------------------------------------ the sleep phrase

# What the room says to "go to sleep" before it sleeps (SleepPhrase): plain, and the same
# in every language.
SLEEP_TEXT = N_("Okay, going to sleep.")


class SleepPhrase:
    """ "go to sleep" (the module note, THE SLEEP PHRASE), heard by the session's
    PhraseVoice (wifi_voice.py) while nothing else holds the caller's words. The room's
    only: agent_session builds it where the backstop is.

    Two ways to ask, one phrase: the request ("go to sleep", "stop listening":
    wifi_voice.request_heard), which wins over anything owed; and a turn that is only a
    goodbye ("Good night.", "Okay, bye!", "Buonanotte!": goodbye_kind, the backstop's
    own matcher, on the caller's words -- #221's option 1, #320), which stands down as
    the backstop does while an answer is still owed (owed_reason), its words then going
    to the model as any others. `gate` and `owed` (the session's FollowupGate and
    owed.Owed) are set by agent_session; without them nothing stands it down.

    `sent`: the room was sent to sleep (once per session); `decisions`, newest last: what
    became of each phrase heard -- SENT, or why not (the tests read it)."""

    def __init__(self):
        self.sent = False
        self._going: asyncio.Task | None = None   # the line said, the sleep to send
        self.decisions: collections.deque = collections.deque(maxlen=8)
        self.gate = self.owed = None
        # The caller has said something in this session (one per wake): "buenas noches"
        # as their first words is hello, after that goodbye (PATTERNS["greets"]).
        self._spoken = False

    def _asked(self, text: str, t: i18n.T) -> bool:
        """Is `text` the caller asking the room to sleep, and may it now? The request
        always; a goodbye of theirs only with nothing owed (logged when it may not)."""
        if wifi_voice.request_heard("sleep", text, t):
            return True
        if goodbye_kind(text, t, greets_part=self._spoken) is None:
            return False
        if self.sent or self._going is not None and not self._going.done():
            return True                            # on its way already
        why = (owed_reason(self.gate, self.owed, signed=True)
               if self.gate is not None and self.owed is not None else None)
        if why is not None:
            self.decisions.append(why)
            logger.info(f"SLEEP: {quoted(text)} is a goodbye, but {why} -- the model "
                        "has it")
            return False
        return True

    async def heard(self, voice, text: str) -> bool:
        asked = self._asked(text, voice.t)
        self._spoken = self._spoken or bool(text.strip())
        if self.sent:
            return asked                           # asked again: it is on its way
        going = self._going is not None and not self._going.done()
        if not asked:
            if going:
                # Words of theirs before the line ended: the conversation went on.
                self._going.cancel()
                self.decisions.append("the caller went on")
                logger.info("SLEEP: the caller went on before the room slept -- it stays "
                            "awake")
            return False
        if going:
            return True                            # asked again while the line plays
        logger.info(f"SLEEP: {quoted(text)} -- the room goes back to sleep, no model asked")
        starts = voice.bot_starts
        await voice.say(voice.t._(SLEEP_TEXT))
        self._going = voice._spawn(self._sleep(voice, starts))
        return True

    async def _sleep(self, voice, starts: int) -> None:
        """Once the line has played out, end_conversation on the bridge with "said": true
        (the goodbye has played: the bridge sleeps once its speaker is quiet,
        local_audio.Restart) -- not if the caller cut it."""
        if not await voice.heard_out(starts):
            self.decisions.append("the caller cut the line")
            logger.info("SLEEP: the caller cut the line -- the room stays awake")
            return
        self.sent = True
        self.decisions.append(SENT)
        logger.info(f"SLEEP: the line played out -- sending the room back to sleep ({TOOL})")
        await call_client_tool(voice, TOOL, {}, extra={"said": True})
