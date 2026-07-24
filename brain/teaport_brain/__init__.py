"""teaport voice brain — the Pipecat pipeline, packaged for install.

The modules (gateway_server, gateway_serializer, services, captions,
memory_recall, memory_hygiene, endpointing, tools, persona, tts_text,
transcript_ledger, heard_context, ...) import their siblings through the
``teaport_brain.`` namespace.

The package is the import closure of the appliance entry point, ``gateway_server``
(``teaport-brain`` console script). Kept import-light: the pipeline's heavy deps
load when a module is imported, not from here.
"""

__version__ = "0.0.0"
