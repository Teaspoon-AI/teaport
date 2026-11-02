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
import math
import re
import time

import httpx
import openai
from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.utils.errors import ErrorCategory, classify_http_exception

from teaport_brain import i18n
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
# (LLM_API_KEY). Not "setup page": that is the Wi-Fi setup page (wifi_setup.py). Worded
# so it also serves a caller on the phone who does not own the box.
ACCOUNT_TEXT = N_("Sorry, I can't answer right now: the account I use for my answers "
                  "needs attention, and trying again won't help. It can be fixed on my "
                  "settings page.")
# The same, on every later failed turn inside the account window: short, because the
# full line was said minutes ago, but not nothing.
ACCOUNT_STILL_TEXT = N_("I still can't answer until my account is sorted out.")

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


def is_account_problem(frame: ErrorFrame) -> bool:
    """A key the provider refused, or an account with nothing left (401/402/403)."""
    return category_of(frame) in ACCOUNT_CATEGORIES


# What may not leave the brain: links (a provider's key-limit message can name the key's
# management page, whose path carries the key's id) and anything key-shaped -- a key by
# its prefix (sk-, pk-, rk-, whole token), a key the provider masked ("sk-proj-****abcd",
# "****abcd": the visible tail is still part of a key), or a long opaque id.
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+", re.IGNORECASE)
_KEYLIKE = re.compile(r"\b(?:sk|pk|rk)[-_]\S*|\S*\*{3,}\S*|[A-Za-z0-9_-]{20,}")
_LINK, _KEY = "<link>", "<key>"
_STATUS_PREFIX = re.compile(r"^\s*error code:\s*\d+\s*-?\s*", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_REASON_MAX = 120


def redact(text: str) -> str:
    """`text` with every link replaced by <link> and every key-shaped token by <key>:
    the one redaction both the error log line (services.push_error_frame) and the shown
    reason (failure_reason) go through."""
    return _KEYLIKE.sub(_KEY, _URL.sub(_LINK, text))


def failure_reason(exception: BaseException | None) -> str:
    """The provider's own first line about a failure, safe to show: the message from its
    response body when there is one (the OpenAI SDK parses it into .body), redacted, with
    every sentence that carried a link dropped, keys removed and no status code. The
    exception's type name when nothing readable is left; "" for no exception."""
    if exception is None:
        return ""
    body = getattr(exception, "body", None)
    text = body.get("message") if isinstance(body, dict) else body
    if not isinstance(text, str) or not text.strip():
        text = str(exception)
    text = redact(_STATUS_PREFIX.sub("", (text.strip().splitlines() or [""])[0]))
    kept = [s for s in _SENTENCES.split(text) if _LINK not in s]
    text = " ".join(" ".join(kept).replace(_KEY, "").split()).strip(" -:;,.")
    if len(text) > _REASON_MAX:
        text = text[:_REASON_MAX - 1].rstrip() + "…"
    return text or type(exception).__name__


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
        was_account = self._category in ACCOUNT_CATEGORIES
        if self._since is None:
            self._since = now
        self._last, self._count = now, self._count + 1
        self._category, self._reason = category, failure_reason(frame.exception)
        if category in ACCOUNT_CATEGORIES and not was_account:
            logger.error(f"answers: the provider refused this account ({category.value}): "
                         f"{self._reason} -- every request will fail until the key or "
                         "the account is fixed")

    def succeeded(self) -> None:
        if self._since is not None:
            logger.info(f"answers: the provider accepts requests again, after "
                        f"{self._count} failure(s)")
        self._since = self._last = self._category = None
        self._count, self._reason, self._ok_at = 0, "", self._clock()

    def status(self) -> dict:
        """state: "ok" (the last request was accepted), "untried" (none since the brain
        started), "failing" (failures of the kind that pass) or "account" (a refused key
        or a spent account). since / last: wall-clock seconds of the run's first and
        latest failure; reason: failure_reason of the latest. A failing state stays until
        the next accepted request: nothing else can tell that the key was fixed."""
        if self._since is None:
            return {"state": "ok" if self._ok_at is not None else "untried"}
        account = self._category in ACCOUNT_CATEGORIES
        return {"state": "account" if account else "failing",
                "category": self._category.value, "since": self._since,
                "last": self._last, "count": self._count, "reason": self._reason}


ANSWERS = AnswerHealth()


class LLMErrorSpeaker(FrameProcessor):
    """Speaks a short line when the LLM call fails; forwards the error untouched.

    lang_fn: the session's voice language (an espeak code, as EngineTTSService's
    espeak_language), read at each notice so a voice switch is followed."""

    def __init__(self, lang_fn=None):
        super().__init__()
        self._lang_fn = lang_fn or (lambda: None)
        # -inf, not 0.0: time.monotonic() is time since BOOT on Linux, so 0.0 is not a
        # "never spoken" sentinel — it is boot. On an appliance that starts the brain a
        # few seconds after boot, `now - 0.0` is smaller than the debounce window, and
        # the FIRST error of the session — the one that matters most — was swallowed as
        # a duplicate of an utterance that never happened.
        self._said = -math.inf          # the vague line
        self._said_account = -math.inf  # the full account line

    def _notice(self, frame: ErrorFrame, now: float) -> str | None:
        """The line for this failure, or None for none; moves the clocks."""
        account_window = _DEBOUNCE_SECS * ACCOUNT_DEBOUNCE_FACTOR
        in_account_window = now - self._said_account < account_window
        if is_account_problem(frame):
            if in_account_window:
                return ACCOUNT_STILL_TEXT
            self._said_account = now
            return ACCOUNT_TEXT
        if now - self._said < _DEBOUNCE_SECS:
            return None
        self._said = now
        # Inside the account window the passing failure still gets a line (the account
        # may have been topped up meanwhile, with the clock still running), but not
        # "try again", which would contradict the account line.
        return GLITCH_NO_RETRY_TEXT if in_account_window else ERROR_TEXT

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and isinstance(frame.processor, LLMService):
            text = self._notice(frame, time.monotonic())
            if text is not None:
                # The category and the redacted reason, not the frame (#205).
                logger.warning(
                    f"LLMErrorSpeaker: speaking {text[:40]!r} ({category_of(frame).value}:"
                    f" {failure_reason(frame.exception) or 'no exception'})")
                t = i18n.for_espeak(self._lang_fn())
                # DOWNSTREAM: the TTS is below this processor, and the error itself
                # still continues upstream to the task on the push below.
                await self.push_frame(TTSSpeakFrame(t._(text), append_to_context=False),
                                      FrameDirection.DOWNSTREAM)
            else:
                logger.debug("LLMErrorSpeaker: notice debounced")
        await self.push_frame(frame, direction)
