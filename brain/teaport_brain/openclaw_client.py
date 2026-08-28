#
# teaport — OpenClaw gateway loopback client (Phase 1: persona + memory READ)
#
# The Pipecat brain calls the co-resident OpenClaw gateway over loopback to share
# the text agent's memory (and, in Phase 2, to delegate hard turns via consult).
# On loopback the bearer token grants full operator scope, so reads like
# memory_search work directly:
#
#   POST http://127.0.0.1:18789/tools/invoke
#   Authorization: Bearer <gateway-token>
#   { "tool": "memory_search", "args": { "query": "..." } }
#   -> { "ok": true, "result": {...}, "details": { "results": [...] } }
#
# EVERYTHING here is best-effort: a slow, erroring, or unreachable gateway must
# NEVER raise into the realtime pipeline. Callers get None / [] and the voice loop
# continues exactly as before. The gateway's `/tools/invoke` surface is READ-only here
# (it denies exec/shell/fs_write); memory WRITE goes a separate, scoped route —
# `remember_note()` appends to the agent's own daily memory note + a local reindex
# (NOT the gateway's fs_write, NOT a consult; see the §5.2 note below).
#
import asyncio
import datetime
import json
import os
import re
import shutil
import urllib.request

from loguru import logger

GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
TOKEN_FILE = os.path.expanduser("~/.config/teaport/openclaw_token")

# Per-search recall budget. This is NOT the turn's wall-clock budget: MemoryRecall fires
# the search on INTERIM transcripts (re-firing as the query grows) and injects whatever has
# completed at the FINAL transcript, cancelling any straggler then — so the real window from
# first-interim to injection is typically 1-3 s of speech+endpointing. The cap only needs to
# exceed the gateway round-trip, which is the WHOLE memory_search (query embed + hybrid
# vector/text search + envelope), NOT just the raw embed: measured 0.30-0.56 s WARM on the
# local bge-m3, and slower COLD when a TTS synth has evicted the embedder's mmap pages
# (the post-GPU-cutover regime — see docs/MEMORY_RECALL_DESIGN.md). The old 0.25 s default
# was sized for the ~62 ms embed alone, so EVERY search self-aborted before returning and
# recall never triggered. Measured on-box: warm 0.30-0.60 s, COLD (embedder evicted by a synth,
# the common state since the engine keeps RAM tight) ~1.14 s. 1.5 s clears cold with margin; since
# MemoryRecall never awaits in the frame path and cancels any straggler at the final transcript,
# a generous budget adds ZERO turn latency. Tune via TEAPORT_RECALL_TIMEOUT.
RECALL_TIMEOUT = float(os.getenv("TEAPORT_RECALL_TIMEOUT", "1.5"))

# Agent-consult: the rich, SLOW *delegation* primitive for hard questions (a future
# `ask_openclaw` tool). Runs one full OpenClaw agent turn via the gateway — full
# tools/skills/memory on the cerebras model — by shelling out to `openclaw agent`
# (agent-run is a gateway WS protocol, not a REST endpoint, so the CLI drives it). Kept
# validated but UNUSED for memory-write: routing "remember X" through a consult proved
# heavy, non-deterministic (the agent didn't reliably save), and Cerebras-throttle-prone
# (turns hung ~180 s). Memory-write uses the sidecar (`remember_note`) instead. ~3-7 s,
# always masked by speech if/when wired for delegation; never on the hot path.
def _find_openclaw_bin() -> str:
    """The host may have no openclaw install at all (appliances run OpenClaw only
    inside the sandbox). Probe: explicit env -> a CLI on PATH -> the deployment
    shim (~/.config/teaport/openclaw-cli, e.g. a nemoclaw-exec wrapper that runs
    the CLI inside the sandbox) -> the legacy ~/node22 path."""
    env = os.getenv("OPENCLAW_BIN")
    if env:
        return env
    found = shutil.which("openclaw")
    if found:
        return found
    shim = os.path.expanduser("~/.config/teaport/openclaw-cli")
    if os.access(shim, os.X_OK):
        return shim
    return os.path.expanduser("~/node22/bin/openclaw")


