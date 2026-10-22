"""Recover tool-ERROR results that strict MCP result validation rejects.

``mcp`` 2.x validates every server result against the negotiated protocol revision's schema
(``ClientSession.send_request`` → ``_methods.validate_server_result``). On the revision our
gateway sessions negotiate, a ``tools/call`` result must carry ``resultType``; the gateway's (or
the target server's) ERROR results arrive without it —
``{"content": [{"type": "text", ...}], "isError": true}`` — so validation raises
``ValidationError: 2 validation errors for union[CallToolResult,InputRequiredResult] …
resultType Field required``. Strands turns that into "Tool execution failed: <pydantic text>",
which HIDES the tool's real error message from the model (staging, 2026-10-01: ~60 fal
``run_model``/``get_model_schema`` failures surfaced only as this parse error, so the model could
not see or correct the actual cause).

pydantic keeps the ORIGINAL payload on the exception (``e.errors()[i]["input"]``). When that
payload is a tool-error result, rebuild it as a plain ``CallToolResult`` and return it, so the
caller receives an ordinary ``isError`` result with the server's full text. Every other
validation failure (a malformed SUCCESS result, an unexpected shape) re-raises unchanged.

Installed by patching the name Strands calls (``strands.tools.mcp.mcp_client.compat_call_tool``);
idempotent. Applies to every gateway tool in the process (main and task agents).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_MARK = "_mediaworks_error_result_recovery"


def recover_error_result(exc: Exception) -> Any | None:
    """Return a ``CallToolResult`` rebuilt from a rejected tool-error payload, else ``None``."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return None
    try:
        items = errors()
    except Exception:  # noqa: BLE001
        return None
    for item in items:
        payload = item.get("input") if isinstance(item, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("isError") is True
            and isinstance(payload.get("content"), list)
        ):
            from mcp.types import CallToolResult

            try:
                return CallToolResult.model_validate(
                    {"content": payload["content"], "isError": True}
                )
            except Exception:  # noqa: BLE001 — unrecoverable content: let the original error stand
                return None
    return None


def install() -> bool:
    """Wrap Strands' ``compat_call_tool`` so tool-error results survive strict validation."""
    try:
        from pydantic import ValidationError
        from strands.tools.mcp import mcp_client as _mc
    except Exception as e:  # noqa: BLE001 — never break import over a compat shim
        logger.warning("mcp_result_compat: not installed: %s", e)
        return False

    original = getattr(_mc, "compat_call_tool", None)
    if original is None or getattr(original, _PATCH_MARK, False):
        return original is not None

    async def compat_call_tool(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except ValidationError as exc:
            recovered = recover_error_result(exc)
            if recovered is None:
                raise
            text = " ".join(
                getattr(c, "text", "") for c in (recovered.content or []) if getattr(c, "text", None)
            )
            logger.warning(
                "mcp_result_compat: recovered a tool error result rejected by strict validation: %s",
                text[:300],
            )
            return recovered

    setattr(compat_call_tool, _PATCH_MARK, True)
    _mc.compat_call_tool = compat_call_tool
    return True


install()
