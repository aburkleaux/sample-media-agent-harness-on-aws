// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { selectedReasoningEffort } from './api';

describe('selectedReasoningEffort', () => {
  beforeEach(() => localStorage.clear());

  it('returns undefined (Default) when unset or empty', () => {
    expect(selectedReasoningEffort()).toBeUndefined();
    localStorage.setItem('lab-orchestrator-model-effort', '');
    expect(selectedReasoningEffort()).toBeUndefined();
  });

  it('returns the enum value when set', () => {
    for (const v of ['low', 'medium', 'high'] as const) {
      localStorage.setItem('lab-orchestrator-model-effort', v);
      expect(selectedReasoningEffort()).toBe(v);
    }
  });

  it('returns undefined for any out-of-enum value', () => {
    for (const bad of ['xhigh', 'max', 'LOW', '2', 'none']) {
      localStorage.setItem('lab-orchestrator-model-effort', bad);
      expect(selectedReasoningEffort()).toBeUndefined();
    }
  });
});
