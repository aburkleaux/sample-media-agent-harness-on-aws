"""Payload scrubbing for logs (projects-access design Component 7 "Dispatch and plans carry no token").

Dependency-free so `orchestration.dispatch_tool` and `task_agent` can import it without pulling in the
search package. The plan chainer Lambda keeps its own copy of `SECRET_PAYLOAD_KEYS` (separate bundle);
`tests/test_search_binding_wiring.py` keeps the two equal.
"""
from __future__ import annotations

import json
from typing import Any

SECRET_PAYLOAD_KEYS = frozenset({"project_token", "delegation_grant", "user_token", "search_dispatch"})
REDACTED = "<redacted>"


def scrub_payload(payload: dict) -> dict:
    """A copy safe to log: secret keys keep their name, never their value."""
    return {k: (REDACTED if k in SECRET_PAYLOAD_KEYS else v) for k, v in payload.items()}


def payload_summary(payload: Any, max_len: int = 80) -> str:
    """A short, log-safe description of a payload: secrets redacted, long strings cut, lists counted."""
    if not isinstance(payload, dict):
        return type(payload).__name__

    def short(v: Any) -> Any:
        if isinstance(v, str):
            return v if len(v) <= max_len else f"{v[:max_len]}…(+{len(v) - max_len})"
        if isinstance(v, (list, tuple)):
            return f"<{len(v)} items>"
        if isinstance(v, dict):
            return f"<{len(v)} keys>"
        return v

    return json.dumps({k: (v if v == REDACTED else short(v)) for k, v in scrub_payload(payload).items()},
                      default=str, ensure_ascii=False, sort_keys=True)
