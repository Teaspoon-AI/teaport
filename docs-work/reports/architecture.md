# architecture

## Changed files
- src/content/docs/reference/architecture.md
- docs-work/reports/architecture.md (this report)

## What changed
- Replaced the 21-line pre-launch scaffold entirely (scaffold aside and outline removed; STYLE.md 1.4 / packet full write). No facts from the old page were dropped: engine `8000`, brain `7861` `/talk` with barge-in and heard-grounding, the plugin's role, and the voice-stays claim are all restated with pinned citations.
- Added the three-part layout and the audio/text flow: plain text to the engine, audio plus per-word timestamps back, per-session pipelines in one brain process.
- Documented all four front ends (`/talk` WebSocket, phone line, Discord bridge, room-microphone bridge) from the systemd unit comments, `brain/teaport_brain/sip_server.py:3-25`, and `session_arbiter.py`/`tools.py` top comments.
- New One conversation at a time section: the arbiter's grant cases, the ask-hold-or-decline path for calls, refused-call behavior (rings vs busy line), and crash/replay recovery.
- New What the voice is told section: the Talk `session_config` path, the gateway voice-context path for room mic/phone, the `TEAPORT_AGENT=none` fallback, and the persona overlay.
- Tools section: the one-record contract, the full tool-name list (`brain/teaport_brain/tools.py:93-340`), agent-mode versus voice-only gating by need, and client-side tools.
- New Network ports table (8000, 7861, 80) from the systemd units and the clients' constants, plus a What leaves the device section.

## Removed content
- The `:::note[Pre-launch scaffold]` aside and its four-item outline, plus the uncited three-bullet summary. Removed because the packet is a full rewrite and STYLE.md 1.4 bans scaffold phrasing; every former fact is restated with a pinned citation (docs/ARCHITECTURE.md:8,11-17,18-23,25 @1e5604c).

## Open questions
- The SIP gateway's listening port: no port number appears in `systemd/teaport-sip.service.in:3-7`; the gateway binds only what the trunk configuration names, and that config lives in the separate teaport-sip repo. The page therefore omits a SIP row from the table.
- The Wi-Fi setup page's own port: `systemd/teaport-wifi-setup.service.in:37-38` references `PAGE_PORT` (defined in `brain/teaport_brain/wifi_setup.py`, outside this packet's cite set) without a value; only the port-80 redirect is documented.
- The owner of TCP 80 outside the Wi-Fi setup window: `systemd/teaport-wifi-setup.service.in:36` calls it "the front door's (Caddy)" in passing, but no teaport unit or allowed source defines that listener.

## Lint
0 error(s), 1 warning(s) in 1 file(s)
- WARN kept, architecture.md:86 "write to you, not we": the "we" is inside the quoted greeting the brain speaks ("sorry, I lost you for a moment, where were we?", `brain/teaport_brain/sip_server.py:51-52` @1e5604c). It is a quotation of product output, not the documentation's voice.
