#
# teaport — the voice system prompt: identity + the voice overlay
#
# The voice brain's system prompt is composed as:   identity  +  overlay
#
#   identity  — who the assistant is. OpenClaw composes it for a voice surface
#               (host_context.py) and it heads the prompt as sent:
#                 Talk        req.instructions: OpenClaw's realtime voice rules, the
#                             operator's talk.realtime.instructions, the agent paragraph
#                             and the IDENTITY / USER / SOUL profile (HOST_RULES);
#                 room, phone the agent paragraph and the profile, from the teaport
#                             plugin's voice-context route (HOST_CONTEXT).
#               Without it (TEAPORT_AGENT=none, or a gateway that cannot answer) it is
#               the persona file TEAPORT_PERSONA_FILE (default ~/.config/teaport/
#               persona.md), else FALLBACK_PERSONA, so the realtime loop never hard-fails
#               on a persona lookup.
#   overlay   — the voice-surface mechanics that must NOT leak into the text agent, in
#               three parts:
#                 tools     tools_paragraph: which of the brain's tools this session has
#                           and how to use them. Tuned paragraphs for the default sets
#                           (with a gateway, _TOOLS_WITH_AGENT; voice-only, _TOOLS_LOCAL,
#                           which says plainly what the assistant cannot do), composed from
#                           the tools' own hints otherwise.
#                 consult   only under OpenClaw's text (_host_paragraph): the consult leaves
#                           the tools paragraph and gets its own, shaped by what the host
#                           text covers (HOST_RULES overrides the Talk rules that clash with
#                           this surface; HOST_CONTEXT says when to consult). Its wording
#                           was chosen by the #230 eval (arm B2).
#                 delivery  _DELIVERY: spell out numbers, one or two spoken sentences, and
#                           the heard-grounding / theory-of-mind paragraph, shared verbatim
#                           by every prompt.
#               The overlay text is carefully tuned (offline theory-of-mind evals lifted
#               grounding 4/21 -> 14/21); preserve its wording, don't reword casually.
#
# So, by source of the identity:
#   persona file       persona + tools (consult named among them) + delivery
#                      (= VOICE_OVERLAY / VOICE_OVERLAY_LOCAL for the default tools)
#   Talk               OpenClaw's text + tools + consult (HOST_RULES) + delivery
#   room, phone        OpenClaw's text + tools + consult (HOST_CONTEXT) + delivery
#   (and other /talk clients: the Discord bridge)
#
import os

from loguru import logger

from teaport_brain.agent_backend import HAS_AGENT
from teaport_brain.settings import setting

# --- identity (OpenClaw owns this at runtime; this is only the offline fallback).
FALLBACK_PERSONA = (
    "You are a friendly voice assistant that runs on a small local device: a "
    "local speech engine hears the user, a language model thinks, and a local "
    "voice speaks. You are warm, concise, and genuinely helpful — the same "
    "assistant whether the user reaches you by voice or text."
)

# --- voice-surface mechanics (tools + delivery + heard-grounding). Moved verbatim
#     from the old monolithic SYSTEM_PROMPT; the heard-grounding paragraph is tuned.
#     Composed below as tools paragraph + _DELIVERY; VOICE_OVERLAY is the gateway one.
#
# The three fragments below are shared VERBATIM between _TOOLS_WITH_AGENT and
# _TOOLS_LOCAL. They used to be duplicated inline in both strings — a later wording
# fix landing in one and not the other would drift silently, since tests only assert
# substring presence. Factored out once so there is one place to edit.
_VOICE_SWITCH_INSTRUCTION = (
    "list_voices / switch_voice (your "
    "speaking voices). If the user starts speaking a different language, "
    "switch_voice to a voice for that language and reply in it from then on; "
    "switch back the same way if they return to the previous language."
)
_NO_TOOL_NAME_INSTRUCTION = (
    "Never say a tool's name or write any <function> text in your reply."
)
_GENERAL_CHAT_CLOSER = (
    "For everything else, just chat normally and helpfully — you "
    "are a capable conversational assistant, not only a tool caller. "
)
# The slow-tool preamble and the consult-in-progress rule, shared by the tuned
# gateway paragraph and the composed one (tools_paragraph).
_PREAMBLE_INSTRUCTION = (
    "takes seconds, so "
    "first say one short natural sentence about what you're doing — in your own "
    "words, specific to this request, never a stock phrase — then call the tool "
    "in the same response."
)
_CONSULT_IN_PROGRESS = (
    "While an openclaw_agent_consult request is still in progress you do not "
    "yet know its answer: never guess or make one up, even if the user says 'go "
    "ahead' or 'start' — say it is still in progress; you will be told the moment "
    "it completes."
)
_CANNOT_LOOK_UP = (
    "You cannot search "
    "the web, open a web page, or recall earlier conversations: if the user asks "
    "for something you would need to look up, say so plainly and help with what "
    "you know rather than guessing or promising to check."
)

