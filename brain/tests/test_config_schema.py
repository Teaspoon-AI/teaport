#
# Schema drift gate: every environment variable the brain reads has a row in
# teaport_brain/config_schema.toml, and every row is well-formed.
#
# The schema is the ONE table the config UI, `teaport doctor` and the CONFIG.md
# generator read, so an os.getenv() added without a row is a setting nobody can
# see, document or validate — exactly the state docs/CONFIG.md was in when the
# table was first drafted (25 knobs read by the code and documented nowhere).
# The read-site regex mirrors settings.setting and the bare os.getenv / os.environ
# forms, either quote (a read inside an f-string is single-quoted); a new reader needs
# adding here too. Comments are not reads: a name mentioned in one must not stand in
# for a read that has gone. Nor is a write: `os.environ["X"] = v` forces a value for
# the process, and `os.environ.setdefault("X", v)` gives a library a default the
# operator's value overrides. The second is a setting, so it needs a row too; the brain
# still does not read it, so that row lists the module in `source`.
#
# A row the brain reads needs no pointer to where: `setting("NAME")` is the read, and
# a search for the name finds it. Any other row lists in `source` every file that
# handles it -- a file, not a line, which went stale under every edit above it -- and
# FORMS says what handling it means for that kind of file: the store's `writer` writes
# it into the store, a unit expands it from the store, the CLI reads it from the store,
# the Discord bridge reads process.env, the brain module setdefaults it. Each listed
# file must do so (merely naming it is not enough: install.sh names ENGINE_PORT as its
# own shell variable and BRIDGE_GUILD_ID in a sed read-back), and no other file in
# HANDLERS may do so unlisted.
#
# Run: python test_config_schema.py   (or via pytest)
#
import collections
import functools
import io
import pathlib
import re
import sys
import tokenize

PKG = pathlib.Path(__file__).resolve().parent.parent / "teaport_brain"
REPO = PKG.parent.parent

sys.path.insert(0, str(PKG.parent))
from teaport_brain import config_schema  # noqa: E402  (stdlib-only: tomllib)
from tempdirs import tempdir  # noqa: E402

READ_RE = re.compile(
    r'(?:getenv|\bsetting|environ\.get|environ\[)\(?\s*(["\'])([A-Z][A-Z0-9_]+)\1(?!\]\s*=[^=])'
)
SETDEFAULT = r"""\benviron\.setdefault\(\s*(["']){name}\1"""

# How a file handles a row's setting, by the file's kind and its role: the store's
# `writer` writes the key in the store's format, any other file uses it. {name} is the
# setting and {file} the store's file name; every pattern must match the file with its
# COMMENTS dropped (and a shell file's continued lines joined).
FORMS = {
    ("shell", "writes env"):       # an argument of write_env "<dir>/<file>" (a ?seed too)
        (r'^[ \t]*write_env[ \t]+"[^"\n]*/{file}"[^\n]*"\??(?:\$\{{\w+\}})?{name}=',),
    ("shell", "writes keyvalue"):  # a `name=` line of a here-document
        (r"""<<-?[ \t]*(['"]?)(\w+)\1(?![^\s;&|<>)]).*\n(?:(?![ \t]*\2$).*\n)*?{name}=""",),
    ("shell", "uses"): (r'\benvval[ \t]+"[^"\n]*/{file}"[ \t]+{name}\b',),
    ("unit", "uses"): (r"^EnvironmentFile=-?\S*/{file}$", r"\$\{{{name}\}}"),
    ("js", "uses"): (r"\bprocess\.env\.{name}\b",),
    ("python", "uses"): (SETDEFAULT,),
}
# Comments, by kind (python: tokenize): a shell `#` starts a word, a unit's a line.
COMMENTS = {"shell": r"(?:^|[ \t])#[^\n]*", "unit": r"^[ \t]*[#;][^\n]*",
            "js": r"/\*[\s\S]*?\*/|(?:^|[ \t])//[^\n]*"}
# Where a setting the brain does not read can be handled: each must be listed if it does.
HANDLERS = ("install.sh", "cli/*", "systemd/*.service.in", "bridge/*/*.js",
            "brain/teaport_brain/*.py")
