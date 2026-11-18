# Engine license gate: operating it, and the switchover

Internal note (not published on the docs site). The personal data it stores is described in
`engine-license-data.md`.

## What it is

get.teaspoon.tech is the Cloudflare Worker `teaport-get` (`get-worker/`). The Worker is
the domain's origin: it reads the R2 bucket `teaport-get` through a binding. The bucket has
no public access of its own (no custom domain, r2.dev off). The Worker serves everything
public as before:

- the manifests
- `/teaport`
- `/eula/...`
- `/connect/...`
- `/dl/brain/...`
- the engine's `THIRD_PARTY_NOTICES.txt`

`/dl/engine/*` and `/dl/models/*` need one of these:

- **A signed URL.** `?aid=&exp=&sig=`, an HMAC over the path, the expiry and the acceptance
  id. It lives 2 hours, and the check happens when a request starts, so a long download
  that is already under way finishes.
- **The CI token** (`Authorization: Bearer`). `manifest/check.py` uses it, from the repo
  secret `TEAPORT_DL_TOKEN`.

Anything else gets a 403.

`install.sh` gets signed URLs from `POST /v1/engine-license/accept` the first time a box
accepts a license version. After that, it gets them from `POST /v1/engine-license/urls`,
using the token that `accept` returned and that the box keeps in
`/var/lib/teaport/eula-accepted.json`.

## Operating it

Run all of the following from `get-worker/` with Node 22 or later.

```sh
npm test                                         # unit tests (also in CI)
npx wrangler@4 dev --local                       # local, against Miniflare's R2 + D1
npx wrangler@4 deploy                            # deploy
npx wrangler@4 d1 execute teaport-engine-license --remote \
  --command "SELECT id, license_version, accepted_at, method, email FROM acceptances ORDER BY accepted_at DESC LIMIT 20"
# revoke one acceptance (its token stops issuing URLs; URLs already issued live out their 2h)
npx wrangler@4 d1 execute teaport-engine-license --remote \
  --command "UPDATE acceptances SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = '<id>'"
```

- **Secrets:** `SIGNING_KEY` and `CI_TOKEN` (`wrangler secret put`). Rotating `SIGNING_KEY`
  invalidates every token, so every box must accept again on its next repair run. Rotate it
  only when it has leaked.
- **Rate limit:** 10 acceptances a minute per client IP.
- **Retention:** a daily cron deletes rows older than `RETENTION_DAYS`.
- **A new license version:** upload `eula/teaport-engine-license-<v>.txt` before the
  manifest that names it. `/accept` refuses any version whose published text does not hash
  to what the box accepted.

## The switchover (and installers that predate the gate)

### Who runs an older installer

- **The one-liner** (`bash <(curl -fsSL https://get.teaspoon.tech/teaport)`) clones `main`
  on every run. Once this PR merges, every one-liner run, fresh install or repair, uses the
  new installer. No action is needed.
- **A box that runs `./install.sh` from its own checkout** runs whatever that checkout
  holds. `check_brain_source` already refuses a checkout that is behind its upstream, unless
  `TEAPORT_ALLOW_STALE_SOURCE=1`. The refusal tells the owner to `git pull`, and a pulled
  checkout has the new installer. So an old checkout reaches the gated download only if it
  never fetched, or if the owner overrode the freshness check.
- **`teaport update` and the brain's update check** only read the manifest (public). They
  download no engine file, so the gate does not affect them.

### What an older installer does once the gate is enforced

The old installer downloads an engine file or model only when the copy on the box is
missing or has the wrong sha256 (`engine_fetch`, `download`). A repair run on a healthy box
downloads nothing, so it never meets the gate. Only these runs meet it:

- a fresh install
- an engine release (`--only engine`, or a full run after a manifest bump)
- a repair that replaces a damaged file

Those runs fail at the first engine download with `download failed (is get.teaspoon.tech
live?)`. The failure comes before anything is swapped: files are staged, and the live
engine is untouched. The 403 body says the engine downloads through `install.sh` after
the license is accepted. The fix is to update the checkout (`git pull`) or use the
one-liner, then run it again.

### What the new installer does on a box that accepted before the gate

The receipt has the version but no token. The installer says that the earlier acceptance
was never recorded with Teaspoon AI, and asks for acceptance again:

- **On a TTY:** the full text, `accept`, and an email address.
- **Headless:** it stops with the `--accept-eula=<version> --eula-email=<email>` hint.

That re-acceptance is the point: those owners never sent Teaspoon AI an acceptance.

### Order of steps

1. Deploy the Worker with `GATE = "open"`. Move the domain from the bucket's custom domain
   to the Worker's custom domain. Check that the manifest, `/teaport`, `/eula`, `/connect`,
   `/dl/brain` and an engine file all still answer 200, with the same bytes. Downloads keep
   working for every installer.
2. Run a fresh install on a Jetson from this branch's installer. Check that the acceptance
   row appears in D1.
3. Merge the PR. The one-liner now installs with the new installer.
4. Set `GATE = "enforce"` (the default in `wrangler.toml`), then `wrangler deploy`. Unsigned
   engine and model URLs now answer 403. Turn off r2.dev on the bucket if it is on. Set the
   repo secret `TEAPORT_DL_TOKEN`, then re-run the publish workflow's check.
5. Tell the known boxes' owners (there are few before the first release) to `git pull` or
   use the one-liner, and to accept again with an email address.

The deprecated bare `--accept-eula` and `TEAPORT_ACCEPT_EULA=1` keep working for one release,
with a warning. Remove them in the next release that changes `install.sh`'s flags.

**Rollback:** set `GATE = "open"` and deploy. That takes seconds and needs no DNS change.
The bucket's custom domain can be re-attached if the Worker itself has to go.
