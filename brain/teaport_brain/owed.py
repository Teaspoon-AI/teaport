#
# owed.py — "owed until heard" (#203): what the caller is still owed, outside the
# conversation.
#
# An async consult (tools.ask_openclaw) answers later than the turn that asked for it.
# Until 2026-10 its answer was handed over by a one-shot USER-role trigger written into
# the context ("tell the user now ...") and retired once the caller had heard a third of
# the reply's WORDS. #203 broke that premise: 1.1 s of a lead-in that restated the
# question ("The top story on Hacker News right now ...") was 31% of the words and none
# of the information, the trigger was retired as "already given", and "I'm ready." was
# answered with a second 49-second lookup.
#
# The shape that replaced it has three concepts and is checked before it was written
# (brain/formal/Owed.tla, which LEADS this code; every action and row named below is
# there):
#
#   the RECORD   -- one per consult, and one per cut telling of a sync answer tool's
#                   result (#204: Owed._resolve, _cut_answer), kept HERE, never in the
#                   context: owed -> closed, or dropped (evicted unheard). Land.
#   the NOTE     -- rendered from the records into every request while one is owed
#                   (render(), called by services.BoundedOpenAILLMService._open_stream
#                   after HeardContextCorrector's reconcile), and never stored.
#                   NoteWhileOwed, NoStoredNote (ow_storeNote), ow_renderInGet.
#   the EVIDENCE -- the caller HEARD every item of the outcome: at least min(2, |terms|)
#                   of each item's terms in one reply's pts-EXACT heard text (the
#                   corpus premise, DESIGN §3), or, for an item with no terms, a run
#                   of ours that played to its end uncut. Only a reply whose request
#                   carried the note counts. ClosedOnEvidence; ow_fraction (today's
#                   1/3 of the words, #203), ow_countSettle, ow_singleWord, ow_oneItem,
#                   ow_ledgerMax, ow_asWritten_closed.
#
# The brain speaks unprompted at most twice per record: one RUN when the answer lands
# (unless a caller request is still to be rendered: that one carries the note), and
# one RE-OFFER at a long lull (REOFFER_QUIET_S) once the re-offer is armed -- by a
# caller turn, an audible cut, or any run that ended without closing the record (Owed.tla
# REOFFER = "anyMiss": ow_asWritten_live is what arming on a cut or a turn alone
# strands). The door both go through is FollowupGate.ask (followup_gate.py: the #198
# hold, a slot of their own beside the post-tool answer's, dropped at the cap, a window
# both VAD- and turn-level); where it renders (view) it is checked again, and a run
# whose records were heard meanwhile sends no request. OneReoffer, HoldNoLoss,
# NoRunUnderSpeech, NoRunMidTurn, NoRunAfterHeard, OwedResolves.
#
# The SETTLE FEED is each reply's own end, charted by the transcript ledger: at the
# interruption that cut it (TranscriptLedger._interrupt, ahead of the commit by
# CutReplyFence), or once its synthesis is done and all of its audio has played
# (_close_ready, at the window close its own last chunk played out in), with the
# pts-exact heard prefix, never the ledger's heard_text (the longer of that prefix and
# an audio-fraction estimate: ow_ledgerMax). Every reader of the records -- the render,
# the lull -- first waits for the replies still to settle (settled()): ow_lagSettle,
# ow_lagSettle_repeat.
#
# What the model and the measured replay rounds fixed in the texts below: DESIGN.md §2
# and "v3: corrections from Owed.tla" in the #203 design notes; RESULTS-round3..5.
#
import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext

from teaport_brain.common_words import COMMON
from teaport_brain.followup_gate import SYSTEM_NOTICE_TAG
from teaport_brain.heard_context import msg_text
from teaport_brain.settings import setting
from teaport_brain.transcript_ledger import _SPACELESS_SCRIPT
from teaport_brain.tts_text import _normalize_for_tts, has_speech, strip_urls_for_speech

# ------------------------------------------------------------------------- constants

# The brain's own unprompted requests (Owed.tla `Runs`): the landing run and the
# re-offer. Every other request -- a commit, a post-tool answer, a client note's
# reaction -- is "caller": the note goes before the caller's last words.
RUN, REOFFER = "run", "reoffer"
RUNS = frozenset({RUN, REOFFER})

OWED, CLOSED, DROPPED = "owed", "closed", "dropped"

# At most this many owed records render; a newer one evicts the oldest, which ends
# DROPPED -- not heard, its tool result kept, no note (DESIGN v3 #10: closing it would
# break ClosedOnEvidence).
MAX_RENDERED = 3

# How long a reader waits for replies that have ended but not yet settled (the ledger is
# an observer: it charts a reply's end within milliseconds of the frame). Past it the
# reader goes on with the record as it stands and says so; a stuck reply still settles
# when its end arrives (Owed.settled), which errs owed.
SETTLE_WAIT_S = 2.0

# The lull before the one re-offer (§4 (a), the user's choice 2026-10-16): the line has
# been quiet this long, with the caller's turn answered. setting(): brain.env.
REOFFER_QUIET_S = setting("TEAPORT_REOFFER_AFTER_QUIET_S")
_LULL_TICK_S = 0.25

# ---- the note's texts: DESIGN.md §2, each replayed (rounds 3-5, 20 trials each) -----
# The run line goes FIRST in the note (round 5 "C-lead-then-what": 20/20 "Tesla Roadster
# is the top story on Hacker News right now."). The re-offer line goes LAST: that is
# where rounds 4 and 5 measured it (spec_round4.note_with(run=RE_S), appended; 18-20/20
# "By the way, ..." after an unrelated exchange or "thanks", 10/20 stay_silent after
# "hold on"). §2's sketch lists both lines under the tag; the measured placement is kept.
RUN_LINE = ("Nothing else is going on: tell them now in one or two short spoken sentences. "
            "Lead with the answer, then say what it answers (\"<answer> is …\"), not "
            "the other way round.")
