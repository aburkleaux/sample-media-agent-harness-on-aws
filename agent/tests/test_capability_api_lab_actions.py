"""Which lab actions the control plane may serve — the gate, and its totality.

The Capability API mounts the **same** ``LabService`` dispatcher the runtime uses.
Without a gate, every action would be reachable from a process that has no model,
no MCP session, and — deliberately — no ``InvokeAgentRuntime`` permission, so a
data-plane action would not fail cleanly there: it would fail as a 500 from a
gateway browser that cannot connect, or worse, succeed against an *empty* tool
projection and quietly reject valid input.

So these tests are about the gate being **total**, not about its current contents.
An action added to the dispatch table later must be classified deliberately, or
this suite fails — rather than the new action silently becoming reachable in a
process that cannot serve it.

The split is by **dependency, not by verb**, which is the part that is easy to get
wrong twice over: ``PublishAsset`` is a write and belongs to the control plane,
while ``ResolveAgentConfig`` is a read and does not. Both directions are asserted
below so the rule survives someone "tidying" the sets by CRUD shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import lab.service as service_mod
from capability_api.lab_actions import (
    CONTROL_PLANE_ACTIONS,
    GATEWAY_FREE_ACTIONS,
    RUNTIME_ONLY_ACTIONS,
    RUNTIME_OWNED_READS,
    is_control_plane,
    runtime_only_reason,
)


def _lab_gateway_free_actions() -> set[str]:
    """``main.LAB_GATEWAY_FREE_ACTIONS``, read from source rather than imported.

    Importing ``main`` pulls the whole Runtime stack (strands, bedrock_agentcore)
    into a test about a control-plane module. Parsing mirrors what
    ``ui/src/capability-api.routing.contract.test.ts`` does with the CDK stack, and
    keeps this test runnable in the same import-isolated way the module it guards
    has to be.
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()
    match = re.search(
        r"LAB_GATEWAY_FREE_ACTIONS: frozenset\[str\] = frozenset\(\{(.*?)\n\}\)",
        src,
        re.S,
    )
    assert match, "could not locate LAB_GATEWAY_FREE_ACTIONS in src/main.py"
    # Strip comments first: the block explains its own omissions in prose, and
    # those explanations name the very actions being excluded.
    body = re.sub(r"#.*", "", match.group(1))
    return set(re.findall(r'"([A-Za-z]+)"', body))


def _dispatch_actions() -> set[str]:
    """The real dispatch table, read off a LabService built with fakes.

    Deliberately not a hand-copied list: the point of these tests is to notice
    when `lab/service.py` grows an action, which a duplicated list cannot do.
    """
    service = service_mod.LabService(
        ctx=MagicMock(),
        s3=MagicMock(),
        index=MagicMock(),
        gateway_browser=MagicMock(),
    )
    return set(service._action_handlers)


def test_every_dispatch_action_is_classified() -> None:
    """No action may be unclassified — that is the whole contract."""
    actions = _dispatch_actions()
    classified = CONTROL_PLANE_ACTIONS | set(RUNTIME_ONLY_ACTIONS)
    unclassified = actions - classified
    assert not unclassified, (
        "these lab actions are in the dispatch table but classified neither "
        f"control-plane nor runtime-only: {sorted(unclassified)}. Decide by "
        "dependency: does the handler — or anything it delegates to — touch "
        "gateway_browser, chat_agent, test_harness or readiness?"
    )


def test_no_action_is_classified_both_ways() -> None:
    overlap = CONTROL_PLANE_ACTIONS & set(RUNTIME_ONLY_ACTIONS)
    assert not overlap, f"classified twice: {sorted(overlap)}"


def test_classification_does_not_name_actions_that_do_not_exist() -> None:
    """A typo'd action name would otherwise sit in a set matching nothing.

    Left unchecked, a misspelling in CONTROL_PLANE_ACTIONS is invisible: the
    action never arrives under that name, so nothing fails — while the real
    action stays unclassified and gets refused.
    """
    actions = _dispatch_actions()
    classified = CONTROL_PLANE_ACTIONS | set(RUNTIME_ONLY_ACTIONS)
    phantom = classified - actions
    assert not phantom, (
        f"classified but absent from the dispatch table: {sorted(phantom)}"
    )


