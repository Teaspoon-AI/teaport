#
# install.sh's --only engine and --rollback engine (issue #158): an engine release reaches an
# installed box without a full run. The arguments (the parser, main's dispatch); the dry
# run's plan, from the real script; engine.env gaining only the keys it lacks
# (env_add_missing, and its keys kept in step with phase_services); the engine's files
# staged and swapped in whole (phase_engine, engine_swap_in); the rollback copies kept and
# pruned (engine_keep_live, engine_prune); --rollback engine and the automatic rollback
# (main_rollback_engine, engine_swap); the health check (engine_verify); and the freshness
# check's scope (check_brain_source with ENGINE_ONLY_PATHS).
#
# As in test_install_brain_only.py, the functions are taken out of install.sh as they are and
# run by bash with the commands around them stubbed (sudo, systemctl, ss, the downloads),
# against a temp dir. Nothing outside a temp dir is touched.
#
# Run: python test_install_engine_only.py   (or via pytest test_suite.py)
#
import hashlib
import json
import os
import pathlib
import re
import subprocess

import appliance
from tempdirs import tempdir

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"

STUBS = r'''
set -euo pipefail
DRY_RUN=0 ONLY=engine RUN_USER=nobody ENGINE_PORT=8000 AGENT_MODE="" ASSET_KEY=l4t38-cu13
ETC="$T/etc" PREFIX="$T/opt" SANDBOX="" ENV_ADDED=0
log()  { printf 'LOG %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die()  { printf 'DIE %s\n' "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf 'DRY %s\n' "$*"; else "$@"; fi; }
# GNU sha256sum's two forms, on any system (macOS's has no --status).
_sha() { python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
sha256sum() {
  if [ "$1" = -c ]; then local s f; read -r s f; [ -f "$f" ] && [ "$(_sha "$f")" = "$s" ]; return; fi
  [ -f "$1" ] || return 1; printf '%s  %s\n' "$(_sha "$1")" "$1"
}
# sudo reads the unreadable (as root would), or is refused (SUDO_REFUSED=1).
sudo() {
  printf 'sudo %s\n' "$*" >> "$T/sudo.log"
  [ "${SUDO_REFUSED:-0}" = 0 ] || { echo "sudo: a password is required" >&2; return 1; }
  case "$1" in
    -n|-v) return 0 ;;
    cat) [ -e "$2" ] || return 1; chmod u+r "$2"; command cat "$2"; chmod 000 "$2" ;;
    *) return 1 ;;
  esac
}
# SUDO: recorded; the writes land (tee, write_root's install), ownership is skipped (a user
# cannot chown to root, and no test checks it), systemctl is only recorded.
SUDO() {
  printf '%s\n' "$*" >> "$T/sudo.log"
  if [ "$DRY_RUN" = 1 ]; then printf 'DRY sudo %s\n' "$*"; return 0; fi
  case "$1" in
    tee) [ ! -e "$2" ] || chmod u+rw "$2"; command tee "$2" ;;   # root writes through the mode
    install) mkdir -p "$(dirname "${@: -1}")"; cat > "${@: -1}" ;;
    mkdir|rm|touch) "$@" ;;
    *) : ;;
  esac
}
'''


def _fn(name: str) -> str:
    """One function's text out of install.sh, from `name() {` to its closing brace."""
    lines = INSTALL.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _line(prefix: str) -> str:
    """One top-level assignment out of install.sh (e.g. ENGINE_FILES=...)."""
    return next(ln for ln in INSTALL.read_text().splitlines() if ln.startswith(prefix)) + "\n"


# The engine layout as install.sh defines it, under the test's $PREFIX.
LAYOUT = "".join(_line(p) for p in ("ENGINE_STAGE=", "ENGINE_ROLLBACKS=", "ENGINE_SWAP_MARK=", "ENGINE_KEEP=",
                                    "ENGINE_FILES=", "ENGINE_MODELS="))
LAYOUT += 'ENGINE_UNIT="$T/etc/systemd/teaport-engine.service" ENGINE_CHANGED="" ENGINE_KEPT=""\n'
ENGINE_FNS = ["read_env_file", "write_root", "engine_fetch", "engine_entry_py", "engine_describe", "engine_copies",
              "engine_swap_abort", "engine_swap_recover",
              "engine_keep_live", "engine_swap_in", "engine_restore", "engine_rollback_target",
              "engine_prune", "manifest_take_py", "manifest_take"]


def _run(tmp, script, fns=(), env=None):
    body = STUBS + LAYOUT + "".join(_fn(f) for f in fns) + script
    e = {**os.environ, "T": str(tmp), **(env or {})}
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def _sudo_calls(tmp):
    log = pathlib.Path(tmp, "sudo.log")
    return log.read_text().splitlines() if log.exists() else []


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


ENGINE_REL = ("bin/voxtral", "bin/g2p/kokoro_g2p.v2.dict", "bin/g2p/kokoro_pos.bin", "THIRD_PARTY_NOTICES.txt")


def _engine(tmp, tag, release=None, unit=True):
    """A live engine of release <tag> under $T/opt: its four files (each "<tag> <name>"), its
    unit, and (release) an installed manifest that names its binary as that release."""
    opt = pathlib.Path(tmp, "opt")
    for rel in ENGINE_REL:
        _write(opt / rel, f"{tag} {rel.rsplit('/', 1)[-1]}\n")
    if unit:
        _write(pathlib.Path(tmp, "etc/systemd/teaport-engine.service"), f"unit {tag}\n")
    if release:
        _write(opt / "manifest.json", json.dumps({
            "version": "0.0.0", "brain": {"tag": "main"},
            "engine": {"release": release, "assets": {"l4t38-cu13": {"sha256": _sha(f"{tag} voxtral\n")}}}}))
    return opt


def _live(tmp):
    opt = pathlib.Path(tmp, "opt")
    return {rel: (opt / rel).read_text().split()[0] for rel in ENGINE_REL if (opt / rel).exists()}


def _copies(tmp):
    d = pathlib.Path(tmp, "opt/engine.rollback")
    return sorted(os.listdir(d)) if d.exists() else []


# --- arguments ----------------------------------------------------------------------------

def _install(*args, env=None):
    p = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    return p.returncode, p.stdout + p.stderr


def test_the_parser_takes_engine_and_names_it_when_refusing():
    for args, want in ((["--only", "nope"], "--only: unknown component 'nope' (supported: brain, engine)"),
                       (["--rollback=nope"], "--rollback: unknown component 'nope' (supported: brain, engine)"),
                       (["--only"], "--only needs a component (brain or engine)"),
                       (["--only="], "--only needs a component (brain or engine)"),
                       (["--rollback="], "--rollback needs a component (brain or engine)"),
                       (["--only", "engine", "--rollback", "engine"], "--only and --rollback are separate runs"),
                       (["--only=engine", "--rollback=brain"], "--only and --rollback are separate runs")):
        rc, out = _install(*args)
        assert rc == 2 and want in out, (args, out)
    rc, out = _install("--help")
    assert rc == 0 and "--only engine     update just the engine" in out, out
    assert "--rollback engine put the previous engine back" in out and "--only engine does the same" in out, out


