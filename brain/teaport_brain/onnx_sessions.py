#
# teaport — one ONNX Runtime session per model, for the whole brain process.
#
# The brain runs two ONNX models in process, both built by pipecat: Silero VAD
# (pipecat/audio/vad/silero.py, SileroOnnxModel.__init__) and Smart Turn v3
# (pipecat/audio/turn/smart_turn/local_smart_turn_v3.py, LocalSmartTurnAnalyzerV3.__init__).
# build_agent_session builds a fresh analyzer of each for every session -- every wake
# word, every Talk reconnect, every phone call -- and pipecat builds a fresh
# onnxruntime.InferenceSession inside each one. Measured 2026-10-13 (desktop A/B with
# the box's exact pins: Python 3.12, pipecat 1.8.1, onnxruntime 1.24.4, numpy 2.5.2): a
# second concurrent copy of the pair costs ~47-52 MB anonymous memory, and copies DO
# overlap -- during a session handover, and while a room session and a phone call
# coexist -- on an 8 GB unified-memory box that OpenClaw and a NemoClaw sandbox share.
#
# So this module does two things, at the one seam pipecat leaves:
#
#   1. SHARES each InferenceSession process-wide, keyed by model path + options. That is
#      safe because the session holds no per-stream state, on either model:
#        - Silero's recurrent state (h/c, as one `state` tensor) and its 64-sample
#          context live on SileroOnnxModel (`self._state`, `self._context`; silero.py
#          85-88), are fed IN as inputs and read back OUT of `session.run` (silero.py
#          116-119). Each analyzer keeps its own SileroOnnxModel; only `.session` is
#          shared.
#        - Smart Turn scores the whole (<= 8 s) turn window in one `run` with no
#          state carried between calls (local_smart_turn_v3.py 161-168).
#      Neither pipecat site does anything to the session but `.run()` (silero.py 117,
#      local_smart_turn_v3.py 168). Concurrent `run()` is what sharing adds: each
#      analyzer runs its model on its OWN single-thread executor (vad_analyzer.py 92,
#      base_smart_turn.py 182), so two sessions' VADs can call one InferenceSession at
#      once. ONNX Runtime documents that as supported -- InferenceSession::Run:
#      "Multiple threads are allowed to run this function; hence its thread-safe."
#      (onnxruntime/core/session/inference_session.h, v1.24.4), and the Python binding
#      releases the GIL around it. tests/test_onnx_sessions.py runs interleaved and
#      concurrent audio through two analyzers on one session and checks each gets
#      exactly what it gets alone.
#
#   2. TURNS OFF the CPU memory arena and the memory-pattern planner
#      (enable_cpu_mem_arena / enable_mem_pattern). The arena keeps the peak of every
#      run's intermediate tensors for the life of the session and grows it in chunks;
#      the pattern planner pre-sizes one block per input shape it has seen. Both are
#      throughput features for big batched models; these two models are tiny (Silero
#      runs a 576-sample frame) and the brain is not throughput-bound. Every option
#      pipecat sets (1/1 threads, sequential execution, ORT_ENABLE_ALL) is kept: this
#      only adds to the SessionOptions pipecat built.
#
# Measured 2026-10-13 on the desktop (same pins; three runs each, load average 5-8 on 6
# cores), anonymous memory after a malloc_trim (MemoryReclaim then ran one per reply): one live
# session 90-96 -> 61-62 MB, two concurrent sessions 142-143 -> 62 MB. Smart Turn's
# median inference and Silero's per-frame time did not move beyond the desktop's noise
# (~140-150 ms and ~0.25 ms, both ways). Before the trim the arena-off peak is barely
# lower (one session ~94 vs ~101 MB): the freed tensors sit in glibc until the trim.
# The cost: the shared sessions stay resident with no session built (an idle brain
# held ~44 MB anon before, ~61 MB now) -- which on the box is rarely the case, as the
# room mic's session stays up, asleep behind its wake gate. 50 sequential build/run/
# teardown cycles grew anon +0.3 MB with this, +0.5 to +28.7 MB without: one run in
# three jumped +27 MB between cycles 25 and 50, a ratchet the per-session rebuilds can
# cause and a shared session has nothing to cause it with. (Whether that is the brain's
# 145 -> 290 MB growth over hours is not shown by this.)
#
# WHY A MODULE-NAME SWAP AND NOT A SUBCLASS. pipecat gives no way in: both sessions are
# built inside __init__ (silero.py 49-58, local_smart_turn_v3.py 70-76) from SessionOptions
# pipecat creates on the spot. A brain subclass would have to skip those two __init__s
# and copy their bodies (model-path resolution, Silero's state set-up, the options
# pipecat chooses), which a pipecat bump would leave silently out of date -- and letting
# them run and swapping the session afterwards still builds (and frees) a whole session
# per analyzer, the very churn this removes. So install() replaces the `onnxruntime`
# name in exactly those two pipecat modules (silero.py binds it as `onnxruntime`,
# local_smart_turn_v3.py as `ort`) with a stand-in whose InferenceSession is
# shared_session() and whose every other attribute is the real module's. Nothing else in
# the process sees it: onnxruntime itself is untouched, and so is any other importer.
# If a pipecat bump renames either import, install() logs it and that model is simply
# built per session again; test_onnx_sessions.py fails on the pin bump, which is when
# to look.
#
# install() is called by build_agent_session -- the one place every front-end (Talk,
# the room mic, SIP) builds its analyzers -- before it builds them, and by selfcheck.py,
# so the new venv is checked under the options the brain actually runs. It is idempotent.
#
import os
import sys
import threading

