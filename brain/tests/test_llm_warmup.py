#
# services.warm_first_use_imports loads everything the first session and the first reply
# would otherwise import on the event loop.
#
# openai builds client.chat lazily, and that import (~500 modules) ran on the event loop
# inside the brain's first chat request: a 1297 ms freeze right after a wake word on the
# box (2026-10-15), once per process. The server now calls warm_first_use_imports at
# startup (test_memory_reclaim pins the call, before the freeze). This pins what it
# covers: after it, a first streamed chat completion and pipecat's first sentence split
# import nothing more. An openai or pipecat bump that adds a lazy import on that path
# fails here.
#
# Run: python test_llm_warmup.py   (or via pytest test_suite.py)
#
import os
import subprocess
import sys

import appliance

# A fresh interpreter per case: in this one, another test may have imported it all already.
# The completion goes to a local HTTP server through openai's default http client (the
# transport the brain's client uses), so the whole path runs: the connection (httpcore,
# anyio's backend), the request, SSE parsing, the chunk models. No network.
_FIRST_REPLY = """
import asyncio, sys
import nltk
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pipecat.utils.string import match_endofsentence
from teaport_brain.services import warm_first_use_imports
try:  # the warm-up splits a sentence only with the data on disk, as on a box
    nltk.data.find("tokenizers/punkt_tab")
except LookupError:
    nltk.download("punkt_tab", quiet=True)
WARM
SSE = (b'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
       b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hi there."},'
       b'"finish_reason":null}]}\\n\\ndata: [DONE]\\n\\n')
async def serve(reader, writer):
    head = await reader.readuntil(b"\\r\\n\\r\\n")
    length = [int(h.split(b":")[1]) for h in head.split(b"\\r\\n")
              if h.lower().startswith(b"content-length:")]
    await reader.readexactly(length[0] if length else 0)
    writer.write(b"HTTP/1.1 200 OK\\r\\ncontent-type: text/event-stream\\r\\n"
                 b"content-length: %d\\r\\nconnection: close\\r\\n\\r\\n" % len(SSE) + SSE)
    await writer.drain()
    writer.close()
async def first_reply():
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    before = set(sys.modules)
    client = AsyncOpenAI(api_key="warm-up-test", base_url=f"http://127.0.0.1:{port}/v1",
                         http_client=DefaultAsyncHttpxClient())
    stream = await client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True)
    text = "".join([c.choices[0].delta.content or "" async for c in stream])
    assert text == "Hi there.", text
    assert match_endofsentence(text)
    late = sorted(set(sys.modules) - before)
    await client.close()
    server.close()
    return late
print(" ".join(asyncio.run(first_reply())) or "-")
"""


def _child(code: str, **env) -> str:
    """Run code in a fresh interpreter; its last line. This machine's proxy settings are
    kept (the child may download punkt_tab through them) but bypassed for the local
    server the first reply goes to."""
    brain = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = "127.0.0.1,localhost"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": brain, "no_proxy": local,
                            "NO_PROXY": local, **env}, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    return p.stdout.strip().splitlines()[-1]


def _first_reply(warm: bool) -> list[str]:
    """The modules a first streamed reply imports, after the warm-up or without it."""
    return _child(_FIRST_REPLY.replace("WARM", "warm_first_use_imports()" if warm else "")
                  ).split()


def test_after_the_warm_up_a_first_reply_imports_nothing():
    late = _first_reply(warm=True)
    assert late == ["-"], (f"{len(late)} modules imported by the first reply, after the "
                           f"warm-up: {' '.join(late[:8])} ...")


def test_without_the_warm_up_the_first_reply_imports_the_chat_api():
    # The control: if openai ever loads it eagerly, this part of the warm-up is dead code.
    assert "openai.resources.chat" in _first_reply(warm=False)


def test_offline_without_nltk_data_the_warm_up_neither_downloads_nor_fails():
    # A first boot with no network (the spoken Wi-Fi setup is why the brain must start
    # then) and punkt_tab never downloaded: pipecat's loader would try the download (on
    # a blackholed network, with no timeout) and leave the tokenizer broken. The warm-up
    # must not try it, and must return; the first session's own warm-up fetches the
    # data later, in its thread.
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        out = _child(
            "import os, sys, nltk\n"
            "nltk.data.path[:] = [os.path.join(os.environ['HOME'], 'nltk_data')]\n"
            "nltk.download = lambda *a, **k: sys.exit(f'the warm-up tried to download {a}')\n"
            "from teaport_brain.services import warm_first_use_imports\n"
            "warm_first_use_imports()\n"
            "print('returned')\n",
            HOME=home)
    assert out == "returned", out


def test_broken_nltk_data_on_disk_does_not_stop_the_start():
    # punkt_tab there but cut short (an unzip interrupted): the split raises. The
    # warm-up is an optimisation; the brain must still start.
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        os.makedirs(os.path.join(home, "nltk_data", "tokenizers", "punkt_tab"))
        out = _child(
            "import os, nltk\n"
            "nltk.data.path[:] = [os.path.join(os.environ['HOME'], 'nltk_data')]\n"
            "from teaport_brain.services import warm_first_use_imports\n"
            "warm_first_use_imports()\n"
            "print('returned')\n",
            HOME=home)
    assert out == "returned", out


if __name__ == "__main__":
    appliance.run_tests([test_after_the_warm_up_a_first_reply_imports_nothing,
                         test_without_the_warm_up_the_first_reply_imports_the_chat_api,
                         test_offline_without_nltk_data_the_warm_up_neither_downloads_nor_fails,
                         test_broken_nltk_data_on_disk_does_not_stop_the_start])
