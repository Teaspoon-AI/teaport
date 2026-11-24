# Releasing the brain package

The brain ships as the `teaport-brain` .deb (#60). CI builds it; a person releases it. Nothing
in CI uploads it: the upload to the download bucket and the manifest entry are the two steps
that make a release, and both are deliberate.

What a release is, end to end:

    tag brain-vX.Y.Z  ->  CI builds the .deb  ->  you test it  ->  you upload it
                      ->  manifest PR (url + sha256)  ->  merge publishes the manifest

`install.sh` reads `manifest/stable.json` from get.teaspoon.tech, so a package a box can
install by itself exists only once its manifest entry is merged *and* published. From then
on `teaport update` offers it to every box on the channel whose installed package is older
(it reads the version from the `.deb`'s file name, so keep the name as below), and installs
it with the installer at `brain.repo`/`brain.tag`.

## Before the tag

- [ ] `main` is green, and it is the commit you mean to ship (the tag must be on it).
- [ ] `BASE_VERSION` in `packaging/build-deb.sh` is X.Y.Z. `build-deb.sh` refuses a release
      whose version is not `BASE_VERSION`, because the dev builds of this tree are numbered
      below it (`X.Y.Z~dev<run>+g<sha>`); for any release after the first, bump it in a PR
      first.
- [ ] The license material matches the lock. If `brain/uv.lock` changed since the last
      release (`git diff brain-v<previous>..HEAD -- brain/uv.lock`), read the changes for a
      new package under the LGPL, GPL, AGPL or a license with its own terms, and update
      `THIRD_PARTY_LICENSES.md` and `packaging/deb/copyright`. `teaport licenses` on a box
      with the new package shows every package's license and marks the LGPL ones; "not
      stated" needs a look.
- [ ] The LGPL notice (soxr, libsndfile, num2words) is still true, and the source versions it
      names are the ones in this lock. The written source offer (stefano@teaspoon.tech) runs
      three years from the release that shipped those versions, so each release has its own
      three years: requests for an older release are still answered.

## Cut it

```sh
git tag brain-vX.Y.Z <commit>      # on main, nothing else
git push origin brain-vX.Y.Z
```

CI's `brain-deb` job builds from the tag, on the arm runner, and runs the self-check,
`dpkg -V` and shared-library checks against the installed package. Its artifact is
`teaport-brain-deb` (the `.deb` and its `.sha256`):

```sh
gh run download <run id> -n teaport-brain-deb -D /tmp/brain-release
cd /tmp/brain-release && sha256sum -c teaport-brain_X.Y.Z_arm64.deb.sha256
dpkg-deb -f teaport-brain_X.Y.Z_arm64.deb Package Version Architecture
```

- [ ] The version is exactly `X.Y.Z`, with no `~dev` or `.dirty`, the architecture is `arm64`,
      and the checksum matches.

## Test it on a box that is not the one you live on

- [ ] Copy the .deb into the checkout on the spare box (the bare name is read from where
      you run `install.sh`), then
      `TEAPORT_BRAIN_DEB=teaport-brain_X.Y.Z_arm64.deb ./install.sh --only brain`:
      a first install on a box that still has the old venvs, and an upgrade over the
      previous release. For the first release there is no previous release: install a CI
      dev build the same way (the brain-deb artifact of any run) and upgrade from that. The
      run must end with the brain up.
- [ ] `teaport doctor` has no failures, `dpkg -V teaport-brain` is silent.
- [ ] The Settings page opens; its footer shows the revision and build date; `/licenses`
      opens, and so does `teaport licenses --notices`.
- [ ] A call or a room turn works end to end.
- [ ] `./install.sh --rollback brain` after an upgrade puts the previous package (the dev
      build, for the first release) back, and a second run undoes it.

The first release is the oldest a box can go back to: a box that had the pre-package venvs has
no earlier package to return to, and `install.sh` removes those venvs once the package's brain
is running.

## Upload it

Uploading is outward-facing and cannot be taken back: a version, once served, is never
overwritten (the manifest pins its sha256, and apt treats a second X.Y.Z as the same package).

```sh
aws --profile teaport s3 cp teaport-brain_X.Y.Z_arm64.deb \
  s3://teaport-get/dl/brain/teaport-brain_X.Y.Z_arm64.deb \
  --content-type application/vnd.debian.binary-package
```

- [ ] Do not `curl` the new URL before the upload has finished: the edge caches a 404 for a
      few minutes. Check it with `?x=$RANDOM` appended first, then the plain URL.

## Right after the tag

- [ ] Bump `BASE_VERSION` in `packaging/build-deb.sh` to the next release, in a PR. Until you
      do, main's dev builds are `X.Y.Z~dev…`, which sort *below* the release you just cut: a
      box on the release that is given one has to downgrade to take it.

## The manifest

In a PR, set in `manifest/stable.json` (the beta channel keeps its `TODO` until a beta):

```json
"brain": {
  "repo": "Teaspoon-AI/teaport", "tag": "brain-vX.Y.Z",
  "deb": {
    "url": "https://get.teaspoon.tech/dl/brain/teaport-brain_X.Y.Z_arm64.deb",
    "sha256": "<the .sha256 file's hash>"
  }
}
```

- [ ] `brain.tag` is the release tag, not `main`: `teaport update` runs that tag's
      `install.sh` as root, and refuses a branch (it moves) and any installer without
      `UPDATE_PROTOCOL=1`. The Discord bridge is cloned from the same tag.
- [ ] With `access` in the manifest (the engine license gate), `brain.tag` is a tag cut
      at or after the gate's merge commit. `teaport update` runs that tag's
      `install.sh --only engine` as root, and an installer from before the gate ignores
      `access`: every gated download gets a 401 once the gate enforces, so every engine or
      model update fails and rolls back. `brain-v0.1.0`, `brain-v0.1.1-rc1` and
      `brain-v0.1.1-rc2` all predate it. `manifest/check.py` refuses such a tag: it reads
      the tag's `install.sh`, which must say `ACCESS_PROTOCOL=1` (the gate's installer
      does). It reads the installer rather than comparing commits because a PR may be
      squash-merged, so the gate's merge commit is not known in advance.
- [ ] The PR's `publish` check downloads the package and compares its sha256
      (`manifest/check.py`); it fails when the upload is missing or differs. That is why the
      upload comes first.
- [ ] For a gated file (the engine's files and models), a pull request's `publish` run has
      no `DL_CHECK_TOKEN`. It checks only that the file refuses an anonymous download and
      is not served at an encoded path, not its sha256; the merge's run compares it, but
      only after the merge. So before merging a release PR, run the full check locally,
      with the token read from a prompt, which keeps it out of your shell history:

      ```sh
      read -rsp 'DL_CHECK_TOKEN: ' DL_CHECK_TOKEN && echo && export DL_CHECK_TOKEN
      python3 manifest/check.py manifest/<channel>.json   # beta: add --placeholders-ok
      unset DL_CHECK_TOKEN
      ```

      It downloads every artifact (~4.6 GB), and the gate logs those downloads as `ci`.
- [ ] `brain.deb` is in `REQUIRED` in `manifest/check.py` (since 0.1.0), so a stable
      manifest without one fails the check. The beta channel's `TODO` passes only under
      `--placeholders-ok`.
- [ ] A channel never names an older brain or engine than a box on it may have:
      `teaport update` refuses such a release outright (`update_apply.not_older`), so a
      box that took a beta and is moved to stable gets nothing until stable catches up.
      Engine releases are ordered as `dpkg --compare-versions` orders them: a dev or
      test engine is named with `~` (`1.7~dev1` sorts before `1.7`), never `-` or `+`
      (`1.7-dev` sorts after `1.7`, and would hold a box back from the release).
- [ ] A model change ships with a new `engine.release`. Model sha256s have no order, so
      the engine release is the only thing that tells a newer set of models from an older
      one; a channel that changes `models` under the same engine can take a box's models
      back.
- [ ] The engine's files and models (every URL under the manifest's `access.gated`:
      `dl/engine/`, `dl/models/`) go to the private bucket `teaport-engine-dist`, never
      `teaport-get`. The download gate Worker (`worker/`) serves them only after the
      engine license is accepted; a copy in `teaport-get` would be public. Its
      `THIRD_PARTY_NOTICES.txt` goes to `teaport-get` under `dl/notices/`, public on
      purpose.
- [ ] No gated file is left in `teaport-get`. The Worker is routed on
      `get.teaspoon.tech/dl/engine/*` and `/dl/models/*`, and a percent-encoded path
      (`dl%2Fengine%2F…`, `dl/engine%2F…`) matches neither route, so the public bucket
      answers it: a copy left there is served to anyone, around the gate. The `publish`
      check asks for every gated file that way on every run, and fails on a 2xx.
- [ ] The gate's `GATE` in `worker/wrangler.toml` is `"enforce"`. `"open"` serves a gated
      file to a request without a token (marked `x-teaport-gate: open`): it served the
      switchover, and is the way back below. While it is open, every `publish` run warns,
      and the weekly run fails (`check.py --require-enforce`). The switchover, in order:
      1. Done: the private bucket `teaport-engine-dist` holds the gated files, the D1
         database and the Worker's secrets (`TOKEN_KEY`, `CHECK_TOKEN`, and the repo
         secret `DL_CHECK_TOKEN`) are set, the notices are in `teaport-get` under
         `dl/notices/`, and the Worker was deployed with `GATE = "open"`. An anonymous
         download was served (`curl -sI` on a gated URL: 200, `x-teaport-gate: open`), so
         the installer on main from before the gate still downloaded, through the
         Worker.
      2. Done: `dl/engine/*` and `dl/models/*` are deleted from `teaport-get`, so
         `curl -sI 'https://get.teaspoon.tech/dl%2Fengine%2Fteaport-engine-1.6-pos.bin'`
         answers 404.
      3. Done: the installer that accepts through the Worker is merged (#439), and the
         Worker is redeployed from main's HEAD with `GATE = "open"`. Accept → token → a
         ranged download was checked against it.
      4. Done (#460, Worker version 30ffbbee): `GATE = "enforce"` in
         `worker/wrangler.toml`, deployed from main's HEAD after noting the live, open
         version id (`cd worker && npx wrangler deployments status`). These checks passed,
         and they are the checks after any deploy of the Worker, in this order:
         - Without a token: `curl -sI` on a gated URL answers 401, with no
           `x-teaport-gate` header. This is the check that the gate enforces: the
           two below pass with the gate open too (the publish check only warns).
         - A customer's token: `POST /api/token` with a test acceptance's id, then
           `curl -sI -r 0-0` on a gated file with that token: 206. This takes the path
           every install takes: the D1 lookup of the acceptance and the current license
           read from the manifest. It records no new acceptance, and a HEAD records no
           download (a GET would, against that acceptance: use a test one, not a box's).
         - `CHECK_TOKEN`: run a `publish` check (Actions → publish → Run workflow), which
           fetches every gated file with `DL_CHECK_TOKEN` and compares its sha256. On
           main it also uploads the manifests and the bootstrap again, unchanged.
         If enforcing breaks installs, the way back is a Worker version with the gate open:
         `wrangler rollback <version-id>`, with the locked tool (`(cd .github/wrangler &&
         npm ci)`, then from `worker/` `../.github/wrangler/node_modules/.bin/wrangler
         rollback <version-id>`), to the open version noted before the
         deploy (a bare `rollback` takes the deployment before the current one, which a
         later `wrangler secret put` changes; wrangler asks before rolling back over
         changed secrets), or, with the id lost, a PR to main that sets `GATE = "open"`
         (the workflow deploys it).
         The weekly check fails while it is open. Then fix main and deploy main again.
      5. Done: deleted `dl/engine/teaport-engine-1.6-THIRD_PARTY_NOTICES.txt` from
         `teaport-engine-dist`. It is a switchover-only copy: main's manifest before #439's
         merge named the notices under `dl/engine/`, which the open Worker serves from
         there. The notices' place is `teaport-get` `dl/notices/`; once the gate enforces,
         nothing should read the copy.
- [ ] A change to `worker/`, to the deploy tool (`.github/wrangler/`) or to the workflow
      deploys through the `worker-deploy` workflow once it is on main:
      Actions → worker-deploy, approve the `worker-deploy` environment. The job runs the
      Worker's tests, prints the live version id first (the rollback target), applies D1
      migrations, deploys, and checks that an anonymous gated download answers as
      `GATE` says. After it, run step 4's token and `CHECK_TOKEN` checks. To go back, use
      step 4's way back (`wrangler rollback <version-id>`, the id the job printed, with
      the locked tool: `(cd .github/wrangler && npm ci)`, then from `worker/`
      `../.github/wrangler/node_modules/.bin/wrangler rollback <version-id>`),
      not a deploy by hand. To deploy main again (the first time, or after fixing the
      environment), use Run workflow from main, not Re-run on an older run: a re-run
      deploys that run's commit. A rollback does not undo D1 migrations, which the job applies
      before it deploys: a migration must leave the schema usable by the version before
      it (add, never rename or drop).
- [ ] Merge. The `publish` workflow uploads the manifests and reads each back from
      get.teaspoon.tech.

## GitHub settings

What the release path relies on outside the repo's files (Settings on GitHub). teaport
pushes from one account, `teaspoonai`, so no rule here may need a second person: a PR's
author cannot approve it, and an environment with "Prevent self-review" ticked never
deploys a run its only reviewer started. The same account is the one the coding agents
on the development machine use, so an environment's reviewer is a step a person sees,
not a barrier an agent cannot cross. What protects a box is elsewhere: each file's
sha256 in the manifest (and, with manifest signing, the signature the box verifies), and
the review of what merges to main, which is also what decides what the download gate
lets through.

- **`main`**: ruleset "main: PRs and green CI". No deletion, no force push, a change
  only through a PR with the required checks green: `brain (ubuntu-latest)`,
  `brain (ubuntu-24.04-arm)`, `brain-deb`, `plugin`, `formal`, `installer`, `worker`
  and `docs`. No approvals. The admin role bypasses it only by merging a PR, never by
  pushing.
- **`worker-deploy` environment** (`.github/workflows/worker-deploy.yml`). GitHub creates
  it, unprotected, the first time the workflow names it; set its protections before its
  secret, or the next change to `worker/` deploys unapproved:
  - required reviewer `teaspoonai`, "Prevent self-review" unticked;
  - "Allow administrators to bypass configured protection rules" unticked;
  - deployment branches: `main` only;
  - secret `CLOUDFLARE_API_TOKEN`, an environment secret so no other workflow reads
    it: an API token for this account only, with Workers Scripts: Edit, Workers
    Routes: Edit and Zone: Read (zone `teaspoon.tech`: wrangler looks the zone up by
    name for the routes, after it uploads the script) and D1: Edit, and no User Details or
    Memberships permission (wrangler prints what those can read when a call fails, and
    the job logs are public; the workflow masks email addresses in wrangler's output
    too). The account's name should not be an email address either: Cloudflare's
    default is "<email>'s Account";
  - variable `CLOUDFLARE_ACCOUNT_ID`.
- **`r2-publish` environment** (`.github/workflows/publish.yml`'s upload job): deployment
  branches `main` only, no reviewer; secrets `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and
  `R2_ENDPOINT`, an R2 token with write access to `teaport-get`, the bucket that serves
  the manifests and the bootstrap a box runs as root. Without the branch policy, a
  workflow on any branch could read them.
- **Repository secret `DL_CHECK_TOKEN`**: equal to the download gate Worker's
  `CHECK_TOKEN`; the publish check downloads the gated files with it on every run but a
  pull request's. Change both together (`wrangler secret put CHECK_TOKEN` with the locked
  wrangler, under "Worker secrets", then the secret), or the next check fails.
- **`.github/CODEOWNERS`** names who answers for the paths that ship to users or serve
  them. No rule requires the owner's review.

## Worker secrets

The download gate Worker's secrets are set on the Worker, from `worker/`, with the locked
wrangler: `../.github/wrangler/node_modules/.bin/wrangler secret put <NAME>`, after `(cd
.github/wrangler && npm ci)` from the repository's root (`wrangler` below means that one).
They are never in `worker/wrangler.toml`, and the `worker-deploy` workflow leaves them
alone. A `secret put` takes effect at once, as a new Worker version, without CI: whoever
runs it is the review.

- **`TOKEN_KEY`** signs every download token. Changing it stops every token issued (a box
  gets a new one on its next run, from its receipt). It also keys the mark of
  `FACTORY_TOKEN` in `maker_acceptances`, so the daily cap counts from zero again.
- **`CHECK_TOKEN`** is the publish check's (the repository secret `DL_CHECK_TOKEN`, under
  GitHub settings).
- **`FACTORY_TOKEN`** is the factory station's (teaport#464), set by the product owner:
  `openssl rand -hex 32` (the Worker records nothing for one shorter than 32 characters,
  or one equal to `CHECK_TOKEN` or `TOKEN_KEY`), put on the Worker, on the station and in
  the password manager, nowhere else (apart from Workers Logs, below). It is never a
  GitHub secret or variable: no workflow needs it, and CI never sees it (the Worker's
  tests use a made-up one). It is never on a box either.
  - **What it does:** when a box provisions itself at its first boot in the factory (it
    asks the station's `/provision` for its serial), the station sends `POST
    /api/factory` with `FACTORY_TOKEN`, the box's `device_id` and the installer revision
    the box will run. The Worker records a maker acceptance for that box
    (`maker_acceptances`: Teaspoon AI installing its own box, no one's acceptance of the
    license) and returns its id, which reaches the box in the `/provision` answer. The
    box writes it as its license receipt (below). Its first-boot `install.sh` gets
    download tokens with it, whatever the license's version, for `MAKER_MAX_DAYS` (14, in
    `worker/wrangler.toml`) after provisioning. The seal removes the receipt, so the box
    ships without one and its owner accepts the license (teaport#465) before its first
    engine download or update.
  - **What a leak exposes:** whoever holds it can record maker acceptances for made-up
    boxes, and each gets every gated file (all of `dl/engine/` and `dl/models/`, the same
    files a customer's token reaches) for `MAKER_MAX_DAYS`, with no license accepted.
    `CHECK_TOKEN` reaches the same files. `FACTORY_TOKEN` itself downloads nothing and
    gets no token.
    The station's `/provision` hands out the same records without the secret leaking, to
    whoever can ask it: so it answers only to the boxes' USB network (below). If the
    factory's public IP address is stable, a Cloudflare rule letting `/api/factory` in
    only from it would make a leaked secret close to useless elsewhere (not set up).
  - **What slows a leak down, and what shows it:** at most `FACTORY_DAILY_MAX` (200) boxes
    are recorded in any 24 hours (a box recorded again within the day is not counted
    again, so the cap limits made-up boxes; records for one box are limited to 10 in 24
    hours), and
    20 requests a minute per IP address. That limit is shared: the station's calls and its
    boxes' token requests come from one address through its NAT, so a batch of boxes can
    meet a 429, which their retries ride out. Each record holds its time, box, IP address
    and country, so records from outside the factory's network, or for boxes it never
    built, stand out: `wrangler d1 execute teaport-gate --remote --command "SELECT
    created_at, device_id, ip, country FROM maker_acceptances ORDER BY created_at DESC
    LIMIT 50"`. Neither brake stops one holder: one record downloads everything.
  - **Rotating it:** `wrangler secret put FACTORY_TOKEN` with a new value, then the
    station's copy, and, a minute later (an edge can answer for the old version that
    long), restart `factory serve`, which reads it at startup (stripping the trailing
    newline `openssl rand -hex 32 > file` leaves). That stops new records; boxes already
    provisioned keep theirs. The daily cap counts from zero for the new secret. Rotate it
    when anyone who held it leaves, and on any doubt. To stop records a leak made, revoke
    them: `UPDATE maker_acceptances SET revoked_at = '<now>' WHERE …` (by IP, by time, or
    all since the leak; a revoked box's next run gets no token, and its owner accepts). A
    Worker version carries the secrets it was deployed with, so a rollback (`wrangler
    rollback`, the way back above) to a version from before a rotation brings the old
    `FACTORY_TOKEN` back (wrangler asks first): rotate again after it, by redeploying a
    fixed main and then `secret put` (a plain `secret put` is refused while the deployed
    version is not the latest), or with `wrangler versions upload` from the rolled-back
    commit, `wrangler versions secret put FACTORY_TOKEN`, and `wrangler versions deploy
    <the id that last command printed>@100%`. A rollback to a Worker from before the
    factory kind refuses every maker acceptance (and its retention cron leaves them alone)
    until main is deployed again.
  - **Turning it off:** `wrangler secret delete FACTORY_TOKEN`. The factory then records
    nothing until a new one is set and on the station; rotate instead to keep it going.
    Never fall back to an ordinary acceptance in Teaspoon AI's name: it records the
    maker as the box's licensee.
  - It travels in the `Authorization` header of `POST /api/factory`, so Workers Logs may
    keep it for their retention (docs/engine-license-data.md, "Who else sees it"), as
    they may `CHECK_TOKEN`.
  - **What teaport-factory does (not built yet):**
    - **The station** (`handle_provision`), for a box it provisions: `device_id` is
      SHA-256 of `teaport-appliance:` plus the appliance serial it assigns (as in the
      box's `/etc/appliance-id`, without its trailing newline), in lowercase hex: the
      station computes it, and the serial is what it knows (the box has its
      `/etc/machine-id` by then, which its owner's acceptance will hash; whether that
      acceptance sends this id instead is teaport#465's to decide). The Worker cannot
      check how it was made. It calls `POST https://get.teaspoon.tech/api/factory`
      (`Authorization: Bearer <FACTORY_TOKEN>`, `{"device_id", "installer": "git:<pin>"}`,
      the pin the box's own agent reports in its `/provision` request (its baked
      `TEAPORT_PIN`, which the agent is to start sending; the station checks it is hex),
      not the station's own, as an `image_id` does not tell bakes apart; with a User-Agent
      of its own (it fills the record's `user_agent`, and Cloudflare's browser check may
      refuse urllib's default); the URL from its own config, not from a manifest, which
      would let whatever publishes one decide where the secret goes) and puts the returned
      `acceptance_id` at the top level of its `/provision` answer, beside
      `appliance_serial`, not inside `config` with `ProvisionConfig`'s keys
      (`ProvisionConfig.from_payload` refuses a key outside its set, so an agent baked
      before the change would never provision; and teaport-factory's docs/06, which
      describes the answer's shape, is to name the new key, not docs/09 §4, which is
      `config`'s set). So the station needs the internet at provisioning, as the box's
      install does just after. Its call has an overall deadline well under the box's
      10-second request (say 5 s, the call in a thread joined with a timeout; a call that
      succeeds after it leaves a record no box gets, one of that box's 10), as urllib's
      `timeout` bounds each socket step, not the call, and not DNS. When it fails (no
      network, 429 from the per-IP limit or a cap, 401 or 503 for a wrong secret or
      setting), the station answers the box non-200, so the box's own retry loop asks
      again later.
    - **Who may get one:** `/provision` hands out what downloads every gated file, so the
      station takes it only from the boxes' USB interfaces: an nft input rule by
      interface, in a filter input chain in `host-firewall.nft` (`table inet`, so IPv6 is
      covered too; renaming the interfaces away from `usbN` also means updating what names
      them, such as the NetworkManager profile for 192.168.55.100), on `serve`'s
      configured port (`iifname "lo" accept`, then `tcp dport <port> iifname != "tpbox*"
      drop`, with the boxes' interfaces named under that prefix by a udev rule on NVIDIA's
      USB ids, such as `ATTRS{idVendor}=="0955", ATTRS{idProduct}=="7020",
      NAME="tpbox$attr{ifindex}"` (unique while the station runs: `%n` is the kernel
      name's number, `usb0` and `usb1` for every box, so a second box's rename would
      collide and the rule would drop it), so a USB uplink never matches; not a `.link`
      file, whose `Name=` names one interface only, while several boxes are attached at
      once), and `factory serve` refuses to start unless that rule covers its port
      (reading the ruleset needs `sudo nft list ruleset`). Binding to its USB address does
      not do it: Linux takes traffic for any of its addresses on any interface, so a host
      on the factory Wi-Fi can reach 192.168.55.100. Nor does `SO_BINDTODEVICE`: each box
      comes up as an interface of its own, made again whenever the box re-enumerates.
      Today it listens on every interface. (The station keeps no record of what it flashed
      or sealed by module, so "only modules it flashed" cannot be checked yet; if it comes
      to, the station should add it.)
    - **Where the code goes:** the Worker call and the loading of `FACTORY_TOKEN` live in
      the station's code (`station_api.py`), never in `device_provision.py` or anything it
      imports: that module is baked verbatim into every box's image (`agent_fileset`).
    - **Sending the secret safely:** the URL must be `https://` (the station refuses
      anything else at startup), redirects are not followed (an opener whose redirect
      handler refuses), and the header is set with `add_unredirected_header`: urllib
      follows a redirect of a POST and copies its headers, `Authorization` included, to
      whatever host it names.
    - **When the Worker refuses:** after an answer whose `error` is `bad_factory_token` or
      `factory_misconfigured` (a wrong secret or setting) the station stops calling until
      it restarts (so after fixing the Worker's settings, restart it too), and after
      `factory_cap` it waits an hour, answering boxes non-200 meanwhile; any other failure
      (a 5xx from Cloudflare itself, no network) is retried at the box's next ask: each
      box keeps asking every few minutes, and every call shares the per-IP limit with the
      boxes' own token requests.
    - **Retries and provisioning again:** the station records a new maker acceptance on
      every `/provision` it answers, and never hands a stored one back: it cannot tell
      whether a stored one still stands (revoked, or near its end), nor a box retrying
      after a lost answer (it gives up after 10 seconds and asks again) from one an
      operator provisions again. The Worker takes that: the daily cap counts boxes, and
      a box gets up to 10 records in 24 hours. It calls the Worker last: after the unit's
      row is committed (not inside the serial allocation) and after everything else in
      the answer is built and checked, just before the 200, so a station failure after
      the call (a bad config, say) cannot spend a box's 10 records on answers it never
      gets. `factory serve` should check its `StationConfig` once at startup. A 429
      `factory_box_cap` means that box was recorded 10 times in 24 hours: look at why, as
      waiting a day is the only way past it short of a rotation; `factory_cap` is the
      daily cap.
    - **Where it keeps `FACTORY_TOKEN`:** a file of mode 0600, the operator's or root's,
      that `factory serve` reads at startup; never the environment (the station copies the
      operator's environment into the bake and flash commands that write box images),
      `StationConfig`'s config (sent to every box), its event log or its logs. Nor does it
      store or log the maker ids it passes on (each downloads every gated file while it
      stands): a prefix at most.
    - **The box's agent** writes the receipt atomically (a temporary file, fsync, rename,
      fsync of the directory) before it writes `/etc/appliance-id` (which marks the box
      provisioned for good, so a crash between the two must not leave a box without one):
      `/var/lib/teaport/eula-accepted.json`, owned by root, mode 0600, `{"method":
      "factory", "acceptance_id": "maker-…"}` and nothing else, in `/var/lib/teaport`
      (made root:root 0755 if it is not there yet) (no license version, which would read
      as an acceptance of it; no name or email). An answer without an `acceptance_id`, or
      with one not matching `^maker-[A-Za-z0-9_-]{24}$` (with `re.fullmatch`: Python's `$`
      lets a trailing newline through) (a station from before the change, say), is a
      failed attempt: the agent writes nothing, not `/etc/appliance-id` either, and asks
      again. The receipt is never in the shared, baked image.
    - **The first-boot unit** runs an installer revision with this support (the
      `TEAPORT_PIN` bump), no longer sets `TEAPORT_ACCEPT_EULA` (with it set, `install.sh`
      accepts the license instead of using the factory record, and stops on a version
      that names no license), and runs only once the box is provisioned
      (`ConditionPathExists=/etc/appliance-id`), so it never starts without a receipt. Its
      boot start already waits for the provisioning unit, and its timer tries again 15
      minutes after a skipped start. (If the agent is to start it sooner, it must be
      `systemctl start --no-block`: a plain start from inside the provisioning unit, which
      the first-boot unit is ordered after, waits on itself for ever.)
    - **The seal** removes `/var/lib/teaport/eula-accepted.json` and checks it is gone, as
      it checks `setup-done`; no receipt at all passes. It refuses a box whose receipt is
      anything but a factory one
      (`"method": "factory"`): another means someone accepted the license on it at the
      factory, in Teaspoon AI's name (to recover such a box: revoke that acceptance and
      remove the receipt; no receipt passes). It also refuses
      a box whose install has not finished. A box that ships with the receipt would
      download engine updates for its owner, within `MAKER_MAX_DAYS`, with no license
      accepted, recorded under the maker's id.
    - **Provisioning a box again** (its install outlasted `MAKER_MAX_DAYS`, or its record
      was revoked): remove its `/etc/appliance-id` (the provisioning unit runs only
      without it), and it records a new maker acceptance at its next boot.
  - **After the factory:** the owner's acceptance (teaport#465) writes a receipt
    `install.sh` takes as its own (`version`, `acceptance_id`), or every engine update
    asks again. The maker record itself stays in `maker_acceptances`, with its age past
    `MAKER_MAX_DAYS` soon after; nothing needs to revoke it.

## Release candidates

A candidate is the next release, tested on spare boxes and the beta channel before
it gets its final number. Tag `brain-vX.Y.Z-rcN` (N from 1, no leading zero) on main,
where X.Y.Z is `BASE_VERSION`. CI builds version **`X.Y.Z~rcN`**:
`teaport-brain_X.Y.Z~rcN_arm64.deb`.

```sh
git tag brain-vX.Y.Z-rcN <commit>   # on main
git push origin brain-vX.Y.Z-rcN
TEAPORT_DEB_PRINT_VERSION=1 TEAPORT_DEB_VERSION=X.Y.Z-rcN packaging/build-deb.sh  # at the tag: prints X.Y.Z~rcN
```

- Debian's `~` sorts before anything, so the order is
  dev builds (`X.Y.Z~devM+g…`) < `X.Y.Z~rc1` < `X.Y.Z~rc2` < `X.Y.Z`. Each candidate,
  and then the release, installs over the one before it as an upgrade, and
  `teaport update` and `update_apply.not_older` order them the same way.
- `BASE_VERSION` stays X.Y.Z through the candidates. Bump it only after the final
  `brain-vX.Y.Z` tag.
- Test a candidate the same way as a release (above). A candidate goes on the
  **beta** channel only: `manifest/beta.json` gets its `brain` entry, with `tag` set to
  the candidate's tag. Stable never names a candidate.
- Upload it under its own name, as above. Like a release, a candidate's file is never
  overwritten: a fix is the next candidate.

## After

- [ ] From a box, with no `TEAPORT_BRAIN_DEB`: `./install.sh --only brain --dry-run` prints
      the download of the package (its URL carries the version), then the real run installs
      it.
- [ ] Note the release (version, sha256, date) where you keep release notes.