def test_main_sends_each_component_to_its_own_path():
    stubs = ''.join(f'{f}() {{ echo "MAIN {f}"; }}\n' for f in
                    ("main_rollback_brain", "main_rollback_engine", "main_brain_only", "main_engine_only"))
    stubs += 'log() { echo "MAIN full"; exit 0; }\n'
    for only, rollback, want in (("engine", "", "main_engine_only"), ("", "engine", "main_rollback_engine"),
                                 ("brain", "", "main_brain_only"), ("", "brain", "main_rollback_brain"),
                                 ("", "", "full")):
        p = subprocess.run(["bash", "-c", f'ONLY="{only}" ROLLBACK="{rollback}" DRY_RUN=0\n{stubs}{_fn("main")}main'],
                           capture_output=True, text=True)
        assert p.stdout.strip() == f"MAIN {want}", (only, rollback, p.stdout + p.stderr)


# --- the dry run's plan, from the real script ----------------------------------------------

def _dry_run(tmp):
    env = {"TEAPORT_PREFIX": f"{tmp}/opt", "TEAPORT_ETC": f"{tmp}/etc", "TEAPORT_STATE": f"{tmp}/state",
           "TEAPORT_SECRETS_DIR": f"{tmp}/secrets", "HOME": f"{tmp}/home", "TMPDIR": tempdir("teaport-mf-"),
           "TEAPORT_MANIFEST_URL": str(REPO / "manifest/stable.json")}
    for k in ("TEAPORT_ALLOW_NON_JETSON", "TEAPORT_BRAIN_SRC", "TEAPORT_ENGINE_PORT"):
        env[k] = ""
    return _install("--only", "engine", "--dry-run", env=env)


def _tree(tmp):
    return sorted(str(p.relative_to(tmp)) for p in pathlib.Path(tmp).rglob("*"))


def test_the_dry_run_shows_the_plan_off_the_appliance_and_touches_nothing():
    tmp = tempdir("teaport-eng-dry-")
    pathlib.Path(tmp, "home").mkdir()
    rc, out = _dry_run(tmp)
    assert rc == 0, out
    for want in ("teaport installer — engine only", "no installed engine here",
                 "not an NVIDIA Jetson", "showing the plan; a real run stops here",
                 "display engine license", "check espeak-ng, libopenblas.so.0 and libcudnn.so.9",
                 f"-o {tmp}/opt/engine.staged/bin/voxtral.part", f"-o {tmp}/opt/engine.staged/bin/g2p/kokoro_g2p.v2.dict.part",
                 f"-o {tmp}/opt/engine.staged/THIRD_PARTY_NOTICES.txt.part",
                 f"-o {tmp}/opt/engine.staged/models/voxtral/consolidated.safetensors.part",   # the models are staged too
                 "nothing to keep for --rollback engine",
                 f"add to {tmp}/etc/engine.env the keys it lacks", "KOKORO_RESERVE_FPT=12", "VOX_REQUIRE_DICT_G2P=1",
                 "render teaport-engine.service.in -> /etc/systemd/system/teaport-engine.service",
                 "restart teaport-engine.service if any of the above changed it",
                 "put the previous engine back if it does not", "done — engine 1.6",
                 "undo:    install.sh --rollback engine"):
        assert want in out, (want, out)
    # The engine alone: no brain build, no packages, no other unit, no env file but engine.env.
    for never in ("uv sync", "apt-get", "brain.env", "teaport-brain.service.in", "venv"):
        assert never not in out, (never, out)
    assert _tree(tmp) == ["home"], _tree(tmp)


def test_the_dry_run_on_an_installed_engine_keeps_what_it_would_and_changes_nothing():
    tmp = tempdir("teaport-eng-dry-")
    pathlib.Path(tmp, "home").mkdir()
    _engine(tmp, "old", release="1.5", unit=False)
    env = _write(pathlib.Path(tmp, "etc/engine.env"), "# tuned on site\nKOKORO_RESERVE_FPT=8\nENGINE_PORT=8000\n")
    before = {p: pathlib.Path(tmp, p).read_bytes() for p in _tree(tmp) if pathlib.Path(tmp, p).is_file()}
    rc, out = _dry_run(tmp)
    assert rc == 0, out
    assert f"keep the live engine (1.5) for --rollback engine: {tmp}/opt/engine.rollback/" in out, out
    assert "KOKORO_RESERVE_FPT=…   (kept as it is)" in out and "ENGINE_PORT=…   (kept as it is)" in out, out
    assert "KOKORO_RESERVE_FPT=12" not in out and "    ENGINE_DELAY=480" in out, out
    after = {p: pathlib.Path(tmp, p).read_bytes() for p in _tree(tmp) if pathlib.Path(tmp, p).is_file()}
    assert after == before and env.read_text().startswith("# tuned on site"), _tree(tmp)


# --- engine.env: only the keys it lacks ----------------------------------------------------

ENV_FNS = ["read_env_file", "env_add_missing"]


def test_missing_keys_are_added_and_every_existing_line_stays():
    tmp = tempdir("teaport-eng-env-")
    # A comment, an operator's own key, a tuned value, an indented key, a commented-out key,
    # and a last line without its newline.
    text = ("# tuned on site: see #264\nOPERATOR_KNOB=7\nKOKORO_RESERVE_FPT=8\n  TTS_CTX=256\n"
            "#ENGINE_DELAY=240\n; old\nENGINE_PORT=8000")
    path = _write(pathlib.Path(tmp, "etc/engine.env"), text)
    rc, out = _run(tmp, f'env_add_missing "{path}" KOKORO_RESERVE_FPT=6 ENGINE_PORT=8000 ENGINE_DELAY=480 '
                        'TTS_CTX=192 VOX_REQUIRE_DICT_G2P=1; echo "ADDED $ENV_ADDED"', ENV_FNS)
    assert rc == 0 and "ADDED 2" in out, out
    assert path.read_text() == text + "\nENGINE_DELAY=480\nVOX_REQUIRE_DICT_G2P=1\n", path.read_text()
    assert "adding 2 key(s)" in out and "ENGINE_DELAY VOX_REQUIRE_DICT_G2P" in out, out


def test_a_file_with_every_key_is_not_written():
    tmp = tempdir("teaport-eng-env-")
    path = _write(pathlib.Path(tmp, "etc/engine.env"), "A=1\nB=2\n")
    rc, out = _run(tmp, f'env_add_missing "{path}" A=9 B=9; echo "ADDED $ENV_ADDED"', ENV_FNS)
    assert rc == 0 and "ADDED 0" in out and "left as it is" in out, out
    assert path.read_text() == "A=1\nB=2\n" and _sudo_calls(tmp) == [], _sudo_calls(tmp)


def test_no_engine_env_gets_every_key():
    tmp = tempdir("teaport-eng-env-")
    path = pathlib.Path(tmp, "etc/engine.env")
    path.parent.mkdir()
    rc, out = _run(tmp, f'env_add_missing "{path}" A=1 B=2', ENV_FNS)
    assert rc == 0 and path.read_text() == "A=1\nB=2\n", out
    assert f"chmod 640 {path}" in _sudo_calls(tmp), _sudo_calls(tmp)


