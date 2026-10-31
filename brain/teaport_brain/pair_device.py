#
# teaport — pairing a laptop or phone, the session half (issue #243; the codes, the
# /p/ link and the device watcher are pairing.py).
#
# The pair_device tool (tools.py) hands to the session's DevicePairing: it mints an
# offer (pairing.start), puts its short link up as a QR code on the box's own screen
# (display.Screen "pair") and returns at once -- the turn is never held for the minutes
# a scan can take. A background task waits for the offer's end (pairing.wait), shows
# "Paired" for PAIRED_SCREEN_SECS or takes the screen down, and tells the model how it
# went: one English line through the session's notes (client_notes.py, add_notice), as
# Wi-Fi setup's end does (wifi_voice._ended, issue #97). A device paired asks for a
# reaction too, so the voice says it is connected at the next quiet moment, in the
# session's language, the way the model says everything else; the other endings are
# context only, for the model to mention when it fits.
#
# The code is only ever drawn, never spoken: a Talk user or a voice in the room could be
# overheard, and the link is the owner's access. So without a screen that draws QR codes
# (display.Screen.fits_qr) nothing is minted and the tool says the box cannot show it.
# The screen text is ASCII English (the avatar's font, display.py), as Wi-Fi setup's is.
#
# One offer per session: a second pair_device call while one is up replaces it (pairing
# allows one live offer per process anyway; its start() ends the one before, or says it
# is still confirming a device that opened the last link, and then the old one stays).
# The old code stays on the screen while the new one is minted (half a minute on the
# box), and whatever ends the mint -- a code, no code, the call cancelled by a barge-in,
# cancel() or close() meanwhile -- leaves either the new code up or nothing: never a dead
# code re-sent forever. cancel() takes it down; close(), at the session's end, does the
# same silently and for good -- no task outlives the session, no code stays on the
# screen, and a mint still running then shows nothing.
#
import asyncio
import math
import time

from loguru import logger

from teaport_brain import display, pairing

SCREEN_ID = "pair"
SCREEN_TITLE = "Pair"
PAIRED_SCREEN_SECS = 10.0
DEVICE_MAX_CHARS = 24

# What start() says.
SHOWN, NO_SCREEN, UNAVAILABLE, BUSY, STOPPED = (
    "shown", "no_screen", "unavailable", "busy", "stopped")
# What cancel() says.
GONE, CONFIRMING, NOTHING = "gone", "confirming", "nothing"

# pairing's outcomes beyond the four of its Result: another start() (another session's,
# ours never reach the watcher) replaced the code, or wrong codes tried at /p/ ended it.
SUPERSEDED = getattr(pairing, "SUPERSEDED", "superseded")
LOCKED = getattr(pairing, "LOCKED", "locked")

# The model's line for each ending (English: it is context). None: no line -- a
# "cancelled" this session did not cause is pairing's own (its wait failed, say), which
# is nothing the user did or needs to hear about.
_ENDED = {
    "opened": "the link on the screen was opened, but the box could not confirm that a "
              "device was connected; if the user says it did not work, offer to try again.",
    "expired": "the code on the screen ran out before anyone used it; nothing was paired.",
    LOCKED: "the code was cancelled after several wrong tries; nothing was paired.",
    SUPERSEDED: "a newer pairing request replaced the code on the screen; nothing was "
                "paired here.",
}


def _still_confirming(e: "pairing.PairingUnavailable") -> bool:
    """pairing.start() refused because a device that opened the last link is still
    being confirmed (that offer stays live)."""
    named = getattr(pairing, "STILL_CONFIRMING", None)
    return e.reason == named if named else "confirming" in e.reason.lower()


