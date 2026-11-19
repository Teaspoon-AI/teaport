#
# teaport — how the run user calls a JSON root helper: wifi_apply.py (wifi.py) and
# login_apply.py (owner_login.py). Each is a root-owned copy under /usr/lib/teaport that
# its sudoers line names (packaging/deb/sudoers-sync), run on the system python, never the
# venv's interpreter nor this tree's copy. The verb is its only argument and the request a
# JSON object on stdin, so a password never goes on a command line; the reply is a JSON
# object on stdout. Standard library only: Wi-Fi setup runs this alone on a box short of
# memory.
#
import json
import subprocess

SYSTEM_PYTHON = "/usr/bin/python3"


def call(helper: str, verb: str, req: dict, *, timeout: float, stop_secs: float,
         name: str) -> dict:
    """`helper` <verb> under sudo, the request on stdin: its reply; or {"ok": False,
    "err": ...} when it could not run, and the same with the reply's other fields when it
    refused (its "refused" is the "err"). `name` says which helper in those errors."""
    try:
        p = subprocess.Popen(["sudo", "-n", SYSTEM_PYTHON, helper, verb], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return {"ok": False, "err": str(e)}
    try:
        out, err = p.communicate(json.dumps(req), timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return {"ok": False, "err": f"{name} timed out"}
    except BaseException:
        # A stop (wifi_setup's Stopped): the helper had the unit's SIGTERM too and is
        # finishing or putting back what it began. Wait for that, so the caller's own
        # clean-up (the old network back up) does not race it.
        try:
            p.wait(timeout=stop_secs)
        except BaseException:
            p.kill()
        raise
    try:
        reply = json.loads(out)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"ok": False, "err": (err.strip() or f"{name} exited {p.returncode}")[-300:]}
    if "refused" in reply:
        return {**{k: v for k, v in reply.items() if k != "refused"},
                "ok": False, "err": str(reply["refused"])}
    return reply
