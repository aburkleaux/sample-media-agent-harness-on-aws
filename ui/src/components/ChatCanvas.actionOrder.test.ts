// A timeline action made while a turn is running goes AFTER that turn's reply (staging 2026-10-19: a crop made
// while "Show the Shot Segmentation results" was answering was drawn above the answer).
import { describe, it, expect } from 'vitest';
import { commitTurnReply, splitLateActions } from './ChatCanvas';
import type { ChatMessage } from '../lab-api';

const m = (id: string, role: ChatMessage['role'], timestamp: number): ChatMessage => ({ id, role, content: '', timestamp });

describe('timeline actions during a running turn', () => {
  const prev = [m('t0-u', 'user', 100), m('t0-a', 'assistant', 101), m('a0-x', 'action', 150),
    m('t1-u', 'user', 200), m('a1-x', 'action', 260), m('a2-x', 'action', 270)];

  it('draw below the live reply while it streams', () => {
    const { before, late } = splitLateActions(prev);
    expect(before.map((x) => x.id)).toEqual(['t0-u', 't0-a', 'a0-x', 't1-u']);
    expect(late.map((x) => x.id)).toEqual(['a1-x', 'a2-x']);
  });

  it('the finished reply is committed before them, stamped like the durable copy (question + 1 ms)', () => {
    const next = commitTurnReply(prev, m('t1-a', 'assistant', 999), 't1-u');
    expect(next.map((x) => x.id)).toEqual(['t0-u', 't0-a', 'a0-x', 't1-u', 't1-a', 'a1-x', 'a2-x']);
    expect(next[4].timestamp).toBe(201);
  });

  it('with nothing after the question the reply is simply appended', () => {
    const next = commitTurnReply(prev.slice(0, 4), m('t1-a', 'assistant', 999), 't1-u');
    expect(next.map((x) => x.id)).toEqual(['t0-u', 't0-a', 'a0-x', 't1-u', 't1-a']);
    expect(next[4].timestamp).toBe(999);
  });
});
