#
# llm_error_speaker.py — say something when the model call fails.
#
# An LLM failure currently reaches the journal as an ErrorFrame and reaches the caller
# as nothing at all. On a voice call that is indistinguishable from being ignored, and
# it has now cost three debugging sessions: a 402 that silenced nine turns while the key
# still had credit (2026-08-18), and a stalled request that hung a turn until the
# operator tore the session down (2026-08-19). In both the logs held the answer and the
# room held silence.
#
# A timeout (services._llm_timeout) bounds how long the silence lasts. It does not make
# the silence mean anything — that is this processor's job.
#
# PLACEMENT — this sits immediately BEFORE the LLM service, not after it.
# push_error() ends in `push_frame(error, FrameDirection.UPSTREAM)`, so an ErrorFrame
# raised by the LLM travels towards the transport input and terminates at PipelineTask.
# Downstream of the LLM this processor saw nothing at all: the 402 above still played as
# silence, and the only errors that DID reach it were the ones the TTS pushed up past it,
# which it then announced as an LLM failure — into the very TTS that had just failed.
# Upstream of the LLM, the LLM's errors pass through here on their way to the task.
#
# Attribution is explicit rather than positional. ErrorFrame carries `.processor` (set by
# push_error to the originator), so a TTS or transport error travelling up past this point
# is forwarded untouched instead of being blamed on the model. An ErrorFrame with no
# attribution is left alone for the same reason — guessing wrong is worse than silence
# from a processor whose whole job is to explain silence.
#
# TTSSpeakFrame, not LLMTextFrame: this is the pipeline talking, not the model. It goes
# straight to the TTS without entering the conversation as an assistant turn
# (append_to_context=False -- pipecat's default would commit the spoken notice as
# an assistant message, and TranscriptLedger charts a marked filler as nothing), so a
# failed call cannot poison the context the way a degenerate completion does (see
# llm_text_guard).
#
# Debounced because a failing endpoint errors per turn and sometimes per retry; saying
# it once per window is information, saying it five times is noise.
#
# TWO CAUSES (#205). The vague line is right for a timeout or a provider outage, which
# pass. It is wrong advice for a key the provider has refused or an account that has run
# out (401, 402, 403 -- pipecat's AUTHENTICATION, QUOTA and AUTHORIZATION): that fails
# on every turn until someone changes the key or the account, and "try again" sent the
# owner round in circles twice (the 402 above, and a spent key that OpenRouter reports
# as 403). pipecat classifies every ErrorFrame by the HTTP status its exception carries
# (FrameProcessor.push_error_frame, utils/errors.py), so the cause is read off
# frame.category, not re-parsed here. Everything else -- RATE_LIMIT, SERVER,
# CONNECTIVITY, INVALID_REQUEST, timeouts, UNKNOWN -- keeps the vague line.
#
# The account class gets a full line (what is wrong, that retrying won't help, where it
# is fixed) once per ACCOUNT_DEBOUNCE_FACTOR x the window, and a short one on every
# other failed turn inside that window -- a turn that fails in silence is the dead air
# this processor exists to prevent. Inside the window a passing failure gets the vague
# line without its "could you try again?" (GLITCH_NO_RETRY_TEXT), on the vague line's
# own clock: after "trying again won't help" that advice contradicts itself, but
# silence would be dead air (the owner may have topped up the account meanwhile).
#
# OUT OF CREDIT, NOT "NEEDS ATTENTION". The account class has two kinds the owner fixes
# in different places: a key refused, revoked or at its own limit (a key to change:
# Settings), and an account with nothing left to pay for answers (credit to add: the
# provider's site). The second is said as it is: is_out_of_credit -- a 402 (QUOTA), or
# the provider's own words (ai_account.says_out_of_credit), from a refusal or from an
# error the stream itself carried (no HTTP status: pipecat's UNKNOWN, which the words, or
# its body's code 402, make the account class). Its lines (_credit_text) name
# OpenRouter's website only where the endpoint is OpenRouter's, and "open my settings" only
# where it is heard, as the account lines do.
#
# Every line is said in the session's voice language (i18n.py catalogs).
#
# RECORDED, NOT ONLY SPOKEN (#205). ANSWERS keeps the current run of provider failures
# -- since when, the latest, which class, the provider's own words with any link or key
# removed -- and clears on the next request the provider accepts.
# services.BoundedOpenAILLMService feeds it (every LLM error, whether or not this
# processor is in the pipeline), gateway_server's /health serves it, and
# `teaport status` / `teaport doctor` print it, so a refused key no longer lives only
# in the journal.
#
# Disable with TEAPORT_LLM_ERROR_SPEECH=0 (the speech only; ANSWERS still records).
#
# NO MODEL AT ALL (#277). With no key or no endpoint address there is no LLM service to
# fail: the session is built without one (agent_session.build_agent_session), and this
# processor takes the LLM's place as well as its usual one. Every request for a reply
# (an LLMContextFrame: the user's turn, the greeting, a wake word's greeting) is taken
# here and answered with one fixed line, on the vague line's clock (once per window,
# whatever is said in between) -- one processor and one clock for "there is no answer",
# whatever the reason. Which line (_no_model_line):
#   * where spoken Wi-Fi setup is on and the box is offline, how to get it online (its
#     page takes the AI account's key too, #279);
#   * where "open my settings" is heard (settings_access.py, #279), that the account is
#     not set up and how to set it up: that phrase;
#   * anywhere else, that the account for its answers is not set up.
#   * Whatever the place, when the box's own trial credit is spent (credit.py, #280):
#     that it has run out, and (where it is heard) to say "open my settings".
# A phone call never asks: sip_server says NO_MODEL_CALL_TEXT through say_last_line,
# heard in full, and hangs up.
# A request that finds a model could be made now (a key written meanwhile: the key file
# is read per session) gets "I'm ready now" once, and the session ends so that the next
# one is built with it: through the client's own restart where it has one (the room's
# bridge opens the next session awake, so "ask me again" is heard), else as any session
# ends, with the line that says to start a new conversation (a Talk client). Spoken
# whatever TEAPORT_LLM_ERROR_SPEECH says: it is not a failure notice, and without it the
# box would say nothing at all.
#
import math
import time

