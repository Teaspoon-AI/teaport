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
#
# Fire-and-forget datagrams, as the local audio bridge's face events are, but only to
# an avatar that says it draws screens: while it serves, it keeps a features file next
# to its socket (FEATURES_SUFFIX: /run/oled-avatar/face.sock.features), JSON such as
# {"screen": 1}, and removes it on exit. A datagram that is accepted proves nothing: an
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


def supports_screens(path: str = SOCK) -> bool:
    """Whether the avatar on `path` is running and draws screens: its features file
    names "screen". No file, or one that is not a JSON object, is no."""
    try:
        with open(path + FEATURES_SUFFIX, "rb") as f:
            features = json.loads(f.read(FEATURES_MAX_BYTES))
    except (OSError, ValueError):
        return False
    return isinstance(features, dict) and bool(features.get("screen"))


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
                 send=send, supports=supports_screens):
        self.id, self._path, self._refresh, self._send = screen_id, path, refresh_secs, send
        self._supports = supports
        self._held: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None

    # Every send is made under the lock (they never block): a refresh cannot land after
    # the clear or the newer show that replaced it. And every one asks the avatar first:
    # one that does not draw screens is sent nothing, take-downs included.
    def _put(self, event: dict) -> bool:
        return self._supports(self._path) and bool(self._send(event, self._path))

    def show(self, title: str, lines: list[str], secs: float | None = None) -> bool:
        """Up for `secs` seconds, or (None) held until clear() or close(). True when the
        avatar took it (it is running and draws screens), so the voice can say where to
        look. A held screen is kept even when it is False: an avatar that comes up later
        gets it at the next refresh."""
        event = {"screen": {"id": self.id, "title": title, "lines": list(lines),
                            "ttl": secs if secs is not None else HOLD_TTL_SECS}}
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