OPENCLAW_BIN = _find_openclaw_bin()
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT_ID", "main")
# 45 s, not 30: the sandbox-exec shim adds ~14 s of spawn tax (nemoclaw + docker
# exec + CLI node startup, measured) before the agent turn even starts, and a
# Discord-action turn needs real time after that. Callers cap the voice wait
# separately (TEAPORT_ASK_OPENCLAW_TIMEOUT must stay above ack+consult).
CONSULT_TIMEOUT = float(os.getenv("TEAPORT_CONSULT_TIMEOUT", "45"))

# Memory WRITE goes the sidecar/direct route (NOT the consult — that proved heavy,
# non-deterministic, and Cerebras-throttle-prone, see docs/UNIFIED_AGENT_PLAN.md §5.2):
# append the fact to the SAME daily note the OpenClaw text agent writes, then reindex.
# Shared store → a voice-saved memory is recalled by both voice and text. Deterministic,
# ~instant write + ~7 s background reindex, zero-egress (no LLM call to save a fact).
MEMORY_DIR = os.getenv("OPENCLAW_MEMORY_DIR",
                       os.path.expanduser("~/.openclaw/workspace/memory"))


def _token() -> str | None:
    """Gateway bearer token: OPENCLAW_GATEWAY_TOKEN, else ~/.config/teaport/openclaw_token."""
    tok = os.getenv("OPENCLAW_GATEWAY_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _invoke_sync(tool: str, args: dict, token: str, timeout: float) -> dict | None:
    body = json.dumps({"tool": tool, "args": args}).encode()
    req = urllib.request.Request(
        f"{GATEWAY_URL}/tools/invoke",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


async def invoke_tool(tool: str, args: dict, *, timeout: float = 2.0) -> dict | None:
    """POST /tools/invoke on loopback. Returns the parsed {ok, result, details}
    envelope, or None on any failure (never raises into the pipeline)."""
    token = _token()
    if not token:
        logger.warning("openclaw_client: no gateway token; skipping tool '{}'", tool)
        return None
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _invoke_sync, tool, args, token, timeout),
            timeout=timeout + 0.5,
        )
    except Exception as e:  # noqa: BLE001 — best-effort; the voice loop must not break
        logger.debug("openclaw_client.invoke_tool('{}') failed: {}", tool, e)
        return None


async def invoke_ok(tool: str, args: dict, *, timeout: float = 2.0) -> tuple[bool, dict]:
    """invoke_tool + envelope unwrap, ALSO reporting whether the call succeeded.
    Returns (ok, details). ok=False means the call itself failed — transport error,
    timeout, or a not-ok envelope (e.g. the search provider is down / bot-blocked) —
    which callers MUST tell apart from a genuine empty result (ok=True, empty
    details). invoke_details is the ok-discarding shorthand."""
    env = await invoke_tool(tool, args, timeout=timeout)
    if not env or not env.get("ok"):
        return False, {}
    result = env.get("result") or {}
    return True, (result.get("details") or env.get("details") or {})


async def invoke_details(tool: str, args: dict, *, timeout: float = 2.0) -> dict:
    """invoke_tool + envelope unwrap in one step. The gateway nests the structured
    payload under result.details (a JSON copy is also stringified in
    result.content[0].text); tolerate a top-level details too. Returns {} on any
    failure or a not-ok envelope — callers can chain .get() safely. Use invoke_ok
    when you must distinguish a failure from a genuine empty result."""
    _ok, details = await invoke_ok(tool, args, timeout=timeout)
    return details


