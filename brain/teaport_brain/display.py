#
# teaport — text on the box's display: the OLED avatar's screens.
#
# The OLED avatar (a display daemon on the face socket; Teaspoon AI appliances come with
# one) draws a face, and over it, for as long as a sender wants, a
# "screen": a title and a few lines of text, for whatever a person has to read off the
# box — the setup network's name and password (wifi_setup.py), a pairing code, an
# address to open. This is the sending side, for any teaport process:
#
#     screen = display.Screen("wifi-setup")
#     screen.show("Setup mode", ["Network  teaport-ab12", "Password  48201937"])
#     ...                                  # held up (re-sent) until
#     screen.close()                       # it is taken down
#     screen.show("Setup mode", ["Connected"], secs=10)   # or up for a while, alone
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
# Text in the user's script only where the avatar draws it (#207). The avatar's own font
# (Pillow's built-in one, Aileron, unless it was given one with --font) is ASCII only;
# where the box has fallback fonts (DejaVu Sans, Noto Sans and its Arabic, Devanagari and
# CJK families, ...) it draws every other character with them, and its features file
# says "unicode". One rule for every text the brain puts on a screen, screen_text(): as it
# is to an avatar that says "unicode", else folded into ASCII (fold_ascii). A screen's own
# words (a title, "Network") go through ScreenText: in the reader's language where the
# avatar says "unicode", else in English -- folded, a translation would be nothing
# ("Пароль") or wrong. A held screen is re-sent to whatever avatar is there by then: one
# that no longer says "unicode" gets what it cannot draw folded (Screen._put). A box
# without those fonts gets ASCII English screens, as before.
# The voice and the phone page speak the user's language whatever the screen draws.
#
import json
import os
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
# The avatar reads each event with recvfrom(4096): a longer datagram arrives cut short
# and is dropped as bad JSON.
DATAGRAM_MAX_BYTES = 4096


def features(path: str = SOCK) -> dict:
    """What the avatar on `path` draws beyond its face, from its features file: {} when
    it is not running, or the file is missing or not a JSON object."""
    try:
        with open(path + FEATURES_SUFFIX, "rb") as f:
            found = json.loads(f.read(FEATURES_MAX_BYTES))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def started(path: str = SOCK):
    """Which start of the avatar on `path` is serving: each one writes its features file
    anew once its socket is bound (a new file renamed into place), so a change is a new
    daemon, which has forgotten all it was told. None when there is no file."""
    try:
        st = os.stat(path + FEATURES_SUFFIX)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


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


# What no screen draws as text, whatever its fonts: controls (a newline would start a
# second line over the next one), surrogates, private-use characters, and the bidi
# embedding, override and isolate controls (U+202A-202E, U+2066-2069), which would turn
# the rest of the line around. The marks and joiners scripts need (ZWJ, ZWNJ) stay.
_NOT_TEXT = frozenset(("Cc", "Cs", "Co"))
_BIDI_CONTROLS = frozenset(map(chr, (*range(0x202A, 0x202F), *range(0x2066, 0x206A))))


def screen_text(text: str, unicode, fallback: str = "") -> str:
    """`text` (a network's name, a caller id) as it goes on a screen: as it is when the
    avatar says "unicode" (`unicode`: its features' value, or whether it draws "unicode"),
    but for what no screen draws (_NOT_TEXT, _BIDI_CONTROLS) and with every run of
    whitespace one space; else folded into ASCII (fold_ascii). `fallback` when nothing is
    left. The one rule for text on the avatar (#207): every sender asks it, so no two
    can disagree."""
    if not unicode:
        return fold_ascii(text, fallback)
    kept = "".join(" " if c.isspace() else c for c in text
                   if c.isspace() or not (unicodedata.category(c) in _NOT_TEXT
                                          or c in _BIDI_CONTROLS))
    return " ".join(kept.split()) or fallback


def _ascii(text: str) -> str:
    return text if text.isascii() else fold_ascii(text, "")


class ScreenText:
    """One screen's text for the avatar as it is when the screen is made: names through
    screen_text(), and the screen's own words through p(), as i18n.T.p() -- in `t`'s
    language (an i18n.T) to an avatar that says "unicode", else in English, the msgid
    itself. Made once per screen, so its words and its names agree."""

    def __init__(self, unicode, t=None):
        self.unicode, self._t = bool(unicode), t

    def text(self, text: str, fallback: str = "") -> str:
        return screen_text(text, self.unicode, fallback)

    def p(self, context: str, msgid: str) -> str:
        return self._t.p(context, msgid) if self.unicode and self._t else msgid


_VS16, _ZWJ, _ZWNJ = "\ufe0f", "\u200d", "\u200c"


def _is_regional(c: str) -> bool:
    return "\U0001F1E6" <= c <= "\U0001F1FF"


def _is_skin_tone(c: str) -> bool:
    return "\U0001F3FB" <= c <= "\U0001F3FF"


def _width(c: str) -> int:
    if unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def columns(text: str) -> int:
    """About how much of a line `text` takes, in columns of the avatar's 10 px font (an
    average Latin letter): a CJK character, a Hangul syllable or an emoji (East Asian wide
    or fullwidth, or drawn as an emoji: VS16 after it) two; a mark drawn on or under its
    letter (an accent, a virama, a vowel sign above or below) or an invisible joiner none;
    anything else one, a vowel sign beside its letter (ा ि ी ो) too. An estimate, for the
    length budgets of the screens' own words (wifi_setup.SCREEN_WORD_COLUMNS) and for
    cut(): the panel's own check is the avatar's."""
    total = last = 0
    for c in text:
        if c == _VS16:
            total += 1 if last == 1 else 0   # the character before it, drawn as an emoji
            last = 2
            continue
        if _is_skin_tone(c) and last:
            continue                         # it colors the emoji before it
        width = _width(c)
        total += width
        last = width or last
    return total


