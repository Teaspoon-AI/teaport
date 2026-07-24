#
# teaport — shared persona (Phase 1 of the unified voice + text agent)
#
# The voice brain's system prompt is composed as:   persona  +  VOICE_OVERLAY
#
#   persona       — the assistant's IDENTITY / "soul". Single source of truth is a
#                   shared file (TEAPORT_PERSONA_FILE, default
#                   ~/.config/teaport/persona.md) that the OpenClaw *text* agent can
#                   read too, so a user who talks hears the same assistant they text.
#                   If the file is missing/empty we fall back to FALLBACK_PERSONA so
#                   the realtime loop never hard-fails on a persona lookup.
#   VOICE_OVERLAY — delivery-only, voice-surface mechanics that must NOT leak into
#                   the text agent: which local tools exist, spell-out-numbers,
#                   one-to-two spoken sentences, and the heard-grounding /
#                   theory-of-mind paragraph. The overlay text is carefully tuned
#                   (offline theory-of-mind evals lifted grounding 4/21 -> 14/21);
#                   preserve its wording, don't reword casually.
#
# Splitting persona out of the old monolithic SYSTEM_PROMPT is what lets OpenClaw
# own identity while Pipecat keeps the tuned voice mechanics.
#
import os

# --- identity (OpenClaw owns this at runtime; this is only the offline fallback).
FALLBACK_PERSONA = (
    "You are a friendly voice assistant that runs on a small local device: a "
    "local speech engine hears the user, a language model thinks, and a local "
    "voice speaks. You are warm, concise, and genuinely helpful — the same "
    "assistant whether the user reaches you by voice or text."
)

# --- voice-surface mechanics (tools + delivery + heard-grounding). Moved verbatim
#     from the old monolithic SYSTEM_PROMPT; the heard-grounding paragraph is tuned.
VOICE_OVERLAY = (
    "You have tools — use them instead of guessing: get_host_status (this "
    "machine's live free memory, CPU load, decode speed), get_current_time, "
    "web_search (search the web for anything current, factual, or that you don't "
    "know), web_fetch (read a specific web page), search_memory (recall what "
    "the user told you before, by voice or text), ask_openclaw (your full desktop "
    "agent — every tool, deeper thinking; for multi-step or open-ended requests "
    "your quick tools can't handle), and list_voices / switch_voice (your "
    "speaking voices). If the user starts speaking a different language, "
    "switch_voice to a voice for that language and reply in it from then on; "
    "switch back the same way if they return to the previous language. "
    "When the user asks about your "
    "status or the time, about something recent or factual you'd need to look up, "
    "a web page, or something they told you earlier, call the matching tool "
    "directly. Quick lookups (time, host status, memory) need no preamble — just "
    "call silently. A web search, page fetch, or ask_openclaw takes seconds, so "
    "first say one short natural sentence about what you're doing — in your own "
    "words, specific to this request, never a stock phrase — then call the tool "
    "in the same response. Never say a tool's name or write any <function> text "
    "in your reply. For everything else, just chat normally and helpfully — you "
    "are a capable conversational assistant, not only a tool caller. "
    "Your words are spoken aloud, so write them as they should be SAID, not "
    "written: spell out numbers, dates, times, units, and symbols. Say 'ten "
    "ten PM' not '10:10 PM', 'June tenth' not 'June 10', 'about twenty-nine "
    "milliseconds' not '28.7 ms', 'megabytes' not 'MB'. Round awkward "
    "decimals and expand abbreviations. "
    "Answer in one or two short spoken sentences: no markdown, lists, or emojis. "
    # Theory-of-mind grounding: DESCRIPTIVE (model the listener's perception), not
    # the deontic "never reveal the unheard part" that spiraled the model into
    # refusals. Stops the model answering "what did you say?" from a RETAINED tool
    # result (the distractor that produced "nutmeg" after a correct truncation).
    "Keep the listener's point of view in mind: this is a voice call, so they hear "
    "only the words you actually speak aloud. Tool results and anything else in "
    "your context that you did not say are invisible to them — that information "
    "lives only in your own mind. So when they ask about, or try to recall, what "
    "you said or the last thing you said, they mean your spoken words, not the "
    "tool's data; if they remember it wrong, gently go by what they actually heard. "
    "If your speech was cut off, they heard only what you spoke before the "
    "interruption."
)

# Shared source of truth for identity. The OpenClaw text agent should read the same
# file (e.g. symlinked from its workspace AGENTS.md) so both surfaces are one agent.
PERSONA_FILE = os.getenv(
    "TEAPORT_PERSONA_FILE", os.path.expanduser("~/.config/teaport/persona.md")
)


# --- OpenClaw workspace context. Read the same files the OpenClaw text agent gets
# injected, LIVE from its workspace, so an identity or user-context edit on the text
# side is heard on the voice side at the next session — no more stale persona.md copy.
# Deliberately NOT included by default: AGENTS.md / TOOLS.md / HEARTBEAT.md —
# text-surface delivery mechanics; the tuned VOICE_OVERLAY owns delivery on this
# surface and the two would fight. (Note: IDENTITY.md is agent-writable by design —
# the agent updates its own identity in conversation — so whatever lands there is
# injected here too; that's the intended shared-identity behavior per the user.)
WORKSPACE_DIR = os.getenv(
    "OPENCLAW_WORKSPACE", os.path.expanduser("~/.openclaw/workspace")
)
WORKSPACE_FILES = [
    f.strip() for f in os.getenv(
        "TEAPORT_WORKSPACE_FILES", "SOUL.md,IDENTITY.md,USER.md,MEMORY.md").split(",")
    if f.strip()
]
# One runaway file (e.g. a fast-growing MEMORY.md) must not eat the prompt budget —
# prompt tokens are prefill latency on every turn.
_WORKSPACE_FILE_CAP = 16 * 1024


def load_workspace_context() -> str:
    """Concatenate the configured OpenClaw workspace files (each is a markdown
    section with its own heading). Missing/empty files are skipped silently —
    the caller falls back to the persona file, then FALLBACK_PERSONA."""
    sections = []
    for name in WORKSPACE_FILES:
        try:
            with open(os.path.join(WORKSPACE_DIR, name), encoding="utf-8") as f:
                text = f.read(_WORKSPACE_FILE_CAP).strip()
        except OSError:
            continue
        if text:
            sections.append(text)
    return "\n\n".join(sections)


def load_persona() -> str:
    """The shared identity + context text. Prefers the live OpenClaw workspace
    files (the same ones the text agent reads); falls back to TEAPORT_PERSONA_FILE,
    then the baked-in FALLBACK_PERSONA, so the voice loop never hard-fails."""
    ws = load_workspace_context()
    if ws:
        return ws
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return FALLBACK_PERSONA


def build_system_prompt(persona: str | None = None) -> str:
    """Compose the voice system prompt: shared identity + voice-only overlay.
    Pass a persona (e.g. fetched at session start) or leave None to load it."""
    base = (persona or "").strip() or load_persona()
    return base + "\n\n" + VOICE_OVERLAY
