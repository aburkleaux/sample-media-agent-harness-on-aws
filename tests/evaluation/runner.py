"""Evaluation runner — orchestrates scenario execution + evaluation.

Core (`run_dataset`) is pure orchestration with an injected ``invoker_fn`` so it
is unit-testable without a live agent. The CLI (`main`) wires the real
``agent_invoker`` and (best-effort) the AgentCore ``EvaluationClient`` for
built-in evaluators, runs the custom evaluators locally, writes timestamped
results, and exits non-zero below a pass-rate threshold (CI gate).
"""
from __future__ import annotations

import argparse
import json
import os

from .deployment import deployment_region
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tests.evaluation.evaluators import CUSTOM_EVALUATORS, run_custom_evaluators
from tests.evaluation.invoker import TurnOutput
from tests.evaluation.loader import load_dataset, filter_scenarios


def make_session_id(scenario_id: str) -> str:
    """Stable-per-scenario, unique-per-run session id, padded >= 33 chars
    (AgentCore rejects shorter runtimeSessionId)."""
    base = f"eval-{scenario_id}-{int(time.time() * 1000)}"
    return base if len(base) >= 33 else (base + "-" + "0" * (33 - len(base)))


def split_evaluators(names: list[str]) -> tuple[list[str], list[str]]:
    """Return (builtin_ids, custom_names)."""
    builtins = [n for n in names if n.startswith("Builtin.")]
    customs = [n for n in names if n in CUSTOM_EVALUATORS]
    return builtins, customs


