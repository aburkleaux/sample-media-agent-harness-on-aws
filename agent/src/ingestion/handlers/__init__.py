"""Lambda entrypoints for the ingestion stack (design §2, §7, §8, §16).

P0 ships only :mod:`ingestion.handlers.asset_service` — the single writer of Project_Asset rows that
the proxy and the Main_Agent runtime invoke. The Dispatcher, IngestTick, rebuild, purge, and
Generated_Media handlers land with later waves.
"""