def test_a_root_only_engine_env_is_read_with_sudo_and_never_rewritten_blind():
    appliance.require_modes_bind()   # root reads it anyway: skipped, by name
    tmp = tempdir("teaport-eng-env-")
    path = _write(pathlib.Path(tmp, "etc/engine.env"), "KOKORO_RESERVE_FPT=8\n")
    path.chmod(0o000)
    rc, out = _run(tmp, f'env_add_missing "{path}" KOKORO_RESERVE_FPT=6 TTS_CTX=192', ENV_FNS)
    path.chmod(0o640)
    assert rc == 0 and path.read_text() == "KOKORO_RESERVE_FPT=8\nTTS_CTX=192\n", out
    path.write_text("KOKORO_RESERVE_FPT=8\n")
    path.chmod(0o000)
    rc, out = _run(tmp, f'env_add_missing "{path}" KOKORO_RESERVE_FPT=6 TTS_CTX=192', ENV_FNS, {"SUDO_REFUSED": "1"})
    path.chmod(0o640)
    assert rc != 0 and "DIE cannot read" in out and path.read_text() == "KOKORO_RESERVE_FPT=8\n", out


def test_the_env_dry_run_names_what_it_would_add_and_writes_nothing():
    tmp = tempdir("teaport-eng-env-")
    path = _write(pathlib.Path(tmp, "etc/engine.env"), "KOKORO_RESERVE_FPT=8\n")
    rc, out = _run(tmp, f'DRY_RUN=1; env_add_missing "{path}" KOKORO_RESERVE_FPT=6 TTS_CTX=192 GATEWAY_TOKEN=s3cret',
                   ENV_FNS)
    assert rc == 0 and "add to" in out and "    TTS_CTX=192" in out, out
    assert "KOKORO_RESERVE_FPT=…   (kept as it is)" in out and "GATEWAY_TOKEN=<redacted>" in out, out
    assert "s3cret" not in out and path.read_text() == "KOKORO_RESERVE_FPT=8\n" and _sudo_calls(tmp) == []


def _quoted(text):
    return re.findall(r'"([^"]*)"', text)


def test_the_keys_it_adds_are_the_keys_phase_services_writes():
    # One list: phase_services writes engine_env_lines' keys, and --only engine adds the
    # missing ones from the same call (#158 review: a copy had gone stale at 6 when #315
    # moved phase_services to 8).
    services = _fn("phase_services")
    assert "engine_env_lines" in services and 'write_env "$ETC/engine.env" "${ENGINE_ENV[@]}"' in services, services
    # ... and what it computes, with and without an agent.
    for agent, want in (("", "KOKORO_RESERVE_FPT=12"), ("openclaw", "KOKORO_RESERVE_FPT=8")):
        rc, out = _run(tempdir("teaport-eng-env-"), f'AGENT_MODE={agent}; engine_env_lines; printf "%s\\n" "${{ENGINE_ENV[@]}}"',
                       ["engine_env_lines"])
        assert rc == 0 and out.splitlines()[0] == want and "ENGINE_PORT=8000" in out, out


# --- staged, then swapped in whole ---------------------------------------------------------

PHASE_STUBS = r'''
mget() { python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d = d[k]
print(d)' "$MF" "$1"; }
# A download writes the file the manifest names; FAIL=<name> breaks that one.
download() { [ "${FAIL:-}" != "${2##*/}" ] || die "download failed: ${2##*/}"; printf 'DOWNLOAD %s\n' "$2"; printf 'new %s\n' "${2##*/}" > "$2"; }
ldd() { if [ -n "${LDD_MISSING:-}" ]; then printf '\tlibcudnn.so.9 => not found\n'; fi; }
'''


def _manifest(tmp, keep_pos=False):
    """A manifest whose engine is "new": each file's sha is that of "new <name>" (the
    download stub's), the POS tagger's that of the live "old" one when keep_pos."""
    def art(name, tag="new"):
        return {"url": f"https://dl/{name}", "sha256": _sha(f"{tag} {name}\n")}
    mf = {"engine": {"release": "1.6", "assets": {"l4t38-cu13": art("voxtral")},
                     "g2p_dict": art("kokoro_g2p.v2.dict"),
                     "pos_model": art("kokoro_pos.bin", "old" if keep_pos else "new"),
                     "notices": art("THIRD_PARTY_NOTICES.txt")},
          "models": {"voxtral": art("consolidated.safetensors"), "kokoro": art("Kokoro_espeak_F16.gguf")}}
    return _write(pathlib.Path(tmp, "mf.json"), json.dumps(mf))


def _phase(tmp, only, env=None):
    mf = _manifest(tmp, keep_pos=True)
    return _run(tmp, f'{PHASE_STUBS}MF="{mf}"; ONLY="{only}"; phase_engine; echo "CHANGED$ENGINE_CHANGED"',
                ENGINE_FNS + ["phase_engine"], env)


def test_only_engine_stages_what_differs_and_leaves_the_live_engine_alone():
    tmp = tempdir("teaport-eng-stage-")
    _engine(tmp, "old", release="1.5")
    rc, out = _phase(tmp, "engine")
    assert rc == 0, out
    # The POS tagger already matches: kept, not downloaded. The rest wait in the stage.
    assert ("CHANGED bin/voxtral bin/g2p/kokoro_g2p.v2.dict THIRD_PARTY_NOTICES.txt "
            "models/voxtral/consolidated.safetensors models/kokoro/Kokoro_espeak_F16.gguf\n") in out, out
    assert "have kokoro_pos.bin (sha ok)" in out, out
    assert set(_live(tmp).values()) == {"old"}, _live(tmp)
    stage = pathlib.Path(tmp, "opt/engine.staged")
    assert (stage / "bin/voxtral").read_text() == "new voxtral\n" and os.access(stage / "bin/voxtral", os.X_OK)
    assert _copies(tmp) == [] and "1.5" in pathlib.Path(tmp, "opt/manifest.json").read_text(), out


def test_a_full_run_swaps_the_new_files_in_and_keeps_the_old_engine():
    tmp = tempdir("teaport-eng-stage-")
    _engine(tmp, "old", release="1.5")
    rc, out = _phase(tmp, "")
    assert rc == 0, out
    live = _live(tmp)
    assert live == {"bin/voxtral": "new", "bin/g2p/kokoro_g2p.v2.dict": "new",
                    "bin/g2p/kokoro_pos.bin": "old", "THIRD_PARTY_NOTICES.txt": "new"}, live
    assert not pathlib.Path(tmp, "opt/engine.staged").exists(), out
    (copy,) = _copies(tmp)
    assert re.fullmatch(r"000001-\d{8}T\d{6}Z-1\.5", copy), copy
    kept = pathlib.Path(tmp, "opt/engine.rollback", copy)
    for rel in ENGINE_REL:
        assert (kept / rel).read_text().startswith("old "), rel
    assert (kept / "teaport-engine.service").read_text() == "unit old\n"
    assert json.loads((kept / "engine.json").read_text())["engine"]["release"] == "1.5"
    assert json.loads(pathlib.Path(tmp, "opt/manifest.json").read_text())["engine"]["release"] == "1.6"


def test_an_unresolved_library_or_a_failed_download_swaps_nothing():
    for env, why in (({"LDD_MISSING": "1"}, "unresolved libraries"), ({"FAIL": "kokoro_g2p.v2.dict"}, "download failed")):
        tmp = tempdir("teaport-eng-stage-")
        _engine(tmp, "old", release="1.5")
        rc, out = _phase(tmp, "", env)
        assert rc != 0 and why in out, (env, out)
        assert set(_live(tmp).values()) == {"old"} and _copies(tmp) == [], (env, _live(tmp))
        if why == "unresolved libraries":
            assert "nothing was swapped: the live engine is untouched" in out, out


