"""Tests for the write-free purge reference scan.

The scan's job is not to be exhaustive — it cannot be, since it sees only what
the actor sees — but to be *honest*. So the tests split evenly between "does it
find the reference" and "does it admit what it could not read", because a scan
that silently returns `()` on failure would present as "nothing will break"
right before an irreversible delete.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import lab.service as lab_service  # noqa: E402
from lab.context import LabRequestContext  # noqa: E402
from lab.operation_results import VisibleAssetsPage  # noqa: E402
from lab.purge_scan import (  # noqa: E402
    scan_agent_references,
    scan_references,
    scan_skill_references,
    scan_tool_collection_references,
)

_TARGET = "p-agent-1"


def _run(coro):
    return asyncio.run(coro)


def _ctx(actor_id: str = "u-jane") -> LabRequestContext:
    return LabRequestContext(
        actor_id=actor_id, session_id="sess-test", request_id="req-test"
    )


def _workflow_row(asset_id: str, *, name: str = "Ingest", status: str = "draft"):
    row = MagicMock()
    row.asset_id = asset_id
    row.name = name
    row.status = status
    row.s3_prefix = f"users/u-jane/drafts/workflows/{asset_id}/"
    return row


def _doc(*steps: dict) -> bytes:
    return json.dumps({"name": "wf", "steps": list(steps)}).encode("utf-8")


def _step(step_id: str, *, kind: str = "specialty", asset_id: Optional[str] = _TARGET):
    agent: dict = {"kind": kind}
    if asset_id is not None:
        agent["assetId"] = asset_id
    return {"stepId": step_id, "agent": agent, "prompt": "go"}


def _patch_listing(monkeypatch, rows, next_cursor=None, error=None):
    """Replace list_visible_assets on the module the scan imports lazily."""
    calls: list[dict] = []

    async def _fake(ctx, category, mode, **kwargs):
        calls.append({"category": category, "mode": mode, **kwargs})
        if error is not None:
            raise error
        return VisibleAssetsPage(items=list(rows), next_cursor=next_cursor)

    monkeypatch.setattr(lab_service, "list_visible_assets", _fake)
    return calls


def _s3_returning(mapping: dict[str, bytes]):
    s3 = MagicMock()

    async def _read(key):
        if key not in mapping:
            raise FileNotFoundError(key)
        value = mapping[key]
        if isinstance(value, Exception):
            raise value
        return value

    s3.read_absolute = AsyncMock(side_effect=_read)
    return s3


# ---------------------------------------------------------------------------
# Finding references
# ---------------------------------------------------------------------------


def test_finds_a_workflow_step_that_names_the_agent(monkeypatch):
    row = _workflow_row("wf-1", name="Nightly ingest", status="published")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({f"{row.s3_prefix}workflow.json": _doc(_step("transcribe"))})

    hits, partial = _run(
        scan_agent_references(
            ctx=_ctx(), asset_index=MagicMock(), s3=s3,
            published_id=_TARGET, org_id="o-acme",
        )
    )

    assert partial == ()
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.kind, hit.asset_id, hit.name, hit.status) == (
        "workflow-step", "wf-1", "Nightly ingest", "published",
    )
    assert "transcribe" in hit.detail


def test_reports_every_referencing_step_separately(monkeypatch):
    """Two steps in one workflow are two things that break, not one."""
    row = _workflow_row("wf-1")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({
        f"{row.s3_prefix}workflow.json": _doc(
            _step("a"), _step("b", kind="core", asset_id=None), _step("c"),
        )
    })

    hits, _ = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))

    assert [h.detail for h in hits] == ["step 'a'", "step 'c'"]


def test_ignores_steps_pointing_at_other_agents_and_the_core_agent(monkeypatch):
    row = _workflow_row("wf-1")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({
        f"{row.s3_prefix}workflow.json": _doc(
            _step("a", asset_id="p-other"),
            _step("b", kind="core", asset_id=None),
            {"stepId": "c", "prompt": "no agent key at all"},
        )
    })

    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))
    assert hits == () and partial == ()


def test_scans_every_visible_workflow(monkeypatch):
    rows = [_workflow_row(f"wf-{i}") for i in range(12)]
    _patch_listing(monkeypatch, rows)
    s3 = _s3_returning({
        f"{r.s3_prefix}workflow.json": _doc(_step("s")) for r in rows
    })

    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))
    # More rows than the concurrency bound, so this also proves the semaphore
    # gates rather than drops.
    assert len(hits) == 12 and partial == ()
    assert s3.read_absolute.await_count == 12


def test_includes_deprecated_workflows(monkeypatch):
    """A deprecated workflow still references the agent and can be revived."""
    calls = _patch_listing(monkeypatch, [])
    _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=_s3_returning({}),
        published_id=_TARGET, org_id="o-acme",
    ))
    assert calls[0]["include_deprecated"] is True
    assert calls[0]["category"] == "workflow"


# ---------------------------------------------------------------------------
# Honesty about what was not scanned
# ---------------------------------------------------------------------------


def test_missing_org_id_is_reported_not_ignored(monkeypatch):
    """The context carries no org_id, so this is the common case.

    Without this the dialog would claim "no references" while an org-shared
    workflow silently breaks.
    """
    _patch_listing(monkeypatch, [])
    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=_s3_returning({}),
        published_id=_TARGET,
    ))
    assert hits == ()
    assert any("org" in p for p in partial)


def test_org_id_from_the_context_suppresses_the_warning(monkeypatch):
    ctx = MagicMock(wraps=_ctx())
    ctx.actor_id, ctx.group_ids, ctx.org_id = "u-jane", [], "o-acme"
    calls = _patch_listing(monkeypatch, [])
    _, partial = _run(scan_agent_references(
        ctx=ctx, asset_index=MagicMock(), s3=_s3_returning({}), published_id=_TARGET,
    ))
    assert partial == ()
    assert calls[0]["org_id"] == "o-acme"


def test_listing_failure_is_reported_not_raised(monkeypatch):
    _patch_listing(monkeypatch, [], error=RuntimeError("ddb down"))
    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=_s3_returning({}),
        published_id=_TARGET, org_id="o-acme",
    ))
    assert hits == ()
    assert any("listing failed" in p for p in partial)


def test_truncation_is_reported(monkeypatch):
    _patch_listing(monkeypatch, [], next_cursor="opaque")
    _, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=_s3_returning({}),
        published_id=_TARGET, org_id="o-acme", limit=5,
    ))
    assert any("beyond the first 5" in p for p in partial)


def test_unreadable_workflow_json_is_reported_and_does_not_stop_the_scan(monkeypatch):
    """One bad read must not hide the reference in the next workflow."""
    bad, good = _workflow_row("wf-bad", name="Broken"), _workflow_row("wf-good")
    _patch_listing(monkeypatch, [bad, good])
    s3 = _s3_returning({f"{good.s3_prefix}workflow.json": _doc(_step("s"))})

    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))

    assert [h.asset_id for h in hits] == ["wf-good"]
    assert len(partial) == 1
    assert "wf-bad" in partial[0] and "unreadable" in partial[0]


def test_malformed_json_is_reported(monkeypatch):
    row = _workflow_row("wf-1")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({f"{row.s3_prefix}workflow.json": b"{not json"})
    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))
    assert hits == () and len(partial) == 1


@pytest.mark.parametrize("steps_value", ['"nope"', "42", "{}"])
def test_non_list_steps_is_reported(monkeypatch, steps_value):
    row = _workflow_row("wf-1")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({
        f"{row.s3_prefix}workflow.json":
            f'{{"name": "wf", "steps": {steps_value}}}'.encode(),
    })
    hits, partial = _run(scan_agent_references(
        ctx=_ctx(), asset_index=MagicMock(), s3=s3,
        published_id=_TARGET, org_id="o-acme",
    ))
    assert hits == () and len(partial) == 1
    assert "malformed" in partial[0]


# ---------------------------------------------------------------------------
# Write-free
# ---------------------------------------------------------------------------


def test_the_scan_writes_nothing(monkeypatch):
    """Preflight runs on a live lineage the user has not yet confirmed."""
    row = _workflow_row("wf-1")
    _patch_listing(monkeypatch, [row])
    s3 = _s3_returning({f"{row.s3_prefix}workflow.json": _doc(_step("s"))})
    index = MagicMock()

    _run(scan_agent_references(
        ctx=_ctx(), asset_index=index, s3=s3, published_id=_TARGET, org_id="o-acme",
    ))

    for forbidden in (
        "write", "write_absolute", "delete", "delete_absolute",
        "delete_prefix_absolute",
    ):
        assert not getattr(s3, forbidden).called, forbidden
    for forbidden in ("put", "update", "transact_write"):
        assert not getattr(index, forbidden).called, forbidden


# ---------------------------------------------------------------------------
# Skill references — an agent's compatible-skill list
# ---------------------------------------------------------------------------
#
# Cheaper than the workflow scan and with one fewer failure mode:
# ``compatible_skills`` is serialized onto the AssetIndex row, so there is no
# per-row S3 read and therefore no unreadable-document case. What remains is the
# same obligation — say what could not be checked rather than reporting "clean".

_SKILL = "p-skill-1"


def _agent_row(
    asset_id: str,
    *,
    skills,
    name: str = "Research Agent",
    status: str = "draft",
    version: Optional[int] = None,
):
    row = MagicMock()
    row.asset_id = asset_id
    row.name = name
    row.status = status
    row.version = version
    # Set explicitly: a bare MagicMock attribute is a MagicMock, which the scan
    # would (correctly) report as a malformed list.
    row.compatible_skills = skills
    return row


def _scan_skill(index=None, **kwargs):
    return _run(
        scan_skill_references(
            ctx=_ctx(),
            asset_index=index if index is not None else MagicMock(),
            published_id=_SKILL,
            **kwargs,
        )
    )


def test_finds_an_agent_whose_compatible_skills_names_the_skill(monkeypatch):
    _patch_listing(monkeypatch, [_agent_row("a-1", skills=[_SKILL])])

    hits, _partial = _scan_skill(org_id="o-acme")

    assert len(hits) == 1
    assert hits[0].kind == "agent-compatible-skill"
    assert hits[0].asset_id == "a-1"
    assert hits[0].name == "Research Agent"
    assert hits[0].detail == "compatible skill"


def test_reports_the_version_so_an_immutable_reference_is_recognisable(monkeypatch):
    """A published agent can never drop the reference; a draft can.

    ``status`` carries that, and the version makes the row identifiable — the
    user is being asked to accept that a specific shipped version will break.
    """
    _patch_listing(
        monkeypatch,
        [_agent_row("a-1", skills=[_SKILL], status="published", version=3)],
    )

    hits, _partial = _scan_skill(org_id="o-acme")

    assert hits[0].status == "published"
    assert hits[0].detail == "compatible skill (v3)"


def test_scans_agents_not_workflows(monkeypatch):
    calls = _patch_listing(monkeypatch, [])

    _scan_skill(org_id="o-acme")

    assert [c["category"] for c in calls] == ["specialty-agent"]


def test_includes_deprecated_agents(monkeypatch):
    """A deprecated agent still names the skill, and may be un-deprecated."""
    calls = _patch_listing(
        monkeypatch, [_agent_row("a-1", skills=[_SKILL], status="deprecated")]
    )

    hits, _partial = _scan_skill(org_id="o-acme")

    assert calls[0]["include_deprecated"] is True
    assert len(hits) == 1
    assert hits[0].status == "deprecated"


def test_an_agent_naming_other_skills_is_not_a_hit(monkeypatch):
    _patch_listing(
        monkeypatch,
        [
            _agent_row("a-1", skills=["p-skill-other"]),
            _agent_row("a-2", skills=[]),
        ],
    )

    hits, partial = _scan_skill(org_id="o-acme")

    assert hits == ()
    assert partial == ()


def test_skill_scan_missing_org_id_is_reported_not_ignored(monkeypatch):
    _patch_listing(monkeypatch, [_agent_row("a-1", skills=[])])

    hits, partial = _scan_skill()

    assert hits == ()
    assert any("org" in p for p in partial), partial


def test_skill_scan_listing_failure_is_reported_not_raised(monkeypatch):
    _patch_listing(monkeypatch, [], error=RuntimeError("ddb down"))

    hits, partial = _scan_skill(org_id="o-acme")

    assert hits == ()
    assert any("listing failed" in p and "RuntimeError" in p for p in partial), partial


def test_skill_scan_truncation_is_reported(monkeypatch):
    _patch_listing(monkeypatch, [_agent_row("a-1", skills=[])], next_cursor="more")

    _hits, partial = _scan_skill(org_id="o-acme", limit=1)

    assert any("beyond the first 1" in p for p in partial), partial


@pytest.mark.parametrize("bad", ["p-skill-1", {"p-skill-1": True}, 7])
def test_malformed_compatible_skills_is_reported_not_read_as_no_reference(
    monkeypatch, bad
):
    """Absent is not empty.

    A string containing the id would even pass a naive ``in`` check. Reporting
    the row as unreadable is the only honest answer, since the scan cannot tell
    whether this agent references the skill.
    """
    _patch_listing(monkeypatch, [_agent_row("a-1", skills=bad)])

    hits, partial = _scan_skill(org_id="o-acme")

    assert hits == ()
    assert any("malformed compatible-skill list" in p for p in partial), partial


def test_row_with_no_compatible_skill_list_at_all_is_reported(monkeypatch):
    row = MagicMock(spec=["asset_id", "name", "status"])
    row.asset_id, row.name, row.status = "a-1", "Odd", "draft"
    _patch_listing(monkeypatch, [row])

    hits, partial = _scan_skill(org_id="o-acme")

    assert hits == ()
    assert any("no compatible-skill list" in p for p in partial), partial


def test_the_skill_scan_writes_nothing(monkeypatch):
    _patch_listing(monkeypatch, [_agent_row("a-1", skills=[_SKILL])])
    index = MagicMock()

    _scan_skill(index=index, org_id="o-acme")

    for forbidden in ("put", "update", "transact_write", "delete"):
        assert not getattr(index, forbidden).called, forbidden


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
#
# The regression that matters most. The route runs whichever scan it is handed
# and presents the result as the blast radius, so pointing the workflow scan at a
# skill would render "nothing references this skill" over agents that depend on
# it — a confident falsehood immediately before an irreversible delete.


def test_dispatch_runs_the_agent_scan_for_specialty_agents(monkeypatch):
    calls = _patch_listing(monkeypatch, [])

    _run(scan_references(
        category="specialty-agent", ctx=_ctx(), asset_index=MagicMock(),
        s3=_s3_returning({}), published_id=_TARGET, org_id="o-acme",
    ))

    assert [c["category"] for c in calls] == ["workflow"]


def test_dispatch_runs_the_skill_scan_for_skills(monkeypatch):
    calls = _patch_listing(monkeypatch, [_agent_row("a-1", skills=[_SKILL])])

    hits, _partial = _run(scan_references(
        category="skill", ctx=_ctx(), asset_index=MagicMock(),
        s3=_s3_returning({}), published_id=_SKILL, org_id="o-acme",
    ))

    assert [c["category"] for c in calls] == ["specialty-agent"]
    assert len(hits) == 1


@pytest.mark.parametrize(
    "category", ["workflow", "tool-collection", "hook", "nonsense"]
)
def test_dispatch_refuses_a_category_with_no_scan(category):
    """Raising is the point: ``()`` reads as "nothing references this".

    ``tool-collection`` belongs here even though
    :func:`scan_tool_collection_references` exists: this dispatcher serves
    *purge*, and purging a flat collection is still unimplemented. The scan gates
    the ordinary delete instead, so wiring it in here would advertise a purge
    nothing performs.
    """
    with pytest.raises(ValueError, match="no reference scan"):
        _run(scan_references(
            category=category, ctx=_ctx(), asset_index=MagicMock(),
            s3=_s3_returning({}), published_id=_TARGET,
        ))


# ---------------------------------------------------------------------------
# Tool-collection references — an agent's tool_collections list
# ---------------------------------------------------------------------------
#
# Same shape and cost as the skill scan (``toolCollections`` is on the index
# row), but the stakes differ: this one gates a *delete* rather than describing a
# purge, so "no hits" is what lets the row be destroyed. The honesty tests below
# are therefore load-bearing rather than courteous.

_COLLECTION = "tc-media-1"


def _agent_with_collections(
    asset_id: str,
    *,
    collections,
    name: str = "Media Agent",
    status: str = "draft",
    version: Optional[int] = None,
):
    row = MagicMock()
    row.asset_id = asset_id
    row.name = name
    row.status = status
    row.version = version
    # Explicit for the same reason as `_agent_row`: a bare MagicMock attribute
    # would be reported as a malformed list.
    row.tool_collections = collections
    return row


def _scan_collection(index=None, **kwargs):
    return _run(
        scan_tool_collection_references(
            ctx=_ctx(),
            asset_index=index if index is not None else MagicMock(),
            collection_id=_COLLECTION,
            **kwargs,
        )
    )


def test_finds_an_agent_whose_tool_collections_names_the_collection(monkeypatch):
    _patch_listing(
        monkeypatch, [_agent_with_collections("a-1", collections=[_COLLECTION])]
    )

    hits, partial = _scan_collection(org_id="o-acme")

    assert partial == ()
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.kind, hit.asset_id, hit.name, hit.status) == (
        "agent-tool-collection", "a-1", "Media Agent", "draft",
    )


def test_a_published_agents_reference_carries_its_version(monkeypatch):
    """The distinction the caller acts on: a published reference is frozen.

    A draft's reference can be edited away, so the refusal is advice. A published
    version's cannot, which is why the version travels in ``detail``.
    """
    _patch_listing(
        monkeypatch,
        [
            _agent_with_collections(
                "a-1", collections=[_COLLECTION], status="published", version=2
            )
        ],
    )

    hits, _partial = _scan_collection(org_id="o-acme")

    assert hits[0].status == "published"
    assert "v2" in hits[0].detail


def test_a_working_copy_reference_says_so(monkeypatch):
    _patch_listing(
        monkeypatch, [_agent_with_collections("a-1", collections=[_COLLECTION])]
    )

    hits, _partial = _scan_collection(org_id="o-acme")

    assert "working copy" in hits[0].detail


def test_an_agent_naming_other_collections_is_not_a_hit(monkeypatch):
    _patch_listing(
        monkeypatch,
        [
            _agent_with_collections("a-1", collections=["tc-other"]),
            _agent_with_collections("a-2", collections=[]),
        ],
    )

    hits, partial = _scan_collection(org_id="o-acme")

    assert (hits, partial) == ((), ())


def test_collection_scan_missing_org_id_is_reported_not_ignored(monkeypatch):
    """The usual case, and the reason a delete may not block on ``partial``."""
    _patch_listing(monkeypatch, [_agent_with_collections("a-1", collections=[])])

    hits, partial = _scan_collection()

    assert hits == ()
    assert any("org-shared agents" in p for p in partial), partial


def test_collection_scan_listing_failure_is_reported_not_raised(monkeypatch):
    _patch_listing(monkeypatch, [], error=RuntimeError("ddb down"))

    hits, partial = _scan_collection(org_id="o-acme")

    assert hits == ()
    assert any("listing failed" in p for p in partial), partial


def test_collection_scan_truncation_is_reported(monkeypatch):
    _patch_listing(
        monkeypatch,
        [_agent_with_collections("a-1", collections=[])],
        next_cursor="more",
    )

    hits, partial = _scan_collection(org_id="o-acme")

    assert hits == ()
    assert any("beyond the first" in p for p in partial), partial


def test_malformed_tool_collections_is_reported_not_read_as_no_reference(
    monkeypatch,
):
    """Absent ≠ empty. A string is iterable, so ``in`` would silently mis-answer."""
    _patch_listing(
        monkeypatch, [_agent_with_collections("a-1", collections=_COLLECTION)]
    )

    hits, partial = _scan_collection(org_id="o-acme")

    assert hits == ()
    assert any("malformed tool-collection list" in p for p in partial), partial


def test_row_with_no_tool_collection_list_at_all_is_reported(monkeypatch):
    row = MagicMock()
    row.asset_id = "a-1"
    row.name = "Legacy Agent"
    row.status = "draft"
    row.version = None
    row.tool_collections = None
    _patch_listing(monkeypatch, [row])

    hits, partial = _scan_collection(org_id="o-acme")

    assert hits == ()
    assert any("no tool-collection list" in p for p in partial), partial


def test_the_collection_scan_writes_nothing(monkeypatch):
    _patch_listing(
        monkeypatch, [_agent_with_collections("a-1", collections=[_COLLECTION])]
    )
    index = MagicMock()

    _scan_collection(index=index, org_id="o-acme")

    for forbidden in ("put", "update", "transact_write", "delete"):
        assert not getattr(index, forbidden).called, forbidden