def _clusters(text: str) -> list[str]:
    """`text` in the pieces a cut must not split (near enough Unicode's grapheme
    clusters): a letter with its marks, a conjunct (a virama joins the next letter), an
    emoji sequence (ZWJ, VS16), a flag (two regional indicators)."""
    out: list[str] = []
    for c in text:
        prev = out[-1][-1] if out else ""
        if out and (unicodedata.category(c) in ("Mn", "Mc", "Me") or c in (_ZWJ, _ZWNJ)
                    or prev == _ZWJ
                    or (unicodedata.combining(prev) == 9                 # 9: a virama
                        and unicodedata.category(c).startswith("L"))
                    or _is_skin_tone(c)
                    or (_is_regional(c) and len(out[-1]) == 1 and _is_regional(prev))):
            out[-1] += c
        else:
            out.append(c)
    return out


def cut(text: str, max_columns: int) -> str:
    """`text` cut to at most `max_columns` (columns()), between clusters (_clusters): no
    letter loses its marks, and no virama, joiner or half a flag is left at the end."""
    kept, used = [], 0
    for cluster in _clusters(text):
        used += columns(cluster)
        if used > max_columns:
            break
        kept.append(cluster)
    return "".join(kept).rstrip()


def send(event: dict, path: str = SOCK) -> bool:
    """One event to the avatar. False when it is not there (or not keeping up)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            # UTF-8 as is, not \u-escaped: the avatar reads at most DATAGRAM_MAX_BYTES,
            # and an escape is six bytes where UTF-8 takes three.
            # A lone surrogate (JSON from the far end can carry one) is replaced, not raised.
            s.sendto(json.dumps(event, ensure_ascii=False).encode("utf-8", "replace"), path)
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
        found = self._features(self._path)
        if not found.get("screen"):
            return False
        if not found.get("unicode") and "lines" in event["screen"]:
            # Made for an avatar that drew every script, held, and now going to one that
            # does not (it restarted without its fallback fonts): what it cannot draw is
            # folded on the way (#207). ASCII goes as it is, so the screens made for such
            # an avatar go byte for byte.
            screen = event["screen"]
            event = {"screen": {**screen, "title": _ascii(screen.get("title", "")),
                                "lines": [_ascii(line) for line in screen["lines"]]}}
        return bool(self._send(event, self._path))

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

    def close(self, take_down: bool = True) -> None:
        """`take_down` False: the sender leaves a held screen to whoever now shows one by
        the same id (it is no longer this sender's to take down)."""
        with self._lock:
            held, self._held, self._closed = self._held, None, True
            if held and take_down:
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
# The avatar cuts a caller id to this many characters (its CALLER_MAX_CHARS); cut here
# first, so a far end's long display name can never push the datagram past
# DATAGRAM_MAX_BYTES (48 characters are 192 bytes of UTF-8 at most).
CALLER_MAX_CHARS = 48
RINGING, ACTIVE = "ringing", "active"


class CallFace:
    """The phone on the avatar's face, for one phone line: ringing(), active(), end().
    Each is for a call id, and end() for another call than the one shown (a stale
    hangup for a call the gateway has already replaced) changes nothing. A call that
    rings while another is active covers it: if the ringing one ends without being
    answered, the face goes back to the active call underneath rather than to none;
    answered, it replaces it. `state` is what
    is shown (RINGING, ACTIVE or None), whether or not an avatar draws it, and
    `listeners` are called (sync, no arguments, on the caller's thread) when it changes:
    the busy lamp follows the ringing from here."""

    def __init__(self, path: str = SOCK, refresh_secs: float = CALL_REFRESH_SECS,
                 send=send, features=features):
        self._path, self._refresh, self._send, self._features = path, refresh_secs, send, features
        self.state: str | None = None
        self.call_id = None
        self._caller: str | None = None
        self._under = None  # the active call a ringing one covers
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
            if call_id is not None and call_id == self._under:
                self._under = None  # gone underneath the ringing call: nothing shows
                return
            if self.state is None or (call_id is not None and call_id != self.call_id):
                return
            if call_id is not None and self._under is not None:
                # The ringing call went unanswered: back to the call still up.
                self.state, self.call_id, self._caller = ACTIVE, self._under, None
                self._under = None
                self._put(self._event())
            else:
                self.state, self.call_id, self._caller, self._under = None, None, None, None
                self._put({"call": {"state": "none"}})
        self._kick.set()
        self._changed()

    def _show(self, call_id, state: str, caller: str | None) -> None:
        with self._lock:
            changed = state != self.state
            if state == RINGING and self.state == ACTIVE and call_id != self.call_id:
                self._under = self.call_id
            elif state == ACTIVE:
                self._under = None  # answered: it replaces whatever it covered
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
            # Sent with every ringing re-send (the avatar reads it only with ringing), and
            # asked of the avatar each time (screen_text): to one without fallback fonts,
            # a name all in other scripts folds to nothing, and the handset is shown alone.
            caller = screen_text(self._caller, self._features(self._path).get("unicode"))
            if caller:
                call["caller"] = caller[:CALLER_MAX_CHARS]
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