def test_a_re_run_at_the_same_release_stages_and_keeps_nothing():
    tmp = tempdir("teaport-eng-stage-")
    _engine(tmp, "old", release="1.5")
    rc, out = _phase(tmp, "")
    rc, out = _phase(tmp, "")
    assert rc == 0 and "CHANGED\n" in out and "the live ones are the manifest's" in out, out
    assert len(_copies(tmp)) == 1, _copies(tmp)


# --- the rollback copies: kept, named, pruned ----------------------------------------------

def test_a_binary_the_manifest_does_not_name_is_kept_as_unknown():
    tmp = tempdir("teaport-eng-keep-")
    _engine(tmp, "hand", release=None)   # a hand deploy: no manifest entry for it
    rc, out = _run(tmp, 'engine_keep_live; echo "KEPT $ENGINE_KEPT"', ENGINE_FNS)
    assert rc == 0, out
    (copy,) = _copies(tmp)
    assert copy.endswith("-unknown") and f"KEPT {tmp}/opt/engine.rollback/{copy}" in out, out
    assert json.loads(pathlib.Path(tmp, "opt/engine.rollback", copy, "engine.json").read_text()) == \
        {"engine": {"release": "unknown"}}


def _copy(tmp, name, tag, complete=True):
    d = pathlib.Path(tmp, "opt/engine.rollback", name)
    if complete:
        _write(d / "bin/voxtral", f"{tag} voxtral\n")
        _write(d / "teaport-engine.service", f"unit {tag}\n")
        _write(d / "engine.json", json.dumps({"engine": {"release": tag}}))
    else:
        d.mkdir(parents=True)
    return d


def test_pruning_keeps_the_newest_two_and_nothing_incomplete():
    tmp = tempdir("teaport-eng-prune-")
    _engine(tmp, "live")
    for i, name in enumerate(("000001-20261001T000000Z-1.2", "000002-20261002T000000Z-1.3",
                              "000003-20261003T000000Z-1.4", "000004-20261004T000000Z-1.5")):
        _copy(tmp, name, f"v{i}")
    _copy(tmp, "000006-20261006T000000Z-1.6.partial", "x", complete=False)   # an interrupted copy
    _copy(tmp, "000005-20261005T000000Z-x", "x", complete=False)             # no binary in it
    stray = _copy(tmp, "by-hand-1.4", "hand")                                # not one of ours
    hand = _write(pathlib.Path(tmp, "opt/bin/voxtral.rollback-1.3"), "by hand\n")
    rc, out = _run(tmp, 'engine_rollback_target; engine_prune', ENGINE_FNS)
    assert rc == 0, out
    # The rollback target is the newest complete copy, not the partial or the empty one.
    assert out.splitlines()[0] == f"{tmp}/opt/engine.rollback/000004-20261004T000000Z-1.5", out
    assert _copies(tmp) == ["000003-20261003T000000Z-1.4", "000004-20261004T000000Z-1.5", "by-hand-1.4"], _copies(tmp)
    assert (stray / "bin/voxtral").exists()
    assert hand.read_text() == "by hand\n" and _live(tmp)["bin/voxtral"] == "live"


def test_each_change_keeps_one_and_the_oldest_goes():
    tmp = tempdir("teaport-eng-prune-")
    _engine(tmp, "v1", release="1.1")
    for tag in ("v2", "v3", "v4"):
        _write(pathlib.Path(tmp, "opt/engine.staged/bin/voxtral"), f"{tag} voxtral\n")
        rc, out = _run(tmp, 'ENGINE_CHANGED=" bin/voxtral"; date() { echo "2026110${TAG#v}-000000"; }; engine_swap_in',
                       ENGINE_FNS, {"TAG": tag})
        assert rc == 0 and _live(tmp)["bin/voxtral"] == tag, out
    kept = [pathlib.Path(tmp, "opt/engine.rollback", c, "bin/voxtral").read_text().split()[0] for c in _copies(tmp)]
    assert kept == ["v2", "v3"], (kept, _copies(tmp))


# --- --rollback engine ---------------------------------------------------------------------

ROLLBACK_STUBS = r'''
engine_restart() { echo "RESTART"; [ "${RESTART_FAILS:-0}" = 0 ]; }
engine_dropins_note() { :; }
'''
ROLLBACK_FNS = ENGINE_FNS + ["sudo_ready", "main_rollback_engine"]


def _rollback(tmp, env=None):
    return _run(tmp, ROLLBACK_STUBS + 'main_rollback_engine', ROLLBACK_FNS, env)


def test_rollback_puts_the_previous_engine_back_and_a_second_one_undoes_it():
    tmp = tempdir("teaport-eng-rb-")
    _engine(tmp, "new", release="1.6")
    old = _copy(tmp, "000001-20261001T000000Z-1.5", "old")
    for rel in ENGINE_REL[1:]:
        _write(old / rel, f"old {rel.rsplit('/', 1)[-1]}\n")
    _write(old / "engine.json", json.dumps({"engine": {
        "release": "1.5", "assets": {"l4t38-cu13": {"sha256": _sha("old voxtral\n")}}}}))
    rc, out = _rollback(tmp)
    assert rc == 0 and "done — engine rolled back to 000001-20261001T000000Z-1.5" in out and "RESTART" in out, out
    assert set(_live(tmp).values()) == {"old"}, _live(tmp)
    unit = pathlib.Path(tmp, "etc/systemd/teaport-engine.service")
    assert unit.read_text() == "unit old\n", unit.read_text()
    calls = _sudo_calls(tmp)
    assert "systemctl daemon-reload" in calls and any(c.startswith("install -D -m 0644 -o root -g root") for c in calls), calls
    assert json.loads(pathlib.Path(tmp, "opt/manifest.json").read_text())["engine"]["release"] == "1.5"
    # The one it replaced is the next rollback target.
    (copy,) = _copies(tmp)
    assert copy.endswith("-1.6"), copy
    rc, out = _rollback(tmp)
    assert rc == 0 and set(_live(tmp).values()) == {"new"} and unit.read_text() == "unit new\n", out
    assert json.loads(pathlib.Path(tmp, "opt/manifest.json").read_text())["engine"]["release"] == "1.6"
    assert [c.rsplit("-", 1)[-1] for c in _copies(tmp)] == ["1.5"], _copies(tmp)


def test_rollback_with_nothing_kept_refuses():
    tmp = tempdir("teaport-eng-rb-")
    _engine(tmp, "new")
    _copy(tmp, "000001-20261001T000000Z-1.5.partial", "old", complete=False)
    rc, out = _rollback(tmp)
    assert rc != 0 and "DIE no previous engine to roll back to" in out and "RESTART" not in out, out
    assert set(_live(tmp).values()) == {"new"}


def test_a_rollback_that_does_not_come_up_says_how_to_undo_it():
    tmp = tempdir("teaport-eng-rb-")
    _engine(tmp, "new", release="1.6")
    _copy(tmp, "000001-20261001T000000Z-1.5", "old")
    rc, out = _rollback(tmp, {"RESTART_FAILS": "1"})
    assert rc != 0 and "did not come back on 000001-20261001T000000Z-1.5" in out and "--rollback engine again" in out, out


