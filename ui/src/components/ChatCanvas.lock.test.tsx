/**
 * @vitest-environment jsdom
 *
 * Full-freeze composer: when the active session is locked, ChatCanvas disables
 * the input + Send (with a "locked" placeholder) and refuses to dispatch a turn.
 * Unlocked, the composer dispatches as normal. Mirrors the render harness in
 * ChatCanvas.sessionRender.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';

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

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
}
function setSession(id: string) {
  mocks.useLabContext.mockReturnValue({ sessionId: id, refreshCategory: vi.fn() });
}

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

describe('ChatCanvas — composer freeze when session is locked', () => {
  it('disables the input + Send and blocks dispatch when locked', async () => {
    setSession('A');
    render(<ChatCanvas locked />);
    const textarea = screen.getByLabelText('Chat message input') as HTMLTextAreaElement;
    expect(textarea).toBeDisabled();
    expect(textarea.placeholder).toMatch(/locked/i);
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();

    // Even a forced change + Enter must not start a turn.
    fireEvent.change(textarea, { target: { value: 'hello' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });
    await flush();
    expect(mocks.invokeAgentStreaming).not.toHaveBeenCalled();
  });

  it('dispatches normally when the session is unlocked', async () => {
    setSession('B');
    render(<ChatCanvas />); // locked defaults to false
    const textarea = screen.getByLabelText('Chat message input') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();
    fireEvent.change(textarea, { target: { value: 'hello' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });
    await flush();
    expect(mocks.invokeAgentStreaming).toHaveBeenCalledTimes(1);
  });

  it('does NOT run a job-results auto-render turn when locked', async () => {
    setSession('A');
    render(<ChatCanvas locked />);
    await act(async () => {
      window.dispatchEvent(new CustomEvent('chat:auto-render', { detail: { prompt: 'summarize', sessionId: 'A', displayText: 'Show results' } }));
    });
    await flush();
    expect(mocks.invokeAgentStreaming).not.toHaveBeenCalled();
  });

  it('does NOT run a Jobs-panel Results (chat:send) turn when locked', async () => {
    setSession('A');
    render(<ChatCanvas locked />);
    await act(async () => {
      window.dispatchEvent(new CustomEvent('chat:send', { detail: { prompt: 'fetch results', sessionId: 'A', displayText: 'Results' } }));
    });
    await flush();
    expect(mocks.invokeAgentStreaming).not.toHaveBeenCalled();
  });

  it('DOES run an auto-render turn when unlocked (control)', async () => {
    setSession('B');
    render(<ChatCanvas />);
    await act(async () => {
      window.dispatchEvent(new CustomEvent('chat:auto-render', { detail: { prompt: 'summarize', sessionId: 'B', displayText: 'Show results' } }));
    });
    await flush();
    expect(mocks.invokeAgentStreaming).toHaveBeenCalledTimes(1);
  });
});
