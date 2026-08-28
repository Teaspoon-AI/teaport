"""teaport voice brain — the Pipecat pipeline, packaged for install.

The modules (gateway_server, gateway_serializer, services, captions,
memory_recall, memory_hygiene, endpointing, tools, persona, tts_text,
transcript_ledger, heard_context, ...) import their siblings through the
``teaport_brain.`` namespace.

The package is the import closure of the appliance entry point, ``gateway_server``
(``teaport-brain`` console script). Kept import-light: the pipeline's heavy deps
load when a module is imported, not from here.
"""

import os

# Cache-only HF hub. Nothing in the brain's import closure pulls huggingface_hub in
# today — it is not even installed, on this box or the appliance (checked
# 2026-08-28), and the Silero VAD + smart-turn ONNX models ship inside the pipecat
# wheel rather than being fetched. This is kept anyway, because it costs one line
# and the day something DOES drag the hub in, a pipeline build that quietly reaches
# for the network is the failure it prevents.
#
# It lives HERE rather than atop each entry point because the value is in being set
# before pipecat is imported, and Python runs a package's __init__ to completion
# before any submodule body. The three servers each carried their own copy above
# their imports, which bought nothing extra and cost 52 E402 lint suppressions
# to silence the very lint their own preamble caused.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

__version__ = "0.0.0"
