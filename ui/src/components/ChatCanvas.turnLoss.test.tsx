/**
 * @vitest-environment jsdom
 *
 * Reproduction + regression guard for the "agent responses vanish" bug (sessions 6e32c4de,
 * ec9ce2b6, a6541154): every user turn survived but intermediate assistant replies were dropped.
 *
 * Root cause: the `session-cache-external-update` live-refresh (fired by the background auto-render
 * path when an async job completes) did `setMessages(cachedCopy)` — REPLACING in-memory state with
 * the localStorage cache. When that cache lagged the just-added assistant turn, the refresh wiped
 * it from state, and it then persisted away. This test drives exactly that sequence and asserts the
 * assistant turn SURVIVES. It fails against the old `setMessages(parsed)` and passes with
 * `mergeSessionMessages` (the fix).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import type { StreamCallback } from '../api';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  invokeAgentStreaming: vi.fn(),
  selectedOrchestratorModelId: vi.fn(() => 'model-x'),
  createSession: vi.fn(),
  listMemoryEvents: vi.fn(),
  memoryEventsToChatMessages: vi.fn(() => []),
  updateSessionActivity: vi.fn(),
  getConfig: vi.fn(),
  getSessionTranscript: vi.fn(() => Promise.resolve([] as unknown[])),
  updateSessionTranscript: vi.fn(() => Promise.resolve()),
  logPersistFailure: vi.fn(),
  upload: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../api', () => ({
  invokeAgentStreaming: mocks.invokeAgentStreaming,
  selectedOrchestratorModelId: mocks.selectedOrchestratorModelId,
  selectedReasoningEffort: () => undefined,
}));
vi.mock('../lab-api', () => ({
  createSession: mocks.createSession,
  listMemoryEvents: mocks.listMemoryEvents,
  memoryEventsToChatMessages: mocks.memoryEventsToChatMessages,
  updateSessionActivity: mocks.updateSessionActivity,
  getConfig: mocks.getConfig,
  getSessionTranscript: mocks.getSessionTranscript,
  updateSessionTranscript: mocks.updateSessionTranscript,
  logPersistFailure: mocks.logPersistFailure,
}));
vi.mock('../services/agent-service', () => ({ AgentService: { upload: mocks.upload } }));
vi.mock('./chat/JobReference', () => ({ default: () => null }));

import ChatCanvas from './ChatCanvas';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
}
function typeAndSubmit(text: string) {
  const textarea = screen.getByLabelText('Chat message input');
  fireEvent.change(textarea, { target: { value: text } });
  fireEvent.keyDown(textarea, { key: 'Enter' });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.useLabContext.mockReturnValue({ sessionId: 'A', refreshCategory: vi.fn() });
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  // Each dispatch emits one text chunk then RESOLVES, so the turn finalizes into an assistant msg.
  mocks.invokeAgentStreaming.mockImplementation(async (req: { prompt: string }, cb: StreamCallback) => {
    await Promise.resolve();
    cb({ type: 'text', text: `REPLY::${req.prompt}` } as never);
  });
});

describe('ChatCanvas — background cache-refresh must not drop an assistant turn', () => {
  it('keeps the assistant reply when session-cache-external-update fires with a STALE cache', async () => {
    render(<ChatCanvas />);
    typeAndSubmit('animate the first image');
    await flush();

    // The assistant reply for the turn is on screen.
    expect(screen.getByText(/REPLY::animate the first image/)).toBeInTheDocument();

    // Simulate the background auto-render path: it wrote a STALE session cache (missing the
    // just-added assistant reply — its own write lagged) and fired the refresh event.
    const stale = [{ id: 'u-old', role: 'user', content: 'animate the first image', timestamp: 1 }];
    localStorage.setItem('lab-session-A', JSON.stringify(stale));
    await act(async () => {
      window.dispatchEvent(new CustomEvent('session-cache-external-update', { detail: { sessionId: 'A' } }));
    });
    await flush();

    // REGRESSION ASSERTION: the assistant reply must still be present. With the old
    // setMessages(parsed) it was replaced by the stale cache and this fails; mergeSessionMessages
    // retains it.
    expect(screen.getByText(/REPLY::animate the first image/)).toBeInTheDocument();
  });
});


describe('ChatCanvas — restore must not let a partial cache mask the durable transcript', () => {
  it('merges the complete durable transcript on mount even when localStorage is partial (session 3b59f5b8)', async () => {
    // localStorage holds only the FIRST user turn — a stale/quota-frozen partial cache.
    const partial = [{ id: 'u1', role: 'user', content: 'can you extract the technical metadata', timestamp: 1 }];
    localStorage.setItem('lab-session-A', JSON.stringify(partial));
    // The durable transcript (authoritative) has the full multi-turn conversation.
    mocks.getSessionTranscript.mockResolvedValueOnce([
      { id: 'u1', role: 'user', content: 'can you extract the technical metadata', timestamp: 1 },
      { id: 'a1', role: 'assistant', content: 'DURABLE-ASSISTANT-REPLY', timestamp: 2 },
      { id: 'u2', role: 'user', content: 'DURABLE-SECOND-USER', timestamp: 3 },
    ]);

    render(<ChatCanvas />);
    await flush();

    // REGRESSION: the durable-only turns the partial cache was masking are now on screen.
    // Against the old `if (messages.length > 0) return` the durable load was skipped and these
    // were absent; the merge-on-restore fix restores them.
    expect(screen.getByText(/DURABLE-ASSISTANT-REPLY/)).toBeInTheDocument();
    expect(screen.getByText(/DURABLE-SECOND-USER/)).toBeInTheDocument();
    // The original first turn is retained (not dropped, not duplicated).
    expect(screen.getAllByText(/can you extract the technical metadata/)).toHaveLength(1);
  });
});


describe('ChatCanvas — in-flight turn persistence is now the proxy\'s job', () => {
  it('does not write the durable transcript from the client during or after a turn', async () => {
    // Mid-turn recovery (a long turn's steps surviving a reload) is now provided by the PROXY,
    // which writes throttled provisional snapshots and a final upsert server-side
    // (docs/design-proxy-authoritative-transcript.md). The client performs NO durable write.
    mocks.invokeAgentStreaming.mockImplementationOnce(async (_req: unknown, cb: StreamCallback) => {
      await Promise.resolve();
      cb({ type: 'step', step: { status: 'running', label: 'Running code…', node_id: 'n1' } } as never);
      cb({ type: 'text', text: 'here is your answer' } as never);
    });
    render(<ChatCanvas />);
    typeAndSubmit('generate a wolf');
    await flush();

    expect(mocks.updateSessionTranscript).not.toHaveBeenCalled();
    // The completed turn (with its step group) still renders on screen.
    expect(screen.getAllByText(/here is your answer/)).toHaveLength(1);
  });
});
