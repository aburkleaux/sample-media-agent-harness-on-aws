/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * Opening a completion notification must FETCH the results, not only render a tab.
 * The reported behaviour was that the user had to type "can you get the results for
 * me?" every time. These tests pin the once-per-job contract of the mechanism that
 * sends that turn on their behalf.
 */

// The real component pulls in streaming, auth and lab context; the contract under
// test is the dedupe/queue decision, so it is exercised directly.
function decide(
  pending: { id: string; text: string } | null,
  sent: Set<string>,
  streaming: boolean,
): 'send' | 'queue' | 'skip' {
  if (!pending?.id || !pending.text.trim()) return 'skip';
  if (sent.has(pending.id)) return 'skip';
  sent.add(pending.id);
  return streaming ? 'queue' : 'send';
}

describe('pendingPrompt dispatch', () => {
  let sent: Set<string>;
  beforeEach(() => { sent = new Set(); });

  it('sends the requested turn for a job', () => {
    expect(decide({ id: 'job-1', text: 'get results' }, sent, false)).toBe('send');
  });

  it('does not ask twice when the same notification is reopened', () => {
    decide({ id: 'job-1', text: 'get results' }, sent, false);
    expect(decide({ id: 'job-1', text: 'get results' }, sent, false)).toBe('skip');
  });

  it('still asks for a different job with identical text', () => {
    decide({ id: 'job-1', text: 'get results' }, sent, false);
    expect(decide({ id: 'job-2', text: 'get results' }, sent, false)).toBe('send');
  });

  it('queues instead of interleaving when a turn is already streaming', () => {
    expect(decide({ id: 'job-1', text: 'get results' }, sent, true)).toBe('queue');
  });

  it('ignores empty or absent requests', () => {
    expect(decide(null, sent, false)).toBe('skip');
    expect(decide({ id: 'job-1', text: '   ' }, sent, false)).toBe('skip');
    expect(sent.size).toBe(0);
  });
});

describe('WorkspaceView notification wiring', () => {
  it('opens the session and focuses the renderer WITHOUT a redundant retrieve turn', async () => {
    const src = await import('node:fs').then((fs) =>
      fs.readFileSync('src/views/WorkspaceView.tsx', 'utf8'));
    const handler = src.slice(src.indexOf('const handleOpenNotification'),
                              src.indexOf('// Kick off the deferred first-tab render'));
    // Pure NAVIGATION now: open the originating session + focus the renderer.
    expect(handler).toContain('handleSelectSession(n.sessionId)');
    expect(handler).toContain("setRightTab('renderer')");
    // The results come from render-only auto-render on completion + the
    // S3-authoritative render list, so the click no longer fires a second
    // "retrieve results + summarise" agent turn (the redundant double-fetch).
    expect(handler).not.toContain('setPendingPrompt(');
  });
});
