#
# teaport — the starter key's credit (#280): tell the owner before the box's included
# trial credit runs out, and stop (never switch models) when it has.
#
# Appliances ship with a provisioned OpenRouter key capped at a few dollars. Which key that
# is, provisioning records as a marker (MARKER): the SHA-256 of the key, in hex, alone on
# one line, in the run user's state directory -- ~/.local/state/teaport/starter_key_sha256
# (settings.state_dir; teaport-appliance#3 writes it). The recipe:
#     printf %s "$KEY" | sha256sum | cut -d' ' -f1 > ~/.local/state/teaport/starter_key_sha256
# (the key without a newline). `sha256sum llm_key` of a key file ending in one newline is
# accepted too, so the easy mistake still matches. Only the hash: the marker says which
# key is the starter, never what it is. While the key in use (ai_account.current_key) has
# that hash, the box is "on its trial credit"; any other key saved later is the owner's,
# and everything here goes quiet.
#
# The monitor (Monitor, one per brain: MONITOR) reads the key's own record from OpenRouter,
# GET /api/v1/key (ai_account.check_key, the same cheap request a key is checked with on
# save): every POLL_SECS, after each conversation, and after an answer the provider refused
# for the account. It runs beside the sessions and never in a turn: what sessions read is
# the last answer. A request that fails is "unknown" for that read, never a warning: the
# last state known stays, marked not fresh. The record's fields are read tolerantly
# (ai_account.key_record: "data" or the top level; limit_remaining, or limit minus usage)
# -- the docs show two shapes. A newer record also carries effective_budget {limit_usd,
# spend_usd, remaining_usd, ...}: whichever of it and the key's own limit has less left is
# the one judged. ai_account.credit_left is that reading, the one the save-time key check
# uses too; a missing or malformed block changes nothing.
#
# Three states warn (owner decision on #283), judged from the record (judge):
#   - half: HALF_USED of the limit is used (TEAPORT_STARTER_HALF_USED, default half);
#   - low: LOW_USD or less is left (TEAPORT_STARTER_LOW_USD, default $1);
#   - empty: nothing is left.
# Each is said once, at the start of a conversation on the room or Talk (never a phone
# call nor a bridge: notice(), heard in full, counted once it played: told()), in plain
# words, and again at most once a day while it lasts; the
# Settings page shows it as a line (page_line). Empty is not a notice but a stop: with no
# other key, no model is made (services.make_llm raises NoModel with SPENT_REASON, and
# model_missing says the same: #277's path), so every request for an answer gets the trial-credit line
# (llm_error_speaker.py), "open my settings" and setup mode still work by voice, and the
# next session after the owner saves a key has a model again. It never switches to another
# model on its own.
#
# /health's "credit" is status(); `teaport status` and `teaport doctor` print it.
#
# No Pipecat import (the CLI's checks and the config page read it too).
#
import asyncio
import hashlib
import json
import os
import re
import time

from loguru import logger

from teaport_brain import ai_account
from teaport_brain.i18n import N_
from teaport_brain.settings import setting, state_dir

MARKER = "starter_key_sha256"
# When each warning was last said, per key and state, so a restart does not say it again;
# and what was left when the provider refused the key for its credit (refused_for_credit).
NOTICES = "credit_notices.json"
HALF_USED = setting("TEAPORT_STARTER_HALF_USED")
LOW_USD = setting("TEAPORT_STARTER_LOW_USD")
POLL_SECS = setting("TEAPORT_STARTER_POLL_SECS")
# Reads at most this often: a poke (a conversation ending, a refusal) sooner waits for it,
# so a burst of reconnects or failures is one read, not one each.
MIN_READ_SECS = 60.0
# How long a session being built waits for the first read on a box that just started.
FIRST_READ_WAIT_SECS = 3.0
# A warning is said again, while its state lasts, at most this often.
REPEAT_SECS = 24 * 3600.0
# A 402 counts as the trial credit running out (sticky: refused_for_credit) at or below
# this many dollars left, and only a read this much above the refusal counts as a top-up:
# LOW_USD, but never less than STICKY_FLOOR_USD -- with the low warning set to 0 (no
# warning), a refusal at a cent left must still not flap.
STICKY_FLOOR_USD = 0.10


