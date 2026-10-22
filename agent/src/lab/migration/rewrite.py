"""Workflow ``agent:`` ref rewriter — Task 8.6.

Scans every workflow asset (legacy and migrated) for the legacy
``agent:{kind:'core', ref:'task-agent'}`` reference shape and rewrites
each occurrence to ``agent:{ref:'specialty-agent/<core_task_agent_id>@v1'}``.
Idempotent: a second run with all refs already in the new form is a
no-op. In dry-run mode the rewriter walks the same set of workflows and
returns a summary of intended edits without writing.

The rewrite covers three S3 roots that may hold a workflow JSON at
migration time:

- ``skills/`` — the legacy layout. The migration treats this as a
  best-effort scan; if the legacy tree never stored workflows under
  this root, the scan simply yields nothing and the summary is
  unaffected.
- ``org/`` — the org-published tree, including the freshly-migrated
  rows produced by :mod:`lab.migration.copy`.
- ``users/`` — both the user-drafts tree and the user-published tree.

Group-scoped (``groups/``) prefixes are intentionally not scanned: the
``Share`` canonical operation is the only producer of group-scoped
rows and it does not run during the one-shot migration. If a future
re-run encounters group-scoped rows the rewriter is forward-compatible
— extend :data:`_WORKFLOW_SCAN_PREFIXES` to include ``"groups/"``.

Workflow files are identified by the standard ``workflow.json`` filename
(per :data:`workflow_crud._WORKFLOW_FILENAME`); matching by suffix lets
the rewriter survive layout drift between draft and published trees.

Requirements: 14.3
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: S3 root prefixes scanned for workflow JSON files. Order is irrelevant
#: — the rewriter walks the union and produces an order-independent
#: summary. ``groups/`` is omitted because Share is the only producer
#: of group-scoped rows and it does not run during the one-shot
#: migration window (see module docstring).
_WORKFLOW_SCAN_PREFIXES: tuple[str, ...] = (
    "skills/",
    "org/",
    "users/",
)

#: Workflow JSON files are named ``workflow.json`` everywhere in the
#: codebase (see ``workflow_crud._WORKFLOW_FILENAME`` and
#: ``readiness._WORKFLOW_FILENAME``). Matching by suffix keeps the
#: rewriter independent of the surrounding directory structure
#: (drafts/published/legacy all produce the same filename).
_WORKFLOW_JSON_SUFFIX: str = "/workflow.json"

#: The legacy core-task-agent ref string. Pre-migration workflows
#: encoded the core agent as ``{"kind": "core", "ref": "task-agent"}``
#: or, in older corners, the bare string ``"task-agent"``.
_LEGACY_CORE_TASK_AGENT_REF: str = "task-agent"

#: Canonical Asset_Ref grammar for a specialty-agent published row.
#: Mirrors the ``_ASSET_REF_PATTERN`` in ``workflow_crud.py`` but
#: narrowed to the ``specialty-agent`` category — that is the only
#: form the rewriter produces and the only one we treat as "already
#: modern" for idempotency. Identifiers use the same
#: ``[A-Za-z0-9_-]+`` character class.
_MODERN_SPECIALTY_REF_PATTERN: re.Pattern[str] = re.compile(
    r"^specialty-agent/[A-Za-z0-9_-]+@v\d+$"
)


# ---------------------------------------------------------------------------
# Summary dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RewriteSummary:
    """Summary of a rewrite pass.

    Attributes:
        workflows_scanned: Total number of workflow assets walked
            (legacy + migrated).
        refs_rewritten: Number of legacy ``agent:`` refs replaced with
            the new Asset_Ref form. In dry-run mode this is the
            *intended* count.
        workflows_modified: Subset of ``workflows_scanned`` whose
            content changed (one workflow may carry multiple legacy
            refs).
        skipped_already_modern: Number of workflows where every
            ``agent:`` ref was already in the new form (the idempotent
            no-op path).
        modified_keys: S3 keys of the workflow assets whose content was
            (or would be, in dry-run) updated. Surfaced in the report
            so operators can spot-check a sample after the run.
    """

    workflows_scanned: int = 0
    refs_rewritten: int = 0
    workflows_modified: int = 0
    skipped_already_modern: int = 0
    modified_keys: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_modern_specialty_ref(value: Any) -> bool:
    """Return ``True`` iff ``value`` is a string matching the canonical
    Asset_Ref grammar for a specialty agent (``specialty-agent/<id>@v<n>``).

    Used to detect refs that are already in the new form so a re-run
    over fully-migrated content is a no-op.
    """
    return (
        isinstance(value, str)
        and _MODERN_SPECIALTY_REF_PATTERN.match(value) is not None
    )


def _rewrite_agent(
    agent: Any, new_ref: str
) -> tuple[Any, bool, bool]:
    """Rewrite one step's ``agent`` value if it carries a legacy core ref.

    Recognised legacy shapes:

    1. ``{"kind": "core", "ref": "task-agent"}`` — the canonical legacy
       form. Sibling keys (e.g. an unrelated ``promptOverride``) are
       preserved on the rewritten value; only ``kind`` is dropped and
       ``ref`` is replaced.
    2. ``{"kind": "core", "ref": "task-agent", ...}`` — same as above,
       sibling fields preserved.
    3. ``{"ref": "task-agent"}`` (no ``kind``) — defensive; the older
       shape sometimes omitted ``kind``.
    4. The bare string ``"task-agent"`` directly on ``agent`` — also
       defensive; older corners stored a string in place of the dict.

    Returns ``(new_agent_value, was_rewritten, is_modern)`` where
    ``is_modern`` is ``True`` iff the input was already in the canonical
    Asset_Ref form (``agent.ref`` matches ``specialty-agent/<id>@v<n>``).
    Modern values are returned unchanged so a re-run is a no-op.
    """
    # Bare-string variant: ``"agent": "task-agent"``.
    if isinstance(agent, str):
        if agent == _LEGACY_CORE_TASK_AGENT_REF:
            return {"ref": new_ref}, True, False
        # Some other bare string — leave alone, not modern.
        return agent, False, False

    if isinstance(agent, dict):
        kind = agent.get("kind")
        ref = agent.get("ref")

        # Legacy match: ref is the bare ``"task-agent"`` literal AND
        # ``kind`` is either absent or explicitly ``"core"``. We do NOT
        # match when ``kind`` is some other category (e.g.
        # ``"specialty"``) because that would be a malformed step we
        # have no mandate to repair.
        is_legacy = (
            ref == _LEGACY_CORE_TASK_AGENT_REF
            and kind in (None, "core")
        )

        if is_legacy:
            new_agent = dict(agent)  # preserve sibling fields
            new_agent.pop("kind", None)
            new_agent["ref"] = new_ref
            return new_agent, True, False

        # Already-modern: ``agent.ref`` matches the Asset_Ref grammar.
        # We don't insist on ``kind`` being absent — the canonical form
        # produced by this rewriter drops ``kind`` but pre-existing
        # callers may have left it set. Treat any modern-grammar ref as
        # idempotent.
        if _is_modern_specialty_ref(ref):
            return agent, False, True

        # Anything else — a specialty agent referenced by ``assetId``,
        # an unrecognised kind, or a malformed dict — is left alone and
        # is neither modern nor legacy. It does not move the
        # ``skipped_already_modern`` counter because the workflow as a
        # whole is not "fully modern" if its ``agent`` field uses a
        # different shape.
        return agent, False, False

    # ``agent`` is None, a list, or some other unexpected type. Leave
    # the value untouched; it will neither be rewritten nor counted as
    # modern.
    return agent, False, False


def _rewrite_workflow_doc(
    doc: Any, new_ref: str
) -> tuple[bool, int, bool]:
    """Walk a parsed workflow JSON document and rewrite every legacy
    core-task-agent ref in place.

    Mutates ``doc`` directly so the caller can re-serialize without an
    extra deep copy (workflows here are small, but this saves an
    allocation per file).

    Returns ``(modified, refs_rewritten, all_refs_modern)``:

    - ``modified`` is ``True`` iff at least one ref was rewritten.
    - ``refs_rewritten`` is the count of replacements made.
    - ``all_refs_modern`` is ``True`` iff the workflow had at least
      one step with an ``agent`` field AND every such ``agent`` was
      already in the modern form (no rewrites needed). A workflow
      with zero ``agent`` fields does NOT count as "all modern" — it
      is genuinely empty rather than fully migrated.
    """
    if not isinstance(doc, dict):
        return False, 0, False

    steps = doc.get("steps")
    if not isinstance(steps, list):
        return False, 0, False

    refs_seen = 0
    refs_rewritten = 0
    refs_modern = 0

    for step in steps:
        if not isinstance(step, dict) or "agent" not in step:
            continue
        refs_seen += 1
        new_agent, was_rewritten, is_modern = _rewrite_agent(
            step["agent"], new_ref
        )
        if was_rewritten:
            step["agent"] = new_agent
            refs_rewritten += 1
        elif is_modern:
            refs_modern += 1

    modified = refs_rewritten > 0
    all_modern = (
        refs_seen > 0
        and refs_rewritten == 0
        and refs_modern == refs_seen
    )
    return modified, refs_rewritten, all_modern


def _iter_workflow_keys(s3: Any, bucket: str) -> Iterable[str]:
    """Yield every ``*/workflow.json`` key under the migration scan roots.

    Paginates ``ListObjectsV2`` once per root prefix in
    :data:`_WORKFLOW_SCAN_PREFIXES`; deliberately does NOT pass a
    ``Delimiter`` so the scan recurses into every depth. Filters by
    suffix to ``/workflow.json`` so unrelated objects (skill manifests,
    persona files, sample inputs) are skipped without an extra
    HEAD/GET round trip.
    """
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in _WORKFLOW_SCAN_PREFIXES:
        page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)
        for page in page_iterator:
            for entry in page.get("Contents", []) or []:
                key = entry.get("Key")
                if isinstance(key, str) and key.endswith(_WORKFLOW_JSON_SUFFIX):
                    yield key


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def rewrite_workflow_refs(
    *,
    s3: Any,
    asset_index: Any,
    core_task_agent_id: str,
    bucket: str,
    dry_run: bool = False,
) -> RewriteSummary:
    """Replace legacy core-task-agent refs across every workflow.

    Walks every ``workflow.json`` under the three migration scan roots
    (:data:`_WORKFLOW_SCAN_PREFIXES`), and for each step's ``agent``
    field replaces any legacy ``{"kind": "core", "ref": "task-agent"}``
    (or its bare-string equivalent) with
    ``{"ref": "specialty-agent/<core_task_agent_id>@v1"}``. Workflows
    whose every ``agent`` ref is already in the modern Asset_Ref form
    are skipped entirely and counted toward ``skipped_already_modern``;
    a re-run after a successful pass is therefore a true no-op.

    The rewrite is in place: each modified workflow JSON is written
    back to its same key with ``json.dumps(..., indent=2)`` so diffs
    remain reviewable. In ``dry_run`` mode the rewriter visits the
    same files and computes the same summary, but performs no
    ``put_object`` calls.

    The ``asset_index`` parameter is accepted to keep the signature
    stable with the rest of the migration package (every public
    rewrite/copy/bootstrap entrypoint accepts the same dependency
    pair). The current implementation enumerates workflow files via
    S3 directly because legacy workflows have no AssetIndex row
    until the migration commits — a pure-S3 scan is therefore the
    only way to reach all of them in one pass.

    Args:
        s3: A boto3 ``s3`` client (or test fake). Must support
            ``get_paginator("list_objects_v2")``, ``get_object``, and
            (when ``dry_run`` is False) ``put_object``.
        asset_index: The :class:`AssetIndex` instance. Currently
            unused by the rewriter; retained for API symmetry with
            the rest of the migration package.
        core_task_agent_id: The bootstrapped Core_Task_Agent's
            ``publishedId`` (produced by
            :func:`lab.migration.bootstrap.ensure_core_task_agent`).
            Becomes the ``<id>`` segment of every rewritten ref.
        bucket: Artifacts S3 bucket holding both the legacy and
            migrated trees.
        dry_run: When ``True``, no ``put_object`` calls are issued;
            the returned summary describes the intended plan and the
            ``modified_keys`` tuple lists every workflow that would
            be edited.

    Returns:
        :class:`RewriteSummary` describing the pass.

    Raises:
        ValueError: If ``core_task_agent_id`` or ``bucket`` is empty.
    """
    if not core_task_agent_id:
        raise ValueError("core_task_agent_id must be a non-empty string")
    if not bucket:
        raise ValueError("bucket must be a non-empty string")

    # ``asset_index`` is part of the stable migration-package API but
    # the rewriter does not consult it (see method docstring). Bind it
    # to ``_`` to make the intent explicit and silence linters.
    _ = asset_index

    new_ref = f"specialty-agent/{core_task_agent_id}@v1"

    workflows_scanned = 0
    refs_rewritten_total = 0
    workflows_modified = 0
    skipped_already_modern = 0
    modified_keys: list[str] = []

    for key in _iter_workflow_keys(s3, bucket):
        workflows_scanned += 1

        try:
            response = s3.get_object(Bucket=bucket, Key=key)
            body_bytes = response["Body"].read()
            doc = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            # A malformed workflow.json should not abort the pass —
            # the migration's rewrite step is best-effort across
            # heterogeneous legacy data. Surface the bad key in the
            # logs so operators can investigate, then continue.
            logger.warning(
                "rewrite: skipping malformed workflow at key=%s: %s",
                key,
                err,
            )
            continue

        modified, refs_rewritten, all_modern = _rewrite_workflow_doc(
            doc, new_ref
        )

        if modified:
            workflows_modified += 1
            refs_rewritten_total += refs_rewritten
            modified_keys.append(key)
            if dry_run:
                logger.info(
                    "rewrite (DRY): would rewrite %d ref(s) in key=%s",
                    refs_rewritten,
                    key,
                )
            else:
                # ``indent=2`` keeps diffs reviewable. The body must
                # be bytes so that ``put_object`` does not need to
                # infer an encoding from a str.
                body_out = json.dumps(doc, indent=2).encode("utf-8")
                s3.put_object(Bucket=bucket, Key=key, Body=body_out)
                logger.info(
                    "rewrite: rewrote %d ref(s) in key=%s",
                    refs_rewritten,
                    key,
                )
        elif all_modern:
            # Idempotent path: every agent ref is already in the
            # modern form. Count this toward the skipped tally so the
            # report can show "N workflows already migrated, no edit
            # needed".
            skipped_already_modern += 1

    return RewriteSummary(
        workflows_scanned=workflows_scanned,
        refs_rewritten=refs_rewritten_total,
        workflows_modified=workflows_modified,
        skipped_already_modern=skipped_already_modern,
        modified_keys=tuple(modified_keys),
    )
