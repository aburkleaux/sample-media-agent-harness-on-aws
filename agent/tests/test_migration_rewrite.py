"""Unit tests for the workflow-ref rewriter (Task 8.6).

Validates: Requirement 14.3 — every workflow asset (legacy and
migrated) is scanned for ``agent:{kind:'core', ref:'task-agent'}`` (and
its JSON / bare-string equivalents) and rewritten to
``agent:{ref:'specialty-agent/<core_task_agent_id>@v1'}``. The
rewriter is idempotent (a second run over fully-migrated content is
a no-op) and supports a dry-run that produces the summary without
issuing ``put_object`` calls.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# Make ``lab.migration`` importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration.rewrite import RewriteSummary, rewrite_workflow_refs

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

CORE_PID = "core-pid-abc"
NEW_REF = f"specialty-agent/{CORE_PID}@v1"
BUCKET = "test-bucket"


def _make_paginator(prefix_to_keys: dict[str, list[str]]) -> MagicMock:
    """Build a paginator that yields one page per prefix lookup.

    The rewriter calls ``paginate(Bucket=..., Prefix=<root>)`` once per
    scan root in ``_WORKFLOW_SCAN_PREFIXES`` (``skills/``, ``org/``,
    ``users/``). The fake paginator dispatches on the ``Prefix`` kwarg
    so each test can seed only the roots it cares about.
    """
    paginator = MagicMock()

    def _paginate(*, Bucket, Prefix, **_kwargs):
        keys = prefix_to_keys.get(Prefix, [])
        return iter([{"Contents": [{"Key": k} for k in keys]}])

    paginator.paginate.side_effect = _paginate
    return paginator


def _make_s3(
    *,
    prefix_to_keys: dict[str, list[str]],
    objects: dict[str, bytes],
) -> MagicMock:
    """Build a ``MagicMock`` boto3 ``s3`` client.

    Args:
        prefix_to_keys: Mapping from scan-root prefix to the keys
            yielded by ``ListObjectsV2`` for that prefix. The fixture
            wires this through ``get_paginator`` exactly as boto3
            does at runtime.
        objects: Mapping from full key to the bytes ``get_object``
            returns for that key. ``get_object`` raises ``KeyError``
            for unknown keys so a test that mis-spells a key fails
            loudly rather than silently returning ``b""``.
    """
    s3 = MagicMock()
    s3.get_paginator.return_value = _make_paginator(prefix_to_keys)

    def _get_object(*, Bucket, Key):
        body_bytes = objects[Key]
        body = MagicMock()
        body.read.return_value = body_bytes
        return {"Body": body}

    s3.get_object.side_effect = _get_object
    return s3


def _wf(steps: list[dict], name: str = "wf") -> bytes:
    """Render a workflow.json bytes payload."""
    return json.dumps({"name": name, "steps": steps}, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_legacy_dict_ref_is_rewritten():
    """A workflow with ``{"kind":"core","ref":"task-agent"}`` is rewritten
    to the canonical ``specialty-agent/<pid>@v1`` form, ``kind`` is
    dropped, and the put_object body is the re-serialized JSON."""
    legacy_key = "skills/foo/workflow.json"
    legacy_steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]

    s3 = _make_s3(
        prefix_to_keys={"skills/": [legacy_key]},
        objects={legacy_key: _wf(legacy_steps)},
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert isinstance(summary, RewriteSummary)
    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 1
    assert summary.refs_rewritten == 1
    assert summary.skipped_already_modern == 0
    assert summary.modified_keys == (legacy_key,)

    # Exactly one put_object, with the rewritten body.
    s3.put_object.assert_called_once()
    call = s3.put_object.call_args
    assert call.kwargs["Bucket"] == BUCKET
    assert call.kwargs["Key"] == legacy_key
    new_doc = json.loads(call.kwargs["Body"].decode("utf-8"))
    new_agent = new_doc["steps"][0]["agent"]
    assert new_agent == {"ref": NEW_REF}
    assert "kind" not in new_agent


def test_modern_ref_is_skipped_and_counts_toward_already_modern():
    """A workflow whose every agent is already in the modern Asset_Ref
    form is left untouched and counted toward
    ``skipped_already_modern``."""
    modern_key = "org/o-acme/published/workflows/p-1/v1/workflow.json"
    modern_steps = [
        {
            "stepId": "s1",
            "agent": {"ref": "specialty-agent/abc@v1"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]

    s3 = _make_s3(
        prefix_to_keys={"org/": [modern_key]},
        objects={modern_key: _wf(modern_steps)},
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 0
    assert summary.refs_rewritten == 0
    assert summary.skipped_already_modern == 1
    assert summary.modified_keys == ()
    s3.put_object.assert_not_called()


def test_mixed_workflow_rewrites_only_legacy_step():
    """A workflow with one legacy ref and one already-modern ref has
    only the legacy one rewritten; the modern ref is preserved
    verbatim."""
    mixed_key = "users/u-jane/drafts/workflows/d-1/workflow.json"
    mixed_steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p1",
            "inputs": [],
        },
        {
            "stepId": "s2",
            "agent": {"ref": "specialty-agent/sa-007@v3"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p2",
            "inputs": ["s1"],
        },
    ]

    s3 = _make_s3(
        prefix_to_keys={"users/": [mixed_key]},
        objects={mixed_key: _wf(mixed_steps)},
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    # One workflow scanned, one modified, one ref rewritten — the
    # other ref was already modern but its presence does not push the
    # whole workflow into the "already modern" bucket because at least
    # one step needed work.
    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 1
    assert summary.refs_rewritten == 1
    assert summary.skipped_already_modern == 0
    assert summary.modified_keys == (mixed_key,)

    # Check the rewritten body: step 1 is the new ref, step 2 is
    # untouched.
    body = s3.put_object.call_args.kwargs["Body"]
    new_doc = json.loads(body.decode("utf-8"))
    assert new_doc["steps"][0]["agent"] == {"ref": NEW_REF}
    assert new_doc["steps"][1]["agent"] == {"ref": "specialty-agent/sa-007@v3"}


def test_dry_run_walks_same_set_but_writes_nothing():
    """``dry_run=True`` produces identical summary counts and modified
    keys but issues zero ``put_object`` calls."""
    legacy_key = "skills/foo/workflow.json"
    modern_key = "org/o/published/workflows/p-1/v1/workflow.json"
    legacy_steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]
    modern_steps = [
        {
            "stepId": "s1",
            "agent": {"ref": "specialty-agent/abc@v1"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]

    s3 = _make_s3(
        prefix_to_keys={
            "skills/": [legacy_key],
            "org/": [modern_key],
        },
        objects={
            legacy_key: _wf(legacy_steps),
            modern_key: _wf(modern_steps),
        },
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=True,
    )

    assert summary.workflows_scanned == 2
    assert summary.workflows_modified == 1
    assert summary.refs_rewritten == 1
    assert summary.skipped_already_modern == 1
    assert summary.modified_keys == (legacy_key,)
    s3.put_object.assert_not_called()


def test_idempotent_second_run_is_noop():
    """After a real run rewrites everything, a second run sees only
    modern refs and is a true no-op (zero put_object, zero rewrites)."""
    legacy_key = "skills/foo/workflow.json"
    legacy_steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]

    # First run: legacy → modern.
    s3 = _make_s3(
        prefix_to_keys={"skills/": [legacy_key]},
        objects={legacy_key: _wf(legacy_steps)},
    )
    rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )
    rewritten_body = s3.put_object.call_args.kwargs["Body"]

    # Second run: feed the rewritten body back in.
    s3_run2 = _make_s3(
        prefix_to_keys={"skills/": [legacy_key]},
        objects={legacy_key: rewritten_body},
    )
    summary = rewrite_workflow_refs(
        s3=s3_run2,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 0
    assert summary.refs_rewritten == 0
    assert summary.skipped_already_modern == 1
    assert summary.modified_keys == ()
    s3_run2.put_object.assert_not_called()


def test_bare_string_legacy_ref_is_rewritten():
    """The defensive bare-string variant ``"agent": "task-agent"`` is
    also rewritten to the canonical dict form."""
    key = "skills/old/workflow.json"
    steps = [
        {
            "stepId": "s1",
            "agent": "task-agent",
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]
    s3 = _make_s3(
        prefix_to_keys={"skills/": [key]},
        objects={key: _wf(steps)},
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert summary.refs_rewritten == 1
    assert summary.workflows_modified == 1
    body = s3.put_object.call_args.kwargs["Body"]
    new_doc = json.loads(body.decode("utf-8"))
    assert new_doc["steps"][0]["agent"] == {"ref": NEW_REF}


def test_specialty_agent_step_left_alone():
    """A step whose agent is a non-core specialty reference is not a
    target — the rewriter must not touch it and the workflow does not
    count as ``skipped_already_modern`` either (its agent is not in
    the modern Asset_Ref grammar this rewriter recognises)."""
    key = "users/u/published/workflows/p/v1/workflow.json"
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa@v2"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]
    s3 = _make_s3(
        prefix_to_keys={"users/": [key]},
        objects={key: _wf(steps)},
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 0
    assert summary.refs_rewritten == 0
    assert summary.skipped_already_modern == 0
    s3.put_object.assert_not_called()


def test_legacy_ref_preserves_sibling_fields():
    """When the legacy ``agent`` dict carries unrelated sibling fields,
    those fields are preserved on the rewritten value; only ``kind`` is
    dropped and ``ref`` is replaced."""
    key = "skills/foo/workflow.json"
    steps = [
        {
            "stepId": "s1",
            "agent": {
                "kind": "core",
                "ref": "task-agent",
                "promptOverride": "be terse",
            },
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]
    s3 = _make_s3(
        prefix_to_keys={"skills/": [key]},
        objects={key: _wf(steps)},
    )

    rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    body = s3.put_object.call_args.kwargs["Body"]
    new_doc = json.loads(body.decode("utf-8"))
    new_agent = new_doc["steps"][0]["agent"]
    assert new_agent == {"ref": NEW_REF, "promptOverride": "be terse"}


def test_validates_inputs():
    """The public entrypoint rejects empty ``core_task_agent_id`` and
    empty ``bucket`` values."""
    s3 = MagicMock()
    asset_index = MagicMock()

    with pytest.raises(ValueError):
        rewrite_workflow_refs(
            s3=s3,
            asset_index=asset_index,
            core_task_agent_id="",
            bucket=BUCKET,
        )

    with pytest.raises(ValueError):
        rewrite_workflow_refs(
            s3=s3,
            asset_index=asset_index,
            core_task_agent_id=CORE_PID,
            bucket="",
        )


def test_non_workflow_keys_are_skipped():
    """Objects under the scan roots that are not ``*/workflow.json`` are
    ignored without a ``get_object`` call."""
    legacy_key = "skills/foo/workflow.json"
    skill_md_key = "skills/foo/SKILL.md"
    s3 = _make_s3(
        prefix_to_keys={
            "skills/": [legacy_key, skill_md_key],
        },
        objects={
            legacy_key: _wf(
                [
                    {
                        "stepId": "s1",
                        "agent": {"kind": "core", "ref": "task-agent"},
                        "skill": {"assetId": None},
                        "toolCollection": {"assetId": None},
                        "prompt": "p",
                        "inputs": [],
                    }
                ]
            ),
            # SKILL.md is intentionally missing from objects so a
            # get_object call would raise KeyError; the test passes
            # only when the rewriter never reaches for it.
        },
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    assert summary.workflows_scanned == 1
    assert summary.workflows_modified == 1
    # Ensure get_object was called only for the workflow.json key.
    fetched_keys = [
        c.kwargs["Key"] for c in s3.get_object.call_args_list
    ]
    assert fetched_keys == [legacy_key]


def test_malformed_json_is_skipped():
    """A workflow whose body fails to parse as JSON is skipped without
    aborting the pass; subsequent workflows still get processed."""
    bad_key = "skills/bad/workflow.json"
    good_key = "skills/good/workflow.json"
    good_steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "p",
            "inputs": [],
        }
    ]
    s3 = _make_s3(
        prefix_to_keys={"skills/": [bad_key, good_key]},
        objects={
            bad_key: b"not-json{",
            good_key: _wf(good_steps),
        },
    )

    summary = rewrite_workflow_refs(
        s3=s3,
        asset_index=MagicMock(),
        core_task_agent_id=CORE_PID,
        bucket=BUCKET,
        dry_run=False,
    )

    # Both files are scanned (the bad one is counted) but only the
    # good one produces a rewrite.
    assert summary.workflows_scanned == 2
    assert summary.workflows_modified == 1
    assert summary.modified_keys == (good_key,)
