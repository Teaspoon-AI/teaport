#
# teaport — text on the box's display: the OLED avatar's screens.
#
# The OLED avatar (github.com/Teaspoon-AI/teaport-oled-avatar, install.sh
# phase_oled_avatar) draws a face, and over it, for as long as a sender wants, a
# "screen": a title and a few lines of text, for whatever a person has to read off the
# box — the setup network's name and password (wifi_setup.py), a pairing code, an
# address to open. This is the sending side, for any teaport process:
#
#     screen = display.Screen("wifi-setup")
#     screen.show("Wi-Fi setup", ["Network  teaport-ab12", "Password  48201937"])
#     ...                                  # held up (re-sent) until
#     screen.close()                       # it is taken down
#     screen.show("Wi-Fi setup", ["Connected"], secs=10)   # or up for a while, alone
#     screen.show("Pair", ["Code 4821"], qr="https://...")  # with a QR code beside or
#                                                           # in turn with the text
#
# Fire-and-forget datagrams, as the local audio bridge's face events are, but only to
# an avatar that says it draws screens: while it serves, it keeps a features file next
# to its socket (FEATURES_SUFFIX: /run/oled-avatar/face.sock.features), JSON such as
# {"screen": 1, "qr": 1, "qr_max_bytes": 53} ("qr": it draws QR codes too, up to that
# many bytes of UTF-8 at a size a phone scans: fits_qr()), and removes it on exit. A
# datagram that is accepted proves nothing: an
# avatar too old to know screens takes it, draws nothing and logs it, password and all.
# So nothing is sent, and show() says False, unless the file names "screen"; it is
# read again before every send (a small file, every few seconds), so an avatar that
# restarts or is upgraded during a setup is seen at the next refresh. No avatar means
# nothing shows and nothing else changes. No Pipecat import: wifi_setup.py runs alone
# on a box short of memory.
#
# The avatar draws with Pillow's built-in font unless it was given one (--font), and
# that font is Latin only: text for it is kept to ASCII — names, digits, and a word or
# two of English; fold_ascii() folds a network name into it. The voice and the phone page
# speak the user's language.
#
import json
import socket
import threading
import unicodedata

# The avatar unit's socket (install.sh renders it there; the local audio bridge's
# LOCAL_AUDIO_FACE_SOCK defaults to this).
SOCK = "/run/oled-avatar/face.sock"
# A held screen is re-sent this often with a ttl of HOLD_TTL_SECS: a lost datagram or an
# avatar restart mends at the next send, and a sender that dies without taking its
# screen down (SIGKILL, a power cut) leaves it up for one ttl at most.
REFRESH_SECS = 5.0
HOLD_TTL_SECS = 15.0
# The avatar's features file is its socket's path plus this.
FEATURES_SUFFIX = ".features"
FEATURES_MAX_BYTES = 4096


def features(path: str = SOCK) -> dict:
    """What the avatar on `path` draws beyond its face, from its features file: {} when
    it is not running, or the file is missing or not a JSON object."""
    try:
        with open(path + FEATURES_SUFFIX, "rb") as f:
            found = json.loads(f.read(FEATURES_MAX_BYTES))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def supports_screens(path: str = SOCK) -> bool:
    """Whether the avatar on `path` is running and draws screens."""
    return bool(features(path).get("screen"))


def fold_ascii(text: str, fallback: str = "your network") -> str:
    """`text` in what the avatar's font draws: accents dropped (Café -> Cafe), any other
    character outside printable ASCII left out, any run of whitespace made one space.
    `fallback` when nothing is left (a name all in Japanese, say, or emoji)."""
    kept = "".join(" " if c.isspace() else c
                   for c in unicodedata.normalize("NFKD", text)
                   if c.isspace() or " " <= c <= "~")
    return " ".join(kept.split()) or fallback


