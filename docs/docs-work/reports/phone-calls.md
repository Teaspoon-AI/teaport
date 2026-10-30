# phone-calls

## Changed files
- src/content/docs/guides/phone-calls.md (new)

## What changed
- New task page "Set up phone calls": prerequisites (SIP trunk or session border
  controller account; the separate gateway binary, which the installer does not
  build or ship), the `teaport sip configure` wizard (interactive, `--conf`
  adoption, re-run keep semantics, `--fresh`), `status`/`aec`/`restart`/
  `disable`, call behavior during a conversation (ring, ask, hold, resume,
  no-answer default), and the fake-gateway test path with `tools/sip-line-guard`.
- Call-during-conversation behavior is summarized in prose from the
  session-arbiter table (docs/CONFIG.md:599-601) instead of reproducing the
  table; the settings behind it are linked, not copied (per packet: link
  /reference/settings/).
- The testing section mirrors the docs/CONFIG.md:480-498 block as numbered
  steps, keeping the guard rationale (a runtime drop-in beats
  `systemctl mask --runtime` because /etc outranks /run) and the cleanup order.
- Links: /reference/cli/ page-level (the CLI reference is a later wave; its
  `teaport sip` anchors do not exist yet in this repo), 
  /reference/settings/#sip-front-end (heading exists in the finished settings
  page), /reference/architecture/ page-level (the current file has no H2s), and
  /getting-started/installation/ under Before you begin.
- Every source comment's line range was checked against the pin with grep/sed
  while writing; two ranges were widened after verification
  (docs/CONFIG.md:607-612, 618-623).

## Removed content
None; the page is new.

## Open questions
1. How the reader obtains or builds the gateway binary: the teaport-sip
   repository is outside the pin; install.sh:2255-2256 and cli/teaport:28 give
   only the expected path (`~/teaport-sip/build/teaport-sip`), so the page names
   no URL or build steps.
2. docs/CONFIG.md:525 continues past the page's coverage with further
   `fake_sip_gateway.py` invocation variants ("Other calls:"); unverified, left
   out.

## Lint
0 error(s), 1 warning(s) in 1 file(s)

WARN kept, phone-calls.md:217: "we" in *"Sorry about that — where were
we?"* — a verbatim line the agent speaks, quoted from docs/CONFIG.md:599;
changing it would misquote the product.
