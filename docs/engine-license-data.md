# Engine license acceptance: the personal data it stores

Internal note (not published on the docs site). It exists so that the Teaport Engine
License and a privacy notice can describe this processing accurately. If the code changes,
change this note in the same PR. The code is `worker/src/index.js` (`accept()`, `token()`,
`download()`, `expire()`), `worker/migrations/`, and `install.sh` (`eula_gate`,
`eula_record`).

## Where

Two tables in the Cloudflare D1 database `teaport-gate`, in Teaspoon AI's Cloudflare
account. The Worker `teaport-download-gate`, in front of get.teaspoon.tech's `/api/*`,
`/dl/engine/*` and `/dl/models/*`, writes them:

- `acceptances`: one row each time `install.sh` calls `POST /api/accept`. That happens once
  per box per license version, after the person has typed `accept` or passed
  `--accept-eula=<version>`, and again if the box loses its receipt.
- `downloads`: one row per request for a gated file. A resumed download is several rows.

## What an acceptance row holds

| Column | Content | Personal data? |
| --- | --- | --- |
| `id` | Random identifier (18 bytes, base64url). The box keeps the same value as `acceptance_id`. | Pseudonymous identifier |
| `created_at` | UTC timestamp from the server's clock | Yes, together with the rest of the row |
| `channel` | `stable` or `beta`: whose manifest named the license | No |
| `eula_version` | The license version accepted, such as `1.0` | No |
| `eula_sha256` | SHA-256 of the exact text accepted. The server only takes the current one. | No |
| `name` | The name the person typed or passed with `--name`. Not verified. | **Yes** |
| `email` | The email address the person typed or passed with `--email`. Not verified. | **Yes** |
| `use_type` | `personal` or `licensed` | No |
| `license_id` | For licensed use: the Teaspoon AI license's ID | Linked to the licensee |
| `device_id` | SHA-256 of `teaport-device:` plus the box's `/etc/machine-id`. The machine ID itself is not sent. | Pseudonymous device identifier |
| `method` | `tty` (typed at the prompt) or `flag` (`--accept-eula=<version>`) | No |
| `installer` | The installer's git revision, such as `git:0123456789ab`, or `unknown` | No |
| `ip` | Public IP address of the request (`CF-Connecting-IP`). This is usually the network's address, not the box's. | **Yes** |
| `country` | Cloudflare's country for that IP address | Yes, with the rest |
| `user_agent` | The request's user agent, at most 200 characters (curl's) | No |
| `last_token_at` | When the box last got a download token for this acceptance | Yes, with the rest |
| `revoked_at` | Empty. Set only by hand, to revoke an acceptance. | No |

## What a download row holds

`at` (UTC), `acceptance_id` (or `ci` for the publish workflow's check, or `open` for a
download without a token while the gate is open for the switchover), `key` (which file),
`range_start` (where a resumed download started), `status` (the HTTP status), `ip` and
`country`. So the record shows which files an acceptance downloaded, when, and from where.

## What is not stored

The box's hostname or local user, the manifest, the request bodies of downloads, and the
token itself (it is signed, not stored). A new token for an existing acceptance
(`POST /api/token`) writes no row; it only updates `last_token_at`.

## On the box

`install.sh` keeps a copy in `/var/lib/teaport/eula-accepted.json`, readable only by root
(mode 600). It holds the version, the hash, the box's timestamp, the local run user, the
method, the acceptance `id`, the server's URL, the name, the email, the use, and the license
ID for licensed use. Next to it, the installer keeps the accepted text as
`EULA-v<version>.txt`. Neither file leaves the box, apart from the `id`, which the box sends
to get a new token.

## Who else sees it

- **Cloudflare** hosts the database and the Worker, and acts as Teaspoon AI's processor.
- **Workers Logs** (observability is on in `worker/wrangler.toml`) keeps request metadata
  for Cloudflare's log retention period for the plan, a few days. That metadata includes the
  client IP and the request URL. It does not include the request body, so the name and email
  are not logged.

## How long

- **D1 rows:** 7 years after `created_at` (acceptances) or `at` (downloads). Then the
  Worker's daily cron deletes them (`RETENTION_DAYS = "2557"` in `worker/wrangler.toml`).
  Seven years outlasts the usual limitation periods for a contract claim. After an
  acceptance is deleted, the box's next token request gets "unknown acceptance", and the
  installer asks for acceptance again.
  **This period is a proposal to confirm with counsel. It is an open item of the engine
  license work.** If the period changes, change `RETENTION_DAYS` and this note together.
- **Workers Logs:** Cloudflare's retention for the plan, a few days.
- **The box's copy:** until the box is wiped. It is the owner's own record.

## Why (for the privacy notice)

- **Purpose:** proof that this person accepted this exact license text, when, and for which
  use, and which files were downloaded under that acceptance. The record also lets Teaspoon
  AI enforce the license, including organizational use without a commercial license. The
  engine downloads only after acceptance.
- **Lawful basis (if GDPR applies):** entering into and performing the license contract
  (Art. 6(1)(b)). The legitimate interest in establishing and defending legal claims
  (Art. 6(1)(f)) also applies.
- **Not used for:** marketing or analytics. The email address is not added to any mailing
  list. Using it for anything other than the license would need its own notice and, for
  marketing, consent.
- **Access and erasure requests:** find the person's rows by `email`, and their downloads
  by those rows' `id`. Deleting an acceptance before its retention period ends removes the
  proof of acceptance and stops the box's downloads. The license and privacy notice should
  say so.