def test_rollback_dry_run_shows_the_plan_and_moves_nothing():
    tmp = tempdir("teaport-eng-rb-")
    _engine(tmp, "new", release="1.6")
    _copy(tmp, "000001-20261001T000000Z-1.5", "old")
    rc, out = _run(tmp, ROLLBACK_STUBS + 'DRY_RUN=1; main_rollback_engine', ROLLBACK_FNS)
    assert rc == 0 and "keep the live engine (1.6)" in out and "move the engine in" in out, out
    assert "RESTART" not in out and "verify it listens on :8000" in out, out
    assert set(_live(tmp).values()) == {"new"} and _copies(tmp) == ["000001-20261001T000000Z-1.5"]


# --- --only engine's swap: restart, verify, roll back on its own ----------------------------

SWAP_STUBS = r'''
# render_unit writes the checkout's unit (UNIT_TEXT); the restart's verdicts come from
# RESTARTS, one character a restart: 1 up, 0 not.
render_unit() { printf '%s\n' "${UNIT_TEXT:-unit new}" > "$ENGINE_UNIT"; }
engine_dropins_note() { :; }
engine_restart() {
  local n; n="$(cat "$T/restarts" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$T/restarts"
  echo "RESTART on $(cut -d' ' -f1 "$PREFIX/bin/voxtral") / $(cat "$ENGINE_UNIT")"
  if [ -n "${LOSE_KEPT:-}" ]; then rm -rf "$ENGINE_KEPT"; fi   # the kept copy gone before the rollback
  [ "${RESTARTS:$n:1}" = 1 ]
}
engine_verify() { echo VERIFY; }
systemctl() { [ "$1" = is-active ] && [ "${ACTIVE:-1}" = 1 ]; }
'''
SWAP_FNS = ENGINE_FNS + ["sudo_ready", "engine_swap", "env_add_missing", "env_put_back"]


def _swap(tmp, changed=True, env=None):
    pathlib.Path(tmp, "restarts").unlink(missing_ok=True)   # RESTARTS counts from this run's first
    if changed:
        _write(pathlib.Path(tmp, "opt/engine.staged/bin/voxtral"), "new voxtral\n")
    script = f'{SWAP_STUBS}ENGINE_CHANGED="{" bin/voxtral" if changed else ""}"; ENV_ADDED=${{ADDED:-0}}; engine_swap; echo SWAPPED'
    return _run(tmp, script, SWAP_FNS, env)


def test_a_new_engine_that_comes_up_stays():
    tmp = tempdir("teaport-eng-swap-")
    _engine(tmp, "old", release="1.5")
    rc, out = _swap(tmp, env={"RESTARTS": "1"})
    assert rc == 0 and "SWAPPED" in out and "RESTART on new / unit new" in out, out
    assert "new engine files, a new unit" in out, out
    assert _live(tmp)["bin/voxtral"] == "new" and len(_copies(tmp)) == 1, out


def test_a_new_engine_that_does_not_come_up_is_rolled_back_with_its_unit():
    tmp = tempdir("teaport-eng-swap-")
    _engine(tmp, "old", release="1.5")
    rc, out = _swap(tmp, env={"RESTARTS": "01"})
    assert rc != 0 and "DIE rolled back: the engine is running on what it had before this run" in out, out
    assert "RESTART on new / unit new" in out and "RESTART on old / unit old" in out, out
    assert set(_live(tmp).values()) == {"old"} and _copies(tmp) == [], (_live(tmp), _copies(tmp))
    assert pathlib.Path(tmp, "etc/systemd/teaport-engine.service").read_text() == "unit old\n"
    # ... and when the old one does not come back either, the run says that instead.
    tmp = tempdir("teaport-eng-swap-")
    _engine(tmp, "old", release="1.5")
    rc, out = _swap(tmp, env={"RESTARTS": "00"})
    assert rc != 0 and "not healthy there either" in out, out


def test_a_new_unit_alone_that_does_not_come_up_gets_the_previous_unit_back():
    tmp = tempdir("teaport-eng-swap-")
    _engine(tmp, "same", release="1.6")
    rc, out = _swap(tmp, changed=False, env={"RESTARTS": "01"})
    assert rc != 0 and "put back as they were before this run: the unit\n" in out, out
    assert "RESTART on same / unit same" in out, out
    assert pathlib.Path(tmp, "etc/systemd/teaport-engine.service").read_text() == "unit same\n"


def test_nothing_changed_is_checked_not_restarted():
    tmp = tempdir("teaport-eng-swap-")
    _engine(tmp, "same", release="1.6")
    rc, out = _swap(tmp, changed=False, env={"UNIT_TEXT": "unit same", "RESTARTS": "1"})
    assert rc == 0 and "VERIFY" in out and "RESTART" not in out and "not restarting it" in out, out
    # Not running: restarted (a repair), even with nothing new.
    rc, out = _swap(tmp, changed=False, env={"UNIT_TEXT": "unit same", "RESTARTS": "1", "ACTIVE": "0"})
    assert rc == 0 and "RESTART on same" in out and "it is not running" in out, out
    # An engine.env key added: restarted, since the engine reads it only when it starts.
    rc, out = _swap(tmp, changed=False, env={"UNIT_TEXT": "unit same", "RESTARTS": "1", "ADDED": "1"})
    assert rc == 0 and "RESTART on same" in out and "new engine.env keys" in out, out


def test_main_engine_only_refuses_a_box_with_no_engine_and_runs_its_steps_in_order():
    stubs = ''.join(f'{f}() {{ echo "STEP {f}"; }}\n' for f in
                    ("check_brain_source", "fetch_manifest", "eula_gate", "l4t_gate", "engine_libs_check",
                     "phase_engine", "detect_agent", "env_add_missing", "engine_swap", "manifest_take"))
    stubs += 'mget() { echo 1.6; }\nengine_env_lines() { ENGINE_ENV=(A=1); }\nMANIFEST_URL=mf MF="$T/mf.json" HERE="$T/src"\n'
    for installed, dry in ((False, 0), (False, 1), (True, 0)):
        tmp = tempdir("teaport-eng-main-")
        _write(pathlib.Path(tmp, "src/systemd/teaport-engine.service.in"), "x\n")
        if installed:
            _engine(tmp, "old")
        unit = 'systemctl() { [ "$1" = cat ]; }\n' if installed else 'systemctl() { return 1; }\n'
        rc, out = _run(tmp, f'{stubs}{unit}DRY_RUN={dry}; main_engine_only', ["main_engine_only"])
        steps = [ln.split()[1] for ln in out.splitlines() if ln.startswith("STEP ")]
        if not installed and not dry:
            assert rc != 0 and "DIE no installed engine here" in out and steps == [], out
            continue
        assert rc == 0, out
        want = ["check_brain_source", "fetch_manifest", "eula_gate", "l4t_gate", "phase_engine",
                "detect_agent", "env_add_missing", "engine_swap", "manifest_take"]
        if not dry:
            want.insert(4, "engine_libs_check")   # a dry run only says it would check
        assert steps == want, (installed, dry, out)


MODELS = ("models/voxtral/consolidated.safetensors", "models/kokoro/Kokoro_espeak_F16.gguf")


def _models(tmp, tag):
    for m in MODELS:
        _write(pathlib.Path(tmp, "opt", m), f"{tag} {m.rsplit('/', 1)[-1]}\n")


def _live_models(tmp):
    return {m: pathlib.Path(tmp, "opt", m).read_text().split()[0] for m in MODELS}


