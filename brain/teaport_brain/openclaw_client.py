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
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from loguru import logger
from teaport_brain.settings import secret_file, setting

GATEWAY_URL = setting("OPENCLAW_GATEWAY_URL").rstrip("/")
# The last failed CLI consult's full stderr, for a postmortem (the log line is cut).
CONSULT_ERR_FILE = "/tmp/teaport-consult-last.err"

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
RECALL_TIMEOUT = setting("TEAPORT_RECALL_TIMEOUT")

# Agent-consult: the rich, SLOW *delegation* primitive for hard questions (a future
# `openclaw_agent_consult` tool). Runs one full OpenClaw agent turn via the gateway — full
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
    env = setting("OPENCLAW_BIN")
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
# The agent this box talks to: OPENCLAW_AGENT_ID when the operator set it, else None --
# the gateway's own default agent. The consult lanes and the voice context
# (host_context.fetch, ?agent=) name it from here, so a consult and the room's profile
# are about one agent:
#   /v1/chat/completions  model "openclaw/<id>" (OpenClaw's agent model id, 2026.9.7
#                         http-utils.ts resolveAgentIdFromModel: an unconfigured id is a
#                         400), unset "openclaw" -> resolveDefaultAgentId;
#   the voice context     agent=<id>, unset nothing: the route's strict resolver, the
#                         agent the lane's "openclaw" answers with whenever it answers.
# The CLI (the consult fallback and `memory index`) is as it was before: --agent <id>, or
# --agent main when unset (with no --agent, `memory index` would index every agent, and
# the consult would fail on a box with several agents and no default marked). Memory
# and the web tools are not named at all: remember_note writes OPENCLAW_MEMORY_DIR and
# /tools/invoke carries no agent, so they stay with the gateway's default agent.
AGENT_ID = setting("OPENCLAW_AGENT_ID")


def agent_model() -> str:
    """The /v1/chat/completions model that reaches the agent this box talks to."""
    return f"openclaw/{AGENT_ID}" if AGENT_ID else "openclaw"


def agent_args() -> list[str]:
    """The `openclaw` CLI arguments naming the agent: OPENCLAW_AGENT_ID, else "main"."""
    return ["--agent", AGENT_ID or "main"]


# 45 s, not 30: the sandbox-exec shim adds ~14 s of spawn tax (nemoclaw + docker
# exec + CLI node startup, measured) before the agent turn even starts, and a
# Discord-action turn needs real time after that. Callers cap the voice wait
# separately (TEAPORT_OPENCLAW_AGENT_CONSULT_TIMEOUT must stay above ack+consult). This is the
# SYNC path's budget only: the async path (SIP, or a relay that never acks) passes its
# own, TEAPORT_ASYNC_CONSULT_TIMEOUT, since its wait is off the turn (#80).
CONSULT_TIMEOUT = setting("TEAPORT_CONSULT_TIMEOUT")

# Memory WRITE goes the sidecar/direct route (NOT the consult — that proved heavy,
# non-deterministic, and Cerebras-throttle-prone, see docs/UNIFIED_AGENT_PLAN.md §5.2):
# append the fact to the SAME daily note the OpenClaw text agent writes, then reindex.
# Shared store → a voice-saved memory is recalled by both voice and text. Deterministic,
# ~instant write + ~7 s background reindex, zero-egress (no LLM call to save a fact).
MEMORY_DIR = setting("OPENCLAW_MEMORY_DIR")


def _token() -> str | None:
    """Gateway bearer token: OPENCLAW_GATEWAY_TOKEN, else ~/.config/teaport/openclaw_token."""
    return setting("OPENCLAW_GATEWAY_TOKEN") or secret_file("OPENCLAW_GATEWAY_TOKEN")


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