# The tuned gateway paragraph, in two versions from the same words: with the consult
# named among the tools (no host text: the persona file), and without it, for a session
# whose host text came from OpenClaw -- there the consult has its own paragraph after the
# tools (_host_paragraph), and this one keeps only the brain's quick tools. The rest is
# byte-identical between the two (the #230 eval ran the second as arm A/B2's tools part).
_GATEWAY_TOOLS_HEAD = (
    "You have tools — use them instead of guessing: get_host_status (this "
    "machine's live free memory, CPU load, decode speed), get_current_time, "
    "web_search (search the web for anything current, factual, or that you don't "
    "know), web_fetch (read a specific web page), search_memory (recall what "
    "the user told you before, by voice or text), "
)
_CONSULT_HINT = (
    "openclaw_agent_consult (your full desktop "
    "agent — every tool, deeper thinking; for multi-step or open-ended requests "
    "your quick tools can't handle)"
)
_GATEWAY_TOOLS_WHEN = (
    " "
    "When the user asks about your "
    "status or the time, about something recent or factual you'd need to look up, "
    "a web page, or something they told you earlier, call the matching tool "
    "directly. Quick lookups (time, host status, memory) need no preamble — just "
    "call silently. "
)


def _gateway_tools(consult: bool) -> str:
    return (_GATEWAY_TOOLS_HEAD + (_CONSULT_HINT + ", " if consult else "") + "and "
            + _VOICE_SWITCH_INSTRUCTION + _GATEWAY_TOOLS_WHEN
            + ("A web search, page fetch, or openclaw_agent_consult " if consult
               else "A web search or page fetch ")
            + _PREAMBLE_INSTRUCTION + " " + _NO_TOOL_NAME_INSTRUCTION + " "
            + (_CONSULT_IN_PROGRESS + " " if consult else "") + _GENERAL_CHAT_CLOSER)


_TOOLS_WITH_AGENT = _gateway_tools(consult=True)
_TOOLS_HOSTED = _gateway_tools(consult=False)

# The voice-only box: the same local tools, and an honest statement of what is not
# there. Named so the model says "I can't look that up" instead of the ack it would
# otherwise give before a search that never happens.
_TOOLS_LOCAL = (
    "You have tools — use them instead of guessing: get_host_status (this "
    "machine's live free memory, CPU load, decode speed), get_current_time, and "
    + _VOICE_SWITCH_INSTRUCTION + " "
    "When the user asks about your status or the time, call the matching "
    "tool directly; these are quick, so no preamble — just call silently. "
    + _NO_TOOL_NAME_INSTRUCTION + " " + _CANNOT_LOOK_UP + " " + _GENERAL_CHAT_CLOSER
)