def test_every_runtime_only_action_states_why() -> None:
    """The reason is returned to the caller, so it must be worth reading."""
    for action, reason in RUNTIME_ONLY_ACTIONS.items():
        assert reason and reason.strip(), f"{action} has no reason"
        assert len(reason) > 15, f"{action}'s reason is too terse to help: {reason!r}"


def test_publish_is_control_plane_even_though_it_writes() -> None:
    """Verb is not the criterion. Publishing is pure DynamoDB + S3."""
    for action in ("PublishAsset", "EditAsset", "DiscardChanges", "RenameAsset"):
        assert is_control_plane(action), f"{action} should be control-plane"
        assert runtime_only_reason(action) is None


def test_resolve_agent_config_is_runtime_only_even_though_it_reads() -> None:
    """The other direction: a read that needs live gateway truth."""
    assert not is_control_plane("ResolveAgentConfig")
    assert "gateway" in runtime_only_reason("ResolveAgentConfig").lower()


def test_tool_collection_writes_are_runtime_only() -> None:
    """The subtle pair, and the reason this gate is dependency-based.

    Their handlers never mention the gateway. `ToolCollectionCRUD.create/update`
    reaches it through `_validate_tool_refs`, so a handler-level reading of
    `service.py` classifies them wrongly — and the failure would not be a crash:
    control-plane gateway enumeration returns nothing for MCP-server targets, so
    validation would reject legitimate federated tool refs.
    """
    for action in ("CreateToolCollectionDraft", "UpdateToolCollectionDraft"):
        assert not is_control_plane(action), (
            f"{action} delegates to _validate_tool_refs, which browses the gateway"
        )
        reason = runtime_only_reason(action)
        assert "validat" in reason.lower()

    # ...while the reads and the delete on the same category are fine.
    for action in (
        "ReadToolCollectionDraft",
        "DeleteToolCollectionDraft",
        "ListToolCollectionDrafts",
    ):
        assert is_control_plane(action), f"{action} touches no gateway"


def test_unknown_actions_are_refused_rather_than_served() -> None:
    """Fail closed: an unrecognised action must not be treated as control-plane."""
    assert not is_control_plane("NoSuchAction")
    assert not is_control_plane("")


def test_the_two_gateway_free_lists_agree() -> None:
    """One question, one answer.

    ``main.LAB_GATEWAY_FREE_ACTIONS`` decides whether the runtime may skip its
    ~8.3 s MCP handshake; ``CONTROL_PLANE_ACTIONS`` decides which host may serve
    an action. Both are really asking "does this need a live gateway?", and when
    this test was written they had drifted in *both* directions: the two
    tool-collection writes were on the runtime's skip path despite validating refs
    against the gateway, and `ReadPublishedSkill` / `ResolvePublishedAgentRef` were
    missing from it despite touching nothing — so the skill drill-in paid a
    handshake for no reason.
    """
    runtime_side = _lab_gateway_free_actions()
    assert runtime_side == GATEWAY_FREE_ACTIONS, (
        "LAB_GATEWAY_FREE_ACTIONS and CONTROL_PLANE_ACTIONS | RUNTIME_OWNED_READS "
        "disagree about which actions need a live gateway.\n"
        f"  only in main.py:      {sorted(runtime_side - GATEWAY_FREE_ACTIONS)}\n"
        f"  only in lab_actions:  {sorted(GATEWAY_FREE_ACTIONS - runtime_side)}"
    )


def test_runtime_owned_reads_are_refused_for_product_reasons_not_dependency() -> None:
    """Their reason must not claim a dependency they do not have.

    `list_runs` / `read_run_record` touch no gateway and no model. Refusing them
    is a scope decision; saying otherwise would send the next reader looking for a
    dependency that isn't there.
    """
    for action in RUNTIME_OWNED_READS:
        assert not is_control_plane(action)
        reason = runtime_only_reason(action).lower()
        assert "gateway" not in reason and "model" not in reason, (
            f"{action}'s reason implies a dependency it does not have: {reason!r}"
        )


def test_the_split_is_the_size_the_design_settled_on() -> None:
    """A tripwire, not a spec.

    Totality is already covered above; this catches an action being *moved*
    between the two sets without that being the point of the change.
    """
    actions = _dispatch_actions()
    assert len(actions) == 46, (
        f"dispatch table is now {len(actions)} actions — reclassify, then update "
        "this count deliberately"
    )
    assert len(CONTROL_PLANE_ACTIONS) == 35
    assert len(RUNTIME_ONLY_ACTIONS) == 11