def test_a_release_that_changes_the_models_puts_the_old_ones_back_when_it_fails():
    # Round 1 (#339): the models went live before the swap, and a rollback left them new.
    tmp = tempdir("teaport-eng-models-")
    _engine(tmp, "old", release="1.5")
    _models(tmp, "old")
    mf = _manifest(tmp)
    pathlib.Path(tmp, "restarts").unlink(missing_ok=True)
    script = f'{PHASE_STUBS}{SWAP_STUBS}MF="{mf}"; ONLY=engine; phase_engine; ENV_ADDED=0; engine_swap; echo SWAPPED'
    rc, out = _run(tmp, script, SWAP_FNS + ["phase_engine"], {"RESTARTS": "01"})
    assert rc != 0 and "the engine files, models and unit" in out and "rolled back" in out, out
    assert set(_live(tmp).values()) == {"old"} and set(_live_models(tmp).values()) == {"old"}, out
    assert "with the model(s) consolidated.safetensors Kokoro_espeak_F16.gguf" in out, out
    # The same release, coming up: the new models are live and the old ones kept, linked.
    tmp = tempdir("teaport-eng-models-")
    _engine(tmp, "old", release="1.5")
    _models(tmp, "old")
    mf = _manifest(tmp)
    rc, out = _run(tmp, script.replace(f'MF="{mf}"', f'MF="{_manifest(tmp)}"'), SWAP_FNS + ["phase_engine"], {"RESTARTS": "1"})
    assert rc == 0 and set(_live_models(tmp).values()) == {"new"}, out
    (copy,) = _copies(tmp)
    for m in MODELS:
        assert pathlib.Path(tmp, "opt/engine.rollback", copy, m).read_text().startswith("old "), m


def test_rollback_moves_the_kept_models_back_and_keeps_the_ones_it_replaces():
    tmp = tempdir("teaport-eng-models-")
    _engine(tmp, "new", release="1.6")
    _models(tmp, "new")
    old = _copy(tmp, "000001-20261001T000000Z-1.5", "old")
    _write(old / MODELS[0], "old consolidated.safetensors\n")   # 1.5 had another STT model
    rc, out = _rollback(tmp)
    assert rc == 0 and _live_models(tmp) == {MODELS[0]: "old", MODELS[1]: "new"}, out
    (copy,) = _copies(tmp)
    kept = pathlib.Path(tmp, "opt/engine.rollback", copy)
    assert (kept / MODELS[0]).read_text().startswith("new ") and not (kept / MODELS[1]).exists(), copy
    rc, out = _rollback(tmp)   # and undone
    assert rc == 0 and set(_live_models(tmp).values()) == {"new"} and _live(tmp)["bin/voxtral"] == "new", out


def test_a_clock_behind_still_makes_the_newest_copy():
    # Round 1 (#339): copies were named by local time, so a box booted in another year (no
    # RTC) made one that sorted oldest, and pruning removed the copy this run had just made.
    tmp = tempdir("teaport-eng-clock-")
    _engine(tmp, "old", release="1.5")
    _copy(tmp, "000001-20261101T000000Z-1.3", "v13")
    _copy(tmp, "000002-20261102T000000Z-1.4", "v14")
    _write(pathlib.Path(tmp, "opt/engine.staged/bin/voxtral"), "new voxtral\n")
    rc, out = _run(tmp, 'date() { echo 20250101T000000Z; }; ENGINE_CHANGED=" bin/voxtral"; engine_swap_in; '
                        'echo "KEPT $ENGINE_KEPT"; engine_rollback_target', ENGINE_FNS)
    assert rc == 0, out
    assert _copies(tmp) == ["000002-20261102T000000Z-1.4", "000003-20250101T000000Z-1.5"], _copies(tmp)
    assert out.splitlines()[-1].endswith("/000003-20250101T000000Z-1.5"), out


def test_pruning_never_removes_the_copy_this_run_made():
    tmp = tempdir("teaport-eng-clock-")
    for name in ("000001-20261101T000000Z-1.3", "000002-20261102T000000Z-1.4", "000003-20261103T000000Z-1.5"):
        _copy(tmp, name, "x")
    rc, out = _run(tmp, 'ENGINE_KEEP=1; ENGINE_KEPT="$ENGINE_ROLLBACKS/000001-20261101T000000Z-1.3"; engine_prune', ENGINE_FNS)
    assert rc == 0 and _copies(tmp) == ["000001-20261101T000000Z-1.3", "000003-20261103T000000Z-1.5"], (_copies(tmp), out)


def test_a_rollback_copy_that_is_gone_is_refused_loudly_and_nothing_restarts():
    tmp = tempdir("teaport-eng-gone-")
    _engine(tmp, "live")
    rc, out = _run(tmp, 'engine_restore "$ENGINE_ROLLBACKS/000009-x-1.0"; echo RESTORED', ENGINE_FNS)
    assert rc != 0 and "DIE cannot put the engine in" in out and "RESTORED" not in out, out
    assert "engine files back" not in out and set(_live(tmp).values()) == {"live"}, out
    # In engine_swap's rollback: the kept copy vanished; the run stops, and restarts nothing on it.
    tmp = tempdir("teaport-eng-gone-")
    _engine(tmp, "old", release="1.5")
    rc, out = _swap(tmp, env={"RESTARTS": "01", "LOSE_KEPT": "1"})
    assert rc != 0 and "DIE cannot put the engine in" in out and out.count("RESTART on") == 1, out


def test_a_rename_that_fails_mid_swap_puts_the_kept_engine_back():
    # Round 1 (#339): a failed mv left a new binary beside an old dict, and nothing undid it.
    tmp = tempdir("teaport-eng-mid-")
    _engine(tmp, "old", release="1.5")
    for rel in ("bin/voxtral", "bin/g2p/kokoro_g2p.v2.dict"):
        _write(pathlib.Path(tmp, "opt/engine.staged", rel), f"new {rel.rsplit('/', 1)[-1]}\n")
    # The rename fails once (FAIL_ONCE) or on every try (a disk gone read-only, say).
    script = ('mv() { case "${@: -1}" in */opt/bin/g2p/kokoro_g2p.v2.dict) '
              'if [ ! -e "$T/failed" ] || [ -z "${FAIL_ONCE:-}" ]; then touch "$T/failed"; echo "mv: simulated failure"; return 1; fi ;; esac; '
              'command mv "$@"; }\nENGINE_CHANGED=" bin/voxtral bin/g2p/kokoro_g2p.v2.dict"; engine_swap_in; echo SWAPPED')
    rc, out = _run(tmp, script, ENGINE_FNS, {"FAIL_ONCE": "1"})
    assert rc != 0 and "could not move bin/g2p/kokoro_g2p.v2.dict into place" in out and "SWAPPED" not in out, out
    assert "back as it was before this run" in out and set(_live(tmp).values()) == {"old"}, (_live(tmp), out)
    assert not pathlib.Path(tmp, "opt/engine.swapping").exists() and _copies(tmp) == [], _copies(tmp)
    # Failing for good: the putting back fails too, says so, and leaves the marker for the next run.
    tmp = tempdir("teaport-eng-mid-")
    _engine(tmp, "old", release="1.5")
    for rel in ("bin/voxtral", "bin/g2p/kokoro_g2p.v2.dict"):
        _write(pathlib.Path(tmp, "opt/engine.staged", rel), f"new {rel.rsplit('/', 1)[-1]}\n")
    rc, out = _run(tmp, script, ENGINE_FNS)
    assert rc != 0 and "DIE could not move bin/g2p/kokoro_g2p.v2.dict back from" in out, out
    assert pathlib.Path(tmp, "opt/engine.swapping").exists(), out


