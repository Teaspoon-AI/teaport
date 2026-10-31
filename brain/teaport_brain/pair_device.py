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
# allows one live offer per process anyway). cancel() takes it down; close(), at the
# session's end, does the same silently -- no task outlives the session, no code stays
# on the screen.
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
SHOWN, NO_SCREEN, UNAVAILABLE = "shown", "no_screen", "unavailable"

# The model's line for each ending (English: it is context). "cancelled" is ours when
# this session took it down (cancel, close, a new offer: no line); one that comes
# anyway was another session's new offer, which took this one's place.
_ENDED = {
    "opened": "the link on the screen was opened, but the box could not confirm that a "
              "device was connected; if the user says it did not work, offer to try again.",
    "expired": "the code on the screen ran out before anyone used it; nothing was paired.",
    "cancelled": "the code was taken off the screen before anyone used it; nothing was "
                 "paired.",
}


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
        self._screen = None
        self._watcher: asyncio.Task | None = None

    @property
    def live(self) -> bool:
        return self._offer is not None

    async def start(self) -> tuple[str, dict]:
        """Put a new code up: (SHOWN, {"minutes": ...}), (NO_SCREEN, {}) or
        (UNAVAILABLE, {"reason": ...}). One already up is replaced."""
        self._end_offer()  # its screen stays up until the new code replaces it
        screen = self._screen or self._new_screen()
        if not screen.draws("qr"):
            # Asked before minting: an offer nobody can see is nothing to open.
            logger.info("pair_device: no screen that draws QR codes")
            self._take_down(screen)
            return NO_SCREEN, {}
        try:
            offer = await pairing.start()
        except pairing.PairingUnavailable as e:
            logger.warning(f"pair_device: no code: {e.reason}")
            self._take_down(screen)
            return UNAVAILABLE, {"reason": e.reason}
        minutes = max(1, math.ceil((offer.expires_at - self._clock()) / 60))
        if not (screen.fits_qr(offer.link)
                and screen.show(SCREEN_TITLE, ["Point your camera", "at this code",
                                               f"Open for {minutes} min"], qr=offer.link)):
            # The avatar went, or draws smaller codes than this one.
            logger.info("pair_device: the screen cannot draw this code")
            pairing.cancel(offer)
            self._take_down(screen)
            return NO_SCREEN, {}
        self._offer, self._screen = offer, screen
        self._watcher = asyncio.create_task(self._watch(offer), name="pair-device")
        logger.info(f"pair_device: code shown for {minutes} min")
        return SHOWN, {"minutes": minutes}

    def cancel(self) -> bool:
        """Take the code down. False when none was up."""
        if not self._end_offer():
            return False
        self._take_down(self._screen)
        logger.info("pair_device: code taken down")
        return True

    def close(self) -> None:
        """The session is over: whatever is up goes, and nothing is told."""
        self.notes = None
        self.cancel()

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
        screen, self._offer, self._screen, self._watcher = self._screen, None, None, None
        logger.info(f"pair_device: {result.outcome}")
        if result.outcome == "paired":
            device = display.fold_ascii(result.device or "", fallback="")[:DEVICE_MAX_CHARS]
            # Up for a while on its own: the held code goes with it, and close() below
            # has nothing held left to take down.
            screen.show(SCREEN_TITLE, ["Paired", device] if device else ["Paired"],
                        secs=PAIRED_SCREEN_SECS)
            screen.close()
            what = f"the user's {result.device}" if result.device else "the user's device"
            self._tell(f"{what} is now connected to the box.", respond=True)
            return
        if result.outcome in _ENDED and result.outcome != "cancelled":
            self._take_down(screen)
        else:
            # Not this session's cancel (those stop the watcher first): another one's new
            # code took this one's place, on the same screen, which stays up for it.
            screen.close(take_down=False)
        self._tell(_ENDED.get(result.outcome, _ENDED["cancelled"]))

    def _tell(self, outcome: str, respond: bool = False) -> None:
        if self.notes is not None:
            self.notes.add_notice(f"Pairing a device ended: {outcome}", respond=respond)

    # -- the screen and the task
    def _end_offer(self) -> bool:
        """Cancel the live offer and stop waiting on it, the screen left as it is. False
        when there was none."""
        offer, watcher = self._offer, self._watcher
        self._offer, self._watcher = None, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
        if offer is None:
            return False
        pairing.cancel(offer)
        return True

    def _take_down(self, screen) -> None:
        if screen is not None:
            screen.clear()
        self._drop(screen)

    def _drop(self, screen) -> None:
        if screen is not None:
            screen.close()
        if screen is self._screen:
            self._screen = None