import httpx
import openai
from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMContextFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.utils.errors import (ErrorCategory, classify_http_exception,
                                  classify_http_status_code)

from teaport_brain import ai_account, credit, i18n
from teaport_brain.ai_account import clean_reason, redact  # noqa: F401 (redact: services)
from teaport_brain.i18n import N_
from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_LLM_ERROR_SPEECH")

# Deliberately vague about the cause: the caller cannot act on "429" or "read timeout",
# and guessing wrong is worse than saying nothing specific. The journal has the detail.
# Plain words, as everything the caller hears: no "language model".
ERROR_TEXT = N_("Sorry, I couldn't get an answer just then. Could you try again?")
# The same without the advice, for a passing failure inside the account window (see
# LLMErrorSpeaker._notice).
GLITCH_NO_RETRY_TEXT = N_("Sorry, I couldn't get an answer just then.")

# The account class. Says it will not pass by itself and where it is fixed: the config
# page, which describes itself as "Settings for this Teaport box" and holds the key
# (LLM_API_KEY). Not "setup": setup mode is the state the box is in while it joins a
# Wi-Fi network (wifi_voice.py), and its phone page is named for that action. Worded
# so it also serves a caller on the phone who does not own the box.
ACCOUNT_TEXT = N_("Sorry, I can't answer right now: the account I use for my answers "
                  "needs attention, and trying again won't help. It can be fixed on my "
                  "settings page.")
# The same where "open my settings" is heard (the room, Talk: settings_access.usable):
# the owner is the one listening, and the phrase -- or asking in any words, the
# open_settings tool -- gets them there. The quoted words are that phrase, in each catalog
# one its own "settings" pattern hears (tests/test_i18n.py checks). A phone call keeps
# ACCOUNT_TEXT: settings are not opened from there.
ACCOUNT_SETTINGS_TEXT = N_("Sorry, I can't answer right now: the account I use for my "
                           "answers needs attention, and trying again won't help. Say "
                           "\"open my settings\" to fix it.")
# The same, on every later failed turn inside the account window: short, because the
# full line was said minutes ago, but not nothing.
ACCOUNT_STILL_TEXT = N_("I still can't answer until my account is sorted out.")
# The account is out of credit (is_out_of_credit; the module note): the cause, and where
# credit is added. On OpenRouter its website, by name: a spoken line, so not the bare
# address, which the voice reads badly (as wifi_voice.ACCOUNT_NO_CREDIT_TEXT; the page
# and the CLI show openrouter.ai/settings/credits). Where "open my settings" is heard,
# that phrase too (the quoted words, in each catalog, one its "settings" pattern hears:
# tests/test_i18n.py).
CREDIT_SETTINGS_TEXT = N_("Sorry, I can't answer: my AI account has run out of credit. Add "
                          "credit on OpenRouter's website, or say \"open my settings\" for "
                          "help.")
