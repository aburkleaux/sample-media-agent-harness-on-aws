"""Async job tracking — response-parsing helpers.

Historical note
---------------
This module originally hosted :class:`JobDispatchInterceptor`, an
async dispatch wrapper that registered start-tool calls and gated
results-tool calls. The interceptor was wired over
:meth:`MCPManager.dispatch_tool_call`, but Strands' ``Agent``
dispatches MCP tools via :class:`MCPAgentTool.stream` directly — that
chokepoint was unreachable from the LLM-driven tool-call path, so
the interceptor never actually fired in production.

The replacement lives in :mod:`agent.src.jobs.strands_tool_wrap`,
which subclasses :class:`MCPAgentTool` for trio members and performs
the registry write / results gate on the path Strands actually
takes. The helpers below survive because the wrap reuses them — the
``job_id`` extraction logic, the SFN ARN heuristic, and the
synthetic results-response builder are all shape-stable across the
two implementations.

Helpers retained
----------------
* :func:`_extract_job_id` — parse a ``job_id`` out of an MCP-shaped
  response (top-level dict, ``structuredContent``, or JSON-encoded
  text content block). The wrap re-implements the Strands-typed
  variant locally because Strands' :class:`ToolResult` carries a
  different content shape, but the rules are kept structurally
  identical so audiostem / SFN / generic trios behave the same on
  both paths.
* :func:`_resolve_gateway_id_for_job` — apply the SFN execution-ARN
  heuristic at registration time (R7.2). When ``job_id`` matches
  :data:`SFN_EXECUTION_ARN_RE`, override ``gateway_id`` with
  :data:`~agent.src.jobs.SFN_GATEWAY_ID` so the polling worker takes
  the ``DescribeExecution`` direct path; on any mismatch, the trio's
  own ``gateway_id`` is preserved (silent fall-back to the MCP
  path).
* :func:`_build_synthetic_results_response` — build the MCP-shaped
  ``{structuredContent, content, isError}`` payload returned in lieu
  of an underlying ``*_results`` call when the gate fires (R10.2,
  R10.3). The wrap wraps this dict into a
  :class:`~strands.types.tools.ToolResult` for delivery into
  Strands' agent loop.
* :func:`_coerce_block_text` — pull a text payload off either a
  dict-style or attribute-style MCP block. Used by
  :func:`_extract_job_id`.
* :data:`SFN_EXECUTION_ARN_RE` — the strict SFN execution-ARN
  pattern used by the heuristic above. Duplicated in the polling
  worker (``infra/lib/lambdas/polling-worker/handler.py``) — the two
  MUST stay in lock-step.

Requirements (still enforced via the helpers): 3.3, 7.2, 10.2, 10.3
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from . import SFN_GATEWAY_ID

logger = logging.getLogger(__name__)


# Strict SFN execution ARN regex.
#
# Format::
#
#     arn:aws:states:<region>:<account-id>:execution:<state-machine>:<execution-name>
#
# Anchored at both ends so partial matches (e.g. an ARN embedded in a
# longer string, or a state-machine ARN missing the ``execution``
# segment) do NOT trigger the SFN routing — task 7.2's "fall back
# silently to the MCP path on any mismatch" rule.
#
# The canonical copy of this pattern lives in the polling worker
# (``infra/lib/lambdas/polling-worker/handler.py``); this duplicate
# exists because the agent runtime is a separate deployment artifact
# with no shared module surface to the Lambda. The two MUST stay in
# lock-step.
SFN_EXECUTION_ARN_RE = re.compile(
    r"^arn:aws:states:[^:]+:\d+:execution:[^:]+:[^:]+$"
)


def is_malformed_sfn_arn(job_id: Any) -> bool:
    """True for a value that is an SFN execution ARN with the execution NAME missing.

    A healthy ARN is
    ``arn:aws:states:<region>:<acct>:execution:<state-machine>:<execution-name>``.
    When a workflow is submitted with an empty/invalid required input, the gateway can
    return the ARN prefix with no execution name appended (``…:execution:`` or
    ``…:execution:<sm>``) — the Step Functions execution NEVER STARTED. That is a failed
    submission, not a trackable job; registering it creates a phantom that never
    progresses (the recognize_faces_in_proxy incident). Detects the states-ARN prefix +
    the ``:execution:`` segment WITHOUT the strict full shape, so a genuine job id or a
    non-SFN id (e.g. a fal ``request_id``) is never flagged.
    """
    return (
        isinstance(job_id, str)
        and job_id.startswith("arn:aws:states:")
        and ":execution:" in job_id
        and not SFN_EXECUTION_ARN_RE.match(job_id)
    )


def _resolve_gateway_id_for_job(
    *,
    job_id: str,
    trio_gateway_id: str,
) -> str:
    """Pick the persisted ``gateway_id`` for a freshly returned ``job_id``.

    Returns :data:`~agent.src.jobs.SFN_GATEWAY_ID` when ``job_id``
    matches the strict SFN execution ARN shape; otherwise returns the
    trio's own ``gateway_id`` unchanged. Centralised so the Strands
    wrap stays readable and so the SFN detection is unit-testable in
    isolation (R7.2).
    """
    if SFN_EXECUTION_ARN_RE.match(job_id):
        return SFN_GATEWAY_ID
    return trio_gateway_id


def _coerce_block_text(block: Any) -> Optional[str]:
    """Pull a text payload off either a dict-style or attribute-style MCP block.

    The MCP SDK's typed ``TextContent`` block exposes ``.type`` /
    ``.text`` as attributes; some adapters serialize the same data
    into ``{"type": "text", "text": "..."}`` dict entries. Accepting
    both keeps :func:`_extract_job_id` working regardless of which
    side of the wrapper the caller sees.
    """
    if isinstance(block, dict):
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
        return None
    block_type = getattr(block, "type", None)
    block_text = getattr(block, "text", None)
    if block_type == "text" and isinstance(block_text, str):
        return block_text
    return None


def _extract_job_id(response: Any) -> Optional[str]:
    """Recover an MCP-shaped response's ``job_id`` string, or ``None``.

    Strategy mirrors ``invoke_status_tool`` in the polling worker:

    1. If the response is dict-shaped and carries a top-level
       ``job_id`` string, return it directly. This is the shape a
       FastMCP server's tool function literally returns when callers
       short-circuit the MCP wrapping (e.g. tests).
    2. Prefer ``structuredContent`` over text content — newer MCP
       servers (FastMCP 1.x+) emit both, and structured content is
       authoritative.
    3. Fall back to the first ``content`` block whose text body is
       valid JSON containing a ``job_id`` field. This is the wire
       shape every MCP server emits today.

    Returns ``None`` whenever the response shape doesn't carry a
    non-empty string ``job_id`` — the caller treats ``None`` as
    "skip registration, log diagnostic" (R3.3).

    The Strands-typed variant of this helper lives in
    :mod:`agent.src.jobs.strands_tool_wrap` because Strands'
    :class:`ToolResult` carries content blocks under a different
    typed-dict shape; the rules there are kept structurally
    identical so audiostem / SFN / generic trios behave the same.
    """
    if response is None:
        return None

    # fal's queue tools (submit_job) return the id as `request_id`, not `job_id`; without this a
    # detected fal start tool returned None here and the job was never registered -> invisible in
    # the Jobs panel. Both keys are accepted, job_id first.
    _id_keys = ("job_id", "request_id")

    # 1. Top-level dict.
    if isinstance(response, dict):
        for k in _id_keys:
            top_level = response.get(k)
            if isinstance(top_level, str) and top_level:
                return top_level

    # 2. ``structuredContent`` — accept both attribute and dict access
    #    so we work with the raw MCP SDK type and with dict-shaped
    #    adapter outputs.
    structured = None
    if isinstance(response, dict):
        structured = response.get("structuredContent")
    if structured is None:
        structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        for k in _id_keys:
            sc_job_id = structured.get(k)
            if isinstance(sc_job_id, str) and sc_job_id:
                return sc_job_id

    # 3. ``content`` list — find the first text block whose payload is
    #    JSON containing job_id/request_id. Mirrors the gateway_invoker
    #    logic so the parsing rules stay consistent across the agent and
    #    the polling worker.
    content = None
    if isinstance(response, dict):
        content = response.get("content")
    if content is None:
        content = getattr(response, "content", None)
    if isinstance(content, list):
        for block in content:
            text = _coerce_block_text(block)
            if text is None:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                for k in _id_keys:
                    pj = parsed.get(k)
                    if isinstance(pj, str) and pj:
                        return pj

    return None


def _build_synthetic_results_response(
    *,
    job_id: str,
    status: str,
    last_error: Optional[str] = None,
) -> dict:
    """Build the MCP-shaped tool response used by the results-tool gate.

    The Strands wrap returns this dict — wrapped in a
    :class:`~strands.types.tools.ToolResult` — in lieu of calling the
    underlying ``*_results`` tool when the recorded job status is
    anything other than ``SUCCEEDED`` (R10.2, R10.3).

    Shape mirrors what a FastMCP server would emit for a structured
    result: a ``structuredContent`` dict that newer MCP clients prefer
    when present, plus a JSON-encoded text content block as the
    universal fallback for clients that only consume the legacy
    ``content`` array. ``isError`` is set to ``False`` because the
    short-circuit is *not* an error from the agent's perspective — the
    job is in a non-success state and the gate's job is to communicate
    that status, not to fail the tool call.

    The model can read either slot to recover the same payload, so the
    duplication is deliberate: it makes the gate robust to whichever
    parsing path the downstream agent runtime takes.
    """
    payload: dict[str, Any] = {"job_id": job_id, "status": status}
    if last_error is not None:
        payload["lastError"] = last_error
    return {
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": False,
    }


__all__ = [
    "SFN_EXECUTION_ARN_RE",
    "_build_synthetic_results_response",
    "_coerce_block_text",
    "_extract_job_id",
    "_resolve_gateway_id_for_job",
    "is_malformed_sfn_arn",
]