def sticky_margin() -> float:
    return max(LOW_USD, STICKY_FLOOR_USD)


# Below this many dollars the credit is spent (a request costs more than what is left).
EMPTY_USD = 0.005

# The states (status()["state"]).
NONE = "none"        # no key at all
OWN = "own"          # a key that is not the starter: the owner's
UNKNOWN = "unknown"  # the starter key, and nothing known about its credit yet
OK, HALF, LOW, EMPTY = "ok", "half", "low", "empty"

# services.model_missing's reason while the trial credit is spent (and /health's).
SPENT_REASON = "the trial credit has run out"

# Said at the start of a conversation (notice). The quoted words are the settings phrase
# (settings_access.py; tests/test_i18n.py checks each catalog's).
HALF_TEXT = N_("Quick note: I've used half of my trial credit. When you have a minute, say "
               "\"open my settings\" to connect your own account.")
LOW_TEXT = N_("Quick note: my trial credit is almost used up. When you have a minute, say "
              "\"open my settings\" to connect your own account.")
# In place of every answer once it is spent (llm_error_speaker.py): with the phrase where
# it is heard, else without.
EMPTY_TEXT = N_("My trial credit has run out, so I can't answer questions until you "
                "connect your own account. Say \"open my settings\" to do that.")
EMPTY_PLAIN_TEXT = N_("My trial credit has run out, so I can't answer questions until your "
                      "own account is connected.")
# The Settings page's line (page_line); {amount} is dollars, "$0.87".
PAGE_LINES = {
    OK: N_("Teaport is using its included trial credit."),
    HALF: N_("Half of the trial credit is used. Connect your own account to keep Teaport "
             "answering."),
    LOW: N_("The trial credit is almost used up: about {amount} left. Connect your own "
            "account to keep Teaport answering."),
    EMPTY: N_("The trial credit has run out. Connect your own account so Teaport can answer "
              "again."),
}


def key_hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def starter_hash() -> str | None:
    """The marker's hash, or None: no marker, or not a SHA-256 in hex."""
    try:
        with open(os.path.join(state_dir(), MARKER), encoding="utf-8") as f:
            text = f.read(256).strip().lower()
    except OSError:
        return None
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def on_starter(key: str | None) -> bool:
    """Is `key` the provisioned starter key? Its marker is the hash of the key, or of the
    key and one newline (`sha256sum` of its key file)."""
    want = starter_hash()
    if not (key and want):
        return False
    stripped = key.strip()
    return want in (key_hash(stripped),
                    hashlib.sha256((stripped + "\n").encode()).hexdigest())


def judge(record: dict, half: float = None, low: float = None) -> tuple[str, dict]:
    """(state, {"limit", "remaining", "usage"}) from the key's record, as
    ai_account.credit_left reads it (the key's own limit or its effective budget,
    whichever has less left). UNKNOWN when it says nothing about what is left (no limit,
    no remaining)."""
    half = HALF_USED if half is None else half
    low = LOW_USD if low is None else low
    left = ai_account.credit_left(record)
    numbers = {"limit": left.limit, "remaining": left.remaining, "usage": left.usage}
    if left.remaining is None:
        return UNKNOWN, numbers
    if left.remaining <= EMPTY_USD:
        return EMPTY, numbers
    if left.remaining <= low:
        return LOW, numbers
    if left.limit and left.spent / left.limit >= half:
        return HALF, numbers
    return OK, numbers


