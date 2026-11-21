#
# manifest/check.py's list of what a release manifest must name, against install.sh's reads:
# from engine 1.7 on, the AI watermark's weights (engine.watermark) and the Mandarin dict
# (engine.zh_dict) are among them, by the same rule install.sh's engine_release_from
# applies. Offline: nothing is fetched.
#
# Run: python test_manifest_check.py   (or via pytest test_suite.py)
#
import importlib.util
import json
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"


def _check():
    spec = importlib.util.spec_from_file_location("manifest_check", REPO / "manifest/check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_from(release, var):
    """install.sh's own answer: engine_release_from and its <var> (ENGINE_WATERMARK_FROM,
    ENGINE_ZH_FROM), as written."""
    text = INSTALL.read_text()
    first = re.search(rf"^{var}=.*$", text, re.M).group(0)
    lines = text.splitlines()
    start = lines.index("engine_release_from() {")
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    fn = "\n".join(lines[start:end + 1])
    p = subprocess.run(["bash", "-c", f'{first}\n{fn}\nengine_release_from "$1" "${var}"', "-", release],
                       capture_output=True, text=True)
    assert p.returncode in (0, 1), p.stderr
    return p.returncode == 0


def test_the_release_rules_are_install_shs():
    check = _check()
    for release in ("1.6", "1.6.9", "1.7", "1.7.0", "1.10", "2", "TODO", "unknown", "", "1.7-rc1"):
        assert check.release_marks(release) == _install_from(release, "ENGINE_WATERMARK_FROM"), release
        assert check.release_from(release, check.ZH_FROM) == _install_from(release, "ENGINE_ZH_FROM"), release
    assert check.release_marks(None) and check.release_from(None, check.ZH_FROM)


def test_a_release_that_marks_must_name_its_watermark():
    check = _check()
    for entry in ("engine.watermark", "engine.zh_dict"):
        assert entry not in check.required({"engine": {"release": "1.6"}})
        assert entry in check.required({"engine": {"release": "1.7"}})
        assert entry in check.required({})   # no release named: fail closed
    # What the channels carry today: stable (1.6) does not need it, beta (a placeholder) names one.
    stable = json.loads((REPO / "manifest/stable.json").read_text())
    beta = json.loads((REPO / "manifest/beta.json").read_text())
    if not check.release_marks(stable["engine"]["release"]):
        assert "engine.watermark" not in check.required(stable)
    else:
        assert stable["engine"].get("watermark", {}).get("sha256"), "stable marks but names no watermark"
    assert "engine.watermark" in check.required(beta) and beta["engine"]["watermark"]["url"]
    assert "engine.zh_dict" in check.required(beta) and beta["engine"]["zh_dict"]["url"]
    if check.release_from(stable["engine"]["release"], check.ZH_FROM):
        assert stable["engine"].get("zh_dict", {}).get("sha256"), "stable reads the Mandarin dict but names none"


if __name__ == "__main__":
    test_the_release_rules_are_install_shs()
    test_a_release_that_marks_must_name_its_watermark()
    print("ok")
