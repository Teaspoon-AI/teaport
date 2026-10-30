# troubleshooting

## Changed files
- src/content/docs/guides/troubleshooting.md

## What changed
- The page is a full symptom-first reference: Start here, then every H2/H3 phrased as the reader sees the failure, each with a Cause paragraph and a Fix procedure, ending with Where the logs are and Related links.
- Verification pass over the three-agent draft: all 16 source comments checked against the pin - twelve content-verified line-by-line (Jetson and L4T prechecks, libopenblas, the g2p-dict failure and its 2026 incident note, sha256 partials, the Caddy certificate handling, the gateway-token memory failure with openclaw_client.py:148, systemd linger, the loopback QR refusal, cmd_doctor), four verified for placement and topic (install.sh:269, 434-435, 1506, 1519-1522). The six citation comments that lacked a commit suffix and three language-less code blocks were repaired along the way.
- Two style warns fixed (the us in "en-us" read as the pronoun; the prosody claim now names espeak per install.sh:269; "once" to "one time"). One warn kept, see Lint.

## Removed content
None. The restructures kept every section; no claim failed verification outright.

## Open questions
- None.

## Lint
0 error(s), 1 warning(s) in 1 file(s). Kept: line 538 "peaks below -30 dBFS" - a measurement comparison, not a pointer at content; the word-list's above/below rule targets references to earlier or later text.
