# Engine license acceptance: the personal data it stores

Internal note (not published on the docs site). It exists so that the Teaport Engine
License and a privacy notice can describe this processing accurately. If the code changes,
change this note in the same PR. The code is `worker/src/index.js` (`accept()`, `token()`,
`factory()`, `download()`, `expire()`), `worker/migrations/`, and `install.sh` (`eula_gate`,
`eula_record`).

## Where

Three tables in the Cloudflare D1 database `teaport-gate`, in Teaspoon AI's Cloudflare
account. The Worker `teaport-download-gate`, in front of get.teaspoon.tech's `/api/*`,
`/dl/engine/*` and `/dl/models/*`, writes them:

- `acceptances`: one row each time `install.sh` calls `POST /api/accept`. That happens once
  per box per license version, after the person has typed `accept` or passed
  `--accept-eula=<version>`, and again if the box loses its receipt.
- `downloads`: one row per download of a gated file (a GET; a HEAD request, or one for a
  file that does not exist, writes none). A resumed download is several rows.
- `maker_acceptances`: one row each time the factory station provisions a box
  (`POST /api/factory`). See "The factory's rows".

## What an acceptance row holds

| Column | Content | Personal data? |
| --- | --- | --- |
| `id` | Random identifier (18 bytes, base64url). The box keeps the same value as `acceptance_id`. | Pseudonymous identifier |
| `created_at` | UTC timestamp from the server's clock | Yes, together with the rest of the row |
| `channel` | `stable` or `beta`: whose manifest named the license | No |
| `eula_version` | The license version accepted, such as `1.0` | No |
| `eula_sha256` | SHA-256 of the exact text accepted. The server only takes the current one. | No |
| `name` | Empty from the Worker deployed with migration 0002 on: it writes `''` whatever the installer sends, and 0002 blanks the names stored before. The column stays so a rollback to an earlier Worker still works; a later release drops it. | No, from then on |
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

`at` (UTC), `acceptance_id` (an acceptance's or a maker acceptance's id, or `ci` for the
publish workflow's check, or `open` for a download without a token while the gate is
open: `GATE = "open"`, the switchover or the way back), `key` (which file),
`range_start` (where a resumed download started), `status` (the HTTP status), `ip` and
`country`. So the record shows which files an acceptance downloaded, when, and from where.

## The factory's rows

