#!/usr/bin/env python3
"""Re-import drafts from a synced S3 prefix into the unified AssetIndex.

The legacy lab-artifacts bucket held drafts under:

    <actor_id>/drafts/skills/<assetId>/SKILL.md
    <actor_id>/drafts/specialty-agents/<assetId>/persona.md
    <actor_id>/drafts/tool-collections/<assetId>/collection.json
    <actor_id>/drafts/workflows/<assetId>/workflow.json

A vanilla `aws s3 sync` between the old and new lab-artifacts buckets
preserves the file layout but does NOT recreate the AssetIndex rows
that the platform reads. This script walks the synced S3 tree and writes
one DDB row per draft so the UI can list them again.

Usage:

    python3 agent/scripts/import_drafts.py \\
        --bucket mediaworks-harness-lab-artifacts-100000000004-us-west-2 \\
        --table mediaworks-harness-lab-asset-index \\
        --actor local-dev-user \\
        --profile <profile> --region us-west-2 \\
        --dry-run

Drop --dry-run to actually write rows. The script is idempotent: rows
are written with `attribute_not_exists(PK)` so a second run skips
already-imported drafts and reports them as `skipped`.

Categories handled: skill, specialty-agent, tool-collection, workflow.
Each is gated by detecting its category-specific filename so the
script never blindly creates rows for unexpected S3 keys.

Notes:
* The skill name is parsed from SKILL.md frontmatter (`name:` line).
* Specialty agents have no frontmatter; the dataclass `name` falls
  back to a placeholder if persona.md doesn't carry one.
* Tool collections parse name from collection.json.
* Workflows parse name from workflow.json.

The AssetIndex.put we use validates the canonical s3_prefix grammar
via path_validator.derive_record_prefix; the rendered prefix matches
exactly what the platform writes today for fresh drafts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Make agent/src importable when running from the repo root.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"),
)

import boto3
from botocore.exceptions import ClientError

from lab.asset_index import AssetIndex
from lab.models import (
    Skill,
    SpecialtyAgent,
    ToolCollection,
    ToolRef,
    Workflow,
    WorkflowStep,
    AgentRef,
    SkillRef,
    ToolCollectionRef,
)
from lab.path_validator import derive_record_prefix

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("import_drafts")


# ---------------------------------------------------------------------------
# Frontmatter / payload parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def _parse_skill_name(body: bytes, fallback: str) -> str:
    """Extract `name:` from YAML frontmatter; fall back to first heading
    or the supplied default."""
    text = body.decode("utf-8", errors="replace")
    match = _FRONTMATTER_RE.match(text)
    if match:
        name_match = _NAME_RE.search(match.group(1))
        if name_match:
            return name_match.group(1).strip()
    # Fall back to first ATX heading.
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def _parse_specialty_name(body: bytes, fallback: str) -> str:
    """Specialty personas have no required frontmatter. Try the same
    parse rules as skills; fall back to the asset_id."""
    return _parse_skill_name(body, fallback)


def _parse_tool_collection(body: bytes, fallback: str) -> tuple[str, list[ToolRef]]:
    """Read `{name, tools: [{name, version}]}` from collection.json."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback, []
    name = doc.get("name") or fallback
    refs: list[ToolRef] = []
    for raw in doc.get("tools", []) or []:
        if isinstance(raw, dict) and "name" in raw and "version" in raw:
            refs.append(ToolRef(name=raw["name"], version=raw["version"]))
    return name, refs


