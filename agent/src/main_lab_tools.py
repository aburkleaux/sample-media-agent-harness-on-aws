"""Lab CRUD tools exposed as Strands @tool callables on the Main Agent.

This module is the user-interaction unification seam. The Main Agent absorbs
the LabService runtime's CRUD surface so the orchestrator can manage draft
skills / specialty agents / tool collections / workflows from the same chat
turn it dispatches research and execution work.

Identity is sourced ONLY from the AgentCore runtime context (JWT sub claim) —
the per-request LabService is built fresh in the Main Agent entrypoint and
made available to these tools through a contextvar so each turn binds the
correct actor.

Delete tools require an explicit ``confirm: true`` argument. Without it, they
return ``{"error": "confirmation_required", ...}`` without touching state.

All other CRUD wrappers marshal the typed kwargs into the LabService action
envelope and return the JSON-safe result dict that the runtime already emits.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Literal, Optional

from strands import Skill, tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request LabService binding
# ---------------------------------------------------------------------------
#
# Strands @tool callables are registered once at startup but invoked many
# times across requests. The active LabService for the *current* request is
# stashed in a contextvar so each tool call resolves the correct actor.

_current_lab_service: contextvars.ContextVar = contextvars.ContextVar(
    "current_lab_service", default=None
)


def set_current_lab_service(lab_service) -> contextvars.Token:
    """Bind a LabService for the current request. Returns the reset token."""
    return _current_lab_service.set(lab_service)


def reset_current_lab_service(token: contextvars.Token) -> None:
    """Reset the contextvar to its prior value (paired with ``set_current_lab_service``)."""
    _current_lab_service.reset(token)


def _service():
    svc = _current_lab_service.get()
    if svc is None:
        raise RuntimeError(
            "No LabService bound for this request. "
            "Main Agent entrypoint must call set_current_lab_service before invoking lab tools."
        )
    return svc


async def _dispatch(action: str, **payload) -> dict:
    """Dispatch an action to the request-bound LabService."""
    svc = _service()
    body = {"action": action, **{k: v for k, v in payload.items() if v is not None}}
    try:
        return await svc.handle(body)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("Lab tool %s failed", action)
        return {"error": type(exc).__name__, "message": str(exc)}


def _confirm_guard(asset_id: str, confirm: bool, kind: str) -> Optional[dict]:
    """Return an error dict when ``confirm`` is not strictly True."""
    if confirm is True:
        return None
    return {
        "error": "confirmation_required",
        "kind": kind,
        "assetId": asset_id,
        "message": (
            f"Refusing to delete {kind}={asset_id} without explicit confirmation. "
            "Pass confirm=true to proceed."
        ),
    }


async def lookup_published_agent(
    agent_id: str, version: Optional[int] = None
) -> dict:
    """Validate a published Custom Agent reference and pin its version.

    Public (not a ``@tool``): ``custom_agent_tool`` calls this, the model does
    not. Returns ``{assetId, name, description, version}`` on success, or a
    ``{"error", "message"}`` dict the caller can hand back verbatim.
    """
    return await _dispatch(
        "ResolvePublishedAgentRef", assetId=agent_id, version=version
    )


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------


@tool
async def list_skill_drafts() -> str:
    """List every Skill draft owned by the current user. Returns JSON array of {assetId, name, ...}."""
    result = await _dispatch("ListSkillDrafts")
    return json.dumps(result)


@tool
async def create_skill_draft(name: str, skill_md: str) -> str:
    """Create a new Skill draft.

    Args:
        name: Human-readable skill name (1-128 chars).
        skill_md: Body of SKILL.md describing what the skill does and which tools it uses.
    """
    result = await _dispatch("CreateSkillDraft", name=name, skillMd=skill_md)
    return json.dumps(result)


@tool
async def read_skill_draft(asset_id: str) -> str:
    """Read a Skill draft by assetId. Returns the full record including SKILL.md and supporting files."""
    result = await _dispatch("ReadSkillDraft", assetId=asset_id)
    return json.dumps(result)


@tool
async def update_skill_md(asset_id: str, skill_md: str) -> str:
    """Replace the SKILL.md body of an existing skill draft.

    Args:
        asset_id: The skill's assetId.
        skill_md: New SKILL.md content (full body, not a patch).
    """
    result = await _dispatch("UpdateSkillMd", assetId=asset_id, skillMd=skill_md)
    return json.dumps(result)


@tool
async def delete_skill_draft(asset_id: str, confirm: bool = False) -> str:
    """Permanently delete a skill draft. Requires confirm=true.

    Args:
        asset_id: The skill's assetId.
        confirm: Must be set to true. Without it the tool refuses and returns confirmation_required.
    """
    guard = _confirm_guard(asset_id, confirm, "skill")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch("DeleteSkillDraft", assetId=asset_id)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Specialty Agent tools
# ---------------------------------------------------------------------------


@tool
async def list_specialty_agent_drafts() -> str:
    """List every Specialty Agent draft owned by the current user."""
    result = await _dispatch("ListSpecialtyAgentDrafts")
    return json.dumps(result)


@tool
async def create_specialty_agent_draft(
    name: str, persona: str, compatible_skills: Optional[list[str]] = None,
    description: str = "",
) -> str:
    """Create a new Specialty Agent draft.

    Args:
        name: Agent name (1-128 chars).
        persona: System-prompt-style persona description.
        compatible_skills: Optional list of skill names this agent can use.
        description: One-line summary of what this agent is for. Used to
            discover the agent later via `list_custom_agents`, so write it for
            a reader deciding whether to delegate to it.
    """
    result = await _dispatch(
        "CreateSpecialtyAgentDraft",
        name=name,
        persona=persona,
        compatibleSkills=compatible_skills,
        description=description,
    )
    return json.dumps(result)


@tool
async def read_specialty_agent_draft(asset_id: str) -> str:
    """Read a Specialty Agent draft by assetId."""
    result = await _dispatch("ReadSpecialtyAgentDraft", assetId=asset_id)
    return json.dumps(result)


@tool
async def update_specialty_agent_draft(
    asset_id: str,
    persona: Optional[str] = None,
    compatible_skills: Optional[list[str]] = None,
    description: Optional[str] = None,
) -> str:
    """Update a Specialty Agent's persona, compatible skills, or description."""
    result = await _dispatch(
        "UpdateSpecialtyAgentDraft",
        assetId=asset_id,
        persona=persona,
        compatibleSkills=compatible_skills,
        description=description,
    )
    return json.dumps(result)


