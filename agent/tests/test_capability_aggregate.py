"""Source fan-out, merge, and partial-error behaviour (migration Tasks 22-23).

The properties under test:

* a failing source degrades the request, it does not abort it (R12),
* nothing falls back to the AgentCore Runtime (Property 5),
* the same identity from two sources merges to one row, preferring canonical
  state while keeping the Registry record id (R2),
* only the requested categories are loaded — no hidden preloads (R10/R13),
* Gateway tools keep target-qualified identity even when bare names repeat.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from capability import sources as sources_module
from capability.aggregate import ActorContext, bootstrap, load_category
from capability.config import CapabilityConfig
from capability.dto import CapabilitySummary, merge_summaries
from capability.sources import (
    SourceUnavailable,
    actor_may_see_target,
    gateway_id_from_url,
    is_user_target,
    iter_projectable_gateway_targets,
    summaries_from_gateway,
)


def _config(**overrides: Any) -> CapabilityConfig:
    base = {
        "deploy_env": "sandbox",
        "region": "us-west-2",
        "asset_index_table": "assets",
        "gateway_registry_table": "gateways",
        "registry_id": "reg-1",
        "registry_name": "mediaworks-harness-capabilities-sandbox",
        "modes": {},
        "source_timeout_seconds": 0.5,
    }
    base.update(overrides)
    return CapabilityConfig(**base)


def _actor() -> ActorContext:
    return ActorContext(actor_id="u-jane", group_ids=["g-1"], org_id="o-acme")


def _summary(**overrides: Any) -> CapabilitySummary:
    base = {
        "id": "skill:asset-1:v1",
        "category": "skill",
        "capability_class": "asset",
        "display_name": "Skill One",
        "scope": "org",
        "status": "published",
        "source": "asset-index",
        "asset_id": "asset-1",
        "version": 1,
    }
    base.update(overrides)
    return CapabilitySummary(**base)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_canonical_state_wins_but_keeps_the_registry_record_id():
    """Both halves matter to an owner.

    The canonical row carries lifecycle truth; the Registry twin is what proves
    the publish became discoverable. Dropping either leaves the owner unable to
    tell "published" from "published AND discoverable".
    """
    merged = merge_summaries(
        [
            _summary(),
            _summary(
                source="agent-registry",
                scope="harness-shared",
                registry_record_id="rec-9",
                projection_status="in_sync",
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].source == "asset-index"
    assert merged[0].scope == "org"
    assert merged[0].registry_record_id == "rec-9"
    assert merged[0].projection_status == "in_sync"


def test_different_versions_of_one_lineage_are_distinct_rows():
    merged = merge_summaries(
        [_summary(version=1, id="skill:asset-1:v1"), _summary(version=2, id="skill:asset-1:v2")]
    )
    assert len(merged) == 2


def test_a_working_copy_and_its_version_are_distinct_rows():
    merged = merge_summaries(
        [
            _summary(version=None, status="draft", id="skill:asset-1"),
            _summary(version=1),
        ]
    )
    assert len(merged) == 2


def test_sort_is_deterministic():
    """Shadow parity compares ordered membership; unstable order would noise it."""
    items = [
        _summary(id="a", asset_id="a", display_name="Zebra", updated_at="2026-08-01T00:00:00Z"),
        _summary(id="b", asset_id="b", display_name="Apple", updated_at="2026-08-02T00:00:00Z"),
        _summary(id="c", asset_id="c", display_name="Mango", updated_at=None),
    ]
    first = [s.id for s in merge_summaries(items)]
    second = [s.id for s in merge_summaries(list(reversed(items)))]
    assert first == second
    # Newest-updated first; rows with no timestamp sort last.
    assert first == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# Partial failures (R12 / Property 5)
# ---------------------------------------------------------------------------


def test_a_failing_registry_source_yields_a_partial_error_not_an_exception(monkeypatch):
    async def ok_assets(*_a, **_k):
        return (_summary(),), None

    async def boom_registry(*_a, **_k):
        raise SourceUnavailable("unavailable", "agent-registry")

    async def no_builtins(*_a, **_k):
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", ok_assets)
    monkeypatch.setattr("capability.aggregate.read_registry", boom_registry)
    # Neutralised so this stays a test about SOURCE FAILURE, not about the catalog:
    # `read_builtins("skill")` reads a DynamoDB catalog row, so without a gateway table
    # it contributes a `config` partial error of its own and crowds the assertion below.
    monkeypatch.setattr("capability.aggregate.read_builtins", no_builtins)

    page = _run(
        load_category(
            "skill",
            config=_config(modes={"skill": "registry"}),
            actor=_actor(),
            asset_index=object(),
        )
    )
    assert [item.id for item in page.items] == ["skill:asset-1:v1"]
    assert [(e.source, e.error) for e in page.partial_errors] == [
        ("agent-registry", "unavailable")
    ]
    assert "asset-index" in page.sources_queried


def test_a_slow_source_times_out_into_a_partial_error(monkeypatch):
    async def slow(*_a, **_k):
        await asyncio.sleep(5)
        return ()

    async def ok_assets(*_a, **_k):
        return (_summary(),), None

    monkeypatch.setattr("capability.aggregate.read_asset_index", ok_assets)
    monkeypatch.setattr("capability.aggregate.read_registry", slow)

    # Neutralised so this stays a test about the source under examination. The builtin
    # skill source reads a DynamoDB catalog row, so without a gateway table it reports a
    # `config` partial error of its own and crowds the assertion below.
    async def _no_builtins(*_a, **_k):
        return ()

    monkeypatch.setattr("capability.aggregate.read_builtins", _no_builtins)

    page = _run(
        load_category(
            "skill",
            config=_config(modes={"skill": "registry"}, source_timeout_seconds=0.05),
            actor=_actor(),
            asset_index=object(),
        )
    )
    assert page.items
    assert [e.error for e in page.partial_errors] == ["timeout"]


def test_an_unexpected_exception_is_classified_not_leaked(monkeypatch):
    """A raw exception string can carry private paths or descriptor bodies."""

    async def boom(*_a, **_k):
        raise KeyError("s3://bucket/users/u-someone-else/drafts/secret")

    monkeypatch.setattr("capability.aggregate.read_asset_index", boom)

    # Neutralised so this stays a test about the source under examination. The builtin
    # skill source reads a DynamoDB catalog row, so without a gateway table it reports a
    # `config` partial error of its own and crowds the assertion below.
    async def _no_builtins(*_a, **_k):
        return ()

    monkeypatch.setattr("capability.aggregate.read_builtins", _no_builtins)

    page = _run(
        load_category("skill", config=_config(), actor=_actor(), asset_index=object())
    )
    assert [e.error for e in page.partial_errors] == ["error"]
    serialized = repr(page.to_json())
    assert "u-someone-else" not in serialized


def test_every_source_failing_returns_an_empty_page_with_errors(monkeypatch):
    """Not an exception, and above all not a Runtime call (Property 5)."""

    async def boom(*_a, **_k):
        raise SourceUnavailable("unavailable")

    monkeypatch.setattr("capability.aggregate.read_asset_index", boom)
    monkeypatch.setattr("capability.aggregate.read_builtins", boom)

    page = _run(
        load_category("skill", config=_config(), actor=_actor(), asset_index=object())
    )
    assert page.items == ()
    assert len(page.partial_errors) == 2
    assert page.sources_queried == ()


# ---------------------------------------------------------------------------
# Source planning (R10 / R13)
# ---------------------------------------------------------------------------


def test_registry_is_not_queried_in_legacy_mode(monkeypatch):
    called: list[str] = []

    async def ok_assets(*_a, **_k):
        return (), None

    async def registry(*_a, **_k):
        called.append("registry")
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", ok_assets)
    monkeypatch.setattr("capability.aggregate.read_registry", registry)

    _run(load_category("skill", config=_config(), actor=_actor(), asset_index=object()))
    assert called == []


def test_registry_is_not_queried_in_canonical_mode(monkeypatch):
    """`canonical` is served by this API but from canonical state alone.

    This is the load-bearing property of the mode. A registry IS configured here
    — so this proves the gate is the mode, not the absence of a registry id. If
    it regressed, flipping a category for latency would silently start serving
    projected rows, and a Registry blip would raise a "partial results" banner
    over an agent list that is in fact complete.
    """
    called: list[str] = []

    async def assets(*_a, **_k):
        called.append("asset-index")
        return (), None

    async def registry(*_a, **_k):
        called.append("agent-registry")
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", assets)
    monkeypatch.setattr("capability.aggregate.read_registry", registry)

    page = _run(
        load_category(
            "specialty-agent",
            config=_config(modes={"specialty-agent": "canonical"}),
            actor=_actor(),
            asset_index=object(),
        )
    )
    assert "agent-registry" not in called
    assert called == ["asset-index"]
    # And it must not be reported as queried, or a reader would think it was.
    assert "agent-registry" not in page.sources_queried
    assert page.partial_errors == ()


def test_canonical_and_registry_differ_only_by_the_registry_source(monkeypatch):
    """The two Capability-API modes are otherwise the same read."""
    seen: dict[str, list[str]] = {}

    async def assets(*_a, **_k):
        seen.setdefault(mode, []).append("asset-index")
        return (), None

    async def registry(*_a, **_k):
        seen.setdefault(mode, []).append("agent-registry")
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", assets)
    monkeypatch.setattr("capability.aggregate.read_registry", registry)

    for mode in ("canonical", "registry"):
        _run(
            load_category(
                "specialty-agent",
                config=_config(modes={"specialty-agent": mode}),
                actor=_actor(),
                asset_index=object(),
            )
        )

    assert sorted(seen["canonical"]) == ["asset-index"]
    assert sorted(seen["registry"]) == ["agent-registry", "asset-index"]


def test_tool_category_queries_the_gateway_and_not_the_asset_index(monkeypatch):
    called: list[str] = []

    async def assets(*_a, **_k):
        called.append("asset-index")
        return (), None

    async def gateway(*_a, **_k):
        called.append("gateway")
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", assets)
    monkeypatch.setattr("capability.aggregate.read_gateway_catalog", gateway)

    _run(
        load_category(
            "tool", config=_config(), actor=_actor(), asset_index=object(), gateway_table=object()
        )
    )
    assert called == ["gateway"]


def test_hook_category_never_queries_the_registry(monkeypatch):
    """Actor-private hooks are management-only and never harness-shared (R3)."""
    called: list[str] = []

    async def assets(*_a, **_k):
        return (), None

    async def registry(*_a, **_k):
        called.append("registry")
        return ()

    monkeypatch.setattr("capability.aggregate.read_asset_index", assets)
    monkeypatch.setattr("capability.aggregate.read_registry", registry)

    _run(
        load_category(
            "hook",
            config=_config(modes={"hook": "registry"}),
            actor=_actor(),
            asset_index=object(),
        )
    )
    assert called == []


def test_bootstrap_loads_only_the_requested_categories(monkeypatch):
    """A tab selection must not preload the other four (R10/R13)."""
    seen: list[str] = []

    async def assets(category, **_k):
        seen.append(category)
        return (), None

    monkeypatch.setattr("capability.aggregate.read_asset_index", assets)

    pages = _run(
        bootstrap(["skill"], config=_config(), actor=_actor(), asset_index=object())
    )
    assert seen == ["skill"]
    assert set(pages) == {"skill"}


def test_bootstrap_with_no_categories_does_nothing():
    assert _run(bootstrap([], config=_config(), actor=_actor())) == {}


# ---------------------------------------------------------------------------
# Actor context is trusted, not asserted (R5)
# ---------------------------------------------------------------------------


def test_actor_context_requires_an_actor_id():
    with pytest.raises(ValueError):
        ActorContext(actor_id="")


def test_actor_context_adapts_to_the_lab_request_context():
    ctx = _actor().as_lab_context()
    assert ctx.actor_id == "u-jane"
    assert ctx.group_ids == ["g-1"]
    # The org tier reads this via getattr; without it the tier is skipped.
    assert getattr(ctx, "org_id", None) == "o-acme"


# ---------------------------------------------------------------------------
# Gateway hierarchy (R4 / R10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://gw-abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
            "gw-abc",
        ),
        # An AgentCore Runtime invocations URL is not a gateway.
        (
            "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn/invocations",
            None,
        ),
        ("https://example.com/mcp", None),
        ("", None),
        (None, None),
    ],
)
def test_gateway_id_from_url(url, expected):
    assert gateway_id_from_url(url) == expected


def test_repeated_bare_tool_names_across_targets_stay_distinct():
    summaries = summaries_from_gateway(
        gateway_id="gw-1",
        gateway_name="M2C",
        targets=[
            {"targetId": "t1", "targetName": "media-processing", "tools": [{"name": "get_job_status"}]},
            {"targetId": "t2", "targetName": "speech-diarization", "tools": [{"name": "get_job_status"}]},
        ],
        semantic_search=False,
        actor_id="u-jane",
        owners={},
    )
    assert len(summaries) == 2
    assert {s.id for s in summaries} == {
        "tool:gw-1:t1:get_job_status",
        "tool:gw-1:t2:get_job_status",
    }
    assert {s.qualified_tool_name for s in summaries} == {
        "media-processing___get_job_status",
        "speech-diarization___get_job_status",
    }
    # Merging must not collapse them either.
    assert len(merge_summaries(summaries)) == 2


def test_prohibited_tools_are_filtered_from_the_catalog_not_fatal():
    """A read surface degrades; only a Registry *write* raises.

    Failing the whole Tools tab because one internal tool showed up would be a
    worse outcome than omitting it.
    """
    summaries = summaries_from_gateway(
        gateway_id="gw-1",
        gateway_name="M2C",
        targets=[
            {
                "targetId": "t1",
                "targetName": "media-processing",
                "tools": [{"name": "shell"}, {"name": "process_media"}],
            }
        ],
        semantic_search=False,
        actor_id="u-jane",
        owners={},
    )
    assert [s.bare_tool_name for s in summaries] == ["process_media"]


def test_semantic_search_tool_is_surfaced_when_the_gateway_synthesizes_it():
    """Not a target, so it is absent from the target loop.

    Omitting it would under-report the gateway by exactly the tool that makes
    narrowing possible.
    """
    summaries = summaries_from_gateway(
        gateway_id="gw-1",
        gateway_name="M2C",
        targets=[],
        semantic_search=True,
        actor_id="u-jane",
        owners={},
    )
    assert [s.bare_tool_name for s in summaries] == [sources_module.SEARCH_TOOL_NAME]


def test_user_targets_are_hidden_from_other_actors_and_fail_closed():
    """The Gateway control plane is not interceptor-filtered.

    Without this filter every user's registered MCP tools would be listed to
    everyone even though calling them is denied.
    """
    owners = {"usr-jane": "u-jane"}
    assert actor_may_see_target("u-jane", "usr-jane", owners) is True
    assert actor_may_see_target("u-bob", "usr-jane", owners) is False
    # No ownership record, or no actor → hidden.
    assert actor_may_see_target("u-bob", "usr-unknown", owners) is False
    assert actor_may_see_target("", "usr-jane", owners) is False
    # Platform targets are visible to everyone.
    assert actor_may_see_target("", "media-processing", owners) is True


def test_user_target_prefix_matches_the_agent_and_interceptor_convention():
    """Hyphen, not underscore — the service's target-name pattern rejects `_`.

    A mismatch here makes every user tool look like platform surface, which
    fails OPEN.
    """
    assert sources_module.USER_TARGET_PREFIX == "usr-"
    assert is_user_target("usr-jane") is True
    assert is_user_target("usr_jane") is False
    assert is_user_target("media-processing") is False


def test_user_private_targets_are_excluded_from_registry_projection():
    """Visible to their owner in the Tools tab; never in the shared record (R3)."""
    targets = [
        {"targetId": "t1", "targetName": "media-processing", "tools": []},
        {"targetId": "t2", "targetName": "usr-jane", "tools": []},
    ]
    kept = iter_projectable_gateway_targets(targets)
    assert [t["targetName"] for t in kept] == ["media-processing"]


def test_gateway_summaries_never_carry_an_asset_id():
    """A Gateway tool is not an authored asset and has no lineage."""
    summaries = summaries_from_gateway(
        gateway_id="gw-1",
        gateway_name="M2C",
        targets=[{"targetId": "t1", "targetName": "mp", "tools": [{"name": "process_media"}]}],
        semantic_search=False,
        actor_id="u-jane",
        owners={},
    )
    assert summaries[0].asset_id is None
    assert summaries[0].capability_class == "gateway-mcp"
    assert summaries[0].schema_digest.startswith("sha256:")
