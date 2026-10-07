"""display.py: screens on the OLED avatar, sent fire-and-forget."""

import json
import os
import socket
import tempfile
import time

import pytest

from teaport_brain import display


def _screen(refresh=0.02):
    sent = []
    return display.Screen("t", refresh_secs=refresh, send=lambda ev, path: sent.append(ev["screen"])), sent


def test_a_held_screen_is_resent_until_it_is_cleared():
    screen, sent = _screen()
    screen.show("Title", ["a", "b"])
    time.sleep(0.15)
    assert len(sent) >= 3 and all(s == sent[0] for s in sent)
    assert sent[0] == {"id": "t", "title": "Title", "lines": ["a", "b"], "ttl": display.HOLD_TTL_SECS}
    screen.clear()
    n = len(sent)
    time.sleep(0.1)
    assert len(sent) == n and sent[-1] == {"id": "t"}     # nothing after the take-down


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
    display.Screen("t", path="/nonexistent/face.sock").show("T", ["x"], secs=1)


def test_the_event_reaches_a_listening_socket():
    path = os.path.join(tempfile.mkdtemp(), "face.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(path)
        s.settimeout(2)
        display.Screen("wifi-setup", path=path).show("Wi-Fi setup", ["Network  x"], secs=5)
        ev = json.loads(s.recv(4096))
    assert ev == {"screen": {"id": "wifi-setup", "title": "Wi-Fi setup",
                             "lines": ["Network  x"], "ttl": 5}}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
