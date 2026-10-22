"""DAG Schema data model — Design Agent output schema.

Defines DAGNode, Plan, and ValidationError dataclasses
with JSON serialization/deserialization and the embedded DAG JSON Schema constant.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class NodeKind:
    """The taxonomy of DAG node kinds.

    Two conceptual axes describe every kind (see docs/dynamic-fanout-design.md §6):
      * executor  — who runs it: ``agent`` (a Task-Agent LLM turn),
        ``mechanical`` (the Plan Chainer runs it directly, no LLM), or
        ``control`` (no executor; it only shapes the graph).
      * cardinality — its edge shape: 1->1, 1->N (fan-out), N->1 (fan-in),
        or 1->(1 of N) (branch).

    Kinds:
      TASK       agent / 1->1   default worker: LLM + tools/skills; may spawn an
                                 async gateway job. Every node today is this.
      TOOL       mechanical/1->1 deterministic single gateway/tool call with
                                 pre-resolved params (config.tool + config.arguments),
                                 no LLM: the Task Agent's fast path makes the one call
                                 itself. Falls back to an ordinary TASK turn (its prompt)
                                 whenever the call cannot be made as written.
      SYNTHESIS  agent / N->1   terminal LLM composition of upstream outputs into
                                 the answer (tools=[]); today an implicit tools=[] node.
      MAP        control/1->N   fan-out: expand into N children over a collection
                                 discovered at runtime (config.over).
      GATHER     control/N->1   fan-in: barrier + reduce over a MAP's children.
      BRANCH     control/1-of-N conditional: pick the downstream path by a predicate
                                 over an upstream output (config.on / config.cases).

    ``kind`` is a plain string (not an enum) so unknown values round-trip and are
    reported by the validator rather than raising at deserialization — a plan from
    a newer producer must never crash an older reader.
    """
    TASK = "task"
    TOOL = "tool"
    SYNTHESIS = "synthesis"
    MAP = "map"
    GATHER = "gather"
    BRANCH = "branch"

    #: Kinds the engine currently recognizes. Anything else is an ``unknown_kind``
    #: validation error (structural, non-fatal at parse time).
    KNOWN: frozenset[str] = frozenset({TASK, TOOL, SYNTHESIS, MAP, GATHER, BRANCH})

    #: Kinds whose behavior is wired into execution. The rest are schema-only for
    #: now (recognized + validated, but not yet executed) — they light up per phase.
    IMPLEMENTED: frozenset[str] = frozenset({TASK, SYNTHESIS, TOOL})


def dynamic_fanout_enabled() -> bool:
    """Whether runtime fan-out (``map``/``gather``) execution is turned on.

    Off by default; enabled per-env via the CDK context flag ``dynamicFanoutEnabled``
    (staging deploys with it ``true``; prod does not). The Plan Chainer now DOES
    expand ``map`` nodes at runtime (``_expand_ready_map_nodes`` + ``_run_gather``,
    covered by test_map_fanout.py). This one flag gates all FOUR places that must
    agree — the validator's executability gate, the planning + design agents'
    map/gather emission guidance, and the chainer's runtime expansion — so emission
    and execution can never drift: they turn on together.
    """
    return os.environ.get("DYNAMIC_FANOUT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def executable_kinds() -> frozenset[str]:
    """Node kinds the engine can actually execute in THIS environment right now.

    Always includes the wired-in kinds (``NodeKind.IMPLEMENTED`` = task/synthesis).
    Adds map/gather only when ``dynamic_fanout_enabled()``. Anything KNOWN but not
    in this set is rejected by ``validator.validate_plan`` as ``unsupported_kind``
    (routed to the design-agent retry), so an un-executable kind can never be
    dispatched as a plain task.
    """
    kinds = set(NodeKind.IMPLEMENTED)
    if dynamic_fanout_enabled():
        kinds |= {NodeKind.MAP, NodeKind.GATHER}
    return frozenset(kinds)


@dataclass
class DAGNode:
    """A single node in the execution DAG.

    The base fields (id, kind, depends_on, output_key, output_type) are common to
    every kind. ``prompt``/``tools``/``skills`` carry the common TASK/SYNTHESIS
    payload inline (the overwhelmingly common case, kept top-level for backward
    compatibility); any kind-specific payload lives in ``config`` (e.g. a MAP's
    ``over``/``item_var``/``template``). See NodeKind + the design doc for shapes.
    """
    id: str
    prompt: str
    tools: list[str]
    depends_on: list[str]
    output_key: str
    output_type: str  # "text" | "json" | "s3_uri"
    skills: list[dict] = field(default_factory=list)
    model_id: str | None = None
    kind: str = NodeKind.TASK
    config: dict = field(default_factory=dict)
    #: Cross-plan producer references (guided-mode phase handoff). Each entry is
    #: {"plan": <producer plan display name>, "node": <producer node id>}. The
    #: chainer resolves these to exact deliverable URIs — the model never types
    #: a uuid. Empty for the common single-plan case.
    external_inputs: list[dict] = field(default_factory=list)
    #: The exact fal endpoint id(s) a node that calls ``submit_job``/``run_model`` must use, e.g.
    #: ``["minimax/h3-max/image-to-video"]``. The node's submit is refused before it reaches fal
    #: when it names any other endpoint (session c3447913: planned H3-Max, submitted Hailuo, 422).
    #: Empty = unconstrained.
    endpoint_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "prompt": self.prompt,
            "tools": list(self.tools),
            "depends_on": list(self.depends_on),
            "output_key": self.output_key,
            "output_type": self.output_type,
            "skills": [s if isinstance(s, dict) else {"name": s} for s in self.skills],
        }
        if self.model_id is not None:
            d["model_id"] = self.model_id
        # Emit kind/config only when non-default so existing TASK-node serialization
        # (and the tests asserting it) are byte-for-byte unchanged.
        if self.kind and self.kind != NodeKind.TASK:
            d["kind"] = self.kind
        if self.config:
            d["config"] = dict(self.config)
        if self.external_inputs:
            d["external_inputs"] = [dict(x) for x in self.external_inputs]
        if self.endpoint_ids:
            d["endpoint_ids"] = list(self.endpoint_ids)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> DAGNode:
        return cls(
            id=data["id"],
            prompt=data.get("prompt", ""),
            tools=list(data.get("tools", [])),
            depends_on=list(data["depends_on"]),
            output_key=data["output_key"],
            output_type=data["output_type"],
            skills=[s if isinstance(s, dict) else {"name": s} for s in data.get("skills", [])],
            model_id=data.get("model_id"),
            kind=data.get("kind", NodeKind.TASK) or NodeKind.TASK,
            config=dict(data.get("config", {})),
            external_inputs=[dict(x) for x in data.get("external_inputs", [])],
            endpoint_ids=[e.strip() for e in (data.get("endpoint_ids") if isinstance(
                data.get("endpoint_ids"), list) else []) if isinstance(e, str) and e.strip()],
        )


@dataclass
class Plan:
    """A complete executable DAG plus metadata, produced by the Design Agent."""
    plan_id: str
    goal: str
    nodes: list[DAGNode]
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "nodes": [n.to_dict() for n in self.nodes],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Plan:
        return cls(
            plan_id=data["plan_id"],
            goal=data["goal"],
            nodes=[DAGNode.from_dict(n) for n in data["nodes"]],
            metadata=dict(data.get("metadata", {})),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> Plan:
        return cls.from_dict(json.loads(s))


@dataclass
class ValidationError:
    """Structured validation error for plan validation failures."""
    error_type: str   # "cycle" | "missing_tool" | "invalid_dependency"
    message: str
    details: dict

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ValidationError:
        return cls(
            error_type=data["error_type"],
            message=data["message"],
            details=dict(data["details"]),
        )


# DAG JSON Schema — embedded in Design Agent system prompt
DAG_JSON_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["plan_id", "goal", "nodes"],
    "properties": {
        "plan_id": {"type": "string"},
        "goal": {"type": "string"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "prompt", "tools", "depends_on", "output_key", "output_type"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"enum": ["task", "tool", "synthesis", "map", "gather", "branch"]},
                    "prompt": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "object"}},
                    "model_id": {"type": ["string", "null"]},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "output_key": {"type": "string"},
                    "output_type": {"enum": ["text", "json", "s3_uri"]},
                    "config": {"type": "object"},
                    "for_each": {
                        "type": "object",
                        "required": ["var", "items"],
                        "properties": {
                            "var": {"type": "string"},
                            "items": {"type": "array"},
                        },
                    },
                    "endpoint_ids": {"type": "array", "items": {"type": "string"}},
                    "external_inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["plan", "node"],
                            "properties": {
                                "plan": {"type": "string"},
                                "node": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "metadata": {"type": "object"},
    },
}


def validate_node_kinds(plan: "Plan") -> list["ValidationError"]:
    """Structural validation of node ``kind`` and per-kind ``config``.

    Complements ``validator.validate_plan`` (cycles/deps/tools) with the taxonomy
    rules. Returns a (possibly empty) list of ``ValidationError`` — never raises —
    so a caller can aggregate these with the existing structural errors.

    Rules:
      * ``kind`` must be one of NodeKind.KNOWN (else ``unknown_kind``).
      * MAP: ``config.over`` = {from, path}; ``from`` must reference another node in
        the plan; ``path`` must satisfy the fan-out grammar; ``item_var`` + a
        ``template`` (with a prompt) are required (``invalid_map``).
      * GATHER: ``config.of`` must reference a MAP node in the plan (``invalid_gather``).
      * BRANCH: ``config.on`` = {from, path} referencing another node, plus a
        non-empty ``config.cases`` mapping to node ids (``invalid_branch``).
      * TOOL: ``config.tool`` (a tool name) is required (``invalid_tool_node``).
    """
    # Imported here (not at module top) to avoid a load-order dependency between
    # two sibling src modules; both are on sys.path when the agent runs.
    from fanout_extract import FanoutExtractError, validate_path

    errors: list[ValidationError] = []
    ids = {n.id for n in plan.nodes}
    kinds = {n.id: n.kind for n in plan.nodes}

    def err(kind_err: str, node_id: str, msg: str) -> None:
        errors.append(ValidationError(error_type=kind_err, message=msg, details={"node_id": node_id}))

    for n in plan.nodes:
        if n.kind not in NodeKind.KNOWN:
            err("unknown_kind", n.id,
                f"node {n.id!r} has unknown kind {n.kind!r}; known: {sorted(NodeKind.KNOWN)}")
            continue

        cfg = n.config or {}

        if n.kind == NodeKind.MAP:
            over = cfg.get("over") or {}
            src, path = over.get("from"), over.get("path", "")
            if not src or src not in ids:
                err("invalid_map", n.id,
                    f"map node {n.id!r} config.over.from must reference a node in the plan (got {src!r})")
            try:
                validate_path(path)
            except FanoutExtractError as e:
                err("invalid_map", n.id, f"map node {n.id!r} config.over.path invalid: {e}")
            if not cfg.get("item_var"):
                err("invalid_map", n.id, f"map node {n.id!r} requires config.item_var")
            template = cfg.get("template") or {}
            if not template.get("prompt"):
                err("invalid_map", n.id, f"map node {n.id!r} requires config.template.prompt")
            # The map must depend on its over.from node so it only expands once that node has
            # completed and its list is materialised (the runtime reads over.from's result).
            if src and src in ids and src not in (n.depends_on or []):
                err("invalid_map", n.id,
                    f"map node {n.id!r} must list its over.from producer {src!r} in depends_on")

        elif n.kind == NodeKind.GATHER:
            of = cfg.get("of")
            if not of or of not in ids:
                err("invalid_gather", n.id,
                    f"gather node {n.id!r} config.of must reference a node in the plan (got {of!r})")
            elif kinds.get(of) != NodeKind.MAP:
                err("invalid_gather", n.id,
                    f"gather node {n.id!r} config.of must reference a MAP node (got kind {kinds.get(of)!r})")

        elif n.kind == NodeKind.BRANCH:
            on = cfg.get("on") or {}
            if not on.get("from") or on.get("from") not in ids:
                err("invalid_branch", n.id,
                    f"branch node {n.id!r} config.on.from must reference a node in the plan")
            cases = cfg.get("cases") or {}
            if not isinstance(cases, dict) or not cases:
                err("invalid_branch", n.id, f"branch node {n.id!r} requires a non-empty config.cases mapping")

        elif n.kind == NodeKind.TOOL:
            if not cfg.get("tool"):
                err("invalid_tool_node", n.id, f"tool node {n.id!r} requires config.tool (a tool name)")
            elif "arguments" in cfg and not isinstance(cfg.get("arguments"), dict):
                err("invalid_tool_node", n.id,
                    f"tool node {n.id!r} config.arguments must be an object of the tool's parameters")

    return errors


# --- Async job lifecycle (submit -> poll status -> fetch results) ---------------------------
# A node that can SUBMIT an async gateway job must also be able to observe and collect it. The
# verbs are matched on the federated segment after "___", so this stays gateway-agnostic.
_ASYNC_SUBMIT_VERBS = frozenset({"submit_job"})
_ASYNC_STATUS_VERBS = frozenset({"check_job", "get_job_status"})
_ASYNC_RESULT_VERBS = frozenset({"get_job_result", "get_job_results", "get_results"})
# The model-parameter lookup a generation node is TOLD to make (media-references: "If you must set a tuning
# param, `get_model_schema` first"). A node granted run_model without it was handed an instruction it could not
# follow (session e2fb2692: two nodes stalled demanding an unavailable schema lookup). Read-only and cheap.
_SCHEMA_VERBS = frozenset({"get_model_schema"})

# Verbs whose RESULT can be an unresolved async queue HANDLE (a request_id, not the deliverable),
# so a node using one must be able to resolve it via the same status/result trio. ``submit_job``
# ALWAYS returns a handle; ``run_model`` is synchronous but waits only a short, bounded time and
# so degrades to a handle when the queue is backed up (session 154b5b07: a flux-2 edit came back
# IN_QUEUE, the edit node had no resolver verb, and the downstream video node then had no image
# URL). Gateway-agnostic — matched on the federated verb segment.
_ASYNC_HANDLE_VERBS = _ASYNC_SUBMIT_VERBS | frozenset({"run_model"})


def _tool_verb(tool: str) -> str:
    """The verb segment of a federated tool name ``<target>___<verb>`` (or the whole name)."""
    return str(tool).rpartition("___")[2]


def _tool_target(tool: str) -> str:
    """The gateway/target segment of a federated tool name ``<target>___<verb>`` (or "")."""
    head, sep, _ = str(tool).partition("___")
    return head if sep else ""


def normalize_async_job_nodes(plan: "Plan", available_tools: "set[str] | None" = None) -> list[str]:
    """Auto-repair async-handle-producing nodes so a mechanically-derivable omission does not
    sink a correct plan.

    A node that can produce an async gateway HANDLE needs the sibling *status* and *result* verbs
    of the SAME gateway to observe and collect it. Two producer verbs qualify: ``submit_job``
    (always a handle — also must not declare ``output_type: text``) and ``run_model`` (usually a
    final result, but degrades to a handle under queue load). The Design Agent frequently emits a
    ``submit_job`` node with only that verb + ``output_type: text`` — a shape the validator
    (rightly) rejects but the retry loop keeps reproducing (sessions f0598806, 59c46b2c); and it
    emits ``run_model`` edit nodes with no resolver verb, so a handle returned under load can never
    be resolved and the downstream consumer gets no URL (session 154b5b07).

    Repair it from what the system already knows, rather than hardcoding tool names:
      * the async job TRIO (submit -> status -> result) is a known verb grammar
        (``_ASYNC_*_VERBS``) — the same grouping the Job_Detector discovers at connect time;
      * the concrete sibling tool names are resolved from the LIVE catalog by matching the
        submit tool's own ``<target>___<verb>`` — so it works for any gateway (fal, M2C, a
        user MCP) with no per-gateway constants.

    Only verbs that actually EXIST in ``available_tools`` are added; if a gateway genuinely
    lacks a status/result verb, nothing is added and ``validate_async_job_nodes`` still reports
    it (a real, non-repairable error). ``output_type`` coercion is catalog-independent and
    always applied. Mutates nodes in place; returns human-readable repair notes for logging.
    Never raises.
    """
    catalog = {t for t in (available_tools or []) if isinstance(t, str)}
    # bare-verb -> does the catalog offer this verb on ANY target (fallback for a submit tool
    # emitted without its target prefix, where the specific target cannot be recovered).
    notes: list[str] = []

    def _resolve_sibling(target: str, verbs: "frozenset[str]") -> "str | None":
        # Prefer the SAME target's verb (e.g. ``usr-abc___check_job``); fall back to any
        # catalog tool exposing one of the verbs when the target is unknown/bare.
        if target:
            for v in verbs:
                cand = f"{target}___{v}"
                if cand in catalog:
                    return cand
        if not target:
            for c in catalog:
                if _tool_verb(c) in verbs:
                    return c
        return None

    for n in plan.nodes:
        verbs = {_tool_verb(t) for t in (n.tools or [])}
        if not (verbs & _ASYNC_HANDLE_VERBS):
            continue

        # A `submit_job` ALWAYS returns a handle, never a final result, so a text contract is
        # always wrong for it. `run_model` usually returns a final result (an image URL) and only
        # degrades to a handle under queue load, so its declared output_type is left untouched —
        # only its ability to resolve a handle (the verbs below) is ensured.
        if (verbs & _ASYNC_SUBMIT_VERBS) and n.output_type == "text":
            n.output_type = "json"
            notes.append(f"node {n.id!r}: output_type text->json (async submit)")

        # Resolve the target from the node's own producer tool so sibling verbs come from the
        # SAME gateway. (A bare verb yields "" -> catalog-wide fallback.)
        target = next((_tool_target(t) for t in (n.tools or []) if _tool_verb(t) in _ASYNC_HANDLE_VERBS), "")

        for label, verb_set in (("status", _ASYNC_STATUS_VERBS), ("result", _ASYNC_RESULT_VERBS),
                                ("schema", _SCHEMA_VERBS)):
            if verbs & verb_set:
                continue  # already granted
            sibling = _resolve_sibling(target, verb_set)
            if sibling and sibling not in (n.tools or []):
                n.tools.append(sibling)
                verbs.add(_tool_verb(sibling))
                notes.append(f"node {n.id!r}: granted {label} verb {sibling!r}")

    return notes


def validate_async_job_nodes(plan: "Plan") -> list["ValidationError"]:
    """A node that submits an async gateway job must be able to observe + collect it, and must
    not declare ``output_type: text``.

    An async submit returns a job HANDLE, not the deliverable. A node granted only the submit
    verb cannot poll -> fetch, and one declaring ``output_type: text`` lets a prose response
    satisfy its contract — so it goes terminal before the job lands and a downstream consumer
    finds no artifact (the stitch-crossfade race, session f9105222). Matched on the federated
    verb segment, so it is gateway-agnostic. Returns a (possibly empty) list; never raises.
    """
    errors: list[ValidationError] = []
    for n in plan.nodes:
        verbs = {_tool_verb(t) for t in (n.tools or [])}
        if not (verbs & _ASYNC_SUBMIT_VERBS):
            continue
        if n.output_type == "text":
            errors.append(ValidationError(
                error_type="async_node_output_type",
                message=(f"node {n.id!r} submits an async job but declares output_type 'text'; a "
                         f"submitted job yields a handle/artifact, so declare 'json' or 's3_uri' "
                         f"(prose would satisfy the contract before the job completes)"),
                details={"node_id": n.id}))
        if not (verbs & _ASYNC_STATUS_VERBS) or not (verbs & _ASYNC_RESULT_VERBS):
            errors.append(ValidationError(
                error_type="async_node_missing_tools",
                message=(f"node {n.id!r} submits an async job but lacks the tools to observe and "
                         f"collect it; grant a status verb ({sorted(_ASYNC_STATUS_VERBS)}) and a "
                         f"result verb ({sorted(_ASYNC_RESULT_VERBS)}) for the same gateway"),
                details={"node_id": n.id}))
    return errors


def normalize_tool_nodes(plan: "Plan") -> list[str]:
    """A ``tool`` node must be able to make the call it names.

    Its ``config.tool`` is added to ``tools`` when missing, so tool resolution in the Task Agent,
    the chainer's capability union and the validator's tool-existence check all see it. Mutates in
    place; returns repair notes. Never raises.
    """
    notes: list[str] = []
    for n in plan.nodes:
        if n.kind != NodeKind.TOOL:
            continue
        name = (n.config or {}).get("tool")
        # Compare BARE names: a gateway tool is listed as "<target>___<tool>" or just "<tool>", and both are the
        # same tool. An exact-string check appended "submit_job" beside "usr-…___submit_job", and the node agent
        # then refused the duplicate registration (plan 593333b5: eight fal nodes crashed on start).
        if isinstance(name, str) and name and _bare_tool(name) not in {_bare_tool(t) for t in (n.tools or [])}:
            n.tools = list(n.tools or []) + [name]
            notes.append(f"node {n.id!r}: tools += {name!r} (its config.tool)")
    return notes


def _bare_tool(name: object) -> str:
    """A tool name without its gateway target prefix ("usr-1___submit_job" -> "submit_job")."""
    return name.rsplit("___", 1)[-1] if isinstance(name, str) else ""