def send(event: dict, path: str = SOCK) -> bool:
    """One event to the avatar. False when it is not there (or not keeping up)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(json.dumps(event).encode(), path)
        return True
    except OSError:
        return False


class Screen:
    """One sender's screen, by id: show() puts it up (replacing what this id showed
    before), clear() takes it down, close() is the sender leaving — a held screen goes
    with it, one shown for a few seconds runs out on its own."""

    def __init__(self, screen_id: str, path: str = SOCK, refresh_secs: float = REFRESH_SECS,
                 send=send, features=features):
        self.id, self._path, self._refresh, self._send = screen_id, path, refresh_secs, send
        self._features = features
        self._held: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None

    # Every send is made under the lock (they never block): a refresh cannot land after
    # the clear or the newer show that replaced it. And every one asks the avatar first:
    # one that does not draw screens is sent nothing, take-downs included.
    def _put(self, event: dict) -> bool:
        return self.draws("screen") and bool(self._send(event, self._path))

    def draws(self, feature: str) -> bool:
        """Whether the avatar is up and draws `feature` ("screen", "qr")."""
        return bool(self._features(self._path).get(feature))

    def fits_qr(self, text: str) -> bool:
        """Whether the avatar draws `text` as a QR code a phone can scan: it draws QR
        codes, and `text` is no longer in UTF-8 than its "qr_max_bytes", the largest it
        draws at a scannable size (a longer one it leaves out, showing the text alone).
        An avatar that does not say: nothing fits."""
        found = self._features(self._path)
        limit = found.get("qr_max_bytes", 0)
        return (bool(found.get("qr")) and isinstance(limit, int)
                and len(text.encode()) <= limit)

    def show(self, title: str, lines: list[str], secs: float | None = None,
             qr: str | None = None) -> bool:
        """Up for `secs` seconds, or (None) held until clear() or close(), with a QR code
        of `qr` if given (an avatar without "qr", or a `qr` too long for it, shows the
        text alone: ask fits_qr() first to know which). True when the
        avatar took it (it is running and draws screens), so the voice can say where to
        look. A held screen is kept even when it is False: an avatar that comes up later
        gets it at the next refresh."""
        event = {"screen": {"id": self.id, "title": title, "lines": list(lines),
                            "ttl": secs if secs is not None else HOLD_TTL_SECS,
                            **({"qr": qr} if qr else {})}}
        with self._lock:
            self._held = event if secs is None else None
            if self._held and self._thread is None and not self._closed:
                self._thread = threading.Thread(target=self._keep_up, daemon=True,
                                                name=f"screen-{self.id}")
                self._thread.start()
            return self._put(event)

    def clear(self) -> None:
        with self._lock:
            self._held = None
            self._put({"screen": {"id": self.id}})

    def close(self) -> None:
        with self._lock:
            held, self._held, self._closed = self._held, None, True
            if held:
                self._put({"screen": {"id": self.id}})
        self._wake.set()

    def _keep_up(self) -> None:
        while not self._wake.wait(self._refresh):
            with self._lock:
                if self._held:
                    self._put(self._held)


# --- The phone line on the face ---------------------------------------------------------
# {"call": {"state": "ringing", "caller": ..., "ttl": ...}} while a call rings (the whole
# panel: a handset and who is calling), "active" while the box is on it (a small handset
# under the left eye), "none" when it is over (the avatar's README, "call"). Held calls are
# re-sent every CALL_REFRESH_SECS with a ttl of CALL_TTL_SECS, so a brain that dies mid-call
# leaves the phone up for one ttl at most. The avatar keeps the face awake while a call is
# up, and asleep again after it if that was the last state it was sent.
#
# Only to an avatar whose features file says "call": one from before calls takes the
# datagram, draws nothing and logs it whole, caller id and all. The caller id is personal
# data: nothing here logs it.
CALL_REFRESH_SECS = 4.0
CALL_TTL_SECS = 10.0
RINGING, ACTIVE = "ringing", "active"


class CallFace:
    """The phone on the avatar's face, for one phone line: ringing(), active(), end().
    Each is for a call id, and end() for another call than the one shown (a stale
    hangup for a call the gateway has already replaced) changes nothing. `state` is what
    is shown (RINGING, ACTIVE or None), whether or not an avatar draws it, and
    `listeners` are called (sync, no arguments, on the caller's thread) when it changes:
    the busy lamp follows the ringing from here."""

    def __init__(self, path: str = SOCK, refresh_secs: float = CALL_REFRESH_SECS,
                 send=send, features=features):
        self._path, self._refresh, self._send, self._features = path, refresh_secs, send, features
        self.state: str | None = None
        self.call_id = None
        self._caller: str | None = None
        self.listeners: list = []
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None

    def ringing(self, call_id, caller: str | None) -> None:
        """Call `call_id` is ringing; `caller` is who it says is calling (None: no caller
        id, the handset alone)."""
        self._show(call_id, RINGING, caller)

    def active(self, call_id) -> None:
        """The box is on call `call_id`."""
        self._show(call_id, ACTIVE, None)

    def end(self, call_id=None) -> None:
        """Call `call_id` is over (None: whatever call is shown, as when the line itself
        goes): the phone comes down."""
        with self._lock:
            if self.state is None or (call_id is not None and call_id != self.call_id):
                return
            self.state, self.call_id, self._caller = None, None, None
            self._put({"call": {"state": "none"}})
        self._kick.set()
        self._changed()

    def _show(self, call_id, state: str, caller: str | None) -> None:
        with self._lock:
            changed = state != self.state
            self.state, self.call_id, self._caller = state, call_id, caller
            if self._thread is None:
                self._thread = threading.Thread(target=self._keep_up, daemon=True,
                                                name="call-face")
                self._thread.start()
            self._put(self._event())
        if changed:
            self._changed()

    def _event(self) -> dict:
        call = {"state": self.state, "ttl": CALL_TTL_SECS}
        if self.state == RINGING and self._caller:
            # Sent with every ringing re-send (the avatar reads it only with ringing). An
            # avatar without fallback fonts draws ASCII only: a name all in other scripts
            # folds to nothing, and the handset is shown alone.
            caller = (self._caller if self._features(self._path).get("unicode")
                      else fold_ascii(self._caller, fallback=""))
            if caller:
                call["caller"] = caller
        return {"call": call}

    # Under the lock, as Screen's: a refresh cannot land after the end that replaced it.
    def _put(self, event: dict) -> None:
        if self._features(self._path).get("call"):
            self._send(event, self._path)

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 — a listener's bug is not the face's
                pass

    def _keep_up(self) -> None:
        # Re-sends the call's state while there is one; gone once it is over (the next
        # call starts another).
        while True:
            self._kick.wait(self._refresh)
            self._kick.clear()
            with self._lock:
                if self.state is None:
                    self._thread = None
                    return
                self._put(self._event())
