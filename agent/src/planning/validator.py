"""Plan validator — pure functions for validating a Plan before execution.

Validates DAG structure (cycles, dependency references) and tool availability.
Returns structured ValidationError lists. No side effects.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque

from planning.dag_schema import (
    DAGNode,
    NodeKind,
    Plan,
    ValidationError,
    executable_kinds,
    validate_async_job_nodes,
    validate_node_kinds,
)

logger = logging.getLogger(__name__)


def validate_plan(plan: Plan, available_tools: set[str]) -> list[ValidationError]:
    """Validate a plan. Returns empty list if valid.

    Note: Tool availability check is intentionally lenient — the validator
    accepts bare tool names, gateway-prefixed names, and canonical:// forms.
    Only structural errors (cycles, broken dependencies) are hard failures.
    """
    errors: list[ValidationError] = []

    # Check dependency references
    invalid_deps = check_dependencies(plan.nodes)
    for dep_ref in invalid_deps:
        parts = dep_ref.split(" -> ")
        node_id, missing_id = parts[0], parts[1]
        errors.append(ValidationError(
            error_type="invalid_dependency",
            message=f"Node '{node_id}' depends on non-existent node '{missing_id}'",
            details={"node_id": node_id, "dependency": missing_id},
        ))

    # Edge SATISFIABILITY (design §6): existence is not enough — the handoff redesign
    # guarantees every consumer a deterministic reference to its dependency's PERSISTED
    # output (A1) + resolved deliverables (A2). That guarantee only holds if the producer
    # actually emits a durable output, i.e. it is an EXECUTABLE (output-producing) node.
    # An edge onto a non-producing control node would leave the consumer with nothing to
    # reference at runtime — the class of "missing input" failure the redesign removes — so
    # reject it BEFORE dispatch and route through the existing design-agent re-plan.
    for dep_ref in check_edge_satisfiability(plan.nodes):
        node_id, dep_id = dep_ref.split(" -> ")
        errors.append(ValidationError(
            error_type="unsatisfiable_dependency",
            message=(
                f"Node '{node_id}' depends on '{dep_id}', which produces no durable output "
                f"to reference. Depend only on output-producing (executable) nodes."
            ),
            details={"node_id": node_id, "dependency": dep_id},
        ))

    # Check for cycles
    cycle_path = detect_cycles(plan.nodes)
    if cycle_path is not None:
        errors.append(ValidationError(
            error_type="cycle",
            message=f"Plan contains a cycle: {' -> '.join(cycle_path)}",
            details={"cycle_path": cycle_path},
        ))

    # Node-kind structural checks (unknown_kind / invalid_map / invalid_gather /
    # invalid_branch / invalid_tool_node) — see dag_schema.validate_node_kinds.
    errors.extend(validate_node_kinds(plan))

    # Async-job contract (P3): a node that can submit an async gateway job must be able to poll +
    # fetch it and must not declare output_type 'text', or it goes terminal before the job lands.
    errors.extend(validate_async_job_nodes(plan))

    # Executability gate. A KNOWN kind that isn't executable in THIS environment
    # (map/gather until DYNAMIC_FANOUT_ENABLED + the chainer supports them; tool/
    # branch until their own phases) must never reach dispatch — the chainer
    # dispatches by node def and ignores `kind`, so a stray map node would run as
    # an empty task. Rejecting it here routes through the existing design-agent
    # retry, which re-plans using only executable kinds.
    _executable = executable_kinds()
    for node in plan.nodes:
        if node.kind in NodeKind.KNOWN and node.kind not in _executable:
            errors.append(ValidationError(
                error_type="unsupported_kind",
                message=(
                    f"Node '{node.id}' uses kind '{node.kind}', which is defined but "
                    f"not executable in this environment yet. Re-plan using only "
                    f"executable kinds: {sorted(_executable)}."
                ),
                details={"node_id": node.id, "kind": node.kind},
            ))

    # Tool availability check.
    #
    # If `available_tools` is empty, the catalog itself failed to build
    # (e.g. gateways not yet connected) — that's a system-level failure
    # already logged loudly by the caller (plan_and_execute_tool.py), not
    # something the Design Agent can fix by retrying with the same empty
    # catalog. Skip the check entirely in that case rather than hard-
    # failing every plan whenever the catalog is temporarily unavailable.
    #
    # When `available_tools` IS populated, an unknown tool name is a real
    # Design Agent hallucination (e.g. "start_separate_audio_stems" when
    # the real tool is "separate_audio_stems") and previously was ONLY
    # logged as a warning — never fed back to the retry-with-Design-Agent
    # path below, even though that path already exists and handles cycle/
    # dependency errors this way. A hallucinated tool name given to a Task
    # Agent node resolves to zero tools; observed in production, the node
    # then fabricated a fake job_id and reported success instead of
    # failing (plan deb1cec6-c2d1-4f28-bc9e-8c61a8038daf). Promoting this
    # to a hard ValidationError lets the existing retry mechanism correct
    # the Design Agent's output before any node is ever dispatched.
    if available_tools:
        unknown_tools = check_tools(plan.nodes, available_tools)
        for tool_ref in unknown_tools:
            node_id, tool_name = tool_ref.split(":", 1)
            logger.warning(
                "Plan references unknown tool '%s' on node '%s'",
                tool_name, node_id,
            )
            errors.append(ValidationError(
                error_type="unknown_tool",
                message=(
                    f"Node '{node_id}' references tool '{tool_name}' which does not "
                    f"exist in the available tool catalog. Use one of the exact tool "
                    f"names provided in the catalog."
                ),
                details={"node_id": node_id, "tool_name": tool_name},
            ))

    # external_inputs are cross-plan producer references (guided-mode phase handoff),
    # deliberately NOT part of depends_on (which the missing-node check above rejects
    # for out-of-plan ids). Validate only their SHAPE here — existence is resolved at
    # dispatch by the chainer against the referenced plan's manifest, not at plan time.
    for node in plan.nodes:
        for ref in getattr(node, "external_inputs", None) or []:
            if not isinstance(ref, dict) or not ref.get("plan") or not ref.get("node"):
                errors.append(ValidationError(
                    error_type="invalid_external_input",
                    message=(
                        f"Node '{node.id}' has a malformed external_input "
                        f"(need string 'plan' and 'node'): {ref!r}"
                    ),
                    details={"node_id": node.id, "external_input": ref},
                ))

    return errors


def detect_cycles(nodes: list[DAGNode]) -> list[str] | None:
    """Returns cycle path if found, None if acyclic. Uses Kahn's algorithm."""
    if not nodes:
        return None

    node_ids = {n.id for n in nodes}
    # Build adjacency list and in-degree map (only for valid edges)
    in_degree: dict[str, int] = {n.id: 0 for n in nodes}
    successors: dict[str, list[str]] = defaultdict(list)

    for node in nodes:
        for dep in node.depends_on:
            if dep in node_ids:  # only count valid deps
                in_degree[node.id] += 1
                successors[dep].append(node.id)

    # Kahn's: start with zero in-degree nodes
    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    visited_count = 0

    while queue:
        current = queue.popleft()
        visited_count += 1
        for succ in successors[current]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    # If we didn't visit all nodes, there's a cycle
    if visited_count == len(nodes):
        return None

    # Find the cycle path via DFS on remaining nodes
    remaining = {nid for nid, deg in in_degree.items() if deg > 0}
    return _find_cycle_path(remaining, nodes)


