"""display.py: screens on the OLED avatar, sent fire-and-forget."""

import json
import os
import socket
import threading
import time

import pytest

from teaport_brain import display
from tempdirs import tempdir


def _screen(refresh=0.02, features=lambda path: {"screen": 1}):
    """A Screen recording what it sends, to an avatar that draws screens unless told."""
    sent = []
    return display.Screen("t", refresh_secs=refresh, features=features,
                          send=lambda ev, path: sent.append(ev["screen"])), sent


def test_a_held_screen_is_resent_until_it_is_cleared():
    sent, three = [], threading.Event()

    def record(ev, path):
        sent.append(ev["screen"])
        if len(sent) >= 3:
            three.set()

    screen = display.Screen("t", refresh_secs=0.02, send=record, features=lambda path: {"screen": 1})
    try:
        screen.show("Title", ["a", "b"])
        assert three.wait(timeout=5)                      # the show and two refreshes
        screen.clear()
        assert all(s == sent[0] for s in sent[:-1])
        assert sent[0] == {"id": "t", "title": "Title", "lines": ["a", "b"],
                           "ttl": display.HOLD_TTL_SECS}
        n = len(sent)
        time.sleep(0.1)
        assert len(sent) == n and sent[-1] == {"id": "t"}  # nothing after the take-down
    finally:
        screen.close()
    screen._thread.join(timeout=5)
    assert not screen._thread.is_alive()                   # no refresher left behind


def test_a_timed_screen_is_sent_once_and_outlives_close():
    screen, sent = _screen()
    screen.show("Title", ["held"])
    screen.show("Title", ["done"], secs=10)
    screen.close()
    time.sleep(0.1)
    assert sent[-1] == {"id": "t", "title": "Title", "lines": ["done"], "ttl": 10}
    assert sent.count(sent[-1]) == 1


def test_close_takes_a_held_screen_down_and_stops_for_good():
    screen, sent = _screen()
    screen.show("Title", ["held"])
    screen.close()
    assert sent[-1] == {"id": "t"}
    n = len(sent)
    time.sleep(0.1)
    assert len(sent) == n


def test_no_avatar_is_not_an_error():
    assert display.send({"screen": {"id": "t"}}, "/nonexistent/face.sock") is False
    assert display.Screen("t", path="/nonexistent/face.sock").show("T", ["x"], secs=1) is False


def _features(path, text):
    with open(path + display.FEATURES_SUFFIX, "w") as f:
        f.write(text)


def test_an_avatar_that_does_not_say_it_draws_screens_is_sent_nothing():
    # An older avatar takes the datagram, draws nothing and logs it: the password with
    # it. Only its features file tells the two apart.
    path = os.path.join(tempdir(), "face.sock")
    sent = []
    screen = display.Screen("t", path=path, refresh_secs=0.02,
                            send=lambda ev, p: sent.append(ev["screen"]) or True)
    try:
        assert screen.show("T", ["Password  1234"]) is False     # no file
        time.sleep(0.1)                                         # nor any refresh
        screen.clear()
        assert screen.show("T", ["x"], secs=5) is False
        for text in ("not json", "[1]", '{"screen": 0}', '{"mouth": 1}'):
            _features(path, text)
            assert display.supports_screens(path) is False
            assert screen.show("T", ["x"], secs=5) is False
        assert sent == []
        _features(path, '{"screen": 1}')                          # it does now
        assert display.supports_screens(path) is True
        assert screen.show("T", ["x"], secs=5) is True
        assert sent == [{"id": "t", "title": "T", "lines": ["x"], "ttl": 5}]
    finally:
        screen.close()


def test_an_avatar_that_comes_up_mid_hold_gets_the_screen_at_the_next_refresh():
    path = os.path.join(tempdir(), "face.sock")
    sent, got = [], threading.Event()
    screen = display.Screen("t", path=path, refresh_secs=0.02,
                            send=lambda ev, p: sent.append(ev["screen"]) or got.set() or True)
    try:
        assert screen.show("T", ["held"]) is False
        _features(path, '{"screen": 1}')
        assert got.wait(timeout=5) and sent[0]["lines"] == ["held"]
        os.remove(path + display.FEATURES_SUFFIX)               # the avatar went away
        screen.clear()
        assert {"id": "t"} not in sent                          # nobody to tell
    finally:
        screen.close()


