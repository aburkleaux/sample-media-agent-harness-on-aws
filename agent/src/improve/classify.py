"""Which KIND of invocation a session was, so it can be judged against the right objective.

The loop had one objective function, `goal_judge`, applied to every session in the
population. But the harness serves several kinds of invocation, and most of them have no
user goal at all. Scoring those with a goal rubric does not produce a wrong answer, it
produces a confidently wrong one: measured on the 50-session harvest, 30 of 35 FAILs were
the judge reporting "No conversation or final response is available", which is not a
failure, it is an absence of the thing that rubric measures.

Filtering them out would be worse. The point of the loop is to look at every part of the
harness and attribute what it finds to a Sigma component, so a class of invocation that is
never examined is a part of the harness that can never be improved. So nothing is dropped
here. Each class is routed to the objective it actually has:

    conversational   a model was invoked outside a plan sub-session, so there is agent
                     behaviour to grade. The two-tier goal judge, as before. Note the test is
                     the model span, NOT the presence of a transcript: a conversation whose
                     transcript could not be read is still a conversation, and it has to
                     surface as an evidence gap rather than be classified away.
    plan_node        a plan's own sub-session. Real DAG evidence, but the GOAL belongs to
                     the parent request, so goal scoring is the parent's job.
    tool_discovery   session warm-up that resolves the tool catalog off the gateways.
                     Succeeds or errors; there is no goal to achieve.
    control_plane    a Lab CRUD / control operation. Same.
    unclassified     shape we do not recognise. Deliberately gets NO evaluators, so the
                     verdict is UNSCORED rather than a guess.

**The basis is inferred, and says so.** Classification reads span names, because nothing in
the telemetry records what kind of invocation arrived. The separation is clean on the
harvest measured (`has_model_span` predicts `turns == 0` in 22 of 22 cases, with no
overlap in span counts: 11-85 for model-less sessions against 112-541 for conversations),
but a clean split is not the same as an authoritative one. `Workload.basis` therefore
carries `inferred_from_spans`, the same discipline `Attribution.basis` uses for
`keyword_inferred`. The durable fix is for the runtime to stamp the invocation kind on the
run record, which is run-record field 3 and is recorded nowhere today.

Not to be confused with the workload FAMILY (`{family, mode}`, e.g. `fal_generation:direct`)
that the Workloads surface needs. That is a subdivision of `conversational` by what the user
asked for; this is a division by what the harness was asked to do.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

try:
    from .contract import NO_DATA, Run, Step
except ImportError:  # pragma: no cover
    from improve.contract import NO_DATA, Run, Step  # type: ignore


# A span whose presence proves a model was invoked. `chat` and `chat <model-id>` are the
# Bedrock call itself; `execute_event_loop_cycle` is a Strands agent turn; `invoke_agent` is
# a sub-agent handoff. Prefix-matched because the model id is appended to several of them.
MODEL_SPAN_PREFIXES = ("chat", "execute_event_loop_cycle", "invoke_agent")

# The MCP handshake that resolves a gateway's tool catalog.
DISCOVERY_SPAN = "mcp tools/list"

# Span name prefixes of Lab / control-plane data operations. Deliberately narrow: this is
# used only to separate a control-plane invocation from an unrecognised one, never to judge.
CONTROL_PLANE_SPAN_PREFIXES = (
    "DynamoDB.", "S3.", "Bedrock AgentCore Control.",
)

# Session id prefixes the harness itself assigns. `session_kind` in `observe/population.py`
# reports the same three; they are re-derived rather than imported so that classification
# never depends on the discovery layer, which has the opposite direction of dependency.
PLAN_SESSION_PREFIX = "plan-session-"
LAB_SESSION_PREFIX = "lab-"
CATALOG_SESSION_PREFIX = "harness-catalog-"


class WorkloadClass(str, Enum):
    CONVERSATIONAL = "conversational"
    PLAN_NODE = "plan_node"
    TOOL_DISCOVERY = "tool_discovery"
    CONTROL_PLANE = "control_plane"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Workload:
    """What kind of invocation this run was, and on what grounds.

    ``basis`` is always ``inferred_from_spans`` today. It exists so that a consumer can tell
    an inferred class from a recorded one once the runtime starts stamping the kind, rather
    than the field silently changing meaning.
    """
    cls: WorkloadClass
    basis: str
    evidence: str
    # The SESSION id of the request a `plan_node` belongs to, or None when the evidence names
    # none. Sibling sub-sessions of one plan are not independent evidence for a pattern, and this
    # is what lets a roll-up say so instead of counting them separately.
    #
    # `Run.parent_run_id` supplies it, resolved from the OTEL trace the sub-session shares with
    # its parent. It is NOT derived from the id: the id's suffix is unique per sub-session, so
    # reading it gave every sibling a different "parent" and the de-duplication downstream had
    # nothing to collapse. `hint` is now a misnomer in the honest direction — the value is exact,
    # and the name is kept because it is the field consumers already read.
    parent_hint: Optional[str] = None


# Which evaluators run for each class, and which of them gate the verdict.
#
# This is policy, and it lives beside the classifier rather than in `loop.config.yaml`
# because the two halves have to agree: gating on `dispatch_correct` for a class that never
# runs `goal_judge` yields UNSCORED for every member, silently. The config's flat
# `evaluators` / `gating_evaluators` keys describe the CONVERSATIONAL class, which is what
# they were written for (`dispatch_correct` and `results_delivered` are meaningless for an
# invocation with no request), and they still override it.
#
# `unclassified` maps to no evaluators on purpose. An unrecognised shape must produce
# UNSCORED, never a verdict assembled from whichever checks happened to apply.
# A class is not a partition of the SPANS, only of the invocations. A `control_plane` session
# also performs gateway discovery (measured: 2 of the 3 lab sessions carry `mcp tools/list`, one
# of them 8 times), so it runs the discovery objective too. Ordering the classifier the other
# way round would only move the loss: a lab session would then be judged on its handshakes and
# not on its operations. Running both is the fix; `tool_discovery` returns NOT_APPLICABLE when a
# session has no handshakes, so it is safe on the ones that do not.
EVALUATORS_BY_CLASS: dict = {
    WorkloadClass.CONVERSATIONAL: ("goal_judge", "AsyncJobFlowValidator",
                                   "DagCompletionValidator"),
    WorkloadClass.PLAN_NODE: ("AsyncJobFlowValidator", "DagCompletionValidator"),
    WorkloadClass.TOOL_DISCOVERY: ("ToolDiscoveryValidator",),
    WorkloadClass.CONTROL_PLANE: ("ControlPlaneOpValidator", "ToolDiscoveryValidator"),
    WorkloadClass.UNCLASSIFIED: (),
}

GATING_BY_CLASS: dict = {
    WorkloadClass.CONVERSATIONAL: ("dispatch_correct", "results_delivered"),
    # The node either reached terminal success or it did not. Its goal is the parent's.
    WorkloadClass.PLAN_NODE: ("DagCompletionValidator",),
    WorkloadClass.TOOL_DISCOVERY: ("ToolDiscoveryValidator",),
    # Both gate, and the precedence falls out of `gate` correctly: `ControlPlaneOpValidator` is
    # always NO_DATA today, so a lab session is UNSCORED unless a handshake actually FAILED, in
    # which case the measured failure wins. That is the one thing we CAN establish about a lab
    # session, and before this it was unreachable.
    WorkloadClass.CONTROL_PLANE: ("ControlPlaneOpValidator", "ToolDiscoveryValidator"),
    WorkloadClass.UNCLASSIFIED: (),
}


def _span_names(run: Run) -> list[str]:
    """Raw span names, prefixes intact.

    Deliberately not `tool_name_sequence`, which strips `execute_tool ` and
    `mcp tools/call ` so that evaluator name-predicates match. Classification needs the
    names as recorded: `mcp tools/list` is the signal, and a stripping rule written for a
    different purpose must not be able to erase it.
    """
    trajectory = run.trajectory
    if trajectory is NO_DATA or not trajectory:
        return []
    return [step.name if isinstance(step, Step) else str(step) for step in trajectory]


def has_model_span(run: Run) -> bool:
    """Did this session invoke a model at all?

    The single most useful fact about a session, and the one the goal judge implicitly
    assumes. False means there is no agent behaviour to grade, only harness work.
    """
    return any(name.startswith(MODEL_SPAN_PREFIXES) for name in _span_names(run))


# Recorded goal-tier labels. Their presence is the harness's own statement that this run was
# graded as a request-and-response, which outranks anything inferred from span names.
_GOAL_TIER_LABELS = ("dispatch_correct", "results_delivered")


def classify(run: Run) -> Workload:
    """Which class this run belongs to. Never raises, never returns None."""
    names = _span_names(run)
    run_id = run.run_id or ""
    turns = [t for t in (run.turns or []) if t.get("text")]
    model = any(name.startswith(MODEL_SPAN_PREFIXES) for name in names)
    discovery = sum(1 for name in names if name == DISCOVERY_SPAN)

    # A labelled scenario carrying goal tiers was, by construction, judged as a conversation.
    # That is a RECORDED fact and it takes precedence: the golden benchmark's whole purpose is
    # to be the stable reference, so it must not be reclassified by a span-shape heuristic
    # that a harvest change could move. This is also the one path where `basis` is not
    # inferred, which is why the field exists.
    expected = (run.scenario.expected if run.scenario else None) or {}
    if any(tier in expected for tier in _GOAL_TIER_LABELS):
        return Workload(WorkloadClass.CONVERSATIONAL, "recorded_label",
                        "the scenario carries recorded goal-tier labels")

    if not names:
        return Workload(WorkloadClass.UNCLASSIFIED, "inferred_from_spans",
                        "no trajectory was captured, so nothing identifies this invocation")

    if run_id.startswith(PLAN_SESSION_PREFIX):
        # Sub-session of a plan, decided by the id because the id is the harness's own
        # statement. Its conversation lives under the PARENT session id, so absent turns here
        # are expected and are not an evidence gap.
        #
        # WHICH parent is not in the id — see `Workload.parent_hint`. It is read from the
        # evidence, and a run whose evidence does not name one gets None. There is deliberately
        # no fallback to the id's suffix: a unique-per-row value passes every "is it set?" check
        # while making the sibling de-duplication downstream a no-op, which is worse than absent.
        parent = run.parent_run_id if isinstance(run.parent_run_id, str) else ""
        # Evidence tracks value: the sentence says WHETHER a parent was resolved, derived from the
        # same expression the field is set from, so it cannot assert a link the field lacks. It
        # does not NAME the parent — a session id belongs in the structured field, which is the
        # rule `dto.residue_to_wire` records after two prose fields had to be un-published for
        # interpolating one.
        return Workload(WorkloadClass.PLAN_NODE, "inferred_from_spans",
                        "plan sub-session id; the conversation belongs to the parent request"
                        + (", whose session the shared trace identifies" if parent
                           else ", which this run's evidence does not identify"),
                        parent_hint=parent or None)

    if model:
        # A model ran, so there IS agent behaviour to grade, and this is the class whose
        # objective grades it. Turns are deliberately not required. Requiring them conflated
        # two different facts: "this was not a conversation" and "this was a conversation
        # whose transcript we failed to read". The second is an evidence gap the goal judge
        # reports as NO_DATA, which now yields UNSCORED and routes to the defect register;
        # classifying it away as `unclassified` would hide a Memory-read failure instead.
        return Workload(
            WorkloadClass.CONVERSATIONAL, "inferred_from_spans",
            f"a model was invoked; {len(turns)} conversation turn(s) recovered"
            + ("" if turns else " (no transcript found, so the goal tiers cannot be scored)"))

    if run_id.startswith(LAB_SESSION_PREFIX):
        # Checked BEFORE discovery, because the id is the harness's own statement about what
        # this session is for, and a lab session that also warmed up a gateway is still a lab
        # session. Its handshakes are not lost: `CONTROL_PLANE` runs the discovery objective
        # too (see `EVALUATORS_BY_CLASS`).
        return Workload(WorkloadClass.CONTROL_PLANE, "inferred_from_spans",
                        "Lab session id, no model invoked"
                        + (f"; also performed {discovery} gateway handshake(s)"
                           if discovery else ""))

    if discovery:
        return Workload(
            WorkloadClass.TOOL_DISCOVERY, "inferred_from_spans",
            f"no model invoked; {discovery} gateway tool-catalog handshake(s)"
            + (" and a catalog session id" if run_id.startswith(CATALOG_SESSION_PREFIX) else ""))

    if any(name.startswith(CONTROL_PLANE_SPAN_PREFIXES) for name in names):
        return Workload(WorkloadClass.CONTROL_PLANE, "inferred_from_spans",
                        "no model invoked; only data-plane operations")

    return Workload(WorkloadClass.UNCLASSIFIED, "inferred_from_spans",
                    f"no model invoked and no recognised shape in {len(names)} span(s)")


def evaluators_for(workload: Workload) -> tuple:
    return tuple(EVALUATORS_BY_CLASS.get(workload.cls, ()))


def gating_for(workload: Workload) -> tuple:
    return tuple(GATING_BY_CLASS.get(workload.cls, ()))
