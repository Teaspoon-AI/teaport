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
# Fire-and-forget datagrams, as the local audio bridge's face events are: no avatar (or
# one too old to know screens, which ignores them) means nothing shows and nothing
# else changes. No Pipecat import: wifi_setup.py runs alone on a box short of memory.
#
# The avatar draws with Pillow's built-in font unless it was given one (--font), and
# that font is Latin only: text for it is best kept to ASCII — names, digits, and a
# word or two of English. The voice and the phone page speak the user's language.
#
import json
import socket
import threading

# The avatar unit's socket (install.sh renders it there; the local audio bridge's
# LOCAL_AUDIO_FACE_SOCK defaults to the same path).
SOCK = "/run/oled-avatar/face.sock"
# A held screen is re-sent this often with a ttl of HOLD_TTL_SECS: a lost datagram or an
# avatar restart mends at the next send, and a sender that dies without taking its
# screen down (SIGKILL, a power cut) leaves it up for one ttl at most.
REFRESH_SECS = 5.0
HOLD_TTL_SECS = 15.0


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
                 send=send):
        self.id, self._path, self._refresh, self._send = screen_id, path, refresh_secs, send
        self._held: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None

    # Every send is made under the lock (they never block): a refresh cannot land after
    # the clear or the newer show that replaced it.
    def show(self, title: str, lines: list[str], secs: float | None = None) -> None:
        """Up for `secs` seconds, or (None) held until clear() or close()."""
        event = {"screen": {"id": self.id, "title": title, "lines": list(lines),
                            "ttl": secs if secs is not None else HOLD_TTL_SECS}}
        with self._lock:
            self._held = event if secs is None else None
            if self._held and self._thread is None and not self._closed:
                self._thread = threading.Thread(target=self._keep_up, daemon=True,
                                                name=f"screen-{self.id}")
                self._thread.start()
            self._send(event, self._path)

    def clear(self) -> None:
        with self._lock:
            self._held = None
            self._send({"screen": {"id": self.id}}, self._path)

    def close(self) -> None:
        with self._lock:
            held, self._held, self._closed = self._held, None, True
            if held:
                self._send({"screen": {"id": self.id}}, self._path)
        self._wake.set()

    def _keep_up(self) -> None:
        while not self._wake.wait(self._refresh):
            with self._lock:
                if self._held:
                    self._send(self._held, self._path)