REOFFER_LINE = ("They haven't heard this yet and the conversation has gone quiet. If their "
                "last words asked you to wait and they haven't come back to you, call "
                "stay_silent. Otherwise mention it once in passing, in one short sentence "
                "that says what it's about and gives the answer (\"By the way, that ... is "
                "...\").")
NOTE_HEAD = "Results you have for the user. Each is everything that result contains."
NOT_HEARD = "They have NOT heard this yet."
# Round 4 "S3": 20/20 answer after a partial telling (v1's "heard part of it": 15/20).
PART_HEARD = "They heard only \"{quote}\" before they spoke, which is not the whole answer."
FOOTER = ("Say only what a result says. If they want something it doesn't include (a link, "
          "more detail, other items), say you don't have that and offer to look it up; never "
          "fill it in yourself, and never read out web addresses. Don't look up again what a "
          "result already answers. If they're busy with something else or asked you to "
          "wait, don't push it.")

# The re-offer's way to say nothing (round 5: without it the model re-offered 40/40
# after "hold on", whatever the wait clause said). Offered only in the re-offer run's
# tool list; services.BoundedOpenAILLMService.run_function_calls consumes the call, so
# neither it nor a result ever enters the context.
STAY_SILENT = "stay_silent"
STAY_SILENT_SCHEMA = FunctionSchema(
    name=STAY_SILENT, description="Say nothing this turn.", properties={}, required=[])

# What a consult that produced no answer is recorded as: the tool result's status and
# the record's outcome, in plain words the model can say (#80: each kind says what
# actually happened). A failure is ONE item with no terms (_record_items), so only a run
# of ours played uncut closes it (Owed.tla RUNCLOSE = "terms").
_ACTION_HEDGE = (" If it was something visible (a message, poll or post), it may have gone "
                 "through anyway: ask them to check rather than saying it failed.")


def failure_outcome(failure, detail="") -> tuple[str, str]:
    """(status, outcome) for a consult with no answer."""
    if failure == "timeout":
        return "timeout", "No answer: it ran out of time before it finished." + _ACTION_HEDGE
    if failure == "empty":
        return "empty", "No answer: it finished but came back empty." + _ACTION_HEDGE
    if failure == "error":
        # A whole-number 402, not the digits anywhere: the detail can be a CLI stderr
        # tail or a relay error carrying ids and timestamps, and a false "billing
        # problem" sends the caller off to fix the wrong thing.
        if re.search(r"\b402\b", detail or ""):
            return "error", ("No answer: it couldn't be done because of a billing or credit "
                             "problem with the AI service." + _ACTION_HEDGE)
        return "error", "No answer: it hit an error and couldn't be done." + _ACTION_HEDGE
    # "unknown", not "failed": a consult can die on teardown AFTER its action landed.
    return "unknown", ("No answer: it didn't report back, so it may or may not have been "
                       "done." + _ACTION_HEDGE)


# ------------------------------------------------------------------ the outcome, cleaned

_DETAILS = re.compile(r"<details\b.*?</details>", re.I | re.S)
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((?:[^)\s]+)\)")
# A bare bracketed address ("a link straight to [tesla.com/roadster]").
_BRACKET_ADDR = re.compile(r"\s*\[[^\]\s]*\.[a-z]{2,}[^\]\s]*\]", re.I)
# OpenClaw's trailing meta-notes: "*Recollect note:* per USER.md ...".
_META_NOTE = re.compile(r"^\s*[*_]{1,2}[^*_\n]{0,40}\bnote\s*:\s*[*_]{0,2}", re.I)
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+•·]|\d{1,3}[.)])[ \t]+")
_BULLET = "- "
# A label whose value was emptied (an address removed): "- URL:" with nothing after.
_EMPTY_LABEL = re.compile(r"^(?:- )?[\w ./&'-]{1,30}:\s*$")
_DANGLING = re.compile(r"[ \t]*[—–-][ \t]*$")


def clean_outcome(text: str) -> str:
    """The answer as the record keeps it, and the placeholder becomes: what a model told
    "Say only what a result says" may say aloud. Web addresses go (0/300 invented with
    the answer inline and cleaned, 10/20 by reference: DESIGN "What the evidence
    supports"; live 2026-09-04 21:59, a record answered with every path read aloud), and
    with them a label they emptied ("- URL:") and a dash they leave dangling ("**Tesla
    Roadster** — "); markdown; list numbering, which would clash with the note's own
    "1. They asked" (items become "- " lines); OpenClaw's <details> blocks and trailing
    italic meta-notes ("*Recollect note:* per USER.md ...", redteam-gist #6)."""
    text = _DETAILS.sub("", text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = strip_urls_for_speech(text)
    text = _BRACKET_ADDR.sub("", text)
    paras = re.split(r"\n[ \t]*\n", text.strip())
    while paras and _META_NOTE.match(paras[-1]):
        paras.pop()
    lines = []
    for raw in "\n\n".join(paras).split("\n"):
        line = raw.replace("**", "").replace("__", "").replace("`", "")
        line = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", line)
        line = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", line)
        if _LIST_ITEM.match(line):
            line = _BULLET + _LIST_ITEM.sub("", line)
        line = _DANGLING.sub("", line.rstrip())
        if _EMPTY_LABEL.match(line.strip()) and line.strip():
            continue
        lines.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


def split_items(outcome: str) -> list:
    """The outcome's items: its list items when it is a list (two or more), else the
    whole text as one. A five-story result is five items, so naming one story does not
    close it (DESIGN §3; Owed.tla ow_oneItem)."""
    items = [ln[len(_BULLET):].strip() for ln in outcome.splitlines()
             if ln.startswith(_BULLET) and ln[len(_BULLET):].strip()]
    return items if len(items) >= 2 else [outcome]


# ------------------------------------------------------------------ the spoken form
#
# The outcome and every reply are compared in SPOKEN form: through the TTS normaliser
# (tts_text._normalize_for_tts, the text the engine is handed), web addresses out,
# case-folded, split on word boundaries, a script without spaces one character per
# token. A number is ONE token whichever way it is written: "1,255" and "twelve hundred
# fifty-five" both read "1255" (the persona has the model spell numbers out; the
# outcome carries digits). The normaliser does not spell numbers, so the fold is here;
# a number spelled two ways ("twenty twenty-eight" against "2028") reads as two
# different tokens, which errs owed. A leading zero is not said ("03:45"), and a unit
# written onto a number is a word of its own ("72F", "9pm").
#
# A word with an apostrophe is one token. A clitic contraction ("you'll", "they're",
# "I've", "don't", "I'd", "I'm") is a function word, never a term: split on the
# apostrophe, its "ll" or "re" was a term every "I'll" in a reply matched (the #204
# review: "They're open until nine ..." closed on "Sorry, they're still checking --
# you'll hear back"). A possessive or "is" ("Joy's", "it's") is its word. A name
# ("O'Brien") stays whole.

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*(?:[.,]\d+)*")
_CLITIC = re.compile(r"^(.+?)['’](s|ll|ve|re|d|m|t)$")
_THOUSANDS = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_UNIT = re.compile(r"^(\d+)([a-z]*)$")
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}


