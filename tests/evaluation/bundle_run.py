"""Fetch the completed recommendation and package it into a versioned config
bundle (Phase 5 T18).

    agent/.venv/bin/python -m tests.evaluation.bundle_run <recommendation_id>
"""
import json
import sys
from pathlib import Path

import boto3

from .deployment import deployment_region

from tests.evaluation.optimization import OptimizationRunner

REGION = deployment_region()


def main():
    rec_id = sys.argv[1] if len(sys.argv) > 1 else "eval-goalsuccess-534a6d50-7D89848CA5"
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    rec = client.get_recommendation(recommendationId=rec_id)
    print(f"recommendation {rec_id} status={rec.get('status')}")

    runner = OptimizationRunner(region=REGION)
    bundle = runner.create_config_bundle("v2-format-constraint-adherence", rec, version=2)

    out = Path("tests/evaluation/bundles") / f"{bundle.bundle_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(bundle.to_json())
    print(f"wrote bundle {bundle.bundle_id} -> {out}")
    print(f"system_prompt present: {bool(bundle.system_prompt)} ({len(bundle.system_prompt or '')} chars)")


if __name__ == "__main__":
    main()
