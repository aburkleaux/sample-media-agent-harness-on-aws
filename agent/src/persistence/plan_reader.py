"""Plan Reader — resolves the discovered workflow from the persisted plan (R1).

This module is the input "seam" of the `Persistence_Tool`: it turns the
harness's already-persisted `HighLevelPlan` into the normalized
`list[WorkflowStep]` the rest of the pipeline (resolve → assemble → validate →
save) consumes. The S3/DDB fetch (`read_plan`) is deliberately kept separate
from the pure normalization (`extract_steps`) so the normalization is directly
unit- and property-testable with no I/O — matching the house rule that pure
logic MUST NOT touch I/O.

`extract_steps` is the pure core landed here. It normalizes each `SubGoal` into
one `WorkflowStep` per suggested bare tool, emitting a single tool-less step
(`bare_tool=None`) for sub-goals that suggest no tool so they can be classified
as unmappable downstream (R2.3). It preserves every `sub_goal_id`, each
sub-goal's tool references, and its `depends_on` edges exactly, so the plan DAG
survives normalization (Property 8).

`read_plan` (the `Plan_State_Table` + S3 fetch, task 2.3) resolves the persisted
plan for a session. It reads the `(session_id, node_id="PLAN")` row to obtain the
`output_s3_key`, fetches that S3 object, and parses it via
`HighLevelPlan.from_json`. The DDB/S3 clients are injected via the constructor so
the boundary stays fakeable, and the synchronous boto3 calls are dispatched to a
worker thread with `asyncio.to_thread` per house style. S3/DDB are treated
strictly as an internal input source — the S3 location is never surfaced
downstream (R1.5): `read_plan` returns only the parsed `HighLevelPlan`. A missing
row/object raises `MissingPlanError` and an unparseable object raises
`PlanParseError`, both before any OMS contact.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
Properties: 8
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from botocore.exceptions import ClientError

from planning.high_level_plan import HighLevelPlan, SubGoal

from .models import (
    MissingPlanError,
    PlanParseError,
    WorkflowStep,
    compute_bare_tool_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private constants
# ---------------------------------------------------------------------------

#: Env var naming the DynamoDB `Plan_State_Table` (repo convention — see
#: `agent/src/planning_agent.py`, which writes the `PLAN` row there).
_ENV_PLAN_STATE_TABLE: str = "PLAN_STATE_TABLE"

#: Env var naming the harness S3 bucket holding the planning output (repo
#: convention — see `agent/src/planning_agent.py`).
_ENV_SKILLS_S3_BUCKET: str = "SKILLS_S3_BUCKET"

#: Sort-key value of the planning-output row in the `Plan_State_Table`.
_PLAN_NODE_ID: str = "PLAN"

#: S3 object holding the planning output, by convention (fallback when the DDB
#: row carries no explicit `output_s3_key`).
_PLAN_OUTPUT_KEY_TEMPLATE: str = "plans/{session_id}/planning/output.json"

#: S3 client error codes that mean "the object does not exist" (→ MissingPlan).
_S3_NOT_FOUND_CODES: frozenset[str] = frozenset({"NoSuchKey", "NoSuchBucket", "404"})


# ---------------------------------------------------------------------------
# Pure normalization (R1.2, Property 8)
# ---------------------------------------------------------------------------


def _steps_from_sub_goal(sub_goal: SubGoal) -> list[WorkflowStep]:
    """Normalize a single ``SubGoal`` into its ``WorkflowStep``(s).

    One step is emitted per suggested tool, preserving the sub-goal's id and
    ``depends_on`` edges. Each suggested tool is normalized to its *bare* name
    with the shared ``compute_bare_tool_name`` primitive so the step side of the
    resolver's join matches the (bare) capability side (R3.2/R3.5): planning
    emits namespaced gateway tool names (``audio-stem___separate_audio_stems``),
    which reduce to their bare suffix (``separate_audio_stems``), while an
    already-bare / separator-less tool (``separate_audio_stems``) is kept
    verbatim. A sub-goal that suggests no tool still yields exactly one step with
    ``bare_tool=None`` so it can be classified as unmappable later (R2.3).
    """
    # `depends_on` is copied per step so each WorkflowStep owns an independent
    # list — the source SubGoal is never mutated and edges are preserved exactly.
    depends_on = list(sub_goal.depends_on)

    if not sub_goal.suggested_tools:
        # Tool-less sub-goal (e.g. a pure agent/model/prompt node) — one step
        # with no tool reference so downstream resolution marks it unmappable.
        return [
            WorkflowStep(
                sub_goal_id=sub_goal.id,
                bare_tool=None,
                depends_on=list(depends_on),
                description=sub_goal.description,
            )
        ]

    # One step per suggested tool, in the plan's declared order. Normalize each
    # to its bare name using the SAME shared normalizer as the capability side;
    # ``or t`` keeps an already-bare (separator-less) tool verbatim, since
    # compute_bare_tool_name returns None for a separator-less name.
    return [
        WorkflowStep(
            sub_goal_id=sub_goal.id,
            bare_tool=compute_bare_tool_name(tool) or tool,
            depends_on=list(depends_on),
            description=sub_goal.description,
        )
        for tool in sub_goal.suggested_tools
    ]


def extract_steps(plan: HighLevelPlan) -> list[WorkflowStep]:
    """Normalize a ``HighLevelPlan`` into its ordered list of ``WorkflowStep``s.

    Pure (no I/O). Each ``SubGoal`` contributes one step per suggested bare
    tool; tool-less sub-goals contribute a single ``bare_tool=None`` step. The
    complete set of sub-goal ids, each sub-goal's tool references, and each
    sub-goal's ``depends_on`` edges are preserved exactly (R1.2, Property 8).

    Args:
        plan: The parsed Planning-agent output to normalize.

    Returns:
        The workflow steps in sub-goal declaration order, each sub-goal's steps
        contiguous and in suggested-tool order.
    """
    steps: list[WorkflowStep] = []
    for sub_goal in plan.sub_goals:
        steps.extend(_steps_from_sub_goal(sub_goal))
    return steps


# ---------------------------------------------------------------------------
# Plan Reader — pure normalization now; S3/DDB fetch lands in task 2.3
# ---------------------------------------------------------------------------


class PlanReader:
    """Resolves the persisted ``HighLevelPlan`` for a session and normalizes it.

    The DDB/S3 clients are injected (never constructed inline) so the I/O
    boundary is fakeable when ``read_plan`` lands in a later wave. The pure
    ``extract_steps`` normalization is available now and touches no I/O.
    """

    def __init__(
        self,
        *,
        ddb_client: Optional[Any] = None,
        s3_client: Optional[Any] = None,
        table_name: Optional[str] = None,
        bucket_name: Optional[str] = None,
    ) -> None:
        self._ddb_client = ddb_client
        self._s3_client = s3_client
        # Table/bucket names follow the repo convention (see planning_agent.py):
        # sourced from env vars unless explicitly injected (tests supply their own).
        self._table_name = table_name or os.environ.get(_ENV_PLAN_STATE_TABLE, "")
        self._bucket_name = bucket_name or os.environ.get(_ENV_SKILLS_S3_BUCKET, "")

    def extract_steps(self, plan: HighLevelPlan) -> list[WorkflowStep]:
        """Instance-level entrypoint delegating to the pure module function."""
        return extract_steps(plan)

    async def read_plan(self, session_id: str) -> HighLevelPlan:
        """Read + parse the persisted ``HighLevelPlan`` for ``session_id`` (R1).

        Reads the ``Plan_State_Table`` row ``(session_id, node_id="PLAN")`` to
        obtain ``output_s3_key``, fetches that S3 object, and parses it via
        ``HighLevelPlan.from_json``. The synchronous boto3 calls are dispatched
        to a worker thread with ``asyncio.to_thread`` so the AgentCore request
        handler stays cooperative; the DDB/S3 clients are the injected ones so
        the boundary stays fakeable.

        S3/DDB are strictly an internal input source: only the parsed plan is
        returned — the S3 location is never surfaced downstream (R1.5).

        Args:
            session_id: The session whose persisted planning output to load.

        Returns:
            The parsed ``HighLevelPlan``.

        Raises:
            MissingPlanError: No ``Plan_State_Table`` row or no S3 object exists
                for the session (R1.3). Raised before any OMS contact.
            PlanParseError: The S3 object is not a valid ``HighLevelPlan``
                (R1.4). Raised before any OMS contact.
        """
        output_s3_key = await self._read_output_key(session_id)
        body = await self._read_s3_object(session_id, output_s3_key)
        return self._parse_plan(session_id, body)

    # ------------------------------------------------------------------
    # I/O helpers — each wraps a single synchronous boto3 call in a thread
    # ------------------------------------------------------------------

    async def _read_output_key(self, session_id: str) -> str:
        """Fetch ``output_s3_key`` from the ``PLAN`` row, or raise MissingPlan.

        Falls back to the conventional ``plans/{session_id}/planning/output.json``
        key when the row exists but carries no explicit ``output_s3_key`` (older
        rows), matching how the Planning agent lays the object down.
        """
        table = self._ddb_client.Table(self._table_name)

        def _do_get() -> Optional[dict[str, Any]]:
            resp = table.get_item(
                Key={"session_id": session_id, "node_id": _PLAN_NODE_ID}
            )
            item = resp.get("Item")
            if item:
                return item
            # Plan rows are now written as "PLAN#<plan_id>" so a session can hold more than
            # one; the literal "PLAN" was a fixed sort key and a second plan overwrote the
            # first. An exact get cannot find a qualified key, so fall back to a scan of this
            # session's rows and take the most recently updated plan row.
            try:
                from boto3.dynamodb.conditions import Key as _Key

                resp = table.query(
                    KeyConditionExpression=_Key("session_id").eq(session_id)
                )
            except Exception:  # noqa: BLE001 - a reader must not raise over a fallback
                return None
            plan_rows = [
                r for r in resp.get("Items", [])
                if str(r.get("node_id", "")).startswith(f"{_PLAN_NODE_ID}#")
            ]
            if not plan_rows:
                return None
            plan_rows.sort(key=lambda r: str(r.get("updated_at", "")), reverse=True)
            return plan_rows[0]

        item = await asyncio.to_thread(_do_get)
        if not item:
            # No planning output row at all — nothing to persist (R1.3). No OMS.
            raise MissingPlanError(session_id)

        output_s3_key = item.get("output_s3_key")
        if not output_s3_key:
            output_s3_key = _PLAN_OUTPUT_KEY_TEMPLATE.format(session_id=session_id)
        return output_s3_key

    async def _read_s3_object(self, session_id: str, key: str) -> bytes:
        """Fetch the S3 object bytes, mapping a missing object to MissingPlan."""

        def _do_get() -> bytes:
            resp = self._s3_client.get_object(Bucket=self._bucket_name, Key=key)
            return resp["Body"].read()

        try:
            return await asyncio.to_thread(_do_get)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _S3_NOT_FOUND_CODES:
                # Row pointed at an object that isn't there (R1.3). No OMS.
                raise MissingPlanError(session_id) from exc
            raise

    def _parse_plan(self, session_id: str, body: bytes) -> HighLevelPlan:
        """Parse raw S3 bytes into a ``HighLevelPlan`` or raise PlanParseError."""
        try:
            text = body.decode("utf-8")
            return HighLevelPlan.from_json(text)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            # Invalid / non-conforming planning output (R1.4). No OMS contact.
            raise PlanParseError(
                f"planning output for session {session_id!r} is not a valid "
                f"HighLevelPlan: {exc}",
                session_id=session_id,
            ) from exc


__all__ = ["PlanReader", "extract_steps"]