@tool
async def delete_specialty_agent_draft(asset_id: str, confirm: bool = False) -> str:
    """Permanently delete a Specialty Agent draft. Requires confirm=true."""
    guard = _confirm_guard(asset_id, confirm, "specialty-agent")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch("DeleteSpecialtyAgentDraft", assetId=asset_id)
    return json.dumps(result)


@tool
async def list_custom_agents() -> str:
    """List the user's own published Custom Agents available for delegation.

    Call this before `invoke_custom_agent` — it returns the `assetId` that tool
    needs, plus a `description` saying what each agent is for. Only published
    agents appear; drafts are not invocable from chat.
    """
    result = await _dispatch(
        "ListMyPublishedAssets", category="specialty-agent"
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool Collection tools
# ---------------------------------------------------------------------------


@tool
async def list_tool_collection_drafts() -> str:
    """List every Tool Collection draft owned by the current user."""
    result = await _dispatch("ListToolCollectionDrafts")
    return json.dumps(result)


@tool
async def create_tool_collection_draft(name: str, tool_refs: list[dict]) -> str:
    """Create a new Tool Collection draft.

    Args:
        name: Collection name.
        tool_refs: List of {name, version} dicts referencing Gateway tools.
    """
    result = await _dispatch(
        "CreateToolCollectionDraft", name=name, toolRefs=tool_refs
    )
    return json.dumps(result)


@tool
async def read_tool_collection_draft(asset_id: str) -> str:
    """Read a Tool Collection draft by assetId."""
    result = await _dispatch("ReadToolCollectionDraft", assetId=asset_id)
    return json.dumps(result)


@tool
async def update_tool_collection_draft(
    asset_id: str,
    name: Optional[str] = None,
    tool_refs: Optional[list[dict]] = None,
) -> str:
    """Update a Tool Collection's name or tool references."""
    result = await _dispatch(
        "UpdateToolCollectionDraft",
        assetId=asset_id,
        name=name,
        toolRefs=tool_refs,
    )
    return json.dumps(result)


@tool
async def delete_tool_collection_draft(
    asset_id: str, confirm: bool = False, force: bool = False
) -> str:
    """Permanently delete a Tool Collection. Requires confirm=true. Refuses with error 'still_referenced' (listing the agents) if a specialty agent still selects it — tell the user which agents, and remove the collection from them first. Pass force=true only when the user has been shown that list and asked to proceed anyway."""
    guard = _confirm_guard(asset_id, confirm, "tool-collection")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch(
        "DeleteToolCollectionDraft", assetId=asset_id, force=force
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Hook tools
# ---------------------------------------------------------------------------


HookEvent = Literal[
    "after_tool_result", "before_tool", "session_start", "session_end"
]
HookAction = Literal["render", "notify", "dispatch"]


@tool
async def list_hooks() -> str:
    """List the current user's hooks and the read-only built-in hooks."""
    result = await _dispatch("ListHooks")
    return json.dumps(result)


@tool
async def create_hook(
    name: str,
    event: HookEvent,
    condition: str = "",
    action_type: HookAction = "notify",
    action_params: Optional[dict] = None,
    priority: int = 100,
    enabled: bool = True,
) -> str:
    """Create a declarative user hook.

    Hooks become effective immediately when enabled; they do not use the
    publish lifecycle. Validate non-trivial conditions before creating.
    Built-ins are read-only; create a same-name user hook to override one.

    Args:
        name: Hook name. A built-in name creates an override.
        event: Lifecycle event that triggers evaluation.
        condition: Restricted boolean expression over the normalized event.
        action_type: render, notify, or dispatch.
        action_params: Action-specific parameters.
        priority: Lower values run first.
        enabled: Whether the hook is active immediately.
    """
    result = await _dispatch(
        "CreateHook",
        name=name,
        event=event,
        condition=condition,
        actionType=action_type,
        actionParams=action_params,
        priority=priority,
        enabled=enabled,
    )
    return json.dumps(result)


@tool
async def read_hook(asset_id: str) -> str:
    """Read a user hook by assetId. Built-ins are returned by list_hooks."""
    result = await _dispatch("ReadHook", assetId=asset_id)
    return json.dumps(result)


@tool
async def update_hook(
    asset_id: str,
    name: Optional[str] = None,
    event: Optional[HookEvent] = None,
    condition: Optional[str] = None,
    action_type: Optional[HookAction] = None,
    action_params: Optional[dict] = None,
    priority: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> str:
    """Update only the supplied fields of a user hook.

    Read the hook first when changing its action. Validate a new non-trivial
    condition before updating. Changes are effective immediately when enabled.
    """
    result = await _dispatch(
        "UpdateHook",
        assetId=asset_id,
        name=name,
        event=event,
        condition=condition,
        actionType=action_type,
        actionParams=action_params,
        priority=priority,
        enabled=enabled,
    )
    return json.dumps(result)


@tool
async def delete_hook(asset_id: str, confirm: bool = False) -> str:
    """Permanently delete a user hook. Requires confirm=true."""
    guard = _confirm_guard(asset_id, confirm, "hook")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch("DeleteHook", assetId=asset_id)
    return json.dumps(result)


@tool
async def validate_hook_condition(
    condition: str, sample_event: Optional[dict] = None
) -> str:
    """Validate a restricted hook expression and optionally dry-run it.

    Returns {valid, error?} and, when sample_event is provided, {matched}.
    This never creates or updates a hook.
    """
    result = await _dispatch(
        "ValidateHookCondition", condition=condition, sampleEvent=sample_event
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Workflow tools
# ---------------------------------------------------------------------------


@tool
async def list_workflow_drafts() -> str:
    """List every Workflow draft owned by the current user."""
    result = await _dispatch("ListWorkflowDrafts")
    return json.dumps(result)


@tool
async def create_workflow_draft(name: str, steps: list[dict]) -> str:
    """Create a new Workflow draft.

    Args:
        name: Workflow name.
        steps: Ordered list of step dicts. Each step has shape:
          {stepId, agent: {kind, ref?, assetId?}, skill: {assetId},
           toolCollection: {assetId}, prompt, inputs}.
    """
    result = await _dispatch("CreateWorkflowDraft", name=name, steps=steps)
    return json.dumps(result)


@tool
async def read_workflow_draft(asset_id: str) -> str:
    """Read a Workflow draft by assetId. Returns steps and the resolved-view."""
    result = await _dispatch("ReadWorkflowDraft", assetId=asset_id)
    return json.dumps(result)


@tool
async def update_workflow_draft(
    asset_id: str,
    name: Optional[str] = None,
    steps: Optional[list[dict]] = None,
) -> str:
    """Update a Workflow's name or steps (steps replace the existing list)."""
    result = await _dispatch(
        "UpdateWorkflowDraft", assetId=asset_id, name=name, steps=steps
    )
    return json.dumps(result)


@tool
async def delete_workflow_draft(asset_id: str, confirm: bool = False) -> str:
    """Permanently delete a Workflow draft. Requires confirm=true."""
    guard = _confirm_guard(asset_id, confirm, "workflow")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch("DeleteWorkflowDraft", assetId=asset_id)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Lifecycle tools — publish / edit / discard / list versions
# ---------------------------------------------------------------------------


_VALID_CATEGORIES = ("skill", "specialty-agent", "tool-collection", "workflow")

#: The categories that have a version sub-tier, and therefore the only ones the
#: versioned lifecycle tools (publish / fork-a-version / discard-back-to-a-
#: version) can act on. Tool collections are flat: they have no published form
#: at all — ``lab._canonical_ops._VERSIONED_CATEGORIES`` excludes them and
#: ``ToolCollectionCRUD.promote`` raises ``NotImplementedError`` — so naming
#: ``tool-collection`` in a publish/edit docstring only taught the model to make
#: a call the server can never do anything but reject. Mirror of that frozenset;
#: kept as a plain tuple here so the tool layer does not import the lab package.
_VERSIONED_CATEGORIES = ("skill", "specialty-agent", "workflow")


def _validate_category(
    category: str, allowed: tuple[str, ...] = _VALID_CATEGORIES
) -> Optional[dict]:
    if category not in allowed:
        return {
            "error": "invalid_category",
            "message": f"category must be one of {allowed}, got {category!r}",
        }
    return None


@tool
async def publish_asset(
    category: str,
    asset_id: str,
    scope: str = "private",
    group_ids: Optional[list[str]] = None,
) -> str:
    """Publish working copy as next immutable version. Working copy is removed; canonical state becomes the new version. category: skill|specialty-agent|workflow (tool collections are flat and cannot be published). scope: private|group|org."""
    err = _validate_category(category, _VERSIONED_CATEGORIES)
    if err is not None:
        return json.dumps(err)
    result = await _dispatch(
        "PublishAsset",
        category=category,
        assetId=asset_id,
        scope=scope,
        groupIds=group_ids,
    )
    return json.dumps(result)


@tool
async def edit_asset(
    category: str, asset_id: str, from_version: Optional[int] = None
) -> str:
    """Fork a published version into an editable working copy. Use BEFORE update_* tools. Refuses if a working copy already exists. Defaults to the current version; pass from_version to fork a specific historical version. category: skill|specialty-agent|workflow (tool collections have no versions to fork — edit the collection directly). After forking, tell the user: "Forking version V of <name> as your working copy. Only you can see this until you publish.\""""
    err = _validate_category(category, _VERSIONED_CATEGORIES)
    if err is not None:
        return json.dumps(err)
    result = await _dispatch(
        "EditAsset",
        category=category,
        assetId=asset_id,
        fromVersion=from_version,
    )
    return json.dumps(result)


@tool
async def discard_changes(category: str, asset_id: str, confirm: bool = False) -> str:
    """Discard unpublished working copy. Latest version unaffected. Requires confirm=true. category: skill|specialty-agent|workflow — a flat tool collection has no version to fall back to, so discarding it would be an outright delete; use delete_tool_collection_draft, which checks for agents still referencing it."""
    err = _validate_category(category, _VERSIONED_CATEGORIES)
    if err is not None:
        return json.dumps(err)
    guard = _confirm_guard(asset_id, confirm, f"{category}-working-copy")
    if guard is not None:
        return json.dumps(guard)
    result = await _dispatch(
        "DiscardChanges", category=category, assetId=asset_id
    )
    return json.dumps(result)


@tool
async def list_asset_versions(asset_id: str) -> str:
    """List every published version of an asset with deprecation and current flags."""
    result = await _dispatch("ListAssetVersions", assetId=asset_id)
    return json.dumps(result)


@tool
async def list_my_published_assets(category: str) -> str:
    """List the user's own published assets in a category. Returns latest non-deprecated version per asset.

    category: skill|specialty-agent|workflow. Tool collections are flat and have
    no published form, so they would always list as empty.
    """
    err = _validate_category(category, _VERSIONED_CATEGORIES)
    if err is not None:
        return json.dumps(err)
    result = await _dispatch("ListMyPublishedAssets", category=category)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------


_INDIVIDUAL_LAB_TOOLS: list[Any] = [
    # Skills (5)
    list_skill_drafts,
    create_skill_draft,
    read_skill_draft,
    update_skill_md,
    delete_skill_draft,
    # Specialty Agents (6)
    list_specialty_agent_drafts,
    create_specialty_agent_draft,
    read_specialty_agent_draft,
    update_specialty_agent_draft,
    delete_specialty_agent_draft,
    list_custom_agents,
    # Tool Collections (5)
    list_tool_collection_drafts,
    create_tool_collection_draft,
    read_tool_collection_draft,
    update_tool_collection_draft,
    delete_tool_collection_draft,
    # Hooks (6; draft metadata is effective immediately, no publish lifecycle)
    list_hooks,
    create_hook,
    read_hook,
    update_hook,
    delete_hook,
    validate_hook_condition,
    # Workflows (5)
    list_workflow_drafts,
    create_workflow_draft,
    read_workflow_draft,
    update_workflow_draft,
    delete_workflow_draft,
    # Lifecycle (5) — publish / edit / discard / list versions / list published
    publish_asset,
    edit_asset,
    discard_changes,
    list_asset_versions,
    list_my_published_assets,
]

# ---------------------------------------------------------------------------
# Single dispatcher over the CRUD surface above
# ---------------------------------------------------------------------------
#
# The 33 wrappers above were bound individually, so every conversation carried
# the whole authoring API — 73% of the builtin tool COUNT — including turns that
# would never publish an asset. Each extra tool is another candidate the model
# weighs when selecting, and a media-processing request has no use for
# delete_tool_collection_draft.
#
# Collapsed to one entry rather than gated on a mode: a tool set that varies per
# turn invalidates the cached prompt prefix, which would trade ~4,700 bytes of
# schema for a full ~19k-token prefix rewrite. This dispatcher is static, so the
# prefix stays stable.
#
# Parameters are carried as a dict rather than a 33-member typed union (which
# would be as large as what it replaced). The typing is recovered on demand via
# action="describe", and the registry below is DERIVED from the wrappers so the
# two can never drift.

_LAB_ACTIONS: dict[str, dict] = {}


def _build_lab_action_registry() -> None:
    """Derive the action registry from the individual tools.

    Introspected rather than hand-listed: a hand-maintained table would drift
    the moment someone adds a wrapper above, and the failure would be a silently
    missing action.
    """
    for tool_obj in _INDIVIDUAL_LAB_TOOLS:
        fn = getattr(tool_obj, "__wrapped__", None)
        if fn is None:
            continue
        spec = getattr(tool_obj, "tool_spec", None) or {}
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        params: dict[str, dict] = {}
        for pname, param in sig.parameters.items():
            annotation = param.annotation
            type_name = getattr(annotation, "__name__", None) or str(annotation)
            params[pname] = {
                "type": type_name.replace("inspect._empty", "any"),
                "required": param.default is inspect.Parameter.empty,
            }
        _LAB_ACTIONS[fn.__name__] = {
            "fn": fn,
            "description": (spec.get("description") or "").strip(),
            "params": params,
        }


_build_lab_action_registry()


@tool
async def lab_asset(action: str, params: dict | None = None) -> str:
    """Manage Lab assets: Skills, Specialty Agents, Tool Collections, Workflows and Hooks.

    `action` names the operation. Drafts follow `<verb>_<asset>` where verb is
    list/create/read/update/delete and asset is one of skill_draft,
    specialty_agent_draft, tool_collection_draft, workflow_draft (skill content
    updates use update_skill_md). Hooks: create_hook, read_hook, update_hook,
    delete_hook, list_hooks, validate_hook_condition. Lifecycle: publish_asset,
    edit_asset, discard_changes, list_asset_versions, list_my_published_assets.
    Delegation: list_custom_agents — the user's own PUBLISHED Custom Agents,
    with the assetId and description `invoke_custom_agent` needs. Takes no
    params.

    Pass that action's arguments in `params`. If you are unsure of them, call
    action="describe" with params={"action": "<name>"} for one action, or
    action="describe" with no params to list every action and its arguments.

    Returns JSON — the operation's result, or {"error": ...} naming the valid
    actions or the missing arguments.
    """
    if action == "describe":
        wanted = (params or {}).get("action")
        if wanted:
            entry = _LAB_ACTIONS.get(wanted)
            if entry is None:
                return json.dumps({"error": f"unknown action: {wanted}",
                                   "actions": sorted(_LAB_ACTIONS)})
            return json.dumps({"action": wanted, "description": entry["description"],
                               "params": entry["params"]})
        return json.dumps({
            "actions": {name: {"params": e["params"]} for name, e in sorted(_LAB_ACTIONS.items())}
        })

    entry = _LAB_ACTIONS.get(action)
    if entry is None:
        return json.dumps({"error": f"unknown action: {action}",
                           "actions": sorted(_LAB_ACTIONS)})

    supplied = dict(params or {})
    expected = entry["params"]
    unknown = [k for k in supplied if k not in expected]
    missing = [k for k, meta in expected.items() if meta["required"] and k not in supplied]
    if missing or unknown:
        # Named explicitly: an untyped params dict cannot be schema-validated, so
        # the error has to carry what a schema would have.
        return json.dumps({
            "error": "invalid params for " + action,
            **({"missing": missing} if missing else {}),
            **({"unknown": unknown} if unknown else {}),
            "params": expected,
        })

    return await entry["fn"](**supplied)


# What the Main Agent binds. One entry instead of 33; the individual wrappers
# remain importable for direct use and tests.
LAB_TOOLS: list[Any] = [lab_asset]



# Action set the Main Agent uses to detect direct-CRUD bypass payloads. Kept
# in sync with the LabService dispatch table.
LAB_BYPASS_ACTIONS: frozenset[str] = frozenset({
    # Skills
    "CreateSkillDraft", "ImportSkillPackage", "ReadSkillDraft", "UpdateSkillMd",
    "ReadPublishedSkill",
    "PutSupportingFile", "DeleteSupportingFile",
    "DeleteSkillDraft", "ListSkillDrafts",
    # Specialty Agents
    "CreateSpecialtyAgentDraft", "ReadSpecialtyAgentDraft",
    "UpdateSpecialtyAgentDraft", "DeleteSpecialtyAgentDraft",
    "ListSpecialtyAgentDrafts", "ResolveAgentConfig", "ResolvePublishedAgentRef",
    # Tool Collections
    "CreateToolCollectionDraft", "ReadToolCollectionDraft",
    "UpdateToolCollectionDraft", "DeleteToolCollectionDraft",
    "ListToolCollectionDrafts",
    # Workflows
    "CreateWorkflowDraft", "ReadWorkflowDraft", "UpdateWorkflowDraft",
    "DeleteWorkflowDraft", "ListWorkflowDrafts",
    # Hooks (declarative harness triggers)
    "CreateHook", "ReadHook", "UpdateHook", "DeleteHook", "ListHooks",
    "ValidateHookCondition",
    # Gateway
    "BrowseGatewayTools", "RefreshGatewayTools", "TestGatewayTool",
    # Chat / Harness / Readiness / Generic
    "ChatMessage", "RunHarness", "ListHarnessRuns", "ReadHarnessRun",
    "CheckReadiness", "RenameAsset",
    # Lifecycle
    "PublishAsset", "EditAsset", "DiscardChanges", "ListAssetVersions",
    "ListMyPublishedAssets",
})


def render_inventory_section(snapshot) -> str:
    """Render an InventorySnapshot as a system-prompt section.

    Thin wrapper over snapshot.format_as_prompt_section so the Main Agent's
    prompt builder doesn't have to import from the lab package.
    """
    if snapshot is None:
        return ""
    return snapshot.format_as_prompt_section()


# ---------------------------------------------------------------------------
# Per-request skill auto-load (asset-lifecycle-redesign Stage A)
#
# Owner's draft Skills become runnable as soon as they exist — no separate
# "promote" step required. Each turn, the main agent invokes
# load_owned_drafts_into_skills_manager, which returns request-local Skill
# objects and temporary package roots. The process-global plugin is never
# mutated; main.py builds an isolated plugin and cleans package roots when the
# request stream ends.
# ---------------------------------------------------------------------------


async def load_owned_drafts_into_skills_manager(
    skills_manager,
    lab_service,
) -> tuple[list[Skill], list[Path]]:
    """Build request-scoped owned Skills without exposing them globally.

    Working copies are returned before published versions, so a working copy
    wins when both resolve to the same runtime name. Package-backed Skills are
    materialized under actor-specific hashed temporary roots and must be
    cleaned after the request finishes.
    """
    if lab_service is None or skills_manager is None:
        return [], []

    from lab.asset_index import make_pk_user_owned, make_pk_user_versions

    actor_id = lab_service.ctx.actor_id
    actor_skills: list[Skill] = []
    package_roots: list[Path] = []

    try:
        owned_items, _ = await lab_service.index.query_pk(
            make_pk_user_owned(actor_id), sk_prefix="skill#"
        )
    except Exception as exc:
        logger.warning("Owned-skills list failed: %s", exc)
        owned_items = []

    for item in owned_items:
        try:
            record = await lab_service.skill_crud.read(
                ctx=lab_service.ctx, asset_id=item.asset_id
            )
            if record is None or not record.skill_md:
                continue
            slug = _slugify_skill_name(record.name) or record.asset_id
            if record.supporting_files:
                package_files = {
                    path: await lab_service.s3.read(
                        f"drafts/skills/{record.asset_id}/{path}"
                    )
                    for path in record.supporting_files
                }
                skill, package_root = skills_manager.materialize_runtime_skill_package(
                    name=slug,
                    skill_md=record.skill_md,
                    supporting_files=package_files,
                    package_id=(
                        f"{actor_id}::{lab_service.ctx.request_id}::owned::"
                        f"{record.asset_id}"
                    ),
                )
                actor_skills.append(skill)
                package_roots.append(package_root)
            else:
                actor_skills.append(Skill(
                    name=slug,
                    description=f"Your skill (working copy): {record.name}",
                    instructions=record.skill_md,
                ))
        except Exception as exc:
            logger.warning(
                "Failed to load owned working copy %s (%s): %s",
                item.asset_id, item.name, exc,
            )

    try:
        version_items, _ = await lab_service.index.query_pk(
            make_pk_user_versions(actor_id), sk_prefix="skill#"
        )
    except Exception as exc:
        logger.warning("Owned-versions list failed: %s", exc)
        version_items = []

    latest_per_asset: dict[str, Any] = {}
    for version in version_items:
        if version.deprecated or version.version is None:
            continue
        existing = latest_per_asset.get(version.asset_id)
        if existing is None or version.version > existing.version:
            latest_per_asset[version.asset_id] = version

    for asset_id, version in latest_per_asset.items():
        try:
            import asyncio as _asyncio
            s3_client = lab_service.s3._s3
            bucket = lab_service.s3.bucket
            key = f"{version.s3_prefix}SKILL.md"

            def _get_package():
                response = s3_client.get_object(Bucket=bucket, Key=key)
                files: dict[str, bytes] = {}
                paginator = s3_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=version.s3_prefix):
                    for entry in page.get("Contents", []):
                        object_key = entry.get("Key", "")
                        if not object_key or object_key == key:
                            continue
                        relative = object_key[len(version.s3_prefix):]
                        file_response = s3_client.get_object(Bucket=bucket, Key=object_key)
                        files[relative] = file_response["Body"].read()
                return response["Body"].read(), files

            skill_md_bytes, package_files = await _asyncio.to_thread(_get_package)
            skill_md = skill_md_bytes.decode("utf-8") if skill_md_bytes else ""
            if not skill_md:
                continue
            slug = _slugify_skill_name(version.name) or asset_id
            if package_files:
                skill, package_root = skills_manager.materialize_runtime_skill_package(
                    name=slug,
                    skill_md=skill_md,
                    supporting_files=package_files,
                    package_id=(
                        f"{actor_id}::{lab_service.ctx.request_id}::"
                        f"v{version.version}::{asset_id}"
                    ),
                )
                actor_skills.append(skill)
                package_roots.append(package_root)
            else:
                actor_skills.append(Skill(
                    name=slug,
                    description=f"Your skill (v{version.version}): {version.name}",
                    instructions=skill_md,
                ))
        except Exception as exc:
            logger.warning(
                "Failed to load published skill %s v%s: %s",
                asset_id, version.version, exc,
            )

    if actor_skills:
        logger.info("Prepared %d request-scoped owned skill(s)", len(actor_skills))
    return actor_skills, package_roots


def _slugify_skill_name(name: str) -> str:
    """Convert a human-readable skill name to a kebab-case identifier.

    Skill names in the runtime catalog are kebab-case by convention
    ("pdf-processing", "code-review"). User-typed names like
    "Content Moderation: Hate Speech / Discrimination" become
    "content-moderation-hate-speech-discrimination".
    """
    import re
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s
