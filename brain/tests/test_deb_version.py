"""build-deb.sh's version: dev builds, releases and release candidates.

Runs the real script in a throwaway git repo with TEAPORT_DEB_PRINT_VERSION=1, which
stops right after the version is chosen (before any tool, sudo or build step)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from teaport_brain.updates import version_cmp  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "build-deb.sh"
BASE = re.search(r'^BASE_VERSION="([^"]+)"', SCRIPT.read_text(), re.M).group(1)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


class DebVersion(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / "packaging").mkdir()
        shutil.copy(SCRIPT, self.repo / "packaging" / "build-deb.sh")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "x")

    def version(self, **env):
        r = subprocess.run(["bash", str(self.repo / "packaging" / "build-deb.sh")],
                           capture_output=True, text=True,
                           env={**os.environ, "TEAPORT_DEB_PRINT_VERSION": "1", **env})
        return r.returncode, r.stdout.strip(), r.stderr

    def test_dev_build(self):
        rc, out, err = self.version(TEAPORT_DEB_BUILD="12")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, rf"^{re.escape(BASE)}~dev12\+g[0-9a-f]{{8}}$")

    def test_release(self):
        git(self.repo, "tag", f"brain-v{BASE}")
        rc, out, err = self.version(TEAPORT_DEB_VERSION=BASE)
        self.assertEqual((rc, out), (0, BASE), err)

    def test_release_candidate(self):
        git(self.repo, "tag", f"brain-v{BASE}-rc2")
        rc, out, err = self.version(TEAPORT_DEB_VERSION=f"{BASE}-rc2")
        self.assertEqual((rc, out), (0, f"{BASE}~rc2"), err)

    def test_candidate_needs_its_own_tag(self):
        git(self.repo, "tag", f"brain-v{BASE}")   # the release's tag is not the rc's
        rc, _, err = self.version(TEAPORT_DEB_VERSION=f"{BASE}-rc1")
        self.assertNotEqual(rc, 0)
        self.assertIn(f"brain-v{BASE}-rc1", err)

    def test_candidate_must_be_tagged_head(self):
        git(self.repo, "tag", f"brain-v{BASE}-rc1")
        (self.repo / "packaging" / "more").write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "later")
        rc, _, err = self.version(TEAPORT_DEB_VERSION=f"{BASE}-rc1")
        self.assertNotEqual(rc, 0)
        self.assertIn("must be built from", err)

    def test_refused_shapes(self):
        major, minor, patch = BASE.split(".")
        other = f"{major}.{minor}.{int(patch) + 1}"
        for bad in (f"{BASE}-rc0", f"{BASE}~rc1", f"{BASE}-rc", f"{BASE}-beta1",
                    f"{BASE}-rc01", f"v{BASE}", f"{other}-rc1", other):
            # tagged where git allows the name (~ is not), so only the shape refuses it
            subprocess.run(["git", "-C", str(self.repo), "tag", "-f", f"brain-v{bad}"],
                           capture_output=True, check=False)
            rc, _, err = self.version(TEAPORT_DEB_VERSION=bad)
            self.assertNotEqual(rc, 0, bad)

    def test_order(self):
        # what apt (and updates.version_cmp, update_apply.not_older) install by
        chain = [f"{BASE}~dev999+gffffffff", f"{BASE}~rc1", f"{BASE}~rc2", f"{BASE}~rc10",
                 BASE]
        for lower, higher in zip(chain, chain[1:]):
            self.assertEqual(version_cmp(lower, higher), -1, (lower, higher))
            if shutil.which("dpkg"):
                self.assertEqual(subprocess.run(
                    ["dpkg", "--compare-versions", lower, "lt", higher]).returncode, 0,
                    (lower, higher))


if __name__ == "__main__":
    unittest.main()