def test_a_swap_a_power_cut_stopped_is_put_right_by_the_next_run():
    tmp = tempdir("teaport-eng-mid-")
    _engine(tmp, "old", release="1.5")
    kept = _copy(tmp, "000001-20261101T000000Z-1.5", "old")
    for rel in ENGINE_REL[1:]:
        _write(kept / rel, f"old {rel.rsplit('/', 1)[-1]}\n")
    _write(pathlib.Path(tmp, "opt/bin/voxtral"), "new voxtral\n")             # the binary went in; the rest did not
    mark = _write(pathlib.Path(tmp, "opt/engine.swapping"), f"{kept}\n")
    rc, out = _run(tmp, 'engine_swap_recover; echo RECOVERED', ENGINE_FNS)
    assert rc == 0 and "stopped half way" in out and set(_live(tmp).values()) == {"old"}, (_live(tmp), out)
    assert not mark.exists() and _copies(tmp) == [], out
    # A marker that names something not ours is not followed.
    _write(pathlib.Path(tmp, "elsewhere/bin/voxtral"), "evil voxtral\n")
    _write(mark, f"{tmp}/elsewhere\n")
    rc, out = _run(tmp, 'engine_swap_recover', ENGINE_FNS)
    assert rc == 0 and "nothing was kept from before it" in out and _live(tmp)["bin/voxtral"] == "old", out
    assert not mark.exists()


def test_rollback_after_an_interrupted_swap_puts_back_the_engine_before_it_and_no_further():
    tmp = tempdir("teaport-eng-mid-")
    _engine(tmp, "mixed", release="1.6")
    _copy(tmp, "000001-20261001T000000Z-1.4", "older")
    kept = _copy(tmp, "000002-20261101T000000Z-1.5", "old")
    _write(pathlib.Path(tmp, "opt/engine.swapping"), f"{kept}\n")
    rc, out = _rollback(tmp)
    assert rc == 0 and "RESTART" in out and _live(tmp)["bin/voxtral"] == "old", out
    assert _copies(tmp) == ["000001-20261001T000000Z-1.4"], _copies(tmp)


def test_a_failed_new_engine_takes_back_the_engine_env_keys_it_added():
    tmp = tempdir("teaport-eng-env-")
    _engine(tmp, "old", release="1.5")
    env = _write(pathlib.Path(tmp, "etc/engine.env"), "# tuned\nKOKORO_RESERVE_FPT=8\n")
    _write(pathlib.Path(tmp, "opt/engine.staged/bin/voxtral"), "new voxtral\n")
    pathlib.Path(tmp, "restarts").unlink(missing_ok=True)
    script = (f'{SWAP_STUBS}env_add_missing "{env}" KOKORO_RESERVE_FPT=6 TTS_CTX=192; '
              'ENGINE_CHANGED=" bin/voxtral"; engine_swap')
    rc, out = _run(tmp, script, SWAP_FNS, {"RESTARTS": "01"})
    assert rc != 0 and ", and engine.env" in out and env.read_text() == "# tuned\nKOKORO_RESERVE_FPT=8\n", out


def test_a_masked_engine_unit_is_refused_before_anything_is_downloaded():
    tmp = tempdir("teaport-eng-mask-")
    _engine(tmp, "old", unit=False)
    unit = pathlib.Path(tmp, "etc/systemd/teaport-engine.service")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.symlink_to("/dev/null")
    stubs = ('MANIFEST_URL=mf\nsystemctl() { [ "$1" = cat ]; }\ncheck_brain_source() { echo STEP; }\n'
             'fetch_manifest() { echo STEP; }\n')
    rc, out = _run(tmp, stubs + 'HERE="$T"; main_engine_only', ["main_engine_only"])
    assert rc != 0 and "DIE teaport-engine.service is masked" in out and "STEP" not in out, out
    rc, out = _run(tmp, stubs + 'HERE="$T"; DRY_RUN=1; main_engine_only', ["main_engine_only"])
    assert "WARN teaport-engine.service is masked" in out, out


def test_a_partial_download_resumes_only_into_the_same_file():
    tmp = tempdir("teaport-eng-resume-")
    _engine(tmp, "old")
    dest = pathlib.Path(tmp, "opt/engine.staged/models/voxtral/consolidated.safetensors")
    _write(f"{dest}.part", "half\n")
    _write(f"{dest}.sha256", "aaaa\n")
    stub = 'download() { if [ -e "$2.part" ]; then echo "RESUME $(cat "$2.part")"; else echo FRESH; fi; }\n'
    rc, out = _run(tmp, stub + 'engine_fetch u models/voxtral/consolidated.safetensors aaaa', ENGINE_FNS)
    assert rc == 0 and "RESUME half" in out, out
    rc, out = _run(tmp, stub + 'engine_fetch u models/voxtral/consolidated.safetensors bbbb', ENGINE_FNS)
    assert rc == 0 and "FRESH" in out and dest.with_name(dest.name + ".sha256").read_text() == "bbbb\n", out


def test_a_download_that_fails_its_sha_is_removed_not_resumed_forever():
    tmp = tempdir("teaport-eng-resume-")
    dest = pathlib.Path(tmp, "f.bin")
    script = ('curl() { local o; while [ $# -gt 0 ]; do [ "$1" = -o ] && o="$2"; shift; done; echo corrupt > "$o"; }\n'
              f'download https://x/f.bin "{dest}" {_sha("good" + chr(10))}')
    rc, out = _run(tmp, script, ["download"])
    assert rc != 0 and "sha256 mismatch" in out and not pathlib.Path(f"{dest}.part").exists(), out


# --- one installer at a time -----------------------------------------------------------------

def _lock_script(tmp):
    body = (STUBS + "".join(_fn(f) for f in ("installer_lock",)) +
            f'STATE="{tmp}/state"\n' + _line("INSTALL_LOCK=") +
            'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ARGS=("$@")\n'
            'installer_lock; echo "LOCKED RUN ${TEAPORT_INSTALL_LOCKED:-no} $*"\n')
    return _write(pathlib.Path(tmp, "install-lock-probe.sh"), body)


