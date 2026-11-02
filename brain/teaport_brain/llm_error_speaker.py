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
# TWO LINES, BY CAUSE (#205). The vague line is right for a timeout or a provider
# outage, which pass. It is wrong advice for a key the provider has refused or an
# account that has run out (401, 402, 403 -- pipecat's AUTHENTICATION, QUOTA and
# AUTHORIZATION): that fails on every turn until someone changes the key or the
# account, and "try again" sent the owner round in circles twice (the 402 above, and a
# spent key that OpenRouter reports as 403). pipecat classifies every ErrorFrame by the
# HTTP status its exception carries (FrameProcessor.push_error_frame, utils/errors.py),
# so the cause is read off frame.category, not re-parsed here. Everything else --
# RATE_LIMIT, SERVER, CONNECTIVITY, INVALID_REQUEST, timeouts, UNKNOWN -- keeps the
# vague line. The account line is debounced ACCOUNT_DEBOUNCE_FACTOR times longer: it
# already says trying again won't help, and repeating it every turn adds nothing.
#
# RECORDED, NOT ONLY SPOKEN (#205). ANSWERS keeps the current run of failures -- since
# when, which class, the provider's own words with any link or key stripped -- and
# clears on the next request the provider accepts. services.BoundedOpenAILLMService
# feeds it (every LLM error, whether or not this processor is in the pipeline),
# gateway_server's /health serves it, and `teaport status` / `teaport doctor` print it,
# so a refused key no longer lives only in the journal.
#
# Disable with TEAPORT_LLM_ERROR_SPEECH=0 (the speech only; ANSWERS still records).
#
import math
import re
import time

from loguru import logger

from pipecat.frames.frames import ErrorFrame, Frame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.utils.errors import ErrorCategory, classify_http_exception

from teaport_brain.settings import setting

ENABLED = setting("TEAPORT_LLM_ERROR_SPEECH")

# Deliberately vague about the cause: the caller cannot act on "429" or "read timeout",
# and guessing wrong is worse than saying nothing specific. The journal has the detail.
# Plain words, as everything the caller hears: no "language model".
ERROR_TEXT = "Sorry, I couldn't get an answer just then. Could you try again?"

# The account class. Says it will not pass by itself and where it is fixed: the config
# page, which describes itself as "Settings for this Teaport box" and holds the key
# (LLM_API_KEY). Not "setup page": that is the Wi-Fi setup page (wifi_setup.py). Worded
# so it also serves a caller on the phone who does not own the box.
ACCOUNT_TEXT = ("Sorry, I can't answer right now: the account I use for my answers "
                "needs attention, and trying again won't help. It can be fixed on my "
                "settings page.")

ACCOUNT_CATEGORIES = frozenset({ErrorCategory.AUTHENTICATION,
                                ErrorCategory.AUTHORIZATION,
                                ErrorCategory.QUOTA})

# One spoken notice per window, however many frames arrive.
_DEBOUNCE_SECS = setting("TEAPORT_LLM_ERROR_SPEECH_DEBOUNCE")
# The account line's window, as a multiple of that one (so 0 still means every time).
ACCOUNT_DEBOUNCE_FACTOR = 10


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


# What may not leave the brain in a reason: links (a provider's key-limit message can
# name the key's management page, whose path carries the key's id) and anything
# key-shaped.
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+", re.IGNORECASE)
_KEYLIKE = re.compile(r"\b(?:sk|pk|rk|key)[-_][A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{20,}")
_STATUS_PREFIX = re.compile(r"^\s*error code:\s*\d+\s*-?\s*", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_REASON_MAX = 120


def failure_reason(exception: BaseException | None) -> str:
    """The provider's own first line about a failure, safe to show: the message from its
    response body when there is one (the OpenAI SDK parses it into .body), with every
    sentence that carries a link dropped, key-shaped tokens removed and no status code.
    The exception's type name when nothing readable is left; "" for no exception."""
    if exception is None:
        return ""
    body = getattr(exception, "body", None)
    text = body.get("message") if isinstance(body, dict) else body
    if not isinstance(text, str) or not text.strip():
        text = str(exception)
    text = _STATUS_PREFIX.sub("", (text.strip().splitlines() or [""])[0])
    kept = [s for s in _SENTENCES.split(text) if not _URL.search(s)]
    text = " ".join(_KEYLIKE.sub("", " ".join(kept)).split()).strip(" -:;,.")
    if len(text) > _REASON_MAX:
        text = text[:_REASON_MAX - 1].rstrip() + "…"
    return text or type(exception).__name__


class AnswerHealth:
    """The current run of LLM failures, for /health and the CLI (#205). One per brain
    process, shared by every session in it, cleared by the next accepted request."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._since = None      # first failure of the current run (wall clock)
        self._last = None
        self._count = 0
        self._category = None
        self._reason = ""
        self._ok_at = None      # the last accepted request

    def failed(self, frame: ErrorFrame) -> None:
        category = category_of(frame)
        if category is ErrorCategory.APPLICATION:
            return  # our own tool code failed, not the provider
        now = self._clock()
        was_account = self._category in ACCOUNT_CATEGORIES
        if self._since is None:
            self._since = now
        self._last, self._count = now, self._count + 1
        self._category, self._reason = category, failure_reason(frame.exception)
        if category in ACCOUNT_CATEGORIES and not was_account:
            # Once per run, and the scrubbed reason only: the frame's own text can hold
            # the provider's management link (pipecat's own error line logs it anyway).
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
        latest failure; reason: failure_reason of the latest."""
        if self._since is None:
            return {"state": "ok" if self._ok_at is not None else "untried"}
        account = self._category in ACCOUNT_CATEGORIES
        return {"state": "account" if account else "failing",
                "category": self._category.value, "since": self._since,
                "last": self._last, "count": self._count, "reason": self._reason}


ANSWERS = AnswerHealth()


class LLMErrorSpeaker(FrameProcessor):
    """Speaks a short line when the LLM call fails; forwards the error untouched."""

    def __init__(self):
        super().__init__()
        # -inf, not 0.0: time.monotonic() is time since BOOT on Linux, so 0.0 is not a
        # "never spoken" sentinel — it is boot. On an appliance that starts the brain a
        # few seconds after boot, `now - 0.0` is smaller than the debounce window, and
        # the FIRST error of the session — the one that matters most — was swallowed as
        # a duplicate of an utterance that never happened.
        # One clock per line, keyed by is_account_problem: an account failure after a
        # passing glitch is news, and is said even inside the glitch's window.
        self._last = {False: -math.inf, True: -math.inf}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and isinstance(frame.processor, LLMService):
            account = is_account_problem(frame)
            window = _DEBOUNCE_SECS * (ACCOUNT_DEBOUNCE_FACTOR if account else 1)
            now = time.monotonic()
            if now - self._last[account] >= window:
                self._last[account] = now
                # The category and the scrubbed reason, not the frame: its text can
                # carry the provider's link to the key (#205).
                logger.warning(
                    f"LLMErrorSpeaker: speaking the {'account' if account else 'failure'}"
                    f" notice ({category_of(frame).value}: "
                    f"{failure_reason(frame.exception) or 'no exception'})"
                )
                # DOWNSTREAM: the TTS is below this processor, and the error itself
                # still continues upstream to the task on the push below.
                await self.push_frame(
                    TTSSpeakFrame(ACCOUNT_TEXT if account else ERROR_TEXT,
                                  append_to_context=False),
                    FrameDirection.DOWNSTREAM)
            else:
                logger.debug("LLMErrorSpeaker: notice debounced")
        await self.push_frame(frame, direction)
