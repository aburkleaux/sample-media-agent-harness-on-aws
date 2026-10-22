"""Thin MCP adapter over the six OMS Core tools the Persistence_Tool needs.

This module is the *only* seam through which the persistence pipeline reaches
OMS Core. It reuses the harness's existing gateway machinery: it resolves the
OMS Core `GatewayConnection` from the shared `MCPManager` and invokes each OMS
MCP tool through that connection's `MCPClient.call_tool_sync`, decoding the JSON
content block exactly as `GatewayConnection.search_tools` already does. There is
**no direct HTTP transport** to OMS Core here — every call rides the same MCP
gateway the manager owns.

The adapter exposes typed methods for the six tools the resolver, validator
gate, and save/activate driver depend on (`list_integrations`, `get_integration`,
`get_action_schema`, `validate_template`, `save_template`, `activate_template`),
so callers depend on `dict` / `list[dict]` shapes rather than raw MCP plumbing.

The OMS endpoint is never hardcoded: it is resolved dynamically from the gateway
target configuration (`GatewayConnection.url`) or, failing that, from SSM
`/oms/core/api-url`. The target is OMS Core in account ``100000000006`` /
``us-east-1`` (R11.4). Any gateway / MCP transport failure is wrapped as
`OMSUnavailableError` so the pipeline leaves OMS Core untouched (R12.2).

`_call` is additionally the **single chokepoint** for OMS *structured error
envelopes*: a decoded payload shaped ``{"error": true, "kind": ..., "message":
..., "tool": ...}`` means the transport succeeded but OMS Core refused or failed
the tool — for example the hosted endpoint's read-only gate refusing a mutating
`save_template` because it runs with ``OMS_MCP_READ_ONLY=true``. Such a payload
decodes to an ordinary ``dict``, so it used to flow back as if it were a normal
response: a silent false success in which `persist_workflow` reported
``saved_draft`` while nothing was written to OMS Core. All six tools now funnel
through `_raise_if_structured_error`, which raises the typed `OMSToolError`, so a
refused or failed write can never be reported as a success.

The connection (or the owning `MCPManager`) is injected via the constructor so
tests can substitute a fake `GatewayConnection` that records calls.

Requirements: 11.1, 11.2, 11.3, 11.4
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

import boto3

from .models import OMSToolError, OMSUnavailableError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from mcp_manager import GatewayConnection, MCPManager

logger = logging.getLogger(__name__)

#: Sentinel distinguishing "no decodable content block present" (fall back to
#: ``structuredContent``) from a legitimately decoded ``None`` payload.
_MISSING: Any = object()


def _blocks_to_text(content: Any) -> str:
    """Join the ``text`` of every content block (strands dict or MCP object).

    Used only to surface a human-readable detail when a strands ``ToolResult``
    reports ``status == "error"``.
    """
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Literal namespace separator inside a gateway tool name (``<target>___<bare>``).
#: OMS MCP tools are registered on the gateway under a namespaced name; the six
#: bare names below are matched against the suffix after this separator.
_NAMESPACE_SEPARATOR: str = "___"

#: OMS Core target account / region (R11.4). Used when a default SSM client must
#: be constructed to resolve the endpoint; never a hardcoded OMS endpoint.
_OMS_ACCOUNT_ID: str = "100000000006"
_OMS_REGION: str = "us-east-1"

#: SSM parameter holding the OMS Core API URL — the dynamic endpoint fallback
#: when the gateway target config carries no URL (R11.2, R11.3).
_SSM_OMS_API_URL: str = "/oms/core/api-url"

#: Env var naming the registered OMS Core gateway id in the GatewayRegistry.
_ENV_OMS_GATEWAY_ID: str = "OMS_CORE_GATEWAY_ID"
_DEFAULT_OMS_GATEWAY_ID: str = "oms-core"

# The six OMS Core MCP tools this adapter wraps (bare names, R11.1).
_TOOL_LIST_INTEGRATIONS: str = "list_integrations"
_TOOL_GET_INTEGRATION: str = "get_integration"
_TOOL_GET_ACTION_SCHEMA: str = "get_action_schema"
_TOOL_VALIDATE_TEMPLATE: str = "validate_template"
_TOOL_SAVE_TEMPLATE: str = "save_template"
_TOOL_ACTIVATE_TEMPLATE: str = "activate_template"

#: The SINGULAR key OMS Core sets in a structured error envelope. Only this key
#: marks a payload as an error — a plural ``errors: []`` list is a *normal*
#: ``validate_template`` field and must never be mistaken for one.
_STRUCTURED_ERROR_KEY: str = "error"

#: Envelope keys carrying the error discriminator and human-readable detail.
_STRUCTURED_ERROR_KIND_KEY: str = "kind"
_STRUCTURED_ERROR_MESSAGE_KEY: str = "message"

#: The exact, closed set of OMS MCP tools the adapter is allowed to invoke.
OMS_MCP_TOOLS: tuple[str, ...] = (
    _TOOL_LIST_INTEGRATIONS,
    _TOOL_GET_INTEGRATION,
    _TOOL_GET_ACTION_SCHEMA,
    _TOOL_VALIDATE_TEMPLATE,
    _TOOL_SAVE_TEMPLATE,
    _TOOL_ACTIVATE_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OMSMcpClient:
    """Typed, MCP-only client over the six OMS Core tools (R11).

    Constructor-injected with the OMS Core ``GatewayConnection`` (real or fake)
    so the transport boundary is substitutable in tests. Use
    :meth:`from_manager` to resolve the connection from a shared ``MCPManager``.
    """

    def __init__(
        self,
        connection: "GatewayConnection",
        *,
        ssm_client: Any = None,
        region: str = _OMS_REGION,
        account_id: str = _OMS_ACCOUNT_ID,
        ssm_param: str = _SSM_OMS_API_URL,
    ) -> None:
        self._connection = connection
        self._ssm_client = ssm_client
        self._region = region
        self._account_id = account_id
        self._ssm_param = ssm_param
        # Cache of bare-tool-name -> registered (namespaced) tool name so we
        # resolve the gateway's tool namespace once per tool.
        self._tool_name_cache: dict[str, str] = {}

    # -- construction --------------------------------------------------------

    @classmethod
    def from_manager(
        cls,
        manager: "MCPManager",
        *,
        gateway_id: Optional[str] = None,
        ssm_client: Any = None,
        region: str = _OMS_REGION,
    ) -> "OMSMcpClient":
        """Resolve the OMS Core gateway from ``manager`` and build the adapter.

        Reuses ``MCPManager`` rather than opening any new transport (R11.1). The
        gateway id defaults to ``$OMS_CORE_GATEWAY_ID`` (or ``"oms-core"``).

        Raises:
            OMSUnavailableError: when no gateway is registered under the id.
        """
        gw_id = gateway_id or os.environ.get(_ENV_OMS_GATEWAY_ID, _DEFAULT_OMS_GATEWAY_ID)
        connection = manager.get_gateway(gw_id)
        if connection is None:
            raise OMSUnavailableError(
                f"OMS Core gateway {gw_id!r} is not registered", stage=None
            )
        return cls(connection, ssm_client=ssm_client, region=region)

    # -- endpoint resolution (R11.2, R11.3) ----------------------------------

    def resolve_endpoint(self) -> str:
        """Resolve the OMS Core endpoint dynamically — never hardcoded.

        Prefers the gateway target configuration (``GatewayConnection.url``);
        falls back to SSM ``/oms/core/api-url`` in account ``100000000006`` /
        ``us-east-1``. Raises :class:`OMSUnavailableError` when neither yields a
        value.
        """
        url = getattr(self._connection, "url", None)
        if isinstance(url, str) and url:
            return url
        try:
            client = self._ssm_client or boto3.client("ssm", region_name=self._region)
            resp = client.get_parameter(Name=self._ssm_param)
            value = resp["Parameter"]["Value"]
        except Exception as exc:  # noqa: BLE001 — surface as typed transport failure
            raise OMSUnavailableError(
                f"could not resolve OMS Core endpoint from gateway config or SSM "
                f"{self._ssm_param!r}: {exc}",
                stage=None,
            ) from exc
        if not value:
            raise OMSUnavailableError(
                f"OMS Core endpoint SSM parameter {self._ssm_param!r} is empty",
                stage=None,
            )
        return value

    # -- the six OMS MCP tools (R11.1) ---------------------------------------

    def list_integrations(self) -> list[dict]:
        """List the registered OMS Core services (integrations)."""
        result = self._call(_TOOL_LIST_INTEGRATIONS, {}, stage="resolve")
        return _as_list(result)

    def get_integration(self, integration_id: str) -> dict:
        """Get a single registered service by id."""
        result = self._call(
            _TOOL_GET_INTEGRATION, {"integration_id": integration_id}, stage="resolve"
        )
        return _as_dict(result)

    def get_action_schema(self, integration_id: str, action: str) -> dict:
        """Get an action's input config / output schema (and published tool name)."""
        result = self._call(
            _TOOL_GET_ACTION_SCHEMA,
            {"integration_id": integration_id, "action": action},
            stage="resolve",
        )
        return _as_dict(result)

    def validate_template(self, template: dict) -> dict:
        """Validate a workflow template (cycles, references, typed mappings)."""
        result = self._call(
            _TOOL_VALIDATE_TEMPLATE, {"template": template}, stage="validate"
        )
        return _as_dict(result)

    def save_template(self, template: dict, update: bool) -> dict:
        """Save a workflow template as an inert draft (create or update)."""
        result = self._call(
            _TOOL_SAVE_TEMPLATE,
            {"template": template, "update": update},
            stage="save",
        )
        return _as_dict(result)

    def activate_template(self, template_id: str) -> dict:
        """Activate a previously saved template (only behind the confirm guard)."""
        result = self._call(
            _TOOL_ACTIVATE_TEMPLATE, {"template_id": template_id}, stage="activate"
        )
        return _as_dict(result)

    # -- internals -----------------------------------------------------------

    def _call(self, bare_tool: str, args: dict, *, stage: str) -> Any:
        """Invoke one OMS MCP tool via the connection's ``MCPClient.call_tool_sync``.

        Resolves the namespaced tool name on the gateway, calls the tool, and
        decodes the JSON content block (same decode path as
        ``GatewayConnection.search_tools``). Any transport / protocol failure is
        wrapped as :class:`OMSUnavailableError` tagged with ``stage`` so nothing
        is written to OMS Core (R12.2).
        """
        client = getattr(self._connection, "_client", None)
        if client is None:
            raise OMSUnavailableError(
                f"OMS Core gateway has no active MCP client (tool {bare_tool!r})",
                stage=stage,
            )
        tool_name = self._resolve_tool_name(bare_tool)
        # Strands' ``MCPClient.call_tool_sync`` signature is
        # ``(tool_use_id, name, arguments, ...)`` — ``tool_use_id`` is the
        # REQUIRED first positional arg (verified against the installed strands
        # source). Passing ``(tool_name, args)`` positionally — as this adapter
        # previously did — silently bound ``tool_use_id=tool_name`` and
        # ``name=args``, which is the root cause of the decode failure. Pass a
        # synthetic, stable tool-use id so the call binds correctly.
        tool_use_id = f"oms-{bare_tool}"
        try:
            result = client.call_tool_sync(tool_use_id, tool_name, args)
        except Exception as exc:  # noqa: BLE001 — MCP transport failure
            raise OMSUnavailableError(
                f"OMS Core MCP call {bare_tool!r} failed: {exc}", stage=stage
            ) from exc
        payload = self._decode(result, bare_tool, stage)
        # SINGLE CHOKEPOINT: a decoded payload may itself be an OMS structured
        # error envelope (transport fine, tool refused). All six tools funnel
        # through here, so no caller can mistake a refusal for a success.
        _raise_if_structured_error(payload, bare_tool, stage)
        return payload

    def _resolve_tool_name(self, bare_tool: str) -> str:
        """Resolve the gateway's namespaced name for ``bare_tool``.

        Gateway targets register tools under ``<target>___<bare>``. Match the
        registered tool whose bare suffix equals ``bare_tool``; fall back to the
        bare name when the connection exposes no matching metadata.
        """
        cached = self._tool_name_cache.get(bare_tool)
        if cached is not None:
            return cached
        resolved = bare_tool
        try:
            metadata = self._connection.list_tools()
        except Exception:  # noqa: BLE001 — resolution is best-effort
            metadata = []
        for meta in metadata or []:
            name = meta.get("name", "") if isinstance(meta, dict) else ""
            if name == bare_tool or name.endswith(
                f"{_NAMESPACE_SEPARATOR}{bare_tool}"
            ):
                resolved = name
                break
        self._tool_name_cache[bare_tool] = resolved
        return resolved

    def _decode(self, result: Any, bare_tool: str, stage: str) -> Any:
        """Decode the tool payload from whatever ``call_tool_sync`` returns.

        Strands' ``MCPClient.call_tool_sync`` does NOT return the raw
        ``mcp.types.CallToolResult``. It returns a strands ``MCPToolResult``,
        which is a ``ToolResult`` ``TypedDict`` (i.e. a plain ``dict``) shaped:

            {
              "status": "success" | "error",
              "toolUseId": "<id>",
              "content": [ {"text": "<json string>"} | {"json": <obj>} | ... ],
              "structuredContent": {...},   # optional, present when the MCP
                                            # tool returned structuredContent
              "metadata": {...},            # optional
            }

        Key facts (verified against the installed strands source):
          * ``getattr(dict, "content", None)`` returns ``None`` for a dict — a
            dict exposes ``content`` as a KEY, not an attribute. The old code
            read it as an attribute, so it always saw "no content block".
          * strands maps MCP ``TextContent`` to ``{"text": ...}`` blocks and
            lifts MCP ``structuredContent`` to a TOP-LEVEL key (it is not a
            content block). Some tools/versions also emit ``{"json": <obj>}``
            blocks (``ToolResultContent`` supports a ``json`` field).

        This decoder therefore handles all of:
          1. strands ``MCPToolResult``/``ToolResult`` dicts (the real runtime
             shape) — preferring a ``{"json": ...}`` block, then a
             ``{"text": ...}`` JSON string, then top-level ``structuredContent``.
          2. raw ``mcp.types.CallToolResult`` objects (back-compat / any caller
             that hands us the unmapped result) — via ``.content`` TextContent
             (``.text``) and/or ``.structuredContent``.

        Returns the parsed JSON payload (``dict`` or ``list``). Raises
        :class:`OMSUnavailableError` only when NO decodable payload exists in
        any of these forms — a protocol-level failure that must not be treated
        as a successful (empty) OMS response.
        """
        # --- strands MCPToolResult / ToolResult dict (real runtime shape) ---
        if isinstance(result, dict):
            if result.get("status") == "error":
                raise OMSUnavailableError(
                    f"OMS Core MCP tool {bare_tool!r} returned an error status: "
                    f"{_blocks_to_text(result.get('content')) or '<no detail>'}",
                    stage=stage,
                )
            payload = self._payload_from_blocks(
                result.get("content"), bare_tool, stage
            )
            if payload is not _MISSING:
                return payload
            structured = result.get("structuredContent")
            if isinstance(structured, (dict, list)):
                return structured
            raise OMSUnavailableError(
                f"OMS Core MCP tool {bare_tool!r} returned no decodable content",
                stage=stage,
            )

        # --- raw mcp.types.CallToolResult object (back-compat) ---
        payload = self._payload_from_blocks(
            getattr(result, "content", None), bare_tool, stage
        )
        if payload is not _MISSING:
            return payload
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, (dict, list)):
            return structured
        raise OMSUnavailableError(
            f"OMS Core MCP tool {bare_tool!r} returned no content block",
            stage=stage,
        )

    def _payload_from_blocks(self, content: Any, bare_tool: str, stage: str) -> Any:
        """Extract a JSON payload from a list of MCP/strands content blocks.

        Blocks may be strands dicts (``{"json": <obj>}`` or ``{"text": <str>}``)
        or raw MCP content objects (``TextContent`` with a ``.text`` attribute).
        Prefers a ``json`` block (already-parsed, no ``json.loads`` needed);
        otherwise parses the first ``text`` block as JSON. Returns
        :data:`_MISSING` when no ``json``/``text`` block is present so the caller
        can fall back to ``structuredContent``. Raises
        :class:`OMSUnavailableError` when a ``text`` block exists but is not
        valid JSON.
        """
        if not content:
            return _MISSING
        text_value: Any = _MISSING
        for block in content:
            if isinstance(block, dict):
                if block.get("json") is not None:
                    return block["json"]
                if text_value is _MISSING and isinstance(block.get("text"), str):
                    text_value = block["text"]
            else:
                text_attr = getattr(block, "text", None)
                if text_value is _MISSING and isinstance(text_attr, str):
                    text_value = text_attr
        if text_value is _MISSING:
            return _MISSING
        try:
            return json.loads(text_value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OMSUnavailableError(
                f"OMS Core MCP tool {bare_tool!r} returned undecodable "
                f"content: {exc}",
                stage=stage,
            ) from exc


# ---------------------------------------------------------------------------
# Structured OMS error envelope detection (the silent-false-success guard)
# ---------------------------------------------------------------------------


def is_structured_oms_error(payload: Any) -> bool:
    """True when ``payload`` is an OMS Core structured *error* envelope.

    OMS Core answers a refused / failed tool call with a normal-looking JSON
    object::

        {"error": true, "kind": "read_only", "message": "...", "tool": "save_template"}

    The detection rule is deliberately narrow, so no legitimate success payload
    is ever misread as a failure:

    - the payload must be a ``dict``; and
    - its SINGULAR ``error`` key must be either the boolean ``True`` or a
      non-blank string.

    Explicitly NOT errors: ``{"error": false}``, a payload with no ``error`` key,
    and — crucially — a payload carrying only a plural ``errors`` list, which is
    ``validate_template``'s ordinary shape (``{"valid": true, "errors": []}``).
    """
    if not isinstance(payload, dict):
        return False
    flag = payload.get(_STRUCTURED_ERROR_KEY)
    if flag is True:
        return True
    return isinstance(flag, str) and bool(flag.strip())


def structured_oms_error_parts(payload: dict, bare_tool: str) -> tuple[Optional[str], str]:
    """Extract ``(kind, message)`` from a structured OMS error envelope.

    Falls back to the ``error`` value itself when it is the string carrying the
    detail, and finally to an explicit "no detail" message so the raised error is
    never emptily uninformative.
    """
    kind = payload.get(_STRUCTURED_ERROR_KIND_KEY)
    kind = kind if isinstance(kind, str) and kind else None

    message = payload.get(_STRUCTURED_ERROR_MESSAGE_KEY)
    if not (isinstance(message, str) and message.strip()):
        flag = payload.get(_STRUCTURED_ERROR_KEY)
        message = flag if isinstance(flag, str) and flag.strip() else ""
    if not message:
        message = (
            f"OMS Core returned an error payload for tool {bare_tool!r} with no "
            f"message detail"
        )
    return kind, message


def _raise_if_structured_error(payload: Any, bare_tool: str, stage: str) -> None:
    """Raise :class:`OMSToolError` when ``payload`` is a structured error envelope.

    This is the chokepoint that turns a silent false success into a loud, typed
    failure. It runs for every one of the six tools.
    """
    if not is_structured_oms_error(payload):
        return
    kind, message = structured_oms_error_parts(payload, bare_tool)
    logger.warning(
        "OMS Core tool %r returned a structured error (kind=%s) at stage %s: %s",
        bare_tool,
        kind,
        stage,
        message,
    )
    raise OMSToolError(tool=bare_tool, kind=kind, message=message, stage=stage)


# ---------------------------------------------------------------------------
# Decode coercion helpers
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[dict]:
    """Coerce a decoded payload to ``list[dict]`` for list-returning tools."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Tolerate a single envelope object (e.g. {"integrations": [...]}).
        for key in ("integrations", "items", "results", "data"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
    return []


def _as_dict(value: Any) -> dict:
    """Coerce a decoded payload to ``dict`` for object-returning tools."""
    if isinstance(value, dict):
        return value
    return {}


__all__ = [
    "OMSMcpClient",
    "OMS_MCP_TOOLS",
    "is_structured_oms_error",
    "structured_oms_error_parts",
]
