"""The config web UI: a page and three JSON routes on the Talk brain's own app.

Mounted on gateway_server's FastAPI app rather than run as its own process on
purpose — the appliance is an 8 GB Orin Nano and a second Python service is
~50 MB that the engine's reserve would rather have. On the app that already
runs, this is a static file and a few file reads.

    GET  /config             the page (teaport_brain/static/config.html)
    GET  /api/config         schema + every store's current values + what is pending
    PUT  /api/config         {store, values: {NAME: "text" | null}} — validated, then
                             written through config_apply under sudo (env files) or
                             directly (secret files, which the run user owns)
    POST /api/restart        {unit} — deferred restart through config_apply

Auth is the same shared secret /talk uses: GATEWAY_TOKEN, as a bearer header
or ?token=. Unset means open, exactly like /talk, and the brain already warns
about that at startup.

What the routes know comes from config_schema.toml (types, bounds, tiers,
which file, which unit) — nothing here names a setting. Secrets are never
returned: the API says whether one is set, and accepts a new value.

GET shows a value as the program that reads it takes it, not as the file spells
it (_as_read), and PUT writes the brain's reading of what was typed (_accept,
settings.canonical), so the page needs no parser of its own.

"Pending": a store file newer than a unit's ActiveEnterTimestamp means that
unit runs on stale config. That is the whole restart story — nothing hot-reloads
— and it is computed from systemd's own clock, so it is right even for a save
made by hand or by another brain. A secret file counts too, for the units that
read it once at startup (the Discord bridge; the brains read the LLM key per
session, so a rotated key needs no restart there).

A secret that is ALSO set in its env file is a trap: every reader prefers the
env var, so a new file value would change nothing and the page would still say
"Saved". Writing a secret therefore drops the env-file copy of the same name
(that store's units then show as pending, which is true — their process env
still holds the old value until they restart).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from teaport_brain import config_schema, settings

ETC_DIR = "/etc/teaport"
# The root-owned copy of config_apply.py the sudoers line trusts (install.sh
# install_config_sudoers). NOT sys.executable / the package's own copy: the venv
# belongs to the run user, so trusting it would be trusting the brain itself.
APPLY_HELPER = "/usr/local/lib/teaport/config_apply.py"
SYSTEM_PYTHON = "/usr/bin/python3"
# Keys an operator may have added to an env file by hand that look like
# credentials; masked from the page like the schema's secrets (same glob as
# install.sh's dry-run transcript). A schema row that is not a secret is shown.
_SECRET_LOOKING = re.compile(r".*(TOKEN|KEY|SECRET|PASSWORD)$", re.IGNORECASE)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STORE_FILES = {"engine_env": "engine.env", "brain_env": "brain.env", "bridge_env": "bridge.env",
               "local_audio_env": "local-audio.env"}
# Shown, never written: the flashing step writes it (WIFI_SETUP_* for the paper insert).
READ_ONLY_FILES = {"wifi_setup_env": "wifi-setup.env"}
# Stores whose file IS the feature's on-switch (the unit's ConditionPathExists, and what
# makes an installer repair keep the feature): the page edits them once the installer
# has made them, and never creates one — saving a row would quietly opt the box in.
OPT_IN_STORES = ("local_audio_env",)
SIP_CONF = "~/.config/teaport/teaport-sip.conf"

# Units that run only when asked and exit by themselves: not "services" the page shows
# as up or down (an idle one is inactive by design, not broken).
ON_DEMAND_UNITS = ("teaport-wifi-setup",)

router = APIRouter()


# ------------------------------------------------------------------ auth

def _token() -> str:
    return settings.setting("GATEWAY_TOKEN")


def token_matches(got: str | None, want: str | None) -> bool:
    """Constant-time compare of a presented token against the configured one, so
    the time a wrong guess takes says nothing about how much of it was right.
    Bytes, not str: compare_digest refuses non-ASCII str. surrogateescape so an
    env value that is not valid UTF-8 (os.environ decodes it to lone surrogates)
    still compares instead of raising. None counts as empty."""
    def b(s: str | None) -> bytes:
        return (s or "").encode("utf-8", "surrogateescape")
    return hmac.compare_digest(b(got), b(want))


def _authorize(request: Request) -> None:
    want = _token()
    if not want:
        return
    got = request.query_params.get("token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    if not token_matches(got, want):
        raise HTTPException(status_code=401, detail="bad or missing token")


# ------------------------------------------------------------------ env files

_KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def unquote(raw: str) -> str:
    """systemd EnvironmentFile value rules, near enough: trim, strip one layer
    of matching quotes, unescape \\-sequences inside double quotes."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            inner = re.sub(r'\\(["\\$`\n])', r"\1", inner)
        return inner
    return v


def quote(value: str) -> str:
    """Bare when it can be; double-quoted with escapes when whitespace, quotes,
    a hash or a backslash would otherwise change what systemd reads."""
    if value and not re.search(r"[\s\"'#\\]", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _KV_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = unquote(m.group(2))
    return out


def parse_conf(text: str) -> dict[str, str]:
    """The SIP gateway's conf as its own parser reads it (teaport-sip src/config.h,
    loadConfig): a `#` starts a comment anywhere in a line; a line, its key and its value
    are trimmed of space, tab, CR and LF only; the first `=` splits; quotes are part of
    the value; a blank value is the value "" (the default is not used); the last of a
    repeated key wins. A BOM before the first line is dropped, and a NUL byte ends the
    file. Lines end at LF alone, so the file is read with newline=""."""
    out: dict[str, str] = {}
    for line in text.removeprefix("\ufeff").split("\n"):
        if "\0" in line:
            break
        line = line.split("#", 1)[0].strip(" \t\r\n")
        if "=" in line and not line.startswith("["):
            key, _, value = line.partition("=")
            out[key.strip(" \t\r\n")] = value.strip(" \t\r\n")
    return out


def render_env(text: str, updates: dict[str, str | None]) -> str:
    """Apply {key: value | None} to an env file's text, keeping every line we
    do not touch — comments, blank lines, keys the schema does not know —
    where it was. None removes the key; a new key is appended."""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = _KV_RE.match(line)
        key = m.group(1) if m and not line.lstrip().startswith("#") else None
        if key in updates:
            seen.add(key)
            if updates[key] is None:
                continue
            # Only the LAST occurrence of a duplicated key wins in systemd, so
            # keep exactly one: the first is rewritten, later ones dropped.
            if any(l.startswith(f"{key}=") for l in out):
                continue
            out.append(f"{key}={quote(updates[key])}")
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            out.append(f"{key}={quote(value)}")
    return "\n".join(out) + ("\n" if out else "")


# ------------------------------------------------------------------ schema helpers

def _rows() -> dict[str, dict]:
    return {r["name"]: r for r in config_schema.load()["settings"]}


def _store_path(store: str) -> str:
    if store in STORE_FILES or store in READ_ONLY_FILES:
        return os.path.join(ETC_DIR, {**STORE_FILES, **READ_ONLY_FILES}[store])
    if store == "sip_conf":
        return os.path.expanduser(SIP_CONF)
    raise HTTPException(status_code=400, detail=f"unknown store {store!r}")


def _read_text(path: str, newline: str | None = None) -> str | None:
    """The file's text; "" when it does not exist; None when it exists but
    cannot be read (a root:root brain.env after a plain-sudo first install —
    install.sh write_env documents the case — or not UTF-8). One unreadable store
    must not take the whole page down. `newline` is open()'s."""
    try:
        with open(path, encoding="utf-8", newline=newline) as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as e:
        logger.warning(f"config: cannot read {path}: {e}")
        return None