def test_a_second_run_stops_at_once_while_one_holds_the_lock():
    if not any(os.access(os.path.join(d, "flock"), os.X_OK) for d in os.environ.get("PATH", "").split(os.pathsep)):
        raise appliance.Unavailable("no flock(1) here (util-linux); the appliance and CI have it")
    tmp = tempdir("teaport-eng-lock-")
    script = _lock_script(tmp)
    env = {**os.environ, "T": str(tmp)}
    p = subprocess.run(["bash", str(script), "--only", "engine"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "LOCKED RUN 1 --only engine" in p.stdout, p.stdout + p.stderr
    lock = pathlib.Path(tmp, "state/install.lock")
    holder = subprocess.Popen(["flock", str(lock), "-c", "echo held; exec sleep 30"], stdout=subprocess.PIPE, text=True,
                              start_new_session=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        p = subprocess.run(["bash", str(script), "--only", "engine"], capture_output=True, text=True, env=env)
        assert p.returncode != 0 and "DIE another install.sh is running on this box" in p.stdout, p.stdout + p.stderr
        assert "LOCKED RUN" not in p.stdout
        # A dry run takes no lock: it says a real run would stop, and goes on.
        p = subprocess.run(["bash", "-c", f'DRY=1; source /dev/stdin <<< "$(sed "s/^installer_lock;/DRY_RUN=1; installer_lock;/" {script})"'],
                           capture_output=True, text=True, env=env)
        assert "a real run stops here" in p.stdout and "LOCKED RUN no" in p.stdout, p.stdout + p.stderr
    finally:
        os.killpg(holder.pid, 9)   # flock and the sleep it holds the lock for
        holder.wait()


def test_without_flock_a_run_says_so_and_goes_on():
    tmp = tempdir("teaport-eng-lock-")
    rc, out = _run(tmp, 'have() { [ "$1" != flock ] && command -v "$1" >/dev/null 2>&1; }\n'
                        f'STATE="{tmp}/state"; HERE="$T"; ARGS=(); {_line("INSTALL_LOCK=")}installer_lock; echo WENT_ON',
                   ["installer_lock"])
    assert rc == 0 and "WARN flock is missing" in out and "WENT_ON" in out, out


# --- the health check ----------------------------------------------------------------------

VERIFY_STUBS = r'''
sleep() { :; }
# INVOCATIONS: the InvocationID each `systemctl show` returns in turn (the last repeats);
# LISTEN_AFTER: how many ss calls before the port is listening; JOURNAL: the journal's text.
systemctl() {
  case "$1" in
    is-active) echo "${STATE:-active}" ;;
    show) local n; n="$(cat "$T/shows" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$T/shows"
          local ids=(${INVOCATIONS:-inv-1}); local i=$n; [ "$i" -lt "${#ids[@]}" ] || i=$((${#ids[@]} - 1)); echo "${ids[$i]}" ;;
  esac
}
ss() { local n; n="$(cat "$T/ss" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$T/ss"
       if [ "$n" -ge "${LISTEN_AFTER:-0}" ]; then echo "LISTEN 0 128 0.0.0.0:${PORT:-8000} 0.0.0.0:*"; fi; }
SUDO() { [ "$1" = journalctl ] && { printf 'JOURNAL %s\n' "$*" >> "$T/journal.log"; printf '%b' "${JOURNAL:-}"; }; return 0; }
'''


def _verify(tmp, env=None):
    return _run(tmp, VERIFY_STUBS + 'engine_verify && echo HEALTHY', ["read_env_file", "engine_verify"], env)


def test_the_engine_is_healthy_once_it_listens_and_stays_up():
    tmp = tempdir("teaport-eng-verify-")
    _write(pathlib.Path(tmp, "etc/engine.env"), 'ENGINE_PORT="9100"\n').chmod(0o640)
    rc, out = _verify(tmp, {"PORT": "9100", "LISTEN_AFTER": "5",
                            "JOURNAL": "dict G2P loaded (v2)\\nPOS tagger loaded\\n"})
    assert rc == 0 and "HEALTHY" in out and "listening on :9100" in out, out
    # The journal asked is this start's alone.
    assert "_SYSTEMD_INVOCATION_ID=inv-1" in pathlib.Path(tmp, "journal.log").read_text()


def test_a_crash_a_wrong_port_or_no_dict_is_not_healthy():
    for env, want in (({"INVOCATIONS": "inv-1 inv-1 inv-2", "LISTEN_AFTER": "5"}, "restarted while it loaded"),
                      ({"INVOCATIONS": "inv-1 inv-1 inv-1 inv-1 inv-2", "LISTEN_AFTER": "0"}, "restarted after it came up"),
                      ({"STATE": "activating"}, "teaport-engine is activating while it loads"),
                      ({"LISTEN_AFTER": "1000"}, "did not listen on :8000 within 3 minutes"),
                      ({"JOURNAL": "dict G2P unavailable: no such file\\n"}, "WITHOUT the en-us G2P dict")):
        tmp = tempdir("teaport-eng-verify-")
        rc, out = _verify(tmp, env)
        assert rc != 0 and want in out and "HEALTHY" not in out, (env, out)


def test_a_missing_pos_tagger_is_said_and_does_not_fail():
    tmp = tempdir("teaport-eng-verify-")
    rc, out = _verify(tmp, {"JOURNAL": "dict G2P loaded\\nPOS tagger unavailable: kokoro_pos.bin\\n"})
    assert rc == 0 and "HEALTHY" in out and "WITHOUT the POS tagger" in out, out


# --- the freshness check looks at what --only engine installs -------------------------------

def _git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env,
                   check=True, capture_output=True)


def _behind(touching):
    """A checkout one upstream commit behind, that commit touching <touching>."""
    tmp = tempdir("teaport-eng-fresh-")
    up, here, other = (os.path.join(tmp, d) for d in ("up.git", "here", "other"))
    _git(tmp, "init", "--bare", up)
    _git(tmp, "clone", up, other)
    for f in ("brain/uv.lock", "cli/teaport", "systemd/teaport-engine.service.in",
              "systemd/teaport-brain.service.in", "docs/x.md", "install.sh"):
        _write(pathlib.Path(other, f), "v1\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "v1")
    _git(other, "push", "origin", "HEAD:main")
    _git(tmp, "clone", up, here)
    pathlib.Path(other, touching).write_text("v2\n")
    _git(other, "commit", "-am", "v2")
    _git(other, "push", "origin", "HEAD:main")
    _git(here, "fetch", "--quiet")
    return tmp, here


def _check(tmp, here):
    script = (f'{_line("BRAIN_ONLY_PATHS=")}{_line("ENGINE_ONLY_PATHS=")}BRAIN_SRC="" ALLOW_STALE_SOURCE=0 '
              f'HERE="{here}"; check_brain_source; echo CHECKED')
    return _run(tmp, script, ["brain_src_fetch", "check_brain_source"],
                {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def test_only_engine_is_refused_only_for_upstream_commits_to_what_it_installs():
    for touching in ("brain/uv.lock", "cli/teaport", "systemd/teaport-brain.service.in", "docs/x.md"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here)
        assert rc == 0 and "CHECKED" in out and "behind" not in out, (touching, out)
    for touching in ("systemd/teaport-engine.service.in", "install.sh"):
        tmp, here = _behind(touching)
        rc, out = _check(tmp, here)
        assert rc != 0 and ("1 commit(s) behind origin/main in what --only engine installs "
                            "(systemd/teaport-engine.service.in install.sh)") in out, (touching, out)


def test_every_checkout_path_only_engine_reads_is_in_its_freshness_check():
    paths = re.findall(r"\((.*)\)", _line("ENGINE_ONLY_PATHS="))[0].split()
    read = set()
    for fn in ("main_engine_only", "engine_swap", "phase_engine", "engine_restore", "engine_keep_live"):
        body = _fn(fn)
        read.update("systemd/" + m for m in re.findall(r"render_unit ([\w.-]+\.in)", body))
        read.update(re.findall(r'\$HERE/([\w./-]+)', body))
    assert "systemd/teaport-engine.service.in" in read, read   # found at all
    uncovered = sorted(r for r in read if not any(r == p or r.startswith(p + "/") for p in paths))
    assert not uncovered, f"read by --only engine but not in ENGINE_ONLY_PATHS: {uncovered}"
    assert "install.sh" in paths


if __name__ == "__main__":
    appliance.run_tests([v for k, v in sorted(globals().items()) if k.startswith("test_")])
