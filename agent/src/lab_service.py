"""Lab_Service entrypoint — DEPRECATED.

The Lab_Service capability has been merged into the Main Agent runtime
(``main.py``). This file remains as a compat shim so any leftover CDK or
local script that still references ``lab_service.py`` resolves cleanly.

If invoked as the AgentCore runtime command, this shim logs a deprecation
message and serves the same handler the Lab_Service runtime used — but
the canonical path is now ``main.py`` which:

  * Exposes the 20 Lab CRUD operations as Strands tools.
  * Accepts ``{"action": "..."}`` payloads as a direct-CRUD bypass that
    skips the LLM and routes through ``LabService.handle``.
  * Builds a per-request ``LabService`` bound to the JWT-derived actor.

The CDK construct ``LabServiceRuntime`` is removed in the same change that
introduces this shim. Cf. ``.kiro/specs/main-lab-merge/``.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.warning(
    "lab_service.py is deprecated — Lab capability has been merged into main.py. "
    "The /lab proxy route now forwards to the Main Agent."
)

app = BedrockAgentCoreApp()


def _make_json_safe(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(item) for item in obj]
    return obj


@app.entrypoint
async def handle_request(payload, context):
    """Compat handler — delegates to the in-process LabService factory.

    Kept for the deprecation window. New deployments should not exercise
    this path; the Main Agent runtime is the canonical entrypoint.
    """
    from lab.service import create_lab_service

    try:
        lab_service = create_lab_service(context)
        result = await lab_service.handle(payload)
        return _make_json_safe(result)
    except Exception as exc:
        logger.exception("Lab_Service shim error: %s", exc)
        return {"error": type(exc).__name__, "message": str(exc)}


if __name__ == "__main__":
    app.run()
