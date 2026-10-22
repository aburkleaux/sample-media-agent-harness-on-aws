"""Argparse parser, ``MigrationArgs`` dataclass, and the migration driver.

This module is the single seam between the command-line invocation
(``python -m lab.migration``) and the migration sub-modules. Three
public callables are exported:

- :func:`build_parser` — returns the ``argparse.ArgumentParser`` with
  every documented flag. Tests use this to introspect the surface
  without actually parsing ``sys.argv``.
- :func:`drive` — accepts a parsed :class:`MigrationArgs` (and optional
  pre-built ``S3UserFilesTool`` + :class:`AssetIndex` dependencies) and
  composes ``discovery → bootstrap → copy → rewrite → report``. Sub-
  module functions raise :class:`NotImplementedError` until tasks
  8.2–8.7 land; the wiring here is deliberately stable so those tasks
  only fill in bodies, not signatures.
- :func:`main` — argparse-driven entrypoint used by :mod:`__main__`.
  Builds default boto3-backed dependencies and delegates to
  :func:`drive`. Returns the process exit code.

The flag set is fixed by the spec (Requirement 14.2 / Task 8.1):

============  ======================================================
``--dry-run``  Skip every S3 / DynamoDB write; print the plan only.
``--resume``   Consult the migration markers and skip completed work.
``--admin-user``  Publisher identity placed in ``publishPolicy.ownerIds``.
``--admin-org``   Org id used to render ``org/<adminOrg>/published/...``.
``--bucket``    Artifacts bucket holding both legacy and migrated trees.
``--table``     AssetIndex DynamoDB table name.
============  ======================================================

``--admin-user``, ``--admin-org``, ``--bucket``, and ``--table`` are
all REQUIRED with no environment-variable fallback at this layer
(Requirement 14.4 / 14.7). The migration is a one-shot administrative
operation; failing fast on missing identity / bucket / table is
preferable to silently picking up stale defaults.

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from . import bootstrap, copy as copy_mod, discovery, marker, report, rewrite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsed-argument container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationArgs:
    """Immutable container for the parsed CLI arguments.

    A frozen dataclass instead of a raw ``argparse.Namespace`` so the
    driver and sub-modules can rely on a typed, documented surface, and
    so tests can construct fixtures without invoking argparse.

    Attributes:
        bucket: The S3 bucket holding the legacy ``skills/`` tree and
            the destination ``org/<adminOrg>/published/`` tree. The
            migration treats this as a single bucket; cross-bucket
            moves are out of scope (per design).
        table: The AssetIndex DynamoDB table name. Markers and asset
            rows live in the same table under disjoint partition keys.
        admin_user: User id placed in ``publishPolicy.ownerIds`` on
            every migrated published row and on the bootstrapped
            Core_Task_Agent. Becomes the ``actorId`` field of the
            initial scopeHistory entry.
        admin_org: Org id used to render the destination prefix
            ``org/<admin_org>/published/...``. Drives both bootstrap
            and per-skill copy.
        dry_run: When ``True``, every S3 / DynamoDB write is skipped
            and ``report.emit`` produces a *plan* describing the
            intended changes (Requirement 14.2). The default is
            ``False`` — the migration is one-shot and dry-run must be
            opted into explicitly.
        resume: When ``True``, the driver consults migration markers
            (``marker.is_completed``) before invoking ``copy.migrate_one``
            and skips any legacy skill that already has a completion
            marker. The bootstrap step is independently idempotent via
            its own ``core-task-agent`` marker (Task 8.4) so resume
            also avoids re-creating the Core_Task_Agent on a re-run.
    """

    bucket: str
    table: str
    admin_user: str
    admin_org: str
    dry_run: bool = False
    resume: bool = False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser used by :func:`main`.

    Exposed as a separate function so tests can introspect the option
    surface (``--help`` output, defaults, required-ness) without
    invoking the parser against ``sys.argv``.

    The parser is configured with ``prog='python -m lab.migration'`` so
    the help text matches the documented invocation form.
    """
    parser = argparse.ArgumentParser(
        prog="python -m lab.migration",
        description=(
            "One-shot migration of legacy s3://<bucket>/skills/<name>/ "
            "trees into the unified org/<adminOrg>/published/skills/... "
            "layout, plus bootstrap of the Core_Task_Agent and rewrite "
            "of workflow agent:{kind:'core', ref:'task-agent'} refs."
        ),
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help=(
            "Artifacts S3 bucket holding both legacy and migrated trees. "
            "REQUIRED; no environment-variable fallback at this layer."
        ),
    )
    parser.add_argument(
        "--table",
        required=True,
        help=(
            "AssetIndex DynamoDB table name. Markers and asset rows "
            "share this table under disjoint partition keys. REQUIRED."
        ),
    )
    parser.add_argument(
        "--admin-user",
        required=True,
        dest="admin_user",
        help=(
            "User id placed in publishPolicy.ownerIds on every migrated "
            "row and on the bootstrapped Core_Task_Agent. REQUIRED."
        ),
    )
    parser.add_argument(
        "--admin-org",
        required=True,
        dest="admin_org",
        help=(
            "Org id rendered into the destination prefix "
            "org/<admin-org>/published/...  REQUIRED."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Skip every S3 / DynamoDB write; emit a report describing "
            "the intended plan instead. (Requirement 14.2)"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        dest="resume",
        help=(
            "Consult migration markers and skip already-completed work. "
            "(Requirement 14.7)"
        ),
    )

    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> MigrationArgs:
    """Parse ``argv`` (defaulting to ``sys.argv[1:]``) into a typed
    :class:`MigrationArgs`.

    Kept as a thin convenience wrapper so callers (including tests)
    can skip the dataclass construction boilerplate.
    """
    ns = build_parser().parse_args(argv)
    return MigrationArgs(
        bucket=ns.bucket,
        table=ns.table,
        admin_user=ns.admin_user,
        admin_org=ns.admin_org,
        dry_run=bool(ns.dry_run),
        resume=bool(ns.resume),
    )


# ---------------------------------------------------------------------------
# Default dependency factories
# ---------------------------------------------------------------------------


def _build_default_dependencies(args: MigrationArgs):
    """Construct the default boto3-backed S3 client and AssetIndex.

    Imports are deferred to call time so ``--help`` (and unit tests
    that inject their own dependencies) never pull in boto3. The
    migration package must remain importable in environments without
    AWS credentials configured.

    Returns a ``(s3_client, asset_index)`` tuple. ``s3_client`` is a
    raw ``boto3.client('s3')`` because the migration needs to copy
    across actor boundaries (legacy ``skills/<name>/`` is not under
    any actor prefix) — the actor-scoped ``S3UserFilesTool`` is the
    wrong fit here. Sub-modules that need higher-level operations
    (e.g. ``copy_prefix_absolute``) can wrap the raw client themselves.
    """
    import boto3  # local import; not needed for --help / library use

    from ..asset_index import AssetIndex  # noqa: PLC0415 — defer to call time

    s3_client = boto3.client("s3")
    ddb_table = boto3.resource("dynamodb").Table(args.table)
    asset_index = AssetIndex(ddb_table)
    return s3_client, asset_index


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def drive(
    args: MigrationArgs,
    *,
    s3=None,
    asset_index=None,
) -> int:
    """Compose discovery → bootstrap → copy → rewrite → report.

    This is the single driver function the spec calls out for Task 8.1.
    Sub-module callables are invoked in the order required by the
    design's "Migration sequence" section. Until Tasks 8.2 – 8.7 land,
    each sub-module function raises :class:`NotImplementedError`; the
    wiring below is deliberately complete so those tasks only fill in
    bodies, not signatures.

    Args:
        args: Parsed CLI arguments. Constructed by :func:`parse_args`
            in production; tests typically build one directly.
        s3: Optional pre-built S3 client (or fake) for tests. When
            ``None``, :func:`_build_default_dependencies` constructs a
            real boto3 client.
        asset_index: Optional pre-built :class:`AssetIndex` (or fake)
            for tests. When ``None``, the default factory wires one
            against ``args.table``.

    Returns:
        The intended process exit code. ``0`` on success; non-zero
        codes are reserved for future failure reporting (the current
        skeleton propagates exceptions instead of mapping them to exit
        codes — Task 8.5 / 8.7 will refine this).
    """
    if s3 is None or asset_index is None:
        default_s3, default_index = _build_default_dependencies(args)
        s3 = s3 if s3 is not None else default_s3
        asset_index = asset_index if asset_index is not None else default_index

    logger.info(
        "lab.migration starting: bucket=%s table=%s admin_user=%s "
        "admin_org=%s dry_run=%s resume=%s",
        args.bucket,
        args.table,
        args.admin_user,
        args.admin_org,
        args.dry_run,
        args.resume,
    )

    # ------------------------------------------------------------------
    # 1. Discovery — enumerate legacy skill prefixes under skills/.
    # ------------------------------------------------------------------
    legacy_skills = list(discovery.list_legacy_skills(s3, args.bucket))
    logger.info("discovered %d legacy skill prefix(es)", len(legacy_skills))

    # ------------------------------------------------------------------
    # 2. Bootstrap — ensure the org-scoped Core_Task_Agent exists.
    # Idempotent via the ``core-task-agent`` marker; on resume runs
    # this is a no-op if the marker is already present.
    # ------------------------------------------------------------------
    core_task_agent_id = bootstrap.ensure_core_task_agent(
        asset_index=asset_index,
        s3=s3,
        admin_user=args.admin_user,
        admin_org=args.admin_org,
        bucket=args.bucket,
        dry_run=args.dry_run,
    )
    logger.info("Core_Task_Agent id=%s", core_task_agent_id)

    # ------------------------------------------------------------------
    # 3. Copy — for each legacy skill, marker-check (when resuming)
    # then migrate. Halt on any per-skill exception per Requirement
    # 14.5; the offending key is surfaced via the underlying error.
    # ------------------------------------------------------------------
    skill_mappings: list[tuple[str, str, int]] = []
    for legacy in legacy_skills:
        marker_key = f"legacy-skill::{legacy.legacy_key_prefix}"
        if args.resume and marker.is_completed(asset_index, marker_key):
            logger.info(
                "resume: skipping already-completed legacy=%s",
                legacy.legacy_key_prefix,
            )
            continue
        published_id, version = copy_mod.migrate_one(
            s3=s3,
            asset_index=asset_index,
            legacy_skill=legacy,
            admin_user=args.admin_user,
            admin_org=args.admin_org,
            bucket=args.bucket,
            dry_run=args.dry_run,
        )
        skill_mappings.append(
            (legacy.legacy_key_prefix, published_id, version)
        )

    # ------------------------------------------------------------------
    # 4. Rewrite — replace legacy ``agent:{kind:'core', ref:'task-agent'}``
    # references in every workflow with an Asset_Ref pointing at the
    # bootstrapped Core_Task_Agent. Idempotent.
    # ------------------------------------------------------------------
    rewrite_summary = rewrite.rewrite_workflow_refs(
        s3=s3,
        asset_index=asset_index,
        core_task_agent_id=core_task_agent_id,
        bucket=args.bucket,
        dry_run=args.dry_run,
    )

    # ------------------------------------------------------------------
    # 5. Report — emit the (legacy_key, publishedId, version) mapping
    # plus the rewrite summary. In dry-run mode this is the plan; in a
    # real run it is the post-mortem.
    # ------------------------------------------------------------------
    report.emit(
        skill_mappings=skill_mappings,
        rewrite_summary=rewrite_summary,
        core_task_agent_id=core_task_agent_id,
        admin_user=args.admin_user,
        admin_org=args.admin_org,
        bucket=args.bucket,
        dry_run=args.dry_run,
    )

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Argparse entrypoint used by ``python -m lab.migration``.

    Parses ``argv`` (defaulting to ``sys.argv[1:]``) into a
    :class:`MigrationArgs`, configures basic logging, and delegates to
    :func:`drive`. Returns the process exit code.

    Logging is configured here (rather than in :func:`drive`) so that
    library callers — who supply their own dependencies and likely
    their own logging setup — are not surprised by a side-effecting
    ``basicConfig``.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    return drive(args)