def _snippet(item) -> str:
    """Pull a human-readable chunk out of a memory_search result item, tolerating
    the gateway's field-name variations (text / snippet / content / chunk / body)."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("text", "snippet", "content", "chunk", "body", "excerpt"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


async def memory_search(query: str, *, max_results: int = 3,
                        timeout: float = RECALL_TIMEOUT) -> list[str]:
    """Semantic memory recall via the gateway. Returns up to `max_results` snippet
    strings (possibly empty). `timeout` is the search budget (default RECALL_TIMEOUT,
    sized for the local embedder) — fire this on the INTERIM transcript so it overlaps
    endpointing and the result is usually back before the LLM runs; the caller also
    cancels any straggler at the final transcript, so a slow search is harmless."""
    query = (query or "").strip()
    if len(query) < 4:
        return []
    details = await invoke_details("memory_search", {"query": query}, timeout=timeout)
    results = details.get("results") or []
    out = []
    for item in results[:max_results]:
        s = _snippet(item)
        if s:
            out.append(s)
    return out


# The gateway wraps external tool output in <<<EXTERNAL_UNTRUSTED_CONTENT id=...>>>
# markers (prompt-injection hygiene) and appends "Source: <url>" trailers. The voice
# model only needs clean prose to speak a one-sentence summary, so strip both.
_UNTRUSTED = re.compile(r"<<<[^>]*>>>")


def _clean(text: str) -> str:
    if not isinstance(text, str):
        return ""
    # web_fetch prepends a fixed multi-line "SECURITY NOTICE: ..." block before the
    # wrapped content — drop it up to the first wrapper marker.
    text = re.sub(r"SECURITY NOTICE:.*?(?=<<<EXTERNAL_UNTRUSTED)", "", text, flags=re.DOTALL)
    # drop the <<<...>>> wrapper markers and the "Source: ..." / "---" scaffolding.
    text = _UNTRUSTED.sub(" ", text)
    text = re.sub(r"(?m)^\s*Source:.*$", " ", text)
    text = text.replace("---", " ")
    return " ".join(text.split()).strip()


async def web_search(query: str, *, max_results: int = 4, timeout: float = 8.0) -> list[dict] | None:
    """Web search via the gateway's configured provider. Returns up to `max_results`
    {title, snippet, site} dicts (cleaned); [] if the search RAN but found nothing;
    or None if the search itself FAILED (provider down / bot-blocked / timeout), so
    the caller can surface 'search unavailable' rather than a false 'no results'."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    ok, details = await invoke_ok("web_search", {"query": query}, timeout=timeout)
    if not ok:
        return None
    results = details.get("results") or []
    out = []
    for r in results[:max_results]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": _clean(r.get("title", ""))[:140],
            "snippet": _clean(r.get("snippet", ""))[:320],
            "site": r.get("siteName", ""),
        })
    return out


async def web_fetch(url: str, *, max_chars: int = 1600, timeout: float = 10.0) -> str | None:
    """Fetch + extract the readable text of a web page via the gateway. Returns cleaned
    text (truncated to `max_chars`); '' if the page was reached but had no readable
    text; or None if the fetch FAILED (unreachable / blocked / timeout / bad URL), so
    the caller can say 'couldn't reach it' rather than 'no content'."""
    url = (url or "").strip()
    if not url.startswith("http"):
        return None
    ok, det = await invoke_ok("web_fetch", {"url": url}, timeout=timeout)
    if not ok:
        return None
    return _clean(det.get("text", ""))[:max_chars]


async def _run_openclaw(args: list[str], *, timeout: float, capture: bool) -> tuple[int, bytes, bytes]:
    """Run the `openclaw` CLI as a subprocess, returning (returncode, stdout, stderr).
    Adds the CLI's dir to PATH (it needs its sibling node). Cancellable — kills the child
    on CancelledError so a barge-in can abort an in-flight consult. Raises on spawn/timeout."""
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(OPENCLAW_BIN) + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        OPENCLAW_BIN, *args,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        raise
    return proc.returncode, (out or b""), (err or b"")