CREDIT_TEXT = N_("Sorry, I can't answer: my AI account has run out of credit. Credit can be "
                 "added on OpenRouter's website.")
# Another provider: its site is not ours to name.
CREDIT_OTHER_SETTINGS_TEXT = N_("Sorry, I can't answer: my AI account has run out of credit. "
                                "Add credit to it, or say \"open my settings\" for help.")
CREDIT_OTHER_TEXT = N_("Sorry, I can't answer: my AI account has run out of credit.")
# Its ACCOUNT_STILL_TEXT.
CREDIT_STILL_TEXT = N_("I still can't answer: my AI account is out of credit.")

# No model at all (#277; the module note). The account, as the account line names it.
NO_MODEL_TEXT = N_("I can't answer questions yet, because the account I use for my "
                   "answers isn't set up.")
# The same where "open my settings" is heard (#279). The quoted words are that phrase, in
# each catalog one its own "settings" pattern hears (tests/test_i18n.py checks).
NO_MODEL_SETTINGS_TEXT = N_("I can't answer questions yet, because the account I use for "
                            "my answers isn't set up. Say \"open my settings\" to set it up.")
# The quoted words are the start phrase, in each catalog one its own "start" pattern
# hears (tests/test_i18n.py checks): said back to the box, they start setup mode.
NO_MODEL_WIFI_TEXT = N_("I can't answer questions until I'm set up. Say \"set up Wi-Fi\" "
                        "to get me online.")
# A caller on the phone: short and neutral, no instructions for the box's owner
# (sip_server says it, heard in full, then hangs up).
NO_MODEL_CALL_TEXT = N_("Sorry, I can't take calls right now. Please try again later.")
# A model can be made now: this session ends, and the next one has it. Restarted by the
# client (the room's bridge opens the next one awake, listening): ask again.
NO_MODEL_READY_TEXT = N_("I'm ready now. Please ask me again.")
# Ended, and a new one is the client's to open (a Talk client).
NO_MODEL_READY_NEW_TEXT = N_("I'm ready now. Please start a new conversation with me.")
# How the session asks its client to restart it: the restart_session client tool's own
# message (tools.py, THE TOOL CONTRACT), as the model's call would send it.
RESTART_CALL = {"type": "client_tool", "call_id": "teaport-client-ready",
                "name": "restart_session", "args": {}}

ACCOUNT_CATEGORIES = frozenset({ErrorCategory.AUTHENTICATION,
                                ErrorCategory.AUTHORIZATION,
                                ErrorCategory.QUOTA})

# One spoken notice per window, however many frames arrive.
_DEBOUNCE_SECS = setting("TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE")
# The full account line's window, as a multiple of that one (so 0 still means every
# time).
ACCOUNT_DEBOUNCE_FACTOR = 10

# What a request to the provider raises: an HTTP status (openai.APIStatusError), no
# connection or a timeout (openai.APIConnectionError / APITimeoutError, httpx's own,
# asyncio's TimeoutError -- the base of pipecat's completion timeout). Anything else
# that reaches an LLM ErrorFrame is the brain's own failure, not the provider's.
PROVIDER_EXCEPTIONS = (openai.APIError, httpx.HTTPError, TimeoutError)


def category_of(frame: ErrorFrame) -> ErrorCategory:
    """The frame's category. pipecat settles it before an error travels
    (push_error_frame); an ErrorFrame built some other way may lack one, so fall back to
    what its exception's status code says, as pipecat would have."""
    if frame.category is not None:
        return frame.category
    if frame.exception is not None:
        return classify_http_exception(frame.exception)
    return ErrorCategory.UNKNOWN


def _provider_message(exception: BaseException) -> str:
    """The provider's message about a failure, raw: from its response body when there is
    one (the OpenAI SDK parses it into .body -- for an error the stream carried, the
    error object itself), else the exception's own text."""
    body = getattr(exception, "body", None)
    text = body.get("message") if isinstance(body, dict) else body
    if not isinstance(text, str) or not text.strip():
        text = str(exception)
    return text