def _parse_number(tokens: list, i: int) -> tuple[int, Optional[int]]:
    """The number spelled from tokens[i:], as (end, value); (i, None) if none starts
    there. A lone "one" is a word ("the one with ..."), not 1."""
    total = current = 0
    last = None
    j, n = i, len(tokens)
    while j < n:
        t = tokens[j]
        if t in _UNITS:
            v = _UNITS[t]
            if last == "unit" or (last == "ten" and v >= 10):
                break
            current += v
            last = "unit"
        elif t in _TENS:
            if last in ("unit", "ten"):
                break
            current += _TENS[t]
            last = "ten"
        elif t == "hundred":
            if last not in ("unit", "ten") or current == 0 or current >= 100:
                break
            current *= 100
            last = "hundred"
        elif t in _SCALES:
            if current == 0:
                break
            total += current * _SCALES[t]
            current = 0
            last = "scale"
        elif (t == "and" and last in ("hundred", "scale") and j + 1 < n
              and (tokens[j + 1] in _UNITS or tokens[j + 1] in _TENS)):
            j += 1
            continue
        else:
            break
        j += 1
    if j == i or (j == i + 1 and tokens[i] == "one"):
        return i, None
    return j, total + current


def spoken_tokens(text: str) -> list:
    """`text` in spoken form, as a list of tokens (see the note above)."""
    text = strip_urls_for_speech(_normalize_for_tts(text or "")).casefold()
    raw = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if _SPACELESS_SCRIPT.search(tok):
            # One token per character of a script written without spaces; a Latin or
            # digit run inside it stays whole.
            for part in re.findall(r"[a-z0-9]+|.", tok):
                raw.append(part)
            continue
        clitic = _CLITIC.match(tok)
        if clitic and clitic.group(2) == "s":
            tok = clitic.group(1)
        if _THOUSANDS.match(tok):
            tok = tok.replace(",", "")
        unit = _UNIT.match(tok)
        if unit:                            # "72F", "9pm": the number, then its unit
            raw.extend([str(int(unit.group(1))), unit.group(2)] if unit.group(2) else
                       [str(int(unit.group(1)))])   # "03:45" is said "three forty-five"
            continue
        raw.append(tok)
    out, i = [], 0
    while i < len(raw):
        j, value = _parse_number(raw, i)
        if value is not None:
            out.append(str(value))
            i = j
        else:
            out.append(raw[i])
            i += 1
    return out