# Where OpenClaw's dreaming keeps its working notes, which memory_search indexes with the
# memory: the light and REM sweeps stage whole past turns as "Candidate: Assistant: ..."
# lines with a confidence and "status: staged", and the deep sweep promotes what it keeps
# into MEMORY.md. They are not memories. Recalled, they handed the voice model earlier
# sessions' answers as things it remembers about the user: on 2026-10-24 it told the
# previous day's top stories as today's before its own lookup came back. Every hit on
# five test queries that morning was one of them.
_NOT_MEMORY = ("memory/dreaming/", "memory/.dreams/")


def _is_memory(item) -> bool:
    """A memory_search hit that is the agent's memory, not its dreaming's notes."""
    path = item.get("path") if isinstance(item, dict) else None
    return not (isinstance(path, str) and path.startswith(_NOT_MEMORY))


async def memory_search(query: str, *, max_results: int = 3,
                        timeout: float = RECALL_TIMEOUT) -> list[str]:
    """Semantic memory recall via the gateway. Returns up to `max_results` snippet
    strings (possibly empty) from the memory itself (_is_memory). `timeout` is the search
    budget (default RECALL_TIMEOUT, sized for the local embedder) — fire this on the
    INTERIM transcript so it overlaps endpointing and the result is usually back before
    the LLM runs; the caller also cancels any straggler at the final transcript, so a slow
    search is harmless."""
    query = (query or "").strip()
    if len(query) < 4:
        return []
    details = await invoke_details("memory_search", {"query": query}, timeout=timeout)
    results = details.get("results") or []
    out = []
    for item in results:
        s = _snippet(item) if _is_memory(item) else ""
        if s:
            out.append(s)
            if len(out) == max_results:
                break
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