def is_out_of_credit(frame: ErrorFrame) -> bool:
    """The account has nothing left to pay for answers (the module note): a 402, or the
    provider saying so -- in its words, or a 402 in its error body -- on a refusal or an
    error with no HTTP status. Never a failure of the kind that passes (a rate limit, an
    outage: their words can suggest buying credit), nor one no provider request raises."""
    category = category_of(frame)
    if category is ErrorCategory.QUOTA:
        return True
    if not (category in ACCOUNT_CATEGORIES or category is ErrorCategory.UNKNOWN):
        return False
    exception = frame.exception
    if not isinstance(exception, PROVIDER_EXCEPTIONS):
        return False
    body = getattr(exception, "body", None)
    code = body.get("code") if isinstance(body, dict) else None
    if code in (402, "402"):
        return True
    # Another status in the body of an error the stream carried: its own kind decides. A
    # 500 or 503 that says "credits" (an upstream's, say) passes like any outage.
    if isinstance(code, str) and code.strip().isdigit():
        code = int(code)
    if (isinstance(code, int) and not isinstance(code, bool)
            and classify_http_status_code(code) not in (ACCOUNT_CATEGORIES
                                                        | {ErrorCategory.UNKNOWN})):
        return False
    return ai_account.says_out_of_credit(_provider_message(exception))


def is_account_problem(frame: ErrorFrame) -> bool:
    """A key the provider refused, or an account with nothing left (401/402/403, or out
    of credit by the provider's words: is_out_of_credit)."""
    return category_of(frame) in ACCOUNT_CATEGORIES or is_out_of_credit(frame)


def failure_reason(exception: BaseException | None) -> str:
    """The provider's own first line about a failure, safe to show: _provider_message,
    cleaned as a key check's reason is (ai_account.clean_reason: redacted, every sentence
    that carried a link dropped, keys removed, no status code). The exception's type name
    when nothing readable is left; "" for no exception."""
    if exception is None:
        return ""
    return clean_reason(_provider_message(exception)) or type(exception).__name__


class AnswerHealth:
    """The current run of provider failures, for /health and the CLI (#205). One per
    brain process, shared by every session in it, cleared by the next accepted request."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._since = None      # first failure of the current run (wall clock)
        self._last = None
        self._count = 0
        self._category = None
        self._reason = ""
        self._account = False   # the latest is the account class (is_account_problem)
        self._credit = False    # ... and out of credit (is_out_of_credit)
        self._credit_key = None  # the key it was out of credit on (credit.key_hash)
        self._ok_at = None      # the last accepted request

    def failed(self, frame: ErrorFrame) -> None:
        """Record `frame` when the provider failed it. Not our tool code (APPLICATION),
        and not an exception no provider request raises (a bug in rendering the request,
        say): status would blame the provider for it, and the owner can do nothing about
        it there -- the journal has it, with pipecat's own error line."""
        category = category_of(frame)
        if (category is ErrorCategory.APPLICATION
                or not isinstance(frame.exception, PROVIDER_EXCEPTIONS)):
            return
        now = self._clock()
        was_account = self._account
        if self._since is None:
            self._since = now
        self._last, self._count = now, self._count + 1
        self._category, self._reason = category, failure_reason(frame.exception)
        self._account, self._credit = is_account_problem(frame), is_out_of_credit(frame)
        key = ai_account.current_key()
        self._credit_key = credit.key_hash(key) if self._credit and key else None
        if self._account:
            # On the trial credit this is most likely the credit running out: read it
            # again now, not at the next poll (credit.py, #280).
            credit.MONITOR.poke()
        if self._account and not was_account:
            logger.error(f"answers: the provider refused this account ({category.value}"
                         f"{', out of credit' if self._credit else ''}): {self._reason} "
                         "-- every request will fail until the key or the account is fixed")

    def succeeded(self) -> None:
        if self._since is not None:
            logger.info(f"answers: the provider accepts requests again, after "
                        f"{self._count} failure(s)")
        self._since = self._last = self._category = None
        self._account = self._credit = False
        self._credit_key = None
        self._count, self._reason, self._ok_at = 0, "", self._clock()

    def status(self) -> dict:
        """state: "ok" (the last request was accepted), "untried" (none since the brain
        started), "failing" (failures of the kind that pass) or "account" (a refused key
        or a spent account; with "credit": true when it is out of credit,
        is_out_of_credit). since / last: wall-clock seconds of the run's first and latest
        failure; reason: failure_reason of the latest. A failing state stays until the next
        accepted request: nothing else can tell that the key was fixed."""
        if self._since is None:
            return {"state": "ok" if self._ok_at is not None else "untried"}
        return {"state": "account" if self._account else "failing",
                "category": self._category.value, "since": self._since,
                "last": self._last, "count": self._count, "reason": self._reason,
                **({"credit": True} if self._credit else {})}

    def out_of_credit(self) -> bool:
        """The latest failure says the account is out of credit, no request has been
        accepted since, and the key is still the one it was refused on (the Settings
        page's line: config_ui.account). A key saved since is another account, or one
        topped up: its first request tells, and the page no longer says it ran out."""
        key = ai_account.current_key()
        return (self._credit and self._credit_key is not None and bool(key)
                and credit.key_hash(key) == self._credit_key)


