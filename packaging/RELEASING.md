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
         `npx wrangler rollback <version-id>` with the open version noted before the
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
- **`.github/CODEOWNERS`** names who answers for the paths that ship to users or serve
  them. No rule requires the owner's review.

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