def _completions_sync(message: str, token: str, timeout: float) -> str | None:
    body = json.dumps({"model": "openclaw",
                       "messages": [{"role": "user", "content": message}]}).encode()
    req = urllib.request.Request(
        f"{GATEWAY_URL}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    choices = d.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    return text.strip() or None


async def _completions_consult(message: str, *, timeout: float) -> str | None:
    """WARM-LANE consult: one full agent turn on the already-running gateway
    daemon via its OpenAI-compatible endpoint — ~7s including Discord channel
    actions, vs ~25s for the cold --local CLI spawn. Requires exactly ONE thing:
    gateway.http.endpoints.chatCompletions.enabled=true in the OpenClaw config.
    That endpoint is a STOCK OpenClaw feature but ships DISABLED ("default:
    false"), so teaport's installer turns it on (install.sh agent_openclaw /
    agent_nemoclaw); on an already-set-up box the manual form is
    `openclaw config set gateway.http.endpoints.chatCompletions.enabled true`
    + a gateway restart. A box where it is off 404s here and the caller falls
    back to the CLI lane.

    The openshell-dialback-locality dist-patch (NemoClaw fork commit 1e72408)
    is NOT a requirement for this call — an earlier version of this note wrongly
    bundled it in. It is gated on OPENSHELL_SANDBOX=1 and only matters when a
    warm-lane turn drives a CHANNEL action THROUGH the NemoClaw sandbox egress
    proxy (the device-pairing dial-back loop). On bare OpenClaw (no sandbox) the
    config flag alone makes this lane work.
    Returns reply text, or None on any failure (never raises)."""
    token = _token()
    if not token:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Deliberately stdlib, not tenacity: the nontrivial part is the deadline
    # budget below, which a retry library doesn't provide. If retry policy
    # ever appears in a second/third caller (web_search backoff, engine
    # reconnects), migrate them all to one shared library then.
    for attempt in (1, 2):
        remaining = deadline - loop.time()
        if remaining < 5:
            return None
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _completions_sync, message, token, remaining),
                timeout=remaining + 0.5)
        except Exception as e:  # noqa: BLE001 — warm lane is best-effort by design
            logger.debug("warm-lane consult attempt {} failed ({!r})", attempt, e)
            if attempt == 1:
                # Sporadic gateway faults happen (observed: one HTTP 408 seven
                # seconds into a healthy turn; a concurrency probe cleared the
                # busy-lane theory). One short retry is far cheaper than
                # surrendering to the ~30s cold-CLI fallback.
                await asyncio.sleep(3)
    logger.debug("warm-lane consult failed twice; falling back to CLI")
    return None


async def agent_consult(message: str, *, session_key: str | None = None,
                        timeout: float = CONSULT_TIMEOUT) -> str | None:
    """Run ONE OpenClaw agent turn via the gateway (the 'consult') and return its reply
    text, or None on any failure. The rich, slow path: full agent context/tools/skills/
    memory on the cerebras model — used for delegated questions and for memory writes (a
    "remember X" turn makes the agent persist the fact to its memory/YYYY-MM-DD.md). The
    memory store is shared across sessions, so a voice-written note is recalled by text too.
    Best-effort: never raises into the realtime pipeline; cancellable (kills the child)."""
    message = (message or "").strip()
    if not message:
        return None
    # Warm lane first: the gateway daemon is already running, so a consult
    # there skips the entire CLI/plugin cold start. Fall through to the cold
    # --local CLI only when the warm lane fails (gateway down, endpoint
    # disabled, mid-rebuild) — it is slower but self-contained.
    _loop = asyncio.get_running_loop()
    _deadline = _loop.time() + timeout
    text = await _completions_consult(message, timeout=timeout)
    if text is not None:
        return text
    # The two lanes share ONE budget. When the warm lane consumed it (a
    # task-inherently-slow turn timing out), rerunning the same doomed request
    # through the ~15s CLI spawn just doubles the wait before an honest
    # failure (observed live: 45s warm + 60s cold = 105s of "hang tight").
    # Under ~20s the CLI cannot finish anyway.
    timeout = _deadline - _loop.time()
    if timeout < 20:
        logger.debug("agent_consult: no budget left after warm lane; giving up")
        return None
    # One fresh session per consult. A shared key ("voice:main") proved doubly
    # broken: rapid consecutive consults collide on one session file ("session
    # file changed while embedded prompt", rc=1), and old failure turns poison
    # the context so the agent answers from memory instead of acting. The
    # brain sends self-contained requests, so no cross-consult context is lost.
    if session_key is None:
        import uuid
        session_key = f"voice:consult-{uuid.uuid4().hex[:10]}"
    try:
        # --local: run the agent embedded in the CLI process. On NemoClaw
        # appliances the default gateway-dispatch path cannot complete channel
        # tool calls (its dial-back lands in an endless device-pairing loop,
        # NemoClaw #4616 family); the embedded run loads channel plugins
        # in-process and their REST calls go out through the egress proxy.
        # Destructive Discord action families stay disabled by the sandbox
        # config gates (channels.discord.actions.*).
        rc, out, err = await _run_openclaw(
            ["agent", "--local", "--agent", OPENCLAW_AGENT, "--session-key", session_key,
             "-m", message, "--json"],
            timeout=timeout, capture=True)
    except asyncio.CancelledError:
        raise  # barge-in: propagate so the task actually cancels
    except Exception as e:  # noqa: BLE001 — incl. TimeoutError; best-effort
        logger.debug("agent_consult failed: {}", e)
        return None
    text = _consult_payload_text(out)
    if rc != 0:
        # Keep the full stderr for postmortem — the log line is truncated.
        try:
            with open("/tmp/teaport-consult-last.err", "wb") as f:
                f.write(err)
        except OSError:
            pass
        tail = err.decode("utf-8", "ignore").strip()[-300:]
        if text:
            # Observed live: the --local run can complete the work (message
            # posted, receipt in stdout) and STILL exit nonzero on teardown.
            # Trust the payload — reporting failure after a successful action
            # is worse than tolerating a noisy exit.
            logger.warning("agent_consult: nonzero exit rc={} but payload present — "
                           "using it ({})", rc, tail[:120] or "(no stderr)")
            return text
        logger.warning("agent_consult: openclaw exited rc={}: {}", rc, tail or "(no stderr)")
        return None
    return text