def _parse_workflow(body: bytes, fallback: str) -> tuple[str, list[WorkflowStep]]:
    """Read `{name, steps: [...]}` from workflow.json."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback, []
    name = doc.get("name") or fallback
    steps: list[WorkflowStep] = []
    for raw in doc.get("steps", []) or []:
        if not isinstance(raw, dict):
            continue
        agent_data = raw.get("agent") or {}
        skill_data = raw.get("skill") or {}
        tc_data = raw.get("toolCollection") or {}
        steps.append(
            WorkflowStep(
                step_id=raw.get("stepId", ""),
                agent=AgentRef(
                    kind=agent_data.get("kind", "core"),
                    ref=agent_data.get("ref"),
                    asset_id=agent_data.get("assetId"),
                ),
                skill=SkillRef(asset_id=skill_data.get("assetId")),
                tool_collection=ToolCollectionRef(asset_id=tc_data.get("assetId")),
                prompt=raw.get("prompt", ""),
                inputs=list(raw.get("inputs", []) or []),
            )
        )
    return name, steps


# ---------------------------------------------------------------------------
# S3 walk
# ---------------------------------------------------------------------------


@dataclass
class DraftCandidate:
    category: str       # "skill" | "specialty-agent" | "tool-collection" | "workflow"
    asset_id: str
    s3_key: str         # the marker file (SKILL.md / persona.md / etc.)


# Each category is identified by its primary file name + parent folder.
_CATEGORY_BY_FILE: dict[str, tuple[str, str]] = {
    "SKILL.md":         ("skill",           "skills"),
    "persona.md":       ("specialty-agent", "specialty-agents"),
    "collection.json":  ("tool-collection", "tool-collections"),
    "workflow.json":    ("workflow",        "workflows"),
}


def _iter_drafts(s3, bucket: str, actor: str):
    """Yield DraftCandidate per primary file under <actor>/drafts/."""
    prefix = f"{actor}/drafts/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []) or []:
            key = entry["Key"]
            # Expect: <actor>/drafts/<plural>/<asset_id>/<filename>
            parts = key.split("/")
            if len(parts) != 5:
                continue
            _, _, plural, asset_id, fname = parts
            cat = _CATEGORY_BY_FILE.get(fname)
            if cat is None or cat[1] != plural:
                continue
            yield DraftCandidate(
                category=cat[0],
                asset_id=asset_id,
                s3_key=key,
            )


# ---------------------------------------------------------------------------
# Build typed AssetCommon for each category
# ---------------------------------------------------------------------------


def _build_asset(
    s3,
    bucket: str,
    actor: str,
    candidate: DraftCandidate,
) -> object:
    """Read the marker file from S3 and build the typed dataclass."""
    body = s3.get_object(Bucket=bucket, Key=candidate.s3_key)["Body"].read()
    now = int(time.time() * 1000)

    s3_prefix = derive_record_prefix(
        scope="private",
        owner_user_id=actor,
        owner_group_id=None,
        owner_org_id=None,
        category=candidate.category,
        asset_id=candidate.asset_id,
        version=None,
        status="draft",
    )

    if candidate.category == "skill":
        name = _parse_skill_name(body, candidate.asset_id)
        return Skill(
            asset_id=candidate.asset_id,
            category="skill",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
        )

    if candidate.category == "specialty-agent":
        name = _parse_specialty_name(body, candidate.asset_id)
        return SpecialtyAgent(
            asset_id=candidate.asset_id,
            category="specialty-agent",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            compatible_skills=[],
        )

    if candidate.category == "tool-collection":
        name, refs = _parse_tool_collection(body, candidate.asset_id)
        return ToolCollection(
            asset_id=candidate.asset_id,
            category="tool-collection",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            tool_refs=refs,
        )

    if candidate.category == "workflow":
        name, steps = _parse_workflow(body, candidate.asset_id)
        return Workflow(
            asset_id=candidate.asset_id,
            category="workflow",
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            steps=steps,
        )

    raise ValueError(f"unknown category: {candidate.category}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _put_with_skip(asset_index: AssetIndex, asset, *, owner_user_id: str) -> str:
    """Put the asset; return one of {'put', 'skipped'} on outcome."""
    try:
        await asset_index.put(asset, owner_user_id=owner_user_id)
        return "put"
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        # AssetIndex.put already includes attribute_not_exists(PK)
        # in its condition expression for new rows.
        if code == "ConditionalCheckFailedException":
            return "skipped"
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Walk a synced lab-artifacts S3 prefix and write one AssetIndex "
            "row per draft. Idempotent."
        ),
    )
    parser.add_argument("--bucket", required=True, help="Lab artifacts bucket")
    parser.add_argument("--table", required=True, help="Lab AssetIndex DDB table")
    parser.add_argument(
        "--actor",
        default="local-dev-user",
        help="Actor id whose drafts to import (default: local-dev-user)",
    )
    parser.add_argument("--profile", default=None, help="AWS profile")
    parser.add_argument("--region", default="us-west-2", help="AWS region")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk and report; do not write to AssetIndex",
    )
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")

    counts = {
        "skill": {"found": 0, "put": 0, "skipped": 0, "error": 0},
        "specialty-agent": {"found": 0, "put": 0, "skipped": 0, "error": 0},
        "tool-collection": {"found": 0, "put": 0, "skipped": 0, "error": 0},
        "workflow": {"found": 0, "put": 0, "skipped": 0, "error": 0},
    }

    if args.dry_run:
        # Dry-run: just walk and log what we'd do. No AssetIndex client needed.
        for cand in _iter_drafts(s3, args.bucket, args.actor):
            counts[cand.category]["found"] += 1
            try:
                asset = _build_asset(s3, args.bucket, args.actor, cand)
            except Exception as err:
                logger.error(
                    "DRY: failed to build asset cat=%s id=%s err=%r",
                    cand.category, cand.asset_id, err,
                )
                counts[cand.category]["error"] += 1
                continue
            logger.info(
                "DRY: would put cat=%s id=%s name=%r",
                cand.category, cand.asset_id, getattr(asset, "name", "?"),
            )
        _print_summary(counts, dry_run=True)
        return 0

    # Real run.
    table = session.resource("dynamodb").Table(args.table)
    asset_index = AssetIndex(ddb_table=table)

    async def _run() -> None:
        for cand in _iter_drafts(s3, args.bucket, args.actor):
            counts[cand.category]["found"] += 1
            try:
                asset = _build_asset(s3, args.bucket, args.actor, cand)
            except Exception as err:
                logger.error(
                    "build_asset failed cat=%s id=%s err=%r",
                    cand.category, cand.asset_id, err,
                )
                counts[cand.category]["error"] += 1
                continue

            try:
                outcome = await _put_with_skip(
                    asset_index, asset, owner_user_id=args.actor
                )
            except Exception as err:
                logger.error(
                    "put failed cat=%s id=%s err=%r",
                    cand.category, cand.asset_id, err,
                )
                counts[cand.category]["error"] += 1
                continue

            counts[cand.category][outcome] += 1
            logger.info(
                "%s cat=%s id=%s name=%r",
                outcome.upper(), cand.category, cand.asset_id,
                getattr(asset, "name", "?"),
            )

    asyncio.run(_run())
    _print_summary(counts, dry_run=False)
    return 0


def _print_summary(counts: dict, *, dry_run: bool) -> None:
    label = "DRY-RUN PLAN" if dry_run else "IMPORT SUMMARY"
    print()
    print(f"=== {label} ===")
    for cat in ("skill", "specialty-agent", "tool-collection", "workflow"):
        c = counts[cat]
        line = (
            f"{cat:18s}  found={c['found']:3d}  "
            f"put={c['put']:3d}  skipped={c['skipped']:3d}  errors={c['error']:3d}"
        )
        print(line)


if __name__ == "__main__":
    sys.exit(main())
