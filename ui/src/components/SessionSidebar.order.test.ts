import { describe, it, expect } from 'vitest';
import { orderSessions } from './SessionSidebar';

/**
 * The sidebar shows a FLAT list ordered by creation time (newest first). Date
 * bucketing (Today/Yesterday/Older) was removed. Ordering is derived from
 * `timestamp` here because in-place updates (preview/messageCount) don't re-sort
 * the array — and activity must NOT reorder a session (clicking/messaging a chat
 * must never move it under the user).
 */
describe('orderSessions', () => {
  const s = (id: string, timestamp: number) =>
    ({ id, title: id, timestamp } as unknown as Parameters<typeof orderSessions>[0][number]);

  it('orders newest-created first regardless of input order', () => {
    const now = Date.now();
    const out = orderSessions([
      s('a', now - 3000),
      s('c', now), // newest
      s('b', now - 1500),
    ]);
    expect(out.map((x) => x.id)).toEqual(['c', 'b', 'a']);
  });

  it('is stable by creation time — a later-created session leads regardless of activity', () => {
    const now = Date.now();
    const dayAgo = now - 86400000 * 2;
    // The session created more recently leads; the older one never jumps ahead
    // just because it was interacted with (its timestamp is its creation time).
    const out = orderSessions([s('newer', now), s('older', dayAgo)]);
    expect(out.map((x) => x.id)).toEqual(['newer', 'older']);
  });
});
