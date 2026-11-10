#
# test_collapse_eval.py's verdict, offline (#384, review round 1 of #401): the eval run
# against a local stub of the endpoint. A provider that refuses every request (a bad key,
# a spent limit) must fail loudly, not pass with "0 collapses"; a clean one passes; one
# that collapses fails.
#
# Run: python test_collapse_eval_verdict.py   (or via pytest test_suite.py)
#
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.join(HERE, "test_collapse_eval.py")

CLEAN = "It's three eleven in the afternoon."
COLLAPSED = "Sure" + "\u00a0\u00a0…" * 6 + "\n\n\n\n"     # the guard trips at five "…"


def _stub(mode: str):
    """A chat/completions endpoint: "refuse" answers 401, else streams one reply."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if mode == "refuse":
                body = json.dumps({"error": {"message": "No auth credentials found",
                                             "code": 401}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            text = CLEAN if mode == "clean" else COLLAPSED
            chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                    "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            body = "".join(
                "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 0,
                                       "model": "m", **c}) + "\n\n" for c in chunks)
            body = (body + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_eval(mode: str):
    server = _stub(mode)
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "TEAPORT_EVAL"))}
        env.update({"TEAPORT_EVAL_COLLAPSE": "1", "TEAPORT_EVAL_TRIALS": "1",
                    "TEAPORT_EVAL_SHAPES": "lead",
                    "LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "LLM_API_KEY": "not-a-real-key", "LLM_EXTRA_BODY": "",
                    "TEAPORT_URL": env.get("TEAPORT_URL", "ws://127.0.0.1:9/v1/realtime")})
        proc = subprocess.run([sys.executable, EVAL], env=env, capture_output=True, text=True,
                              timeout=90)
        return proc.returncode, proc.stdout
    finally:
        server.shutdown()


def test_a_provider_that_refuses_every_request_fails_loudly():
    code, out = _run_eval("refuse")
    assert code == 1, (code, out[-800:])
    assert "refused 12 of 12" in out and "no collapse rate" in out, out[-800:]
    assert "ALL PASS" not in out


def test_a_clean_provider_passes():
    code, out = _run_eval("clean")
    assert code == 0, (code, out[-800:])
    assert "ALL PASS (0 collapses in 12 answered, 0 refused)" in out, out[-800:]


def test_a_provider_that_collapses_fails():
    code, out = _run_eval("collapse")
    assert code == 1, (code, out[-800:])
    assert "12 collapses in 12 answered" in out, out[-800:]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
