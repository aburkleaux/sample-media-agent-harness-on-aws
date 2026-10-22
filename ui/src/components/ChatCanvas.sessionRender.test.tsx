/**
 * @vitest-environment jsdom
 *
 * Cross-session routing for the single, persisted ChatCanvas instance.
 *
 * WorkspaceView mounts ONE <ChatCanvas> with no `key`, so the instance survives
 * session switches and its SSE stream keeps running. That means a turn started
 * in session A can still emit events (text, render) AFTER the user has switched
 * to session B. These tests pin the fix that addresses every such event to the
 * session that ORIGINATED the turn (captured at dispatch time), never the one
 * that happens to be on screen when the event lands:
 *
 *   (a) the final assistant message of a turn is written to the originating
 *       session's cache, not the active one;
 *   (b) switching away and back does NOT abort the in-flight stream;
 *   (c) a render block is addressed to the originating session id — both while
 *       that session is active and after the user has switched away.
 *
 * The streaming call is mocked so the test controls exactly when (and with
 * what events) a dispatch progresses, mimicking a slow turn in another session.
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
}));
vi.mock('../services/agent-service', () => ({ AgentService: { upload: mocks.upload } }));
vi.mock('./chat/JobReference', () => ({ default: () => null }));

import ChatCanvas from './ChatCanvas';

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

function deferred<T = void>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setSession(id: string) {
  mocks.useLabContext.mockReturnValue({ sessionId: id, refreshCategory: vi.fn() });
}

function typeAndSubmit(text: string) {
  const textarea = screen.getByLabelText('Chat message input');
  fireEvent.change(textarea, { target: { value: text } });
  fireEvent.keyDown(textarea, { key: 'Enter' });
}

// Capture the (callback, signal) each dispatch was made with so the test can
// drive events onto a still-in-flight stream at will.
interface Captured { req: { session_id?: string; prompt: string }; cb: StreamCallback; signal?: AbortSignal }

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));
});

describe('ChatCanvas — render entries routed by originating session', () => {
  it('(c) addresses a render block to the session that originated the turn while it is active', async () => {
    let captured: Captured | null = null;
    mocks.invokeAgentStreaming.mockImplementation((req, cb, signal) => {
      captured = { req, cb, signal };
      return new Promise<void>(() => {});
    });
    const onRenderBlock = vi.fn();
    setSession('A');
    render(<ChatCanvas onRenderBlock={onRenderBlock} />);

    typeAndSubmit('make an image');
    await flush();
    expect(captured).not.toBeNull();

    await act(async () => {
      captured!.cb({ type: 'render', manifestKey: 'artifacts/x/render/m1.json' });
    });

    expect(onRenderBlock).toHaveBeenCalledTimes(1);
    const [, originatingSessionId] = onRenderBlock.mock.calls[0];
    expect(originatingSessionId).toBe('A');
  });

  it('(c) a render arriving AFTER the user switched away is still addressed to the originating session', async () => {
    let captured: Captured | null = null;
    mocks.invokeAgentStreaming.mockImplementation((req, cb, signal) => {
      captured = { req, cb, signal };
      return new Promise<void>(() => {}); // stays in flight across the switch
    });
    const onRenderBlock = vi.fn();

    setSession('A');
    const { rerender } = render(<ChatCanvas onRenderBlock={onRenderBlock} />);
    typeAndSubmit('render for A');
    await flush();

    // User switches to a different session while A's turn is still streaming.
    setSession('B');
    rerender(<ChatCanvas onRenderBlock={onRenderBlock} />);
    await flush();

    // A's stream now emits its render — the user is looking at B.
    await act(async () => {
      captured!.cb({ type: 'render', manifestKey: 'artifacts/x/render/m2.json' });
    });

    expect(onRenderBlock).toHaveBeenCalledTimes(1);
    const [, originatingSessionId] = onRenderBlock.mock.calls[0];
    expect(originatingSessionId).toBe('A'); // NOT 'B'
  });
});

describe('ChatCanvas — in-flight stream survives session switch', () => {
  it('(b) does not abort the stream when the user switches away and back', async () => {
    let captured: Captured | null = null;
    mocks.invokeAgentStreaming.mockImplementation((req, cb, signal) => {
      captured = { req, cb, signal };
      return new Promise<void>(() => {});
    });

    setSession('A');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('long running A');
    await flush();

    const signal = captured!.signal;
    expect(signal).toBeDefined();
    expect(signal!.aborted).toBe(false);

    // A → B → A on the same persisted instance.
    setSession('B');
    rerender(<ChatCanvas />);
    await flush();
    setSession('A');
    rerender(<ChatCanvas />);
    await flush();

    // The original dispatch is untouched: not aborted, not re-issued.
    expect(signal!.aborted).toBe(false);
    expect(mocks.invokeAgentStreaming).toHaveBeenCalledTimes(1);
  });
});

describe('ChatCanvas — streamed result routed by originating session', () => {
  it('(a) writes the final assistant message to the ORIGINATING session cache after switching away', async () => {
    const dA = deferred();
    let captured: Captured | null = null;
    mocks.invokeAgentStreaming.mockImplementation((req, cb, signal) => {
      captured = { req, cb, signal };
      return dA.promise;
    });

    setSession('A');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('question in A');
    await flush();

    // Stream A's answer text (still on A).
    await act(async () => { captured!.cb({ type: 'text', text: 'answer for A' }); });

    // Switch to B before A finishes.
    setSession('B');
    rerender(<ChatCanvas />);
    await flush();

    // A completes while B is on screen.
    await act(async () => { dA.resolve(); });
    await flush();

    // The completed turn is filed under A, not B.
    const aCache = JSON.parse(localStorage.getItem('lab-session-A') || '[]') as { role: string; content: string }[];
    expect(aCache.some(m => m.role === 'assistant' && m.content.includes('answer for A'))).toBe(true);

    // B (on screen) never shows A's answer.
    expect(screen.queryByText(/answer for A/)).toBeNull();
  });
});
