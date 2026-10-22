# Autonomous UAT Goal-Loop — Driver Runbook (P4.T15)

The goal-loop is an **agentic procedure**: the agent (Kiro) drives the live UI
via the **Playwright MCP** server, observes against the UX invariants
(`invariants.json`), and iterates fix→re-run until all invariants pass or the
iteration budget is exhausted.

## Prerequisite (BLOCKER until done)
The Playwright MCP server is configured in `.kiro/settings/mcp.json` but **MCP
servers load only at kiro-cli session start**. Run `/mcp` — if it shows 0
servers (or no `playwright`), **restart kiro-cli**, then confirm `playwright`
is initialized with browser tools (`browser_navigate`, `browser_snapshot`,
`browser_click`, `browser_type`, `browser_console_messages`, …). The loop
cannot run until these tools are available to the agent.

Also ensure the dev server is up:
```
set -a; . ./.env; set +a; unset LAB_SERVICE_RUNTIME_ARN
cd ui && npm run dev      # localhost:3000 ; re-check: lsof -ti tcp:3000
```

## Inputs
- `tests/evaluation/uat/invariants.json` — the 6 UX invariants + `uat_scenarios`
  (categories to exercise) + loop config (`max_iterations: 10`, autonomous).
- `tests/evaluation/datasets/harvested/harvested.json` — real prompts per
  category to use as scenario inputs.
- `ui/tests/robustness-uat.spec.ts` — the committed deterministic checks
  (2 non-live green; `@live` diarization render is the loop's live check).

## Loop (per iteration)
1. **Select** the next `uat_scenarios` entry (category → pick a representative
   harvested prompt).
2. **Drive** via Playwright MCP: `browser_navigate` localhost:3000 → Dev Login →
   New Session → type the prompt → send → wait for stream completion.
3. **Observe**: `browser_snapshot` (DOM) + `browser_console_messages` (errors) +
   screenshot on completion.
4. **Assert** each mapped invariant (see `invariants.json.signals`):
   - renderer-visibility: drawer open <=> a render block exists.
   - no-inspector-placeholder: no "Select an item..." when clips/cues exist.
   - single-render-per-artifact: one render entry per manifest key.
   - animation-consistent: "Rendering..." shown during compose.
   - drawer-open-compose-render: bubble -> working -> drawer opens -> render (order).
   - no-console-errors: no functional errors / error banners (exclude React
     dev-mode style/key warnings).
5. **Record** failures as structured findings (invariant id, scenario, evidence,
   screenshot path).
6. **Triage -> root-cause -> fix** the code; prefer a pure helper + unit test
   (as in `ui/src/services/renderOrchestration.ts`).
7. **Re-run** the failing scenarios (+ regression: the whole set periodically).
8. **Stop** when: all selected scenarios pass all invariants in one sweep (GOAL),
   or `max_iterations` reached, or an ambiguous UX decision needs the human
   (escalate with the finding + options).

## Safeguards
- Bounded iterations (10); no destructive actions; each fix verified by re-run.
- New bugs' guards are promoted into `renderOrchestration.test.ts` (unit) and/or
  `robustness-uat.spec.ts` (E2E) so they can't regress.

## Current status
- Invariants + checks defined; 2 non-live invariants green; Phase-1 fixes
  (T3-T6) already landed + unit-tested (14 tests), so the loop starts from a
  hardened baseline.
- **Blocked on the MCP restart** to execute the live browser loop (T16).
