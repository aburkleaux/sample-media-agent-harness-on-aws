// The current-model card read "Other · global.openai.gpt-6.1-sol" (staging 2026-10-11): no OpenAI entry.
import { describe, it, expect } from 'vitest';
import { extractProvider } from './ModelSettings';

describe('extractProvider', () => {
  it('names OpenAI models', () => {
    expect(extractProvider('Global OpenAI GPT-6.1 Sol')).toBe('OpenAI');
    expect(extractProvider('US OpenAI GPT-5.5')).toBe('OpenAI');
  });
  it('keeps the others', () => {
    expect(extractProvider('Global Anthropic Claude Opus 4.8')).toBe('Anthropic');
    expect(extractProvider('US Meta Llama 3.2 11B Instruct')).toBe('Meta');
    expect(extractProvider('US DeepSeek-R1')).toBe('Other');
  });
});
