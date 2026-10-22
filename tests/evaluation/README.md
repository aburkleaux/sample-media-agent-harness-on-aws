# Agent Evaluation Suite

Multi-turn evaluation, continuous monitoring, and prompt/tool optimization for the
mediaworks-harness agents, built on **AWS Bedrock AgentCore Evaluations**
(`bedrock_agentcore.evaluation`) + **AgentCore Optimization** (`StartRecommendation`).

Spec: .kiro/specs/agent-evaluation-suite.

## Layout

```
tests/evaluation/
├── decode.py            # Memory-event → turns + tool-trajectory decoder
├── harvester.py         # Harvest all of an actor's UI sessions → dataset
├── scenario_builder.py  # Auto-classify + emit scenarios (design dataset schema)
├── loader.py            # Dataset load + schema validation + filtering
├── invoker.py           # agent_invoker (invoke_agent_runtime) + pure SSE parser
├── evaluators.py        # 4 custom evaluators (Manifest/AsyncJobFlow/DagCompletion/SessionContinuity)
├── runner.py            # CLI: run scenarios, custom + built-in evaluators, results, CI gate
├── optimization.py      # OptimizationRunner: StartRecommendation + poll + config bundle
├── optimize_run.py      # Example: run a recommendation on low-scoring sessions
├── bundle_run.py        # Example: package a completed recommendation into a bundle
├── online_eval_setup.py # Manual enable/describe of continuous online evaluation (CDK is canonical)
├── uat/                 # UX invariants + goal-loop runbook for the Playwright-MCP UAT loop
├── datasets/harvested/  # Harvested scenarios (generated)
├── bundles/             # Config bundles from accepted recommendations
├── results/             # Eval run outputs (gitignored)
└── test_*.py            # Unit tests (run: agent/.venv/bin/python -m pytest tests/evaluation/)
```

Run from the repo root with the agent venv (has boto3 + `bedrock_agentcore`):
`agent/.venv/bin/python -m pytest tests/evaluation/` (unit tests, no AWS).

## 1. Harvest real sessions into a dataset

```bash
set -a; . ./.env; set +a   # MEMORY_ID, AGENT_RUNTIME_ARN
agent/.venv/bin/python -m tests.evaluation.harvester \
  --actor local-dev-user --region us-east-1
# → tests/evaluation/datasets/harvested/harvested.json (auto-classified by category)
```

## 2. Run the evaluation suite

```bash
# Validate the dataset without invoking the agent
agent/.venv/bin/python -m tests.evaluation.runner --dry-run

# Run a category live (custom evaluators run locally, always)
agent/.venv/bin/python -m tests.evaluation.runner \
  --category skill_selection --limit 12 \
  --agent-arn "$AGENT_RUNTIME_ARN" --threshold 0

# Also run AgentCore BUILT-IN evaluators (waits ~180s for CloudWatch span ingestion)
agent/.venv/bin/python -m tests.evaluation.runner \
  --category skill_selection --limit 12 --builtin \
  --agent-arn "$AGENT_RUNTIME_ARN" --threshold 0
```

Results are written per-scenario (timestamped dir) with a `summary.json`; the CLI
exits non-zero when the pass rate is below `--threshold` (CI gate).

### Built-in evaluators — multi-agent caveat
Built-in evaluators (`Builtin.GoalSuccessRate/Helpfulness/Correctness`) are
**session-level** and require every span's log event in the configured log group.
**Single-agent** turns score cleanly. Sessions that dispatch to the **render/task
sub-agents** return `LogEventMissingException` because those runtimes' log events
live in *other* log groups. Custom evaluators (which score `agent_output`) are
unaffected. To score multi-agent sessions, add the sub-agent log groups to the
data source (up to 5).

## 3. Continuous (online) evaluation + monitoring

Provisioned in the **CDK stack** (`infra/lib/agent-infra-stack.ts` →
`OnlineEvaluationConfig` + `OnlineEvaluationRole`), so it is reproducible:

- Continuously samples live main-agent traffic (100%) and scores it with the three
  built-in evaluators.
- Results → CloudWatch log group `/aws/bedrock-agentcore/evaluations/results/<config-id>`.
- Per-evaluator scores → EMF metrics under the **`Bedrock-AgentCore/Evaluations`**
  namespace (keyed by evaluator + config id) → build dashboards & alarms.

Manual toggle / inspection (CDK is the source of truth):
```bash
agent/.venv/bin/python -m tests.evaluation.online_eval_setup --describe
# pause/resume: `agentcore pause online-eval <name>` / `resume`
```

## 4. Optimization loop

See [OPTIMIZATION.md](OPTIMIZATION.md) — generate a recommendation from low-scoring
traces, review it, bundle it, and validate before deploying.

## UX robustness goal-loop (Playwright MCP)

`uat/invariants.json` + `uat/GOAL_LOOP.md` define the autonomous UI UAT loop; the
committed deterministic checks live in `ui/tests/robustness-uat.spec.ts` and the
pure logic in `ui/src/services/renderOrchestration.ts` (+ tests).
