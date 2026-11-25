# Releasing the brain package

The brain ships as the `teaport-brain` .deb (#60). CI builds it; a person releases it. Nothing
in CI uploads it: the upload to the download bucket and the manifest entry are the two steps
that make a release, and both are deliberate.

What a release is, end to end:

    tag brain-vX.Y.Z  ->  CI builds the .deb  ->  you test it  ->  you upload it
                      ->  manifest PR (url + sha256)  ->  sign it offline, commit the .sig
                      ->  merge publishes the manifest and its signature

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

The tag push starts two workflows. `release-deb.yml`, a workflow of the tag alone, builds the
release `.deb` from the tag on the arm runner (`release-deb-build`), hashes it in a job with no
checkout (`brain-deb-sha256`), and only then installs that same artifact (by the build's id
and digest, the sha256 checked) and runs the self-check, `dpkg -V` and the shared-library
checks on it (`release-deb-check`, the checks in `.github/actions/brain-deb-check`). Its
artifact, `teaport-brain-release-deb` (the `.deb` and its `.sha256`), is the one that is
published and signed. `ci.yml` builds and checks the tag as it does every commit (its
artifact is `teaport-brain-deb`, never the release one). Both runs must be green.

```sh
gh run download <the release-deb.yml run id> -n teaport-brain-release-deb -D /tmp/brain-release
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
  "commit": "<git rev-parse brain-vX.Y.Z^{commit}, all 40 characters>",
  "deb": {
    "url": "https://get.teaspoon.tech/dl/brain/teaport-brain_X.Y.Z_arm64.deb",
    "sha256": "<the .sha256 file's hash>"
  }
}
```

- [ ] `brain.tag` is the release tag, not `main`: `teaport update` runs that tag's
      `install.sh` as root, and refuses a branch (it moves) and any installer without
      `UPDATE_PROTOCOL=1`. The Discord bridge is cloned from the same tag.
- [ ] `brain.commit` is the commit that tag points at, in full. A tag can be moved by
      anyone who can push; the commit is what the signature covers. `teaport update`
      refuses a tag that is not at `brain.commit` on GitHub, or a clone that is not, and
      the `publish` check fails on a missing `brain.commit` or a tag that is elsewhere.
- [ ] Every artifact has its sha256, the Voxtral model's `aux` files too: their hashes go
      in `models.voxtral.aux_sha256`, by the same file names as `aux` (a separate map, so
      installers that read `aux` as `{file name: url}` still parse the manifest).
      `install.sh` refuses an `aux` file without one, and so does the `publish` check.
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
         secret `DL_CHECK_TOKEN`, which moves to the `gate-check` environment with signing: see
         "Turning signing on") are set, the notices are in `teaport-get` under
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
           main, once the manifests are signed, it also uploads them and the bootstrap
           again, unchanged; between signing's merge and the turn-on's last step (below)
           it stops at the unsigned `stable.json` and uploads nothing.
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
- [ ] Signed, as the last commit of the PR: [Sign the manifest](#sign-the-manifest). The
      `publish` check fails on a manifest whose signature is missing or does not verify,
      and boxes install nothing from it.
- [ ] Merge. The `publish` workflow checks the signatures again, uploads each manifest's
      `.sig` and then the manifest, and reads each back from get.teaspoon.tech. Between
      the two uploads (seconds) a box reads a manifest and a signature that do not match
      and refuses it; the next look gets the pair. Every run is measured against the last
      commit a `publish` run uploaded and read back (`check.py --base`, from GitHub's own
      record of the runs: `manifest/last_upload.py`), what the bucket really serves. To run
      it again by hand (Actions > publish > Run workflow), leave `base` empty for that
      commit, or name a commit id.
- [ ] Wait for the merge's `publish` run to finish green before merging the next manifest or
      bootstrap change, above all before a rotation PR: GitHub drops a queued run when a
      newer one queues in the same group, and a dropped run never ran. If one was dropped,
      dispatch `publish` with `base` = the last uploaded commit
      (`python3 manifest/last_upload.py commit`, below).
- [ ] A red `publish` run that failed before its upload step (missing bucket credentials
      included: their check is a step of its own) uploaded nothing, so the next
      run is still measured against the last upload: it is not waved through by whatever
      lands after. Answer it with a revert or a fix (a PR, checked against `main`, and its
      merge's run against the last upload). One whose upload step ran but did not read back
      (it failed, or was cancelled) may have changed the bucket: the next run's lookup stops
      and asks for an explicit base. Find out what the bucket serves, then dispatch
      `publish` with `base` = the commit whose files it serves.

The last uploaded commit, for a check on a development machine (`check.py --base`; never on
the signing machine, which runs no code from the repository). No token is needed (the
repository is public; anonymous reads are limited to 60 requests an hour, and
`GITHUB_TOKEN` raises that). It stops while a `publish` run on `main` is still running (it
may be uploading): run it again when that finishes. If the lookup fails, `$base` is empty and
`check.py` refuses an empty `--base`:

```sh
base="$(python3 manifest/last_upload.py commit)" || { echo "lookup failed" >&2; false; }
python3 manifest/check.py --signature-only --base "$base" --boxes-signers manifest/stable.json
```

The signing skill takes the diff base from the signer's own record of the last release they
signed instead ([After](#after)).

## Sign the manifest

Boxes trust a release manifest only for its signature: `install.sh`, `teaport update` (as
root) and the brain's daily check read nothing out of `manifest/<channel>.json` until
`manifest/<channel>.json.sig` verifies against a key in `manifest/allowed_signers` (the
package ships it as `/usr/lib/teaport/allowed_signers`), and a `stable.json` must say
`"channel": "stable"` (a `beta.json`, `"beta"`). Whoever can write to the download bucket
can replace a manifest, but cannot make a box install from it.

The signature is an OpenSSH signature, `ssh-keygen -Y sign`, in the namespace
`teaport-manifest`, made with the **main** release key on a dedicated signing laptop. CI
never signs and never sees a key; it only checks (`manifest/check.py`).

No agent runs on the signing laptop, ever. An agent may help on the development machine
(preparing the manifest PR, reviewing it with the signing helper's read-only copy there,
checking the `.sig` that comes back), and a human signs on the laptop. The laptop has one
login account, the signer's: it owns the keys (on a drive mounted for it, ext4 or LUKS),
the checkout signed from, and the signing helper's record and archive. The signing helper
(`sign_manifest.py`, installed root-owned from a reviewed commit, its sha256 compared with
the development machine's copy) refuses to sign as any other account, or while anything it
trusts can be written by another account than the signer and root. The laptop reads from
the network and writes nothing to it, and runs no inbound service: `git fetch` of the PR,
`git ls-remote` and a fetch of `main` from this repository (the helper holds its record of
what it signed to GitHub's tags and `main`, from a repository of its own, never through the
checkout's git config), an anonymous read of GitHub's API (the release `.deb`'s sha256 from
`release-deb.yml`'s run), an HTTPS download of the `.deb` the manifest names, and an HTTPS
read of the published manifest and bootstrap (the diff base before there is a record;
compared with the record after). The key itself is opened only by `ssh-keygen`, from its drive.

The only commands the signer types there (typed, never pasted), every commit a full
40-character id; nothing else, and never a `;`, `&&`, `|`, `$(`, a backtick, a redirection
or a `NAME=value` in front:

```sh
git -C <checkout> fetch -q
git -C <checkout> checkout -q -f --detach <id>
lsblk -o NAME,SIZE,MODEL,SERIAL,LABEL
sudo mkfs.ext4 -F -L teaport-xfer -E root_owner=<uid>:<gid> <the transfer stick's partition>
udisksctl unlock -b <device>
udisksctl lock -b <device>
udisksctl mount -b <device>
udisksctl unmount -b <device>
python3 -I /usr/local/lib/teaport-signing/sign_manifest.py sign <path> [--commit <id>] [--base-commit <id>] [--against <file>] [--both-keys-lost]
python3 -I /usr/local/lib/teaport-signing/sign_manifest.py sign-bootstrap <path> [--commit <id>] [--base-commit <id>] [--both-keys-lost]
python3 -I /usr/local/lib/teaport-signing/sign_manifest.py keygen main|backup <path on the key drive>
python3 -I /usr/local/lib/teaport-signing/sign_manifest.py export-sig <the archive name sign printed> <the transfer stick's mount>
python3 -I /usr/local/lib/teaport-signing/sign_manifest.py trust-diff <old id> <new id>
```

`-q` keeps git from printing commit subjects (which could carry escape sequences); `-f`
lets a checkout overwrite a `.sig` left from the last signing, which the helper has already
archived. The helper prints the command it received before anything else and refuses an
option it does not take. It shows its view a screenful at a time (long lines in marked
pieces), including the whole diff of the files that decide trust or run as root, ends it
with the key facts (the release, the counts, the keys' fingerprints, every flag) and holds
the last screen before its prompts. The signer compares the fingerprints there with the ones
`keygen` printed (written down on paper then), never with anything another machine shows.

A `.sig` leaves the laptop one way: the signer locks and removes the key drive, inserts a
transfer stick, has the helper write that one `.sig` onto it (`export-sig`, below), and
carries it to the development machine. There, before it is committed, the `.sig` is put beside its file and
checked:

```sh
python3 manifest/check.py --signature-only manifest/stable.json
base="$(python3 manifest/last_upload.py commit)" || { echo "lookup failed" >&2; false; }
python3 manifest/check.py --signature-only --base "$base" --boxes-signers manifest/stable.json
python3 manifest/check.py --base "$base" --boxes-signers --bootstrap install-bootstrap.sh   # a bootstrap's .sig
```

Those checks (and CI's, and every box's) are what catch a swapped or wrong `.sig`: the stick
is not trusted for having come from the laptop. The helper writes the `.sig` to the stick
itself (`sign_manifest.py export-sig <the archive name sign printed> <the stick's mount>`:
the `.sig` only, never onto a key drive, never over a file), with the key drive locked and
removed first.

What the laptop holds each hash to:

- `brain.deb`, first by its content: the helper downloads the `.deb` from its url, checks its
  sha256 against the manifest, and reads all of it with a strict ar/tar reader, as dpkg
  would unpack it. (That reader accepts exactly the dialect nfpm writes today: gzip members
  are read as one gzip stream that ends with the member, xz as one xz stream with no stream
  padding after it, and GNU tar headers. A change of compression, nfpm's tar format or a
  tool that writes the package needs a change to the reader first, in `manifest/check.py`
  and in the helper's identical copy.) Every maintainer script must be byte-identical to the script
  `packaging/nfpm.yaml` names at `brain.commit`, and every file outside the venv
  (`/opt/teaport/brain/`, which uv builds from `brain/uv.lock` and which runs as the brain's
  user) must be one `nfpm.yaml` lists, byte-identical to its `src` at that commit, with its
  mode; any other file there (under `/usr/lib/teaport`, `/usr/bin`, `/etc`, systemd's,
  sudoers' or polkit's directories, or anywhere else) is a STOP, and so is a directory
  outside the venv that is not above such a file. Every entry is root's by number and by
  name (dpkg takes the tar's owner name first), with no group- or other-write and no setuid,
  setgid or sticky bit, every directory 0755, each path once and none under a symbolic link
  the package carries; a hard link, a device, a fifo, a sparse file or a pax header is a
  STOP. In the venv: regular files, directories, and symbolic links that lead inside it,
  but `bin/python` -> `/usr/bin/python3.12`. The control file holds only `Package`
  (`teaport-brain`), `Version` and `Architecture` (the file name's, and the tag's version),
  `Maintainer`, `Description`, `Section`, `Priority`, `Installed-Size`, `Homepage` and
  `Depends`, exactly `nfpm.yaml`'s list at `brain.commit` (no `Pre-Depends`, `Conflicts`,
  `Breaks`, `Replaces`, `Provides`, `Recommends`, `Essential` or `Protected`); `conffiles`
  only `nfpm.yaml`'s config entries (none today); no other control member. That is what the
  signer's reading of the trust diff then covers.
- `brain.deb`, as a cross-check: the sha256 CI printed for the `brain-vX.Y.Z` tag push, from
  `release-deb.yml` (that workflow file's runs only), a workflow of the tag alone:
  `release-deb-build` builds and uploads (`contents: read`, a checkout without credentials,
  the build backend pinned by hash in `brain/build-requirements.txt`), and
  `brain-deb-sha256`, with no checkout and no repository code, downloads that artifact by
  the id `release-deb-build` reported, checks its digest and the `.deb`'s sha256 against the build's,
  and prints the sha256 as a check-run annotation (`::notice title=teaport-brain-deb-sha256::`,
  read anonymously through GitHub's API; exactly one, from a list the helper reads whole:
  a full page of runs, jobs or annotations is a STOP). It is not the root of trust: whether
  an Actions token can add an annotation to a finished job is not established, and the
  content check above does not depend on it. Nor is the build itself: `release-deb-build` runs code
  from every locked wheel (a `.pth` file runs when `--compile-bytecode` starts Python in the
  venv, and the build backend runs on `brain/` and docopt), with the uv and nfpm it fetched
  by hash and an empty uv cache. That is accepted, because the laptop's own reading of the
  `.deb` is the gate: every file and script outside the venv must be the commit's, read the
  way dpkg reads it (one tar dialect, the decompression its member name says, and dpkg-deb's
  own streams and listing compared, so no package shows one tar to the check and another to
  dpkg).
- `brain.commit` must be on `main` of this repository.
- The engine and the models are built outside CI, by sessions the agent may run, so the only
  independent record of their sha256s is one the human makes: written down at the engine's
  build (on paper, or on a device the agent cannot reach), or produced by a build outside the
  agent's reach. Whenever one changes, the helper's view says so and asks for exactly that
  record; the signer compares each sha256 with it before signing.
- The transfer stick is formatted on the laptop before each use, with its root owned by the
  signer (`sudo mkfs.ext4 -F -L teaport-xfer -E root_owner=<uid>:<gid> <device>`, the
  signer's ids as `id` prints them, or `export-sig` cannot write to it), and mounted
  read-only on the development machine (`udisksctl mount -b <device> -o ro`).

Every command block from here to the end of this file is for reading, or for a development
machine. The ones that sign or verify by hand (`ssh-keygen`, `curl`, a pipe or a
redirection) are the operation the helper does on the laptop, written out; on the laptop
they break its command rules, and none of the helper's checks would run. There, only the
commands listed above are typed.

By hand, the signing is this, from a checkout at the PR's last manifest commit (or with the
exact file handed over; a signature covers every byte, whitespace too). On the signing
laptop, run no code from the checkout: not `manifest/check.py`, `manifest/last_upload.py` or `install.sh`, no
tests. A PR's code is unreviewed until it is signed, and the key is on that machine; only
git, ssh-keygen and the signing helper (installed root-owned, never from the checkout) run
there. Every `check.py` command in this file runs on a networked
development machine (and in CI):

```sh
git diff --no-ext-diff --no-textconv --text --no-renames \
  <the published commit> -- manifest/stable.json           # read what you are signing
rm -f manifest/stable.json.sig                             # -Y sign will not overwrite
ssh-keygen -Y sign -f <path to the main key> -n teaport-manifest manifest/stable.json
ssh-keygen -Y verify -f manifest/allowed_signers -I release-main -n teaport-manifest \
  -s manifest/stable.json.sig < manifest/stable.json       # "Good ... signature"
```

- [ ] Read the diff before signing: the brain package (`brain.deb` url, sha256, and the
      version in its file name), `brain.tag` and `brain.commit` (`git rev-parse
      brain-vX.Y.Z^{commit}` in your own clone must print the same), `engine.release` and
      every engine and model url/sha256, the `aux` files' included, `openclaw`. The
      signature says "install this, as root, on every box".
- [ ] The commit. Signing the manifest approves the whole commit it names: everything
      `git diff <published brain.commit> <new brain.commit>` shows, root code included, not
      only two files. Take `<published brain.commit>` from your own record of the last
      release you signed ([After](#after)), not from the bucket, which whoever can write to
      it can point at another commit; or verify the published manifest with your own copy of
      the keys first (the laptop's helper does both: its record, compared with the published
      manifest; by hand, on a development machine):

      ```sh
      curl -fsS -o published.json https://get.teaspoon.tech/manifest/stable.json
      curl -fsS -o published.json.sig https://get.teaspoon.tech/manifest/stable.json.sig
      ssh-keygen -Y verify -f <your copy of the signers file> -I release-main -n teaport-manifest \
        -s published.json.sig < published.json    # then read its brain.commit
      ```

      What boxes will trust comes from that commit's package
      (`packaging/nfpm.yaml` decides what lands in `/usr/lib/teaport/allowed_signers`) and
      from the code that reads it or runs as root (every file `packaging/nfpm.yaml` installs
      outside the docs is in the list below; a test keeps it so), so read every change to
      these, line by line. Read git as text, whatever the checkout's attributes say (a `.gitattributes`
      with `-diff` turns a diff into "Binary files differ"), and every file of the stat:

      ```sh
      git diff --no-ext-diff --no-textconv --text --no-renames --ignore-submodules=none --stat=200 \
        <published brain.commit> <new brain.commit> | cat
      git diff --no-ext-diff --no-textconv --text --no-renames --ignore-submodules=none \
        <published brain.commit> <new brain.commit> -- manifest/allowed_signers \
        install-bootstrap.sh install.sh .github/ systemd/ packaging/ cli/ \
        brain/teaport_brain/manifest_sig.py brain/teaport_brain/config_apply.py \
        brain/teaport_brain/wifi_apply.py brain/teaport_brain/login_apply.py docs/src/content/docs/ \
        plugin/ bridge/ brain/build-requirements.txt \
        ':(glob)**/.gitattributes' ':(glob)**/package.json' ':(glob)**/package-lock.json' \
        ':(glob)**/npm-shrinkwrap.json' ':(glob)**/.npmrc' ':(glob)**/uv.lock' \
        ':(glob)**/pyproject.toml' ':(glob)**/requirements.txt' ':(glob)**/setup.py' \
        ':(glob)**/setup.cfg' ':(glob)**/MANIFEST.in' ':(glob)**/.gitmodules'
      ```

      This list is the signing helper's own (`TRUST_FILES`, then `TRUST_BASENAMES` as the
      globs), item for item: its trust diff shows these files, and its test holds the two
      lists equal.

      `manifest/allowed_signers` and `install-bootstrap.sh`'s key block must not change unless
      this release is a deliberate rotation (below). Anyone who can push could have changed
      any of these; this reading, by you, is the control that stops it. The `publish`
      check's full run reads the file `brain.deb`'s data carries at
      `/usr/lib/teaport/allowed_signers`, and requires it there exactly once, as a regular
      file under real directories, with no maintainer script mentioning it, read strictly
      and the same as `manifest/allowed_signers` at `brain.commit`, which must be this
      tree's (every release carries exactly the keys of the tree that releases it). The one
      exception is a re-signature, as a rotation needs: a manifest byte for byte the one at
      `--base` (the PR's base; on `main`, the last uploaded commit), naming
      the release it names. With
      `--boxes-signers` it passes a change against the boxes' keys (those at the
      `brain.commit` that `--base`'s manifest names, else `--base`'s bootstrap's) only when
      the manifest is signed by a key they already trust, which yours is: it names a change,
      it does not judge one. It takes all of this from git at `--base`, never from the
      bucket: CI publishes only what is on `main`. What the package's code does with that
      file once installed is the diff above, read by you.
- [ ] The same for `manifest/beta.json` when it names anything installable. A beta that is
      all `TODO` may stay unsigned (`check.py --placeholders-ok` allows it; boxes refuse it
      anyway). No sha256 or url placeholder is signed: in one that names anything
      installable, every `sha256`, `*_sha256` and `url` is filled in first (boxes would pass
      a `TODO` sha256 through unchecked). Other `TODO` fields, such as
      `nemoclaw.tested_tag`, may stay.
- [ ] Commit the `.sig` beside the manifest, and change nothing in the manifest after it.
      Until that commit, the PR's CI is red by design: its installer dry runs read the
      checkout's `stable.json` with the `.sig` beside it, which is the previous release's
      and does not verify the edited file.
      On a development machine (never the signing machine),
      `python3 manifest/check.py --signature-only manifest/stable.json` checks it without
      downloading anything.

## Sign the bootstrap

The one-line installer, `install-bootstrap.sh` (served as `get.teaspoon.tech/teaport`), is
signed too, in its own namespace, `teaport-bootstrap`, so a manifest's signature is never a
bootstrap's. It carries the release keys (its `trusted_signers` block is
`manifest/allowed_signers`, whole) and verifies the manifest with them before it clones the
commit the manifest names. Its own signature is for whoever checks the script before running
it (the installation guide's verify-first path). Sign it whenever it changes, the key block
included; `publish` refuses to upload it unsigned and uploads `teaport.sig` before `teaport`.
On the laptop that is the helper's `sign-bootstrap`; by hand, it is this:

```sh
git diff --no-ext-diff --no-textconv --text --no-renames \
  <the last signed commit> -- install-bootstrap.sh         # read what you are signing
rm -f install-bootstrap.sh.sig
ssh-keygen -Y sign -f <path to the main key> -n teaport-bootstrap install-bootstrap.sh
ssh-keygen -Y verify -f manifest/allowed_signers -I release-main -n teaport-bootstrap \
  -s install-bootstrap.sh.sig < install-bootstrap.sh       # "Good ... signature"
```

Then, on a development machine (never the signing machine), with the `.sig` beside the
script, before it is committed:

```sh
python3 manifest/check.py --boxes-signers --bootstrap install-bootstrap.sh  # its key block is the file's; it verifies, under --base's bootstrap keys too
```

- [ ] Read the whole diff: this script runs on every new box. Check that the keys in its
      `trusted_signers` block are the ones in `manifest/allowed_signers`, and that the
      default path still verifies before it clones (only `TEAPORT_REF`, the developer's
      path, skips that, and it says so).
- [ ] The keys it carries are unchanged since the bootstrap published now, unless this is a
      deliberate rotation: the same `git diff --no-ext-diff --no-textconv --text
      --no-renames <published brain.commit> <this commit> -- manifest/allowed_signers
      install-bootstrap.sh` as for the manifest prints no key line.
      `check.py --boxes-signers --bootstrap` passes a key change only when the script is
      signed by a key `--base`'s bootstrap already carries.
- [ ] Commit `install-bootstrap.sh.sig` beside it.

## The release keys

Two Ed25519 keys, each with a passphrase, both on the signing laptop's side
only: the **main** key signs every release; the **backup**, kept somewhere else, signs only
when the main key is lost or leaked. Never `ssh-add` either key: an agent holding it signs
with no passphrase, for whatever can reach the agent's socket. The public halves go in
`manifest/allowed_signers`, one line each:

```sh
ssh-keygen -t ed25519 -a 128 -C release-main   -f <dir>/teaport-release-main     # asks for a passphrase
ssh-keygen -t ed25519 -a 128 -C release-backup -f <dir>/teaport-release-backup
# the signers file's lines (public halves only; never copy the private files anywhere):
printf 'release-main namespaces="teaport-manifest,teaport-bootstrap" %s\n'   "$(cut -d' ' -f1,2 <dir>/teaport-release-main.pub)"
printf 'release-backup namespaces="teaport-manifest,teaport-bootstrap" %s\n' "$(cut -d' ' -f1,2 <dir>/teaport-release-backup.pub)"
```

The same lines go, whole file, into `install-bootstrap.sh`'s `trusted_signers` block, and the
two key lines into the installation guide's verify-first path
(`docs/src/content/docs/getting-started/installation.md`); `check.py --bootstrap` and
`brain/tests/test_install_bootstrap.py` fail while any of the three differ. Each line names
both namespaces (`teaport-manifest,teaport-bootstrap`): a key is trusted for nothing else.

`packaging/build-deb.sh` refuses a release build while `manifest/allowed_signers` or
`install-bootstrap.sh` still says `PLACEHOLDER`, or the signers file names fewer than two keys.

**Rotation.** Boxes trust the keys in the package they have. So a key is only ever signed
with once boxes trust it, and a lost or leaked main key is replaced through the backup:

1. The backup becomes the main key: sign with it from now on (every box already trusts it).
2. Generate a new backup key, kept elsewhere again.
3. In one PR: `manifest/allowed_signers` drops the old main key's line, names the old
   backup `release-main` and adds the new backup as `release-backup`, and the bootstrap and
   the installation guide get the same lines. In the same PR, with the old backup:
   - re-sign the manifests published now (`manifest/stable.json`, and `beta.json` if it is
     signed): their signatures by the dropped key would fail this PR's own `publish` check,
     which reads the PR's signers file, and every weekly one after it;
   - re-sign `install-bootstrap.sh`: its key block changed, and `publish` holds the new
     bootstrap's signature to the keys the published one carries, which the old backup is
     one of.

   Update the fingerprints page (`<KEY-FINGERPRINTS-PAGE>`) to the new two keys when the PR
   merges. Then release a brain package with it, and sign that release's manifest with the
   old backup (now the main).
   Boxes take the package and stop trusting the old key. Until a box has updated, it still
   trusts a leaked key: release the rotation promptly.

The rotation release's manifest must verify under **both** signers files: the new one in the
PR, and the one boxes on the channel have, which is the `allowed_signers` at the
`brain.commit` that the last uploaded commit's `manifest/stable.json` names (the release they installed, not
the newest `brain-vX.Y.Z` tag: the new release's own tag is pushed before its manifest PR).
That is why it is signed with the old backup, which both name. The `publish` check enforces
it (`manifest/check.py --boxes-signers`, measured against `--base`: the PR's base, and on
`main` the last uploaded commit): a
manifest signed only by a key added since fails it, because every box would refuse it, the
update that brings the new keys included. Locally, on the PR's branch, against what was last
uploaded: the two commands under [Merge](#the-manifest) above (`last_upload.py commit`, then
`check.py --base "$base"`; an empty `--base` is refused). Without `--base`, it is the
branch's merge-base with `origin/main`.

With both keys lost, no box can verify a manifest signed by new keys, and a checkout with the
new keys does not help: on a box with the brain package, `install.sh` verifies with the
package's `/usr/lib/teaport/allowed_signers`, never its checkout's. So each box takes the
release that ships the new keys by hand, from its operator, who already has root on it, the
way `teaport update` runs `install.sh` (as root, for the brain's user):

1. Get the new release's `manifest/stable.json`, the one that names it: from `main` after its
   manifest PR, or from `https://get.teaspoon.tech/manifest/stable.json`. (Not from the release
   tag: a tag's tree cannot hold the manifest that names its own commit, which comes after
   it.) Check it yourself: its `brain.commit` must equal `git rev-parse brain-vX.Y.Z^{commit}`
   in your own clone, and its sha256s your own record of the release. Copy only that file,
   without a `.sig`.
2. On the box, as root: a checkout of that release that root owns, and the manifest beside
   nothing (no `.sig`), both where only root writes:

   ```sh
   sudo git clone --depth 1 --branch brain-vX.Y.Z https://github.com/Teaspoon-AI/teaport.git /root/teaport-recovery
   sudo git -C /root/teaport-recovery rev-parse HEAD        # the commit you checked
   sudo install -m 0600 stable.json /root/teaport-stable.json
   systemctl show -p User --value teaport-brain.service     # the brain's user
   sudo TEAPORT_USER=<the brain's user> TEAPORT_MANIFEST_URL=/root/teaport-stable.json \
     TEAPORT_MANIFEST_UNSIGNED=1 /root/teaport-recovery/install.sh --only brain
   ```

   `TEAPORT_USER` keeps the units' `User=` the brain's user (without it, a root run renders
   `User=root`). Run it again with `--only engine` when the release changes the engine.
   Only a local manifest with no `.sig` is taken unsigned, and only with that variable set
   by whoever runs `install.sh`; `teaport update` never sets it.
3. The package it installs carries the new `allowed_signers`: from then on `teaport update`
   verifies manifests signed with the new keys. Fresh installs need the new bootstrap, which
   carries them.

Publishing that release cannot go through `publish`, by design: its check holds the new
manifest and bootstrap to the keys `main` records (the lost ones), so nothing signed by the
new keys alone passes. The owner publishes that one pair by hand, after checking it, from the
PR that carries it (the new keys in `manifest/allowed_signers`, in the bootstrap and in the
guide, `stable.json` naming the new release, both signed with the new main key):

```sh
# on a development machine (not the signing machine), in your own clone at the PR's head,
# the new keys being yours:
ssh-keygen -Y verify -f manifest/allowed_signers -I release-main -n teaport-manifest \
  -s manifest/stable.json.sig < manifest/stable.json
ssh-keygen -Y verify -f manifest/allowed_signers -I release-main -n teaport-bootstrap \
  -s install-bootstrap.sh.sig < install-bootstrap.sh
python3 manifest/check.py manifest/stable.json                # without --boxes-signers
python3 manifest/check.py --bootstrap install-bootstrap.sh
# each .sig before its file:
aws --profile teaport s3 cp manifest/stable.json.sig s3://teaport-get/manifest/stable.json.sig --content-type text/plain
aws --profile teaport s3 cp manifest/stable.json s3://teaport-get/manifest/stable.json --content-type application/json
aws --profile teaport s3 cp install-bootstrap.sh.sig s3://teaport-get/teaport.sig --content-type text/plain
aws --profile teaport s3 cp install-bootstrap.sh s3://teaport-get/teaport --content-type text/x-shellscript
```

Then merge, knowing that the PR's `publish` check and the merge's `publish` run fail (they
hold it to the lost keys) and upload nothing: the bucket already has the pair. A hand upload
is not a run, so the next run would still be measured against the lost keys. Once, after the
merge: check that the files `publish` uploads, as the merge commit has them, are the ones you
uploaded, then
dispatch `publish` with `base` = that merge commit, by id:

```sh
git fetch origin main && merge="$(git rev-parse origin/main)"
for f in manifest/stable.json manifest/stable.json.sig manifest/beta.json manifest/beta.json.sig \
         install-bootstrap.sh install-bootstrap.sh.sig; do
  git diff --quiet "$merge" -- "$f" || echo "differs: $f"   # in the clone you uploaded from
done
gh workflow run publish.yml --ref main -f base="$merge"
```

That run is measured against the new keys, passes, and uploads the same files again; from
then on it is the last upload, and the checks pass. Update the fingerprints page to the new
keys.

## What signing does not cover

Signing makes the bucket's write access, and a moved tag, unable to choose what a box
installs as root. It does not cover:

- **The plain one-line command's script.** `bash <(curl -fsSL get.teaspoon.tech/teaport)`
  runs the bootstrap as the download host serves it, trusting HTTPS to that host: a bucket
  writer can replace the script, signature or not. Everything after the script is checked
  (it verifies the manifest with the keys it carries, and fetches the signed commit, not
  `main`). A factory image that runs `install.sh` from a pinned checkout does not use the
  script at all.
- **The verify-first path's keys.** The installation guide checks the script against the two
  keys on the page, and the page is built from `main`: whoever can push to `main` can change
  the keys it shows along with the script's (a `publish` run then refuses a script signed by
  a key the published one does not carry, but the page is built on its own). So that path
  trusts GitHub write access to `main`, not only the keys. Publish the two keys'
  fingerprints somewhere not built from this repository (`<KEY-FINGERPRINTS-PAGE>`: fill in
  where), and the guide tells readers to compare the page's keys with them; checked that
  way, the path trusts only the keys.
- **`TEAPORT_REF`.** The bootstrap's developer path installs a branch or tag as GitHub has it,
  with no signature behind it; it says so when it runs.
- **The packages themselves.** A signature vouches for the sha256 of a `.deb` that CI built;
  nothing on the signing side rebuilds it. The signer trusts CI's build of the tagged commit.
- **Outsiders stalling a publish.** Unless fork PR workflows need approval for every
  external contributor ([GitHub settings](#github-settings)), a returning
  outsider's fork PR can run its own workflow in the `publish` concurrency group, cancel a
  `publish` run mid-upload (the next lookup then stops on the cut-off upload until a
  dispatch names a base) or hold the group so a merge goes unpublished. Nothing unsigned
  reaches boxes: this is availability only.
- **Freshness.** An older signed manifest stays valid: a bucket writer can serve one again.
  `teaport update` never installs an older brain or engine than the box has, but a fresh
  install takes whatever signed manifest it is served.

## Turning signing on (once)

Until this lands, `get.teaspoon.tech/teaport` is the old bootstrap, which clones `main` and
runs its `install.sh`, and `manifest/stable.json` is unsigned. From the merge on, `main`'s
`install.sh` verifies every manifest, and the new bootstrap installs nothing until it
carries real keys and the manifest it reads is signed by them. The first signed manifest
names the first release with keys, cut after the license gate (#439): every manifest has
`access`, and the installer of a release from before the gate (`brain-v0.1.0`,
`brain-v0.1.1-rc1`, `-rc2`) gets a 401 for every gated download, so no signed manifest
names one (`check.py` and the signing helper refuse it: no `ACCESS_PROTOCOL=1`).

From the signing change's merge until the turn-on's last upload, a fresh install through
the old bootstrap stops at the unsigned manifest, and nothing is installed. Everything that
can be done before that merge is done before it (step 1), so the window holds: the merge;
the tag and `release-deb.yml`'s run (about half an hour); the `.deb`'s upload; on the
laptop, the release's view and the signature, where the view is the whole diff from
`brain-v0.1.0` and its root and trust code in full: every such change since 0.1.0, the
long part; the manifest PR's checks (`publish` downloads every artifact, a few minutes, and
`ci.yml`, about half an hour), once for the PR and once more for its `.sig` commit; the
merge, the `r2-publish` approval and the upload. Half a day or more, longer if a check
fails or the view raises something. Do it when nobody is installing.

First, the repository's settings (as an admin), which keep the upload's secrets on `main`
and outsiders' workflows out of the `publish` runs:

- [ ] Before step 1: `r2-publish`, `gate-check` and fork pull request workflows as
      [GitHub settings](#github-settings) has them. `gate-check`'s `DL_CHECK_TOKEN` is the
      repository secret's value (Environment secrets > Add secret). Keep the
      repository-level secret for now: `main`'s `publish.yml` binds no environment until
      this change merges, and its runs read the repository's; a job that binds
      `gate-check` reads the environment's, which takes precedence.
- [ ] Right after this change merges: delete the repository-level `DL_CHECK_TOKEN`
      (Settings > Secrets and variables > Actions).

Then, in order:

1. **Before the merge, in the signing change's own PR.** The keys: on the signing laptop,
   with the helper's `keygen main` and `keygen backup` (the signing helper's SETUP.md;
   [The release keys](#the-release-keys) shows the same by hand), each fingerprint written
   down on paper; the two `.pub` files come to the development machine on the transfer
   stick. Their lines go into this PR: `manifest/allowed_signers` (the placeholder paragraph
   goes), the same file in `install-bootstrap.sh`'s `trusted_signers` block, and the two
   lines in the installation guide. Publish the keys' fingerprints
   (`cut -d' ' -f3,4 manifest/allowed_signers | ssh-keygen -lf -`) somewhere not built from
   this repository, and put its address in place of `<KEY-FINGERPRINTS-PAGE>` in the guide
   and in "What signing does not cover". In the guide, delete the sentence "Until the release
   keys are published, these two lines are placeholders, and the checks in the next steps
   fail." The shorter signers file and bootstrap move lines the docs cite: run
   `cd docs && node docs-work/lint.mjs --fix-sources`, read each item it lists as one it
   cannot place against the current file, and finish with `node docs-work/lint.mjs` at 0
   errors. Sign `install-bootstrap.sh` on the laptop
   ([Sign the bootstrap](#sign-the-bootstrap), `--base-commit <main before the turn-on>`:
   the bootstrap `main` has carries no keys) and commit its `.sig` as the PR's last commit;
   check it on the development machine as that section says (`verify-bootstrap`, and
   `check.py --boxes-signers --bootstrap`). Make the engine and model baseline on the
   laptop now too (the signing helper's SETUP.md): its paper record is what the first
   manifest's engine values are compared with. To make step 3's long read a re-read of
   code you know, read it once now: on the laptop, `sign_manifest.py trust-diff
   38f7fac2558d1e29bd8faca66a2ff316c8e78771 <the PR's head, 40 characters>` pages the whole
   diff of the root and trust files from `brain-v0.1.0` to the PR's head. It is read-only:
   it records nothing, and step 3's view is the same full view whether you ran it or not.
   The manifests stay as they are.
2. **Merge it.** The merge's `publish` run is red, as expected: its `artifacts` job stops at
   the unsigned `stable.json` and uploads nothing, so the bucket keeps the old bootstrap and
   manifest, and its `--bootstrap` line is never reached (the bootstrap's `.sig` is checked
   only by step 1's check on the development machine, and again in step 4's run). The
   merge itself is not blocked: `publish`'s jobs are not among the contexts main's ruleset
   requires (`brain` on both runners, `brain-deb`, `plugin`, `formal`, `installer`,
   `worker`, `docs`), and that red run is on `main`, after the merge.
3. **The first release with signing.** Cut `brain-v0.1.1` from `main` as usual; `main` now
   carries the keys, the gate's installer and the CI change that prints the `.deb`'s sha256
   (`release-deb.yml`), and `build-deb.sh` refuses a release build while any key is still
   the placeholder. Upload its `.deb`, and open its manifest PR: `manifest/stable.json` gets
   `brain.tag` `brain-v0.1.1`, `brain.commit` its commit, the `.deb`'s url and sha256, and
   keeps `access` and the engine and model entries it has. Sign it on the laptop with
   `--base-commit 38f7fac2558d1e29bd8faca66a2ff316c8e78771` (`brain-v0.1.0`, what boxes run:
   the laptop has no release in its record yet) and read its whole diff from `38f7fac` as
   [Sign the manifest](#sign-the-manifest) says: every file listed, the root and trust code
   in full. This one diff adds the signers file, which
   is expected here and never again outside a rotation, so check that its keys are exactly
   yours: the helper requires the keys at the new commit to be exactly its own two (boxes
   trust none yet: a LOOK, not a key change) and shows their fingerprints, which you compare
   with your paper. (On a development machine, by hand: the fingerprints of
   `git show <new brain.commit>:manifest/allowed_signers | grep '^release-' | cut -d' ' -f3,4 | ssh-keygen -lf -`.)
   `beta.json` stays unsigned while it is all placeholders. Commit the `.sig` as the PR's
   last commit. The PR's checks pass: `check.py --boxes-signers` verifies it with the PR's
   keys, and `--base` is the last upload, from before the turn-on: its `stable.json` names no
   commit and the bootstrap it serves carries no keys, so there is nothing earlier to hold
   them to; the release's commit and package carry exactly this tree's keys, and its
   installer says `ACCESS_PROTOCOL=1`.
4. **Merge.** `publish` runs the same checks, the bootstrap's included, and uploads
   `stable.json.sig`, `stable.json`, `beta.json` (removing any `beta.json.sig`),
   `teaport.sig` and `teaport`, in that order; from its upload on, it is the last upload.
   From then on a fresh install verifies the bootstrap's manifest, runs `0.1.1`'s installer,
   which checks everything, and boxes on it verify every manifest. Boxes already installed
   are unchanged until they take `0.1.1` (below).

Boxes that `teaport update` cannot carry past this, and how each recovers:

- **A package from between #417 and this change's merge** (`brain-v0.1.1-rc1`, or a dev
  build of `main` from then). Its updater hands the new `install.sh` a manifest with no
  signature beside it, which it refuses. The package has no `/usr/lib/teaport/allowed_signers`,
  so `install.sh` verifies with its checkout's keys: one `./install.sh --only brain` from a
  checkout of the new release, as the box's user, installs it.
- **A dev build of `main` from between this change's merge and step 3.** Its package carries
  the placeholder signers file, which `install.sh` and `teaport update` prefer over any
  checkout's, so it refuses every manifest (no keys). It takes the new package without a
  manifest: download the release's `.deb` and the published `stable.json` with its `.sig`,
  check the signature yourself
  (`ssh-keygen -Y verify -f <a signers file with the two keys from the fingerprints page> -I release-main -n teaport-manifest -s stable.json.sig < stable.json`)
  and the `.deb`'s sha256 against its `brain.deb.sha256`, then, from a checkout of the
  release, as the box's user:
  `TEAPORT_BRAIN_DEB=/path/to/teaport-brain_X.Y.Z_arm64.deb ./install.sh --only brain`.
  With `TEAPORT_BRAIN_DEB`, `--only brain` reads no manifest; the package it installs
  carries the real keys, and `teaport update` works from there on. (The unsigned path
  under "With both keys lost" works too.)

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
  branches `main` only; required reviewer `teaspoonai`, "Prevent self-review" unticked,
  "Allow administrators to bypass configured protection rules" unticked, so each upload
  waits in its run's page until you approve it, once you have checked that the run is the
  merge you expect; secrets `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_ENDPOINT`,
  an R2 token with write access to `teaport-get`, the bucket that serves the manifests and
  the bootstrap a box runs as root. Without the branch policy, a workflow on any branch
  could read them.
- **`gate-check` environment** (`publish.yml`'s `artifacts` job, on a run that is not a pull
  request's): deployment branches `main` only, no reviewer (the weekly run uses it
  unattended); secret `DL_CHECK_TOKEN`, equal to the download gate Worker's
  `CHECK_TOKEN`, with which the publish check downloads the gated files. Never a
  repository secret: a workflow on any pushed branch can read one, an environment's only
  the branches it allows. A pull request's run binds no environment and checks the gated
  files without the token; run `publish` by hand (Actions > publish > Run workflow) from
  `main` only, as from another branch the job stops at the branch policy. Change the
  secret and `CHECK_TOKEN` together (`wrangler secret put CHECK_TOKEN` with the locked
  wrangler, under "Worker secrets", then the secret), or the next check fails.
- **Fork pull request workflows** (Settings > Actions > General): require approval for all
  external contributors, not only first-time ones. Concurrency groups are repository-wide:
  a fork PR can add its own workflow file in the `publish` group
  (`publish-refs/heads/main`, or `publish-weekly`) with `cancel-in-progress`, and cancel a
  `publish` run mid-upload or hold the group and displace a waiting run. Before approving
  a fork PR's run, read its `.github/workflows` diff: a fork's workflow file runs in this
  repository and shares its concurrency groups.
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
- **`CHECK_TOKEN`** is the publish check's (the `gate-check` environment's `DL_CHECK_TOKEN`,
  under GitHub settings).
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
    download tokens with it, whatever the license's version, for `MAKER_MAX_DAYS` (3, in
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
  - **What slows a leak down, and what shows it:** at most `FACTORY_DAILY_MAX` (50) boxes
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
- A candidate is built like a release: until the real keys replace the `PLACEHOLDER` lines
  in `manifest/allowed_signers` and `install-bootstrap.sh`'s key block, `build-deb.sh`
  refuses an rc tag's build, in `ci.yml` and in `release-deb.yml` alike.
- Test a candidate the same way as a release (above). A candidate goes on the
  **beta** channel only: `manifest/beta.json` gets its `brain` entry, with `tag` set to
  the candidate's tag. Stable never names a candidate. That is enforced, not only
  written. A signed manifest that names a release (a full `brain.commit`) with a tag that
  is no release tag, that names `brain-vX.Y.Z-rcN` anywhere but in `beta.json`, or whose
  `brain.deb` is not `teaport-brain_X.Y.Z~rcN_<arch>.deb`, is refused by the signing
  helper, by `check.py`, by every box (`manifest_sig.release_problem`, in the installer,
  `teaport update` and the daily check, which offers none of these), and by the one-line
  installer. The signing helper and `check.py` also read the package and hold its control
  file's `Version` to its file name's and to the tag's. `release-deb.yml` builds and hashes a candidate's tag as a release's
  (its `brain-v*` filter covers both), and the signing helper reads it the same way.
- Upload it under its own name, as above. Like a release, a candidate's file is never
  overwritten: a fix is the next candidate.
- When the final `brain-vX.Y.Z` ships on stable, move `beta.json` on too, in a signed PR of
  its own: to the final tag (beta may name a final release) or to the next candidate.
  Otherwise beta boxes stay on the last candidate.

## After

- [ ] From a box, with no `TEAPORT_BRAIN_DEB`: `./install.sh --only brain --dry-run` prints
      the download of the package (its URL carries the version), then the real run installs
      it.
- [ ] Note the release (version, `brain.commit`, the `.deb`'s sha256, date) where you keep
      release notes, off this repository: the next release's diff starts from that
      `brain.commit` (Sign the manifest).