def _secret_is_set(row: dict, store_values: dict[str, str]) -> bool:
    """Set in the brain's own env (for a brain.env secret: a unit drop-in counts),
    in the row's store (an env-file copy, or the sip conf's password, which has no
    file), or in its secret file — each read by settings.normalize, so a blank one
    is not set."""
    name = row["name"]
    env = os.getenv(name) if row["store"] == "brain_env" else None
    return any(settings.normalize(row, v) for v in (env, store_values.get(name))) \
        or settings.secret_file(name) is not None


def _is_hidden(name: str, rows: dict[str, dict]) -> bool:
    """Never hand a secret to the page — a schema secret, or a hand-added key
    whose name says credential (the schema decides for the names it knows)."""
    if name in rows:
        return rows[name]["type"] == "secret"
    return bool(_SECRET_LOOKING.match(name))


# Who reads a row decides what GET shows for it. The brain (setting()) and the Discord
# bridge (bridge/discord/env.js, the same rule) read a value as settings.canonical gives
# it: stripped, blank is not set, a value not of the row's type is ignored (it reads as
# not set: _ignored says what that means for the row). Every other reader gets the text as written and judges it its own way, so GET
# shows that text, "" included:
#   - engine.env, and a `read = "unit"` row: systemd hands the text over as is (a quoted
#     " 240 " stays padded); a blank one is an empty argument, and `--serve ""` stops the
#     engine rather than meaning its default;
#   - a `consumer` row: the library reads it (HF_HUB_OFFLINE= is online, not the default);
#   - the SIP gateway's conf: its own parser (parse_conf), where a blank value is "".
AS_WRITTEN_STORES = ("engine_env", "sip_conf")


