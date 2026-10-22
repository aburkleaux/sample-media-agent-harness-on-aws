"""WorkflowCRUD — CRUD operations for Workflow_Draft assets in the MediaWorks Lab.

Workflows compose specialty agents, skills, and tool collections in an ordered
sequence of steps. Each step binds an agent reference, optional skill, optional
tool collection, a prompt template, and explicit input dependencies on prior steps.

S3 key layout:
    {actor_id}/drafts/workflows/{assetId}/workflow.json

Validation rules on create/update (Requirements 5.2–5.5; Property 11):
- Each stepId is unique within the workflow.
- agent.kind is "core" (only "task-agent" currently valid) or "specialty"
  (then assetId must resolve to a Specialty_Agent_Draft owned by the requesting actorId).
- skill.assetId, when non-null, must resolve to a Skill_Draft owned by the requesting actorId.
- toolCollection.assetId, when non-null, must resolve to a Tool_Collection_Draft
  owned by the requesting actorId.
- Every inputs[i] must equal a stepId that appears earlier in the steps array.
- If both skill.assetId and a specialty agent.assetId are non-null, then the skill's
  assetId must appear in that specialty's compatibleSkills list.

When validation fails, no S3 write and no Asset_Index mutation occurs (Property 11).

Delete is two-phase: soft-delete the Asset_Index entry (status=archived),
then delete_prefix on S3 content.

Cross-actor access returns not_found (no existence disclosure).

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
Properties: 5, 6, 11, 12
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from botocore.exceptions import ClientError

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
    make_sk_owned,
)
from .context import LabRequestContext
from .errors import DeprecatedReferenceError, NotFoundOrForbidden
from .models import Workflow, WorkflowStep
from .path_validator import derive_record_prefix
from .s3_user_files_tool import S3UserFilesTool

if TYPE_CHECKING:
    from .models import Scope
    from .operation_results import Promoted, Shared, Deprecated, Cloned, RolledBack

logger = logging.getLogger(__name__)

# Constants
_CATEGORY = "workflow"
_SKILL_CATEGORY = "skill"
_SPECIALTY_CATEGORY = "specialty-agent"
_TOOL_COLLECTION_CATEGORY = "tool-collection"
_WORKFLOW_FILENAME = "workflow.json"
_DRAFTS_PREFIX = "drafts/workflows"

# The only valid core agent ref
_VALID_CORE_AGENT_REF = "task-agent"

# Asset_Ref grammar (Requirement 11.1):
#
#     <category>/<publishedId>@v<version>
#
# where ``category`` is one of the four singular AssetIndex categories,
# ``publishedId`` is the lineage identifier (uuid-shaped but not strictly
# constrained), and ``version`` is a positive integer. Anything that does
# not match this grammar is not an Asset_Ref and is left alone — bare
# core-agent refs (e.g. ``task-agent``) and pre-migration draft asset_ids
# pass through untouched.
_ASSET_REF_PATTERN = re.compile(
    r"^(?P<category>skill|specialty-agent|tool-collection|workflow)"
    r"/(?P<published_id>[A-Za-z0-9_-]+)"
    r"@v(?P<version>\d+)$"
)


def _parse_asset_ref(value: object) -> Optional[tuple[str, str, int]]:
    """Parse an Asset_Ref string of the form
    ``<category>/<publishedId>@v<version>``.

    Returns the ``(category, published_id, version)`` tuple on a match,
    or ``None`` for any non-string value or string that does not match
    the grammar. The version segment must be a positive integer (``v0``
    is rejected because version numbering starts at 1; see Requirement
    2.5).

    Bare core-agent refs such as ``"task-agent"`` and pre-migration
    plain ``assetId`` values are intentionally non-matching and skipped
    by the resolvability gate — only strings matching the canonical
    Asset_Ref form participate in Requirement 11's resolution.
    """
    if not isinstance(value, str):
        return None
    match = _ASSET_REF_PATTERN.match(value)
    if match is None:
        return None
    version = int(match.group("version"))
    if version <= 0:
        return None
    return (match.group("category"), match.group("published_id"), version)


@dataclass
class ResolvedStepView:
    """Resolved view of a single workflow step with named references."""

    step_id: str
    agent_kind: str
    agent_name: Optional[str]
    agent_asset_id: Optional[str]
    agent_resolved: bool
    skill_name: Optional[str]
    skill_asset_id: Optional[str]
    skill_resolved: bool
    tool_collection_name: Optional[str]
    tool_collection_asset_id: Optional[str]
    tool_collection_resolved: bool
    prompt: str
    inputs: list[str]


@dataclass
class WorkflowReadResult:
    """Result of reading a Workflow_Draft."""

    asset_id: str
    name: str
    steps: list[WorkflowStep]
    resolved_view: list[ResolvedStepView]
    metadata: dict


@dataclass
class WorkflowCreateResult:
    """Result of creating a Workflow_Draft."""

    asset_id: str
    s3_prefix: str


class WorkflowCRUD:
    """CRUD operations for Workflow_Draft assets.

    Uses S3UserFilesTool for file operations and AssetIndex for metadata.
    All operations are scoped to the actor_id bound in the provided context.

    Validation on create/update ensures all references resolve and input
    ordering is correct. When validation fails, no S3 write and no
    Asset_Index mutation occurs (Property 11).

    Args:
        s3: The S3UserFilesTool instance (already bound to actor_id).
        index: The AssetIndex instance (already bound to actor_id).
    """

    def __init__(self, s3: S3UserFilesTool, index: AssetIndex) -> None:
        self._s3 = s3
        self._index = index

    async def _validate_steps(
        self, ctx: LabRequestContext, steps: list[WorkflowStep]
    ) -> None:
        """Validate all workflow steps against the rules in Requirements 5.2–5.5.

        Checks:
        1. Each stepId is unique within the workflow.
        2. agent.kind is "core" (only "task-agent" valid) or "specialty"
           (assetId must resolve to a Specialty_Agent_Draft owned by same actor).
        3. skill.assetId, when non-null, must resolve to a Skill_Draft owned by same actor.
        4. toolCollection.assetId, when non-null, must resolve to a Tool_Collection_Draft
           owned by same actor.
        5. Every inputs[i] must equal a stepId that appears earlier in the steps array.
        6. If both skill.assetId and specialty agent.assetId are non-null, the skill
           must appear in that specialty's compatibleSkills list.

        Args:
            ctx: The request context (provides actor_id used to scope draft lookups).
            steps: List of WorkflowStep objects to validate.

        Raises:
            ValueError: If any validation rule is violated, with details
                identifying each violation.
        """
        errors: list[str] = []
        # Drafts referenced by these steps live under the actor's drafts
        # partition; resolve every (specialty, skill, tool-collection)
        # asset_id via PK = USER#<actor>#DRAFTS, SK = <category>#<id>#draft.
        # Tool collections are flat (no version sub-tier) but still live
        # under the drafts partition with a "draft" status during the
        # authoring phase, matching the rest of the lab CRUD layer.
        drafts_pk = make_pk_user_owned(ctx.actor_id)

        # 1. Check stepId uniqueness
        seen_step_ids: list[str] = []
        duplicate_ids: set[str] = set()
        for step in steps:
            if step.step_id in seen_step_ids:
                duplicate_ids.add(step.step_id)
            seen_step_ids.append(step.step_id)

        if duplicate_ids:
            errors.append(
                f"Duplicate stepId(s): {', '.join(sorted(duplicate_ids))}"
            )

        # Validate each step
        for i, step in enumerate(steps):
            step_prefix = f"Step '{step.step_id}' (index {i})"

            # 2. Validate agent reference
            if step.agent.kind == "core":
                # Only "task-agent" is currently valid for core agents
                if step.agent.ref != _VALID_CORE_AGENT_REF:
                    errors.append(
                        f"{step_prefix}: agent.kind='core' but ref='{step.agent.ref}' "
                        f"is not a valid core agent (only '{_VALID_CORE_AGENT_REF}' is valid)"
                    )
            elif step.agent.kind == "specialty":
                # assetId must resolve to a Specialty_Agent_Draft owned by same actor
                if not step.agent.asset_id:
                    errors.append(
                        f"{step_prefix}: agent.kind='specialty' requires a non-null assetId"
                    )
                else:
                    specialty_record = await self._index.get(
                        drafts_pk,
                        make_sk_owned(_SPECIALTY_CATEGORY, step.agent.asset_id),
                    )
                    if specialty_record is None:
                        errors.append(
                            f"{step_prefix}: agent assetId '{step.agent.asset_id}' "
                            f"does not resolve to a Specialty_Agent_Draft owned by "
                            f"the requesting actor"
                        )
                    elif specialty_record.status != "draft":
                        errors.append(
                            f"{step_prefix}: agent assetId '{step.agent.asset_id}' "
                            f"is not an active Specialty_Agent_Draft"
                        )
            else:
                errors.append(
                    f"{step_prefix}: agent.kind must be 'core' or 'specialty', "
                    f"got '{step.agent.kind}'"
                )

            # 3. Validate skill reference (when non-null)
            if step.skill.asset_id is not None:
                skill_record = await self._index.get(
                    drafts_pk,
                    make_sk_owned(_SKILL_CATEGORY, step.skill.asset_id),
                )
                if skill_record is None:
                    errors.append(
                        f"{step_prefix}: skill assetId '{step.skill.asset_id}' "
                        f"does not resolve to a Skill_Draft owned by the requesting actor"
                    )
                elif skill_record.status != "draft":
                    errors.append(
                        f"{step_prefix}: skill assetId '{step.skill.asset_id}' "
                        f"is not an active Skill_Draft"
                    )

            # 4. Validate tool collection reference (when non-null)
            if step.tool_collection.asset_id is not None:
                tc_record = await self._index.get(
                    drafts_pk,
                    make_sk_owned(
                        _TOOL_COLLECTION_CATEGORY, step.tool_collection.asset_id
                    ),
                )
                if tc_record is None:
                    errors.append(
                        f"{step_prefix}: toolCollection assetId "
                        f"'{step.tool_collection.asset_id}' does not resolve to a "
                        f"Tool_Collection_Draft owned by the requesting actor"
                    )
                elif tc_record.status != "draft":
                    errors.append(
                        f"{step_prefix}: toolCollection assetId "
                        f"'{step.tool_collection.asset_id}' is not an active "
                        f"Tool_Collection_Draft"
                    )

            # 5. Validate input dependencies (must reference earlier steps)
            earlier_step_ids = seen_step_ids[:i]
            for input_ref in step.inputs:
                if input_ref not in earlier_step_ids:
                    errors.append(
                        f"{step_prefix}: input '{input_ref}' does not refer to "
                        f"a stepId that appears earlier in the steps array"
                    )

            # 6. Validate skill/specialty pairing
            if (
                step.skill.asset_id is not None
                and step.agent.kind == "specialty"
                and step.agent.asset_id is not None
            ):
                # The skill must appear in the specialty's compatibleSkills list.
                # Re-fetch the specialty record (the previous fetch may have
                # produced a None or a non-draft row; either way we already
                # surfaced an error, so the lookup here either confirms the
                # pairing or no-ops harmlessly).
                specialty_record = await self._index.get(
                    drafts_pk,
                    make_sk_owned(_SPECIALTY_CATEGORY, step.agent.asset_id),
                )
                if (
                    specialty_record is not None
                    and specialty_record.status == "draft"
                ):
                    compatible_skills = getattr(
                        specialty_record, "compatible_skills", []
                    )
                    if step.skill.asset_id not in compatible_skills:
                        errors.append(
                            f"{step_prefix}: skill '{step.skill.asset_id}' is not in "
                            f"the compatibleSkills list of specialty agent "
                            f"'{step.agent.asset_id}'"
                        )

        if errors:
            raise ValueError(
                "Workflow validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    def _build_workflow_json(
        self, name: str, steps: list[WorkflowStep]
    ) -> bytes:
        """Serialize a workflow.json document from name and steps.

        Args:
            name: The workflow name.
            steps: List of WorkflowStep objects.

        Returns:
            UTF-8 encoded JSON bytes.
        """
        doc = {
            "name": name,
            "steps": [
                {
                    "stepId": step.step_id,
                    "agent": {
                        "kind": step.agent.kind,
                        **({"ref": step.agent.ref} if step.agent.ref else {}),
                        **({"assetId": step.agent.asset_id} if step.agent.asset_id else {}),
                    },
                    "skill": {"assetId": step.skill.asset_id},
                    "toolCollection": {"assetId": step.tool_collection.asset_id},
                    "prompt": step.prompt,
                    "inputs": step.inputs,
                }
                for step in steps
            ],
        }
        return json.dumps(doc, indent=2).encode("utf-8")

    def _parse_workflow_json(
        self, data: bytes
    ) -> tuple[str, list[WorkflowStep]]:
        """Parse a workflow.json document into name and steps.

        Args:
            data: UTF-8 encoded JSON bytes.

        Returns:
            Tuple of (name, steps).
        """
        from .models import AgentRef, SkillRef, ToolCollectionRef

        doc = json.loads(data.decode("utf-8"))
        name = doc.get("name", "")
        raw_steps = doc.get("steps", [])

        steps: list[WorkflowStep] = []
        for raw_step in raw_steps:
            agent_data = raw_step.get("agent", {})
            skill_data = raw_step.get("skill", {})
            tc_data = raw_step.get("toolCollection", {})

            agent_ref = AgentRef(
                kind=agent_data.get("kind", "core"),
                ref=agent_data.get("ref"),
                asset_id=agent_data.get("assetId"),
            )
            skill_ref = SkillRef(asset_id=skill_data.get("assetId"))
            tc_ref = ToolCollectionRef(asset_id=tc_data.get("assetId"))

            step = WorkflowStep(
                step_id=raw_step.get("stepId", ""),
                agent=agent_ref,
                skill=skill_ref,
                tool_collection=tc_ref,
                prompt=raw_step.get("prompt", ""),
                inputs=raw_step.get("inputs", []),
            )
            steps.append(step)

        return name, steps

    async def _resolve_step_view(
        self, ctx: LabRequestContext, step: WorkflowStep
    ) -> ResolvedStepView:
        """Build a resolved view for a single workflow step.

        Resolves each agent, skill, and tool collection reference against the
        Asset_Index. Marks deleted or missing references as unresolved
        (Requirement 5.7, Property 12).

        Args:
            ctx: The request context (provides actor_id used to scope draft lookups).
            step: The WorkflowStep to resolve.

        Returns:
            A ResolvedStepView with resolution status for each reference.
        """
        drafts_pk = make_pk_user_owned(ctx.actor_id)

        # Resolve agent
        agent_name: Optional[str] = None
        agent_resolved = True

        if step.agent.kind == "core":
            agent_name = step.agent.ref or _VALID_CORE_AGENT_REF
            agent_resolved = True
        elif step.agent.kind == "specialty" and step.agent.asset_id:
            specialty_record = await self._index.get(
                drafts_pk,
                make_sk_owned(_SPECIALTY_CATEGORY, step.agent.asset_id),
            )
            if specialty_record is not None and specialty_record.status == "draft":
                agent_name = specialty_record.name
                agent_resolved = True
            else:
                agent_name = None
                agent_resolved = False

        # Resolve skill
        skill_name: Optional[str] = None
        skill_resolved = True

        if step.skill.asset_id is not None:
            skill_record = await self._index.get(
                drafts_pk,
                make_sk_owned(_SKILL_CATEGORY, step.skill.asset_id),
            )
            if skill_record is not None and skill_record.status == "draft":
                skill_name = skill_record.name
                skill_resolved = True
            else:
                skill_name = None
                skill_resolved = False

        # Resolve tool collection
        tc_name: Optional[str] = None
        tc_resolved = True

        if step.tool_collection.asset_id is not None:
            tc_record = await self._index.get(
                drafts_pk,
                make_sk_owned(
                    _TOOL_COLLECTION_CATEGORY, step.tool_collection.asset_id
                ),
            )
            if tc_record is not None and tc_record.status == "draft":
                tc_name = tc_record.name
                tc_resolved = True
            else:
                tc_name = None
                tc_resolved = False

        return ResolvedStepView(
            step_id=step.step_id,
            agent_kind=step.agent.kind,
            agent_name=agent_name,
            agent_asset_id=step.agent.asset_id,
            agent_resolved=agent_resolved,
            skill_name=skill_name,
            skill_asset_id=step.skill.asset_id,
            skill_resolved=skill_resolved,
            tool_collection_name=tc_name,
            tool_collection_asset_id=step.tool_collection.asset_id,
            tool_collection_resolved=tc_resolved,
            prompt=step.prompt,
            inputs=step.inputs,
        )

    async def create(
        self,
        ctx: LabRequestContext,
        name: str,
        steps: list[WorkflowStep],
    ) -> WorkflowCreateResult:
        """Create a new Workflow_Draft.

        Validates all references, input ordering, and skill/specialty pairing.
        On validation success, writes workflow.json to S3 and records the draft
        in the Asset_Index. On validation failure, no S3 write and no
        Asset_Index mutation occurs (Property 11).

        Args:
            ctx: The request context (provides actor_id).
            name: User-supplied workflow name.
            steps: Ordered list of WorkflowStep objects (may be empty).

        Returns:
            WorkflowCreateResult with the assigned asset_id and s3_prefix.

        Raises:
            ValueError: If any validation rule is violated.
        """
        # Requirement 5.2–5.5: validate all steps before any write
        if steps:
            await self._validate_steps(ctx, steps)

        # Assign unique asset_id
        asset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Canonical S3 prefix as recorded in the AssetIndex row. The
        # actor-scoped S3UserFilesTool used below auto-prepends the
        # actor id when resolving relative keys, so writes go to
        # <actor_id>/drafts/workflows/<asset_id>/...; the AssetIndex
        # `s3_prefix` field carries the canonical bucket-absolute form
        # produced by `derive_record_prefix`.
        s3_prefix = derive_record_prefix(
            scope="private",
            owner_user_id=ctx.actor_id,
            owner_group_id=None,
            owner_org_id=None,
            category=_CATEGORY,
            asset_id=asset_id,
            version=None,
            status="draft",
        )
        # Write workflow.json at the canonical grammar prefix (bucket-absolute)
        # so the physical location matches the recorded ``s3_prefix`` that
        # promote/clone/share copy from and consumers read (BUG-2).
        workflow_key = f"{s3_prefix}{_WORKFLOW_FILENAME}"

        # Write workflow.json to S3
        workflow_data = self._build_workflow_json(name, steps)
        await self._s3.write_absolute(workflow_key, workflow_data)

        # Record in Asset_Index (Requirement 5.1) as a typed Workflow
        # draft. AssetIndex.put computes PK = USER#<actor>#DRAFTS,
        # SK = workflow#<asset_id>#draft and validates the prefix.
        workflow = Workflow(
            asset_id=asset_id,
            category=_CATEGORY,
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            steps=list(steps) if steps else [],
        )
        await self._index.put(workflow, owner_user_id=ctx.actor_id)

        logger.info(
            "Created workflow draft: asset_id=%s, name=%s, "
            "step_count=%d, actor=%s",
            asset_id,
            name,
            len(steps),
            ctx.actor_id,
        )

        return WorkflowCreateResult(asset_id=asset_id, s3_prefix=s3_prefix)

    async def read(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[WorkflowReadResult]:
        """Read a Workflow_Draft by asset_id.

        Returns the stored workflow definition plus a resolved view that names
        each referenced agent, skill, and tool collection. Deleted references
        are marked as unresolved in the resolved view (Requirement 5.7,
        Property 12). The stored workflow.json is never rewritten on read.

        Returns None if the draft does not exist or belongs to a different actor
        (no existence disclosure).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.

        Returns:
            WorkflowReadResult if found and owned by this actor, else None.
        """
        # Cross-actor access returns not_found: the draft only exists
        # under the requesting actor's drafts partition.
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Read workflow.json from the record's canonical prefix (bucket-absolute).
        workflow_key = f"{record.s3_prefix}{_WORKFLOW_FILENAME}"
        try:
            workflow_bytes = await self._s3.read_absolute(workflow_key)
            workflow_name, steps = self._parse_workflow_json(workflow_bytes)
        except FileNotFoundError:
            # workflow.json missing — shouldn't happen for a valid draft
            workflow_name = record.name
            steps = []

        # Build resolved view (Requirement 5.6, 5.7; Property 12)
        resolved_view = [
            await self._resolve_step_view(ctx, step) for step in steps
        ]

        return WorkflowReadResult(
            asset_id=asset_id,
            name=workflow_name,
            steps=steps,
            resolved_view=resolved_view,
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
            },
        )

    async def update(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        name: Optional[str] = None,
        steps: Optional[list[WorkflowStep]] = None,
    ) -> Optional[dict]:
        """Update an existing Workflow_Draft.

        Updates the workflow name and/or steps. Full re-validation is performed
        on steps change (Requirements 5.2–5.5). When validation fails, no S3
        write and no Asset_Index mutation occurs (Property 11).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.
            name: Optional new workflow name.
            steps: Optional new ordered list of WorkflowStep objects. If provided,
                full re-validation is performed.

        Returns:
            Dict with updatedAt if successful, None if not found.

        Raises:
            ValueError: If any validation rule is violated on the new steps.
        """
        # Verify ownership: cross-actor access surfaces as not-found.
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Requirement 5.2–5.5: full re-validation on steps change. We
        # intentionally validate BEFORE any S3 write so a malformed
        # update leaves both stores untouched (Property 11).
        if steps is not None and steps:
            await self._validate_steps(ctx, steps)

        # Determine effective name and steps for the workflow.json
        effective_name = name if name is not None else record.name

        if steps is None:
            # Read current workflow.json (canonical prefix) to get current steps
            workflow_key = f"{record.s3_prefix}{_WORKFLOW_FILENAME}"
            try:
                workflow_bytes = await self._s3.read_absolute(workflow_key)
                _, current_steps = self._parse_workflow_json(workflow_bytes)
            except FileNotFoundError:
                current_steps = []
            effective_steps = current_steps
        else:
            effective_steps = steps

        # Write updated workflow.json at the record's canonical prefix.
        workflow_key = f"{record.s3_prefix}{_WORKFLOW_FILENAME}"
        workflow_data = self._build_workflow_json(effective_name, effective_steps)
        await self._s3.write_absolute(workflow_key, workflow_data)

        # Build the AssetIndex update expression. updatedAt is always
        # bumped; name is updated only when the caller supplied a new
        # value. Both are written as a single SET expression so the
        # update is one round-trip.
        now = int(time.time() * 1000)
        update_parts: list[str] = ["updatedAt = :updatedAt"]
        attr_values: dict[str, object] = {":updatedAt": now}
        attr_names: dict[str, str] = {}
        if name is not None:
            update_parts.append("#name = :name")
            attr_values[":name"] = name
            # `name` is a DynamoDB reserved word; use a placeholder.
            attr_names["#name"] = "name"

        update_kwargs: dict[str, object] = {
            "update_expression": "SET " + ", ".join(update_parts),
            "expression_attribute_values": attr_values,
        }
        if attr_names:
            update_kwargs["expression_attribute_names"] = attr_names

        try:
            await self._index.update(pk, sk, **update_kwargs)
        except ClientError as err:
            # If the row was concurrently deleted between the get above
            # and this update, surface as not-found rather than a 5xx.
            if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise

        logger.info(
            "Updated workflow draft: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return {"updatedAt": now}

    async def delete(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[bool]:
        """Delete a Workflow_Draft using a hard-delete in two phases.

        Phase 1: Hard-delete the AssetIndex row via ``transact_write``
        with an ``attribute_exists(PK)`` precondition so a missing row
        (or one owned by a different actor) surfaces as not-found
        rather than a 5xx.
        Phase 2: ``delete_prefix`` the S3 artifacts under the workflow's
        draft prefix.

        Returns None if the draft does not exist or belongs to a different actor
        (no existence disclosure).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.

        Returns:
            True if successfully deleted, None if not found.
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)

        # Capture the canonical prefix before deleting the row so Phase 2 can
        # reclaim the correct (bucket-absolute) location where artifacts now
        # live (BUG-2). Cross-actor access returns None here (no disclosure).
        existing = await self._index.get(pk, sk)
        prefix_to_reclaim = getattr(existing, "s3_prefix", None) if existing else None

        # Phase 1: hard-delete the AssetIndex row (Requirement 5.8). The
        # condition ensures we only return success when a row actually
        # existed; a concurrent delete or a cross-actor lookup surfaces
        # as not-found.
        ops = [
            {
                "Delete": {
                    "TableName": self._index.table_name,
                    "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            }
        ]
        try:
            await self._index.transact_write(ops)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            # TransactionCanceledException with a ConditionalCheckFailed
            # reason is the canonical signal that the row didn't exist.
            # We also tolerate a bare ConditionalCheckFailedException in
            # case the underlying mock surfaces it directly.
            if code == "TransactionCanceledException":
                reasons = err.response.get("CancellationReasons", [])
                if any(
                    isinstance(r, dict)
                    and r.get("Code") == "ConditionalCheckFailed"
                    for r in reasons
                ):
                    return None
                raise
            if code == "ConditionalCheckFailedException":
                return None
            raise

        # Phase 2: Delete all S3 objects under the workflow's canonical
        # (bucket-absolute) prefix, matching where create/update write
        # artifacts (BUG-2). Nothing to reclaim if the row was already gone.
        if prefix_to_reclaim:
            await self._s3.delete_prefix_absolute(prefix_to_reclaim)

        logger.info(
            "Deleted workflow draft: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return True

    async def list(self, ctx: LabRequestContext) -> list[Workflow]:
        """List all Workflow_Drafts owned by the requesting actor.

        Queries the actor's drafts partition with an SK prefix scoped to
        the workflow category, returning every typed Workflow draft.
        Pagination is intentionally hidden — the lab UI lists drafts as
        a flat panel and a single user is unlikely to exceed the first
        page; if that changes, expose ``last_evaluated_key`` here.

        Args:
            ctx: The request context (provides actor_id).

        Returns:
            List of typed :class:`Workflow` drafts.
        """
        pk = make_pk_user_owned(ctx.actor_id)
        items, _ = await self._index.query_pk(
            pk, sk_prefix=f"{_CATEGORY}#"
        )
        return items

    async def promote(
        self,
        ctx: LabRequestContext,
        draft_id: str,
        scope: "Scope",
        group_ids: Optional[list[str]] = None,
        published_id: Optional[str] = None,
        *,
        org_id: Optional[str] = None,
        publish_policy_owner_ids: Optional[list[str]] = None,
    ) -> "Promoted":
        """Promote a Workflow_Draft into a new Published_Record.

        Walks every step's Asset_Ref of the form
        ``<category>/<publishedId>@v<version>`` (Requirement 11.1),
        resolves each lineage via :meth:`AssetIndex.query_lineage`, and
        rejects the Promote BEFORE the S3 copy and the AssetIndex
        ``TransactWriteItems`` if any reference fails to resolve or
        points at a deprecated published version (Requirements 11.2,
        11.3, Property 9). Resolution pins to the exact
        ``(publishedId, version)`` named in the ref and ignores
        ``current`` for that lineage (Requirement 11.4).

        Once the resolvability gate passes, dispatches to
        :func:`lab._canonical_ops.promote_asset` for the lifecycle
        mechanics (S3 copy, transactional Put + current-pointer flip
        + draft.lastPublishedAs update, compensating delete on cancel).

        Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 11.1, 11.2, 11.3, 11.4
        Properties: 3, 4, 9

        Raises:
            NotFoundOrForbidden: The draft does not exist or is not
                owned by ``ctx.actor_id``; or any Asset_Ref in the
                workflow's steps fails to resolve to a published row.
            DeprecatedReferenceError: Any Asset_Ref in the workflow's
                steps points at a published version whose
                ``status == "deprecated"``.
            ValueError: Scope/group_ids/org_id combination is
                malformed (delegated to ``promote_asset``).
            ConflictError / CompensationFailedError: As documented on
                :func:`lab._canonical_ops.promote_asset`.
        """
        from ._canonical_ops import promote_asset

        # Resolve all Asset_Refs BEFORE any S3 copy or AssetIndex
        # mutation. Failure here surfaces a clean validation error to
        # the caller and leaves AssetIndex and S3 untouched (Property 9).
        await self._validate_asset_refs(ctx, draft_id)

        return await promote_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            draft_id=draft_id,
            scope=scope,
            group_ids=group_ids,
            published_id=published_id,
            org_id=org_id,
            publish_policy_owner_ids=publish_policy_owner_ids,
        )

    async def _validate_asset_refs(
        self, ctx: LabRequestContext, draft_id: str
    ) -> None:
        """Walk the workflow draft's steps and resolve every Asset_Ref.

        Reads the draft's ``workflow.json`` from the actor-scoped S3
        prefix, scans every step's ``agent.ref``, ``agent.assetId``,
        ``skill.assetId``, and ``toolCollection.assetId`` for strings
        of the canonical Asset_Ref form
        ``<category>/<publishedId>@v<version>``, and resolves each
        unique ``publishedId`` via :meth:`AssetIndex.query_lineage`.

        For each Asset_Ref:

          * If the lineage has no row at the named ``version``, raise
            :class:`NotFoundOrForbidden` (the workflow author is
            referencing a published version that does not exist).
          * If the lineage's row at the named ``version`` has
            ``status == "deprecated"``, raise
            :class:`DeprecatedReferenceError` (Requirement 11.3).
          * Otherwise the ref pins exactly to that row regardless of
            whether it is ``current`` (Requirement 11.4).

        Bare ``task-agent`` refs and pre-migration ``assetId`` values
        that do not match the Asset_Ref grammar are left alone — they
        are not Asset_Refs in the sense of Requirement 11. Only strings
        matching the grammar are subject to the resolvability gate.

        ``query_lineage`` results are cached per ``publishedId`` for
        the duration of the validation pass so a workflow with N refs
        sharing M lineages issues exactly M GSI queries.

        Args:
            ctx: The request context (provides actor_id).
            draft_id: The workflow draft identifier.
        """
        # Read the draft's workflow.json. We deliberately do NOT use the
        # AssetIndex draft row here — that row carries metadata only,
        # not the steps payload (steps live in S3, per the design and
        # the existing read() implementation). Reading from S3 also
        # ensures we resolve the same bytes the canonical-op layer is
        # about to copy.
        workflow_path = (
            f"{_DRAFTS_PREFIX}/{draft_id}/{_WORKFLOW_FILENAME}"
        )
        try:
            workflow_bytes = await self._s3.read(workflow_path)
        except FileNotFoundError:
            # Empty/missing steps payload ⇒ no refs to resolve, gate
            # passes vacuously. The canonical-op layer below will
            # surface NotFoundOrForbidden if the AssetIndex draft row
            # is also absent.
            return

        try:
            doc = json.loads(workflow_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Malformed workflow.json: nothing to resolve. Surface as
            # a validation error so the user knows their draft is
            # corrupt rather than silently skipping the gate.
            raise ValueError(
                f"workflow draft {draft_id} has malformed workflow.json"
            )

        # Collect every (category, published_id, version, location)
        # tuple from the steps. We scan every string-valued slot that
        # may legitimately hold an Asset_Ref (`agent.ref`,
        # `agent.assetId`, `skill.assetId`, `toolCollection.assetId`)
        # so future refactors that move refs into different fields do
        # not silently bypass the gate.
        refs: list[tuple[str, str, int, str]] = []
        for step in doc.get("steps", []):
            step_id = step.get("stepId", "<unknown>")

            agent_data = step.get("agent") or {}
            for field_name in ("ref", "assetId"):
                value = agent_data.get(field_name)
                parsed = _parse_asset_ref(value)
                if parsed is not None:
                    cat, pid, ver = parsed
                    refs.append(
                        (cat, pid, ver, f"step '{step_id}' agent.{field_name}")
                    )

            skill_data = step.get("skill") or {}
            value = skill_data.get("assetId")
            parsed = _parse_asset_ref(value)
            if parsed is not None:
                cat, pid, ver = parsed
                refs.append(
                    (cat, pid, ver, f"step '{step_id}' skill.assetId")
                )

            tc_data = step.get("toolCollection") or {}
            value = tc_data.get("assetId")
            parsed = _parse_asset_ref(value)
            if parsed is not None:
                cat, pid, ver = parsed
                refs.append(
                    (cat, pid, ver, f"step '{step_id}' toolCollection.assetId")
                )

        if not refs:
            return

        # Resolve each unique publishedId once; pin every ref to its
        # exact (publishedId, version) pair via the cached lineage.
        lineage_cache: dict[str, list] = {}
        for category, published_id, version, location in refs:
            if published_id not in lineage_cache:
                lineage_cache[published_id] = await self._index.query_lineage(
                    published_id
                )
            lineage = lineage_cache[published_id]

            # Pin to the exact (publishedId, version) pair, ignoring
            # current — Requirement 11.4. The category segment of the
            # Asset_Ref must match the lineage's category; a mismatch
            # is treated as not-found because the caller's ref is
            # internally inconsistent.
            target = next(
                (
                    r
                    for r in lineage
                    if r.version == version and r.category == category
                ),
                None,
            )
            if target is None:
                raise NotFoundOrForbidden(
                    f"{location}: Asset_Ref "
                    f"'{category}/{published_id}@v{version}' does not "
                    f"resolve to a published version"
                )
            if target.status == "deprecated":
                raise DeprecatedReferenceError(
                    category=category,
                    published_id=published_id,
                    version=version,
                    step_location=location,
                )


    async def share(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
        new_scope: "Scope",
        group_ids: Optional[list[str]] = None,
        *,
        org_id: Optional[str] = None,
    ) -> "Shared":
        """Widen a published Workflow_Record's scope.

        Thin wrapper over :func:`lab._canonical_ops.share_asset` with
        the workflow category constant. See that helper for the full
        sequence and error semantics (Authz, scope-narrowing rejection,
        cross-PK move, compensating-delete on transaction cancel,
        post-txn source-prefix cleanup).

        Note: Asset_Ref resolvability validation against the workflow's
        steps (Requirement 11) is implemented at Promote time; Share
        does not re-validate refs, since Share preserves the
        ``(published_id, version)`` identity and only widens scope.

        Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.1, 13.2, 13.3, 13.4
        Properties: 5, 6, 8
        """
        from ._canonical_ops import share_asset

        return await share_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
            new_scope=new_scope,
            group_ids=group_ids,
            org_id=org_id,
        )

    async def deprecate(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Deprecated":
        """Mark a published Workflow_Record as deprecated.

        Thin wrapper over :func:`lab._canonical_ops.deprecate_asset`
        with the workflow category constant. See that helper for the
        full sequence and error semantics.

        Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
        Properties: 3, 8
        """
        from ._canonical_ops import deprecate_asset

        return await deprecate_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )
    async def clone(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Cloned":
        """Clone a published Workflow_Record into a new Draft owned by the actor.

        Thin wrapper over :func:`lab._canonical_ops.clone_asset` with
        the workflow category constant. The cloned draft carries the
        source row's ``steps`` payload (Asset_Refs and prompts) so the
        actor can edit the workflow without re-authoring it.

        Note: Asset_Ref resolvability validation runs at Promote time
        (Task 3.6), not at Clone time; a clone may carry refs whose
        target lineages have since been deprecated, and the actor
        resolves that on the next Promote.

        Requirements: 7.1, 7.2, 7.3, 7.4
        """
        from ._canonical_ops import clone_asset

        return await clone_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )

    async def rollback(
        self,
        ctx: LabRequestContext,
        published_id: str,
        to_version: Optional[int] = None,
    ) -> "RolledBack":
        """Roll back the ``current`` pointer of a Workflow lineage.

        Thin wrapper over :func:`lab._canonical_ops.rollback_asset`
        with the workflow category constant. Rollback NEVER mutates
        ``status`` (Requirement 8.3) and does not touch S3 — it
        toggles the ``current`` flag between the prior current row
        and the resolved target version in a single
        ``TransactWriteItems``.

        Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
        Properties: 3, 8
        """
        from ._canonical_ops import rollback_asset

        return await rollback_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            to_version=to_version,
        )
