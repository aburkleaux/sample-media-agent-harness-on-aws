"""Tests for Task 8.7 — migration ``report.emit``.

Verifies the JSON payload shape and on-disk behaviour of
``lab.migration.report.emit`` (Requirement 14.6):

* Applied (non-dry-run) mode produces ``mode == "applied"`` and
  includes every ``(legacy_key, publishedId, version)`` tuple plus
  the rewrite summary, all camel-cased.
* Dry-run mode produces the same shape with ``mode == "dry-run"`` —
  the report describes the intended plan (Requirement 14.2 / 14.6).
* Empty skill mappings + zero rewrite still produce a valid report.
* The function prints a one-line confirmation to stdout containing
  the destination path so operators running the CLI see it land.

Validates: Requirement 14.6
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration.report import DEFAULT_REPORT_FILENAME, emit  # noqa: E402
from lab.migration.rewrite import RewriteSummary  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_report(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.loads(fh.read())


# ---------------------------------------------------------------------------
# Applied mode
# ---------------------------------------------------------------------------


def test_emit_applied_writes_expected_payload(tmp_path, capsys):
    """Non-dry-run run produces ``mode='applied'`` plus full payload."""
    output_path = tmp_path / "report.json"

    emit(
        skill_mappings=[
            ("skills/foo/", "p-1", 1),
            ("skills/bar/", "p-2", 1),
        ],
        rewrite_summary=RewriteSummary(
            workflows_scanned=2,
            refs_rewritten=1,
            workflows_modified=1,
            skipped_already_modern=1,
            modified_keys=("k1",),
        ),
        core_task_agent_id="cta-1",
        admin_user="u",
        admin_org="o",
        bucket="b",
        dry_run=False,
        output_path=str(output_path),
    )

    assert output_path.exists()
    payload = _read_report(output_path)

    assert payload["mode"] == "applied"
    assert payload["coreTaskAgentId"] == "cta-1"
    assert payload["adminUser"] == "u"
    assert payload["adminOrg"] == "o"
    assert payload["bucket"] == "b"

    assert payload["skills"] == [
        {"legacyKey": "skills/foo/", "publishedId": "p-1", "version": 1},
        {"legacyKey": "skills/bar/", "publishedId": "p-2", "version": 1},
    ]

    assert payload["rewrite"] == {
        "workflowsScanned": 2,
        "refsRewritten": 1,
        "workflowsModified": 1,
        "skippedAlreadyModern": 1,
        "modifiedKeys": ["k1"],
    }

    # Operator-facing confirmation line on stdout.
    captured = capsys.readouterr()
    assert str(output_path) in captured.out
    assert "applied" in captured.out


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def test_emit_dry_run_marks_mode(tmp_path, capsys):
    """``dry_run=True`` flips ``mode`` to ``'dry-run'``."""
    output_path = tmp_path / "report.json"

    emit(
        skill_mappings=[("skills/foo/", "p-1", 1)],
        rewrite_summary=RewriteSummary(
            workflows_scanned=1,
            refs_rewritten=1,
            workflows_modified=1,
            skipped_already_modern=0,
            modified_keys=("k1",),
        ),
        core_task_agent_id="cta-1",
        admin_user="u",
        admin_org="o",
        bucket="b",
        dry_run=True,
        output_path=str(output_path),
    )

    payload = _read_report(output_path)
    assert payload["mode"] == "dry-run"
    # Body still describes the (intended) plan in full.
    assert payload["skills"] == [
        {"legacyKey": "skills/foo/", "publishedId": "p-1", "version": 1},
    ]
    assert payload["rewrite"]["refsRewritten"] == 1

    captured = capsys.readouterr()
    assert "dry-run" in captured.out


# ---------------------------------------------------------------------------
# Empty / zero case
# ---------------------------------------------------------------------------


def test_emit_empty_run_produces_valid_report(tmp_path):
    """No skills + no rewrites still emits a well-formed report."""
    output_path = tmp_path / "report.json"

    emit(
        skill_mappings=[],
        rewrite_summary=RewriteSummary(),
        core_task_agent_id="cta-1",
        admin_user="u",
        admin_org="o",
        bucket="b",
        dry_run=False,
        output_path=str(output_path),
    )

    payload = _read_report(output_path)
    assert payload["mode"] == "applied"
    assert payload["skills"] == []
    assert payload["rewrite"] == {
        "workflowsScanned": 0,
        "refsRewritten": 0,
        "workflowsModified": 0,
        "skippedAlreadyModern": 0,
        "modifiedKeys": [],
    }


# ---------------------------------------------------------------------------
# Default-path behaviour
# ---------------------------------------------------------------------------


def test_emit_defaults_to_cwd_report_json(tmp_path, monkeypatch):
    """Without ``output_path`` the report lands in CWD/report.json."""
    monkeypatch.chdir(tmp_path)

    emit(
        skill_mappings=[],
        rewrite_summary=RewriteSummary(),
        core_task_agent_id="cta-1",
        admin_user="u",
        admin_org="o",
        bucket="b",
    )

    expected = tmp_path / DEFAULT_REPORT_FILENAME
    assert expected.exists()
    payload = _read_report(expected)
    assert payload["mode"] == "applied"