def _consult_payload_text(out: bytes) -> str | None:
    try:
        raw = out.decode("utf-8", "ignore")
        # --local runs interleave plugin/proxy banners on stdout ahead of the
        # payload; the --json object is pretty-printed, so it starts at the
        # first bare "{" line. Strict whole-stdout json.loads broke on those
        # banners and silently ate every consult.
        lines = raw.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.strip() == "{"), None)
        if start is None:
            return None
        d = json.loads("\n".join(lines[start:]))
        payloads = d.get("payloads") or (d.get("result") or {}).get("payloads") or []
        text = (payloads[0].get("text") if payloads else "") or ""
        return text.strip() or None
    except Exception:  # noqa: BLE001
        return None


async def reindex_memory(*, timeout: float = 30.0) -> bool:
    """Reindex the memory store so a freshly-written note is *semantically* recallable —
    a write is keyword-recallable at once, but the bge-m3 vector index is stale until this
    runs. Subprocess `openclaw memory index`; best-effort, returns whether it succeeded."""
    try:
        rc, _, _ = await _run_openclaw(
            ["memory", "index", "--agent", OPENCLAW_AGENT], timeout=timeout, capture=False)
        return rc == 0
    except Exception as e:  # noqa: BLE001
        logger.debug("reindex_memory failed: {}", e)
        return False


def remember_note(fact: str) -> bool:
    """Append a fact to TODAY's shared memory note (`memory/YYYY-MM-DD.md`) — the same
    file the OpenClaw text agent writes — matching its `[Wed YYYY-MM-DD HH:MM TZ] …`
    line format. The store is shared, so a voice-saved memory is recalled by voice AND
    text. Synchronous + deterministic; the append is ~instant (keyword-recallable at
    once). Call `reindex_memory()` after (in the background) to make it *semantically*
    recallable. Best-effort: returns False on any failure, never raises."""
    fact = " ".join((fact or "").split())  # collapse whitespace/newlines to one line
    if not fact:
        return False
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        now = datetime.datetime.now().astimezone()  # tz-aware local time (for %Z)
        stamp = now.strftime("[%a %Y-%m-%d %H:%M %Z]")
        path = os.path.join(MEMORY_DIR, now.strftime("%Y-%m-%d") + ".md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {fact}\n")
        return True
    except OSError as e:  # best-effort
        logger.debug("remember_note failed: {}", e)
        return False