class DevicePairing:
    """The session's pairing offer: start(), cancel(), close(). `notes` is the session's
    ClientNotes, set once it exists (agent_session.py); None, and the model is told
    nothing of how an offer ended."""

    def __init__(self, notes=None, screen=lambda: display.Screen(SCREEN_ID),
                 clock=time.monotonic):
        self.notes = notes
        self._new_screen = screen
        self._clock = clock
        self._offer: pairing.Offer | None = None
        self._screen = None  # this session's, held while a code is up or being minted
        self._watcher: asyncio.Task | None = None
        # Bumped by every start(), cancel() and close(): a mint that finds it moved on
        # shows nothing.
        self._gen = 0
        self._shown_gen = 0  # _gen when the live offer went up
        self._minting = 0
        self._closed = False

    @property
    def live(self) -> bool:
        return self._offer is not None

    async def start(self) -> tuple[str, dict]:
        """Put a new code up: (SHOWN, {"minutes": ...}), (NO_SCREEN, {}), (BUSY, {}: the
        last device is still being confirmed), (STOPPED, {}: cancel() or a newer start()
        came first) or (UNAVAILABLE, {"reason": ...}). One already up is replaced."""
        if self._closed:
            return STOPPED, {}
        self._gen += 1
        mine = self._gen
        screen = self._screen = self._screen or self._new_screen()
        if not screen.draws("qr"):
            # Asked before minting: an offer nobody can see is nothing to open.
            logger.info("pair_device: no screen that draws QR codes")
            self._end_offer()
            self._take_down()
            return NO_SCREEN, {}
        self._minting += 1
        try:
            offer = await pairing.start()
        except pairing.PairingUnavailable as e:
            logger.warning(f"pair_device: no code: {e.reason}")
            self._after_failed_mint(mine)
            if mine != self._gen:
                return STOPPED, {}  # cancelled, or a newer start() of ours is the one
            return (BUSY, {}) if _still_confirming(e) else (UNAVAILABLE, {"reason": e.reason})
        except BaseException:
            self._after_failed_mint(mine)  # a barge-in cancelled the call, or worse
            raise
        finally:
            self._minting -= 1
        if mine != self._gen or self._closed:
            pairing.cancel(offer)
            logger.info("pair_device: stopped while the code was being made")
            return STOPPED, {}
        minutes = max(1, math.ceil((offer.expires_at - self._clock()) / 60))
        if not (screen.fits_qr(offer.link)
                and screen.show(SCREEN_TITLE, ["Point your camera", "at this code",
                                               f"Open for {minutes} min"], qr=offer.link)):
            # The avatar went, or draws smaller codes than this one.
            logger.info("pair_device: the screen cannot draw this code")
            pairing.cancel(offer)
            self._end_offer()
            self._take_down()
            return NO_SCREEN, {}
        self._end_offer(cancel=False)  # pairing.start() has ended the one before
        self._offer, self._shown_gen = offer, mine
        self._watcher = asyncio.create_task(self._watch(offer), name="pair-device")
        logger.info(f"pair_device: code shown for {minutes} min")
        return SHOWN, {"minutes": minutes}

    def _after_failed_mint(self, mine: int) -> None:
        """No new code: the screen comes down, unless a newer start() of ours owns it now
        or the old code is still live (pairing kept it: still confirming). An old offer
        pairing.start() superseded before it failed may not have reached its watcher yet
        (a CLI that is not there fails without a pause): the watcher then takes the
        screen down itself, as the end of our own (_watch)."""
        if mine == self._gen and self._offer is None:
            self._take_down()

    def cancel(self) -> str:
        """Take the code off the screen, and stop one still being made: GONE, or NOTHING
        when there was neither. CONFIRMING: its link was already opened, so the device
        is still being looked for, and the outcome is told as usual (pairing.withdraw)."""
        self._gen += 1
        confirming = self._offer is not None and pairing.withdraw(self._offer)
        # withdraw() has already cancelled an offer whose link was not opened.
        had = (confirming or self._end_offer(cancel=False) or self._minting > 0
               or self._screen is not None)
        self._take_down()
        if had:
            logger.info("pair_device: code taken down"
                        + (", still confirming the device" if confirming else ""))
        return CONFIRMING if confirming else GONE if had else NOTHING

    def close(self) -> None:
        """The session is over: whatever is up goes (a confirmation too: no task outlives
        the session), nothing is told, and nothing is shown after."""
        self._closed = True
        self.notes = None
        self._gen += 1
        self._end_offer()
        self._take_down()

    # -- the offer's end
    async def _watch(self, offer: pairing.Offer) -> None:
        try:
            result = await pairing.wait(offer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the screen must still come down
            logger.warning(f"pair_device: waiting on the code failed: {e!r}")
            result = pairing.Result("cancelled")
        if self._offer is not offer:
            return  # replaced or taken down meanwhile
        self._offer, self._watcher = None, None
        logger.info(f"pair_device: {result.outcome}")
        ours = self._gen != self._shown_gen  # a start() or cancel() of ours since
        if result.outcome in ("cancelled", SUPERSEDED) and (self._minting or ours):
            # Our own new code ended this one: its screen stays up while the new one is
            # made (the mint's end takes it down if none comes), or comes down now when
            # that mint is already over without one. Nothing to tell.
            if not self._minting:
                self._take_down()
            return
        screen, self._screen = self._screen, None
        if result.outcome == "paired":
            # A screen taken down meanwhile (cancel() while the device was confirmed)
            # still says so.
            screen = screen or self._new_screen()
            device = display.fold_ascii(result.device or "", fallback="")[:DEVICE_MAX_CHARS]
            # Up for a while on its own: the held code goes with it, and close() below
            # has nothing held left to take down.
            screen.show(SCREEN_TITLE, ["Paired", device] if device else ["Paired"],
                        secs=PAIRED_SCREEN_SECS)
            screen.close()
            what = f"the user's {result.device}" if result.device else "the user's device"
            self._tell(f"{what} is now connected to the box.", respond=True)
            return
        if screen is None:
            pass  # already down (cancel() while the device was confirmed)
        elif result.outcome == SUPERSEDED:
            # Another session's new code is on the same screen now: not ours to take down.
            screen.close(take_down=False)
        else:
            screen.clear()
            screen.close()
        if _ENDED.get(result.outcome):
            self._tell(_ENDED[result.outcome])

    def _tell(self, outcome: str, respond: bool = False) -> None:
        if self.notes is not None:
            self.notes.add_notice(f"Pairing a device ended: {outcome}", respond=respond)

    # -- the screen and the task
    def _end_offer(self, cancel: bool = True) -> bool:
        """Stop waiting on the live offer and (`cancel`) cancel it, the screen left as it
        is. False when there was none."""
        offer, watcher = self._offer, self._watcher
        self._offer, self._watcher = None, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
        if offer is None:
            return False
        if cancel:
            pairing.cancel(offer)
        return True

    def _take_down(self) -> None:
        """This session's screen, held or not, comes down and is let go."""
        screen, self._screen = self._screen, None
        if screen is not None:
            screen.clear()
            screen.close()
