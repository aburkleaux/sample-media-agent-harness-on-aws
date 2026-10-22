"""End-to-end smoke test for ``python -m lab.migration --dry-run`` (Task 9.1).

Stands up an in-memory fixture bucket containing two legacy skill
prefixes plus one workflow JSON that references the legacy
``agent:{kind:'core', ref:'task-agent'}`` shape, drives
``cli.drive(MigrationArgs(..., dry_run=True))`` against that fixture,
and asserts the dry-run is genuinely read-only:

- exit code is ``0`` (cli.drive contract)
- no ``s3.copy_object`` / ``s3.put_object`` / ``s3.delete_objects`` /
  ``s3.delete_object`` calls (Req 14.2)
- no ``asset_index.put`` and no ``asset_index.put_marker`` calls
  (Req 14.2 / 14.4 / 14.7 — markers must not be persisted on a dry run)
- a ``report.json`` is written under ``cwd`` with ``mode == "dry-run"``,
  one entry per legacy skill (Req 14.1 / 14.6), the rewrite stage
  reports at least one workflow scanned and the legacy reference seen
  (Req 14.3), and the printed plan names the report path (Req 14.6)

The test wires the migration through ``cli.drive`` rather than
``cli.main`` so argparse stays out of the picture; the same dependency
injection seam used by ``test_migration_bootstrap`` /
``test_migration_copy`` / ``test_migration_rewrite`` lets us pass a
``MagicMock`` S3 client and AssetIndex without booting boto3 or moto.

Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# Make ``lab.migration`` importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration import cli  # noqa: E402
from lab.migration.cli import MigrationArgs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

ADMIN_USER = "u-admin"
ADMIN_ORG = "o-acme"
BUCKET = "fixture-bucket"
TABLE = "fixture-table"

# Two legacy skill prefixes under ``skills/`` — the discovery step
# yields one :class:`LegacySkill` per ``CommonPrefixes`` entry.
LEGACY_FOO_PREFIX = "skills/foo/"
LEGACY_BAR_PREFIX = "skills/bar/"

# One workflow JSON under the migrated org tree carrying the legacy
# ``agent:{kind:'core', ref:'task-agent'}`` reference. The rewrite
# stage's ``_iter_workflow_keys`` paginates ``Prefix="org/"`` (no
# Delimiter) so a key under ``org/`` is sufficient to exercise the
# rewrite path.
WORKFLOW_KEY = "org/o-acme/published/workflows/wf-1/v1/workflow.json"
WORKFLOW_BODY = json.dumps(
    {
        "name": "wf-1",
        "steps": [
            {
                "stepId": "s1",
                "agent": {"kind": "core", "ref": "task-agent"},
                "skill": {"assetId": None},
                "toolCollection": {"assetId": None},
                "prompt": "p",
                "inputs": [],
            }
        ],
    },
    indent=2,
).encode("utf-8")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_paginator() -> MagicMock:
    """Build a paginator whose ``paginate`` dispatches on the call kwargs.

    The driver hits ``ListObjectsV2`` in two distinct shapes:

    1. **Discovery** (``lab.migration.discovery.list_legacy_skills``)
       — ``paginate(Bucket=..., Prefix="skills/", Delimiter="/")``.
       Returns ``CommonPrefixes`` for both fixture skills.

    2. **Rewrite** (``lab.migration.rewrite._iter_workflow_keys``) —
       once per scan root in
       ``rewrite._WORKFLOW_SCAN_PREFIXES = ("skills/", "org/", "users/")``,
       no Delimiter. Returns ``Contents`` listing the workflow keys
       under each root; only ``org/`` carries the fixture workflow.

    In dry-run mode ``copy.migrate_one`` short-circuits before
    listing the source prefix, so we never see a
    ``Prefix="skills/foo/"`` (no Delimiter) call from copy. If a
    future change introduced one, the empty-default branch would
    return an empty page and the smoke would still pass through copy
    cleanly — what matters is that no S3 *writes* happen.
    """
    paginator = MagicMock()

    def _paginate(*, Bucket, Prefix, **kwargs):
        delimiter = kwargs.get("Delimiter")
        # Discovery: Prefix="skills/" + Delimiter="/" → CommonPrefixes
        if Prefix == "skills/" and delimiter == "/":
            return iter(
                [
                    {
                        "CommonPrefixes": [
                            {"Prefix": LEGACY_FOO_PREFIX},
                            {"Prefix": LEGACY_BAR_PREFIX},
                        ]
                    }
                ]
            )
        # Rewrite: Prefix=<root>, no Delimiter
        if delimiter is None:
            if Prefix == "org/":
                return iter([{"Contents": [{"Key": WORKFLOW_KEY}]}])
            # ``skills/`` and ``users/`` carry no workflow JSONs in
            # this fixture.
            return iter([{"Contents": []}])
        # Defensive default — a paginate shape we did not plan for.
        # Returning an empty page rather than raising keeps the test's
        # failure mode tied to its assertions, not to a stray
        # ``KeyError`` out of ``MagicMock``.
        return iter([{"Contents": []}])

    paginator.paginate.side_effect = _paginate
    return paginator


def _make_s3() -> MagicMock:
    """Build a ``MagicMock`` boto3 ``s3`` client wired for a dry-run pass.

    The paginator handles both discovery and rewrite shapes;
    ``get_object`` returns the single fixture workflow body. All write
    surfaces (``copy_object``, ``put_object``, ``delete_object``,
    ``delete_objects``) remain bare ``MagicMock`` attributes so the
    ``assert_not_called`` checks at the end of the test surface any
    accidental side effect.
    """
    s3 = MagicMock()
    s3.get_paginator.return_value = _make_paginator()

    def _get_object(*, Bucket, Key):
        if Key == WORKFLOW_KEY:
            body = MagicMock()
            body.read.return_value = WORKFLOW_BODY
            return {"Body": body}
        # Anything else is a test-fixture mismatch — fail loudly so we
        # don't silently rewrite an unintended object.
        raise KeyError(Key)

    s3.get_object.side_effect = _get_object
    return s3


def _make_asset_index() -> MagicMock:
    """Build a ``MagicMock`` AssetIndex wired for a dry-run pass.

    ``get_marker`` always resolves to ``None`` (no resume); ``put``
    and ``put_marker`` are no-op ``AsyncMock`` instances installed so
    the ``assert_not_called`` checks at the end of the test surface
    any accidental write.
    """
    index = MagicMock()
    index.get_marker = AsyncMock(return_value=None)
    index.put_marker = AsyncMock(return_value=None)
    index.put = AsyncMock(return_value=None)
    return index


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------


def test_dry_run_smoke_emits_plan_without_side_effects(
    tmp_path, monkeypatch, capsys
):
    """``cli.drive(MigrationArgs(..., dry_run=True))`` over a fixture
    bucket exits 0, performs zero S3 / AssetIndex / marker writes, and
    writes a ``report.json`` describing the intended plan.

    Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7
    """
    # ``report.emit`` writes ``./report.json`` relative to ``cwd``;
    # steer it into the test-isolated tmp dir.
    monkeypatch.chdir(tmp_path)

    s3 = _make_s3()
    asset_index = _make_asset_index()

    args = MigrationArgs(
        bucket=BUCKET,
        table=TABLE,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        dry_run=True,
        resume=False,
    )

    rc = cli.drive(args, s3=s3, asset_index=asset_index)

    # cli.drive contract: 0 = success.
    assert rc == 0

    # ------------------------------------------------------------------
    # Req 14.2 — dry-run is read-only against S3.
    # ------------------------------------------------------------------
    s3.copy_object.assert_not_called()
    s3.put_object.assert_not_called()
    s3.delete_objects.assert_not_called()
    s3.delete_object.assert_not_called()

    # ------------------------------------------------------------------
    # Req 14.2 / 14.4 / 14.7 — dry-run is read-only against AssetIndex
    # and the migration markers.
    # ------------------------------------------------------------------
    asset_index.put.assert_not_called()
    asset_index.put_marker.assert_not_called()

    # ------------------------------------------------------------------
    # Req 14.6 — a plan report is emitted next to ``cwd``. Resolve via
    # ``os.getcwd()`` rather than ``tmp_path`` directly so the assert
    # tracks the path ``report.emit`` actually wrote (macOS resolves
    # ``tmp_path`` through ``/private/var/...`` which can otherwise
    # produce a non-matching string).
    # ------------------------------------------------------------------
    report_path = os.path.join(os.getcwd(), "report.json")
    assert os.path.isfile(report_path)

    with open(report_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    assert payload["mode"] == "dry-run"
    assert payload["bucket"] == BUCKET
    assert payload["adminUser"] == ADMIN_USER
    assert payload["adminOrg"] == ADMIN_ORG
    assert (
        isinstance(payload["coreTaskAgentId"], str)
        and payload["coreTaskAgentId"]
    )

    # Req 14.1 — every legacy skill is in the plan, each with a
    # freshly-generated publishedId and version=1.
    skills = payload["skills"]
    assert len(skills) == 2
    legacy_keys = {entry["legacyKey"] for entry in skills}
    assert legacy_keys == {LEGACY_FOO_PREFIX, LEGACY_BAR_PREFIX}
    published_ids: set[str] = set()
    for entry in skills:
        assert isinstance(entry["publishedId"], str) and entry["publishedId"]
        assert entry["version"] == 1
        published_ids.add(entry["publishedId"])
    # Each legacy skill gets a distinct publishedId in the plan.
    assert len(published_ids) == 2

    # Req 14.3 — the rewrite stage walked the workflow tree, found the
    # legacy reference, and recorded its key. ``modifiedKeys`` is the
    # *intent* (no put_object happened, asserted above).
    rewrite = payload["rewrite"]
    assert rewrite["workflowsScanned"] >= 1
    assert rewrite["refsRewritten"] >= 1
    assert WORKFLOW_KEY in rewrite["modifiedKeys"]

    # Req 14.6 — the operator-facing print line names the dry-run mode
    # and the report filename, so a CI run's stdout doubles as the
    # plan summary.
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "report.json" in out