def _not_voice(pid: int):
    """The brain's unit puts the voice path last in line for the OOM killer
    (OOMScoreAdjust in teaport-brain.service), and a child inherits that. The cold CLI is
    a whole OpenClaw, not voice: it goes back to the default, so memory pressure takes it
    before the call."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write("0")
    except OSError:
        pass  # not Linux, or the child has already exited


async def _run_openclaw(args: list[str], *, timeout: float, capture: bool) -> tuple[int, bytes, bytes]:
    """Run the `openclaw` CLI as a subprocess, returning (returncode, stdout, stderr).
    Adds the CLI's dir to PATH (it needs its sibling node). Cancellable — kills the child
    on CancelledError so a barge-in can abort an in-flight consult. Raises on spawn/timeout.
    OPENCLAW_GATEWAY_URL is left out of the child's environment: here it is the brain's
    own setting (the HTTP lane, brain.env), but the CLI reads the same name as a gateway
    override and then refuses its configured credentials ("gateway url override requires
    explicit credentials", rc 1). Without it the CLI talks to the gateway its own config
    names, as it does when an operator runs it."""
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_GATEWAY_URL"}
    env["PATH"] = os.path.dirname(OPENCLAW_BIN) + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        OPENCLAW_BIN, *args,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _not_voice(proc.pid)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        raise
    return proc.returncode, (out or b""), (err or b"")


def _completions_sync(message: str, token: str, timeout: float) -> str | None:
    body = json.dumps({"model": agent_model(),
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


@dataclass(frozen=True)
class ConsultOutcome:
    """What one consult produced. `text` is the agent's answer, or None, and then
    `failure` says why: "timeout" (the budget ran out first), "error" (the gateway,
    the agent or its model provider failed or refused), or "empty" (the agent finished
    with nothing to say). `detail` is for the log line and the follow-up's wording."""
    text: str | None
    failure: str | None = None
    detail: str = ""


def _warm_lane_error(e: BaseException) -> tuple[str, str, bool, bool]:
    """(failure, detail, gateway_answered, worth_retrying) for one warm-lane exception.

    `gateway_answered` decides the fallback: a gateway that answered at all is up and
    owns the state directory, so the cold `--local` CLI cannot run beside it (it exits
    rc=1, "A Gateway is running for this state directory"). `worth_retrying` is false
    for the answers a second attempt cannot change: a timeout means the budget is gone,
    and a 4xx such as 402 Payment Required (seen live twice on 2026-10-01: the model
    provider out of credit) refuses the retry exactly as it refused the first."""
    if isinstance(e, urllib.error.HTTPError):
        retry = e.code in (408, 429) or e.code >= 500
        return "error", f"HTTP {e.code} {e.reason}", True, retry
    if isinstance(e, urllib.error.URLError):
        if isinstance(e.reason, TimeoutError):
            return "timeout", "the gateway did not answer in time", True, False
        return "error", f"gateway unreachable ({e.reason})", False, True
    if isinstance(e, TimeoutError):
        return "timeout", "the agent did not finish in time", True, False
    return "error", repr(e), True, True


async def _completions_consult(message: str, *,
                               timeout: float) -> tuple[ConsultOutcome, bool]:
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

    Returns (outcome, gateway_answered); never raises. gateway_answered is None
    when there is no token to ask with, so nothing is known about the gateway."""
    token = _token()
    if not token:
        return ConsultOutcome(None, "error", "no gateway token"), None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    outcome = ConsultOutcome(None, "timeout", "no budget for the warm lane")
    answered = True
    # Deliberately stdlib, not tenacity: the nontrivial part is the deadline
    # budget below, which a retry library doesn't provide. If retry policy
    # ever appears in a second/third caller (web_search backoff, engine
    # reconnects), migrate them all to one shared library then.
    for attempt in (1, 2):
        remaining = deadline - loop.time()
        if remaining < 5:
            break
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, _completions_sync, message, token, remaining),
                timeout=remaining + 0.5)
        except Exception as e:  # noqa: BLE001 — warm lane is best-effort by design
            failure, detail, answered, retry = _warm_lane_error(e)
            outcome = ConsultOutcome(None, failure, detail)
            logger.info("warm-lane consult attempt {} failed: {} ({})",
                        attempt, failure, detail)
            if attempt == 1 and retry:
                # Sporadic gateway faults happen (observed: one HTTP 408 seven
                # seconds into a healthy turn; a concurrency probe cleared the
                # busy-lane theory). One short retry is far cheaper than
                # surrendering to the ~30s cold-CLI fallback.
                await asyncio.sleep(3)
                continue
            break
        if text is None:
            # The turn ran and ended with no text. Asking again would rerun the
            # whole turn (and repeat any action it took) for the same nothing.
            return ConsultOutcome(None, "empty", "the agent finished with no text"), True
        return ConsultOutcome(text), True
    return outcome, answered


# HTTP answers that mean the warm lane itself is missing (the chat-completions
# endpoint is disabled), not that the turn failed: only these are worth the CLI.
_LANE_MISSING = ("HTTP 404", "HTTP 405")


async def agent_consult(message: str, *, session_key: str | None = None,
                        timeout: float = CONSULT_TIMEOUT) -> ConsultOutcome:
    """Run ONE OpenClaw agent turn via the gateway (the 'consult') and return its
    outcome. The rich, slow path: full agent context/tools/skills/memory on the agent's
    model — used for delegated questions. Best-effort: never raises into the realtime
    pipeline; cancellable (kills the child)."""
    message = (message or "").strip()
    if not message:
        return ConsultOutcome(None, "error", "empty request")
    # Warm lane first: the gateway daemon is already running, so a consult
    # there skips the entire CLI/plugin cold start.
    _loop = asyncio.get_running_loop()
    _deadline = _loop.time() + timeout
    warm, answered = await _completions_consult(message, timeout=timeout)
    if warm.text is not None:
        return warm
    # The CLI is a fallback for a MISSING warm lane, not a second try at a failed
    # turn. A timeout, an empty turn, a 402 or a 5xx came from the same gateway and
    # model the CLI would use, so rerunning the request there only doubles the wait
    # before the same failure (observed live: 45s warm + 60s cold = 105s of "hang
    # tight"; and 2026-10-01, 402 twice and then a CLI that could not even start).
    if answered and not warm.detail.startswith(_LANE_MISSING):
        return warm
    timeout = _deadline - _loop.time()
    # Under ~20s the CLI cannot finish anyway.
    if timeout < 20:
        logger.debug("agent_consult: no budget left after warm lane; giving up")
        return ConsultOutcome(None, "timeout", f"no budget left for the CLI ({warm.detail})")
    # One fresh session per consult. A shared key ("voice:main") proved doubly
    # broken: rapid consecutive consults collide on one session file ("session
    # file changed while embedded prompt", rc=1), and old failure turns poison
    # the context so the agent answers from memory instead of acting. The
    # brain sends self-contained requests, so no cross-consult context is lost.
    if session_key is None:
        session_key = f"voice:consult-{uuid.uuid4().hex[:10]}"
    args = ["agent", *agent_args(), "--session-key", session_key,
            "-m", message, "--json"]
    if not answered:
        # --local only when no gateway answered: it runs the agent embedded in the
        # CLI process, which the CLI refuses while a gateway owns the state directory
        # ("A Gateway is running for this state directory ... Run without --local to
        # use it"). With the gateway up but its chat-completions endpoint disabled,
        # the plain CLI dispatches the turn through the gateway instead.
        #
        # On NemoClaw appliances the embedded run is also what completes channel tool
        # calls (gateway dispatch lands in an endless device-pairing loop, NemoClaw
        # #4616 family); the installer enables the warm lane there, so this path is
        # only reached when that gateway is down.
        args.insert(1, "--local")
    try:
        rc, out, err = await _run_openclaw(args, timeout=timeout, capture=True)
    except asyncio.CancelledError:
        raise  # barge-in: propagate so the task actually cancels
    except TimeoutError:
        return ConsultOutcome(None, "timeout", "the CLI agent did not finish in time")
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug("agent_consult failed: {}", e)
        return ConsultOutcome(None, "error", f"CLI failed to run ({e!r})")
    text = _consult_payload_text(out)
    if rc != 0:
        # Keep the full stderr for postmortem — the log line is truncated.
        try:
            with open(CONSULT_ERR_FILE, "wb") as f:
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
            return ConsultOutcome(text)
        logger.warning("agent_consult: openclaw exited rc={}: {}", rc, tail or "(no stderr)")
        return ConsultOutcome(None, "error", f"CLI exited rc={rc}: {tail[-120:]}")
    if not text:
        return ConsultOutcome(None, "empty", "the CLI agent finished with no text")
    return ConsultOutcome(text)


def cli_json(out: bytes, want=lambda d: True) -> dict | None:
    """The CLI's --json result: the last top-level JSON object on stdout that `want`
    accepts. Plugin and proxy banners can come before it (and, from a --local run,
    after it), so stdout is not one document; each object starts on a line of its own,
    one line (dashboard) or pretty-printed (devices list, agent). Objects nested inside
    one already read are not candidates. None when no object qualifies: a caller that
    needs a key says so in `want` rather than reading a banner's object as an empty
    result."""
    raw = out.decode("utf-8", "ignore")
    decoder = json.JSONDecoder()
    found, pos = None, 0
    for m in re.finditer(r"^[ \t]*\{", raw, re.M):
        start = m.end() - 1
        if start < pos:
            continue  # inside an object already read
        try:
            obj, end = decoder.raw_decode(raw, start)
        except ValueError:
            continue
        pos = end
        if isinstance(obj, dict) and want(obj):
            found = obj
    return found


def _consult_payload_text(out: bytes) -> str | None:
    # --local runs interleave plugin/proxy banners on stdout ahead of the payload;
    # strict whole-stdout json.loads broke on those banners and silently ate every
    # consult. cli_json skips them.
    d = cli_json(out, lambda d: "payloads" in d or isinstance(d.get("result"), dict))
    if d is None:
        return None
    try:
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
            ["memory", "index", *agent_args()], timeout=timeout, capture=False)
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