def _read_by_rule(row: dict) -> bool:
    return row["store"] not in AS_WRITTEN_STORES and row.get("read") != "unit" \
        and "consumer" not in row


def _as_read(rows: dict[str, dict], file_values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """A store's values for the page, each as its reader takes it (_read_by_rule): the
    brain's reading in its one spelling, left out where it is not set, or the text as
    written; a key the schema does not know as written; hidden keys dropped. And
    {name: the page's note} for each value the brain ignores (_ignored)."""
    out: dict[str, str] = {}
    ignored: dict[str, str] = {}
    for name, value in file_values.items():
        if _is_hidden(name, rows):
            continue
        if name in rows and _read_by_rule(rows[name]):
            value, why = settings.canonical(rows[name], value)
            if why is not None:
                ignored[name] = _ignored(rows[name], why)
            if value is None:
                continue
        out[name] = value
    return out, ignored


def _ignored(row: dict, why: str) -> str:
    """The page's note for a file value its reader ignores: why, and what the reader does
    instead, which is what it does with the setting unset."""
    if row.get("required"):
        instead = "the setting is missing"  # LLM_BASE_URL, the bridge's guild: it cannot run
    elif "default" in row or "default_by" in row:
        instead = "the default applies"
    else:
        instead = "it reads as not set"
    return f"The value in the file is not used — {why}; {instead}."


def _accept(row: dict, value: str | None) -> tuple[str | None, str | None]:
    """(the text to write, None) for a value typed on the page, or (None, the reason).
    The text is the brain's reading of it (settings.canonical): stripped, in its one
    spelling, so what the page writes is what the brain reads. Blank is refused where it
    means "not set" (clearing the field is how to unset; a secret cannot be). An env file
    holds one line per value, as str.splitlines() counts lines (\\v, \\f, \\x85, U+2028
    too): quote() does not escape line breaks, and config_apply, which splits the same
    way, would refuse the file — a 500 naming no field instead of this 400."""
    text, problem = settings.canonical(row, value)
    if text is None and problem is None:
        problem = ("a secret cannot be cleared from here" if row["type"] == "secret"
                   else "empty — clear the field to unset it instead")
    elif text and text.splitlines() != [text]:
        problem = "must be a single line"
    elif text and row["type"] in ("int", "float"):
        problem = settings.out_of_bounds(row, settings.parse(row, text)[0])
    return (None, problem) if problem else (text, None)


def _effective(rows: dict[str, dict], values: dict[str, str]) -> dict[str, float]:
    """Numeric view of the settings a constraint may name: the value in the
    file as the brain reads it, else (unset, or not a value) the row default."""
    out: dict[str, float] = {}
    for name, row in rows.items():
        raw, _ = settings.canonical(row, values.get(name))
        raw = row.get("default") if raw is None else raw
        try:
            out[name] = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
    return out


def _constraint_names(c: dict) -> set[str]:
    names: set[str] = set()
    for k in ("lhs", "rhs", "names"):
        v = c.get(k, [])
        names |= {v} if isinstance(v, str) else set(v)
    return names


def check_constraints(rows: dict[str, dict], values: dict[str, str],
                      touched: set[str] | None = None) -> list[str]:
    """Messages of the cross-row constraints `values` violates. With `touched`,
    only constraints naming one of those settings are checked: a violation
    already in the file must not block an unrelated edit."""
    num = _effective(rows, values)
    failed: list[str] = []
    for c in config_schema.load().get("constraints", []):
        if touched is not None and not (_constraint_names(c) & touched):
            continue
        kind = c["kind"]
        try:
            if kind == "sum_lt":
                ok = sum(num[n] for n in c["lhs"]) < num[c["rhs"]]
            elif kind == "lt":
                ok = num[c["lhs"]] < num[c["rhs"]]
            elif kind == "le_chain":
                seq = [num[n] for n in c["names"]]
                ok = all(a <= b for a, b in zip(seq, seq[1:]))
            else:
                continue  # "follows" is advice, not a check
        except KeyError:
            continue
        if not ok:
            failed.append(c["message"])
    return failed


# ------------------------------------------------------------------ systemd

async def _run(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _privileged(action: str, target: str, stdin: str | None = None) -> None:
    """config_apply under sudo — the root-owned copy, on the system python,
    byte-for-byte the command the sudoers line names. Replaced in tests."""
    rc, out, err = await _run("sudo", "-n", SYSTEM_PYTHON, APPLY_HELPER, action, target, stdin=stdin)
    if rc != 0:
        detail = (err or out).strip().splitlines()[-1:] or ["unknown error"]
        logger.warning(f"config_apply {action} {target} failed rc={rc}: {detail[0]}")
        raise HTTPException(status_code=500, detail=f"{action} {target}: {detail[0]}")


async def _unit_states(units: list[str]) -> dict[str, dict[str, Any]]:
    """{unit: {state, active_since}} from systemctl show; active_since is a
    wall-clock float or None.

    The realtime stamp, not the monotonic one: a file mtime is the wall clock
    at the moment of the write, and systemd's realtime stamp is the wall clock
    at the moment of activation — the same frame. Reconstructing it from the
    monotonic stamp is off by every clock step in between (NTP settling after
    a boot without an RTC battery, which is every Jetson), and BOOTTIME vs
    MONOTONIC differ by suspend on top. `--timestamp=us+utc` is the one fixed,
    locale-proof spelling systemd 249 (Ubuntu 22.04 / JetPack 6) offers:
    `Fri 2026-09-11 13:00:47.411058 UTC`; `unix` only arrived in 251."""
    if not units:
        return {}
    rc, out, _ = await _run("systemctl", "show", "--timestamp=us+utc",
                            "-p", "Id,ActiveState,ActiveEnterTimestamp", "--", *units)
    states: dict[str, dict[str, Any]] = {u: {"state": "unknown", "active_since": None} for u in units}
    if rc != 0:
        return states
    for block in out.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = props.get("Id", "").removesuffix(".service")
        if unit not in states:
            continue
        states[unit]["state"] = props.get("ActiveState", "unknown")
        states[unit]["active_since"] = _parse_stamp(props.get("ActiveEnterTimestamp", ""))
    return states


def _parse_stamp(stamp: str) -> float | None:
    """`Fri 2026-09-11 13:00:47.411058 UTC` → epoch seconds; None for `n/a`
    (a unit that never activated) or anything else."""
    try:
        _, rest = stamp.split(" ", 1)
        return datetime.strptime(rest, "%Y-%m-%d %H:%M:%S.%f UTC").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# ------------------------------------------------------------------ routes

@router.get("/config", include_in_schema=False)
async def config_page():
    return FileResponse(os.path.join(STATIC_DIR, "config.html"), media_type="text/html")


@router.get("/config/logo.svg", include_in_schema=False)
async def config_logo():
    return FileResponse(os.path.join(STATIC_DIR, "teaport-logo.svg"), media_type="image/svg+xml")


@router.get("/api/config")
async def get_config(request: Request):
    _authorize(request)
    schema = config_schema.load()
    rows = _rows()

    values: dict[str, dict[str, str]] = {}
    ignored: dict[str, str] = {}
    parsed: dict[str, dict[str, str]] = {}
    mtimes: dict[str, float | None] = {}
    unreadable: list[str] = []
    for store in [*STORE_FILES, *READ_ONLY_FILES, "sip_conf"]:
        path = _store_path(store)
        conf = store == "sip_conf"
        text = await asyncio.to_thread(_read_text, path, "" if conf else None)
        if text is None:
            unreadable.append(store)
            text = ""
        parsed[store] = parse_conf(text) if conf else parse_env(text)
        values[store], why = _as_read(rows, parsed[store])
        ignored.update(why)
        mtimes[store] = _mtime(path)

    absent = [s for s in OPT_IN_STORES if not os.path.exists(_store_path(s))]
    secrets = {name: _secret_is_set(row, parsed.get(row["store"], {}))
               for name, row in rows.items() if row["type"] == "secret"}

    units = sorted({u for st in schema["stores"].values() for u in st["services"]}
                   - set(ON_DEMAND_UNITS))
    states = await _unit_states(units)
    pending: dict[str, list[str]] = {}

    def _stale(mtime: float | None, unit: str) -> bool:
        since = states.get(unit, {}).get("active_since")
        return mtime is not None and since is not None and mtime > since

    for store, st in schema["stores"].items():
        for unit in st["services"]:
            if _stale(mtimes.get(store), unit):
                pending.setdefault(unit, []).append(store)
    # A secret file read once at startup is config that unit runs stale on too.
    for name, row in rows.items():
        if row["type"] != "secret" or not row.get("file") or row.get("read") != "startup":
            continue
        for unit in row.get("services") or schema["stores"][row["store"]]["services"]:
            if _stale(_mtime(os.path.expanduser(row["file"])), unit):
                pending.setdefault(unit, []).append(name)

    return {
        "schema": {"groups": schema["groups"], "stores": schema["stores"],
                   "settings": schema["settings"], "constraints": schema.get("constraints", [])},
        "values": values,
        "ignored": ignored,
        "secrets": secrets,
        "services": {u: s["state"] for u, s in states.items()},
        "pending": pending,
        "auth": bool(_token()),
        "writable": [s for s in STORE_FILES if s not in unreadable and s not in absent],
        "unreadable": unreadable,
        "absent": absent,
    }


@router.put("/api/config")
async def put_config(request: Request):
    _authorize(request)
    body = await request.json()
    store = body.get("store")
    updates = body.get("values")
    if store not in STORE_FILES or not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="body must be {store: <env store>, values: {NAME: text|null}}")
    rows = _rows()

    errors: dict[str, str] = {}
    env_updates: dict[str, str | None] = {}
    secret_updates: dict[str, str] = {}
    for name, value in updates.items():
        row = rows.get(name)
        if row is None or row["store"] != store:
            errors[name] = f"not a {store} setting"
            continue
        if row["tier"] == "installer":
            errors[name] = "set by the installer; re-run install.sh to change it"
            continue
        if value is not None and not isinstance(value, str):
            errors[name] = "must be text"
            continue
        if value is None and row["type"] != "secret":
            env_updates[name] = None
            continue
        text, err = _accept(row, value)
        if err:
            errors[name] = err
        elif row["type"] != "secret":
            env_updates[name] = text
        elif not row.get("file"):
            errors[name] = "has no secret file"
        else:
            secret_updates[name] = text
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    path = _store_path(store)
    if store in OPT_IN_STORES and not os.path.exists(path):
        raise HTTPException(status_code=409, detail=f"{STORE_FILES[store]} is not set up on this box — the installer creates it when the feature is turned on")
    current = await asyncio.to_thread(_read_text, path)
    if current is None:
        raise HTTPException(status_code=500, detail=f"{STORE_FILES[store]} is not readable by the brain")
    merged = parse_env(current)
    # The env-file copy of a secret shadows its file for every reader: a new
    # file value must take the copy with it, or the save changes nothing.
    superseded = [n for n in secret_updates if n in merged]
    for n in superseded:
        env_updates[n] = None
    for k, v in env_updates.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    failed = check_constraints(rows, merged, touched=set(env_updates))
    if failed and not body.get("force"):
        return JSONResponse(status_code=400, content={"constraints": failed})

    if env_updates:
        new_text = render_env(current, env_updates)
        if new_text != current:
            await _privileged("write", STORE_FILES[store], stdin=new_text)
            logger.info(f"config: {STORE_FILES[store]} updated — {', '.join(sorted(env_updates))}")
    for name, value in secret_updates.items():
        spath = os.path.expanduser(rows[name]["file"])
        await asyncio.to_thread(_write_secret, spath, value)
        logger.info(f"config: secret {name} written to {spath}")

    if superseded:
        logger.info(f"config: {STORE_FILES[store]} copy of {', '.join(superseded)} dropped — the file wins now")
    return {"ok": True, "written": sorted(n for n in env_updates if n not in superseded),
            "secrets": sorted(secret_updates), "superseded": superseded, "warnings": failed}


def _write_secret(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, path)


@router.post("/api/restart")
async def restart(request: Request):
    _authorize(request)
    body = await request.json()
    unit = body.get("unit")
    units = {u for st in config_schema.load()["stores"].values() for u in st["services"]}
    if unit not in units:
        raise HTTPException(status_code=400, detail=f"unit must be one of {sorted(units)}")
    await _privileged("restart", unit)
    logger.info(f"config: restart of {unit} requested from the config page")
    return {"ok": True, "unit": unit, "in_seconds": 2}


# ------------------------------------------------------------------ Wi-Fi
# The online half of Wi-Fi setup (wifi.py): see the box's network, scan, switch — a
# switch that fails puts the old connection back — or start the offline setup network
# (teaport-wifi-setup, wifi_setup.py), which takes the box off its current network.
# Behind the page's token like everything else here; the setup network's own page is
# the unauthenticated one, and it is a separate, short-lived process.

_wifi_lock = asyncio.Lock()
_SETUP_BUSY = "Wi-Fi setup is running on this box"


def _wifi_nm():
    from teaport_brain.wifi import NM
    return NM()


async def _wifi_setup_busy() -> bool:
    """The setup unit has the radio: running, starting, stopping, or about to start
    (config_apply's restart runs it from a two-second timer). Replaced in tests."""
    from teaport_brain import config_apply, wifi_voice
    _, out, _ = await _run("systemctl", "show", "-p", "ActiveState", "--value", "--",
                           wifi_voice.UNIT + ".service")
    if out.strip() in ("active", "activating", "deactivating", "reloading"):
        return True
    _, out, _ = await _run("systemctl", "list-units", "--plain", "--no-legend", "--state=waiting",
                           "--", config_apply._restart_timer(wifi_voice.UNIT))
    return bool(out.strip())


def _wifi_state(nm) -> dict:
    dev = nm.wifi_device()
    if not dev:
        return {"available": False}
    from teaport_brain import wifi_setup, wifi_voice
    connection = nm.active_wifi(dev)
    setup = None
    try:
        with open(os.path.join(wifi_setup.STATUS_DIR, wifi_setup.STATUS_FILE)) as f:
            setup = {k: v for k, v in json.load(f).items() if k != "password"}
    except (OSError, ValueError):
        pass
    # Online through the Wi-Fi itself: on Ethernet the box is online whatever the Wi-Fi does.
    return {"available": True, "device": dev, "connection": connection,
            "online": nm.online(dev) if connection else False,
            "setup_installed": wifi_voice.available(), "setup_last": setup}


@router.get("/api/wifi")
async def wifi_state(request: Request):
    _authorize(request)
    return await asyncio.to_thread(_wifi_state, _wifi_nm())


@router.post("/api/wifi/scan")
async def wifi_scan(request: Request):
    _authorize(request)
    if await _wifi_setup_busy():  # the radio is an access point: it cannot scan
        raise HTTPException(status_code=409, detail=_SETUP_BUSY)
    nm = _wifi_nm()
    dev = await asyncio.to_thread(nm.wifi_device)
    if not dev:
        raise HTTPException(status_code=404, detail="this box has no Wi-Fi device")
    return {"networks": await asyncio.to_thread(nm.scan, dev)}


@router.post("/api/wifi/connect")
async def wifi_connect(request: Request):
    _authorize(request)
    body = await request.json()
    ssid = str(body.get("ssid") or "").strip()
    password = str(body.get("password") or "")
    hidden = bool(body.get("hidden"))
    if not ssid or len(ssid.encode()) > 32:
        raise HTTPException(status_code=400, detail="a network name of 1 to 32 bytes")
    if password and not 8 <= len(password) <= 63:
        raise HTTPException(status_code=400, detail="Wi-Fi passwords are 8 to 63 characters")
    if _wifi_lock.locked():
        raise HTTPException(status_code=409, detail="a Wi-Fi change is already in progress")
    async with _wifi_lock:
        # Its timeout would put back the network it found, undoing this switch.
        if await _wifi_setup_busy():
            raise HTTPException(status_code=409, detail=_SETUP_BUSY)
        from teaport_brain.wifi import switch
        nm = _wifi_nm()
        dev = await asyncio.to_thread(nm.wifi_device)
        if not dev:
            raise HTTPException(status_code=404, detail="this box has no Wi-Fi device")
        ok, why = await asyncio.to_thread(switch, nm, dev, ssid, password, hidden)
    logger.info(f"config: Wi-Fi switch to {ssid!r} from the config page: {'ok' if ok else why}")
    return {"ok": ok, "ssid": ssid, "error": why or None}


@router.post("/api/wifi/setup")
async def wifi_setup_start(request: Request):
    _authorize(request)
    from teaport_brain import wifi_voice
    if not wifi_voice.available():
        raise HTTPException(status_code=404, detail="Wi-Fi setup is not installed on this box")
    if _wifi_lock.locked():
        raise HTTPException(status_code=409, detail="a Wi-Fi change is already in progress")
    async with _wifi_lock:
        # "restart" on a running unit would stop it mid-setup and start it over.
        if await _wifi_setup_busy():
            raise HTTPException(status_code=409, detail=_SETUP_BUSY)
        await _privileged("restart", wifi_voice.UNIT)
    logger.info("config: Wi-Fi setup network started from the config page")
    return {"ok": True}
