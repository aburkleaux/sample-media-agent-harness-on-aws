/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { ModelCompare, fmtDuration, fmtTokens, modelLabel } from './ModelCompare';
import type { ModelScores } from '../../improve-api';

const mm = (v: number | 'UNMEASURED', n = 10) => ({ mean: v, measuredOf: n, total: n });
function model(id: string, o: { runs: number; pass: number; fail: number; delivery: number; dispatch: number; dag?: number; tokens?: number }): ModelScores {
  return {
    model: id,
    population: { total: o.runs, pass: o.pass, fail: o.fail, unscored: o.runs - o.pass - o.fail },
    passRate: o.pass / (o.pass + o.fail),
    tiers: { dispatch: mm(o.dispatch), delivery: mm(o.delivery) },
    checks: o.dag === undefined ? {} : { DagCompletionValidator: { pass: 1, fail: 0, notMeasured: 0, rate: o.dag } },
    perRun: { tokens: mm(o.tokens ?? 48200), llmCalls: mm(4), latencyMs: mm(30000) },
  } as ModelScores;
}
const MODELS = [
  model('global.anthropic.claude-opus-5', { runs: 412, pass: 335, fail: 54, delivery: 0.83, dispatch: 0.94, dag: 0.88 }),
  model('global.anthropic.claude-sonnet-5', { runs: 96, pass: 73, fail: 17, delivery: 0.78, dispatch: 0.92, tokens: 31000 }),
  model('openai.gpt-5-5', { runs: 60, pass: 40, fail: 10, delivery: 0.8, dispatch: 0.9 }),
  model('global.anthropic.claude-haiku-5', { runs: 31, pass: 10, fail: 5, delivery: 0.61, dispatch: 0.82 }),
];
const cards = () => screen.getAllByTestId('model-card');

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe('ModelCompare', () => {
  it('formats cost compactly', () => {
    expect(fmtTokens(48200)).toBe('48.2k');
    expect(fmtTokens(1683800)).toBe('1.7M');
    expect(fmtDuration(41000)).toBe('41s');
    expect(fmtDuration(216000)).toBe('3.6m');
    expect(fmtDuration(2343000)).toBe('39m');
    expect(fmtDuration(9000000)).toBe('2.5h');
  });

  it('names models plainly', () => {
    expect(modelLabel('global.anthropic.claude-opus-5')).toBe('Claude Opus 5');
    expect(modelLabel('us.anthropic.claude-sonnet-4-20250514-v1:0')).toBe('Claude Sonnet 4');
    expect(modelLabel('global.anthropic.claude-sonnet-4-5')).toBe('Claude Sonnet 4.5');
    expect(modelLabel('openai.gpt-5-5')).toBe('GPT-5.5');
  });

  it('shows as many cards as fit, the first as the reference, every metric as a bar with its difference from it', () => {
    render(<ModelCompare models={MODELS} window="last 7 days" />);
    expect(cards().map((c) => c.getAttribute('data-model'))).toEqual(MODELS.slice(0, 3).map((m) => m.model)); // 3 fit
    const [ref, sonnet] = cards();
    expect(within(ref).getByText('reference')).toBeInTheDocument();
    expect(within(ref).getByTestId('model-card-pass')).toHaveTextContent('86%');
    // Every metric, same order on every card; a check this model never needed reads "—", not 0%.
    const labels = (c: HTMLElement) => within(c).getAllByTestId('model-card-metric').map((x) => x.getAttribute('data-metric'));
    expect(labels(ref)).toEqual(['delivery', 'dispatch', 'DagCompletionValidator']);
    expect(labels(sonnet)).toEqual(labels(ref));
    const dagOnSonnet = within(sonnet).getAllByTestId('model-card-metric')[2];
    expect(dagOnSonnet).toHaveTextContent('Plan steps all completed—');
    expect(within(sonnet).getAllByTestId('model-card-metric')[0]).toHaveTextContent('Task completed78%−5');
    expect(sonnet).toHaveTextContent('Tokens / run31.0k−36%');
    expect(screen.getByTestId('model-compare-chooser-button')).toHaveTextContent('Models · 3 of 4 shown');
  });

  it('Models picks which show and in what order; the first is the reference, and the choice is remembered', () => {
    const { unmount } = render(<ModelCompare models={MODELS} />);
    fireEvent.click(screen.getByTestId('model-compare-chooser-button'));
    const dialog = screen.getByTestId('model-compare-chooser');
    // Hide Opus, move Haiku to the top.
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Show Claude Opus 5' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move Claude Haiku 5 up' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move Claude Haiku 5 up' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move Claude Haiku 5 up' }));
    expect(cards().map((c) => c.getAttribute('data-model'))).toEqual([
      'global.anthropic.claude-haiku-5', 'global.anthropic.claude-sonnet-5', 'openai.gpt-5-5']);
    expect(within(cards()[0]).getByText('reference')).toBeInTheDocument();
    expect(cards()[0]).toHaveTextContent('few runs');
    // A tick means on screen: Opus is off (unticked), and the 4th that does not fit is unticked too.
    expect(within(dialog).getByRole('checkbox', { name: 'Show Claude Opus 5' })).not.toBeChecked();
    // Ticking Opus swaps it in for the last card shown.
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Show Claude Opus 5' }));
    expect(cards().map((c) => c.getAttribute('data-model'))).toEqual([
      'global.anthropic.claude-haiku-5', 'global.anthropic.claude-sonnet-5', 'global.anthropic.claude-opus-5']);
    unmount();
    render(<ModelCompare models={MODELS} />);
    expect(cards().map((c) => c.getAttribute('data-model'))).toEqual([
      'global.anthropic.claude-haiku-5', 'global.anthropic.claude-sonnet-5', 'global.anthropic.claude-opus-5']);
  });

  it('adds a row per AgentCore evaluator some shown model has data for, in a fixed order, plainly labelled', () => {
    const [opus, sonnet, gpt, haiku] = MODELS;
    const withEval = [
      { ...opus, agentcore: {
        'Builtin.Helpfulness': mm(0.81, 9), 'Builtin.GoalSuccessRate': mm(0.84, 5),
        'Builtin.ToolSelectionAccuracy': mm(0.9, 3), 'Builtin.Correctness': mm('UNMEASURED', 0),
      } },
      { ...sonnet, agentcore: { 'Builtin.Helpfulness': mm(0.7, 4), 'Builtin.SkillInstructionFollowing': mm(0.5, 2) } },
      gpt,                                                       // old snapshot: no `agentcore` at all
      { ...haiku, agentcore: { 'Builtin.Faithfulness': mm(0.6, 1) } },   // not shown: no row for it
    ] as ModelScores[];
    render(<ModelCompare models={withEval} />);
    const [ref, son, g] = cards();
    const keys = (c: HTMLElement) => within(c).getAllByTestId('model-card-metric').map((x) => x.getAttribute('data-metric'));
    // Correctness is UNMEASURED everywhere and Faithfulness is only on a model not shown: neither gets a row.
    expect(keys(ref)).toEqual(['delivery', 'dispatch', 'DagCompletionValidator',
      'agentcore:Builtin.GoalSuccessRate', 'agentcore:Builtin.Helpfulness',
      'agentcore:Builtin.ToolSelectionAccuracy', 'agentcore:Builtin.SkillInstructionFollowing']);
    expect(keys(g)).toEqual(keys(ref));
    const row = (c: HTMLElement, key: string) =>
      within(c).getAllByTestId('model-card-metric').find((x) => x.getAttribute('data-metric') === key)!;
    expect(row(ref, 'agentcore:Builtin.GoalSuccessRate')).toHaveTextContent('Goal success (AgentCore)84%');
    expect(row(ref, 'agentcore:Builtin.GoalSuccessRate')).toHaveAttribute('title', 'AgentCore scored 5 of 5 runs');
    expect(row(son, 'agentcore:Builtin.Helpfulness')).toHaveTextContent('Helpfulness70%−11');
    expect(row(son, 'agentcore:Builtin.SkillInstructionFollowing')).toHaveTextContent('Followed the skill50%');
    expect(row(ref, 'agentcore:Builtin.SkillInstructionFollowing')).toHaveTextContent('Followed the skill—');
    expect(row(g, 'agentcore:Builtin.ToolSelectionAccuracy')).toHaveTextContent('Tool choice—');
    expect(row(g, 'agentcore:Builtin.ToolSelectionAccuracy')).toHaveAttribute('title', 'AgentCore scored none of these runs');
  });
});