def run_scenario(scenario: dict, invoker_fn: Callable[[str, str], TurnOutput],
                 manifest_fetch: Callable[[str], dict] | None = None) -> dict:
    """Execute a scenario's turns and run its CUSTOM evaluators. Returns a report."""
    sid = make_session_id(scenario["scenario_id"])
    turns_out: list[TurnOutput] = []
    turns: list[dict] = []
    for turn in scenario.get("turns", []):
        try:
            out = invoker_fn(turn["input"], sid)
        except Exception as e:  # Requirement 19: record error, keep going
            out = TurnOutput(response_text=f"ERROR: {e}")
        turns_out.append(out)
        turns.append({"input": turn["input"], "response_text": out.response_text})

    manifest_keys = [k for o in turns_out for k in o.manifest_keys]
    manifests: list[dict] = []
    if manifest_fetch:
        for k in manifest_keys:
            try:
                m = manifest_fetch(k)
                if m:
                    manifests.append(m)
            except Exception:
                pass

    result = {
        "turns": turns,
        "manifests": manifests,
        "manifest_keys": manifest_keys,
        "trajectory": scenario.get("_observed_trajectory", []),
        "plan": None,
        "response_text": turns[-1]["response_text"] if turns else "",
    }

    _builtins, customs = split_evaluators(scenario.get("evaluators", []))
    custom_results = run_custom_evaluators(customs, result)
    overall_pass = all(c.passed for c in custom_results) if custom_results else True

    return {
        "scenario_id": scenario["scenario_id"],
        "category": scenario.get("category"),
        "session_id": sid,
        "overall_pass": overall_pass,
        "custom_results": [asdict(c) for c in custom_results],
        "builtin_scores": {},  # filled by the live CLI path via EvaluationClient
        "manifest_keys": manifest_keys,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def run_dataset(scenarios: list[dict], invoker_fn: Callable[[str, str], TurnOutput],
                manifest_fetch: Callable[[str], dict] | None = None,
                on_report: Callable[[dict], None] | None = None) -> list[dict]:
    reports: list[dict] = []
    for sc in scenarios:
        report = run_scenario(sc, invoker_fn, manifest_fetch)
        reports.append(report)
        if on_report:
            on_report(report)  # incremental persistence (Requirement 11.3)
    return reports


BUILTIN_EVALUATOR_IDS = ["Builtin.Correctness", "Builtin.Helpfulness", "Builtin.GoalSuccessRate"]


def agent_id_from_arn(arn: str) -> str:
    """Extract the runtime id (used to derive the CloudWatch log group)."""
    return arn.rstrip("/").split("/")[-1]


def attach_builtin_scores(reports: list[dict], agent_id: str, region: str,
                          wait_s: int = 180, evaluator_ids: list[str] | None = None,
                          on_update=None) -> None:
    """After scenarios are invoked, wait for CloudWatch span ingestion, then run
    AgentCore built-in evaluators per session and merge scores into each report.

    NOTE: built-in evaluators are SESSION-level and need every span's log event
    in the configured log group. Multi-agent sessions that dispatch to sub-agent
    runtimes (render/task) put some log events in OTHER log groups → those score
    with errorCode LogEventMissingException. Single-agent sessions score cleanly.
    Errors are recorded per evaluator (not raised)."""
    evaluator_ids = evaluator_ids or BUILTIN_EVALUATOR_IDS
    from bedrock_agentcore.evaluation import EvaluationClient
    client = EvaluationClient(region_name=region)
    if wait_s > 0:
        print(f"Waiting {wait_s}s for CloudWatch span ingestion before built-in evaluation…")
        time.sleep(wait_s)
    for report in reports:
        sid = report.get("session_id")
        scores: dict = {}
        errors: dict = {}
        for ev in evaluator_ids:
            try:
                results = client.run(evaluator_ids=[ev], session_id=sid, agent_id=agent_id)
                if not results:
                    errors[ev] = "NO_RESULTS"
                    continue
                r = results[0]
                if r.get("errorCode"):
                    errors[ev] = r["errorCode"]
                elif r.get("value") is not None:
                    scores[ev] = r["value"]
            except Exception as e:
                errors[ev] = f"{type(e).__name__}"
        report["builtin_scores"] = scores
        if errors:
            report["builtin_errors"] = errors
        if on_update:
            on_update(report)


def summarize(reports: list[dict]) -> dict:
    total = len(reports)
    passed = sum(1 for r in reports if r.get("overall_pass"))
    # Average each built-in evaluator across reports that have a numeric score.
    avg_scores: dict = {}
    sums: dict = {}
    counts: dict = {}
    for r in reports:
        for ev, val in (r.get("builtin_scores") or {}).items():
            if isinstance(val, (int, float)):
                sums[ev] = sums.get(ev, 0.0) + val
                counts[ev] = counts.get(ev, 0) + 1
    for ev in sums:
        avg_scores[ev] = sums[ev] / counts[ev]
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": (passed / total) if total else 0.0,
        "avg_scores": avg_scores,
        "builtin_scored": counts,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the agent evaluation suite")
    ap.add_argument("--dataset", default="tests/evaluation/datasets/harvested/harvested.json")
    ap.add_argument("--category")
    ap.add_argument("--scenario")
    ap.add_argument("--mode")
    ap.add_argument("--output", default="tests/evaluation/results")
    ap.add_argument("--agent-arn", default=os.environ.get("AGENT_RUNTIME_ARN", ""))
    ap.add_argument("--region", default=deployment_region())
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--limit", type=int, default=0, help="Cap number of scenarios (0 = no cap)")
    ap.add_argument("--builtin", action="store_true", help="Also run AgentCore built-in evaluators (needs span ingestion wait)")
    ap.add_argument("--builtin-wait", type=int, default=180, help="Seconds to wait for CloudWatch span ingestion before built-in eval")
    ap.add_argument("--dry-run", action="store_true", help="Validate + load only, do not invoke the agent")
    args = ap.parse_args(argv)

    scenarios, errors = load_dataset(args.dataset)
    for e in errors:
        print(f"[skip] {e['scenario_id']}: {e['errors']}")
    scenarios = filter_scenarios(scenarios, args.category, args.scenario, args.mode)
    if args.limit and args.limit > 0:
        scenarios = scenarios[: args.limit]
    if not scenarios:
        print("No scenarios matched filters.")
        return 2

    print(f"Loaded {len(scenarios)} scenario(s) ({len(errors)} skipped as invalid).")
    if args.dry_run:
        return 0

    if not args.agent_arn:
        ap.error("AGENT_RUNTIME_ARN required to invoke (or pass --dry-run)")

    from tests.evaluation.invoker import agent_invoker
    invoker_fn = lambda prompt, sid: agent_invoker(prompt, sid, args.agent_arn, args.region)  # noqa: E731

    out_dir = Path(args.output) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    def persist(report: dict) -> None:
        (out_dir / f"{report['scenario_id']}.json").write_text(json.dumps(report, indent=2))
        tag = 'PASS' if report['overall_pass'] else 'FAIL'
        bs = report.get('builtin_scores')
        print(f"  {report['scenario_id']}: {tag}" + (f" builtin={bs}" if bs else ""))

    reports = run_dataset(scenarios, invoker_fn, on_report=persist)

    if args.builtin:
        attach_builtin_scores(reports, agent_id_from_arn(args.agent_arn), args.region,
                              wait_s=args.builtin_wait, on_update=persist)

    summary = summarize(reports)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nSummary: {summary['passed']}/{summary['total']} passed ({summary['pass_rate']:.0%})")
    if summary.get("avg_scores"):
        print(f"Built-in avg scores: {json.dumps(summary['avg_scores'], indent=2)}")
    return 0 if summary["pass_rate"] >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
