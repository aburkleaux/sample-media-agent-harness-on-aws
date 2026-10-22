"""media-harness-project-ingestion product package (design "From spike to product modules").

One package serves every Python consumer of ingestion: the Asset_Service Lambda, the pipeline Lambdas,
the Generated_Media Lambda, and the local development driver. The P0 slice shipped here is the
asset read/URL path — the parts a deployed env needs to resolve assets and register uploads/S3
locations before the full pipeline exists:

- :mod:`ingestion.names`   — ``sanitize`` (object-key file name) and ``derive_display_name`` (pure).
- :mod:`ingestion.store`   — the Project_Asset row reader/writer over the projects table.
- :mod:`ingestion.registrar` — the S3_Source_Registrar checks for ``register_s3_location`` (design §6).
- :mod:`ingestion.handlers.asset_service` — the Asset_Service Lambda (design §2).

The deeper pipeline modules (reducer, rebuild, purge, generated) land in later waves. D1 adds the pure
document path and the M2C/index I/O it needs:

- :mod:`ingestion.m2c` — stateless JSON-RPC client for the M2C gateway (typed ``Transient``/``ToolError``/
  ``Validation``); :mod:`ingestion.catalog` — required tools per kind.
- :mod:`ingestion.workflows` — submit arguments per step, ``is_no_audio_stream``.
- :mod:`ingestion.outputs` — read a workflow output by label (size cap, reuse check).
- :mod:`ingestion.adapters` → :mod:`ingestion.documents` → :mod:`ingestion.contract` → :mod:`ingestion.records`
  — M2C outputs to validated, serialized Index_Documents.
- :mod:`ingestion.chunking`, :mod:`ingestion.indexer`, :mod:`ingestion.index_errors`, :mod:`ingestion.collection`
  — chunked ``index_content`` submission as one pure state machine (``IndexRun``).
- :mod:`ingestion.reasons` — Req 7.3 failure-reason composition with credential redaction.
"""
