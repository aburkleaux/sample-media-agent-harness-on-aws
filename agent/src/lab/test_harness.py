"""Test_Harness — executes a draft against a Sample_Input and persists results.

The Test_Harness routes to the correct execution path based on draft category,
manages per-run working directories, enforces per-run timeouts, and persists
Test_Harness_Run_Records under {actor_id}/lab-runs/{runId}/.

Requirements: 7.1, 7.5, 7.6, 7.8, 7.9
Properties: 15, 23
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import urllib.parse
import uuid
from dataclasses import asdict
from typing import Any, Optional

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
    make_sk_owned,
)
from .context import LabRequestContext
from .errors import GatewayUnavailableError
from .gateway_browser import GatewayBrowser, ToolInvocationResult
from .models import (
    AssetCommon,
    Category,
    DraftRef,
    HarnessError,
    HarnessRunStatus,
    HarnessSubject,
    SampleRef,
    SpecialtyAgent,
    TestHarnessRunRecord,
    ToolCollection,
    ToolRef,
)
from .s3_user_files_tool import S3UserFilesTool

# `skills_manager` lives one directory up (it is a Runtime module, not part of
# the lab package), so its directory has to be importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)

# Timeout constants (Requirement 7.9)
_DEFAULT_TIMEOUT_SKILL = 300       # seconds
_DEFAULT_TIMEOUT_SPECIALTY = 300   # seconds
_DEFAULT_TIMEOUT_TOOL = 300        # seconds
_DEFAULT_TIMEOUT_WORKFLOW = 1800   # seconds
_MAX_TIMEOUT = 7200                # seconds

# Working directory base path (AgentCore Managed Session Storage)
_WORKSPACE_BASE = "/mnt/workspace"

# Task Agent baseline system prompt (R layer in composition)
# This mirrors the default system_prompt from agent/src/task_agent.py
# The UI sends camelCase binding keys; the execution paths below read snake_case.
# Nothing mapped between them, so `agentAssetId` and `skillAssetId` were silently
# dropped: selecting an agent to compose with a skill added no persona layer and
# reported no error — the run looked like it honoured the binding and did not. Only
# the tool collection worked, because it alone had grown a camelCase fallback at its
# read site, which is what a one-place patch for this looks like.
#
# Normalised ONCE, on the way in, so there is a single spelling below.
_BINDING_ALIASES = {
    "agentAssetId": "specialty_asset_id",
    "agent_asset_id": "specialty_asset_id",
    "specialtyAssetId": "specialty_asset_id",
    "skillAssetId": "skill_asset_id",
    "toolCollectionAssetId": "tool_collection_asset_id",
}


def _pick_published_row(rows: list, version: Optional[int]):
    """Delegate to the ONE existing implementation of this selection rule.

    Imported lazily: `specialty_crud` imports the CRUD graph, and this module is
    loaded on paths that do not otherwise need it.
    """
    from .specialty_crud import _pick_published_row as _pick

    return _pick(rows, version)


def normalise_bindings(bindings: Optional[dict]) -> dict:
    """Map any accepted binding spelling onto the canonical snake_case keys.

    Empty strings are dropped as well as ``None``: the UI's "None" option submits
    ``""``, and a falsy id must mean "no binding" rather than a lookup for an asset
    whose id is the empty string.
    """
    if not bindings:
        return {}
    normalised: dict = {}
    for key, value in bindings.items():
        if value in (None, ""):
            continue
        normalised[_BINDING_ALIASES.get(key, key)] = value
    return normalised


_TASK_AGENT_BASELINE_PROMPT = (
    "You are a task execution agent. Complete the given task using your available tools. "
    "Be thorough but concise — focus on the most important findings and avoid excessive detail."
)


class InvalidTimeoutError(Exception):
    """Raised when a user-supplied timeout value is invalid.

    Invalid values: non-integer, zero, negative, or exceeds 7200 seconds.
    """

    def __init__(self, supplied: Any) -> None:
        self.supplied = supplied
        super().__init__(
            f"Invalid timeoutSeconds: {supplied!r}. "
            f"Must be a positive integer not exceeding {_MAX_TIMEOUT}."
        )


class IncompatibleSkillError(Exception):
    """Raised when a skill is not in a specialty's compatible-skill list.

    Requirement 3.5 / 7.3: Before composing a specialty with a skill,
    the skill must be in the specialty's compatible-skill list.
    """

    def __init__(self, skill_id: str, specialty_id: str) -> None:
        self.skill_id = skill_id
        self.specialty_id = specialty_id
        super().__init__(
            f"Skill '{skill_id}' is not in the compatible-skill list "
            f"of specialty agent '{specialty_id}'"
        )


class WorkflowStepFailedError(Exception):
    """Raised when a workflow step fails during harness execution.

    Requirement 7.4: Stop at the first failed step while recording
    the failed step identifier and reason.
    """

    def __init__(self, step_id: str, reason: str) -> None:
        self.step_id = step_id
        self.reason = reason
        super().__init__(
            f"Workflow step '{step_id}' failed: {reason}"
        )


# Layer delimiter used between non-empty composition layers (Property 10)
_LAYER_DELIMITER = "\n\n---\n\n"


def _strip_skill_frontmatter(skill_md_content: str) -> str:
    """Strip YAML frontmatter from SKILL.md content using skills_manager._parse_skill_md.

    Returns only the instructions body (the content after the frontmatter).
    If there is no frontmatter, returns the content unchanged.

    Args:
        skill_md_content: Raw SKILL.md content potentially with YAML frontmatter.

    Returns:
        The skill instructions with frontmatter stripped.
    """
    if not skill_md_content:
        return ""

    # Imported here, not at module scope: `skills_manager` imports Strands at
    # module level, and a module-level import here made `import lab` (via
    # service.py) pull Strands into any process that only wanted the lab
    # domain layer — which the Capability API and the projection Lambda do,
    # neither of which has Strands installed (R1, migration Task 6).
    from skills_manager import SkillsManager

    # Use _parse_skill_md to extract the instructions (body after frontmatter)
    name, _description, instructions = SkillsManager._parse_skill_md(skill_md_content)

    # If parsing found a name, frontmatter was present — return the instructions body
    # (which may be empty if the SKILL.md had only frontmatter)
    if name:
        return instructions
    # No frontmatter detected — return original content as-is
    return skill_md_content


def compose_invocation(
    r: Optional[str] = None,
    p: Optional[str] = None,
    k: Optional[str] = None,
    i: Optional[str] = None,
    *,
    skill_asset_id: Optional[str] = None,
    specialty_asset_id: Optional[str] = None,
    specialty_compatible_skills: Optional[list[str]] = None,
    strip_frontmatter: bool = True,
) -> str:
    """Compose a layered invocation prompt from R, P, K, I layers.

    Implements the layered composition order defined by Requirement 3.5:
    Task Agent runtime (R), Specialty_Agent_Draft persona prompt (P),
    Skill_Draft SKILL.md content (K), and per-invocation prompt (I).

    Non-empty layers are joined with the delimiter `\\n\\n---\\n\\n`.
    Empty or None layers are skipped (not included in output).

    Before composing, validates that if both a skill and specialty are
    provided, the skill is in the specialty's compatible-skill list.

    Args:
        r: Task Agent baseline prompt (R layer).
        p: Specialty agent persona prompt (P layer).
        k: Skill SKILL.md content (K layer). Frontmatter is stripped by default.
        i: Per-invocation prompt (I layer).
        skill_asset_id: The assetId of the skill being composed (for validation).
        specialty_asset_id: The assetId of the specialty being composed (for validation).
        specialty_compatible_skills: The specialty's compatible-skill list (for validation).
        strip_frontmatter: Whether to strip YAML frontmatter from K layer (default True).

    Returns:
        The composed prompt string with layers joined by the delimiter.

    Raises:
        IncompatibleSkillError: If both skill and specialty are provided but
            the skill is not in the specialty's compatible-skill list.

    Requirements: 3.5, 7.2, 7.3
    Properties: 10
    """
    # Validate skill/specialty compatibility before composition
    if skill_asset_id and specialty_asset_id and specialty_compatible_skills is not None:
        if skill_asset_id not in specialty_compatible_skills:
            raise IncompatibleSkillError(skill_asset_id, specialty_asset_id)

    # Strip frontmatter from K layer if requested
    k_processed = k
    if k and strip_frontmatter:
        k_processed = _strip_skill_frontmatter(k)

    # Collect non-empty layers in order: R, P, K, I
    layers: list[str] = []
    for layer in [r, p, k_processed, i]:
        if layer and layer.strip():
            layers.append(layer)

    # Join with delimiter
    return _LAYER_DELIMITER.join(layers)


def _category_to_subject(category: Category) -> HarnessSubject:
    """Map an asset category to the harness subject type."""
    mapping: dict[str, HarnessSubject] = {
        "skill": "skill",
        "specialty-agent": "specialty",
        "workflow": "workflow",
        "tool-collection": "tool",
    }
    return mapping[category]


class TestHarness:
    """Executes drafts against sample inputs and persists run records.

    The TestHarness manages the full lifecycle of a test run:
    1. Validate timeout
    2. Look up target asset in the index to determine category
    3. Allocate runId and working directory
    4. Route to the appropriate execution path
    5. Persist the run record
    6. Tear down working directory

    Args:
        s3: The S3UserFilesTool instance bound to the current actor.
        index: The AssetIndex instance bound to the current actor.
        gateway_browser: Optional GatewayBrowser for single-tool invocations.
    """

    def __init__(
        self,
        s3: S3UserFilesTool,
        index: AssetIndex,
        gateway_browser: Optional[GatewayBrowser] = None,
    ) -> None:
        self._s3 = s3
        self._index = index
        self._gateway_browser = gateway_browser

    @staticmethod
    def resolve_timeout(
        category: Category, supplied: Optional[int] = None
    ) -> int:
        """Resolve the per-run timeout based on category and user-supplied value.

        If the user supplies a timeoutSeconds value, it is validated and used.
        Otherwise, the default is selected based on the target category:
        - 300 seconds for skill, specialty-agent, or tool-collection (single tool)
        - 1800 seconds for workflow

        Args:
            category: The target draft's category.
            supplied: Optional user-supplied timeout in seconds.

        Returns:
            The resolved timeout in seconds.

        Raises:
            InvalidTimeoutError: If the supplied value is not a positive integer
                or exceeds 7200 seconds.
        """
        if supplied is None:
            if category in ("skill", "specialty-agent", "tool-collection"):
                return _DEFAULT_TIMEOUT_SKILL
            else:
                return _DEFAULT_TIMEOUT_WORKFLOW

        # Validate supplied value
        if not isinstance(supplied, int) or isinstance(supplied, bool):
            raise InvalidTimeoutError(supplied)
        if supplied <= 0 or supplied > _MAX_TIMEOUT:
            raise InvalidTimeoutError(supplied)

        return supplied

    async def run(
        self,
        ctx: LabRequestContext,
        target_asset_id: str,
        sample_input: SampleRef,
        bindings: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> TestHarnessRunRecord:
        """Execute a draft against a sample input and persist the run record.

        This is the main entry point for the Test_Harness. It:
        1. Looks up the target asset to determine its category
        2. Validates the timeout
        3. Allocates a runId and working directory
        4. Routes to the appropriate execution path
        5. Persists the Test_Harness_Run_Record
        6. Tears down the working directory

        Args:
            ctx: The LabRequestContext for the current request.
            target_asset_id: The assetId of the draft to test.
            sample_input: The sample input data reference.
            bindings: Optional bindings (e.g., specialty or skill to compose with).
            timeout_seconds: Optional user-supplied timeout in seconds.

        Returns:
            The persisted TestHarnessRunRecord.

        Raises:
            InvalidTimeoutError: If the supplied timeout is invalid.
            FileNotFoundError: If the target asset does not exist or is not
                owned by the current actor.
        """
        bindings = normalise_bindings(bindings)

        # Step 1: Look up the target asset in the index
        asset_record = await self._lookup_asset(ctx, target_asset_id)
        if asset_record is None:
            raise FileNotFoundError(
                f"Target asset not found: {target_asset_id}"
            )

        category: Category = asset_record.category

        # Step 2: Validate timeout (before allocating any resources)
        resolved_timeout = self.resolve_timeout(category, timeout_seconds)

        # Step 3: Allocate runId and working directory
        run_id = str(uuid.uuid4())
        working_dir = os.path.join(_WORKSPACE_BASE, run_id)
        started_at = int(time.time() * 1000)

        # Determine the harness subject
        subject = _category_to_subject(category)

        # Create the draft reference
        draft_ref = DraftRef(category=category, asset_id=target_asset_id)

        # Initialize the run record (will be updated with results)
        record = TestHarnessRunRecord(
            run_id=run_id,
            subject=subject,
            draft_ref=draft_ref,
            sample_ref=sample_input,
            status="failed",  # default to failed; set to succeeded on success
            outputs=[],
            logs="",
            started_at=started_at,
            ended_at=0,
            error=None,
        )

        try:
            # Create working directory
            os.makedirs(working_dir, exist_ok=True)

            # Step 4: Route to the appropriate execution path with timeout
            try:
                result = await asyncio.wait_for(
                    self._dispatch(
                        ctx=ctx,
                        category=category,
                        asset_record=asset_record,
                        sample_input=sample_input,
                        bindings=bindings or {},
                        working_dir=working_dir,
                    ),
                    timeout=resolved_timeout,
                )

                # Execution succeeded
                record.status = "succeeded"
                record.outputs = result.get("outputs", [])
                record.logs = result.get("logs", "")

            except asyncio.TimeoutError:
                # Timeout exceeded (Requirement 7.8)
                record.status = "failed"
                record.error = HarnessError(cause="timeout")
                record.logs = f"Run exceeded timeout of {resolved_timeout}s"
                logger.warning(
                    "Test harness run %s timed out after %ds",
                    run_id,
                    resolved_timeout,
                )

            except WorkflowStepFailedError as exc:
                # Workflow step failure (Requirement 7.4)
                record.status = "failed"
                record.error = HarnessError(
                    cause="step_failed", step_id=exc.step_id
                )
                record.logs = f"Workflow step '{exc.step_id}' failed: {exc.reason}"
                logger.warning(
                    "Test harness run %s: workflow step '%s' failed: %s",
                    run_id,
                    exc.step_id,
                    exc.reason,
                )

            except Exception as exc:
                # Execution failed
                record.status = "failed"
                error_cause = str(exc) if str(exc) else type(exc).__name__
                record.error = HarnessError(cause=error_cause)
                record.logs = f"Run failed: {error_cause}"
                logger.error(
                    "Test harness run %s failed: %s",
                    run_id,
                    error_cause,
                    exc_info=True,
                )

        finally:
            # Step 5: Record end timestamp
            record.ended_at = int(time.time() * 1000)

            # Step 6: Persist the run record
            await self._persist_run_record(record)

            # Step 7: Tear down working directory
            self._teardown_working_dir(working_dir)

        return record

    async def list_runs(self, ctx: LabRequestContext) -> list[dict]:
        """List prior Test_Harness_Run_Records for the current actor.

        Returns only records whose storage prefix begins with
        {actor_id}/lab-runs/.

        Stub implementation — task 10.6 will provide the full implementation.

        Args:
            ctx: The LabRequestContext for the current request.

        Returns:
            A list of run record summaries (runId, targetAssetId, status,
            startedAt, endedAt).
        """
        # List all manifest.json files under lab-runs/
        paths = await self._s3.list("lab-runs/")
        records = []

        for path in paths:
            if path.endswith("/manifest.json"):
                try:
                    data = await self._s3.read(path)
                    manifest = json.loads(data)
                    records.append({
                        "runId": manifest.get("run_id"),
                        "targetAssetId": manifest.get("draft_ref", {}).get(
                            "asset_id"
                        ),
                        "status": manifest.get("status"),
                        "startedAt": manifest.get("started_at"),
                        "endedAt": manifest.get("ended_at"),
                    })
                except (FileNotFoundError, json.JSONDecodeError):
                    # Skip corrupted or missing records
                    continue

        return records

    async def read_run_record(
        self, ctx: LabRequestContext, run_id: str
    ) -> Optional[TestHarnessRunRecord]:
        """Read a single Test_Harness_Run_Record by runId.

        Stub implementation — task 10.6 will provide the full implementation.

        Args:
            ctx: The LabRequestContext for the current request.
            run_id: The unique run identifier.

        Returns:
            The TestHarnessRunRecord if found, else None.
        """
        path = f"lab-runs/{run_id}/manifest.json"
        try:
            data = await self._s3.read(path)
            manifest = json.loads(data)
            return TestHarnessRunRecord(
                run_id=manifest["run_id"],
                subject=manifest["subject"],
                draft_ref=DraftRef(
                    category=manifest["draft_ref"]["category"],
                    asset_id=manifest["draft_ref"]["asset_id"],
                ),
                sample_ref=SampleRef(
                    kind=manifest["sample_ref"]["kind"],
                    value=manifest["sample_ref"]["value"],
                ),
                status=manifest["status"],
                outputs=manifest.get("outputs", []),
                logs=manifest.get("logs", ""),
                started_at=manifest.get("started_at", 0),
                ended_at=manifest.get("ended_at", 0),
                error=(
                    HarnessError(
                        cause=manifest["error"]["cause"],
                        step_id=manifest["error"].get("step_id"),
                    )
                    if manifest.get("error")
                    else None
                ),
            )
        except FileNotFoundError:
            return None

    async def _lookup_asset(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[AssetCommon]:
        """Look up a draft asset in the index across all categories.

        The harness operates on drafts only (per design: "harness continues
        to operate against drafts"). Skill, specialty-agent, and workflow
        drafts live under ``USER#<actor>#DRAFTS``; tool-collection rows
        live under the flat ``USER#<actor>#TOOL_COLLECTIONS`` partition
        with their lifecycle stage carried on the row itself. We try the
        three versioned-category drafts first, then the tool-collection
        partition, returning the first match whose ``status == "draft"``.

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The asset identifier to look up.

        Returns:
            The typed :class:`AssetCommon` subclass if found, else None.
        """
        drafts_pk = make_pk_user_owned(ctx.actor_id)
        for category in ("skill", "specialty-agent", "workflow"):
            record = await self._index.get(
                drafts_pk, make_sk_owned(category, asset_id)
            )
            if record is not None and record.status == "draft":
                return record

        # Tool collections live under their own flat partition.
        tc_pk = make_pk_user_owned(ctx.actor_id)
        record = await self._index.get(
            tc_pk, make_sk_owned("tool-collection", asset_id)
        )
        if record is not None and record.status == "draft":
            return record

        # No working copy anywhere. Fall back to the lineage's published version:
        # publishing CONSUMES the working copy, so for anything the actor has shipped
        # this is the ONLY row that exists, and Test on a published row failed with
        # "Target asset not found" — on the normal case, not an edge one.
        #
        # `_pick_published_row` is imported rather than re-implemented. AGENTS.md
        # records this selection rule as already duplicated in two places
        # (`task_agent._pick_lineage_row` and `specialty_crud._pick_published_row`);
        # a third copy is exactly what should not be added. Its logic reads only
        # version/current/deprecated, so it is category-agnostic despite its home.
        #
        # Tool collections are flat and absent from the lineage GSI, so they simply
        # find nothing here — correct, since they have no published form.
        lineage = await self._index.query_lineage(asset_id)
        if lineage:
            published = _pick_published_row(lineage, None)
            if published is not None:
                return published
        return None

    async def _dispatch(
        self,
        ctx: LabRequestContext,
        category: Category,
        asset_record: AssetCommon,
        sample_input: SampleRef,
        bindings: dict[str, Any],
        working_dir: str,
    ) -> dict[str, Any]:
        """Route execution to the appropriate path based on category.

        This method dispatches to the correct execution handler:
        - skill → _execute_skill
        - specialty-agent → _execute_specialty
        - workflow → _execute_workflow
        - tool-collection → _execute_tool

        Args:
            ctx: The LabRequestContext.
            category: The target draft's category.
            asset_record: The typed asset record from the index.
            sample_input: The sample input reference.
            bindings: Optional bindings for composition.
            working_dir: The run's working directory path.

        Returns:
            A dict with "outputs" and "logs" keys.
        """
        if category == "skill":
            return await self._execute_skill(
                ctx, asset_record, sample_input, bindings, working_dir
            )
        elif category == "specialty-agent":
            return await self._execute_specialty(
                ctx, asset_record, sample_input, bindings, working_dir
            )
        elif category == "workflow":
            return await self._execute_workflow(
                ctx, asset_record, sample_input, bindings, working_dir
            )
        elif category == "tool-collection":
            return await self._execute_tool(
                ctx, asset_record, sample_input, bindings, working_dir
            )
        else:
            raise ValueError(f"Unknown category: {category}")

    async def _execute_skill(
        self,
        ctx: LabRequestContext,
        asset_record: AssetCommon,
        sample_input: SampleRef,
        bindings: dict[str, Any],
        working_dir: str,
    ) -> dict[str, Any]:
        """Execute a Skill_Draft against the sample input.

        Composes the skill with either the core Task Agent runtime or a
        Specialty_Agent_Draft specified in the run bindings, then dispatches
        to the Task Agent runtime.

        The execution path:
        1. Read the SKILL.md from S3 (using the asset's s3 prefix)
        2. If bindings include a specialty_asset_id, read the specialty's
           persona from S3 and validate compatibility
        3. Compose the invocation using compose_invocation()
        4. Dispatch to the Task Agent runtime

        Requirements: 7.2
        """
        start_time = time.time()
        drafts_pk = make_pk_user_owned(ctx.actor_id)

        # Step 1: Read the SKILL.md at the target's own prefix — draft or published.
        skill_asset_id = asset_record.asset_id
        skill_md_content = await self._read_record_artifact_text(
            asset_record, "SKILL.md"
        )

        # Step 2: Optionally read specialty persona if bindings include one
        persona: Optional[str] = None
        specialty_asset_id: Optional[str] = bindings.get("specialty_asset_id")
        specialty_compatible_skills: Optional[list[str]] = None

        if specialty_asset_id:
            specialty_record = await self._index.get(
                drafts_pk,
                make_sk_owned("specialty-agent", specialty_asset_id),
            )
            if specialty_record is None or specialty_record.status != "draft":
                raise FileNotFoundError(
                    f"Specialty agent not found in bindings: {specialty_asset_id}"
                )
            specialty_compatible_skills = list(
                getattr(specialty_record, "compatible_skills", [])
            )
            persona = await self._read_draft_artifact_text(
                "specialty-agents", specialty_asset_id, "persona.md"
            )

        # Step 3: Resolve tool collection from bindings (if specified)
        tools: list[str] | None = None
        tool_collection_id = bindings.get("tool_collection_asset_id")
        if tool_collection_id:
            tc_record = await self._index.get(
                make_pk_user_owned(ctx.actor_id),
                make_sk_owned("tool-collection", tool_collection_id),
            )
            if tc_record is not None and tc_record.status == "draft":
                tc_path = f"drafts/tool-collections/{tool_collection_id}/collection.json"
                try:
                    tc_bytes = await self._s3.read(tc_path)
                    tc_doc = json.loads(tc_bytes.decode("utf-8"))
                    tool_refs = tc_doc.get("toolRefs", []) or tc_doc.get("tools", [])
                    tools = [ref.get("name", "") for ref in tool_refs if ref.get("name")]
                except (FileNotFoundError, json.JSONDecodeError):
                    tools = None

        # Step 4: Compose the invocation
        composed_prompt = compose_invocation(
            r=_TASK_AGENT_BASELINE_PROMPT,
            p=persona,
            k=skill_md_content,
            i=sample_input.value,
            skill_asset_id=skill_asset_id,
            specialty_asset_id=specialty_asset_id,
            specialty_compatible_skills=specialty_compatible_skills,
        )

        # Step 5: Dispatch to Task Agent runtime with scoped tools
        result = await self._invoke_task_agent(composed_prompt, working_dir, tools=tools)

        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "outputs": result.get("outputs", [composed_prompt]),
            "logs": result.get("logs", ""),
            "tool_invocations": result.get("tool_invocations", []),
            "composed_prompt": composed_prompt,
            "duration_ms": elapsed_ms,
        }

    async def _execute_specialty(
        self,
        ctx: LabRequestContext,
        asset_record: AssetCommon,
        sample_input: SampleRef,
        bindings: dict[str, Any],
        working_dir: str,
    ) -> dict[str, Any]:
        """Execute a Specialty_Agent_Draft against the sample input.

        Composes the specialty with an optional skill binding, then
        dispatches to the Task Agent runtime.

        The execution path:
        1. Read the specialty's persona from S3
        2. If bindings include a skill_asset_id, read the skill's SKILL.md
           and validate compatibility
        3. Compose the invocation using compose_invocation()
        4. Dispatch to the Task Agent runtime

        Requirements: 7.3
        """
        start_time = time.time()
        drafts_pk = make_pk_user_owned(ctx.actor_id)

        # Step 1: Read the specialty's persona from S3
        specialty_asset_id = asset_record.asset_id
        specialty_compatible_skills = list(
            getattr(asset_record, "compatible_skills", [])
        )
        persona = await self._read_record_artifact_text(asset_record, "persona.md")

        # Step 2: Optionally read skill if bindings include one
        skill_md_content: Optional[str] = None
        skill_asset_id: Optional[str] = bindings.get("skill_asset_id")

        if skill_asset_id:
            skill_record = await self._index.get(
                drafts_pk, make_sk_owned("skill", skill_asset_id)
            )
            if skill_record is None or skill_record.status != "draft":
                raise FileNotFoundError(
                    f"Skill not found in bindings: {skill_asset_id}"
                )
            skill_md_content = await self._read_draft_artifact_text(
                "skills", skill_asset_id, "SKILL.md"
            )

        # Step 3: Resolve tool collection from bindings (if specified)
        tools: list[str] | None = None
        tool_collection_id = bindings.get("tool_collection_asset_id") or bindings.get("toolCollectionAssetId")
        if tool_collection_id:
            tc_record = await self._index.get(
                make_pk_user_owned(ctx.actor_id),
                make_sk_owned("tool-collection", tool_collection_id),
            )
            if tc_record is not None and tc_record.status == "draft":
                tc_path = f"drafts/tool-collections/{tool_collection_id}/collection.json"
                try:
                    tc_bytes = await self._s3.read(tc_path)
                    tc_doc = json.loads(tc_bytes.decode("utf-8"))
                    tool_refs = tc_doc.get("toolRefs", []) or tc_doc.get("tools", [])
                    tools = [ref.get("name", "") for ref in tool_refs if ref.get("name")]
                except (FileNotFoundError, json.JSONDecodeError):
                    tools = None

        # Step 4: Compose the invocation
        composed_prompt = compose_invocation(
            r=_TASK_AGENT_BASELINE_PROMPT,
            p=persona,
            k=skill_md_content,
            i=sample_input.value,
            skill_asset_id=skill_asset_id,
            specialty_asset_id=specialty_asset_id,
            specialty_compatible_skills=specialty_compatible_skills,
        )

        # Step 5: Dispatch to Task Agent runtime with scoped tools
        result = await self._invoke_task_agent(composed_prompt, working_dir, tools=tools)

        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "outputs": result.get("outputs", [composed_prompt]),
            "logs": result.get("logs", ""),
            "tool_invocations": result.get("tool_invocations", []),
            "composed_prompt": composed_prompt,
            "duration_ms": elapsed_ms,
        }

    async def _execute_workflow(
        self,
        ctx: LabRequestContext,
        asset_record: AssetCommon,
        sample_input: SampleRef,
        bindings: dict[str, Any],
        working_dir: str,
    ) -> dict[str, Any]:
        """Execute a Workflow_Draft end-to-end.

        Translates workflow.json into a Plan (using dag_schema.Plan/DAGNode),
        then executes steps in topological order honoring input-dependency edges.
        Stops at the first failed step, recording the failed stepId and reason.

        The execution uses a harness_dispatch function that composes each step's
        prompt using compose_invocation (layering R, P, K, I) and simulates
        execution by returning the composed prompt as output.

        Requirements: 7.4
        """
        asset_id = asset_record.asset_id

        # Step 1: Read workflow.json from its canonical draft prefix (BUG-2)
        workflow_bytes = await self._s3.read_absolute(
            f"{(getattr(asset_record, 's3_prefix', '') or '').strip('/')}/workflow.json"
        )
        workflow_doc = json.loads(workflow_bytes.decode("utf-8"))

        # Step 2: Parse steps from the workflow document
        raw_steps = workflow_doc.get("steps", [])
        if not raw_steps:
            return {
                "outputs": [],
                "logs": "Workflow has no steps — nothing to execute.",
            }

        # Step 3: Translate each WorkflowStep → DAGNode
        # Import DAGNode and Plan from dag_schema
        from planning.dag_schema import DAGNode, Plan

        dag_nodes: list[DAGNode] = []
        for raw_step in raw_steps:
            step_id = raw_step.get("stepId", "")
            prompt = raw_step.get("prompt", "")
            inputs = raw_step.get("inputs", [])

            node = DAGNode(
                id=step_id,
                prompt=prompt,
                tools=[],
                depends_on=inputs,
                output_key=f"{step_id}_output",
                output_type="text",
            )
            dag_nodes.append(node)

        # Step 4: Build a Plan from the DAGNodes
        plan = Plan(
            plan_id=f"harness-{asset_id}",
            goal=f"Execute workflow: {workflow_doc.get('name', asset_id)}",
            nodes=dag_nodes,
        )

        # Step 5: Execute steps in topological order (fail-fast)
        # Build step lookup for composition context
        step_lookup = {s.get("stepId"): s for s in raw_steps}
        node_outputs: dict[str, str] = {}
        completed: set[str] = set()
        execution_logs: list[str] = []
        all_outputs: list[str] = []

        # Topological execution: process nodes whose deps are all satisfied
        remaining_node_ids = [n.id for n in dag_nodes]

        while remaining_node_ids:
            # Find next ready node (all deps completed)
            ready_node_id = None
            for nid in remaining_node_ids:
                node = next(n for n in dag_nodes if n.id == nid)
                if all(dep in completed for dep in node.depends_on):
                    ready_node_id = nid
                    break

            if ready_node_id is None:
                # No node is ready — stuck (cycle or missing dep)
                stuck_ids = ", ".join(remaining_node_ids)
                error_msg = (
                    f"Workflow execution stuck: no ready nodes. "
                    f"Remaining: [{stuck_ids}]"
                )
                execution_logs.append(error_msg)
                raise WorkflowStepFailedError(
                    step_id=remaining_node_ids[0],
                    reason=error_msg,
                )

            # Execute the ready node
            remaining_node_ids.remove(ready_node_id)
            current_node = next(n for n in dag_nodes if n.id == ready_node_id)
            raw_step = step_lookup.get(ready_node_id, {})

            try:
                output = await self._execute_workflow_step(
                    ctx=ctx,
                    step=raw_step,
                    node=current_node,
                    node_outputs=node_outputs,
                    sample_input=sample_input,
                )
                node_outputs[ready_node_id] = output
                completed.add(ready_node_id)
                all_outputs.append(output)
                execution_logs.append(
                    f"Step '{ready_node_id}' completed successfully."
                )
            except Exception as exc:
                # Stop at first failed step (Requirement 7.4)
                reason = str(exc) if str(exc) else type(exc).__name__
                execution_logs.append(
                    f"Step '{ready_node_id}' FAILED: {reason}"
                )
                raise WorkflowStepFailedError(
                    step_id=ready_node_id,
                    reason=reason,
                )

        return {
            "outputs": all_outputs,
            "logs": "\n".join(execution_logs),
        }

    async def _execute_workflow_step(
        self,
        ctx: LabRequestContext,
        step: dict[str, Any],
        node: Any,
        node_outputs: dict[str, str],
        sample_input: SampleRef,
    ) -> str:
        """Execute a single workflow step using layered composition + Task Agent dispatch.

        Composes the step's prompt using compose_invocation with the step's
        agent/skill/tool bindings, resolves the tool collection to get scoped
        tool names, then dispatches to the Task Agent with only those tools.

        Args:
            ctx: The LabRequestContext.
            step: The raw step dict from workflow.json.
            node: The DAGNode for this step.
            node_outputs: Outputs from previously completed steps.
            sample_input: The sample input for the run.

        Returns:
            The Task Agent's response text.
        """
        # Build the invocation prompt (I layer) with dependency context
        prompt = step.get("prompt", "")

        # Inject dependency context into prompt
        if node.depends_on:
            context_parts = ["Context from previous steps:"]
            for dep_id in node.depends_on:
                dep_output = node_outputs.get(dep_id, "")
                context_parts.append(f"  [{dep_id}]: {dep_output}")
            prompt = "\n".join(context_parts) + "\n\n" + prompt

        # For the first step with no deps, include the sample input
        if not node.depends_on and sample_input.value:
            prompt = f"Input: {sample_input.value}\n\n{prompt}"

        # Resolve agent/skill/specialty for composition layers
        r_layer: Optional[str] = _TASK_AGENT_BASELINE_PROMPT  # R: Task Agent baseline
        p_layer: Optional[str] = None  # P: Specialty persona
        k_layer: Optional[str] = None  # K: Skill content

        agent_data = step.get("agent", {})
        skill_data = step.get("skill", {})
        tool_collection_data = step.get("toolCollection", {})

        drafts_pk = make_pk_user_owned(ctx.actor_id)

        # Resolve specialty persona (P layer)
        if agent_data.get("kind") == "specialty" and agent_data.get("assetId"):
            specialty_id = agent_data["assetId"]
            specialty_record = await self._index.get(
                drafts_pk, make_sk_owned("specialty-agent", specialty_id)
            )
            if specialty_record is not None and specialty_record.status == "draft":
                try:
                    persona_bytes = await self._s3.read_absolute(
                        self._draft_artifact_key(
                            "specialty-agents", specialty_id, "persona.md"
                        )
                    )
                    p_layer = persona_bytes.decode("utf-8")
                except FileNotFoundError:
                    p_layer = None

        # Resolve skill content (K layer)
        if skill_data.get("assetId"):
            skill_id = skill_data["assetId"]
            skill_record = await self._index.get(
                drafts_pk, make_sk_owned("skill", skill_id)
            )
            if skill_record is not None and skill_record.status == "draft":
                try:
                    skill_bytes = await self._s3.read_absolute(
                        self._draft_artifact_key("skills", skill_id, "SKILL.md")
                    )
                    k_layer = skill_bytes.decode("utf-8")
                except FileNotFoundError:
                    k_layer = None

        # Resolve tool collection → scoped tool names
        tools: list[str] | None = None  # None = all tools; list = scoped
        if tool_collection_data.get("assetId"):
            tc_id = tool_collection_data["assetId"]
            tc_record = await self._index.get(
                make_pk_user_owned(ctx.actor_id),
                make_sk_owned("tool-collection", tc_id),
            )
            if tc_record is not None and tc_record.status == "draft":
                # Read collection.json to get tool refs
                tc_path = f"drafts/tool-collections/{tc_id}/collection.json"
                try:
                    tc_bytes = await self._s3.read(tc_path)
                    tc_doc = json.loads(tc_bytes.decode("utf-8"))
                    tool_refs = tc_doc.get("toolRefs", []) or tc_doc.get("tools", [])
                    tools = [ref.get("name", "") for ref in tool_refs if ref.get("name")]
                except (FileNotFoundError, json.JSONDecodeError):
                    tools = None  # Fall back to all tools if collection can't be read

        # Compose the invocation using the layered composition
        composed = compose_invocation(
            r=r_layer,
            p=p_layer,
            k=k_layer,
            i=prompt,
        )

        # Dispatch to Task Agent with scoped tools
        result = await self._invoke_task_agent(composed, "", tools=tools)
        output = result.get("outputs", [composed])
        return output[0] if output else composed

    async def _execute_tool(
        self,
        ctx: LabRequestContext,
        asset_record: AssetCommon,
        sample_input: SampleRef,
        bindings: dict[str, Any],
        working_dir: str,
    ) -> dict[str, Any]:
        """Execute a single Gateway tool invocation.

        Invokes the Gateway call_tool directly (no Task Agent involvement)
        and returns the raw response body, status, and duration.

        The tool name and version are extracted from the asset record's
        ``tool_refs`` list. If bindings provide explicit tool_name and
        tool_version, those are used instead.

        Requirements: 4.6

        Args:
            ctx: The LabRequestContext.
            asset_record: The typed :class:`ToolCollection` record from the index.
            sample_input: The sample input reference containing the tool input data.
            bindings: Optional bindings. May contain:
                - tool_name: Explicit tool name to invoke.
                - tool_version: Explicit tool version to invoke.
            working_dir: The run's working directory path.

        Returns:
            A dict with "outputs" and "logs" keys. Outputs include the raw
            response body, status code, and duration in milliseconds.

        Raises:
            GatewayUnavailableError: If the Gateway is unreachable.
            ValueError: If no gateway_browser is configured or no tool
                can be identified from the asset/bindings.
        """
        if self._gateway_browser is None:
            raise ValueError(
                "GatewayBrowser is required for tool invocations but was not configured."
            )

        # Determine tool name and version from bindings or asset record
        tool_name = bindings.get("tool_name")
        tool_version = bindings.get("tool_version")

        if not tool_name or not tool_version:
            # Fall back to the first tool in the collection's tool_refs
            tool_refs = list(getattr(asset_record, "tool_refs", []))
            if not tool_refs:
                raise ValueError(
                    f"No tool references found in asset {asset_record.asset_id} "
                    "and no tool_name/tool_version provided in bindings."
                )
            first_ref = tool_refs[0]
            # tool_refs may be ToolRef dataclass instances or dicts (for
            # legacy callers that build the record by hand).
            if isinstance(first_ref, dict):
                tool_name = tool_name or first_ref.get("name")
                tool_version = tool_version or first_ref.get("version")
            else:
                tool_name = tool_name or first_ref.name
                tool_version = tool_version or first_ref.version

        # Parse sample input data
        if sample_input.kind == "inline":
            try:
                input_data = json.loads(sample_input.value)
            except (json.JSONDecodeError, TypeError):
                # If not valid JSON, pass as raw string
                input_data = sample_input.value
        else:
            # For s3 kind, read the content from S3
            raw = await self._s3.read(sample_input.value)
            try:
                input_data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                input_data = raw.decode("utf-8") if isinstance(raw, bytes) else raw

        # Record start time for duration measurement
        start_ms = int(time.time() * 1000)

        # Invoke the Gateway tool directly
        result: ToolInvocationResult = self._gateway_browser.call_tool(
            tool_name=tool_name,
            tool_version=tool_version,
            input_data=input_data,
        )

        end_ms = int(time.time() * 1000)
        duration_ms = end_ms - start_ms

        # Build the output with raw response, status, and duration
        outputs = [
            {
                "response_body": result.response_body,
                "status": result.status,
                "duration_ms": result.duration_ms,
            }
        ]

        logs = (
            f"Tool invocation: {tool_name}@{tool_version}\n"
            f"Status: {result.status}\n"
            f"Duration: {result.duration_ms}ms\n"
            f"Wall-clock duration: {duration_ms}ms"
        )

        return {"outputs": outputs, "logs": logs}

    async def _persist_run_record(self, record: TestHarnessRunRecord) -> None:
        """Persist a Test_Harness_Run_Record to S3.

        Writes the record as manifest.json under {actor_id}/lab-runs/{runId}/.

        Args:
            record: The TestHarnessRunRecord to persist.
        """
        manifest = self._record_to_dict(record)
        manifest_json = json.dumps(manifest, indent=2).encode("utf-8")
        path = f"lab-runs/{record.run_id}/manifest.json"

        await self._s3.write(path, manifest_json)
        logger.info(
            "Persisted run record %s with status=%s",
            record.run_id,
            record.status,
        )

    @staticmethod
    def _record_to_dict(record: TestHarnessRunRecord) -> dict:
        """Convert a TestHarnessRunRecord to a JSON-serializable dict."""
        result = {
            "run_id": record.run_id,
            "subject": record.subject,
            "draft_ref": {
                "category": record.draft_ref.category,
                "asset_id": record.draft_ref.asset_id,
            },
            "sample_ref": {
                "kind": record.sample_ref.kind,
                "value": record.sample_ref.value,
            },
            "status": record.status,
            "outputs": record.outputs,
            "logs": record.logs,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "error": (
                {
                    "cause": record.error.cause,
                    "step_id": record.error.step_id,
                }
                if record.error
                else None
            ),
        }
        return result

    @staticmethod
    def _teardown_working_dir(working_dir: str) -> None:
        """Remove the run's working directory.

        Uses shutil.rmtree for recursive removal. Logs but does not raise
        on failure (cleanup is best-effort after the record is persisted).

        Args:
            working_dir: The path to the working directory to remove.
        """
        try:
            if os.path.exists(working_dir):
                shutil.rmtree(working_dir)
                logger.debug("Removed working directory: %s", working_dir)
        except OSError as exc:
            logger.warning(
                "Failed to remove working directory %s: %s",
                working_dir,
                exc,
            )

    async def _read_s3_text(self, path: str) -> str:
        """Read a text file from S3 and decode as UTF-8.

        Args:
            path: Relative path within the actor's namespace.

        Returns:
            The file content as a string.

        Raises:
            FileNotFoundError: If the object does not exist.
        """
        data = await self._s3.read(path)
        return data.decode("utf-8")

    def _draft_artifact_key(self, category_plural: str, asset_id: str, filename: str) -> str:
        """Bucket-absolute key for a draft asset's artifact.

        Draft artifacts (SKILL.md, persona.md, workflow.json) are written by
        the CRUD layer at the canonical grammar prefix
        ``users/<actor>/drafts/<plural>/<id>/`` (BUG-2 fix), so the harness
        must read them there via the absolute methods rather than the
        actor-scoped relative path it used before. Harness-internal artifacts
        (``lab-runs/…`` run manifests) and tool-collection drafts keep their
        actor-scoped access and do NOT go through here.
        """
        return (
            f"users/{self._s3.actor_id}/drafts/{category_plural}/"
            f"{asset_id}/{filename}"
        )

    async def _read_draft_artifact_text(
        self, category_plural: str, asset_id: str, filename: str
    ) -> str:
        """Read a draft asset's text artifact from its canonical prefix."""
        data = await self._s3.read_absolute(
            self._draft_artifact_key(category_plural, asset_id, filename)
        )
        return data.decode("utf-8")

    async def _read_record_artifact_text(
        self, record: AssetCommon, filename: str
    ) -> str:
        """Read a text artifact at the record's OWN prefix.

        Used for the target under test, which may be a working copy
        (``users/<a>/drafts/skills/<id>/``) or a published version
        (``users/<a>/published/skills/<id>/v<n>/``). Deriving the drafts key
        instead — as this used to — cannot read the second, and the second is the
        only row that exists for anything the actor has published.
        """
        prefix = (getattr(record, "s3_prefix", "") or "").strip("/")
        if not prefix:
            raise FileNotFoundError(
                f"asset {getattr(record, 'asset_id', '?')} has no s3_prefix to read "
                f"{filename} from"
            )
        data = await self._s3.read_absolute(f"{prefix}/{filename}")
        return data.decode("utf-8")

    async def _invoke_task_agent(
        self, composed_prompt: str, working_dir: str, tools: list[str] | None = None
    ) -> dict[str, Any]:
        """Dispatch the composed prompt to the Task Agent runtime via AgentCore.

        Args:
            composed_prompt: The fully composed prompt (R+P+K+I joined).
            working_dir: The run's working directory path.
            tools: Optional list of tool names to restrict the agent to.
                   None = load all tools; [] = no tools; ["x","y"] = only those.

        Returns:
            A dict with "outputs", "logs", and "tool_invocations" keys.
        """
        import json as _json
        import urllib.request

        # Env, else the runtime-ARN SSM parameters (the deploy no longer puts it in runtime env).
        import runtime_arns

        task_agent_arn = runtime_arns.get("TASK_AGENT_RUNTIME_ARN")
        if not task_agent_arn:
            # Fallback: return composed prompt if no Task Agent configured
            logger.warning("TASK_AGENT_RUNTIME_ARN not set — returning composed prompt")
            return {
                "outputs": [composed_prompt],
                "logs": "Task Agent runtime not configured (TASK_AGENT_RUNTIME_ARN missing)",
                "tool_invocations": [],
            }

        region = os.environ.get("AWS_REGION", "us-west-2")
        encoded_arn = urllib.parse.quote(task_agent_arn, safe="")
        invoke_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"

        # Build the payload for the Task Agent
        payload_dict: dict[str, Any] = {
            "prompt": composed_prompt,
        }
        # Pass tools explicitly when specified — controls what the agent can use
        if tools is not None:
            payload_dict["tools"] = tools
        payload = _json.dumps(payload_dict).encode()

        try:
            # Use SigV4 signing via botocore
            import botocore.session
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest

            session = botocore.session.get_session()
            credentials = session.get_credentials().get_frozen_credentials()

            request = AWSRequest(
                method="POST",
                url=invoke_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                },
            )
            SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)

            req = urllib.request.Request(
                invoke_url,
                data=payload,
                headers=dict(request.headers),
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                response_body = resp.read().decode("utf-8")

            # Parse the response — Task Agent returns streaming newline-delimited JSON
            # or a single JSON response
            outputs = []
            logs_parts = []

            for line in response_body.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    event = _json.loads(line)
                    if isinstance(event, dict):
                        if event.get("type") == "text":
                            outputs.append(event.get("text", ""))
                        elif "text" in event:
                            outputs.append(event["text"])
                        elif "content" in event:
                            outputs.append(event["content"])
                        else:
                            outputs.append(_json.dumps(event))
                    else:
                        outputs.append(str(event))
                except _json.JSONDecodeError:
                    # Plain text line
                    outputs.append(line)

            final_output = "".join(outputs) if outputs else response_body

            return {
                "outputs": [final_output],
                "logs": "\n".join(logs_parts) if logs_parts else "Task Agent invocation completed",
                "tool_invocations": [],
            }

        except Exception as exc:
            logger.error("Task Agent invocation failed: %s", exc, exc_info=True)
            return {
                "outputs": [f"Task Agent invocation failed: {exc}"],
                "logs": f"Error: {exc}",
                "tool_invocations": [],
            }