_DELIVERY = (
    "Your words are spoken aloud, so write them as they should be SAID, not "
    "written: spell out numbers, dates, times, units, and symbols. Say 'ten "
    "ten PM' not '10:10 PM', 'June tenth' not 'June 10', 'about twenty-nine "
    "milliseconds' not '28.7 ms', 'megabytes' not 'MB'. Round awkward "
    "decimals and expand abbreviations. Never read out a web address or URL — "
    "name the source instead ('on the NIST site', 'from Anthropic'). "
    # DELIVERY LENGTH AND SHAPE. This was one clause ("Answer in one or two short spoken
    # sentences: no markdown, lists, or emojis") buried at the end of the tool paragraph,
    # and the model ignored both halves. Live 2026-09-09, one 18-minute SIP call: 19 of
    # 220 synth texts opened with a bullet, eight replies ran past fifteen seconds, one
    # assistant turn ran thirty-seven, and the user's own words in the ledger were
    # "Soliloquy.", "Okay, stop.", "You're rambling." and "They're useless."
    #
    # So the rule now names the MECHANISM (the synth pronounces the marks), gives the
    # list-shaped answer somewhere to go instead of nowhere, and gives brevity a reason
    # the model can weigh against helpfulness — an over-long answer is not merely verbose
    # here, it gets barged over and lost, and on that call twelve replies were cut before
    # a single word of them was heard. tts_text._LIST_MARKER strips the marks that arrive
    # anyway; this is the half that keeps the SHAPE conversational, which no strip can.
    #
    # The last sentence is about prompt dilution: identity comes from OpenClaw's profile
    # files (IDENTITY/USER/SOUL.md; the workspace was read directly then), which are
    # markdown by nature — 3322 chars of headings and bullets on that call — so the model
    # reads a formatted document and then, a page later, is told not to write one.
    "Answer in one or two short spoken sentences. You are speaking, not writing: never "
    "use markdown, headings, bullet points, numbered lists, or emojis — the voice reads "
    "those marks aloud. When the answer is list-shaped, say the two or three items that "
    "matter as a sentence and offer the rest, like 'they've got pastries, sandwiches and "
    "a daily soup — want me to go through any of those?'. Never recite more than three "
    "items unless the user asks you to. A long answer gets talked over and lost, so stop "
    "at the useful part and let them ask for more. The identity text above is written "
    "for the screen; never copy its formatting into speech. "
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

VOICE_OVERLAY = _TOOLS_WITH_AGENT + _DELIVERY
VOICE_OVERLAY_LOCAL = _TOOLS_LOCAL + _DELIVERY

# --- When OpenClaw's text heads the prompt (host_context.py), the consult gets a
#     paragraph of its own, after the tools and before _DELIVERY. What it says depends on
#     what the host text already covers:
#
#   HOST_RULES    a Talk session: OpenClaw's realtime voice rules lead the host text
#                 (DEFAULT_REALTIME_INSTRUCTIONS: call openclaw_agent_consult, say "Let me
#                 check that for you", call openclaw_agent_control for progress, cancel or
#                 redirect), then the agent paragraph and the profile. Some of those rules
#                 clash with this surface, so the paragraph opens by overriding them: the
#                 stock acknowledgement gives way to the tuned preamble. The control tool is
#                 the model's here (tools.py, run control: the Talk client runs it), so the
#                 paragraph says when to call it, for what OpenClaw's own spoken run control
#                 did not already take (run_control.py).
#   HOST_CONTEXT  the room mic and the phone: the agent paragraph and the profile only
#                 (the plugin's voice-context route). Nothing says when to consult and
#                 nothing runs run control, so the paragraph says when to consult, and says
#                 plainly that a request in progress cannot be stopped or changed from here.
#
# Both keep the tuned preamble, the in-progress rule, and two lines the #230 eval found
# the host text needs (34 synthetic cases x 10, gpt-oss-120b, 2026-10-25, arm B2): answer
# from the conversation instead of re-consulting for what is already in it, and never
# describe the agent's other sessions, which the agent paragraph tells the model exist
# but it cannot see. Without them: re-consults, and other sessions described from
# nothing. A tool the host's rules name that the session lacks is named as missing (a
# call to a tool not offered fails the whole completion on Groq, and the user hears
# nothing: 20% of in-progress turns when the control was named but not offered).
HOST_RULES = "rules"
HOST_CONTEXT = "context"

_HOST_RULES_WIN = "Where the instructions above differ, these win."
# Talk with the control tool: when to call it, and when not. Last in the paragraph, after
# the B2 lines: placed first (an earlier wording), it pulled the model toward tools on
# turns that only needed the conversation (re-consults on the grounding cases 17/90
# against 8/90 without it; 1/10 on "okay, go on" here against 6/10 there) and read small
# talk ("how are you doing today?") as a progress question (9/10 status calls). Straight
# away, because the Talk client holds the tool's result until the voice has finished
# what it is saying (a line before the call delays the answer by that line). #230 eval,
# synthetic cases x 10, gpt-oss-120b, 2026-10-25.
_RUN_CONTROL_TOOL = (
    "While a request you gave openclaw_agent_consult is still in progress (its result has "
    "not come back yet) and the user asks about that request, in any words or language, "
    "call openclaw_agent_control straight away, with no line before it, with their words "
    "as text: mode status when they ask how it is going or how long it will take, cancel "
    "to stop it, steer to change it, followup for something to do after it. Do that "
    "instead of answering in words or calling openclaw_agent_consult again. Everything "
    "else is answered as usual: small talk such as 'how are you?', and any new request, "
    "even one that says cancel or stop, like cancelling a meeting. When no request is in "
    "progress, never call openclaw_agent_control."
)
# Talk with the control switched off (TEAPORT_TOOL_OPENCLAW_AGENT_CONTROL): only the
# relay's spoken run control is left, which answers what it recognizes itself.
_RUN_CONTROL_IN_WORDS = (
    "When the user asks how a request that is in progress is going, or wants it "
    "cancelled or changed, just answer in words; that is taken care of for you."
)
# The room and the phone: no run control at all. Said, so the model neither pretends to
# stop a request nor promises a change that never reaches it.
_NO_RUN_CONTROL = (
    "A request openclaw_agent_consult is already working on can't be stopped or changed "
    "from here: if the user asks, say so plainly, and that its answer will still come."
)
# When to consult, for the room and the phone, whose host text does not say: the tuned
# hint's "multi-step or open-ended", widened with the list OpenClaw's own Talk rules use
# (DEFAULT_REALTIME_INSTRUCTIONS: code, repository state, files, current OpenClaw context,
# tool-backed actions). The hint alone sent a repository question to web_search and left
# an "add it to the list" unanswered: consult recall 40/60 against 50/60 with this (#230
# eval, the room prompt, 10 reps).
_CONSULT_WHEN = (
    "You can also call openclaw_agent_consult: your full desktop agent, with every tool "
    "and deeper thinking. Call it when the user asks for code, repository state, files, "
    "current OpenClaw context, tool-backed actions, or multi-step or open-ended work your "
    "quick tools can't handle."
)
_CONSULT_PREAMBLE = (
    "Before you call openclaw_agent_consult, say one short natural sentence about what "
    "you're doing — in your own words, specific to this request, never a stock phrase "
    "like 'Let me check that for you' — then call it in the same response."
)
_FROM_THE_CONVERSATION = (
    "Answer from the conversation when the answer is already in it, such as what you "
    "just said or what a tool already returned; call openclaw_agent_consult only for "
    "something you do not have yet."
)
_OTHER_SESSIONS = (
    "You cannot see the agent's other sessions from this call, so never describe them: "
    "if the user asks what else you are doing or what is running, call "
    "openclaw_agent_consult to find out."
)


def _host_paragraph(host: str, consult: bool, control: bool = False) -> str:
    """The consult paragraph for a prompt headed by OpenClaw's text (`host`, HOST_RULES or
    HOST_CONTEXT), `consult`: the session has openclaw_agent_consult, `control`: it has
    openclaw_agent_control (Talk only, and only with the consult)."""
    parts = []
    if host == HOST_RULES:
        # The host's rules name both tools; one this session lacks is named as missing.
        missing = (([] if control else ["openclaw_agent_control"])
                   + ([] if consult else ["openclaw_agent_consult"]))
        parts.append(_HOST_RULES_WIN)
        if missing:
            parts.append(f"You have no {' or '.join(missing)} tool here; never call "
                         + ("it." if len(missing) == 1 else "them."))
        if consult and not control:
            parts.append(_RUN_CONTROL_IN_WORDS)
    elif consult:
        parts.append(_CONSULT_WHEN)
    if consult:
        parts += [_CONSULT_PREAMBLE, _CONSULT_IN_PROGRESS]
        if host != HOST_RULES:
            parts.append(_NO_RUN_CONTROL)
        parts += [_FROM_THE_CONVERSATION, _OTHER_SESSIONS]
        if host == HOST_RULES and control:
            parts.append(_RUN_CONTROL_TOOL)
    return "".join(p + " " for p in parts)


def voice_overlay() -> str:
    """The overlay for this box: gateway tools named only where there is a gateway."""
    return VOICE_OVERLAY if HAS_AGENT else VOICE_OVERLAY_LOCAL


# The standard tools each tuned paragraph above is written for. A session whose
# standard tools are exactly one of these sets gets that paragraph verbatim; any other
# set (a tool switched off with its TEAPORT_TOOL_* flag) gets one composed from the
# tools' own hints, so a tool that is off is never named.
# Under OpenClaw's text the consult is taken out of these sets first (tools_paragraph):
# _TUNED_WITH_AGENT without it gets _TOOLS_HOSTED.
_CONSULT = "openclaw_agent_consult"
_CONTROL = "openclaw_agent_control"
_TUNED_WITH_AGENT = frozenset({
    "get_host_status", "get_current_time", "web_search", "web_fetch", "search_memory",
    "remember", _CONSULT, "list_voices", "switch_voice"})
_TUNED_LOCAL = frozenset({"get_host_status", "get_current_time", "list_voices", "switch_voice"})
_SLOW_TOOLS = ("web_search", "web_fetch", _CONSULT)
_LOOKUP_TOOLS = frozenset({"web_search", "web_fetch", "search_memory", _CONSULT})


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _compose_tools(tools, device: bool = False, consult_after: bool = False) -> str:
    """The paragraph for the standard tools `tools`. `device`: client tools get their
    own sentence after it, so "you have no tools" would be untrue. `consult_after`: the
    consult is not among `tools` but has its own paragraph after this one (a prompt
    headed by OpenClaw's text), so the session can look things up after all."""
    names = {t.name for t in tools}
    if not tools:
        return (("" if device or consult_after else "You have no tools in this conversation. ")
                + _NO_TOOL_NAME_INSTRUCTION + " "
                + ("" if consult_after else _CANNOT_LOOK_UP + " ")
                + _GENERAL_CHAT_CLOSER)
    text = ("You have tools — use them instead of guessing: "
            + _join([t.hint or t.name for t in tools]) + ". "
            "When the user asks for something one of them covers, call it directly. ")
    slow = [n for n in _SLOW_TOOLS if n in names]
    if len(slow) < len(names):
        text += "Quick lookups need no preamble — just call silently. "
    if slow:
        text += "A " + " or ".join(n.replace("_", " ") for n in slow) + " " + _PREAMBLE_INSTRUCTION + " "
    text += _NO_TOOL_NAME_INSTRUCTION + " "
    if "openclaw_agent_consult" in names:
        text += _CONSULT_IN_PROGRESS + " "
    if not names & _LOOKUP_TOOLS and not consult_after:
        text += _CANNOT_LOOK_UP + " "
    return text + _GENERAL_CHAT_CLOSER


def tools_paragraph(tools, host: str | None = None) -> str:
    """The tools part of the overlay for a session's active tools (tools.active_tools),
    under a prompt headed by `host` (HOST_RULES, HOST_CONTEXT, or None: the persona).

    Standard tools: the tuned paragraph for that exact set, else a composed one. Client
    tools (performed by the connected device) get their own sentence after it, and so does
    answer_phone_call (only for a question the brain itself puts, so it leaves the tuned
    paragraphs alone). Under OpenClaw's text the consult leaves the tools paragraph and
    gets its own, last (_host_paragraph), and so does the control, which only a Talk
    session has."""
    asides = [t for t in tools if "call_prompt" in t.needs]
    tools = [t for t in tools if t not in asides]
    consult = any(t.name == _CONSULT for t in tools)
    control = any(t.name == _CONTROL for t in tools)
    if host:
        tools = [t for t in tools if t.name not in (_CONSULT, _CONTROL)]
    standard = [t for t in tools if not t.client]
    names = {t.name for t in standard}
    if not host and names == _TUNED_WITH_AGENT:
        text = _TOOLS_WITH_AGENT
    elif host and consult and names == _TUNED_WITH_AGENT - {_CONSULT}:
        text = _TOOLS_HOSTED
    elif names == _TUNED_LOCAL and not (host and consult):
        text = _TOOLS_LOCAL
    else:
        text = _compose_tools(standard, device=any(t.client for t in tools),
                              consult_after=bool(host) and consult)
    device = [t for t in tools if t.client]
    if device:
        text += ("On the device the user is talking to you through you can "
                 + ("also use " if standard else "use ")
                 + _join([t.hint or t.name for t in device])
                 + ": when they ask for one, call it directly, no preamble. ")
    if asides:
        text += ("When a phone call comes in, you may ask the user whether to step away "
                 "for it; give their answer with " + _join([t.hint or t.name for t in asides])
                 + ", straight away and with no line before it. ")
    if host:
        text += _host_paragraph(host, consult, control)
    return text


# Shared source of truth for identity. The OpenClaw text agent should read the same
# file (e.g. symlinked from its workspace AGENTS.md) so both surfaces are one agent.
PERSONA_FILE = setting("TEAPORT_PERSONA_FILE")
# The workspace settings the brain read its own persona with before #230; OpenClaw
# composes that text now (host_context.py), so they are only warned about.
if os.getenv("OPENCLAW_WORKSPACE") is not None:
    logger.warning("OPENCLAW_WORKSPACE is no longer read and is ignored: OpenClaw composes "
                   "the voice context (its IDENTITY, USER and SOUL profile) and the teaport "
                   "plugin serves it")
if os.getenv("TEAPORT_WORKSPACE_FILES") is not None:
    logger.warning("TEAPORT_WORKSPACE_FILES is no longer read and is ignored: the voice "
                   "profile's files are OpenClaw's choice (IDENTITY, USER, SOUL), and "
                   "MEMORY.md is reached through search_memory")


def load_persona() -> str:
    """The persona when the host gave no voice context: TEAPORT_PERSONA_FILE (the source
    with TEAPORT_AGENT=none, and the fallback when the gateway cannot answer), else the
    baked-in FALLBACK_PERSONA, so the voice loop never hard-fails. The OpenClaw
    workspace is never read here: OpenClaw composes its own profile text for a voice
    surface (host_context.py)."""
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return FALLBACK_PERSONA


def build_system_prompt(persona: str | None = None, tools=None,
                        host: str | None = None) -> str:
    """Compose the voice system prompt: identity + voice-only overlay.
    Pass a persona (OpenClaw's text, host_context.py, with `host` saying what it covers:
    HOST_RULES or HOST_CONTEXT) or leave None to load the persona file. A given one starts
    the prompt as given, only trailing whitespace trimmed: the host's text is the host's.
    `tools` is the session's tools.active_tools(); None means this box's default set."""
    if not (persona or "").strip():
        persona, host = load_persona(), None
    if tools is None and host:
        from teaport_brain import tools as _tools  # the default set, as a session has it
        tools = _tools.active_tools(_tools.ToolContext(has_tts=True))
    overlay = voice_overlay() if tools is None else tools_paragraph(tools, host) + _DELIVERY
    return persona.rstrip() + "\n\n" + overlay