ANSWERS = AnswerHealth()


class LLMErrorSpeaker(FrameProcessor):
    """Speaks a short line when the LLM call fails; forwards the error untouched.

    lang_fn: the session's voice language (an espeak code, as EngineTTSService's
    espeak_language), read at each notice so a voice switch is followed.

    no_model: the session has no LLM (#277; the module note): this processor answers
    every request for a reply in its place. Then also `wifi` (the session's
    WifiSetupVoice, where spoken Wi-Fi setup is on), `phrases` (the session's fixed
    phrases, wifi_voice.PhraseVoice: told what was said, which quotes a phrase),
    `settings` ("open my settings" is heard here), `why` (why no model can be made:
    services.model_missing, read as the request comes), `gate` (the session's FollowupGate:
    the request it took is not left claiming the window), `model_back` (True once a
    model could be made), `restarts` (the client restarts a session itself: the
    restart_session client tool) and `end` (an async callable that ends the session,
    set by the session builder)."""

    def __init__(self, lang_fn=None, *, no_model: bool = False, wifi=None, gate=None,
                 model_back=None, end=None, restarts: bool = False, phrases=None,
                 settings: bool = False, why=None, owner: bool = False):
        super().__init__()
        self._lang_fn = lang_fn or (lambda: None)
        self.no_model = no_model
        self._restarts = restarts
        self._wifi = wifi
        self._phrases = phrases if phrases is not None else wifi
        self._settings = settings
        self._why = why or (lambda: None)
        # The owner's own surface (the room, Talk; not a bridge, not a phone call): only
        # there is the box's trial credit anyone's business (#280).
        self._owner = owner
        self._gate = gate
        self.model_back = model_back or (lambda: False)
        self.end = end
        self._ready_said = False
        # -inf, not 0.0: time.monotonic() is time since BOOT on Linux, so 0.0 is not a
        # "never spoken" sentinel — it is boot. On an appliance that starts the brain a
        # few seconds after boot, `now - 0.0` is smaller than the debounce window, and
        # the FIRST error of the session — the one that matters most — was swallowed as
        # a duplicate of an utterance that never happened.
        self._said = -math.inf          # the vague line
        self._said_account = -math.inf  # the full account line
        # The last line was the trial credit running out on a refusal (#280): the
        # session ends after it, so the next is built without a model.
        self._end_after = False

    def _notice(self, frame: ErrorFrame, now: float) -> str | None:
        """The line for this failure, or None for none; moves the clocks."""
        account_window = _DEBOUNCE_SECS * ACCOUNT_DEBOUNCE_FACTOR
        in_account_window = now - self._said_account < account_window
        if is_account_problem(frame):
            out_of_credit = is_out_of_credit(frame)
            if in_account_window:
                return CREDIT_STILL_TEXT if out_of_credit else ACCOUNT_STILL_TEXT
            self._said_account = now
            # On the owner's surface, a refusal that is the trial credit running out -- the
            # provider said so (out of credit), or nothing is left -- says so (#280). A
            # revoked key or a policy 403 with credit left is the usual account line.
            state = credit.MONITOR.state()
            if self._owner and state not in (credit.NONE, credit.OWN) and (
                    out_of_credit or state == credit.EMPTY):
                self._end_after = True
                return self._spent_text()
            if out_of_credit:
                return self._credit_text()
            return ACCOUNT_SETTINGS_TEXT if self._settings else ACCOUNT_TEXT
        if now - self._said < _DEBOUNCE_SECS:
            return None
        self._said = now
        # Inside the account window the passing failure still gets a line (the account
        # may have been topped up meanwhile, with the clock still running), but not
        # "try again", which would contradict the account line.
        return GLITCH_NO_RETRY_TEXT if in_account_window else ERROR_TEXT

    def _credit_text(self) -> str:
        """The account is out of credit: where it is added (OpenRouter's website on its
        endpoint, read as the failure comes), and "open my settings" where it is heard."""
        if ai_account.is_openrouter(setting("LLM_BASE_URL")):
            return CREDIT_SETTINGS_TEXT if self._settings else CREDIT_TEXT
        return CREDIT_OTHER_SETTINGS_TEXT if self._settings else CREDIT_OTHER_TEXT

    def _spent_text(self) -> str:
        """The trial credit is spent (#280): with "open my settings" where it is heard."""
        return credit.EMPTY_TEXT if self._settings else credit.EMPTY_PLAIN_TEXT

    # -- no model (#277)
    async def _no_model_line(self) -> str | None:
        """The line for a request with no model to answer it, or None (debounced); moves
        the clock. Asked as the request comes, so a key written meanwhile is found."""
        if self.model_back():
            if self._ready_said:
                return None
            self._ready_said = True
            return NO_MODEL_READY_TEXT if self._restarts else NO_MODEL_READY_NEW_TEXT
        now = time.monotonic()
        if now - self._said < _DEBOUNCE_SECS:
            return None
        self._said = now
        if self._wifi is not None and not await self._wifi.box_online():
            return NO_MODEL_WIFI_TEXT
        if self._why() == credit.SPENT_REASON:
            # Asked for an answer while it is spent: read again (spaced: MIN_READ_SECS),
            # so a top-up is found within a minute, not at the next poll (#280).
            credit.MONITOR.poke()
            if self._owner:
                return self._spent_text()
        return NO_MODEL_SETTINGS_TEXT if self._settings else NO_MODEL_TEXT

    async def _answer_without_model(self) -> None:
        text = await self._no_model_line()
        if text is None:
            logger.debug("no model: the line was said moments ago -- not again yet")
            return
        logger.info(f"no model: speaking {text[:40]!r}")
        if text is NO_MODEL_READY_TEXT:
            # Before the line: the client ends the session once the line after its
            # restart has played, and opens the next one at once, awake.
            logger.info("no model: one can be made now -- the client restarts this "
                        "session, so the next one is built with it")
            await self.push_frame(OutputTransportMessageUrgentFrame(message=dict(RESTART_CALL)),
                                  FrameDirection.DOWNSTREAM)
        await self._say(text)
        if text is NO_MODEL_READY_NEW_TEXT:
            await self._end_for_the_next("no model: one can be made now")

    async def _say(self, text: str) -> None:
        """`text` (a msgid) in the session's language, the pipeline talking: out of the
        context, noted as the box's own voice (a phrase may be in it)."""
        t = i18n.for_espeak(self._lang_fn())
        line = t._(text)
        if self._phrases is not None:
            self._phrases.note_said(line)
        await self.push_frame(TTSSpeakFrame(line, append_to_context=False),
                              FrameDirection.DOWNSTREAM)

    async def _end_for_the_next(self, why: str) -> None:
        """End this session after the line just pushed (drained, so it plays), so the next
        one is built afresh: with a model once one can be made (#277), without one once
        the trial credit has run out (#280)."""
        if self.end is not None:
            logger.info(f"{why} -- ending this session, so the next one is built anew")
            await self.end()

    def _spawn(self, coro):
        return self.create_task(coro)  # pipecat's task manager (tests use asyncio's)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (self.no_model and isinstance(frame, LLMContextFrame)
                and direction == FrameDirection.DOWNSTREAM):
            # The request a model would render: taken here, as a model takes it -- the
            # gate stops holding the window for a completion that will never start.
            if self._gate is not None:
                self._gate.request_rendered()
                self._gate.release_claim()
            # A task: the online check may take a few seconds, and nothing waits on it.
            self._spawn(self._answer_without_model())
            return
        if isinstance(frame, ErrorFrame) and isinstance(frame.processor, LLMService):
            text = self._notice(frame, time.monotonic())
            if text is not None:
                # The category and the redacted reason, not the frame (#205).
                logger.warning(
                    f"LLMErrorSpeaker: speaking {text[:40]!r} ({category_of(frame).value}:"
                    f" {failure_reason(frame.exception) or 'no exception'})")
                # DOWNSTREAM: the TTS is below this processor, and the error itself
                # still continues upstream to the task on the push below.
                await self._say(text)
                if self._end_after:
                    # The trial credit ran out mid-conversation (#280): it counts as
                    # spent now, and this session ends, so the next has no model.
                    self._end_after = False
                    credit.MONITOR.refused_for_credit()
                    await self._end_for_the_next("the trial credit ran out")
            else:
                logger.debug("LLMErrorSpeaker: notice debounced")
        await self.push_frame(frame, direction)
