"""Project_Token for a dispatched sub-agent (Task Agent / plan node), minted from its delegation grant.

The Main_Agent's Project_Token arrives in a request header and never travels in a dispatch or plan payload
(`test_no_project_token.py`). A sub-agent instead receives only `project_scope: {project_id}` plus the
delegation grant `dispatch_credentials` already sends, and asks `DelegatedTokenIssuerFn` for its own token: the
issuer verifies the grant (kms:Verify), takes the actor from it, re-checks membership, and signs the same
Project_Token. So a sub-agent's graph calls go through the Search_Gateway's Cedar policy exactly like the
Main_Agent's — privacy exactly like search.

`DelegatedTokenRef.current()` has the `ProjectTokenRef` contract the graph tools use: a valid token, re-minted
shortly before `exp`; `AccessEnded` when the actor is no longer a member; `NotEstablished` otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Optional

from projects.ids import is_project_id

from .token_ref import AccessEnded, NotEstablished

logger = logging.getLogger(__name__)

DELEGATED_FN_ENV = "PROJECT_TOKEN_DELEGATED_FN"
REISSUE_BEFORE_EXP_S = 300


def _default_invoke(fn: str, event: dict) -> dict:
    import boto3

    resp = boto3.client("lambda").invoke(FunctionName=fn, InvocationType="RequestResponse",
                                         Payload=json.dumps(event).encode())
    if resp.get("FunctionError"):
        return {"status": "error"}
    return json.loads(resp["Payload"].read() or b"{}")


class DelegatedTokenRef:
    def __init__(self, project_id: str, grant: str, *, fn: Optional[str] = None,
                 invoke: Callable[[str, dict], dict] = _default_invoke, clock: Callable[[], float] = time.time):
        self.project_id, self._grant = project_id, grant
        self._fn = fn if fn is not None else os.environ.get(DELEGATED_FN_ENV, "")
        self._invoke, self._clock = invoke, clock
        self._token: Optional[str] = None
        self._exp = 0.0
        self._lock = asyncio.Lock()

    async def current(self) -> str:
        async with self._lock:
            if self._token and self._exp - self._clock() > REISSUE_BEFORE_EXP_S:
                return self._token
            if not self._fn or not self._grant:
                raise NotEstablished()
            try:
                resp = await asyncio.to_thread(self._invoke, self._fn, {"grant": self._grant, "projectId": self.project_id})
            except Exception as exc:  # noqa: BLE001 — unreachable issuer: not established, retry next call
                logger.warning("delegated project token: issuer unreachable (%s)", type(exc).__name__)
                raise NotEstablished() from None
            status = (resp or {}).get("status")
            if status == "ok" and isinstance(resp.get("token"), str):
                self._token, self._exp = resp["token"], float(resp.get("exp") or 0)
                return self._token
            self._token = None
            if status == "not_found":
                raise AccessEnded()
            raise NotEstablished()


_GRAPH_LINE = ("\n## Knowledge graph\n- The active project has a knowledge graph. Use the `graph_*` tools (start with "
               "`graph_schema` / `graph_find`) for who, where, what and how things connect. Rows marked sample are "
               "illustrative, never facts about the project.\n")


async def subagent_graph_binding(payload: dict, *, caller: str = "task") -> tuple[list, str]:
    """Graph tools + prompt section for a dispatched sub-agent, or ``([], "")``. Never raises.

    Binds only when the dispatch carried `project_scope` (the dispatching turn's graph-enabled Project) and a
    delegation grant — a sub-agent with only a fallback user token cannot mint a Project_Token (TokenIssuerFn is
    proxy-only) and so gets no graph tools.
    """
    try:
        scope = payload.get("project_scope") if isinstance(payload, dict) else None
        pid = scope.get("project_id") if isinstance(scope, dict) else None
        grant = payload.get("delegation_grant") if isinstance(payload, dict) else None
        if not is_project_id(pid) or not isinstance(grant, str) or not grant:
            return [], ""
        from . import skill_text
        from .graph_tools import make_graph_tools

        tools = await make_graph_tools(DelegatedTokenRef(pid, grant), pid, caller=caller)
        if not tools:
            return [], ""
        return tools, _GRAPH_LINE + skill_text.graph_prompt_section(skill_text.load_project_knowledge_graph())
    except Exception:  # noqa: BLE001
        logger.warning("sub-agent graph binding failed; continuing without graph tools", exc_info=True)
        return [], ""
