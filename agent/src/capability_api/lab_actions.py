"""Which ``LabService`` actions this process may serve, and why the rest may not.

The Capability API mounts the **same** dispatcher as the agent runtime — one
implementation, two hosts, so a response cannot differ by which host answered
(see ``docs/superpowers/specs/2026-08-24-control-plane-capability-migration.md``).
That is only safe with a gate, because this process is missing three things the
runtime has: a model, live MCP sessions, and — deliberately — any
``bedrock-agentcore:InvokeAgentRuntime`` permission. There is no fallback: an
action that needs the runtime cannot be forwarded from here, only refused.

**The split is by dependency, not by verb.** An action belongs to the control
plane iff neither its handler nor anything it delegates to touches
``gateway_browser``, ``chat_agent``, ``test_harness`` or ``readiness``. Both
directions of that rule are counter-intuitive and both are real:

* ``PublishAsset`` *writes*, and is control-plane — it is DynamoDB + S3 only.
* ``ResolveAgentConfig`` *reads*, and is not — it resolves tool bindings against
  the live gateway projection.

The transitive half matters most for the two tool-collection writes. Their
handlers never name the gateway; ``ToolCollectionCRUD.create/update`` reaches it
via ``_validate_tool_refs``. Serving them here would not crash — control-plane
gateway enumeration returns **nothing** for MCP-server targets, so ref validation
would quietly reject legitimate federated tools. Refusing is the safe answer.

``tests/test_capability_api_lab_actions.py`` asserts this gate is *total*: every
action in the dispatch table is classified, and nothing is classified that the
dispatcher does not have.
"""

from __future__ import annotations

from typing import Optional

#: Actions this process serves: pure DynamoDB + S3, no model, no gateway.
CONTROL_PLANE_ACTIONS: frozenset[str] = frozenset(
    {
        # --- Skills: authored content lives in S3, the row in DynamoDB.
        "CreateSkillDraft",
        "ImportSkillPackage",
        "ReadSkillDraft",
        "ReadPublishedSkill",
        "UpdateSkillMd",
        "PutSupportingFile",
        "DeleteSupportingFile",
        "DeleteSkillDraft",
        "ListSkillDrafts",
        # --- Custom agents. `ResolveAgentConfig` is the exception, below.
        "CreateSpecialtyAgentDraft",
        "ReadSpecialtyAgentDraft",
        "UpdateSpecialtyAgentDraft",
        "DeleteSpecialtyAgentDraft",
        "ListSpecialtyAgentDrafts",
        "ResolvePublishedAgentRef",
        # --- Tool collections: reads and delete only. Create/update validate
        # refs against the gateway and are refused here.
        "ReadToolCollectionDraft",
        "DeleteToolCollectionDraft",
        "ListToolCollectionDrafts",
        # --- Workflows.
        "CreateWorkflowDraft",
        "ReadWorkflowDraft",
        "UpdateWorkflowDraft",
        "DeleteWorkflowDraft",
        "ListWorkflowDrafts",
        # --- Hooks. `ValidateHookCondition` evaluates an expression against a
        # sample event; it calls nothing.
        "CreateHook",
        "ReadHook",
        "UpdateHook",
        "DeleteHook",
        "ListHooks",
        "ValidateHookCondition",
        # --- Lifecycle, any category. All row + object manipulation.
        "RenameAsset",
        "PublishAsset",
        "EditAsset",
        "DiscardChanges",
        "ListAssetVersions",
        "ListMyPublishedAssets",
    }
)

#: Actions that must go to the runtime, each with the reason the caller is told.
#: The reason is user-visible: it is what tells a client which host to use, so it
#: names the missing dependency rather than saying "not supported".
RUNTIME_ONLY_ACTIONS: dict[str, str] = {
    "ChatMessage": (
        "needs model inference; the control plane has no Bedrock invoke path"
    ),
    "RunHarness": (
        "invokes the agent under test; needs the runtime's model and tool session"
    ),
    "ListHarnessRuns": (
        "harness run records stay with the runtime that writes them; no UI caller"
    ),
    "ReadHarnessRun": (
        "harness run records stay with the runtime that writes them; no UI caller"
    ),
    "CheckReadiness": (
        "the readiness scan resolves tools against the live gateway projection"
    ),
    "BrowseGatewayTools": (
        "needs a live MCP gateway session; the control plane sees no MCP-server "
        "target tools"
    ),
    "RefreshGatewayTools": (
        "needs a live MCP gateway session to re-read the tool projection"
    ),
    "TestGatewayTool": (
        "executes a gateway tool, which is a data-plane MCP call"
    ),
    "ResolveAgentConfig": (
        "resolves which tools actually bind, against the live gateway projection"
    ),
    "CreateToolCollectionDraft": (
        "validates tool refs against the live gateway; control-plane enumeration "
        "omits MCP-server target tools and would reject valid refs"
    ),
    "UpdateToolCollectionDraft": (
        "re-validates tool refs against the live gateway; control-plane "
        "enumeration omits MCP-server target tools and would reject valid refs"
    ),
}

#: Actions that need no gateway and no model, yet are still refused here.
#:
#: These are a *product* boundary, not a dependency one, and separating the two
#: kinds of "no" is what lets the invariant below be checked mechanically.
#: Harness run records are written by the runtime and read by nothing in the SPA
#: (``ui/src/lab-api.ts`` notes "ListHarnessRuns has no client"), so serving them
#: from a second process would widen the surface for no caller.
RUNTIME_OWNED_READS: frozenset[str] = frozenset(
    {"ListHarnessRuns", "ReadHarnessRun"}
)

#: The invariant tying this module to ``main.LAB_GATEWAY_FREE_ACTIONS``:
#:
#:     LAB_GATEWAY_FREE_ACTIONS == CONTROL_PLANE_ACTIONS | RUNTIME_OWNED_READS
#:
#: Both lists answer "does this action need a live gateway?" — one to skip the
#: runtime's ~8.3 s MCP handshake, the other to decide which host may serve it.
#: Two hand-maintained answers to one question drift; they had already drifted in
#: both directions when this module was written. ``tests/test_capability_api_lab_actions.py``
#: asserts the equality so the next divergence fails a test instead of shipping.
GATEWAY_FREE_ACTIONS: frozenset[str] = CONTROL_PLANE_ACTIONS | RUNTIME_OWNED_READS

#: Error code returned for an action this process will not serve. Distinct from a
#: validation error: the request is well-formed and the caller is authorized — it
#: simply arrived at the wrong host.
WRONG_HOST_CODE = "action_not_available_here"


def is_control_plane(action: str) -> bool:
    """True iff this process may serve ``action``.

    Fail-closed on anything unrecognised: an action absent from both sets is
    refused rather than attempted, so a newly added dispatch entry cannot become
    reachable here just by existing.
    """
    return action in CONTROL_PLANE_ACTIONS


def runtime_only_reason(action: str) -> Optional[str]:
    """Why ``action`` must go to the runtime, or ``None`` if it need not.

    Returns a reason for unclassified actions too — callers need something to
    report, and "unknown here" is the honest version of it.
    """
    if action in CONTROL_PLANE_ACTIONS:
        return None
    return RUNTIME_ONLY_ACTIONS.get(
        action,
        "not served by the control plane; send it to the agent runtime",
    )