def test_the_event_reaches_a_listening_socket():
    path = os.path.join(tempdir(), "face.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(path)
        s.settimeout(2)
        screen = display.Screen("wifi-setup", path=path)
        assert not screen.show("Setup mode", ["Network  x"], secs=5)   # not advertised yet
        _features(path, json.dumps({"screen": 1}))
        assert screen.show("Setup mode", ["Network  x"], secs=5)
        ev = json.loads(s.recv(4096))
        s.setblocking(False)
        with pytest.raises(BlockingIOError):
            s.recv(4096)                                       # the first one was never sent
    assert ev == {"screen": {"id": "wifi-setup", "title": "Setup mode",
                             "lines": ["Network  x"], "ttl": 5}}


@pytest.mark.parametrize("name, shown", [
    ("home", "home"),
    ("Café  Wi-Fi", "Cafe Wi-Fi"),
    ("Ｈｏｍｅ\tNet", "Home Net"),
    ("東京 Net", "Net"),
    ("東京", "your network"),
    ("🏠", "your network"),
    ("  ", "your network"),
])
def test_names_fold_to_printable_ascii(name, shown):
    assert display.fold_ascii(name) == shown
    assert all(" " <= c <= "~" for c in display.fold_ascii(name))


class _Words:
    """i18n.T's p(), in a language whose every word is its own."""

    def p(self, context, msgid):
        return f"«{context}:{msgid}»"


@pytest.mark.parametrize("name", ["Кухня", "東京-box", "Café ☕", "منزلي", "घर", "우리집"])
def test_one_rule_for_screen_text_unicode_or_fold(name):
    """#207: as it is to an avatar that says "unicode", else folded -- names and the
    screen's own words alike, the words in English rather than folded."""
    assert display.screen_text(name, 1) == name
    assert display.screen_text(name, None) == display.fold_ascii(name, "")
    words = display.ScreenText(True, _Words())
    assert words.text(name) == name and words.p("screen", "Network") == "«screen:Network»"
    plain = display.ScreenText(False, _Words())
    assert plain.text(name, "your network") == display.fold_ascii(name)
    assert plain.p("screen", "Network") == "Network"      # English, not a folded translation
    assert display.ScreenText(True).p("screen", "Network") == "Network"   # no reader: English


@pytest.mark.parametrize("features, caller, want", [
    ({"call": 1}, "Zoë Ñúñez", "Zoe Nunez"),
    ({"call": 1, "unicode": 1}, "Zoë Ñúñez", "Zoë Ñúñez"),
    ({"call": 1, "unicode": 1}, "東京", "東京"),
    ({"call": 1}, "東京", None),                      # nothing left: the handset alone
])
def test_the_caller_id_takes_the_same_rule(features, caller, want):
    sent = []
    face = display.CallFace(send=lambda ev, path: sent.append(ev), features=lambda p: features)
    face.ringing("A", caller)
    face.end("A")
    assert sent[0]["call"].get("caller") == want and sent[-1] == {"call": {"state": "none"}}


@pytest.mark.parametrize("text, cols", [
    ("Network  teaport-ab12", 21), ("Пароль", 6), ("パスワード", 10), ("비밀번호", 8),
    ("密码", 4), ("（QRコード）", 12),
    ("पासवर्ड", 6),        # ा beside its letter counts, the virama under one does not
    ("कोशिश", 5), ("कुंजी", 3),
    ("تعذّر", 4),          # the shadda sits on its letter
    ("Cafe\u0301", 4), ("a\u200db", 2),
    ("☕", 2), ("❤\ufe0f", 2), ("❤", 1), ("👍🏽", 2), ("🇯🇵", 2),   # VS16: drawn as an emoji
])
def test_columns_count_wide_letters_twice_and_marks_not_at_all(text, cols):
    assert display.columns(text) == cols


@pytest.mark.parametrize("name, shown", [
    ("Evil\nLine", "Evil Line"),            # a newline would draw a second line over the next
    ("Evil\r\nLine", "Evil Line"), ("a\tb\x00c", "a bc"), ("home\x1b[2J", "home[2J"),
    ("\ud800x", "x"),
    ("\u202eemoh", "emoh"), ("x\u2066y\u2069 \u202az\u202c", "xy z"),   # bidi controls
    ("\n\t", "your network"), ("   ", "your network"),
    ("\u200b", "your network"), ("\u200d \u200c", "your network"),  # marks alone draw nothing
    # A name's own spaces and characters stay: they are how it is written.
    ("東京\u3000カフェ", "東京\u3000カフェ"), ("Café\u00a0Wi-Fi", "Café\u00a0Wi-Fi"),
    ("my  net", "my  net"), (" lead", " lead"), ("trail ", "trail "),
    ("\ue000x", "\ue000x"), ("\U000F0001priv", "\U000F0001priv"),        # private use
    ("क्\u200dष", "क्\u200dष"), ("می\u200cخواهم", "می\u200cخواهم"),     # joiners scripts need
])
def test_what_no_screen_draws_is_dropped_even_where_every_script_is_drawn(name, shown):
    """#207: "unicode" is about fonts, not about controls -- those go either way."""
    assert display.screen_text(name, True, "your network") == shown
    assert display.screen_text(name, False, "your network").isascii()


@pytest.mark.parametrize("text, n, cut", [
    ("abcdefghijklmnopqrstuvक्ष", 24, "abcdefghijklmnopqrstuvक्ष"),
    ("abcdefghijklmnopqrstuvwक्ष", 24, "abcdefghijklmnopqrstuvw"),   # not "...wक्"
    ("abcdefghijklmnopqrstuv👩\u200d👧", 24, "abcdefghijklmnopqrstuv"),  # not "...👩\u200d"
    ("abcdefghijklmnopqrstuvw🇯🇵", 24, "abcdefghijklmnopqrstuvw"),     # not half a flag
    ("abcdefghijklmnopqrstuv🇯🇵", 24, "abcdefghijklmnopqrstuv🇯🇵"),
    ("Café au lait", 4, "Café"), ("Cafe\u0301 au lait", 4, "Cafe\u0301"), ("Zoë", 9, "Zoë"),
    ("東京タワー", 5, "東京"),
])
def test_a_cut_goes_between_letters(text, n, cut):
    assert display.cut(text, n) == cut and display.columns(cut) <= n


def test_a_held_screen_is_folded_once_the_avatar_no_longer_draws_every_script():
    """#207: a screen made for an avatar that said "unicode", held, and re-sent to one
    that restarted without its fallback fonts goes in ASCII; ASCII lines go unchanged."""
    feats, seen, sent = {"screen": 1, "unicode": 1}, [], []

    def features(path):          # what the avatar said for the send that follows
        seen.append(dict(feats))
        return seen[-1]

    screen = display.Screen("t", refresh_secs=0.02, features=features,
                            send=lambda ev, path: sent.append((seen[-1], ev["screen"])))
    screen.show("Режим подключения", ["Сеть  Кухня", "Network  teaport-ab12", "Café  ☕"])
    assert sent[0][1]["title"] == "Режим подключения" and sent[0][1]["lines"][0] == "Сеть  Кухня"
    del feats["unicode"]
    deadline = time.time() + 2
    while time.time() < deadline and sent[-1][0].get("unicode"):
        time.sleep(0.01)
    screen.close(take_down=False)
    assert sent[-1][1]["title"] == "" and sent[-1][1]["lines"] == ["", "Network  teaport-ab12",
                                                                   "Cafe"]
    assert all(json.dumps(ev, ensure_ascii=False).isascii()
               for said, ev in sent if not said.get("unicode"))


def test_a_qr_code_goes_with_the_screen_and_draws_asks_the_avatar():
    sent = []
    screen = display.Screen("t", send=lambda ev, path: sent.append(ev["screen"]) or True,
                            features=lambda path: {"screen": 1, "qr": 1})
    assert screen.show("Pair", ["Code 4821"], secs=5, qr="https://x.example/4821")
    assert sent[-1]["qr"] == "https://x.example/4821"
    assert screen.draws("qr") and screen.draws("screen") and not screen.draws("sound")
    screen.show("Pair", ["Code 4821"], secs=5)
    assert "qr" not in sent[-1]                      # none asked for: no key
    old, _ = _screen(features=lambda path: {"screen": 1})
    assert not old.draws("qr")


def test_features_reads_the_file_and_anything_else_is_none():
    d = tempdir()
    path = os.path.join(d, "face.sock")
    assert display.features(path) == {}
    for content, want in (('{"screen": 1, "qr": 1}', {"screen": 1, "qr": 1}),
                          ("[1]", {}), ("not json", {})):
        with open(path + display.FEATURES_SUFFIX, "w") as f:
            f.write(content)
        assert display.features(path) == want


def test_an_avatars_start_is_its_features_file():
    path = os.path.join(tempdir(), "face.sock")
    assert display.started(path) is None
    for name in ("first", "next"):
        with open(f"{path}.{name}", "w") as f:
            f.write('{"typing": 1}')
    os.replace(path + ".first", path + display.FEATURES_SUFFIX)   # as the avatar writes it
    one = display.started(path)
    assert one is not None and display.started(path) == one
    os.replace(path + ".next", path + display.FEATURES_SUFFIX)    # a new start
    assert display.started(path) not in (None, one)


@pytest.mark.parametrize("feats, text, fits", [
    ({"screen": 1, "qr": 1, "qr_max_bytes": 38}, "WIFI:T:WPA;S:teaport-9e35;P:47190352;;", True),
    ({"screen": 1, "qr": 1, "qr_max_bytes": 37}, "WIFI:T:WPA;S:teaport-9e35;P:47190352;;", False),
    ({"screen": 1, "qr": 1, "qr_max_bytes": 6}, "東京", True),         # bytes, not characters
    ({"screen": 1, "qr": 1, "qr_max_bytes": 5}, "東京", False),
    ({"screen": 1, "qr": 1}, "x", False),                             # no limit said: none fits
    ({"screen": 1, "qr": 1, "qr_max_bytes": "53"}, "x", False),       # nor a garbled one
    ({"screen": 1, "qr_max_bytes": 53}, "x", False),                  # no QR codes at all
    ({}, "x", False),
])
def test_a_qr_code_fits_only_within_what_the_avatar_says_it_draws(feats, text, fits):
    screen, _ = _screen(features=lambda path: feats)
    assert screen.fits_qr(text) is fits


def test_a_screen_left_to_another_sender_is_not_taken_down():
    screen, sent = _screen(refresh=60)
    screen.show("Pair", ["a code"])
    screen.close(take_down=False)
    assert sent == [{"id": "t", "title": "Pair", "lines": ["a code"],
                     "ttl": display.HOLD_TTL_SECS}]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