import onnxruntime
from loguru import logger

# (pipecat module, the name it imports onnxruntime under). See the note above.
_SITES = (
    ("pipecat.audio.vad.silero", "onnxruntime"),
    ("pipecat.audio.turn.smart_turn.local_smart_turn_v3", "ort"),
)

# What the brain adds to every SessionOptions it is handed (see 2. above).
_BRAIN_OPTIONS = {"enable_cpu_mem_arena": False, "enable_mem_pattern": False}

# The SessionOptions fields that make two sessions different, for the cache key: two
# requests for one model with different threading or optimisation must not share.
# (pipecat sets none of SessionOptions' config entries, which cannot be enumerated.)
_KEY_FIELDS = (
    "intra_op_num_threads", "inter_op_num_threads", "execution_mode", "execution_order",
    "graph_optimization_level", "enable_cpu_mem_arena", "enable_mem_pattern",
    "enable_mem_reuse", "use_deterministic_compute", "log_severity_level",
    "optimized_model_filepath", "enable_profiling",
)

_lock = threading.Lock()
_sessions: dict[tuple, onnxruntime.InferenceSession] = {}
_installed: dict[str, bool] = {}


def brain_options(sess_options: onnxruntime.SessionOptions | None = None
                  ) -> onnxruntime.SessionOptions:
    """`sess_options` (or a fresh SessionOptions) with the brain's memory options set."""
    so = sess_options if sess_options is not None else onnxruntime.SessionOptions()
    for name, value in _BRAIN_OPTIONS.items():
        setattr(so, name, value)
    return so


def _key(path, so, providers, kwargs) -> tuple:
    return (os.path.realpath(os.fspath(path)),
            tuple((f, str(getattr(so, f))) for f in _KEY_FIELDS),
            tuple(providers) if providers else None,
            tuple(sorted((k, repr(v)) for k, v in kwargs.items())))


def shared_session(path_or_bytes, sess_options=None, providers=None, **kwargs
                   ) -> onnxruntime.InferenceSession:
    """onnxruntime.InferenceSession's signature, returning ONE session per model file and
    options for the whole process, built with the brain's options (brain_options).

    A serialized model passed as bytes is built with the brain's options but not cached:
    nothing here builds one, and a key on its content is not worth computing."""
    so = brain_options(sess_options)
    if providers is not None:
        kwargs["providers"] = providers
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return onnxruntime.InferenceSession(path_or_bytes, sess_options=so, **kwargs)
    key = _key(path_or_bytes, so, providers, {k: v for k, v in kwargs.items()
                                                 if k != "providers"})
    with _lock:
        session = _sessions.get(key)
        if session is None:
            session = onnxruntime.InferenceSession(path_or_bytes, sess_options=so, **kwargs)
            _sessions[key] = session
            logger.info(f"ONNX: built the shared session for {os.path.basename(key[0])} "
                        "(CPU arena and memory patterns off)")
        return session


class _PipecatOnnxruntime:
    """Stands in for the onnxruntime module inside the two pipecat modules in _SITES:
    InferenceSession is shared_session, everything else is the real module's."""

    InferenceSession = staticmethod(shared_session)

    def __getattr__(self, name):
        return getattr(onnxruntime, name)

    def __repr__(self):
        return "<teaport onnx_sessions stand-in for the onnxruntime module>"


_STAND_IN = _PipecatOnnxruntime()


def install() -> dict[str, bool]:
    """Route the pipecat ONNX sites through shared_session. Idempotent; returns
    {module: installed} so a caller (or a test) can see a site that did not take."""
    import importlib

    with _lock:
        for module_name, attr in _SITES:
            if module_name in _installed:
                continue
            module = sys.modules.get(module_name) or importlib.import_module(module_name)
            current = getattr(module, attr, None)
            if current is _STAND_IN:
                _installed[module_name] = True
            elif current is onnxruntime:
                setattr(module, attr, _STAND_IN)
                _installed[module_name] = True
            else:
                # A pipecat that no longer imports onnxruntime under this name: its model
                # is built per session again (correct, just not shared). Say so once.
                logger.warning(f"ONNX: {module_name}.{attr} is not onnxruntime; its "
                               "model will not be shared (pipecat changed?)")
                _installed[module_name] = False
        return dict(_installed)


def shared_sessions() -> int:
    """How many shared sessions exist (tests, diagnostics)."""
    with _lock:
        return len(_sessions)
