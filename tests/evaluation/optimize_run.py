"""Live Phase-5 optimization run: StartRecommendation on the low-scoring
'in one sentence' sessions (GoalSuccessRate = 0.0), targeting Builtin.GoalSuccessRate.

Collects each low-scorer session's spans inline (agentTraces.sessionSpans) so we
avoid multi-log-group config, then polls the recommendation to a terminal state
and prints the recommended system prompt + explanation.

    agent/.venv/bin/python -m tests.evaluation.optimize_run
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import boto3

from .deployment import deployment_region

# Region comes from the CDK deployment config, not a literal here — see
# tests/evaluation/deployment.py for why (a wrong region finds no log group and looks
# like "no low scorers").
REGION = deployment_region()


def _discover_main_agent_id(region: str = REGION) -> str:
    """Find the main agent runtime id rather than pinning one.

    A pinned id silently points at a runtime that no longer exists. This script
    carried one from an earlier deployment, so every span collection looked at a log
    group that was not there and returned nothing to optimize — with no error, just
    an empty result that looks like "no low scorers".
    """
    override = os.environ.get("MAIN_AGENT_ID")
    if override:
        return override
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    for page in client.get_paginator("list_agent_runtimes").paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == "mediaworks_harness_main_agent":
                return (rt.get("agentRuntimeArn") or "").rsplit("/", 1)[-1]
    raise RuntimeError("main agent runtime not found in " + region)


AGENT_ID = _discover_main_agent_id()
LOG_GROUP = f"/aws/bedrock-agentcore/runtimes/{AGENT_ID}-DEFAULT"

# Sessions to optimize against. Supply real low scorers via OPTIMIZE_SESSIONS
# (comma-separated); the eval runner prints them.
LOW_SESSIONS = [s for s in os.environ.get("OPTIMIZE_SESSIONS", "").split(",") if s.strip()]


def _current_system_prompt() -> str:
    """The live orchestrator prompt, read from source.

    Previously a verbatim COPY lived here and had drifted badly — it still described
    plan_and_execute_tool as the default for research tasks and knew nothing of the
    rules added since. Optimizing against a stale baseline produces advice for a
    prompt that is not deployed. Read via ast because building the prompt live needs
    a gateway registry. The static orchestrator text was centralized into
    `agent/src/prompts/orchestrator.py::build_orchestrator_system_prompt` (main's
    `_build_system_prompt` now just injects the runtime catalog/inventory and calls it).
    """
    import ast
    src = Path(__file__).resolve().parents[2] / "agent" / "src" / "prompts" / "orchestrator.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_orchestrator_system_prompt":
            return "".join(
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
    raise RuntimeError("build_orchestrator_system_prompt not found in agent/src/prompts/orchestrator.py")


CURRENT_SYSTEM_PROMPT = _current_system_prompt()


def collect_spans(session_id: str) -> list:
    from bedrock_agentcore.evaluation._agent_span_collector import CloudWatchAgentSpanCollector
    collector = CloudWatchAgentSpanCollector(log_group_name=LOG_GROUP, region=REGION,
                                             max_wait_seconds=30, poll_interval_seconds=3)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    return collector.collect(session_id=session_id, start_time=start, end_time=end)


SPANS_FILE = "/tmp/opt_spans.json"


def main():
    import os
    import sys
    collect_only = "--collect-only" in sys.argv

    if not os.path.exists(SPANS_FILE) or collect_only:
        spans: list = []
        for sid in LOW_SESSIONS:
            s = collect_spans(sid)
            print(f"collected {len(s)} spans for {sid[-20:]}")
            spans.extend(s)
        with open(SPANS_FILE, "w") as f:
            json.dump(spans, f, default=str)
        print(f"wrote {len(spans)} spans to {SPANS_FILE}")
        if collect_only:
            return
    else:
        with open(SPANS_FILE) as f:
            spans = json.load(f)
        print(f"loaded {len(spans)} spans from {SPANS_FILE}")

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    if not hasattr(client, "start_recommendation"):
        print(f"ERROR: boto3 {boto3.__version__} lacks start_recommendation; use a newer boto3.")
        return
    resp = client.start_recommendation(
        name=f"eval-goalsuccess-{uuid.uuid4().hex[:8]}",
        type="SYSTEM_PROMPT_RECOMMENDATION",
        description="Optimize orchestrator prompt for 'answer in one sentence' constraint adherence (GoalSuccessRate=0 low scorers).",
        recommendationConfig={
            "systemPromptRecommendationConfig": {
                "systemPrompt": {"text": CURRENT_SYSTEM_PROMPT},
                "agentTraces": {"sessionSpans": spans},
                "evaluationConfig": {
                    "evaluators": [
                        {"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}
                    ]
                },
            }
        },
        clientToken=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    rec_id = resp["recommendationId"]
    print(f"started recommendation: {rec_id} status={resp.get('status')}")

    terminal = {"COMPLETED", "FAILED", "DELETING"}
    last = resp
    deadline = time.time() + 900
    while time.time() < deadline:
        last = client.get_recommendation(recommendationId=rec_id)
        st = last.get("status")
        print(f"  status={st}")
        if st in terminal:
            break
        time.sleep(15)

    print("=== FINAL ===")
    print(json.dumps({k: v for k, v in last.items() if k not in ("recommendationConfig",)},
                     indent=2, default=str)[:4000])


if __name__ == "__main__":
    main()
