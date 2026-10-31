#
# teaport — pairing a laptop or phone, the session half (issue #243; the codes, the
# /p/ link and the device watcher are pairing.py).
#
# The pair_device tool (tools.py) hands to the session's DevicePairing: it asks pairing
# for a code (pairing.start), puts its short link up as a QR code on the box's own
# screen (display.Screen "pair") and returns at once -- the turn is never held for the
# minutes a scan can take. A background task follows the pairing to its end
# (pairing.wait), shows "Finish on your device" once the code on the screen can no
# longer be used while the pairing still waits on something (pairing.spent), "Paired"
# for PAIRED_SCREEN_SECS or takes the screen down, and tells the model how it went: one
# English line through the session's notes (client_notes.py, add_notice), as Wi-Fi
# setup's end does (wifi_voice._ended, issue #97). A device paired asks for a reaction
# too, so the voice says it is connected at the next quiet moment, in the session's
# language, the way the model says everything else; the other endings are context
# only, for the model to mention when it fits.
#
# The code is only ever drawn, never spoken: a Talk user or a voice in the room could be
# overheard, and the link is the owner's access. So without a screen that draws QR codes
# (display.Screen.fits_qr) nothing is asked for and the tool says the box cannot show it.
# The screen text is ASCII English (the avatar's font, display.py), as Wi-Fi setup's is.
#
# Asking again while a pairing goes on puts its new code up (pairing gives a new code in
# the same pairing, which goes on watching for a device the old code brings in), and the
# one watcher goes on following it. Whatever ends a start() -- a code, no code, the call
# cancelled by a barge-in, cancel() or close() meanwhile -- leaves either the new code
# up or nothing new. cancel() takes the code down (a pairing with a handoff made still
# watches, and its end is told); close(), at the session's end, ends the pairing,
# silently and for good -- no task outlives the session, and no code stays up.
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
SHOWN, NO_SCREEN, UNAVAILABLE, STOPPED = "shown", "no_screen", "unavailable", "stopped"
# What cancel() says.
GONE, CONFIRMING, NOTHING = "gone", "confirming", "nothing"

LOCKED, FAILED = pairing.LOCKED, pairing.FAILED

# The model's line for each ending (English: it is context). None: no line -- a
# "cancelled" is ours (cancel(), close()), or pairing's own wait failing, which is
# nothing the user did or needs to hear about.
_ENDED = {
    "opened": "the link on the screen was opened, but the box could not confirm that a "
              "device was connected; if the user says it did not work, offer to try again.",
    "expired": "the code on the screen ran out before anyone used it; nothing was paired.",
    LOCKED: "the code was cancelled after several wrong tries; nothing was paired.",
    # Context only, as "opened" and LOCKED: the person is at their phone, whose page has
    # already told them to ask again.
    FAILED: "the code was used, but the box could not finish connecting the new device; "
            "nothing was paired. If the user asks, offer to try again.",
}

# The screen once the code is used (pairing.spent): the QR code is of no more use, and
# the device is being looked for while the person finishes on it.
SPENT_LINES = ["Finish on", "your device"]