The factory installs the engine on each box before it ships, as Teaspoon AI on its own
hardware (teaport#464). The box's owner is to accept the license later, at setup
(teaport#465, not built yet), so the factory records no one's acceptance. Instead, as it
provisions each box, the station records a maker acceptance:

| Column of `maker_acceptances` | Content | Personal data? |
| --- | --- | --- |
| `id` | `maker-` and 18 random bytes (base64url). The box keeps it in its receipt. | See below |
| `created_at` | UTC timestamp from the server's clock | See below |
| `device_id` | SHA-256 of `teaport-appliance:` plus the appliance serial the station assigned the box, as the station computes it (the Worker cannot check how it was made). The serial itself is not sent, but the hash hides it from nobody: serials follow a simple pattern (a date and a number for the day), so anyone with a row or a log line can find the serial by trying them all, in seconds. Treat it as the serial. It is not the `device_id` of an acceptance made with `install.sh` (from `/etc/machine-id`), so the two do not match unless teaport#465 makes the owner's acceptance send this one. | See below |
| `installer` | The installer revision the box will run, such as `git:0123456789ab` | No |
| `ip`, `country` | The station's public IP address, and Cloudflare's country for it | No (the factory's); a row written with a leaked `FACTORY_TOKEN` holds someone else's |
| `user_agent` | The station's user agent, at most 200 characters | No |
| `mark` | Which `FACTORY_TOKEN` recorded it: an HMAC of the secret keyed with `TOKEN_KEY`, not the secret | No |
| `last_token_at` | When the box last got a download token with it | See below |
| `revoked_at` | Empty. Set only by hand, to revoke it. | No |

The box's downloads are `downloads` rows with its `id`. A maker acceptance gets download
tokens for `MAKER_MAX_DAYS` (14, in `worker/wrangler.toml`) after it is recorded, whatever
the license's version: long enough for the box's first-boot install at the factory.

When they are written, these rows describe Teaspoon AI's own unsold hardware on Teaspoon
AI's network: nobody's personal data. Two exceptions. A leak: rows recorded with a leaked
`FACTORY_TOKEN`, and the downloads made with them, hold the IP address and country of
whoever used it. And a box that leaves the factory with its maker receipt (the seal is to
remove it: packaging/RELEASING.md, "Worker secrets"): within `MAKER_MAX_DAYS` of its
provisioning, its owner's engine downloads are recorded under the maker's id, with the
owner's IP address and country. After the box is sold, its `device_id` can be linked to its owner:
through Teaspoon AI's sales records (serial to buyer), and through the owner's acceptance
if that sends the same `device_id` (teaport#465 decides). From then on, treat them as the
box's serial, and so as personal data of its owner. What they are for: which engine files
Teaspoon AI installed on which box, when (support, warranty, and the provenance of what
shipped), and spotting a leaked `FACTORY_TOKEN` (records from outside the factory, or for
boxes it never built: packaging/RELEASING.md, "Worker secrets"). Lawful basis, if GDPR
applies once they are linkable: the legitimate interest in those purposes (Art. 6(1)(f));
there is no contract with the owner behind them.

## What is not stored

No name, from the Worker deployed with migration `0002_drop_acceptance_name.sql`. Earlier
installers asked for one and earlier Workers stored it; counsel advised dropping it, since
an unverified typed name adds nothing to the email, the use and the accepted text's hash.

- That Worker discards any name an installer sends. `install.sh` no longer asks for one or
  sends one; `--name` and `TEAPORT_NAME` are taken and ignored, so older scripts keep
  working. A box that cloned `main` before this change still sends one, which is discarded.
  No release tag's installer has the acceptance flow.
- 0002 blanks the names stored before it. They stay recoverable through D1 Time Travel
  until its restore window has passed (30 days on Workers Paid, 7 on Free).
- A rollback to an earlier Worker stores names again, and D1 records 0002 as applied, so it
  will not run again by itself. After rolling forward, blank them by hand:
  `UPDATE acceptances SET name = '' WHERE name <> ''`. Since this installer sends no name,
  such a rollback also makes every fresh install fail: do not roll back past that Worker.
- A box set up before this change may still hold the name in its own receipt (On the box,
  below); that copy is the owner's and never leaves the box.

Also not stored: the box's hostname or local user, the manifest, the request bodies of downloads, and the
token itself (it is signed, not stored). A new token for an existing acceptance
(`POST /api/token`) writes no row; it only updates `last_token_at`.

## On the box

`install.sh` keeps a copy in `/var/lib/teaport/eula-accepted.json`, readable only by root
(mode 600). It holds the version, the hash, the box's timestamp, the local run user, the
method, the acceptance `id`, the server's URL, the email, the use, and the license ID for
licensed use. Next to it, the installer keeps the accepted text as `EULA-v<version>.txt`.
Neither file leaves the box, apart from the `id`, which the box sends to get a new token.
A box being installed at the factory has, instead, the receipt the factory wrote (the seal
removes it before the box ships): `"method": "factory"` and the maker acceptance's `id`,
nothing else: no license version, name or email, and no license text.

## Who else sees it

- **Cloudflare** hosts the database and the Worker, and acts as Teaspoon AI's processor.
- **Workers Logs** (observability is on in `worker/wrangler.toml`). For each request the
  Worker handles, Cloudflare keeps an invocation log that "contains details such as the
  Request, Response, and related metadata", enriched with "information available to
  Cloudflare in the context of the invocation"; for a request, the log's message is its
  method and URL
  ([Workers Logs](https://developers.cloudflare.com/workers/observability/logs/workers-logs/)).
  That page does not list the fields and says nothing about redacting any, so assume the
  request's headers are among them: the client's IP address, the user agent, and, on a
  download, the `Authorization` header with the download token or `CHECK_TOKEN`, and on
  `POST /api/factory` the station's `FACTORY_TOKEN`. Nor does
  it say whether the request body is recorded. The Worker itself writes nothing to the
  logs (it has no `console.log`); the email (and, from an older `install.sh`, a name the
  Worker discards) travel only in the body of
  `POST /api/accept`. Setting `invocation_logs = false` under `[observability.logs]` turns
  the invocation logs off.

## How long

- **D1 rows:** 7 years after `created_at` (acceptances, maker acceptances) or `at`
  (downloads). Then the
  Worker's daily cron deletes them (`RETENTION_DAYS = "2557"` in `worker/wrangler.toml`).
  An acceptance's downloads go with it, even recent ones, so no download row outlives the
  acceptance it names; a maker acceptance's go with it the same way.
  Seven years outlasts the usual limitation periods for a contract claim. After an
  acceptance is deleted, the box's next token request gets "unknown acceptance", and the
  installer asks for acceptance again.
  **This period is a proposal to confirm with counsel. It is an open item of the engine
  license work.** If the period changes, change `RETENTION_DAYS` and this note together.
  The factory's rows follow the same period today. They prove no contract, so counsel may
  want a shorter one for them (a box's warranty, say); that would need a retention
  setting of their own.
- **Workers Logs:** Cloudflare's retention for the plan: 3 days on Workers Free, 7 days
  on Workers Paid (the same page).
- **The box's copy:** until the box is wiped. It is the owner's own record.

## Why (for the privacy notice)

- **Purpose:** proof that this person accepted this exact license text, when, and for which
  use, and which files were downloaded under that acceptance. The record also lets Teaspoon
  AI enforce the license, including organizational use without a commercial license. The
  engine downloads only after acceptance, except the factory's install of Teaspoon AI's own
  boxes ("The factory's rows").
- **Lawful basis (if GDPR applies):** entering into and performing the license contract
  (Art. 6(1)(b)). The legitimate interest in establishing and defending legal claims
  (Art. 6(1)(f)) also applies.
- **Not used for:** marketing or analytics. The email address is not added to any mailing
  list. Using it for anything other than the license would need its own notice and, for
  marketing, consent.
- **Access and erasure requests:** find the person's rows by `email`, and their downloads
  by those rows' `id`. For a box bought from Teaspoon AI, also compute its factory
  `device_id` from its serial (SHA-256 of `teaport-appliance:` plus the serial), then find
  its `maker_acceptances` rows by that `device_id`, and their downloads by their `id`. Deleting an acceptance before its retention period ends removes the
  proof of acceptance and stops the box's downloads. The license and privacy notice should
  say so.
