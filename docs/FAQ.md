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
- **Can I run it without NemoClaw?** Yes — voice-only. Install NemoClaw later
  and run the installer again to add the agent.
- **Can it answer phone calls?** Yes, over SIP — but it is **opt-in**. A fresh
  box is a local voice assistant only; nothing binds a SIP port until you run
  `teaport sip configure` and point it at your trunk/SBC. See
  **docs/CONFIG.md → SIP telephony**.
- **Why did my second session hear "the voice assistant is busy"?** The engine
  serves one speech session at a time, and the local (OpenClaw) brain and the SIP
  phone line share it. Whoever connects first holds it; the second connection
  hears the busy message and ends. For a phone-dedicated box,
  `sudo systemctl disable --now teaport-brain` so the line always wins the slot.

TODO: troubleshooting, updates, uninstall, multi-language notes.