class DevicePairing:
    """The session's pairing: start(), cancel(), close(). `notes` is the session's
    ClientNotes, set once it exists (agent_session.py); None, and the model is told
    nothing of how a pairing ended."""

    def __init__(self, notes=None, screen=lambda: display.Screen(SCREEN_ID),
                 clock=time.monotonic):
        self.notes = notes
        self._new_screen = screen
        self._clock = clock
        self._offer: pairing.Offer | None = None  # the code on the screen
        self._last: pairing.Offer | None = None   # the followed pairing's latest code
        self._screen = None  # this session's, held while a code is up or being asked for
        self._watcher: asyncio.Task | None = None  # follows the pairing (pairing.wait)
        self._spent: asyncio.Task | None = None    # follows the code on the screen
        self._watched = 0                          # the pairing the watcher follows
        # Bumped by every start(), cancel() and close(): a start() that finds it moved
        # on shows nothing.
        self._gen = 0
        self._cancels = 0  # cancel()s: a start() it came during makes its code unusable
        self._closed = False

    @property
    def live(self) -> bool:
        """A pairing is being followed (a code up, or a handoff's device awaited)."""
        return self._watcher is not None

    async def start(self) -> tuple[str, dict]:
        """Put a code up: (SHOWN, {"minutes": ...}), (NO_SCREEN, {}), (STOPPED, {}:
        cancel(), close() or a newer start() came first) or (UNAVAILABLE, {"reason": ...}).
        One already up is replaced, in the same pairing."""
        if self._closed:
            return STOPPED, {}
        self._gen += 1
        mine, cancels = self._gen, self._cancels
        screen = self._screen = self._screen or self._new_screen()
        if not screen.draws("qr"):
            # Asked before anything is made: a code nobody can see is nothing to open.
            logger.info("pair_device: no screen that draws QR codes")
            self._withdraw()
            return NO_SCREEN, {}
        try:
            offer = await pairing.start()
        except pairing.PairingUnavailable as e:
            logger.warning(f"pair_device: no code: {e.reason}")
            if mine != self._gen:
                return STOPPED, {}
            if self._offer is None and not self.live:
                self._take_down()
            return UNAVAILABLE, {"reason": e.reason}
        except BaseException:
            if mine == self._gen and self._offer is None and not self.live:
                self._take_down()  # a barge-in cancelled the call, or worse
            raise
        if mine != self._gen or self._closed:
            # close() or cancel() came meanwhile: this code is not to be used. (A newer
            # start() alone needs nothing: its code replaces this one.)
            if self._closed:
                pairing.cancel(offer)
            elif self._cancels != cancels:
                pairing.withdraw(offer)
            logger.info("pair_device: stopped while the code was being made")
            return STOPPED, {}
        minutes = max(1, math.ceil((offer.expires_at - self._clock()) / 60))
        if not (screen.fits_qr(offer.link)
                and screen.show(SCREEN_TITLE, ["Point your camera", "at this code",
                                               f"Open for {minutes} min"], qr=offer.link)):
            # The avatar went, or draws smaller codes than this one.
            logger.info("pair_device: the screen cannot draw this code")
            self._offer = offer
            self._withdraw()
            return NO_SCREEN, {}
        self._show(offer)
        logger.info(f"pair_device: code shown for {minutes} min")
        return SHOWN, {"minutes": minutes}

    def _show(self, offer: pairing.Offer) -> None:
        """`offer` is the code on the screen: follow its pairing (one watcher per
        pairing) and its code (one spent() per code)."""
        self._offer, self._last = offer, offer
        if self._spent is not None:
            self._spent.cancel()
        self._spent = asyncio.create_task(self._on_spent(offer), name="pair-device-spent")
        if self._watcher is not None and self._watched != offer.session:
            # A new pairing: the last one ended just before it was asked for, and its
            # watcher had not run yet. Its end is old news beside the new code.
            self._watcher.cancel()
            self._watcher = None
        if self._watcher is None:
            self._watched = offer.session
            self._watcher = asyncio.create_task(self._watch(offer), name="pair-device")

    def _withdraw(self) -> bool:
        """The code comes off the screen (pairing.withdraw): True when the pairing goes
        on watching for a handoff's device."""
        offer, self._offer = self._offer, None
        if self._spent is not None:
            self._spent.cancel()
            self._spent = None
        confirming = offer is not None and pairing.withdraw(offer)
        self._take_down()
        return confirming

    def cancel(self) -> str:
        """Take the code off the screen, and stop one still being made: GONE, or NOTHING
        when there was neither. CONFIRMING: a handoff was made, so the device is still
        looked for, and the outcome is told as usual (pairing.withdraw)."""
        self._gen += 1
        self._cancels += 1
        had = self._offer is not None or self._screen is not None
        confirming = self._withdraw()
        if had:
            logger.info("pair_device: code taken down"
                        + (", still confirming the device" if confirming else ""))
        return CONFIRMING if confirming else GONE if had else NOTHING

    def close(self) -> None:
        """The session is over: the pairing ends (a confirmation too: no task outlives
        the session), nothing is told, and nothing is shown after."""
        self._closed = True
        self.notes = None
        self._gen += 1
        last, self._last, self._offer = self._last, None, None
        for task in (self._spent, self._watcher):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._spent = self._watcher = None
        if last is not None:
            pairing.cancel(last)
        self._take_down()

    # -- the pairing's end
    async def _on_spent(self, offer: pairing.Offer) -> None:
        """The code on the screen can no longer be used, and the pairing still waits on
        something: the QR code makes way for SPENT_LINES until the end."""
        if await pairing.spent(offer) and self._offer is offer and self._screen is not None:
            self._screen.show(SCREEN_TITLE, SPENT_LINES)

    async def _watch(self, offer: pairing.Offer) -> None:
        try:
            result = await pairing.wait(offer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the screen must still come down
            logger.warning(f"pair_device: waiting on the pairing failed: {e!r}")
            result = pairing.Result(pairing.CANCELLED)
        if self._watcher is not asyncio.current_task():
            return  # close() meanwhile
        self._watcher = None
        if self._spent is not None:
            self._spent.cancel()
            self._spent = None
        self._offer = None
        logger.info(f"pair_device: {result.outcome}")
        screen, self._screen = self._screen, None
        if result.outcome == pairing.PAIRED:
            # A screen taken down meanwhile (cancel() while the device was confirmed)
            # still says so.
            screen = screen or self._new_screen()
            device = display.fold_ascii(result.device or "", fallback="")[:DEVICE_MAX_CHARS]
            # Up for a while on its own: the held code goes with it, and close() below
            # has nothing held left to take down.
            screen.show(SCREEN_TITLE, ["Paired", device] if device else ["Paired"],
                        secs=PAIRED_SCREEN_SECS)
            screen.close()
            # Said to the user as it stands more often than not (the order asks for one
            # plain sentence, and the model may just read the notice out), so it is
            # written to be heard: no "the user's", no client ids (pairing.device_name).
            self._tell(f"Your {result.device} is now connected." if result.device
                       else "Your new device is now connected.", respond=True)
            return
        if screen is not None:
            screen.clear()
            screen.close()
        if _ENDED.get(result.outcome):
            self._tell(_ENDED[result.outcome])

    def _tell(self, outcome: str, respond: bool = False) -> None:
        if self.notes is not None:
            # A spoken one (respond) stands alone; a context-only one says what it is about.
            self.notes.add_notice(outcome if respond else f"Pairing a device ended: {outcome}",
                                  respond=respond)

    # -- the screen
    def _take_down(self) -> None:
        """This session's screen, held or not, comes down and is let go."""
        screen, self._screen = self._screen, None
        if screen is not None:
            screen.clear()
            screen.close()