class Monitor:
    """The starter key's credit, as last read (module note). `fetch(key, base_url)` is
    ai_account.check_key, replaced in tests (none talks to a provider)."""

    def __init__(self, clock=time.time, fetch=None):
        self._clock = clock
        self._fetch = fetch or ai_account.check_key
        self._for = None          # the key hash the reading below is of
        self._state = UNKNOWN
        self._numbers: dict = {}
        self._checked = None      # wall clock of the last answer
        self._fresh = False       # the last request was answered
        self._wake: asyncio.Event | None = None
        self._mono = time.monotonic  # the read spacing's clock (MIN_READ_SECS)
        self._attempted: asyncio.Event | None = None  # set after each read attempt (run)

    # -- reading
    def status(self) -> dict:
        """/health's "credit": {"state": none | own | unknown | ok | half | low | empty}, and
        while on the starter key: limit, remaining, usage (dollars, or null), checked (wall
        clock of the last answer) and fresh (the last request was answered)."""
        key = ai_account.current_key()
        if not key:
            return {"state": NONE}
        if not on_starter(key):
            return {"state": OWN}
        if self._for != key_hash(key):
            return {"state": UNKNOWN, "fresh": False}
        return {"state": self._state, **self._numbers, "checked": self._checked,
                "fresh": self._fresh}

    def state(self) -> str:
        return self.status()["state"]

    def spent(self) -> bool:
        """The trial credit is used up and no other key replaced the starter."""
        return self.state() == EMPTY

    # -- asking OpenRouter
    async def refresh(self) -> None:
        """One read of the starter key's record (nothing when the key is another)."""
        key = ai_account.current_key()
        if not on_starter(key):
            return
        base = setting("LLM_BASE_URL")
        if not ai_account.is_openrouter(base):
            return  # the starter key is OpenRouter's; another endpoint says nothing of it
        check = await asyncio.to_thread(self._fetch, key, base)
        mine = key_hash(key)
        if not check.data:
            # No record: no answer, or a refusal with nothing to read. Never alarming: what
            # was known stays, as not fresh.
            if self._fresh or self._for != mine:
                logger.info(f"credit: the starter key's record could not be read "
                            f"({check.state}) -- keeping what was known")
            if self._for != mine:
                self._for, self._state, self._numbers = mine, UNKNOWN, {}
            self._fresh = False
            return
        state, numbers = judge(check.data)
        refused_at = _load_notices().get(f"{mine[:16]}:refused")
        if isinstance(refused_at, (int, float)):
            # The provider refused this key for its credit (refused_for_credit): spent it
            # stays -- OpenRouter refuses while a few cents still show (the max_tokens
            # reservation, usage not yet counted) -- until the record shows a real top-up,
            # LOW_USD more than it showed at the refusal. Else every question would go
            # "run out", session ended, "ready now", refused again, round and round.
            remaining = numbers.get("remaining")
            if remaining is not None and remaining > refused_at + sticky_margin():
                logger.info("credit: topped up since the refusal -- the trial credit counts again")
                seen = _load_notices()
                seen.pop(f"{mine[:16]}:refused", None)
                _save_notices(seen)
            else:
                state = EMPTY
        if (state, mine) != (self._state, self._for):
            logger.info(f"credit: the trial credit is {state} "
                        f"({numbers.get('remaining')} of {numbers.get('limit')} left)")
        self._for, self._state, self._numbers = mine, state, numbers
        self._checked, self._fresh = self._clock(), True

    async def settled(self, timeout: float = FIRST_READ_WAIT_SECS) -> None:
        """Before a session is built: on the starter key with nothing read of it yet (the
        brain just started), wait -- at most `timeout` -- for the first read, so a box that
        boots with its trial credit spent starts with no model rather than one that
        refuses every turn. Nothing to wait for otherwise, or with no reader running."""
        key = ai_account.current_key()
        if self._attempted is None or not on_starter(key) or self._for == key_hash(key):
            return
        try:
            await asyncio.wait_for(self._attempted.wait(), timeout)
        except asyncio.TimeoutError:
            logger.info("credit: the trial credit is not read yet -- going on without it")

    def refused_for_credit(self) -> None:
        """The provider refused the starter key for its credit (402) mid-conversation: it
        counts as spent now, so the next session is built without a model (make_llm).
        Read soon.

        Sticky -- across reads and restarts, until one shows a real top-up (refresh) --
        only when the refusal came near empty: the last read left sticky_margin() or less
        (LOW_USD, at least STICKY_FLOOR_USD). A key's
        limit is only a cap, the money is the account's: with plenty left on the key the
        refusal is the account's (the vendor's balance behind the starter keys ran dry),
        and a mark of "more than now plus LOW_USD", which a key's remaining never exceeds,
        would keep the box stopped after a refill. That one is left to the next read, and
        so is a refusal before any read (nothing to judge it by)."""
        key = ai_account.current_key()
        if on_starter(key):
            mine = key_hash(key)
            left = self._numbers.get("remaining") if self._for == mine else None
            if left is not None and left <= sticky_margin():
                logger.info("credit: the provider refused the trial credit -- spent")
                seen = _load_notices()
                seen[f"{mine[:16]}:refused"] = float(left)
                _save_notices(seen)
            else:
                logger.info(f"credit: the provider refused the starter key with "
                            f"{left if left is not None else 'an unknown amount'} left on it "
                            "-- the account's refusal; the next read decides")
            self._for, self._state, self._fresh = mine, EMPTY, False
        self.poke()

    def poke(self) -> None:
        """Read again soon: a conversation ended, or the provider refused the account."""
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        """The brain's loop (gateway_server's lifespan): a read now, then every
        POLL_SECS or when poked. A failed read is logged and waited out."""
        self._wake = asyncio.Event()
        self._attempted = asyncio.Event()
        while True:
            read_at = self._mono()
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 -- the loop must outlive a bad read
                logger.warning(f"credit: the read failed: {e!r}")
            self._attempted.set()
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_SECS)
            except asyncio.TimeoutError:
                pass
            # Poked soon after a read: wait out MIN_READ_SECS, every poke meanwhile one.
            await asyncio.sleep(max(0.0, MIN_READ_SECS - (self._mono() - read_at)))
            self._wake.clear()

    # -- telling the owner
    def notice(self, settings: bool = True) -> str | None:
        """The line (a msgid) due at the start of a conversation on the room or Talk, or
        None: HALF_TEXT or LOW_TEXT the first time the credit is in that state, and again
        at most once a day while it stays there -- counted from told(), which the caller
        calls once the line has played (a line cut short is said again next time).
        Nothing where "open my settings" is not heard (`settings` False: the lines tell
        the owner to say it). Empty is not a notice: the box stops (services.make_llm).

        A told time ahead of now is a clock that is behind (a box with no RTC battery
        boots at its last saved time, before NTP sets it): not due, and kept, so the day
        counts from when it was really said. Only one more than REPEAT_SECS ahead -- a
        time written by a clock that was wrong the other way -- is rewritten to now."""
        state = self.state()
        if state not in (HALF, LOW) or not settings:
            return None
        seen = _load_notices()
        key, now = self._notice_key(state), self._clock()
        last = seen.get(key)
        if isinstance(last, (int, float)) and last > now:
            if last - now > REPEAT_SECS:
                seen[key] = now
                _save_notices(seen)
            return None
        if isinstance(last, (int, float)) and now - last < REPEAT_SECS:
            return None
        return HALF_TEXT if state == HALF else LOW_TEXT

    def told(self, msgid: str) -> None:
        """`msgid` (a notice()) was heard to its end: not again for REPEAT_SECS."""
        state = HALF if msgid == HALF_TEXT else LOW if msgid == LOW_TEXT else None
        if state is None or self._for is None:
            return
        seen = _load_notices()
        seen[self._notice_key(state)] = self._clock()
        _save_notices(seen)
        logger.info(f"credit: the owner was told the trial credit is {state}")

    def _notice_key(self, state: str) -> str:
        return f"{self._for[:16]}:{state}"

    def page_line(self, t) -> str | None:
        """The Settings page's line about the trial credit, in t's language, or None when
        the box is not on it (or nothing is known yet)."""
        status = self.status()
        msgid = PAGE_LINES.get(status["state"])
        if msgid is None:
            return None
        remaining = status.get("remaining")
        return t._(msgid).format(amount=f"${remaining:.2f}" if remaining is not None else "$1")


def _load_notices() -> dict:
    try:
        with open(os.path.join(state_dir(), NOTICES), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _save_notices(seen: dict) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        path = os.path.join(state_dir(), NOTICES)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        logger.warning(f"credit: could not keep when the owner was told ({e})")


MONITOR = Monitor()
