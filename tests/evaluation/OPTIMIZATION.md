# Optimization — AgentCore Recommendations

Closes the quality loop: **evaluate → find low scorers → recommend → validate →
bundle → (deploy/A-B) → re-evaluate**, using AgentCore's `StartRecommendation`
(`SYSTEM_PROMPT_RECOMMENDATION` | `TOOL_DESCRIPTION_RECOMMENDATION`).

## First run — results (2026-07)

A broad built-in-evaluator batch over 12 single-agent `skill_selection` scenarios:

| Evaluator | Avg | Low scorers |
|---|---|---|
| Builtin.Correctness | 1.00 | — |
| Builtin.Helpfulness | 0.80 | 3× at 0.67 |
| **Builtin.GoalSuccessRate** | 0.83 | **2× at 0.0** |

Both 0.0 scorers were the prompt **"In one sentence, what can you help me with?"** —
the agent answered with a long enumeration of capabilities instead of one sentence.

A `SYSTEM_PROMPT_RECOMMENDATION` targeting `Builtin.GoalSuccessRate` (inline
`sessionSpans` from the two low-scoring sessions) returned `COMPLETED`. The
optimizer correctly root-caused it — the **CAPABILITY INVENTORY** rule pushed the
agent to enumerate specifics even when the user asked for brevity — and added:

- **GOAL FRAMING:** *"Strictly honor format constraints the user specifies (e.g.,
  'in one sentence'). Match requested length exactly; do not over-elaborate."*
- **CAPABILITY INVENTORY:** *"For brief general questions … answer at the brevity
  requested — do not enumerate specifics."*

The result is bundled at
[`bundles/v2-format-constraint-adherence.json`](bundles/v2-format-constraint-adherence.json).

## How to run

```bash
set -a; . ./.env; set +a

# 1. Generate a recommendation from low-scoring sessions (edit LOW_SESSIONS in the
#    script, or adapt to read them from a results dir).
agent/.venv/bin/python -m tests.evaluation.optimize_run   # → recommendationId, COMPLETED

# 2. Package a completed recommendation into a versioned, immutable config bundle.
agent/.venv/bin/python -m tests.evaluation.bundle_run <recommendation_id>
# → tests/evaluation/bundles/<name>.json  (system_prompt / tool_descriptions)
```

`optimization.py` also exposes `target_low_scorers(reports, evaluator, threshold)`
to select which traces feed a recommendation from an eval run's reports.

## Requirements / notes

- Needs **boto3 ≥ 1.43** (the agent venv was upgraded locally; `requirements.txt`
  is unchanged, so the deployed agent is unaffected).
- `agentTraces` accepts inline `sessionSpans` (used here, avoids the multi-agent
  log-group issue) or `cloudwatchLogs` (log groups + service names + time range).
- Recommendations are LLM-generated — **always review before applying.**

## Validating a recommendation before deploy

The recommended prompt changes agent behavior, so validation is a **deliberate deploy step**:

1. Apply the bundle's `system_prompt` to the agent — edit
   `agent/src/main.py::_build_system_prompt`, or wire the agent to read the bundle.
2. `cd infra && npx cdk deploy` to roll out the new prompt.
3. Re-run the low scorers and confirm the target metric improves:
   ```bash
   agent/.venv/bin/python -m tests.evaluation.runner \
     --scenario harvested-skill-selection-in-one-sentence-what-can-7690b8cb \
     --builtin --agent-arn "$AGENT_RUNTIME_ARN" --threshold 0
   ```
   Expect `Builtin.GoalSuccessRate` to move from 0.0 toward 1.0 with no regression
   on the other scenarios.
4. For higher-confidence rollouts, A/B two config bundles via Gateway traffic
   splitting before full promotion.

Continuous online evaluation (see [README.md](README.md#3-continuous-online-evaluation--monitoring))
keeps scoring live traffic afterward so regressions surface on the
`Bedrock-AgentCore/Evaluations` CloudWatch metrics.
