"""The Lab CRUD bypass must not pay for gateway/agent initialization.

A `/lab` action such as ``ListSpecialtyAgentDrafts`` is a pure DynamoDB read,
but `main.py`'s entrypoint ran ``_init_agent_subsystems()`` and
``_ensure_gateway_connected()`` *before* reaching the bypass. So a request that
touches zero tools still constructed the Bedrock model, loaded the gateway
registry, and completed an MCP handshake to every gateway. Measured against
sandbox: ~8.5s for that path versus ~190ms once the container was warm.

Not every bypass action can skip it. ``BrowseGatewayTools`` and friends read the
module-global ``mcp_manager`` through ``_RegistryGatewayClient``, so they need
connected gateways; ``ChatMessage`` and ``RunHarness`` run the LLM. The split
below is the contract: pure-CRUD actions skip gateway init, gateway-dependent
ones do not.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main_lab_tools


def test_gateway_free_actions_are_declared():
    """`main` exposes the set of bypass actions that need no gateway."""
    import main

    assert hasattr(main, "LAB_GATEWAY_FREE_ACTIONS"), (
        "main must declare which bypass actions can skip gateway init"
    )


def test_list_actions_do_not_require_a_gateway():
    """The Capabilities panel's list calls are the hot path this fixes."""
    import main

    for action in (
        "ListSpecialtyAgentDrafts",
        "ListSkillDrafts",
        "ListToolCollectionDrafts",
        "ListWorkflowDrafts",
        "ListMyPublishedAssets",
    ):
        assert action in main.LAB_GATEWAY_FREE_ACTIONS, (
            f"{action} is a pure DynamoDB read and must skip gateway init"
        )


def test_gateway_dependent_actions_are_excluded():
    """Actions that read `mcp_manager` must still trigger gateway init.

    Listing one of these as gateway-free would make it return an empty or stale
    tool set instead of being slow — a correctness bug, not a performance one.
    """
    import main

    for action in (
        "BrowseGatewayTools",
        "RefreshGatewayTools",
        "TestGatewayTool",
        "ChatMessage",
        "RunHarness",
    ):
        assert action not in main.LAB_GATEWAY_FREE_ACTIONS, (
            f"{action} depends on connected gateways and must not skip init"
        )


def test_gateway_free_actions_are_all_real_bypass_actions():
    """Guards against a typo silently disabling the fast path for an action."""
    import main

    unknown = main.LAB_GATEWAY_FREE_ACTIONS - main_lab_tools.LAB_BYPASS_ACTIONS
    assert not unknown, f"not real bypass actions (typo?): {sorted(unknown)}"


def test_entrypoint_defers_gateway_connect_for_gateway_free_actions():
    """The entrypoint must consult the set, not connect unconditionally.

    Source-level because the ordering is the behavior: reaching
    ``_ensure_gateway_connected`` at all is the cost being removed, so any test
    that lets the entrypoint run has already paid it.
    """
    import inspect

    import main

    src = inspect.getsource(main.invoke)
    connect_at = src.index("_ensure_gateway_connected")
    gate_at = src.index("LAB_GATEWAY_FREE_ACTIONS")
    assert gate_at < connect_at, (
        "the gateway-free check must precede _ensure_gateway_connected, "
        "otherwise the fast path still pays for the MCP handshake"
    )
