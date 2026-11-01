#!/usr/bin/env python3
# test_install_frontdoor.py — install.sh's front-door names and certificates (#251, #249).
#
# The box's identifier (box_id), the front door's host and its local CA's name
# (resolve_frontdoor, frontdoor_name_taken), the operator's own certificate
# (check_own_cert) and the Caddyfile that serves one or the other (write_caddyfile).
#
# The functions are taken out of install.sh as they are and run by bash with the commands
# around them stubbed (sudo runs the command itself, getent and hostname answer from the
# test), against a temp dir; certificates are real ones made with openssl. With a caddy
# binary on PATH (or CADDY_BIN), both Caddyfiles are also checked by `caddy adapt`.
# Nothing outside a temp dir is touched.
#
# Run: python test_install_frontdoor.py   (or via pytest test_suite.py)
#
import os
import pathlib
import shutil
import subprocess

import appliance
from tempdirs import tempdir

INSTALL = pathlib.Path(__file__).resolve().parents[2] / "install.sh"

STUBS = r'''
set -euo pipefail
DRY_RUN=0 BRAIN_PORT=7861 GATEWAY_PORT=18789 ETC="$T/etc" CADDY_PKI="$T/pki"
FRONTDOOR_HOST="${TEAPORT_HOST:-teaport.local}" FRONTDOOR_CA_NAME=""
TLS_CERT="${TEAPORT_TLS_CERT:-}" TLS_KEY="${TEAPORT_TLS_KEY:-}" REGENERATE_CA="${TEAPORT_REGENERATE_CA:-0}"
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
sudo() { "$@"; }
SUDO() { case "$1" in tee) command tee "$2" >/dev/null ;; *) "$@" ;; esac; }
read_env_file() { cat "$1" 2>/dev/null || true; }
timeout() { shift; "$@"; }
# mDNS: $T/mdns holds the addresses <host> resolves to, one per line; none, it does not.
getent() { [ -s "$T/mdns" ] || return 2; while read -r a; do printf "%s STREAM %s\n" "$a" "$2"; done < "$T/mdns"; }
hostname() { printf '192.168.1.105 192.168.1.234 \n'; }
'''


