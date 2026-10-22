/**
 * @vitest-environment jsdom
 *
 * Reproduction + regression guard for the "switch session mid-stream" turn loss
 * (sessions 09936599, cd95cd42): when the user sends a turn and switches to another
 * session BEFORE the stream terminates, the originating session's assistant turn was
 * lost or left empty — on reload it reconstructed from Memory as fragmented `mem-*`
 * chunks. This test drives that exact sequence with a controllable stream barrier and
 * asserts the ORIGINATING session's durable transcript captures the full assistant turn.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, act } from '@testing-library/react';
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
function typeAndSubmit() {
  const textarea = document.querySelector('textarea[aria-label="Chat message input"]') as HTMLTextAreaElement;
  textarea.value = 'animate the fox';
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  // React controlled input needs the native setter; simpler: use fireEvent via keyDown after change
}

type Msg = { id?: unknown; role?: unknown; content?: unknown; steps?: unknown };

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.useLabContext.mockReturnValue({ sessionId: 'A', refreshCategory: vi.fn() });
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
});

function submitOnCurrent(text: string) {
  const ta = document.querySelector('textarea[aria-label="Chat message input"]') as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
  setter.call(ta, text);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
}

function cachedMessagesFor(sid: string): Msg[] {
  const raw = localStorage.getItem(`lab-session-${sid}`);
  return raw ? (JSON.parse(raw) as Msg[]) : [];
}

describe('ChatCanvas — switch session mid-stream (proxy owns durable persistence)', () => {
  // The originating session's DURABLE transcript is now persisted by the PROXY, which drains the
  // upstream to completion even after the browser switches away (verified in the local harness +
  // server transcriptStore tests). The CLIENT no longer writes the durable transcript; its only
  // remaining off-screen job is to refresh that session's localStorage CACHE so an on-screen tab of
  // it live-updates. These pin the new client contract.
  it('makes NO client durable write for an off-screen turn, and refreshes the originating cache with the full turn', async () => {
    let release!: () => void;
    const barrier = new Promise<void>((r) => { release = r; });
    mocks.invokeAgentStreaming.mockImplementation(async (_req: unknown, cb: StreamCallback) => {
      cb({ type: 'step', step: { status: 'running', label: 'Dispatching…', node_id: 'n1' } } as never);
      await barrier;
      cb({ type: 'text', text: 'FULL-FOX-REPLY' } as never);
    });

    const { rerender } = render(<ChatCanvas />);
    await act(async () => { submitOnCurrent('animate the fox'); });
    await flush();

    // Switch to session B mid-stream (A's stream parked at the barrier).
    mocks.useLabContext.mockReturnValue({ sessionId: 'B', refreshCategory: vi.fn() });
    rerender(<ChatCanvas />);
    await flush();

    // Complete A's stream while it is backgrounded.
    await act(async () => { release(); await Promise.resolve(); });
    await flush();

    // The client never PATCHes the durable transcript — that is the proxy's job now.
    expect(mocks.updateSessionTranscript).not.toHaveBeenCalled();

    // The originating session's CACHE was refreshed with the FULL turn (user + assistant), never
    // truncated to a lone assistant (the e865ed97 invariant, now enforced on the cache write).
    const cachedA = cachedMessagesFor('A');
    expect(cachedA.some((m) => m.role === 'user' && String(m.content).includes('animate the fox'))).toBe(true);
    expect(cachedA.some((m) => m.role === 'assistant' && String(m.content).includes('FULL-FOX-REPLY'))).toBe(true);
  });

  it('never leaves the originating cache holding an empty assistant turn as its final state', async () => {
    let release!: () => void;
    const barrier = new Promise<void>((r) => { release = r; });
    mocks.invokeAgentStreaming.mockImplementation(async (_req: unknown, cb: StreamCallback) => {
      cb({ type: 'step', step: { status: 'running', label: 'Editing…', node_id: 'n1' } } as never);
      await barrier;
      cb({ type: 'text', text: 'edited into anime style' } as never);
    });

    const { rerender } = render(<ChatCanvas />);
    await act(async () => { submitOnCurrent('edit option 1 into anime'); });
    await flush();

    mocks.useLabContext.mockReturnValue({ sessionId: 'B', refreshCategory: vi.fn() });
    rerender(<ChatCanvas />);
    await flush();
    await act(async () => { release(); await Promise.resolve(); });
    await flush();

    const cachedA = cachedMessagesFor('A');
    const lastAssistant = [...cachedA].reverse().find((m) => m.role === 'assistant');
    expect(lastAssistant && String(lastAssistant.content), 'last assistant turn must not be empty').toBeTruthy();
    expect(cachedA.some((m) => m.role === 'user' && String(m.content).includes('edit option 1 into anime'))).toBe(true);
  });

  it('a fresh session never shows the previous session\'s chat under the new header', async () => {
    // A completes a turn; switching to a brand-new empty session B must show B's empty-state
    // greeting, never A's transcript (the "leftover chat on a new session until refresh" bug).
    mocks.invokeAgentStreaming.mockImplementation(async (_req: unknown, cb: StreamCallback) => {
      cb({ type: 'text', text: 'FOX-REPLY-A' } as never);
    });
    const { rerender } = render(<ChatCanvas />);
    await act(async () => { submitOnCurrent('animate the fox'); });
    await flush();
    expect(document.body.textContent).toContain('FOX-REPLY-A');          // A shows its turn

    mocks.useLabContext.mockReturnValue({ sessionId: 'B', refreshCategory: vi.fn() });
    rerender(<ChatCanvas />);
    await flush();

    expect(document.body.textContent).not.toContain('FOX-REPLY-A');      // B must NOT show A's turn
    expect(document.body.textContent).toContain("what's on your mind?"); // B shows its empty state
  });
});
