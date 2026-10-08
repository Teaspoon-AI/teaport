# FAQ

> **Scaffold.** These are seed questions. More come with the public docs pass.

- **Does my voice leave the device?** No. Speech recognition and speech
  synthesis run on the device. Only your chosen LLM is remote. Point the brain
  at your own endpoint and the LLM is local too.
- **Do I need a GPU cloud key?** No. You supply an LLM: a cloud key
  (OpenRouter / Groq / …) or a local OpenAI-compatible server.
- **Which Jetson?** Orin (Nano / NX / AGX), JetPack 7.2 / CUDA 13. The Nano
  8 GB is the reference device.
- **How is this repository licensed?** The brain and the plugin in this
  repository are MIT. The engine is a separate component with its own license
  terms — see the license shown at install time. The installer downloads the
  engine for you.
- **Can I run it without NemoClaw?** Yes — voice-only. The installer writes
  `TEAPORT_AGENT=none`: the brain keeps its local tools (time, host status,
  voices) and reads its persona from `~/.config/teaport/persona.md`; web search,
  memory and the desktop-agent consult need a gateway and are simply not
  offered. Install NemoClaw or OpenClaw later and run the installer again to
  add the agent.
- **Can it answer phone calls?** Yes, over SIP — but it is **opt-in**. A fresh
  box is a local voice assistant only; nothing binds a SIP port until you run
  `teaport sip configure` and point it at your trunk/SBC. See
  **docs/CONFIG.md → SIP telephony**.
- **Why did my second session hear "I'm in another conversation right now"?** The
  box holds one conversation at a time: the app, the dashboard, Discord, the
  box's own microphone and a phone call all share one speech engine. A new
  session while another is live is told the agent is busy and closes; the
  conversation in progress goes on. Only the same client reconnecting replaces
  its own session, and a phone call ends a remote Talk session after telling its
  user why. See **docs/CONFIG.md → One engine, one conversation**. For a
  phone-dedicated box, turn off the box's microphone bridge
  (`sudo systemctl disable --now teaport-local-audio`): a call already outranks
  a remote Talk session.

TODO: troubleshooting, updates, uninstall, multi-language notes.