_INFLECTIONS = (("s", ""), ("es", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"),
                ("ly", ""))


def _forms(tok: str) -> set:
    """`tok` and what a regular inflection of it folds back to ("stores" -> "store")."""
    out = {tok}
    for suffix, back in _INFLECTIONS:
        if tok.endswith(suffix) and len(tok) > len(suffix) + 2:
            out.add(tok[: -len(suffix)] + back)
    return out


def _is_common(tok: str) -> bool:
    if (len(tok) == 1 and tok.isascii() and tok.isalpha()) or _CLITIC.match(tok):
        return True
    # A regular inflection of a listed word counts ("stories" is listed; "answering" is not).
    return not _forms(tok).isdisjoint(COMMON)


def terms_of(item: str, request: str) -> frozenset:
    """An item's terms: its spoken tokens outside the common-word list, minus the
    request's own tokens, a regular inflection of one included (a lead-in that restates
    the question names none: #203; "a Toy Joy store" against "any toy stores", #204)."""
    asked = set().union(*(_forms(t) for t in spoken_tokens(request)))
    return frozenset(t for t in spoken_tokens(item)
                     if _forms(t).isdisjoint(asked) and not _is_common(t))


def item_heard(terms: frozenset, heard: set) -> bool:
    """At least min(2, |terms|) of an item's terms, as whole tokens, in what was heard.
    "present" does not contain the token "sent"; one common word cannot close anything
    (it is no term)."""
    return bool(terms) and len(terms & heard) >= min(2, len(terms))


# ------------------------------------------------------------------------- the record

@dataclass
class Record:
    """One answer, owed to the caller until they hear it (DESIGN §1): a consult's
    outcome, or a cut telling of a sync answer tool's result (#204)."""
    key: str                      # its first tool_call_id
    request: str                  # the model's request, or the caller's words (sync)
    outcome: str                  # cleaned: the answer, or a failure's plain record
    failure: Optional[str] = None
    items: list = field(default_factory=list)       # [(text, terms)]
    state: str = OWED
    heard_quote: str = ""         # what a cut reply carrying the note got through
    reoffers: int = 0             # counted at the ask: a held-then-dropped one is one
    missed: bool = False          # the one re-offer is armed (Owed.tla `missed`)
    ids: tuple = ()               # the tool_call_ids it answers: the consult's, a round's

    @classmethod
    def make(cls, key, request, outcome, failure=None, ids=(), **kw) -> "Record":
        rec = cls(key=key, request=request, outcome=outcome, failure=failure,
                  ids=tuple(ids) or (key,), **kw)
        rec.items = _record_items(outcome, request, failure)
        return rec

    def closes_on(self, heard_text: Optional[str], *, whole: bool, run: bool) -> bool:
        """The close rule, on one reply that carried the note: every item heard -- an
        item with terms by its terms in the reply's pts-exact heard text; one without (a
        failure, a bare "done") when the reply was heard to its end (`whole`: it played
        through with audio, or was cut after its last word) and was a run of ours or is
        about this record (Owed.tla Closes, RUNCLOSE = "about": the failure the caller's
        own turn told in full is not re-offered; an unrelated reply shares nothing with
        the request). `heard_text` None: no pts-exact prefix (the reply was cut and its
        words carried no pts), which closes nothing."""
        heard = set(spoken_tokens(heard_text)) if heard_text else set()
        for _, terms in self.items:
            if terms:
                if not item_heard(terms, heard):
                    return False
            elif not (whole and heard_text and (run or self.about(heard_text))):
                return False
        return True

    def about(self, heard_text: str) -> bool:
        """Whether a reply's words are about this record: a non-common spoken token
        shared with its request or its terms."""
        toks = {t for t in spoken_tokens(heard_text) if not _is_common(t)}
        about = set(spoken_tokens(self.request)) | {t for _, ts in self.items for t in ts}
        return bool(toks & about)

    def to_saved(self) -> dict:
        return {"key": self.key, "request": self.request, "outcome": self.outcome,
                "failure": self.failure, "heard_quote": self.heard_quote,
                "reoffers": self.reoffers, "ids": list(self.ids)}


def _record_items(outcome, request, failure) -> list:
    if failure:
        return [(outcome, frozenset())]
    return [(item, terms_of(item, request)) for item in split_items(outcome)]


# ------------------------------------------------------------------------- the note

def is_caller_message(m) -> bool:
    """A message the caller said: a user message that is not one of the brain's own --
    a tagged notice or app event ("[automated system notice ...]", "[app event ...]") or
    a stage direction ("(The call/session just connected. ...)", the wake and resume
    cues)."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    text = msg_text(m).strip()
    return bool(text) and not text.startswith("[") and not (
        text.startswith("(") and text.endswith(")"))


def note_text(records: list, purpose: str) -> str:
    """The note for `records` (oldest first), as the replay rounds measured it. The
    re-offer's says NOT heard whatever was quoted: with the lead-in quoted it stayed
    silent after "hold on" 9/40 against 18/40 (the confirmation round, RESULTS-confirm):
    the quote contradicts its line and reads as engagement."""
    lines = [SYSTEM_NOTICE_TAG]
    if purpose == RUN:
        lines.append(RUN_LINE)
    lines.append(NOTE_HEAD)
    for i, r in enumerate(records, 1):
        status = (PART_HEARD.format(quote=r.heard_quote)
                  if r.heard_quote and purpose != REOFFER else NOT_HEARD)
        lines.append(f"{i}. They asked: \"{r.request}\"\n   Result: {r.outcome}\n   {status}")
    lines.append(FOOTER)
    if purpose == REOFFER:
        lines.append(REOFFER_LINE)
    return "\n".join(lines)


def render(messages: list, records: list, purpose: str) -> list:
    """The request's messages: `messages` (never modified) with the note for `records`
    -- one tagged user message, immediately before the last CALLER message for any
    request but ours (not before a notice or a wake cue: the caller's words stay what is
    answered), and at the tail for a run of ours, which it is the turn of. No records,
    no note. Pure: the replay harness calls it as the code does.

    `purpose`: "run" | "reoffer" for a run of ours; anything else is a caller's request
    ("caller", or a Reply's kind: COMMIT, POST)."""
    msgs = list(messages)
    if not records:
        return msgs
    note = {"role": "user", "content": note_text(records, purpose)}
    if purpose in RUNS:
        return msgs + [note]
    at = next((i for i in range(len(msgs) - 1, -1, -1) if is_caller_message(msgs[i])), None)
    if at is None:
        # No caller words at all (a greeting cue is the turn): before the tail.
        at = max(0, len(msgs) - 1)
    return msgs[:at] + [note] + msgs[at:]


# ------------------------------------------------------------- a sync tool's answer
#
# A sync ANSWER tool's result (web_search, web_fetch, ...: tools.Tool.answers; an action's
# confirmation is never owed) is answered in the reply after it, and a telling of it cut
# before the caller heard it is owed like a consult (#204). How it is filed is
# formal/OwedAnswers.tla, by POSITION only (four review rounds of reading a reply's
# words for a telling missed tellings and took the wrong ones):
#
#   - the book files each result the first time a render sees it (Owed.noted), to the
#     caller's words that started its round ("" if a notice or a cue did: no record);
#   - its first reply, of whatever kind, settles it if it calls nothing and plays audio:
#     heard in full, told; cut after its stream ended, the record (Owed._resolve);
#   - a reply that did not (no audio, cut mid-stream, a line before a call) leaves it
#     one more; then it is history, as before #204 -- and a new round ends it at once;
#   - a round is an owed record's re-lookup, no answer of its own, only in the caller's
#     turn right after that record's cut, with words that carry no terms of their own
#     ("Hold on, hold on." and web_fetch, 2026-10-18 17:26).

# pipecat's placeholders while a call runs or after it was cancelled: no answer.
_NOT_A_RESULT = frozenset({"IN_PROGRESS", "CANCELLED", "COMPLETED"})


def new_results(messages: list, known) -> list:
    """The tool results in `messages` whose tool_call_ids are not `known`, as (id, tool
    name, the words that started its round), oldest first. Every result in a session is
    first seen by the render right after it lands, so the unknown ones are the new ones,
    wherever pipecat put the line before their call (a dict lookup per message)."""
    tools = {c.get("id"): (c.get("function") or {}).get("name")
             for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
             for c in m.get("tool_calls") or () if isinstance(c, dict)}
    return [(m.get("tool_call_id"), tools.get(m.get("tool_call_id")), _asked(messages, i))
            for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "tool"
            and m.get("tool_call_id") not in known and msg_text(m).strip() not in _NOT_A_RESULT]


def _newest_result(messages: list) -> Optional[str]:
    """The newest tool result's id in `messages`, None if there is none."""
    return next((m.get("tool_call_id") for m in reversed(messages)
                 if isinstance(m, dict) and m.get("role") == "tool"), None)


def tool_call_ids(messages: list) -> set:
    """Every tool result's id in `messages` (a conversation continued after the room's
    sleep: its results are history)."""
    return {m.get("tool_call_id") for m in messages
            if isinstance(m, dict) and m.get("role") == "tool"}


def _asked(messages: list, i: int) -> str:
    """The words that asked for the tool round the result at `i` belongs to: the latest
    user message before it, if the caller said it -- "" for a round a notice or a wake
    cue started (a client note's reaction, a greeting), which nobody asked."""
    for m in reversed(messages[:i]):
        if isinstance(m, dict) and m.get("role") == "user":
            return msg_text(m).strip() if is_caller_message(m) else ""
    return ""


@dataclass
class _Answer:
    """A tool result as the book holds it (Owed._answers)."""
    request: str                # the caller's words that started its round ("" nobody's)
    replies: int = 0            # replies left that may settle it (0: settled, or history)
    record: Optional[str] = None    # the key of the record made of it


# ------------------------------------------------------------------------- the replies

COMMIT, POST = "commit", "post"


class Reply:
    """One request to the LLM and the reply it makes, from the render to its settle.
    Created by the LLM service for every request (services.py); the render fills in what
    its request carried -- the note's record keys (`noted`; for an adopted speculation,
    from its stream) and the sync answers it may settle (`fresh`, their
    tool_call_ids) -- and the ledger settles it at the reply's own end.

    `kind`: what asked for it -- COMMIT (a context frame on its way down: a caller's
    commit, mostly), POST (one pushed up: the answer to a sync tool's result), or a run
    of ours (RUN, REOFFER, for the records `keys`). Every kind but a run's is "caller" to
    the render."""

    __slots__ = ("kind", "keys", "noted", "fresh", "relook", "turn", "settled", "claimed",
                 "texts", "calls")

    def __init__(self, kind: str, keys=()):
        self.kind = kind
        self.keys = tuple(keys)       # a run's records
        self.noted: tuple = ()
        self.fresh: tuple = ()          # the sync answers it may settle (Owed._resolve)
        self.relook: Optional[str] = None   # the record its calls would re-look up
        self.turn = 0                   # the caller turns rendered by its render
        self.settled = False
        self.claimed = False          # the ledger has its LLMFullResponseStartFrame
        self.texts = 0                # LLMTextFrames with text the completion pushed
        self.calls = 0                # function calls it dispatched (stay_silent not one)


# ------------------------------------------------------------------------- the book

class Owed:
    """The session's owed records, and every action of Owed.tla's `rec` on them.

    tools.ask_openclaw registers each consult (dispatch) and lands its outcome (land).
    The LLM service asks for each request's view (view) and registers the reply that view
    carried (noted); the ledger settles replies (settle). `gate`, a FollowupGate, is the
    door the runs go through: the book asks it (ask), and it reports caller turns, runs
    dropped at its hold cap and the session's end through its callbacks. The lull is the
    book's own task."""

    def __init__(self, context, gate=None, *, answer_tools=frozenset(),
                 reoffer_quiet_s: float = None):
        self.context = context
        self.gate = gate
        # The tools whose result is an answer (tools.ANSWER_TOOLS): a cut telling of one
        # is owed (Owed._resolve). An action's confirmation never is.
        self.answer_tools = frozenset(answer_tools)
        self._reoffer_quiet_s = REOFFER_QUIET_S if reoffer_quiet_s is None else reoffer_quiet_s
        self._records: dict = {}              # key -> Record, landing order
        self._running: dict = {}              # request -> key: consults not landed yet
        self._duplicates: dict = {}           # key -> the key whose consult answers it
        # Every tool result the book has seen, by tool_call_id (_Answer): a sync
        # answer's, open for at most two replies; a consult's, and a
        # re-lookup's put out under the note, its record's key from the start.
        self._answers: dict = {}
        self._relook: Optional[str] = None    # the record cut last: its re-lookup turn
        self._turn = 0                        # caller turns rendered (COMMIT renders)
        # Replies the readers wait for: rendered with the note, or with a fresh answer
        # whose cut makes a record, and not settled yet.
        self._pending: list = []
        self._settled = asyncio.Event()
        self._settled.set()
        self._lull = None
        self._closed = False
        if gate is not None:
            gate.on_caller_turn = self.caller_turn
            gate.on_run_dropped = self.run_dropped
            gate.on_session_end = self.close_session

    # ---- what renders --------------------------------------------------------------

    def owed(self) -> list:
        return [r for r in self._records.values() if r.state == OWED]

    def record(self, key) -> Optional[Record]:
        return self._records.get(key)

    def is_settled(self) -> bool:
        """No reply that carried the note is still to settle."""
        return self._settled.is_set()

    async def settled(self, timeout: float = SETTLE_WAIT_S) -> None:
        """Until every pending reply has settled (Owed.tla: `fed = none` before Issue,
        RunGo and Lull). Bounded: past SETTLE_WAIT_S the readers go on without the
        replies still pending, and one lost reply does not hold every later render (the
        accepted residual, formal/README.md). It errs owed: a reply the ledger has
        (claimed) still settles when its end arrives -- a cut sync answer still makes its
        record, a telling still closes -- and only one the ledger never saw (its
        LLMFullResponseStartFrame flushed by an interruption: never played) is settled
        here, as heard nothing."""
        if self._settled.is_set():
            return
        try:
            await asyncio.wait_for(self._settled.wait(), timeout)
        except asyncio.TimeoutError:
            stuck = list(self._pending)
            self._unblock(stuck)
            for r in stuck:
                if not r.claimed:
                    self.settle(r, None, cut=True, audio=False)
            logger.warning(f"OWED: {len(stuck)} repl(ies) unsettled after {timeout:.1f}s; "
                           "going on without them (each still settles at its end)")

    def _unblock(self, replies) -> None:
        """The readers stop waiting for `replies` (each still settles at its end)."""
        for r in replies:
            if r in self._pending:
                self._pending.remove(r)
        if not self._pending:
            self._settled.set()

    def called(self, reply: Optional[Reply], ids=()) -> None:
        """`reply` put the calls `ids` out (services.run_function_calls). Unless its
        request carried the note, nothing waits for it: the round's answer is a request
        of its own, after it, and a cut of it cancels the call. If it is the caller's turn
        right after a record's cut, in words with no terms of their own (`reply.relook`,
        Owed.noted), the round is that record's re-lookup: its results are no answers of
        their own."""
        if reply is None:
            return
        for tid in ids if reply.relook else ():
            self._answers[tid] = _Answer("", record=reply.relook)
        if not reply.noted:
            self._unblock([reply])

    async def view(self, context: LLMContext, purpose: str, run_keys=()):
        """The request `context` becomes, and the record keys its note carries: the
        render hook (services.BoundedOpenAILLMService._open_stream), after every pending
        settle. (context, ()) when nothing is owed; (None, ()) for a run none of whose
        records is owed any more -- it renders nothing and sends no request. This is
        where a run is checked again (Owed.tla RunGo; ow_askOnly): the gate lets it go
        only into a quiet window, so nothing lies between its go and this render."""
        await self.settled()
        records = self.owed()
        if purpose in RUNS and not any(r.key in run_keys for r in records):
            return None, ()
        if not records:
            return context, ()
        msgs = render(context.get_messages(), records, purpose)
        tools = context.tools
        if purpose == REOFFER:
            tools = _with_stay_silent(tools)
        return (LLMContext(msgs, tools=tools, tool_choice=context.tool_choice),
                tuple(r.key for r in records))

    def noted(self, reply: Optional[Reply], keys, messages=()) -> None:
        """`reply`'s request, built from `messages`, carried the note for `keys`: it is
        evidence when it ends. Each tool result in `messages` the book has not seen is
        filed here (new_results) -- an answer tool's open for its two replies, to the
        words that started its round -- and a new round ends any open answer to other
        words, as does a caller's turn in words of their own. A request rendered while
        answers are open may settle them (`fresh`): it is waited for too, note or not,
        since a cut telling of them is a record the next render must carry
        (OwedAnswers.tla). Words "of their own" carry a term outside the common words
        (terms_of): the one test the book reads words by."""
        if reply is None or reply.settled or reply in self._pending:
            return
        reply.noted = tuple(keys)
        if reply.kind not in RUNS:
            if reply.kind == COMMIT:
                self._turn += 1
                words = _asked(messages, len(messages))
                # The caller moves on (OwedAnswers.tla ownWordsKeep): an answer carried
                # already is ended by words of their own beyond ITS question ("Wait,
                # Austin tomorrow?" restates it); one new at this render stays its own.
                for a in self._answers.values():
                    if a.replies and terms_of(words, a.request):
                        a.replies = 0
                if words and self._relook is not None and not terms_of(words, ""):
                    # The turn right after a record's cut, in words that ask nothing of
                    # their own: its round is that record's re-lookup. One turn only.
                    reply.relook = self._relook
                self._relook = None
            elif reply.kind == POST and (last := _newest_result(messages)) is not None:
                # A re-lookup round's own reply passes it on to a call it chains.
                a = self._answers.get(last)
                if a is not None and a.record is not None and not a.replies:
                    reply.relook = a.record
            reply.turn = self._turn
            for tid, tool, words in new_results(messages, self._answers):
                for a in self._answers.values():
                    if a.replies and a.request != words:
                        a.replies = 0
                self._answers[tid] = _Answer(words, 2 if tool in self.answer_tools else 0)
            reply.fresh = tuple(t for t, a in self._answers.items() if a.replies)
        if reply.noted or reply.fresh:
            self._pending.append(reply)
            self._settled.clear()

    # ---- Land ---------------------------------------------------------------------

    def dispatch(self, key: str, request: str) -> Optional[str]:
        """A consult is asked for (tools.ask_openclaw). None: it runs, and its placeholder
        is in the context until land() rewrites it or the session ends (close_session).
        Otherwise the key of the consult already running for this exact request: gpt-oss
        sometimes asks the same thing twice a few seconds apart, and that one's outcome
        answers both (land() rewrites this placeholder to point at it)."""
        running = self._running.get(request)
        self._answers[key] = _Answer(request, record=running or key)
        if running is not None:
            self._duplicates[key] = running
            return running
        self._running[request] = key
        return None

    def land(self, request, text, key, *, failure=None, detail="", ask=True) -> Record:
        """The consult landed: rewrite its placeholder ONCE to the cleaned outcome (the
        only context write delivery makes), make the record owed, and ask the landing run
        -- unless a caller request is still to be rendered (their turn is open, or
        committed and not yet asked): that request carries the note, and a run would
        speak before or over it (Owed.tla Land; DESIGN v3 #7). Skipped, the run's place
        is taken by the re-offer, armed."""
        if self._running.get(request) == key:
            del self._running[request]
        outcome = clean_outcome(text) if text else None
        status = "complete"
        if not outcome:
            # An answer that cleans to nothing (only an address, say) came back empty.
            status, outcome = failure_outcome(failure if text is None else "empty", detail)
            failure = status
        rewrote = self._rewrite(key, {"status": "complete", "answer": outcome} if not failure
                                else {"status": status, "error": outcome})
        for dup in [d for d, of in self._duplicates.items() if of == key]:
            self._rewrite(dup, {"status": "duplicate",
                                "note": "The same request as an earlier call; its result "
                                        "is that call's."})
            del self._duplicates[dup]
        rec = self._owe(Record.make(key, request, outcome, failure=failure),
                        f"landed ({failure or 'answer'}); tool result "
                        f"{'rewritten' if rewrote else 'not found'}")
        if not ask or self._closed or self.gate is None:
            return rec
        if self.gate.caller_request_pending:
            logger.info(f"OWED: {key} lands with a caller request still to be rendered "
                        "-- it carries the note; the re-offer is armed instead of a run")
            self._arm([rec])
        else:
            self.gate.ask([key], RUN, self.context)
        return rec

    def _rewrite(self, key, result: dict) -> bool:
        for m in self.context.get_messages():
            if (isinstance(m, dict) and m.get("role") == "tool"
                    and m.get("tool_call_id") == key):
                m["content"] = json.dumps(result)
                return True
        return False

    def _owe(self, rec: Record, how: str) -> Record:
        """`rec` is owed, the newest: every way a record is made (land, a cut sync
        answer, a wake's restore) goes through here."""
        self._records.pop(rec.key, None)
        self._records[rec.key] = rec
        self._evict()
        logger.info(f"OWED: {rec.key} {how} -- {len(rec.items)} item(s), terms "
                    f"{[sorted(t) for _, t in rec.items]}")
        return rec

    def _evict(self) -> None:
        owed = self.owed()
        for rec in owed[: max(0, len(owed) - MAX_RENDERED)]:
            rec.state = DROPPED
            logger.info(f"OWED: {rec.key} dropped unheard -- {MAX_RENDERED} newer answers "
                        "are owed (its tool result stays)")

    # ---- the re-offer --------------------------------------------------------------

    def run_dropped(self, keys) -> None:
        """The gate dropped a run at its hold cap (room noise): a run that ended without
        closing arms the re-offer (REOFFER = "anyMiss"; Owed.tla Cap)."""
        self._arm([r for k in keys if (r := self._records.get(k)) is not None and r.state == OWED])

    def caller_turn(self) -> None:
        """A caller turn started (Owed.tla TurnStart): it arms the re-offer of every owed
        record. The gate drops the run waiting at its door; the turn's own request
        carries the note."""
        self._arm(self.owed())

    def _arm(self, records) -> None:
        for rec in records:
            rec.missed = True
        if records and not self._closed and self.gate is not None \
                and (self._lull is None or self._lull.done()):
            try:
                self._lull = asyncio.get_running_loop().create_task(self._lull_loop())
            except RuntimeError:
                self._lull = None

    def _due(self) -> list:
        return [r for r in self.owed() if r.missed and r.reoffers == 0]

    async def _lull_loop(self) -> None:
        """Owed.tla Lull: an owed record whose re-offer is armed and unspent, every
        reply settled, and the line quiet (FollowupGate.quiet_for: no caller speech
        pending, no turn, no request or reply, nothing waiting at the door) for
        REOFFER_QUIET_S. One re-offer per record, ever, counted at the ask: OneReoffer."""
        try:
            while self._due():
                await asyncio.sleep(_LULL_TICK_S)
                if self.is_settled() and self.gate.quiet_for() >= self._reoffer_quiet_s:
                    due = self._due()
                    for r in due:
                        r.missed = False
                        r.reoffers += 1
                    if due:
                        logger.info(f"OWED: {self._reoffer_quiet_s:g}s of quiet with "
                                    f"{[r.key for r in due]} unheard -- the one re-offer")
                        self.gate.ask([r.key for r in due], REOFFER, self.context)
        except asyncio.CancelledError:
            pass

    # ---- Settle -------------------------------------------------------------------

    def settle(self, reply: Optional[Reply], heard: Optional[str], *, cut: bool,
               audio: bool, text: Optional[str] = None) -> None:
        """A reply ended (the settle feed: TranscriptLedger, at the interruption that cut
        it or once all of its audio has played). `heard`: its pts-exact heard text -- all
        of it when it played through, None when cut with no per-word pts; `text`: all of
        it, when its stream ended (None when cut before that). Applies the close rule to
        every record its request carried the note for (Owed.tla Finish -> Settle), and
        files the sync answers it may settle (_resolve)."""
        if reply is None or reply.settled:
            return
        reply.settled = True
        self._unblock([reply])
        run = reply.kind in RUNS
        missed = []
        # A reply rendered with a fresh answer is evidence for the record made of it
        # since (a late settle's: OwedAnswers.tla lateNoEvidence).
        made = {r for t in reply.fresh if (r := self._answers[t].record) is not None}
        for key in reply.noted + tuple(k for k in made if k not in reply.noted):
            rec = self._records.get(key)
            if rec is None or rec.state != OWED:
                continue
            if rec.closes_on(heard, whole=audio and not cut, run=run):
                rec.state = CLOSED
                rec.missed = False
                logger.info(f"OWED: {key} closed -- heard by the {reply.kind} reply "
                            f"({'cut' if cut else 'played through'}): {(heard or '')[:80]!r}")
                continue
            # Quoted only when about the record: the #203 session would otherwise have
            # quoted a clarifying question as the answer (test_owed_corpus.py).
            if cut and heard and heard.strip() and (run or rec.about(heard)):
                rec.heard_quote = heard.strip()
            if run or (cut and audio):
                # A run that ended without closing (no audio, partial) and an audible cut
                # both arm the re-offer: REOFFER = "anyMiss".
                missed.append(rec)
            logger.info(f"OWED: {key} still owed after the {reply.kind} reply "
                        f"({'cut' if cut else 'played through' if audio else 'no audio'}; "
                        f"heard {(heard or '')[:60]!r})")
        self._arm(missed)
        self._resolve(reply, heard, cut=cut, audio=audio, text=text)

    def _resolve(self, reply: Reply, heard: Optional[str], *, cut: bool, audio: bool,
                 text: Optional[str]) -> None:
        """The sync answers `reply` may settle, as the LIVE book holds them now (a late
        settle reads them after other replies may have: OwedAnswers.tla lateReplace).
        By position only: a reply that calls nothing, played audio and says something
        of its own settles them -- heard in full, told; cut after its stream ended, the
        record (_cut_answer). Any other reply -- no audio, a line before another call,
        nothing of its own ("Sure, take your time.": OwedAnswers.tla fillerSettles), a
        telling cut before its stream ended (a fragment: partialRecord; the residual
        streamCut, logged so it can be measured) -- spends one of their replies."""
        ids = [t for t in reply.fresh if self._answers[t].replies]
        if not ids:
            return
        request = self._answers[ids[-1]].request
        if (not audio or reply.calls or (cut and not text)
                or not terms_of(text or "", request)):
            if audio and cut and not text and not reply.calls:
                logger.info(f"OWED: a reply carrying {ids} cut before its stream ended "
                            f"(audio played, heard {(heard or '')[:60]!r}) -- no record "
                            "(STREAMCUT)")
            for t in ids:
                self._answers[t].replies -= 1
            return
        rec = self._cut_answer(tuple(ids), request, text, heard) if cut else None
        if rec is not None and reply.turn == self._turn:
            # No caller turn has rendered since the cut (a late settle opens none).
            self._relook = rec.key
        for t in ids:
            self._answers[t].replies = 0
            self._answers[t].record = rec.key if rec is not None else None

    def _cut_answer(self, ids: tuple, request: str, text: str,
                    heard: Optional[str]) -> Optional[Record]:
        """A telling of the sync answers `ids`, cut before it was heard (#204;
        formal/OwedAnswers.tla End, LateSettle): its whole text is owed, to the caller's words that
        asked for it -- not the tool's query (the replay: 19/20 wait at "hold on" with the
        caller's words, 15/20 with the query) -- keyed by the round's tool_call_ids. The
        close rule on the cut's own heard prefix first: a cut after the answer was heard
        makes nothing; so does a round nobody asked for. No run: the caller's turn that
        cut it carries the note; the cut arms the one re-offer. None when nothing is owed
        (the answer is then told)."""
        outcome = clean_outcome(text)
        if not request or not has_speech(outcome):
            return None
        rec = Record.make(ids[0], request, outcome, ids=ids, heard_quote=(heard or "").strip())
        if rec.closes_on(heard, whole=False, run=False):
            logger.info(f"OWED: a sync answer cut after it was heard: {(heard or '')[:60]!r}")
            return None
        self._owe(rec, f"a sync answer to {request[:60]!r} cut at {(heard or '')[:60]!r}")
        self._arm([rec])
        return rec

    # ---- the session's end, and the room's sleep -----------------------------------

    def close_session(self) -> None:
        """The session is over (the gate's cleanup; the room's sleep saves the records
        first, wake_gate). A consult still running cannot report back to it: its
        placeholder becomes its failure record, so no "will arrive later" outlives the
        session, and the record is owed in a conversation that is continued. Idempotent."""
        self._closed = True
        for request, key in list(self._running.items()):
            self.land(request, None, key, failure="unknown")
        lull, self._lull = self._lull, None
        if lull is not None and not lull.done():
            lull.cancel()

    def snapshot(self) -> list:
        """The owed records, for a conversation kept across the room's sleep."""
        return [r.to_saved() for r in self.owed()]

    def restore(self, saved: list) -> None:
        """Records saved with a conversation that is continued (wake_gate.WakeGate)."""
        for d in saved or ():
            rec = self._owe(Record.make(d["key"], d["request"], d["outcome"],
                                        failure=d.get("failure"), ids=d.get("ids") or (),
                                        heard_quote=d.get("heard_quote", ""),
                                        reoffers=d.get("reoffers", 0)),
                            "restored with the conversation, unheard")
            for t in rec.ids:
                self._answers[t] = _Answer(rec.request, record=rec.key)
        for t in tool_call_ids(self.context.get_messages()):
            self._answers.setdefault(t, _Answer(""))


def _with_stay_silent(tools):
    std = list(getattr(tools, "standard_tools", None) or [])
    custom = getattr(tools, "custom_tools", None)
    if any(getattr(t, "name", None) == STAY_SILENT for t in std):
        return tools
    return ToolsSchema(standard_tools=std + [STAY_SILENT_SCHEMA], custom_tools=custom)