TYPES = {"string", "url", "ws_url", "path", "dir", "int", "float", "flag", "enum",
         "json", "secret", "snowflake"}
TIERS = {"setup", "tuning", "diag", "installer"}
READS = {"startup", "session", "unit"}


# Read by the code but not settings: sudo's own variables, seen by config_apply, and
# the readiness socket systemd hands a Type=notify unit (sdnotify).
NOT_SETTINGS = {"SUDO_GID", "SUDO_UID", "SUDO_USER", "NOTIFY_SOCKET"}


def empty_problem(r: dict) -> str | None:
    """What is wrong with a row's `empty` field, or None."""
    if "empty" not in r:
        return None
    # setting() returns "" for it: only a text row may (an enum lists "" in values).
    if (r["type"] not in ("string", "path", "dir", "url", "ws_url")
            or not isinstance(r["empty"], str) or not r["empty"]):
        return "empty is the words for what \"\" means, on a text row"
    # Unset reads as the default and set-but-empty as "": with default "" they are one
    # value, and `empty` would name a difference there is not. `required` may go with
    # `empty`: the operator must choose, and off is a choice.
    if r.get("default") == "":
        return "empty with default \"\": unset already reads as \"\""
    return None


def without_comments(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            lines[row - 1] = lines[row - 1][:col] + " " * (end - col) + lines[row - 1][end:]
    return "".join(lines)


def reads_in(source: str) -> set[str]:
    return {m.group(2) for m in READ_RE.finditer(without_comments(source))}


def env_reads_in_code() -> set[str]:
    found: set[str] = set()
    for p in PKG.glob("*.py"):
        found |= reads_in(p.read_text())
    return found - NOT_SETTINGS


def env_defaults_in_code() -> set[str]:
    """The settings the brain setdefaults for a library."""
    pattern = re.compile(SETDEFAULT.format(name="([A-Z][A-Z0-9_]+)"))
    return {m.group(2) for p in PKG.glob("*.py")
            for m in pattern.finditer(without_comments(p.read_text()))}


def path_problem(src: str, root: pathlib.Path) -> str | None:
    p = pathlib.PurePosixPath(src)
    if p.is_absolute() or ".." in p.parts or re.search(r":\d+$", src) or not (root / p).is_file():
        return f"{src!r} must be an existing file, repo-relative, no line"
    return None


@functools.cache
def kind_and_text(path: pathlib.Path) -> tuple[str | None, str]:
    """A file's kind (a FORMS key) and its text as FORMS sees it."""
    text = path.read_text(encoding="utf-8")
    kind = ("unit" if path.name.endswith(".service.in") else
            {".js": "js", ".py": "python"}.get(path.suffix) or
            ("shell" if re.match(r"#!.*\b(?:ba)?sh\b", text) else None))
    if kind == "python":
        return kind, without_comments(text)
    text = re.sub(COMMENTS[kind], "", text, flags=re.M) if kind else text
    return kind, text.replace("\\\n", " ") if kind == "shell" else text


def role(src: str, store: dict) -> str:
    return f"writes {store.get('format', 'env')}" if src == store.get("writer") else "uses"


def source_problem(name: str, src: str, store: dict, root: pathlib.Path = REPO) -> str | None:
    """Why `src` is not a file that handles the setting `name` of `store`, or None."""
    if why := path_problem(src, root):
        return f"source {why}"
    (kind, text), does = kind_and_text(root / src), role(src, store)
    if (forms := FORMS.get((kind, does))) is None:
        return f"source {src!r}: no FORMS rule for a {kind or 'file of unknown kind'} that {does}"
    file = pathlib.PurePosixPath(store["path"]).name
    if not all(re.search(f.format(name=re.escape(name), file=re.escape(file)), text, re.M)
               for f in forms):
        return (f"source {src!r} never {does.split()[0]} it in a form FORMS knows for a "
                f"{kind} file that {does} ({file})")
    return None


@functools.cache
def handlers(root: pathlib.Path) -> frozenset[str]:
    return frozenset(str(p.relative_to(root)) for g in HANDLERS for p in root.glob(g)
                     if p.is_file())


def sources_problems(row: dict, stores: dict, root: pathlib.Path = REPO) -> list[str]:
    """Why a (normalised) row's `source` is not the list of the files that handle it."""
    srcs, store = row["source"], stores[row["store"]]
    if not isinstance(srcs, list) or not srcs or not all(isinstance(s, str) for s in srcs) \
            or len(set(srcs)) < len(srcs):
        return [f"source must be a file or a list of distinct files, not {srcs!r}"]
    found = [why for src in srcs if (why := source_problem(row["name"], src, store, root))]
    return found + [f"{src!r} handles it too, so source must list it"
                    for src in sorted(handlers(root) - set(srcs))
                    if source_problem(row["name"], src, store, root) is None]


# A tree of small files in each kind FORMS knows, for the source check's self-test: each
# file handles some settings and only mentions others, the way the real ones do.
FIXTURE = {
    "install.sh": """#!/usr/bin/env bash
ENGINE_PORT="${TEAPORT_ENGINE_PORT:-8000}"
guild="$(sed -n 's/^BRIDGE_GUILD_ID=//p' "$ETC/bridge.env")"
warn "set BRIDGE_GUILD_ID in $ETC/bridge.env"
# write_env "$ETC/engine.env" "TTS_CTX=192"
write_env "$ETC/engine.env" \\
  "ENGINE_DELAY=240" "${SEED}SEEDED=1"
write_env "$ETC/brain.env" "?HF_HUB_OFFLINE=1"
write_env "$ETC/engine.env" "HASH=a#b" "AFTER_HASH=${#x}"  # "TRAILING=1"
""",
    "cli/teaport": """#!/usr/bin/env bash
fpt="$(envval "$ETC/engine.env" KOKORO_RESERVE_FPT)"
cat <<EOF
# realm=*
aec_tail_ms=256
EOF
aec=true
cat <<END-X
log_level=3
END-X
uds_path=/x
""",
    "systemd/engine.service.in": """EnvironmentFile=@ETC@/engine.env
ExecStart=/bin/voxtral --serve ${ENGINE_PORT} --delay ${ENGINE_DELAY}
# --tts-ctx ${TTS_CTX}
""",
    "systemd/brain.service.in": "EnvironmentFile=@ETC@/brain.env\nExecStart=/b ${ENGINE_PORT}\n",
    "bridge/discord/index.js": """\
const U = process.env.BRAIN_URL || "ws://h", V = process.env.TEAPORT_VOICE;
// const PRIME = process.env.BRIDGE_PRIME_MS;
const J = /* process.env.BRIDGE_JOIN_CHANNEL_ID || */ "";
log("set BRIDGE_GUILD_ID");
""",
    "brain/teaport_brain/pkg.py": """import os
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ["MALLOC_ARENA_MAX"] = "2"
x = 1  # os.environ.setdefault("TTS_CTX", "1")
""",
    "docs/README.md": 'envval "$ETC/engine.env" ENGINE_PORT\n',  # shell's form, not a shell file
}
FIXTURE_STORES = {
    "engine_env": {"path": "/etc/teaport/engine.env", "writer": "install.sh"},
    "brain_env": {"path": "/etc/teaport/brain.env", "writer": "install.sh"},
    "bridge_env": {"path": "/etc/teaport/bridge.env", "writer": "install.sh"},
    "sip_conf": {"path": "~/.config/teaport/teaport-sip.conf", "format": "keyvalue",
                 "writer": "cli/teaport"},
}


def source_self_test() -> list[str]:
    """What the source check gets wrong on FIXTURE: each file must be accepted for what it
    handles, and refused for what it only mentions, wherever it sits in a list."""
    root = pathlib.Path(tempdir("teaport-schema-src-"))
    for rel, text in FIXTURE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    wrong: list[str] = []
    sh, cli, unit, js, py = ("install.sh", "cli/teaport", "systemd/engine.service.in",
                             "bridge/discord/index.js", "brain/teaport_brain/pkg.py")
    for ok, name, src, store in (
            (True, "ENGINE_DELAY", sh, "engine_env"),            # a continued write_env
            (True, "SEEDED", sh, "engine_env"),                  # a ${..._SEED} prefix
            (True, "HF_HUB_OFFLINE", sh, "brain_env"),           # a ?seed
            (True, "AFTER_HASH", sh, "engine_env"),              # `#` inside a word
            (True, "KOKORO_RESERVE_FPT", cli, "engine_env"),
            (True, "aec_tail_ms", cli, "sip_conf"),
            (True, "ENGINE_PORT", unit, "engine_env"),
            (True, "BRAIN_URL", js, "bridge_env"),
            (True, "TEAPORT_VOICE", js, "bridge_env"),           # after a ws://
            (True, "HF_HUB_OFFLINE", py, "brain_env"),
            (False, "ENGINE_PORT", sh, "engine_env"),            # its own shell variable
            (False, "BRIDGE_GUILD_ID", sh, "bridge_env"),        # a read-back and a warning
            (False, "TTS_CTX", sh, "engine_env"),                # a commented-out write
            (False, "TRAILING", sh, "engine_env"),               # a trailing comment
            (False, "HF_HUB_OFFLINE", sh, "engine_env"),         # written to another store
            (False, "ENGINE_DELA", sh, "engine_env"),            # a prefix
            (False, "DELAY", sh, "engine_env"),                  # a suffix
            (False, "KOKORO_RESERVE_FPT", cli, "brain_env"),     # read from another store
            (False, "aec", cli, "sip_conf"),                     # after the here-doc ends
            (False, "uds_path", cli, "sip_conf"),                # after an <<END-X one ends
            (False, "log_level", cli, "sip_conf"),               # in one FORMS cannot read
            (False, "realm", cli, "sip_conf"),                   # a comment in the conf
            (False, "ENGINE_PORT", "systemd/brain.service.in", "engine_env"),  # another store
            (False, "TTS_CTX", unit, "engine_env"),              # a comment
            (False, "BRIDGE_PRIME_MS", js, "bridge_env"),        # a comment
            (False, "BRIDGE_JOIN_CHANNEL_ID", js, "bridge_env"),  # a /* comment */
            (False, "BRIDGE_GUILD_ID", js, "bridge_env"),        # a string
            (False, "MALLOC_ARENA_MAX", py, "brain_env"),        # forced, not a default
            (False, "TTS_CTX", py, "brain_env"),                 # a comment
            (False, "ENGINE_PORT", "docs/README.md", "engine_env"),  # no FORMS rule
            (False, "ENGINE_PORT", "install.sh.missing", "engine_env"),
            (False, "ENGINE_PORT", f"{unit}:1", "engine_env"),
            (False, "ENGINE_PORT", str(root / unit), "engine_env"),
            (False, "ENGINE_PORT", f"../{root.name}/{unit}", "engine_env")):
        why = source_problem(name, src, FIXTURE_STORES[store], root)
        if ok != (why is None):
            wrong.append(f"{src!r} for {name}: {why or 'accepted'}")

    # A row lists every file that handles it and nothing else: install.sh writes
    # ENGINE_DELAY and the unit expands it, so neither alone is enough; a file that does
    # not handle it fails
    # the row first or last; an empty or repeating list, or a TOML value that is not a
    # list, is not a list of files; a lone string is one.
    for ok, name, srcs in (
            (True, "ENGINE_DELAY", [unit, sh]), (True, "ENGINE_PORT", unit),
            (False, "ENGINE_DELAY", [unit]), (False, "ENGINE_DELAY", [sh]),
            (False, "ENGINE_DELAY", [js, unit, sh]), (False, "ENGINE_DELAY", [unit, sh, js]),
            (False, "ENGINE_PORT", []), (False, "ENGINE_PORT", [unit, unit]),
            (False, "ENGINE_PORT", {"file": unit}), (False, "ENGINE_PORT", 1),
            (False, "ENGINE_PORT", True)):
        r = config_schema.normalise({"settings": [
            {"name": name, "store": "engine_env", "source": srcs}]})["settings"][0]
        why = sources_problems(r, FIXTURE_STORES, root)
        if ok == bool(why):
            wrong.append(f"source {srcs!r} for {name}: {why or 'accepted'}")
    return wrong


def main() -> int:
    schema = config_schema.load()
    rows = schema["settings"]
    names = [r["name"] for r in rows]
    stores = set(schema["stores"])
    groups = {g["id"] for g in schema["groups"]}
    problems: list[str] = []

    for n, c in collections.Counter(names).items():
        if c > 1:
            problems.append(f"duplicate row: {n}")
    for r in rows:
        n = r["name"]
        if r["store"] not in stores:
            problems.append(f"{n}: unknown store {r['store']}")
        if r["group"] not in groups:
            problems.append(f"{n}: unknown group {r['group']}")
        if r["type"] not in TYPES:
            problems.append(f"{n}: unknown type {r['type']}")
        if r["tier"] not in TIERS:
            problems.append(f"{n}: unknown tier {r['tier']}")
        if "help" not in r:
            problems.append(f"{n}: needs help")
        if r["store"] in ("brain_env", "engine_env") and r.get("read") not in READS:
            problems.append(f"{n}: env rows need read = startup|session|unit")
        if r["type"] == "enum":
            vals = r.get("values")
            if not vals:
                problems.append(f"{n}: enum without values")
            elif "default" in r and str(r["default"]) not in vals:
                problems.append(f"{n}: default {r['default']!r} not in values")
        if why := empty_problem(r):
            problems.append(f"{n}: {why}")
        if r["type"] == "secret" and r["store"] != "sip_conf" and "file" not in r \
                and r["tier"] != "installer":
            problems.append(f"{n}: operator secret must name the file the UI writes")
        if "min" in r and "max" in r and r["min"] > r["max"]:
            problems.append(f"{n}: min > max")

    for bad in ({"type": "int", "empty": "off"}, {"type": "path", "empty": ""},
                {"type": "path", "empty": "off", "default": ""}):
        if empty_problem(bad) is None:
            problems.append(f"self-test: empty accepted on {bad}")
    if empty_problem({"type": "path", "empty": "off", "required": True}) is not None:
        problems.append("self-test: empty refused on a required row")

    for c in schema.get("constraints", []):
        refs = []
        for k in ("lhs", "rhs", "names"):
            v = c.get(k, [])
            refs += [v] if isinstance(v, str) else list(v)
        for ref in refs:
            if ref not in names:
                problems.append(f"constraint references unknown setting {ref}")

    code = env_reads_in_code()
    for n in sorted(code - set(names)):
        problems.append(f"read in code, no schema row: {n}")
    for n in sorted(env_defaults_in_code() - set(names)):
        problems.append(f"set by the brain for a library, no schema row: {n}")
    brain_rows = {r["name"] for r in rows
                  if r["store"] == "brain_env" and r.get("read") != "unit" and "consumer" not in r}
    for n in sorted(brain_rows - code):
        problems.append(f"schema row, never read by the brain: {n}")
    for sid, st in schema["stores"].items():
        if "writer" in st and (why := path_problem(st["writer"], REPO)):
            problems.append(f"store {sid}: writer {why}")

    for r in rows:
        n, srcs = r["name"], r.get("source")
        if n in code and srcs is not None:
            problems.append(f"{n}: the brain reads it, so no source (searching the name finds it)")
        elif n not in code and srcs is None:
            problems.append(f"{n}: not read by the brain, so source must list what handles it")
        elif srcs is not None and r["store"] in stores:
            problems += [f"{n}: {why}" for why in sources_problems(r, schema["stores"])]

    problems += [f"self-test: {why}" for why in source_self_test()]
    if reads_in('os.environ["A_B"] = "1"\nos.environ.setdefault("E_F", "1")\n'
                'os.environ["C_D"] == "1"\n') != {"C_D"}:
        problems.append("self-test: an os.environ write counts as a read, or a comparison does not")

    for p in problems:
        print("FAIL", p)
    print(f"{len(rows)} rows, {len(code)} env reads in code, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
