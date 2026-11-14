# Releasing the brain package

The brain ships as the `teaport-brain` .deb (#60). CI builds it; a person releases it. Nothing
in CI uploads it: the upload to the download bucket and the manifest entry are the two steps
that make a release, and both are deliberate.

What a release is, end to end:

    tag brain-vX.Y.Z  ->  CI builds the .deb  ->  you test it  ->  you upload it
                      ->  manifest PR (url + sha256)  ->  merge publishes the manifest

`install.sh` and `teaport update` read `manifest/stable.json` from get.teaspoon.tech, so a
package a box can install exists only once its manifest entry is merged *and* published.

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

- [ ] `TEAPORT_BRAIN_DEB=/tmp/brain-release/teaport-brain_X.Y.Z_arm64.deb ./install.sh --only brain`
      on a spare box: a first install on a box that still has the old venvs, and an upgrade
      over the previous release. The run must end with the brain up.
- [ ] `teaport doctor` has no failures, `dpkg -V teaport-brain` is silent.
- [ ] The Settings page opens; its footer shows the revision and build date; `/licenses`
      opens, and so does `teaport licenses --notices`.
- [ ] A call or a room turn works end to end.
- [ ] `./install.sh --rollback brain` after an upgrade puts the previous package back, and a
      second run undoes it.

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

## The manifest

In a PR, set in `manifest/stable.json` (the beta channel keeps its `TODO` until a beta):

```json
"brain": {
  "repo": "Teaspoon-AI/teaport", "tag": "main",
  "deb": {
    "url": "https://get.teaspoon.tech/dl/brain/teaport-brain_X.Y.Z_arm64.deb",
    "sha256": "<the .sha256 file's hash>"
  }
}
```

- [ ] The PR's `publish` check downloads the package and compares its sha256
      (`manifest/check.py`); it fails when the upload is missing or differs. That is why the
      upload comes first.
- [ ] After the first release, add `brain.deb` to `REQUIRED` in `manifest/check.py`, so a
      manifest without one fails the check.
- [ ] Merge. The `publish` workflow uploads the manifests and reads each back from
      get.teaspoon.tech.

## After

- [ ] From a box, with no `TEAPORT_BRAIN_DEB`: `./install.sh --only brain --dry-run` names the
      package and its version, then the real run installs it.
- [ ] Note the release (version, sha256, date) where you keep release notes.
