import { describe, it, expect } from 'vitest';
import { cleanupDependencies } from './WorkflowEditor';
import type { WorkflowStep } from '../lab-api';

function makeStep(stepId: string, inputs: string[] = []): WorkflowStep {
  return {
    stepId,
    agent: { kind: 'core' },
    skill: { assetId: null },
    toolCollection: { assetId: null },
    prompt: '',
    inputs,
  };
}

describe('cleanupDependencies', () => {
  it('returns empty array for empty input', () => {
    expect(cleanupDependencies([])).toEqual([]);
  });

  it('leaves valid dependencies intact', () => {
    const steps = [
      makeStep('a'),
      makeStep('b', ['a']),
      makeStep('c', ['a', 'b']),
    ];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]);
    expect(result[1].inputs).toEqual(['a']);
    expect(result[2].inputs).toEqual(['a', 'b']);
  });

  it('removes forward references (step referencing a later step)', () => {
    const steps = [
      makeStep('a', ['b']), // 'b' is at index 1, not earlier
      makeStep('b'),
      makeStep('c', ['a']),
    ];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]);
    expect(result[2].inputs).toEqual(['a']);
  });

  it('removes self-references', () => {
    const steps = [
      makeStep('a'),
      makeStep('b', ['b']), // self-reference
    ];
    const result = cleanupDependencies(steps);
    expect(result[1].inputs).toEqual([]);
  });

  it('removes references to non-existent steps', () => {
    const steps = [
      makeStep('a'),
      makeStep('b', ['a', 'nonexistent']),
    ];
    const result = cleanupDependencies(steps);
    expect(result[1].inputs).toEqual(['a']);
  });

  it('handles reorder scenario: step moved after its dependent', () => {
    // Simulates: step 'b' was moved before step 'a'
    // 'b' references 'a' but 'a' is now after 'b'
    const steps = [
      makeStep('b', ['a']),
      makeStep('a'),
      makeStep('c', ['a', 'b']),
    ];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]); // 'a' is not earlier than 'b'
    expect(result[2].inputs).toEqual(['a', 'b']); // both are earlier than 'c'
  });

  it('preserves steps without inputs unchanged', () => {
    const steps = [
      makeStep('a'),
      makeStep('b'),
      makeStep('c'),
    ];
    const result = cleanupDependencies(steps);
    expect(result).toEqual(steps);
  });

  it('handles single step with no dependencies', () => {
    const steps = [makeStep('only')];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]);
  });

  it('handles single step with invalid self-reference', () => {
    const steps = [makeStep('only', ['only'])];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]);
  });

  it('cleans up after removal scenario (dangling reference already removed from list)', () => {
    // After removing step 'b', remaining steps may still reference it
    // But since 'b' is no longer in the list, its stepId won't be in earlierStepIds
    const steps = [
      makeStep('a'),
      makeStep('c', ['a', 'b']), // 'b' was removed from the list
    ];
    const result = cleanupDependencies(steps);
    expect(result[1].inputs).toEqual(['a']);
  });

  it('does not mutate original steps array', () => {
    const steps = [
      makeStep('a'),
      makeStep('b', ['c']), // invalid forward ref
      makeStep('c'),
    ];
    const originalInputs = [...steps[1].inputs];
    cleanupDependencies(steps);
    expect(steps[1].inputs).toEqual(originalInputs);
  });

  it('handles complex reorder with multiple invalid references', () => {
    // Original order: a, b, c, d
    // After reorder: d, c, b, a
    const steps = [
      makeStep('d', ['a', 'b', 'c']), // all are now after 'd'
      makeStep('c', ['a', 'b']),       // 'a' and 'b' are after 'c'
      makeStep('b', ['a']),            // 'a' is after 'b'
      makeStep('a'),
    ];
    const result = cleanupDependencies(steps);
    expect(result[0].inputs).toEqual([]);
    expect(result[1].inputs).toEqual([]);
    expect(result[2].inputs).toEqual([]);
    expect(result[3].inputs).toEqual([]);
  });
});
