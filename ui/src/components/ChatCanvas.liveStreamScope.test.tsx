/**
 * @vitest-environment jsdom
 *
 * Per-session ownership of the LIVE stream buffers.
 *
 * WorkspaceView mounts ONE <ChatCanvas> with no `key`, so the instance survives
 * session switches, and a switch does NOT abort the in-flight dispatch. The
 * live buffers (steps / text / streaming flag) used to be component-level
 * singletons, so the still-running stream for session A wrote into whatever
 * session was on screen: you would watch A's answer stream into B, and then see
 * it "correct itself" when the (already session-guarded) completion path filed
 * the real message into A and cleared the buffers.
 *
 * The reported symptom was exactly that: "I see the response in another session
 * streaming back and then it corrects itself once streaming finishes."
 *
 * These tests pin the three properties that fix requires:
 *   1. a foreign session's tokens never appear in the on-screen session;
 *   2. they are not merely dropped — switching back shows that session's stream
 *      still live (the old code cleared the buffers on switch, so navigating
 *      back mid-turn showed an empty panel until completion);
 *   3. Stop aborts only the on-screen session's stream. The abort controller was
 *      a single ref, so a second concurrent stream overwrote the first and Stop
 *      hit the wrong one, leaving the other unstoppable.
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

// The callback / abort signal handed to the Nth dispatch, in call order.
function callbackFor(n: number): StreamCallback {
  return mocks.invokeAgentStreaming.mock.calls[n][1] as StreamCallback;
}
function signalFor(n: number): AbortSignal {
  return mocks.invokeAgentStreaming.mock.calls[n][2] as AbortSignal;
}
function sessionIdFor(n: number): string {
  return mocks.invokeAgentStreaming.mock.calls[n][0].session_id;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  // Every dispatch stays in flight for the whole test, modelling a slow turn.
  mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));
});

describe('ChatCanvas — live stream buffers are owned by their session', () => {
  it("does not bleed another session's tokens into the session on screen", async () => {
    setSession('A');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('question in A');
    await flush();
    expect(sessionIdFor(0)).toBe('A');

    // Navigate to B while A's turn is still streaming.
    setSession('B');
    rerender(<ChatCanvas />);
    await flush();

    // B starts its OWN turn. This is what made the bug visible: the switch
    // effect used to clear the shared buffers, so B looked clean until it began
    // streaming itself — from then on the shared buffer was live again and A's
    // trailing tokens appended straight into B's bubble.
    typeAndSubmit('question in B');
    await flush();
    expect(sessionIdFor(1)).toBe('B');

    // A's stream keeps emitting — it was never aborted.
    await act(async () => {
      callbackFor(0)({ type: 'text', text: 'ANSWER_FOR_A' });
    });

    // B is streaming its own turn, so a bubble IS on screen — but it must carry
    // only B's content.
    expect(screen.queryByText(/ANSWER_FOR_A/)).not.toBeInTheDocument();

    // And B's own tokens must still land.
    await act(async () => {
      callbackFor(1)({ type: 'text', text: 'ANSWER_FOR_B' });
    });
    expect(screen.getByText(/ANSWER_FOR_B/)).toBeInTheDocument();
    expect(screen.queryByText(/ANSWER_FOR_A/)).not.toBeInTheDocument();
  });

  it("keeps the originating session's live stream visible when you switch back", async () => {
    setSession('A');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('question in A');
    await flush();

    setSession('B');
    rerender(<ChatCanvas />);
    await flush();

    await act(async () => {
      callbackFor(0)({ type: 'text', text: 'ANSWER_FOR_A' });
    });
    expect(screen.queryByText(/ANSWER_FOR_A/)).not.toBeInTheDocument();

    // Back to A: its turn is still in flight, so the tokens emitted while we
    // were away must be on screen and the turn must still read as streaming.
    setSession('A');
    rerender(<ChatCanvas />);
    await flush();

    expect(screen.getByText(/ANSWER_FOR_A/)).toBeInTheDocument();
    expect(screen.getByLabelText('Stop agent')).toBeInTheDocument();
  });

  it('Stop aborts the on-screen session, not whichever stream started last', async () => {
    setSession('A');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('question in A');
    await flush();

    setSession('B');
    rerender(<ChatCanvas />);
    await flush();
    typeAndSubmit('question in B');
    await flush();

    expect(sessionIdFor(0)).toBe('A');
    expect(sessionIdFor(1)).toBe('B');
    expect(signalFor(0).aborted).toBe(false);
    expect(signalFor(1).aborted).toBe(false);

    // Return to A and press Stop. With a single controller ref the LAST
    // dispatch (B's) had overwritten A's, so Stop killed B's stream while A —
    // the one the user was looking at and asked to stop — kept running.
    setSession('A');
    rerender(<ChatCanvas />);
    await flush();

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop agent'));
    });

    expect(signalFor(0).aborted).toBe(true);
    expect(signalFor(1).aborted).toBe(false);
  });
});
