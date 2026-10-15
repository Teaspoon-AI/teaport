#
# services.warm_llm_imports loads everything a client's first chat request imports.
#
# openai builds client.chat lazily, and that import (~540 modules) ran on the event
# loop inside the brain's first chat request: a 1297 ms freeze right after a wake word
# on the box (2026-10-15), once per process. The server now calls warm_llm_imports at
# startup (test_memory_reclaim pins the call, before the freeze). This pins what it
# covers: after it, a fresh client's chat API imports nothing more. An openai bump that
# moves the lazy import fails here.
#
# Run: python test_llm_warmup.py   (or via pytest test_suite.py)
#
import os
import subprocess
import sys

import appliance

_CHILD = """
import sys
from openai import AsyncOpenAI
from teaport_brain.services import warm_llm_imports
warm_llm_imports()
before = set(sys.modules)
client = AsyncOpenAI(api_key="warm-up-test", base_url="http://127.0.0.1:9/v1")
client.chat.completions
print(" ".join(sorted(m for m in set(sys.modules) - before if m.startswith("openai"))) or "-")
"""


def test_after_the_warm_up_a_clients_chat_api_imports_nothing():
    # A fresh interpreter: in this one, another test may have imported the API already.
    brain = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": brain}, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    late = p.stdout.strip().splitlines()[-1].split()
    assert late == ["-"], (f"{len(late)} openai modules imported on the first chat access, "
                           f"after the warm-up: {' '.join(late[:6])} ...")


def test_without_the_warm_up_the_chat_api_is_still_lazy():
    # The control: if openai ever loads it eagerly, the warm-up is dead code.
    brain = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = _CHILD.replace("warm_llm_imports()\n", "")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": brain}, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    assert "openai.resources.chat" in p.stdout, p.stdout[-500:]


if __name__ == "__main__":
    appliance.run_tests([test_after_the_warm_up_a_clients_chat_api_imports_nothing,
                         test_without_the_warm_up_the_chat_api_is_still_lazy])
