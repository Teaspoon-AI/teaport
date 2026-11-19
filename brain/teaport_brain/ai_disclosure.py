#
# teaport — telling people they are talking to an AI
#
# Teaspoon AI is the provider of Teaport under the EU AI Act, and Article 50(1) puts the
# duty on the provider to DESIGN the system so people are told they are dealing with an
# AI, unless that is obvious from the context. So the rule here is by channel: is it
# obvious to the person what they are talking to?
#
#   PHONE (SIP)  Not obvious: a voice on a phone line could be anyone. Every answered
#                call starts with LINE, in the call's language, heard in full (the caller
#                cannot talk over it), outside the model's context, before the greeting
#                -- a resumed call too (the brain that was cut off may not have finished
#                saying it). A call where it cannot be played to its end is hung up
#                (AgentSession.greet: should_end, end_reason "disclosure") and what it
#                recorded deleted. A call answered only to be turned away hears it too,
#                ahead of its line (before(): the busy line, the no-model line, the
#                "I can't hear you" line). The caller is unheard from the moment the
#                call's pipeline is built until the disclosure has played (InputMute,
#                muted at build for SIP), so no reply can start ahead of it.
#                There is deliberately NO setting that turns this off: a setting would
#                mean the shipped product does not ensure people are told. Removing it
#                takes a code change, and whoever ships that modified brain is the
#                provider of their version (and breaches the engine license, 7.5).
#                Any future channel where someone reaches Teaport remotely and
#                voice-only (a web-call widget, ...) must start the same way: call
#                call_notice() and pass it to greet() as its notice.
#   ROOM         Obvious: someone talking to a device on a shelf. Off by default;
#                TEAPORT_ROOM_AI_GREETING turns on ROOM_LINE at the start of a new room
#                conversation, for public-facing set-ups (a front desk) where a visitor
#                may not realize it. A setting may turn a disclosure ON, never one OFF.
#   TALK         Obvious: an app that presents itself as an AI assistant. Nothing spoken.
#
# On EVERY path, whatever the persona says, the model is told it is an AI and to say so
# when asked (PROMPT_LINE, agent_session._build_initial_messages): it never claims or
# implies it is a person.
#
from teaport_brain import call_recording, i18n
from teaport_brain.settings import setting

# The optional room greeting's switch (ROOM above). There is no phone switch, by design.
ROOM_GREETING = setting("TEAPORT_ROOM_AI_GREETING")


def room_greeting(front_end: str, enabled: bool | None = None) -> bool:
    """Whether a session on `front_end` starts its conversations with ROOM_LINE: the room
    only (the box's own mic), and only with TEAPORT_ROOM_AI_GREETING on."""
    return front_end == "room" and (ROOM_GREETING if enabled is None else enabled)

# Spoken on every phone call, before anything else. Translated in locale/*/teaport.po.
LINE = i18n.N_("You're speaking with an AI assistant.")
# The optional room greeting (TEAPORT_ROOM_AI_GREETING).
ROOM_LINE = i18n.N_("I'm an AI assistant.")

# A system message on every session, after the identity: a persona (OpenClaw's profile
# or the persona file) may give the assistant a human name, never a human nature.
PROMPT_LINE = (
    "You are an AI assistant, not a person. Never claim or imply that you are human. "
    "If anyone asks whether they are talking to a person, a human or a machine, say "
    "plainly that you are an AI assistant.")


def line(espeak_language: str | None) -> str:
    """The AI disclosure alone, in the language the voice speaks: ahead of any line a
    phone call hears without a greeting (the busy line, the no-model line, the "I can't
    hear you" line), so no answered call goes untold."""
    return i18n.for_espeak(espeak_language)._(LINE)


def before(espeak_language: str | None, text: str) -> str:
    """`text` (already in that language) with the AI disclosure ahead of it."""
    return line(espeak_language) + " " + text


def call_notice(espeak_language: str | None, recorded: bool = False) -> str:
    """What a phone call starts with, in the language the call's voice speaks: the AI
    disclosure, and on a call recorded with its notice on, the recording notice after it
    -- one line, so the caller hears both before the greeting."""
    line = i18n.for_espeak(espeak_language)._(LINE)
    if recorded:
        line += " " + call_recording.notice_line(espeak_language)
    return line


def room_line(espeak_language: str | None) -> str:
    """The optional room greeting, in the session's voice's language."""
    return i18n.for_espeak(espeak_language)._(ROOM_LINE)