def _find_cycle_path(remaining: set[str], nodes: list[DAGNode]) -> list[str]:
    """DFS to extract an actual cycle path from the remaining nodes."""
    # Build adjacency: node -> nodes it depends on (edges go dep -> node, but
    # we traverse depends_on direction to find back-edges)
    deps_map: dict[str, list[str]] = {}
    for node in nodes:
        if node.id in remaining:
            deps_map[node.id] = [d for d in node.depends_on if d in remaining]

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in remaining}
    path: list[str] = []

    for start in remaining:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path = []
        while stack:
            node_id, idx = stack.pop()
            if idx == 0:
                color[node_id] = GRAY
                path.append(node_id)
            else:
                # Returning to this node after exploring a child —
                # make sure path is trimmed to this node's position
                while path and path[-1] != node_id:
                    path.pop()

            neighbors = deps_map.get(node_id, [])
            if idx < len(neighbors):
                stack.append((node_id, idx + 1))
                neighbor = neighbors[idx]
                if color[neighbor] == GRAY:
                    # Found cycle — extract from path
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    return cycle
                elif color[neighbor] == WHITE:
                    stack.append((neighbor, 0))
            else:
                color[node_id] = BLACK
                if path and path[-1] == node_id:
                    path.pop()

    # Fallback: return the remaining node IDs (shouldn't reach here)
    return list(remaining)


def check_dependencies(nodes: list[DAGNode]) -> list[str]:
    """Returns list of invalid dependency references as 'node_id -> missing_dep_id'."""
    valid_ids = {n.id for n in nodes}
    invalid: list[str] = []
    for node in nodes:
        for dep in node.depends_on:
            if dep not in valid_ids:
                invalid.append(f"{node.id} -> {dep}")
    return invalid


# Kinds that route/decide but emit no durable, referenceable output. A consumer that
# `depends_on` one has nothing to read at runtime (the handoff redesign hands over a
# producer's persisted output; a branch has none). Kept separate from executability so
# the invariant still holds once BRANCH becomes executable.
_NON_PRODUCING_KINDS = frozenset({NodeKind.BRANCH})


def check_edge_satisfiability(nodes: list[DAGNode]) -> list[str]:
    """Returns edges 'node_id -> dep_id' whose PRODUCER emits no referenceable output.

    Existence (check_dependencies) is necessary but not sufficient: the redesign's
    deterministic handoff can only supply a reference if the dependency actually produced
    a durable output. An edge onto a non-producing (routing) node is unsatisfiable and would
    surface at runtime as a missing input — so it is rejected pre-execution."""
    by_id = {n.id: n for n in nodes}
    unsatisfiable: list[str] = []
    for node in nodes:
        for dep in node.depends_on:
            producer = by_id.get(dep)
            if producer is not None and producer.kind in _NON_PRODUCING_KINDS:
                unsatisfiable.append(f"{node.id} -> {dep}")
    return unsatisfiable


_CANONICAL_PREFIX = "canonical://"


def check_tools(nodes: list[DAGNode], available_tools: set[str]) -> list[str]:
    """Returns list of unknown tool names as 'node_id:tool_name'.

    Matching is flexible: a tool name is considered valid if it exactly matches
    an available tool, OR if it matches the bare suffix of a gateway-prefixed
    tool (after the '___' separator). This handles the case where the Design
    Agent emits bare tool names (e.g. 'start_stem_separation') while the
    catalog has gateway-prefixed names (e.g. 'audio-stem-separation___start_stem_separation').
    """
    # Build a lookup of bare suffixes → full tool names for flexible matching
    bare_names: set[str] = set(available_tools)
    for tool_name in available_tools:
        if "___" in tool_name:
            bare = tool_name.split("___", 1)[-1]
            bare_names.add(bare)

    unknown: list[str] = []
    for node in nodes:
        for tool in node.tools:
            bare = tool.removeprefix(_CANONICAL_PREFIX)
            if bare not in bare_names:
                unknown.append(f"{node.id}:{tool}")
    return unknown
