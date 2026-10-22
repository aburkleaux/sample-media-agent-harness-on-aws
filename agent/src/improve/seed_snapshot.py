"""Seed the GLOBAL Improve SNAPSHOT into the DynamoDB Improve store.

Throwaway, offline, Admin-credentialed bootstrap for Step 2B. It reads the wire bundle
``{suggestions, components, patterns}`` that ``mine_local --emit-wire`` produced and writes it
ONCE under the reserved ``__system__`` partition (``sk="SNAPSHOT"``), so the authenticated
``GET /improve/data`` proxy route can serve the same bundle to every signed-in actor. The
suggestions are platform/harness-code — identical for every user — which is why one global
item is correct and a per-actor copy would be duplication.

What this does NOT touch: the per-actor dismiss overlay rows (``sk="SUGG#<id>"``). Those are
written by the proxy at runtime from ``POST/DELETE /improve/suggestions/:id/dismiss`` and are
the human-label substrate — the seeder must never clobber them, which is why it writes only
the single ``__system__`` SNAPSHOT item and nothing under any real actor partition.

This script is temporary. The ``improve-worker`` Lambda (plan §6/§9) takes over the SNAPSHOT
write in Step 3; until then this is how the mined bundle gets into the store.

Not part of the Capability API isolation boundary (``test_capability_api_isolation.py``) — it
is a manual admin tool that is never packaged into that image, so importing boto3 at module
scope here is fine (the online ``improve/**`` code stays stdlib-only).

Usage
-----
    cd agent && uv run python -m improve.mine_local \\
        --emit-wire ../tests/evaluation/experiments/improve-wire.json
    cd agent && uv run python -m improve.seed_snapshot \\
        --bundle ../tests/evaluation/experiments/improve-wire.json \\
        --table mediaworks-harness-improve \\
        [--region us-west-2] [--judge-config-hash <hash>] [--code-version <sha>] \\
        [--created-at 2026-08-25T04:00:00Z] [--dry-run]

There is deliberately NO default ``--table``: the operator must name the target so a stray run
cannot land in the wrong account. Point ``megs-staging`` creds at staging; leave prod alone.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from decimal import Decimal
from typing import Optional

try:
    from .dto import withheld_fields
except ImportError:  # pragma: no cover — direct-script fallback, mirrors the package
    from improve.dto import withheld_fields  # type: ignore

# The reserved global partition, and the one place the shared bundle lives. Must match the proxy
# read path in `ui/server/app.ts` (SYSTEM_ACTOR) exactly, or the read finds nothing.
#
# The reservation is NOT self-enforcing, and an earlier note here said it was on the grounds that
# "real Cognito subjects are UUIDs". Measured against the staging conversation memory, 6 of 14
# actor ids are not: four 85-character `EXT_…` external-caller principals, `local-dev-user` and
# `slack-demo`. None of them is `__system__` today, but nothing prevents one, and since the
# improve-worker now writes REAL actor partitions under the same sort key, an actor called
# `__system__` would overwrite the shared bundle for everybody. The worker refuses that write
# explicitly rather than trusting the id space.
SYSTEM_ACTOR = "__system__"
SNAPSHOT_SK = "SNAPSHOT"

# Sentinel matching the rest of the loop: an absent provenance field is UNKNOWN, never "" or 0,
# so the item never claims a value it does not have.
UNKNOWN = "UNKNOWN"


def load_bundle(path: str) -> dict:
    """Read the wire bundle, with floats as ``Decimal`` so DynamoDB accepts them.

    The DynamoDB resource layer rejects Python ``float`` (``TypeError: Float types are not
    supported``); ``parse_float=Decimal`` converts every score (e.g. 0.983) on the way in. The
    three-key presence check fails loud rather than seeding a half-empty bundle the surface
    would then render as "no data".

    It also REFUSES a bundle carrying any field the global item withholds, and refusing is the
    decision rather than scrubbing. This is the THIRD route onto the shared item — a file the
    operator names by hand, assembled by whatever code was current when it was written — and a
    stale one is not a data condition to paper over: rewriting it silently would publish a bundle
    whose other blocks are also from that older shape, and would hide from the operator that the
    file they chose is out of date. Regenerating takes one command, which the message names. The
    two producer paths scrub instead, because they cannot ask anyone.

    The check is `dto.withheld_fields`, so the names live in one place.
    """
    with open(path) as f:
        bundle = json.load(f, parse_float=Decimal)
    missing = [k for k in ("suggestions", "components", "patterns") if k not in bundle]
    if missing:
        raise ValueError(
            f"{path} is not an ImproveData bundle — missing {missing}. "
            "Regenerate it with `mine_local --emit-wire <path>`.")
    withheld = withheld_fields(bundle)
    if withheld:
        raise ValueError(
            f"refusing to seed {path}: it carries {len(withheld)} field(s) the globally-readable "
            f"SNAPSHOT withholds — {withheld}. Every actor reads that item, and these are session "
            f"ids or prose derived from one tenant's session. This bundle was written by older "
            f"producer code; regenerate it with `mine_local --emit-wire <path>` rather than "
            f"seeding it.")
    return bundle


def build_item(
    bundle: dict,
    created_at: str,
    judge_config_hash: str,
    code_version: str,
    actor_id: str = SYSTEM_ACTOR,
) -> dict:
    """One SNAPSHOT item. Provenance sits beside the bundle, not inside it.

    ``actor_id`` defaults to the reserved global partition, which is what this seeder writes. The
    ``improve-worker`` also writes one item per REAL actor, holding that actor's own evidence
    (``dto.actor_bundles``), and passes their id here — same key layout, same provenance fields, one
    builder, so the two items cannot drift about how a bundle is stamped.
    """
    return {
        "actorId": actor_id,
        "sk": SNAPSHOT_SK,
        "bundle": bundle,
        # Provenance: when the bundle was minted and what produced it, so a reader can tell a
        # fresh seed from a stale one and which judge/code version stands behind the numbers.
        "createdAt": created_at,
        "judgeConfigHash": judge_config_hash,
        "codeVersion": code_version,
    }


def _summary(bundle: dict) -> str:
    return (f"suggestions={len(bundle.get('suggestions', []))} "
            f"components={len(bundle.get('components', {}) or {})} "
            f"patterns={len(bundle.get('patterns', {}) or {})}")


def seed(
    bundle_path: str,
    table_name: str,
    region: Optional[str],
    created_at: str,
    judge_config_hash: str,
    code_version: str,
    dry_run: bool,
) -> int:
    bundle = load_bundle(bundle_path)
    item = build_item(bundle, created_at, judge_config_hash, code_version)

    print(f"Improve SNAPSHOT seed")
    print(f"  bundle : {bundle_path} ({_summary(bundle)})")
    print(f"  target : table={table_name} region={region or '(default)'} "
          f"key=(actorId={SYSTEM_ACTOR}, sk={SNAPSHOT_SK})")
    print(f"  stamp  : createdAt={created_at} judgeConfigHash={judge_config_hash} "
          f"codeVersion={code_version}")

    if dry_run:
        print("  DRY RUN — nothing written.")
        return 0

    import boto3  # local import: keeps a --dry-run / --help run creds-free and import-light

    # Show which account this actually lands in — the production-safety guard is the operator
    # reading this line, since the table name alone does not name the account.
    try:
        ident = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"  account: {ident.get('Account')} as {ident.get('Arn')}")
    except Exception as e:  # noqa: BLE001 — identity is advisory, not required to write
        print(f"  account: (could not resolve caller identity: {e})")

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    table.put_item(Item=item)
    print("  WROTE global SNAPSHOT. Per-actor SUGG# overlay rows are untouched.")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="improve.seed_snapshot",
        description="Seed the global Improve SNAPSHOT (mined bundle) into DynamoDB.")
    parser.add_argument("--bundle", required=True, metavar="PATH",
                        help="the ImproveData wire bundle from `mine_local --emit-wire`.")
    parser.add_argument("--table", required=True, metavar="NAME",
                        help="Improve store table name (no default, so a run cannot hit the "
                             "wrong account by accident), e.g. mediaworks-harness-improve.")
    parser.add_argument("--region", default=None,
                        help="AWS region; defaults to the environment/profile default.")
    parser.add_argument("--judge-config-hash", default=UNKNOWN, metavar="HASH",
                        help="provenance: model+rubric+inputSet+threshold+runs hash behind the "
                             "verdicts. Defaults to UNKNOWN rather than a fabricated value.")
    parser.add_argument("--code-version", default=UNKNOWN, metavar="SHA",
                        help="provenance: git sha of the code that mined this bundle. Defaults "
                             "to UNKNOWN.")
    parser.add_argument("--created-at", default=None, metavar="ISO8601",
                        help="provenance stamp; defaults to now (UTC). Pass a fixed value for a "
                             "reproducible seed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the target and item summary without writing or touching AWS.")
    args = parser.parse_args(argv)

    created_at = args.created_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    return seed(
        bundle_path=args.bundle,
        table_name=args.table,
        region=args.region,
        created_at=created_at,
        judge_config_hash=args.judge_config_hash,
        code_version=args.code_version,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
