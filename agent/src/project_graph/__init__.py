"""Per-Project knowledge graph: Lance tables under ``projects/<pid>/graph/``, queried with lance-graph.

Runs ONLY in the x86_64 graph Lambda image (``agent/graph.Dockerfile``): lance-graph ships a single
``manylinux_2_39_x86_64`` wheel, so ``pyarrow`` / ``lance`` / ``lance_graph`` are imported lazily inside the
functions that need them. The pure modules (``schema``, ``ids``, ``starter``, ``demo``, ``layout``,
``payload``) import nothing heavy, which keeps this package harmless when the agent-runtime and ingestion
zip packagers sweep it up.
"""
