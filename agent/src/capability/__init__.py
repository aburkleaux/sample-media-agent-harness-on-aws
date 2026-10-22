"""Capability control plane — deterministic inventory without AgentCore Runtime.

This package is the Runtime-independent half of the harness (requirement R1).
Everything here is importable **without** Strands, a model client, MCP
sessions, or skill packages: it reads canonical state from DynamoDB/S3, the
harness-shared discovery projection from AWS Agent Registry, and the shared
MCP hierarchy from the Gateway control plane.

Module map::

    dto.py          normalized CapabilitySummary / CategoryPage contract
    naming.py       deterministic Registry record identity (name + version)
    config.py       env-scoped Registry/feature-flag config, fail-closed
    registry.py     boto3 clients for agent-registry / agent-registry-control
    descriptors.py  Registry record descriptor builders + prohibition rules
    sources.py      per-source readers (AssetIndex / Registry / Gateway / builtin)
    aggregate.py    parallel fan-out, merge, dedupe, partial-error reporting
    projector.py    DynamoDB-stream projection worker (Lambda entrypoint)

Two consumers share this package verbatim so descriptor logic cannot drift:

* the always-on Capability API container (``agent/src/capability_api``), and
* the projection Lambda, whose deployment asset is ``agent/src`` with handler
  ``capability.projector.handler``.

The deployed namespace is ``agent-registry`` / ``agent-registry-control``
throughout (R9). The deprecated Registry surface under ``bedrock-agentcore``
is never used; ``bedrock-agentcore`` identity/runtime resources keep their
existing names.
"""
