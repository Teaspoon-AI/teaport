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
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from teaport_brain import config_schema

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
STORE_FILES = {"engine_env": "engine.env", "brain_env": "brain.env", "bridge_env": "bridge.env"}
SIP_CONF = "~/.config/teaport/teaport-sip.conf"
# What env_flag accepts (pipecat's env_truthy table), for validating flag rows.
FLAG_WORDS = {"0", "1", "true", "false", "yes", "no", "on", "off", "y", "n"}

router = APIRouter()


# ------------------------------------------------------------------ auth

def _token() -> str:
    return os.getenv("GATEWAY_TOKEN", "")


def _authorize(request: Request) -> None:
    want = _token()
    if not want:
        return
    got = request.query_params.get("token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    if got != want:
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
    if store in STORE_FILES:
        return os.path.join(ETC_DIR, STORE_FILES[store])
    if store == "sip_conf":
        return os.path.expanduser(SIP_CONF)
    raise HTTPException(status_code=400, detail=f"unknown store {store!r}")


def _read_text(path: str) -> str | None:
    """The file's text; "" when it does not exist; None when it exists but
    cannot be read (a root:root brain.env after a plain-sudo first install —
    install.sh write_env documents the case). One unreadable store must not
    take the whole page down."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError as e:
        logger.warning(f"config: cannot read {path}: {e}")
        return None


def _secret_is_set(row: dict, store_values: dict[str, str]) -> bool:
    """Set in the process env, in the row's store (an env-file copy, or the
    sip conf's password, which has no file), or in its secret file."""
    if os.getenv(row["name"]) or store_values.get(row["name"]):
        return True
    path = row.get("file")
    if not path:
        return False
    try:
        return bool(open(os.path.expanduser(path), encoding="utf-8").read().strip())
    except OSError:
        return False


def _is_hidden(name: str, rows: dict[str, dict]) -> bool:
    """Never hand a secret to the page — a schema secret, or a hand-added key
    whose name says credential (the schema decides for the names it knows)."""
    if name in rows:
        return rows[name]["type"] == "secret"
    return bool(_SECRET_LOOKING.match(name))


def _validate(row: dict, value: str) -> str | None:
    """None when `value` is an acceptable text for this row; else the reason.
    Check with the normalized text (see _normalize) — a json row is compacted
    to one line first, everything else must already be one."""
    t = row["type"]
    if value == "":
        # An explicit empty is only meaningful where the code distinguishes it.
        if t == "enum" and "" in row.get("values", []):
            return None
        return "empty — clear the field to unset it instead"
    if t == "json":
        try:
            if not isinstance(json.loads(value), dict):
                return "must be a JSON object"
        except ValueError as e:
            return f"not valid JSON: {e}"
        return None
    # An env-file value is one line. quote() does not escape newlines, and a
    # value that spans lines would be refused by config_apply after validation
    # had already passed — a 500 with no field named instead of this 400.
    if "\n" in value or "\r" in value:
        return "must be a single line"
    if t == "int":
        if not re.fullmatch(r"[+-]?\d+", value):
            return "must be a whole number"
        n = int(value)
    elif t == "float":
        try:
            n = float(value)
        except ValueError:
            return "must be a number"
        if not math.isfinite(n):
            return "must be a finite number"
    elif t == "enum":
        return None if value in row["values"] else f"must be one of {', '.join(repr(v) for v in row['values'])}"
    elif t == "flag":
        ok = row.get("accepts") or FLAG_WORDS
        return None if value.lower() in ok else f"must be one of {', '.join(sorted(ok))}"
    elif t == "url":
        return None if re.fullmatch(r"https?://\S+", value) else "must start with http:// or https://"
    elif t == "ws_url":
        return None if re.fullmatch(r"wss?://\S+", value) else "must start with ws:// or wss://"
    elif t == "snowflake":
        return None if value.isdigit() else "must be a Discord id (digits only)"
    else:
        return None
    lo, hi = row.get("min"), row.get("max")
    if lo is not None and n < lo:
        return f"must be at least {lo}"
    if hi is not None and n > hi:
        return f"must be at most {hi}"
    return None


def _normalize(row: dict, value: str) -> str:
    """The text that goes in the file. A json row is what the page's textarea
    holds — pretty-printed pastes included — compacted to one line; the JSON
    is unchanged. Everything else is written as typed."""
    if row["type"] == "json" and value:
        try:
            return json.dumps(json.loads(value), separators=(",", ":"))
        except ValueError:
            return value  # _validate names the problem
    return value


def _effective(rows: dict[str, dict], values: dict[str, str]) -> dict[str, float]:
    """Numeric view of the settings a constraint may name: the value in the
    file if set and numeric, else the row default."""
    out: dict[str, float] = {}
    for name, row in rows.items():
        raw = values.get(name, row.get("default"))
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
    parsed: dict[str, dict[str, str]] = {}
    mtimes: dict[str, float | None] = {}
    unreadable: list[str] = []
    for store in list(STORE_FILES) + ["sip_conf"]:
        path = _store_path(store)
        text = await asyncio.to_thread(_read_text, path)
        if text is None:
            unreadable.append(store)
            text = ""
        parsed[store] = parse_env(text)
        values[store] = {k: v for k, v in parsed[store].items() if not _is_hidden(k, rows)}
        mtimes[store] = _mtime(path)

    secrets = {name: _secret_is_set(row, parsed.get(row["store"], {}))
               for name, row in rows.items() if row["type"] == "secret"}

    units = sorted({u for st in schema["stores"].values() for u in st["services"]})
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
        "secrets": secrets,
        "services": {u: s["state"] for u, s in states.items()},
        "pending": pending,
        "auth": bool(_token()),
        "writable": [s for s in STORE_FILES if s not in unreadable],
        "unreadable": unreadable,
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
        if value is None:
            if row["type"] == "secret":
                errors[name] = "a secret cannot be cleared from here"
            else:
                env_updates[name] = None
            continue
        if not isinstance(value, str):
            errors[name] = "must be text"
            continue
        if row["type"] == "secret":
            if not value.strip() or "\n" in value or "\r" in value:
                errors[name] = "must be one non-empty line"
            elif not row.get("file"):
                errors[name] = "has no secret file"
            else:
                secret_updates[name] = value.strip()
            continue
        value = _normalize(row, value)
        err = _validate(row, value)
        if err:
            errors[name] = err
        else:
            env_updates[name] = value
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    path = _store_path(store)
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
