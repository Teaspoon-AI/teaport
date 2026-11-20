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
- [ ] The PR's `publish` check downloads the package and compares its sha256
      (`manifest/check.py`); it fails when the upload is missing or differs. That is why the
      upload comes first.
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
- [ ] Merge. The `publish` workflow uploads the manifests and reads each back from
      get.teaspoon.tech.

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
