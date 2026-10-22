#!/usr/bin/env python3
"""One-shot migration: drain the legacy ``GATEWAYS`` env var into the registry (R5).

Background
----------
Before the gateway-registry-admin spec landed, the Main Agent learned about
its MCP gateways from a JSON-encoded ``GATEWAYS`` env var emitted by the
CDK stack at deploy time. Adding or removing a gateway meant a CDK
redeploy and a Bedrock runtime cold-start (see ``infra/lib/agent-infra-stack.ts``
for the legacy emit sites).

The registry replaces that env var with a DDB table backed by Secrets
Manager: gateway definitions are operational config (which external
services to talk to) rather than infrastructure, and they should be
mutable without redeploying. This script is the bridge — it reads the
existing ``GATEWAYS`` JSON, writes one row per entry to the
``${projectName}-gateway-registry`` DDB table, and stores each entry's
OAuth ``client_secret`` in Secrets Manager at
``/<project>/gateway/<id>/secret``. After this runs successfully, the
``GATEWAYS`` env binding is removed from the CDK stack (task 6.3) and
``MCPManager.load_gateways_from_registry`` becomes the sole loader path
(R6).

The legacy ``GATEWAYS`` entry shape (per ``infra/lib/agent-infra-stack.ts``)::

    {
      "id": "...",
      "name": "...",
      "url": "...",
      "client_id": "...",
      "client_secret": "...",
      "token_endpoint": "...",
      "scope": "..."
    }

Each entry maps to a ``GatewayDescriptor`` with ``auth_type="jwt"`` via
:meth:`GatewayDescriptor.from_legacy_env`. The raw ``client_secret`` is
popped off the dict and handed to :meth:`GatewayRegistry.register` as
``raw_secret`` so it lands in Secrets Manager rather than DDB (R2).

Idempotency
-----------
The script checks each gateway id with :meth:`GatewayRegistry.get` before
attempting to register. Ids already present in DDB are skipped with a
warning rather than re-registered — that keeps a re-run from creating
orphan Secrets Manager entries (``register`` writes the secret *before*
the conditional ``PutItem``, so guarding on the DDB pre-check avoids
leaving a secret behind when the row already exists).

Required environment / inputs
-----------------------------
- ``GATEWAYS`` (or ``--gateways-json`` / ``--gateways-file``): the JSON
  payload to migrate. Defaults to the env var so the script can run as a
  drop-in replacement for the legacy bootstrap path.
- ``GATEWAY_REGISTRY_TABLE`` (or ``--table``): the DDB table name.
- ``PROJECT_NAME`` (or ``--project-name``): used to build the Secrets
  Manager path ``/<project>/gateway/<id>/secret``.
- ``AWS_REGION`` (or ``--region``): boto3 region.

Usage
-----
Dry run against the runtime's existing env::

    python agent/scripts/migrate_gateways_to_registry.py --dry-run

Real migration with explicit overrides::

    GATEWAY_REGISTRY_TABLE=mediaworks-harness-gateway-registry \\
    PROJECT_NAME=mediaworks-harness \\
    AWS_REGION=us-west-2 \\
    python agent/scripts/migrate_gateways_to_registry.py \\
        --gateways-file ./gateways.json

Pass ``--gateways-json '[{...}]'`` to migrate a literal JSON payload
without setting the env var.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

# Make ``agent/src`` importable when the script is run from the repo
# root or from inside ``agent/``. Mirrors the path setup in
# ``import_drafts.py`` so the script doesn't depend on the agent
# package being pip-installed.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"),
)

import boto3  # noqa: E402 — sys.path tweak above must run first

from gateway.registry import (  # noqa: E402
    DuplicateGatewayError,
    GatewayDescriptor,
    GatewayRegistry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("migrate_gateways_to_registry")


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def _load_gateways_json(
    *,
    cli_json: Optional[str],
    cli_file: Optional[str],
) -> list[dict]:
    """Resolve the legacy ``GATEWAYS`` JSON payload from CLI or env.

    Precedence: ``--gateways-json`` > ``--gateways-file`` > ``$GATEWAYS``.
    Returns the parsed list of legacy entry dicts. Raises ``SystemExit``
    with a clear message when no source is configured or the payload is
    not a JSON array of objects.
    """
    raw: Optional[str] = None
    source = ""

    if cli_json:
        raw = cli_json
        source = "--gateways-json"
    elif cli_file:
        try:
            with open(cli_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise SystemExit(f"failed to read --gateways-file {cli_file!r}: {exc}")
        source = f"--gateways-file {cli_file}"
    else:
        env_val = os.environ.get("GATEWAYS")
        if env_val:
            raw = env_val
            source = "$GATEWAYS"

    if not raw:
        raise SystemExit(
            "no gateways payload provided. Set $GATEWAYS, or pass "
            "--gateways-json / --gateways-file."
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"failed to parse gateways JSON from {source}: {exc}")

    if not isinstance(parsed, list):
        raise SystemExit(
            f"gateways payload from {source} must be a JSON array; "
            f"got {type(parsed).__name__}"
        )
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise SystemExit(
                f"gateways payload from {source}: entry {i} is not an object"
            )

    logger.info("Loaded %d gateway entries from %s", len(parsed), source)
    return parsed


# ---------------------------------------------------------------------------
# Per-entry migration
# ---------------------------------------------------------------------------


async def _migrate_one(
    registry: GatewayRegistry,
    raw_entry: dict,
    *,
    dry_run: bool,
) -> str:
    """Migrate a single legacy entry. Returns a status string for the summary.

    Status values: ``"migrated"``, ``"skipped"`` (already present in DDB),
    ``"missing_secret"`` (legacy entry had no ``client_secret``),
    ``"error"`` (anything else; logged with traceback).
    """
    # Work off a copy so we can pop `client_secret` without mutating the
    # caller's list (matters for the migration summary and re-runs).
    cfg = dict(raw_entry)

    # Build descriptor first so an invalid entry surfaces before we touch
    # Secrets Manager. `from_legacy_env` already ignores `client_secret`
    # — that field stays only on the popped raw dict.
    try:
        descriptor = GatewayDescriptor.from_legacy_env(cfg)
    except Exception as exc:  # noqa: BLE001 — defensive logging
        logger.error("entry %r: failed to build descriptor: %s", cfg.get("id"), exc)
        return "error"

    # Stamp the audit trail so the registry can tell migrated rows apart
    # from admin-registered ones at a glance (R1).
    descriptor.created_by = "migration"

    raw_secret = cfg.pop("client_secret", None)
    if raw_secret is None:
        # Legacy entries are all OAuth/JWT today, so a missing secret is
        # almost certainly a malformed input rather than an intentional
        # public gateway. Surface it loudly and skip the row.
        logger.error(
            "gateway %r: legacy entry has no 'client_secret'; skipping",
            descriptor.id,
        )
        return "missing_secret"

    # Idempotency check: if a row already exists for this id, skip
    # entirely. We do this *before* calling `register()` because
    # `register()` writes the secret to Secrets Manager *before* the
    # conditional DDB put — relying on `DuplicateGatewayError` alone
    # would leave an orphan secret behind on every re-run.
    existing = await registry.get(descriptor.id)
    if existing is not None:
        logger.warning(
            "gateway %r: already present in registry (created_at=%s, "
            "created_by=%s); skipping",
            descriptor.id,
            existing.created_at,
            existing.created_by,
        )
        return "skipped"

    if dry_run:
        logger.info(
            "[dry-run] would register gateway %r (url=%s, auth_type=%s)",
            descriptor.id,
            descriptor.url,
            descriptor.auth_type,
        )
        return "migrated"

    try:
        await registry.register(descriptor, raw_secret=raw_secret)
    except DuplicateGatewayError:
        # Race: another runner registered this id between our `get` and
        # `register`. Treat as skipped so the summary reflects reality.
        logger.warning(
            "gateway %r: duplicate detected during register (race); skipping",
            descriptor.id,
        )
        return "skipped"
    except Exception as exc:  # noqa: BLE001 — surface any boto3 / SM failure
        logger.error("gateway %r: register failed: %s", descriptor.id, exc)
        return "error"

    logger.info(
        "gateway %r: registered (auth_type=%s, secret_arn=%s)",
        descriptor.id,
        descriptor.auth_type,
        getattr(descriptor.auth_config, "client_secret_arn", None),
    )
    return "migrated"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    entries = _load_gateways_json(
        cli_json=args.gateways_json,
        cli_file=args.gateways_file,
    )

    table_name = args.table or os.environ.get("GATEWAY_REGISTRY_TABLE")
    if not table_name:
        raise SystemExit(
            "GATEWAY_REGISTRY_TABLE not set. Pass --table or export the env var."
        )

    project_name = args.project_name or os.environ.get("PROJECT_NAME")
    if not project_name:
        raise SystemExit(
            "PROJECT_NAME not set. Pass --project-name or export the env var "
            "(needed to build the Secrets Manager path)."
        )

    region = (
        args.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )

    ddb = boto3.resource("dynamodb", region_name=region)
    secrets = boto3.client("secretsmanager", region_name=region)
    registry = GatewayRegistry(
        ddb_table=ddb.Table(table_name),
        secrets_client=secrets,
        project_name=project_name,
    )

    logger.info(
        "Migrating %d gateway entries → table=%s, project=%s, dry_run=%s",
        len(entries),
        table_name,
        project_name,
        args.dry_run,
    )

    counts: dict[str, int] = {
        "migrated": 0,
        "skipped": 0,
        "missing_secret": 0,
        "error": 0,
    }
    for entry in entries:
        status = await _migrate_one(registry, entry, dry_run=args.dry_run)
        counts[status] = counts.get(status, 0) + 1

    summary: dict[str, Any] = {
        "table": table_name,
        "projectName": project_name,
        "dryRun": args.dry_run,
        "totals": {"scanned": len(entries), **counts},
    }
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")

    return 0 if counts["error"] == 0 and counts["missing_secret"] == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate the legacy GATEWAYS env var into the gateway registry. "
            "See module docstring for full usage examples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gateways-json",
        default=None,
        help="Literal JSON payload (overrides $GATEWAYS and --gateways-file).",
    )
    parser.add_argument(
        "--gateways-file",
        default=None,
        help="Path to a file containing the legacy GATEWAYS JSON.",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Gateway registry DDB table name (defaults to $GATEWAY_REGISTRY_TABLE).",
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help=(
            "Project slug used to build the Secrets Manager path "
            "/<project>/gateway/<id>/secret (defaults to $PROJECT_NAME)."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region for boto3 clients (defaults to $AWS_REGION).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse and validate the payload without writing to DDB or "
            "Secrets Manager. Existing-row checks still run."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
