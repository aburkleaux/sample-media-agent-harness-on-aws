"""Search_Tool error contract (projects-access design Component 7, Req 4.13, 4.17, 7.2–7.4, 7.6, 7.13).

Every failure the Actor can see names a real reason. No category or message mentions token expiry:
an expired token is an internal state the runtime recovers from, never something to explain.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

PROJECT_ACCESS_ENDED = "project_access_ended"
PROJECT_ACCESS_UNAVAILABLE = "project_access_unavailable"
POLICY_DENIED = "policy_denied"
TIMEOUT = "timeout"
SEARCH_FAILED = "search failed"
GATEWAY_UNREACHABLE = "gateway unreachable"
OTHER = "other"
SEARCH_DISPATCH_FAILED = "search_dispatch_failed"
SEARCH_UNAVAILABLE = "search_unavailable"
# The Project's indexing is OFF: its files are stored, not analyzed, so there is nothing to search.
INDEXING_OFF = "indexing_off"
INDEXING_OFF_TEXT = (
    "this project isn't searchable because indexing is off; its files are stored and can be found by name "
    "(project_status with name), and the project owner can turn indexing on in the project's settings"
)

ACCESS_ENDED_TEXT = "you no longer have access to this project"
ACCESS_NOT_ESTABLISHED_TEXT = "project access could not be established — send the message again"
POLICY_DENIED_TEXT = (
    "the search privacy policy denied this call; only the active project "
    "(collection_id from the turn context) can be searched"
)

# AgentCore Gateway reports a Cedar ENFORCE denial as JSON-RPC -32002 with this text
# (observed on a live CUSTOM_JWT gateway, validation/spike_v10_gateway.py).
_CEDAR_DENY_RE = re.compile(r"Tool Execution Denied|policy enforcement", re.I)
_AUTH_REJECT_RE = re.compile(r"Invalid Bearer token|insufficient_scope|invalid_token|Unauthorized", re.I)
_MAX_MESSAGE = 500


def compose_reason(message: Any) -> str:
    """Redact-and-truncate stand-in for media-harness-project-ingestion's failure-reason composer.

    Strips anything bearer-token shaped, then truncates. Replaced by the shared composer when that
    spec ships; the contract (a short, credential-free string) is the same.
    """
    text = str(message or "").strip()
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "<redacted-jwt>", text)
    return text[:_MAX_MESSAGE]


@dataclass(frozen=True)
class SearchOutcome:
    """What one gateway round produced, before classification.

    phase: "initialize" or "call" (where the failure, if any, happened).
    kind:  "ok" | "http" | "rpc_error" | "tool_error" | "transport" | "timeout".
    """
    phase: str
    kind: str
    status: Optional[int] = None
    message: str = ""
    result: Any = None


def classify(outcome: SearchOutcome) -> Optional[tuple[str, str]]:
    """Map an outcome to (category, message), or None when the call succeeded."""
    if outcome.kind == "ok":
        code = _m2c_error_code(outcome.result)
        if code == "SEARCH_TIMEOUT":
            return TIMEOUT, compose_reason(_m2c_message(outcome.result) or "search timed out")
        if code == "SEARCH_FAILED":
            return SEARCH_FAILED, compose_reason(_m2c_message(outcome.result) or "search failed")
        return None
    if outcome.kind == "timeout":
        return TIMEOUT, "the search did not answer within the time limit"
    if outcome.phase == "initialize":
        if outcome.kind == "http" and outcome.status in (401, 403):
            return PROJECT_ACCESS_UNAVAILABLE, ACCESS_NOT_ESTABLISHED_TEXT
        # AgentCore's CUSTOM_JWT authorizer answers a rejected token with an HTTP 401/403 whose body
        # is a JSON-RPC error, which the MCP client surfaces as an rpc error, not an HTTP status:
        # -32001 "Invalid Bearer token" (unknown kid) or -32002 "insufficient_scope …" (expired,
        # wrong aud/iss, foreign key). Observed live in spike_v10_gateway.py.
        if outcome.kind == "rpc_error" and (outcome.status in (-32001, -32002) or _AUTH_REJECT_RE.search(outcome.message or "")):
            return PROJECT_ACCESS_UNAVAILABLE, ACCESS_NOT_ESTABLISHED_TEXT
        if outcome.kind == "transport" or (outcome.kind == "http" and (outcome.status or 0) >= 500):
            return GATEWAY_UNREACHABLE, compose_reason(outcome.message or "the search gateway could not be reached")
        return OTHER, compose_reason(outcome.message)
    # call phase, after a successful initialize
    if outcome.kind in ("rpc_error", "tool_error") and _CEDAR_DENY_RE.search(outcome.message or ""):
        return POLICY_DENIED, POLICY_DENIED_TEXT
    if outcome.kind == "http" and outcome.status in (401, 403):
        return POLICY_DENIED, POLICY_DENIED_TEXT
    if outcome.kind == "transport" or (outcome.kind == "http" and (outcome.status or 0) >= 500):
        return GATEWAY_UNREACHABLE, compose_reason(outcome.message or "the search gateway could not be reached")
    return OTHER, compose_reason(outcome.message)


def error_result(category: str, message: str) -> str:
    return json.dumps({"error": {"category": category, "message": compose_reason(message)}})


def _payload(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return None
    return result


def _m2c_error_code(result: Any) -> Optional[str]:
    p = _payload(result)
    return p.get("errorCode") if isinstance(p, dict) else None


def _m2c_message(result: Any) -> str:
    p = _payload(result)
    return str(p.get("message") or p.get("errorMessage") or "") if isinstance(p, dict) else ""
