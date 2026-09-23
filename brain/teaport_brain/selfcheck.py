#
# selfcheck.py — "does this venv actually run the brain?", asked before it goes live.
#
# install.sh builds each brain venv beside the live one and swaps it in only after this
# passes (python -m teaport_brain.selfcheck, run by the NEW venv's interpreter). A clean
# `uv sync` proves the lock resolved, not that the result works on this box: the risk
# pyproject.toml names is a compiled extension (onnxruntime, loudness) breaking under a
# numpy major, which only shows up when those extensions actually run. So this does more
# than import them — it pushes audio through the two ONNX models the pipeline loads in
# process (Silero VAD, which also measures loudness, and Smart Turn v3).
#
# Hermetic on purpose: no engine, no LLM, no network, no services touched. It is safe to
# run against a venv that is not live yet, and on a box whose engine is busy with a call.
# Exit 0 = pass; anything else names the check that failed.
#
import asyncio
import importlib
import sys

import numpy as np

SAMPLE_RATE = 16000


def _check_imports():
    # Both front-ends: the OpenClaw relay (teaport-brain) and the SIP client
    # (teaport-sip-brain). Importing each pulls in the shared pipeline and every service
    # module, so a missing or ABI-broken dependency anywhere in the tree fails here.
    for mod in ("teaport_brain.gateway_server", "teaport_brain.sip_server"):
        importlib.import_module(mod)


async def _check_vad():
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    vad = SileroVADAnalyzer(sample_rate=SAMPLE_RATE)
    vad.set_sample_rate(SAMPLE_RATE)  # what the transport does at StartFrame
    # A 440 Hz tone at speech level: enough to make the loudness measurement and the
    # ONNX session both do real work (silence can short-circuit either).
    n = vad.num_frames_required()
    t = np.arange(n) / SAMPLE_RATE
    frame = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()
    for _ in range(4):
        await vad.analyze_audio(frame)
    conf = vad.voice_confidence(frame)
    if not 0.0 <= conf <= 1.0:
        raise RuntimeError(f"Silero VAD returned an out-of-range confidence: {conf!r}")


def _check_smart_turn():
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    st = LocalSmartTurnAnalyzerV3(sample_rate=SAMPLE_RATE)
    st.set_sample_rate(SAMPLE_RATE)
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(SAMPLE_RATE) * 0.05).astype(np.float32)
    result = st._predict_endpoint(audio)
    prob = result.get("probability")
    if prob is None or not 0.0 <= prob <= 1.0:
        raise RuntimeError(f"Smart Turn v3 returned no usable probability: {result!r}")


def main() -> int:
    checks = [
        ("import both front-ends", _check_imports),
        ("Silero VAD (onnxruntime + loudness)", lambda: asyncio.run(_check_vad())),
        ("Smart Turn v3 (onnxruntime)", _check_smart_turn),
    ]
    for name, fn in checks:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — every failure is reported, by name
            print(f"selfcheck FAILED: {name}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"selfcheck ok: {name}")
    print(f"selfcheck passed (python {sys.version.split()[0]}, numpy {np.__version__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
