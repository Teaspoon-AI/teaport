# Engine license acceptance: the personal data it stores

Internal note (not published on the docs site). It exists so that the Teaport Engine
License and a privacy notice can describe this processing accurately. If the code changes,
change this note in the same PR. The code is `get-worker/src/index.js` (`accept()`),
`get-worker/migrations/`, and `install.sh` (`eula_gate`).

## Where

Each acceptance is one row in the Cloudflare D1 database `teaport-engine-license`, table
`acceptances`, in Teaspoon AI's Cloudflare account. The Worker `teaport-get`, which serves
get.teaspoon.tech, writes the row when `install.sh` calls
`POST /v1/engine-license/accept`. That call happens once per box per license version, after
the person has typed `accept` (or passed `--accept-eula=<version>`).

## What one row holds

| Column | Content | Personal data? |
| --- | --- | --- |
| `id` | Random UUID. The box keeps the same value as `acceptance_id`. | Pseudonymous identifier |
| `license_version` | The license version accepted, such as `1.0` | No |
| `license_sha256` | SHA-256 of the exact text accepted. The server checks it against the published text of that version. | No |
| `accepted_at` | UTC timestamp from the server's clock | Yes, together with the rest of the row |
| `client_ip` | Public IP address of the request (`CF-Connecting-IP`). This is usually the network's address, not the box's. | **Yes** |
| `method` | `tty` (typed at the prompt) or `flag` (`--accept-eula=<version>`) | No |
| `installer_version` | The installer's git revision, such as `git:c570cb19ab12` | No |
| `email` | The email address the person typed at the prompt or passed with `--eula-email`. It is not verified. | **Yes** |
| `revoked_at` | Empty. Set only by hand, to revoke an acceptance. | No |

Nothing else is stored. In particular, the Worker does not store the user agent, the box's
hostname or local user, the manifest, or the files downloaded. It also does not store the
renewals: when a repair run exchanges its token for fresh download URLs
(`POST /v1/engine-license/urls`), nothing is written. A signed download URL carries the
acceptance `id`, so a leaked URL can be traced back to its acceptance. URLs are not logged
by the Worker itself.

## On the box

`install.sh` also keeps a copy in `/var/lib/teaport/eula-accepted.json`, readable only by
the run user (mode 600). The copy holds the version, the hash, the server's timestamp, the
local run user, the method, the email, the acceptance `id`, the token and the server URL.
Next to it, the installer keeps the accepted text as `EULA-v<version>.txt`. Neither file
leaves the box.

## Who else sees it

- **Cloudflare** hosts the database and the Worker, and acts as Teaspoon AI's processor.
- **Workers Logs** (observability is on in `wrangler.toml`) keeps request metadata for
  Cloudflare's log retention period, currently a few days. That metadata includes the
  client IP and the request URL. It does not include the request body, so the email address
  is not logged.

## How long

- **D1 rows:** 7 years after `accepted_at`. Then the Worker's daily cron deletes them
  (`RETENTION_DAYS = 2557` in `wrangler.toml`). Seven years outlasts the usual limitation
  periods for a contract claim. After a row is deleted, its token stops working, and the
  box's next repair run asks for acceptance again. **This period is a proposal for
  Stefano and counsel to confirm.** If the period changes, change `RETENTION_DAYS` and this
  note together.
- **Workers Logs:** Cloudflare's retention for the plan, a few days.
- **The box's copy:** until the box is wiped. It is the owner's own record.

## Why (for the privacy notice)

- **Purpose:** proof that this person accepted this exact license text, and when. The
  record also lets Teaspoon AI enforce the license, including organizational use without a
  commercial license. The engine downloads only after acceptance.
- **Lawful basis (if GDPR applies):** entering into and performing the license contract
  (Art. 6(1)(b)). The legitimate interest in establishing and defending legal claims
  (Art. 6(1)(f)) also applies.
- **Not used for:** marketing or analytics. The email address is not added to any mailing
  list. Using it for anything other than the license would need its own notice and, for
  marketing, consent.
- **Access and erasure requests:** find the person's rows by `email`. Deleting a row before
  its retention period ends removes the proof of acceptance and revokes the box's
  downloads. The license and privacy notice should say so.
