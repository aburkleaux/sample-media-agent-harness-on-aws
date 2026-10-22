import { describe, it, expect } from 'vitest';
import { supportsEffort } from './reasoningEffort';

// This file pins the SAME support matrix as agent/src/model/factory.py
// (_REASONING_EFFORT_CAPABLE). If the two drift, one of these fails. Verified
// live via Bedrock Converse (us-west-2, 2026-10).

const SUPPORTS = [
  'us.anthropic.claude-opus-4-6',
  'us.anthropic.claude-opus-4-7',
  'global.anthropic.claude-opus-4-8',
  'us.anthropic.claude-opus-5',
  'us.anthropic.claude-opus-5-5',
  'us.anthropic.claude-sonnet-4-6',
  'us.anthropic.claude-sonnet-5',
  'us.anthropic.claude-sonnet-5-5',
  'us.anthropic.claude-haiku-5-5',
];

const DOES_NOT = [
  'us.anthropic.claude-opus-4-5',
  'us.anthropic.claude-sonnet-4-5',
  'us.anthropic.claude-haiku-4-5',
  'us.anthropic.claude-opus-4-1',
  'us.anthropic.claude-3-5-sonnet-20241022-v2:0',
  'us.amazon.nova-pro-v1:0',
];

describe('supportsEffort', () => {
  it('is true for every verified-capable generation', () => {
    for (const id of SUPPORTS) expect(supportsEffort(id), id).toBe(true);
  });

  it('is false for every generation that 400s on the field', () => {
    for (const id of DOES_NOT) expect(supportsEffort(id), id).toBe(false);
  });

  it('is case-insensitive and prefix/suffix agnostic', () => {
    expect(supportsEffort('GLOBAL.ANTHROPIC.CLAUDE-OPUS-4-8')).toBe(true);
    expect(supportsEffort('anthropic.claude-opus-5')).toBe(true);
  });

  it('is false for empty/undefined', () => {
    expect(supportsEffort(undefined)).toBe(false);
    expect(supportsEffort(null)).toBe(false);
    expect(supportsEffort('')).toBe(false);
  });

  it('does not let the 5-entries overmatch the excluded 4-5 ids', () => {
    expect(supportsEffort('us.anthropic.claude-opus-4-5')).toBe(false);
    expect(supportsEffort('us.anthropic.claude-sonnet-4-5')).toBe(false);
  });
});
