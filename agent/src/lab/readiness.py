"""PromotionReadiness — read-only validator for Workflow_Draft promotion eligibility.

Walks a Workflow_Draft and produces a ReadinessReport enumerating each
transitively referenced asset's readiness status. The check is strictly
read-only: no writes to S3, DDB, or memory (Requirement 8.10, Property 18).

Short-circuits on zero-step workflows (Requirement 8.4, Property 24):
returns a single entry with the zero-steps failure and skips all other checks.

Cross-asset references resolve through the unified-platform AssetIndex
single-table API: drafts live under ``USER#<actor>#DRAFTS`` keyed by
``<category>#<asset_id>#draft``, while tool-collection rows live under
``USER#<actor>#TOOL_COLLECTIONS`` keyed by ``tool-collection#<asset_id>``
(no version sub-tier; see :mod:`lab.asset_index`). Status gating uses the
new ``draft | published | deprecated`` domain — readiness operates on
drafts, so a record is treated as missing whenever ``record.status !=
"draft"``.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10
Properties: 16, 17, 18, 19, 24
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
    make_sk_owned,
)
from .context import LabRequestContext
from .gateway_browser import GatewayBrowser
from .models import (
    GatewayToolDescriptor,
    ReadinessEntry,
    ReadinessReport,
    ToolRef,
    WorkflowStep,
)
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)

# Constants
_WORKFLOW_CATEGORY = "workflow"
_SKILL_CATEGORY = "skill"
_SPECIALTY_CATEGORY = "specialty-agent"
_TOOL_COLLECTION_CATEGORY = "tool-collection"
_WORKFLOW_FILENAME = "workflow.json"
_DRAFTS_WORKFLOWS_PREFIX = "drafts/workflows"
_DRAFTS_SKILLS_PREFIX = "drafts/skills"

# Failure message constants
FAILURE_ZERO_STEPS = "Workflow has zero steps"
FAILURE_UNRESOLVED_AGENT = "Step '{step_id}': agent reference '{asset_id}' does not resolve"
FAILURE_UNRESOLVED_SKILL = "Step '{step_id}': skill reference '{asset_id}' does not resolve"
FAILURE_UNRESOLVED_TOOL_COLLECTION = (
    "Step '{step_id}': tool collection reference '{asset_id}' does not resolve"
)
FAILURE_OUT_OF_ORDER_INPUT = (
    "Step '{step_id}': input dependency '{input_ref}' does not appear earlier in the workflow"
)
FAILURE_INCOMPATIBLE_SKILL_SPECIALTY = (
    "Step '{step_id}': skill '{skill_id}' is not compatible with specialty '{specialty_id}'"
)
FAILURE_SKILL_MD_MISSING = "SKILL.md is missing"
FAILURE_SKILL_MD_EMPTY = "SKILL.md is empty"
FAILURE_SKILL_MD_NO_NAME = "SKILL.md does not contain a skill name declaration"
FAILURE_PERSONA_EMPTY = "Persona prompt is empty"
FAILURE_DANGLING_COMPATIBLE_SKILL = (
    "Compatible skill '{skill_id}' does not resolve under the requesting actor"
)
FAILURE_TOOL_COLLECTION_EMPTY = "Tool collection is empty"
FAILURE_TOOL_UNRESOLVED = (
    "Tool reference '{name}@{version}' does not resolve against the Gateway"
)


def _draft_pk(actor_id: str) -> str:
    """Render the drafts partition key for the requesting actor.

    Centralised so every cross-asset lookup in this module funnels
    through the same ``make_pk_user_owned`` call — readiness never
    queries another actor's partition.
    """
    return make_pk_user_owned(actor_id)


def _tool_collections_pk(actor_id: str) -> str:
    """Render the flat tool-collections partition key for the actor.

    Tool collections are stored under ``USER#<u>#TOOL_COLLECTIONS`` (no
    version sub-tier); the flat partition is the only place a
    tool-collection row exists in the new schema.
    """
    return make_pk_user_owned(actor_id)


class PromotionReadiness:
    """Read-only validator that checks whether a Workflow_Draft is promotion-ready.

    Evaluates the workflow and all transitively referenced assets against the
    rules in Requirements 8.3–8.7. Produces a ReadinessReport listing each
    asset's status and failures.

    This class performs NO writes to S3, DDB, or memory (Requirement 8.10).

    Args:
        s3: The S3UserFilesTool instance (already bound to actor_id).
        index: The AssetIndex instance.
        gateway_browser: The GatewayBrowser for resolving tool references.
    """

    def __init__(
        self,
        s3: S3UserFilesTool,
        index: AssetIndex,
        gateway_browser: GatewayBrowser,
    ) -> None:
        self._s3 = s3
        self._index = index
        self._gateway_browser = gateway_browser

    async def check(
        self, ctx: LabRequestContext, workflow_asset_id: str
    ) -> Optional[ReadinessReport]:
        """Perform a Promotion_Readiness_Check on a Workflow_Draft.

        Read-only: no writes to S3/DDB/memory (Requirement 8.10, Property 18).

        Steps:
        1. Verify ownership (Requirement 8.1)
        2. Read the workflow from S3
        3. If zero steps → return single ReadinessEntry with zero-steps failure
           (Requirement 8.4, Property 24)
        4. Enumerate all referenced assets transitively (Requirement 8.2)
        5. Evaluate per-asset rules (Requirements 8.5–8.7)
        6. Evaluate workflow-level rules (Requirement 8.3)
        7. Roll up overall status as conjunction of all entries (Requirement 8.9)

        Args:
            ctx: The request context (provides actor_id).
            workflow_asset_id: The assetId of the Workflow_Draft to check.

        Returns:
            A ReadinessReport if the workflow exists and is owned by the
            requesting actor, else None (no existence disclosure).
        """
        # 1. Verify ownership (Requirement 8.1). Cross-actor access
        #    surfaces as None because the get is keyed on the requesting
        #    actor's drafts partition.
        workflow_record = await self._index.get(
            _draft_pk(ctx.actor_id),
            make_sk_owned(_WORKFLOW_CATEGORY, workflow_asset_id),
        )
        if workflow_record is None:
            return None

        # The new schema uses status ∈ {draft, published, deprecated};
        # readiness operates on drafts only.
        if workflow_record.status != "draft":
            return None

        # 2. Read the workflow from its canonical draft prefix (BUG-2)
        try:
            workflow_bytes = await self._s3.read_absolute(
                f"{workflow_record.s3_prefix}{_WORKFLOW_FILENAME}"
            )
            steps = self._parse_workflow_steps(workflow_bytes)
        except FileNotFoundError:
            steps = []

        # 3. Zero-step short-circuit (Requirement 8.4, Property 24)
        if len(steps) == 0:
            entry = ReadinessEntry(
                category="workflow",
                asset_id=workflow_asset_id,
                status="not_ready",
                failures=[FAILURE_ZERO_STEPS],
            )
            return ReadinessReport(
                workflow_asset_id=workflow_asset_id,
                overall_status="not_ready",
                entries=[entry],
            )

        # 4. Enumerate all referenced assets transitively (Requirement 8.2)
        referenced_skills: set[str] = set()
        referenced_specialties: set[str] = set()
        referenced_tool_collections: set[str] = set()

        for step in steps:
            if step.agent.kind == "specialty" and step.agent.asset_id:
                referenced_specialties.add(step.agent.asset_id)
            if step.skill.asset_id:
                referenced_skills.add(step.skill.asset_id)
            if step.tool_collection.asset_id:
                referenced_tool_collections.add(step.tool_collection.asset_id)

        # Also enumerate skills referenced transitively via specialty compatible-skills
        for specialty_id in list(referenced_specialties):
            specialty_record = await self._index.get(
                _draft_pk(ctx.actor_id),
                make_sk_owned(_SPECIALTY_CATEGORY, specialty_id),
            )
            if specialty_record is not None and specialty_record.status == "draft":
                for skill_id in getattr(specialty_record, "compatible_skills", []):
                    referenced_skills.add(skill_id)

        # 5. Evaluate per-asset rules
        entries: list[ReadinessEntry] = []

        # 5a. Check each referenced skill (Requirement 8.5)
        for skill_id in sorted(referenced_skills):
            entry = await self._check_skill(ctx, skill_id)
            entries.append(entry)

        # 5b. Check each referenced specialty (Requirement 8.6)
        for specialty_id in sorted(referenced_specialties):
            entry = await self._check_specialty(ctx, specialty_id)
            entries.append(entry)

        # 5c. Check each referenced tool collection (Requirement 8.7)
        for tc_id in sorted(referenced_tool_collections):
            entry = await self._check_tool_collection(ctx, tc_id)
            entries.append(entry)

        # 6. Evaluate workflow-level rules (Requirement 8.3)
        workflow_entry = await self._check_workflow(ctx, steps, workflow_asset_id)
        entries.append(workflow_entry)

        # 7. Roll up overall status (Requirement 8.9, Property 16)
        all_ready = all(e.status == "ready" for e in entries)
        overall_status = "ready" if all_ready else "not_ready"

        return ReadinessReport(
            workflow_asset_id=workflow_asset_id,
            overall_status=overall_status,
            entries=entries,
        )

    def _parse_workflow_steps(self, data: bytes) -> list[WorkflowStep]:
        """Parse workflow.json bytes into a list of WorkflowStep objects."""
        from .models import AgentRef, SkillRef, ToolCollectionRef

        doc = json.loads(data.decode("utf-8"))
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

        return steps

    async def _check_skill(
        self, ctx: LabRequestContext, skill_id: str
    ) -> ReadinessEntry:
        """Check a Skill_Draft against Requirement 8.5.

        A skill is not_ready if:
        - SKILL.md is missing
        - SKILL.md is empty
        - SKILL.md does not contain a skill name declaration

        Args:
            ctx: The request context (provides actor_id used to scope the lookup).
            skill_id: The assetId of the Skill_Draft to check.

        Returns:
            A ReadinessEntry for this skill.
        """
        failures: list[str] = []

        # Check if the skill exists in the index under the actor's drafts.
        skill_record = await self._index.get(
            _draft_pk(ctx.actor_id),
            make_sk_owned(_SKILL_CATEGORY, skill_id),
        )
        if skill_record is None or skill_record.status != "draft":
            failures.append(FAILURE_SKILL_MD_MISSING)
            return ReadinessEntry(
                category="skill",
                asset_id=skill_id,
                status="not_ready",
                failures=failures,
            )

        # Read SKILL.md from its canonical draft prefix (BUG-2)
        try:
            skill_md_bytes = await self._s3.read_absolute(
                f"{skill_record.s3_prefix}SKILL.md"
            )
            skill_md_content = skill_md_bytes.decode("utf-8").strip()
        except FileNotFoundError:
            failures.append(FAILURE_SKILL_MD_MISSING)
            return ReadinessEntry(
                category="skill",
                asset_id=skill_id,
                status="not_ready",
                failures=failures,
            )

        # Check if SKILL.md is empty
        if not skill_md_content:
            failures.append(FAILURE_SKILL_MD_EMPTY)
            return ReadinessEntry(
                category="skill",
                asset_id=skill_id,
                status="not_ready",
                failures=failures,
            )

        # Check if SKILL.md contains a skill name declaration
        # A skill name is declared via YAML frontmatter `name:` field or
        # a markdown heading (# Name)
        if not self._has_skill_name(skill_md_content):
            failures.append(FAILURE_SKILL_MD_NO_NAME)

        status = "ready" if not failures else "not_ready"
        return ReadinessEntry(
            category="skill",
            asset_id=skill_id,
            status=status,
            failures=failures,
        )

    def _has_skill_name(self, content: str) -> bool:
        """Check if SKILL.md content contains a skill name declaration.

        A skill name is present if:
        - The content has YAML frontmatter with a `name:` field, OR
        - The content has a markdown heading (# ...) as the first non-empty line

        Args:
            content: The SKILL.md content as a string.

        Returns:
            True if a skill name declaration is found.
        """
        lines = content.split("\n")

        # Check for YAML frontmatter
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if line.strip().startswith("name:"):
                    name_value = line.split(":", 1)[1].strip()
                    if name_value:
                        return True
            # Frontmatter present but no name field — fall through to heading check

        # Check for markdown heading as skill name
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip frontmatter delimiters
            if stripped == "---":
                continue
            # A markdown heading counts as a name declaration
            if stripped.startswith("#"):
                heading_text = stripped.lstrip("#").strip()
                if heading_text:
                    return True
            break  # Only check the first non-empty, non-frontmatter line

        return False

    async def _check_specialty(
        self, ctx: LabRequestContext, specialty_id: str
    ) -> ReadinessEntry:
        """Check a Specialty_Agent_Draft against Requirement 8.6.

        A specialty is not_ready if:
        - Persona prompt is empty
        - Compatible-skill list references a non-existent skill

        The persona-empty failure is detected only by absence of an index
        row (the SpecialtyCRUD validates non-empty persona at create /
        update time, so a present row implies a non-empty persona). A
        compatible-skill reference that does not resolve to a skill draft
        owned by the requesting actor produces a dangling-skill failure
        per Requirement 8.6.

        Args:
            ctx: The request context (provides actor_id used to scope the lookup).
            specialty_id: The assetId of the Specialty_Agent_Draft to check.

        Returns:
            A ReadinessEntry for this specialty.
        """
        failures: list[str] = []

        specialty_record = await self._index.get(
            _draft_pk(ctx.actor_id),
            make_sk_owned(_SPECIALTY_CATEGORY, specialty_id),
        )
        if specialty_record is None or specialty_record.status != "draft":
            # Specialty doesn't exist — surface as persona-empty per the
            # readiness contract; the unresolved-agent failure on the
            # workflow-level entry covers the cross-reference angle.
            failures.append(FAILURE_PERSONA_EMPTY)
            return ReadinessEntry(
                category="specialty-agent",
                asset_id=specialty_id,
                status="not_ready",
                failures=failures,
            )

        # Check compatible-skill list references (Requirement 8.6).
        compatible_skills = getattr(specialty_record, "compatible_skills", [])
        for skill_id in compatible_skills:
            skill_record = await self._index.get(
                _draft_pk(ctx.actor_id),
                make_sk_owned(_SKILL_CATEGORY, skill_id),
            )
            if skill_record is None or skill_record.status != "draft":
                failures.append(
                    FAILURE_DANGLING_COMPATIBLE_SKILL.format(skill_id=skill_id)
                )

        status = "ready" if not failures else "not_ready"
        return ReadinessEntry(
            category="specialty-agent",
            asset_id=specialty_id,
            status=status,
            failures=failures,
        )

    async def _check_tool_collection(
        self, ctx: LabRequestContext, tc_id: str
    ) -> ReadinessEntry:
        """Check a Tool_Collection_Draft against Requirement 8.7.

        A tool collection is not_ready if:
        - The collection is empty
        - Any tool reference doesn't resolve against the Gateway

        Tool collections live in the flat ``USER#<u>#TOOL_COLLECTIONS``
        partition keyed by ``tool-collection#<id>`` (no version sub-tier);
        the row's ``status`` carries lifecycle stage. Readiness operates
        on draft-stage tool collections only.

        Args:
            ctx: The request context for Gateway access and partition scoping.
            tc_id: The assetId of the Tool_Collection_Draft to check.

        Returns:
            A ReadinessEntry for this tool collection.
        """
        failures: list[str] = []

        tc_record = await self._index.get(
            _tool_collections_pk(ctx.actor_id),
            make_sk_owned(_TOOL_COLLECTION_CATEGORY, tc_id),
        )
        if tc_record is None or tc_record.status != "draft":
            failures.append(FAILURE_TOOL_COLLECTION_EMPTY)
            return ReadinessEntry(
                category="tool-collection",
                asset_id=tc_id,
                status="not_ready",
                failures=failures,
            )

        # Get tool refs from the typed record. ``tool_refs`` is a list of
        # ``ToolRef`` dataclasses on the new model; tolerate raw dicts so
        # mocks that pass dicts directly still work.
        tool_refs_raw = getattr(tc_record, "tool_refs", []) or []
        if not tool_refs_raw:
            failures.append(FAILURE_TOOL_COLLECTION_EMPTY)
            return ReadinessEntry(
                category="tool-collection",
                asset_id=tc_id,
                status="not_ready",
                failures=failures,
            )

        # Resolve each tool ref against the Gateway projection
        gateway_projection = self._gateway_browser.get_projection(ctx)
        gateway_tools: dict[tuple[str, str], GatewayToolDescriptor] = {}

        if gateway_projection is not None:
            for tool in gateway_projection.tools:
                gateway_tools[(tool.name, tool.version)] = tool

        for ref in tool_refs_raw:
            # tool_refs can be ToolRef objects (typed records) or dicts
            # (legacy / test fixtures). Both shapes carry the same fields.
            if isinstance(ref, dict):
                name = ref.get("name", "")
                version = ref.get("version", "")
            else:
                name = ref.name
                version = ref.version

            if (name, version) not in gateway_tools:
                failures.append(
                    FAILURE_TOOL_UNRESOLVED.format(name=name, version=version)
                )

        status = "ready" if not failures else "not_ready"
        return ReadinessEntry(
            category="tool-collection",
            asset_id=tc_id,
            status=status,
            failures=failures,
        )

    async def _check_workflow(
        self,
        ctx: LabRequestContext,
        steps: list[WorkflowStep],
        workflow_asset_id: str,
    ) -> ReadinessEntry:
        """Check the Workflow_Draft itself against Requirement 8.3.

        The workflow is not_ready if:
        - Any step references an asset that does not resolve
        - Any step has out-of-order input dependency
        - Any step binds an incompatible skill/specialty

        Note: zero-step case is handled before this method is called.

        Args:
            ctx: The request context (provides actor_id used to scope lookups).
            steps: The parsed workflow steps.
            workflow_asset_id: The workflow's assetId.

        Returns:
            A ReadinessEntry for the workflow itself.
        """
        failures: list[str] = []
        seen_step_ids: list[str] = []
        drafts_pk = _draft_pk(ctx.actor_id)
        tcs_pk = _tool_collections_pk(ctx.actor_id)

        for step in steps:
            # Check agent reference resolves
            if step.agent.kind == "specialty" and step.agent.asset_id:
                agent_record = await self._index.get(
                    drafts_pk,
                    make_sk_owned(_SPECIALTY_CATEGORY, step.agent.asset_id),
                )
                if agent_record is None or agent_record.status != "draft":
                    failures.append(
                        FAILURE_UNRESOLVED_AGENT.format(
                            step_id=step.step_id,
                            asset_id=step.agent.asset_id,
                        )
                    )

            # Check skill reference resolves
            if step.skill.asset_id:
                skill_record = await self._index.get(
                    drafts_pk,
                    make_sk_owned(_SKILL_CATEGORY, step.skill.asset_id),
                )
                if skill_record is None or skill_record.status != "draft":
                    failures.append(
                        FAILURE_UNRESOLVED_SKILL.format(
                            step_id=step.step_id,
                            asset_id=step.skill.asset_id,
                        )
                    )

            # Check tool collection reference resolves
            if step.tool_collection.asset_id:
                tc_record = await self._index.get(
                    tcs_pk,
                    make_sk_owned(
                        _TOOL_COLLECTION_CATEGORY, step.tool_collection.asset_id
                    ),
                )
                if tc_record is None or tc_record.status != "draft":
                    failures.append(
                        FAILURE_UNRESOLVED_TOOL_COLLECTION.format(
                            step_id=step.step_id,
                            asset_id=step.tool_collection.asset_id,
                        )
                    )

            # Check input ordering (Requirement 8.3)
            for input_ref in step.inputs:
                if input_ref not in seen_step_ids:
                    failures.append(
                        FAILURE_OUT_OF_ORDER_INPUT.format(
                            step_id=step.step_id,
                            input_ref=input_ref,
                        )
                    )

            # Check skill/specialty compatibility (Requirement 8.3)
            if (
                step.skill.asset_id
                and step.agent.kind == "specialty"
                and step.agent.asset_id
            ):
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
                        failures.append(
                            FAILURE_INCOMPATIBLE_SKILL_SPECIALTY.format(
                                step_id=step.step_id,
                                skill_id=step.skill.asset_id,
                                specialty_id=step.agent.asset_id,
                            )
                        )

            seen_step_ids.append(step.step_id)

        status = "ready" if not failures else "not_ready"
        return ReadinessEntry(
            category="workflow",
            asset_id=workflow_asset_id,
            status=status,
            failures=failures,
        )