def _fn(name: str) -> str:
    """One function's text out of install.sh, from `name() {` to its closing brace."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


FNS = ("box_id", "frontdoor_name_taken", "resolve_frontdoor", "check_own_cert", "write_caddyfile")


def _run(tmp, script, env=None):
    body = STUBS + "".join(_fn(f) for f in FNS) + script
    e = {**os.environ, "T": str(tmp), "SYSFS_NET": str(pathlib.Path(tmp, "net")), **(env or {})}
    for k in ("TEAPORT_HOST", "TEAPORT_TLS_CERT", "TEAPORT_TLS_KEY", "TEAPORT_REGENERATE_CA"):
        e.setdefault(k, "")
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def _iface(tmp, name, mac, wireless=False, device=True):
    d = pathlib.Path(tmp, "net", name)
    d.mkdir(parents=True)
    (d / "address").write_text(mac + "\n")
    if wireless:
        (d / "wireless").mkdir()
    if device:
        (d / "device").mkdir()


def _box(tmp):
    """A box like the appliance: wired, docker, and Wi-Fi ending 9e35."""
    _iface(tmp, "docker0", "02:42:ac:11:00:01", device=False)
    _iface(tmp, "enP8p1s0", "48:b0:2d:00:11:22")
    _iface(tmp, "wlP1p1s0", "F8:3D:C6:1F:9E:35", wireless=True)


def _cert(tmp, name, cn, sans=("teaport.local",), days=30, key=None):
    """A self-signed certificate (and its key unless `key` names one to reuse)."""
    crt, k = pathlib.Path(tmp, f"{name}.crt"), key or pathlib.Path(tmp, f"{name}.key")
    args = ["openssl", "req", "-x509", "-nodes", "-subj", f"/CN={cn}", "-out", str(crt)]
    if key:
        args += ["-key", str(k)]
    else:
        args += ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-keyout", str(k)]
    args += ["-days", str(days)]
    if sans:
        args += ["-addext", "subjectAltName=" + ",".join(f"DNS:{s}" for s in sans)]
    subprocess.run(args, check=True, capture_output=True)
    return crt, k


def _resolved(out):
    return dict(ln.split("=", 1) for ln in out.splitlines() if ln.startswith(("HOST=", "CA=")))


RESOLVE = 'resolve_frontdoor; echo "HOST=$FRONTDOOR_HOST"; echo "CA=$FRONTDOOR_CA_NAME"\n'


def test_the_box_id_is_the_end_of_the_wifi_mac():
    # The digits Wi-Fi setup names its network by (teaport-9e35), whatever order sysfs
    # lists the interfaces in; a box with no Wi-Fi takes a wired one, never a virtual one.
    t = tempdir()
    _box(t)
    assert _run(t, "box_id\n") == (0, "9e35\n")
    t = tempdir()
    _iface(t, "docker0", "02:42:ac:11:00:01", device=False)
    _iface(t, "can0", "", device=True)
    _iface(t, "eth0", "48:b0:2d:00:ab:CD")
    assert _run(t, "box_id\n") == (0, "abcd\n")
    t = tempdir()
    pathlib.Path(t, "net").mkdir()
    assert _run(t, "box_id\n") == (0, "0000\n")


def test_a_lone_box_is_teaport_local_and_names_a_new_ca_for_itself():
    t = tempdir()
    _box(t)
    rc, out = _run(t, RESOLVE)
    assert rc == 0, out
    assert _resolved(out) == {"HOST": "teaport.local", "CA": "Teaport 9e35 Local Authority"}
    # Its own address answering is still a lone box (a re-run on a working box).
    pathlib.Path(t, "mdns").write_text("192.168.1.234\n")
    assert _resolved(_run(t, RESOLVE)[1])["HOST"] == "teaport.local"


def test_a_second_box_on_the_network_takes_its_own_name():
    t = tempdir()
    _box(t)
    pathlib.Path(t, "mdns").write_text("192.168.1.77\n")
    rc, out = _run(t, RESOLVE)
    assert rc == 0, out
    assert _resolved(out)["HOST"] == "teaport-9e35.local"
    assert "another box already answers to teaport.local" in out


def test_the_name_an_earlier_run_settled_on_stays_and_teaport_host_wins():
    # Devices already use the box's name: a re-run never moves it because the network
    # changed meanwhile. TEAPORT_HOST, said out loud, always wins.
    t = tempdir()
    _box(t)
    pathlib.Path(t, "etc").mkdir()
    pathlib.Path(t, "etc", "brain.env").write_text(
        "BRAIN_PORT=7861\nTEAPORT_FRONTDOOR_HOST=teaport-9e35.local\n")
    assert _resolved(_run(t, RESOLVE)[1])["HOST"] == "teaport-9e35.local"
    pathlib.Path(t, "mdns").write_text("192.168.1.77\n")
    pathlib.Path(t, "etc", "brain.env").write_text("TEAPORT_FRONTDOOR_HOST=teaport.local\n")
    assert _resolved(_run(t, RESOLVE)[1])["HOST"] == "teaport.local"
    out = _run(t, RESOLVE, {"TEAPORT_HOST": "kitchen.local"})[1]
    assert _resolved(out)["HOST"] == "kitchen.local"


def test_an_existing_ca_keeps_its_name_unless_it_is_regenerated():
    # Caddy names a root only when it makes one: the pages must name the root the box
    # has, and a regenerate makes a new one under this box's name.
    t = tempdir()
    _box(t)
    pki = pathlib.Path(t, "pki")
    pki.mkdir()
    crt, _ = _cert(t, "root", "Caddy Local Authority - 2026 ECC Root", sans=())
    shutil.copy(crt, pki / "root.crt")
    assert _resolved(_run(t, RESOLVE)[1])["CA"] == "Caddy Local Authority"
    out = _run(t, RESOLVE, {"TEAPORT_REGENERATE_CA": "1"})[1]
    assert _resolved(out)["CA"] == "Teaport 9e35 Local Authority"
    # A root named some other way is named as it is.
    _cert(t, "root", "Office box CA", sans=())
    shutil.copy(pathlib.Path(t, "root.crt"), pki / "root.crt")
    assert _resolved(_run(t, RESOLVE)[1])["CA"] == "Office box CA"


def test_a_name_too_long_for_the_qr_code_is_warned_about():
    t = tempdir()
    _box(t)
    out = _run(t, RESOLVE, {"TEAPORT_HOST": "a" * 31 + ".local"})[1]
    assert "longer than 36 characters" in out
    assert "longer than" not in _run(t, RESOLVE, {"TEAPORT_HOST": "a" * 30 + ".local"})[1]


def test_the_operators_certificate_is_checked_before_anything_is_installed():
    t = tempdir()
    _box(t)
    crt, key = _cert(t, "good", "box.example.com", sans=("box.example.com",))
    good = {"TEAPORT_HOST": "box.example.com", "TEAPORT_TLS_CERT": str(crt),
            "TEAPORT_TLS_KEY": str(key)}
    rc, out = _run(t, RESOLVE, good)
    assert rc == 0 and "WARN" not in out, out
    # Given a certificate, the name comes from it: no mDNS rename.
    pathlib.Path(t, "mdns").write_text("192.168.1.77\n")
    rc, out = _run(t, RESOLVE, {**good, "TEAPORT_HOST": ""})
    assert rc == 1 and "is not issued for teaport.local" in out, out
    _, other_key = _cert(t, "other", "x")
    cases = {
        "relative": ({**good, "TEAPORT_TLS_CERT": "good.crt"}, "must be absolute paths"),
        "spaces": ({**good, "TEAPORT_TLS_CERT": "/a b/c.crt"}, "with spaces"),
        "missing": ({**good, "TEAPORT_TLS_KEY": str(key) + ".gone"}, "does not exist"),
        "not a cert": ({**good, "TEAPORT_TLS_CERT": str(key)}, "is not a PEM certificate"),
        "wrong key": ({**good, "TEAPORT_TLS_KEY": str(other_key)}, "is not the private key"),
        "wrong host": ({**good, "TEAPORT_HOST": "teaport.example.com"}, "is not issued for"),
    }
    for name, (env, says) in cases.items():
        rc, out = _run(t, RESOLVE, env)
        assert rc == 1 and says in out, (name, out)
    crt, _ = _cert(t, "soon", "box.example.com", sans=("box.example.com",), days=3, key=key)
    rc, out = _run(t, RESOLVE, {**good, "TEAPORT_TLS_CERT": str(crt)})
    assert rc == 0 and "expires within 7 days" in out, out


def _caddyfile(t, env=None):
    rc, out = _run(t, 'FRONTDOOR_CA_NAME="Teaport 9e35 Local Authority"; '
                      'write_caddyfile "$T/Caddyfile"\n', env)
    assert rc == 0, out
    return pathlib.Path(t, "Caddyfile").read_text()


def _adapt(path):
    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    if not caddy:
        return None
    p = subprocess.run([caddy, "adapt", "--config", str(path), "--adapter", "caddyfile"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_the_caddyfile_names_the_ca_and_serves_one_certificate_or_the_other():
    t = tempdir()
    local = _caddyfile(t)
    lines = [ln for ln in local.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "{"  # Caddy's global options come first or not at all
    assert '            name "Teaport 9e35 Local Authority"' in local
    assert "    tls internal" in local
    adapted = _adapt(pathlib.Path(t, "Caddyfile"))
    if adapted is not None:
        assert '"name":"Teaport 9e35 Local Authority"' in adapted
    own = _caddyfile(t, {"TEAPORT_TLS_CERT": "/etc/teaport/tls/fullchain.pem",
                         "TEAPORT_TLS_KEY": "/etc/teaport/tls/key.pem"})
    assert '    tls "/etc/teaport/tls/fullchain.pem" "/etc/teaport/tls/key.pem"' in own
    assert "tls internal" not in own
    # Everything else is the same: /p/*, the CA download on both blocks.
    assert own.replace('tls "/etc/teaport/tls/fullchain.pem" "/etc/teaport/tls/key.pem"',
                       "tls internal") == local
    adapted = _adapt(pathlib.Path(t, "Caddyfile"))
    if adapted is not None:
        assert "/etc/teaport/tls/fullchain.pem" in adapted


if __name__ == "__main__":
    appliance.run_tests([fn for name, fn in list(globals().items()) if name.startswith("test_")])
