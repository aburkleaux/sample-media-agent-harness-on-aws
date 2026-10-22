// A finished turn's steps never read as still working (staging 2026-10-19: every completed search turn's header said
// "8 steps · Calling search_content..." with the live dot, because a tool's start step stays `running`).
import { describe, it, expect } from 'vitest';
import { settleSteps } from './ChatCanvas';
import type { StepEvent } from '../lab-api';

const step = (label: string, status: StepEvent['status'], node_id = label): StepEvent => ({ label, status, node_id } as StepEvent);

describe('settleSteps', () => {
  it("drops a tool's start step when its finished twin is there", () => {
    const out = settleSteps([step('Using project_status...', 'running', 'a'), step('Using project_status', 'done', 'b'),
      step('Using extract_frames…', 'running', 'c'), step('Using extract_frames', 'completed', 'd')]);
    expect(out.map((s) => s.label)).toEqual(['Using project_status', 'Using extract_frames']);
  });

  it('shows any other leftover running step as pending, so nothing is running', () => {
    const out = settleSteps([step('Calling search_content...', 'running'), step('Ready', 'done')]);
    expect(out.map((s) => [s.label, s.status])).toEqual([['Calling search_content...', 'pending'], ['Ready', 'done']]);
    expect(out.some((s) => s.status === 'running')).toBe(false);
  });

  it('leaves finished steps alone', () => {
    const steps = [step('Ready', 'done'), step('x', 'failed')];
    expect(settleSteps(steps)).toEqual(steps);
  });
});
