
"""Migration report emitter (Requirement 14.6 / Task 8.7).

Writes ``report.json`` summarising a migration run. After a real run
the report is the post-mortem operators consult to confirm every
``(legacy_key, publishedId, version)`` mapping landed; in
``--dry-run`` mode the same payload describes the intended plan
(Requirement 14.2 / 14.6).

The on-disk JSON uses camelCase keys to match the rest of the
platform's storage convention (see ``models.py`` ↔ ``asset_index.py``
mapping).

Requirements: 14.6
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Sequence

from .rewrite import RewriteSummary


# Default filename written next to the operator's CWD. Kept as a
# module-level constant so tests and downstream tooling can reference
# the canonical name without re-typing the string.
DEFAULT_REPORT_FILENAME = "report.json"


def emit(
    *,
    skill_mappings: Sequence[tuple[str, str, int]],
    rewrite_summary: RewriteSummary,
    core_task_agent_id: str,
    admin_user: str,
    admin_org: str,
    bucket: str,
    dry_run: bool = False,
    output_path: Optional[str] = None,
) -> None:
    """Write the migration report to ``report.json``.

    Builds a JSON-serialisable payload with the camelCase shape::

        {
          "mode": "dry-run" | "applied",
          "coreTaskAgentId": "<pid>",
          "adminUser": "<user>",
          "adminOrg": "<org>",
          "bucket": "<bucket>",
          "skills": [
            {"legacyKey": "skills/foo/", "publishedId": "p-1", "version": 1},
            ...
          ],
          "rewrite": {
            "workflowsScanned": N,
            "refsRewritten": N,
            "workflowsModified": N,
            "skippedAlreadyModern": N,
            "modifiedKeys": ["...", ...]
          }
        }

    Writes the payload to ``output_path`` (default
    ``./report.json``) using ``json.dumps(..., indent=2)`` and prints
    a one-line confirmation to stdout so an operator running the CLI
    sees the path plus a quick summary.

    Args:
        skill_mappings: One ``(legacy_key_prefix, published_id, version)``
            tuple per migrated skill, in the order the driver
            processed them.
        rewrite_summary: The :class:`RewriteSummary` returned by
            :func:`rewrite.rewrite_workflow_refs`.
        core_task_agent_id: The bootstrapped Core_Task_Agent's
            ``publishedId``.
        admin_user: User id from ``--admin-user``.
        admin_org: Org id from ``--admin-org``.
        bucket: Artifacts bucket from ``--bucket``.
        dry_run: When ``True``, the emitted report describes the
            intended plan and ``mode`` is set to ``"dry-run"``
            (Requirement 14.2 / 14.6); otherwise ``mode`` is
            ``"applied"``.
        output_path: Optional override for the destination file path.
            Defaults to ``./report.json`` resolved against the
            current working directory. Tests typically supply a
            ``tmp_path``-derived value.
    """
    skills_payload: list[dict[str, Any]] = [
        {
            "legacyKey": legacy_key,
            "publishedId": published_id,
            "version": version,
        }
        for (legacy_key, published_id, version) in skill_mappings
    ]

    rewrite_payload: dict[str, Any] = {
        "workflowsScanned": rewrite_summary.workflows_scanned,
        "refsRewritten": rewrite_summary.refs_rewritten,
        "workflowsModified": rewrite_summary.workflows_modified,
        "skippedAlreadyModern": rewrite_summary.skipped_already_modern,
        "modifiedKeys": list(rewrite_summary.modified_keys),
    }

    payload: dict[str, Any] = {
        "mode": "dry-run" if dry_run else "applied",
        "coreTaskAgentId": core_task_agent_id,
        "adminUser": admin_user,
        "adminOrg": admin_org,
        "bucket": bucket,
        "skills": skills_payload,
        "rewrite": rewrite_payload,
    }

    destination = output_path or os.path.join(
        os.getcwd(), DEFAULT_REPORT_FILENAME
    )

    with open(destination, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2))

    # One-line operator-facing confirmation. Kept compact so it does
    # not crowd the migration's other log output.
    print(
        f"migration report ({payload['mode']}) written to {destination}: "
        f"{len(skills_payload)} skill(s), "
        f"{rewrite_summary.refs_rewritten} ref(s) rewritten across "
        f"{rewrite_summary.workflows_modified}/"
        f"{rewrite_summary.workflows_scanned} workflow(s)"
    )
