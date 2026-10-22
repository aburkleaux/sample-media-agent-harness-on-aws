"""Knowledge-graph tools for agents — the graph extension of project search, reached exactly like search.

One `ProjectGraphTool` per graph tool the Search_Gateway lists under the `project-graph___` target (Cedar filters
`tools/list` to what the caller's Project_Token may call). Privacy is search's: the bearer is the Project_Token,
and the gateway's ENFORCE-mode Cedar policy permits a graph call only when its `project_id` equals the token's
`project_id` claim. On top of that the harness — never the model — supplies `project_id`: it is removed from the
model-visible schema and injected from the turn's active Project, so a model cannot even ask for another Project.

Used by the Main_Agent (token from the proxy's ProjectToken header) and by the Task Agent / plan nodes (token
minted from the delegation grant, `delegated_token.py`); both pass any object with `async current() -> str`.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Optional

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from . import errors
from .client import SearchGatewayClient
from .token_ref import AccessEnded, NotEstablished

logger = logging.getLogger(__name__)

GRAPH_TARGET = "project-graph"
GRAPH_PREFIX = f"{GRAPH_TARGET}___"
GRAPH_POLICY_DENIED_TEXT = (
    "the project privacy policy denied this call; only the active project's knowledge graph can be queried"
)
_SPEC_TTL_S = 600
_spec_cache: dict[str, tuple[float, list[dict]]] = {}


def _model_spec(tool: Any) -> Optional[dict]:
    """The model-visible spec for a gateway graph tool: bare name, `project_id` removed (the harness fills it)."""
    name = getattr(tool, "name", "") or ""
    if not name.startswith(GRAPH_PREFIX):
        return None
    from strands.tools.mcp._compat import input_schema  # mcp 1.x/2.x field-name shim

    schema = copy.deepcopy(input_schema(tool) or {"type": "object", "properties": {}})
    (schema.get("properties") or {}).pop("project_id", None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "project_id"]
    return {"name": name[len(GRAPH_PREFIX):], "gateway_name": name,
            "description": getattr(tool, "description", "") or "Query the active project's knowledge graph.",
            "inputSchema": {"json": schema}}


async def fetch_graph_specs(client: SearchGatewayClient, ref: Any) -> list[dict]:
    """Graph tool specs from the Search_Gateway's `tools/list` (cached 10 min; Cedar filters the list)."""
    key = client.url
    hit = _spec_cache.get(key)
    if hit and time.time() - hit[0] < _SPEC_TTL_S:
        return hit[1]
    tools = await client.list_tools(await ref.current())
    specs = [s for s in (_model_spec(t) for t in tools) if s]
    _spec_cache[key] = (time.time(), specs)
    return specs


class ProjectGraphTool(AgentTool):
    def __init__(self, ref: Any, spec: dict, *, project_id: str, client: SearchGatewayClient, caller: str = "main",
                 turn_id: str = ""):
        super().__init__()
        self._ref, self._client, self._project_id, self._caller = ref, client, project_id, caller
        self._turn_id = turn_id
        self._gateway_name = spec["gateway_name"]
        self._spec = {k: v for k, v in spec.items() if k != "gateway_name"}

    @property
    def tool_name(self) -> str:
        return self._spec["name"]

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec  # type: ignore[return-value]

    @property
    def tool_type(self) -> str:
        return "python"

    def __repr__(self) -> str:
        return f"<ProjectGraphTool {self.tool_name} project={self._project_id}>"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        t0 = time.monotonic()
        category, message, text = await self._run(dict(tool_use.get("input") or {}))
        logger.info("project_graph call tool=%s caller=%s project=%s ok=%s category=%s ms=%d", self.tool_name,
                    self._caller, self._project_id, category is None, category, int((time.monotonic() - t0) * 1000))
        if category is None:
            yield ToolResultEvent({"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]})
        else:
            yield ToolResultEvent({"toolUseId": tool_use_id, "status": "error",
                                   "content": [{"text": errors.error_result(category, message)}]})

    async def _run(self, args: dict) -> tuple[Optional[str], str, str]:
        # 1. The harness owns the Project: whatever the model put in project_id is replaced (Cedar re-checks it).
        args["project_id"] = self._project_id
        # 2. Token (refreshed or re-minted inside `current()`); access errors never mention tokens.
        try:
            token = await self._ref.current()
        except AccessEnded:
            return errors.PROJECT_ACCESS_ENDED, errors.ACCESS_ENDED_TEXT, ""
        except NotEstablished:
            return errors.PROJECT_ACCESS_UNAVAILABLE, errors.ACCESS_NOT_ESTABLISHED_TEXT, ""
        # 3. Call through the Search_Gateway (CUSTOM_JWT + Cedar ENFORCE).
        outcome = await self._client.call_tool(token, self._gateway_name, args)
        cls = errors.classify(outcome)
        if cls:
            category, msg = cls
            return category, (GRAPH_POLICY_DENIED_TEXT if category == errors.POLICY_DENIED else msg), ""
        # 4. The graph Lambda answers {"ok": true, ...} or {"ok": false, "error": {code, message}}.
        try:
            body = json.loads(outcome.result) if isinstance(outcome.result, str) else outcome.result
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("ok") is False:
            err = body.get("error") or {}
            return str(err.get("code") or "graph_error"), errors.compose_reason(err.get("message") or "graph query failed"), ""
        if isinstance(body, dict):
            # The visual footprint goes to the turn's "↳ Graph" render (Main_Agent only — a sub-agent or plan
            # node would open a tab per call), never to the model: it would only repeat the result in tokens.
            subgraph = body.pop("subgraph", None)
            if self._caller == "main" and self._turn_id:
                from . import graph_ledger

                graph_ledger.record(self._turn_id, self._project_id, self.tool_name, subgraph)
            return None, "", json.dumps(body, separators=(",", ":"))
        return None, "", outcome.result if isinstance(outcome.result, str) else json.dumps(outcome.result)


async def make_graph_tools(ref: Any, project_id: str, *, caller: str = "main",
                           client: Optional[SearchGatewayClient] = None, turn_id: str = "") -> list[ProjectGraphTool]:
    """The graph tools for one Project, or [] when the gateway is not configured or lists none (never raises)."""
    client = client or SearchGatewayClient()
    if not client.url or ref is None:
        return []
    try:
        specs = await fetch_graph_specs(client, ref)
    except Exception as exc:  # noqa: BLE001 — no graph tools this turn; search and the rest still bind
        logger.warning("graph tool spec fetch failed (%s); no graph tools this turn", type(exc).__name__)
        return []
    return [ProjectGraphTool(ref, s, project_id=project_id, client=client, caller=caller, turn_id=turn_id)
            for s in specs]
