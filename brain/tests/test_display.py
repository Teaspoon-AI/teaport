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
        assert not screen.show("Wi-Fi setup", ["Network  x"], secs=5)   # not advertised yet
        _features(path, json.dumps({"screen": 1}))
        assert screen.show("Wi-Fi setup", ["Network  x"], secs=5)
        ev = json.loads(s.recv(4096))
        s.setblocking(False)
        with pytest.raises(BlockingIOError):
            s.recv(4096)                                       # the first one was never sent
    assert ev == {"screen": {"id": "wifi-setup", "title": "Wi-Fi setup",
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
