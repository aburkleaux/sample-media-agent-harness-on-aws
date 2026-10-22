/**
 * @vitest-environment jsdom
 *
 * The composer re-fits its height when its WIDTH changes, not only when its text does. Sized while the pane
 * was narrow (a Renderer tab opening, the sidebar/window resizing), a near-empty composer used to stay ~200px
 * tall after the pane widened, until the text changed or the page reloaded. Mirrors the render harness in
 * ChatCanvas.lock.test.tsx.
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

let roCallback: (() => void) | null = null;
class ResizeObserverCapture {
  constructor(cb: () => void) { roCallback = cb; }
  observe() {} unobserve() {} disconnect() {}
}
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverCapture;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.useLabContext.mockReturnValue({ sessionId: 'A', refreshCategory: vi.fn() });
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));
});

/** jsdom has no layout: the textarea's wrapped height is a function of the pane width we set. */
function layout(el: HTMLTextAreaElement, geom: { width: number; contentHeight: number }) {
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => geom.width });
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => geom.contentHeight });
}

describe('ChatCanvas composer height', () => {
  it('shrinks back when the pane widens, without a text change', async () => {
    render(<ChatCanvas />);
    const ta = screen.getByLabelText('Chat message input') as HTMLTextAreaElement;
    const geom = { width: 120, contentHeight: 260 };   // narrow pane: the text wraps tall
    layout(ta, geom);
    fireEvent.change(ta, { target: { value: 'describe shot 46 please' } });
    expect(ta.style.height).toBe('200px');              // capped at the max

    geom.width = 640; geom.contentHeight = 40;          // the pane widens; the same text fits one line
    act(() => { roCallback?.(); });
    expect(ta.style.height).toBe('40px');
  });

  it('ignores observer callbacks that do not change the width', async () => {
    render(<ChatCanvas />);
    const ta = screen.getByLabelText('Chat message input') as HTMLTextAreaElement;
    const geom = { width: 300, contentHeight: 80 };
    layout(ta, geom);
    fireEvent.change(ta, { target: { value: 'hello' } });
    expect(ta.style.height).toBe('80px');
    geom.contentHeight = 120;                            // height-only change (our own re-fit) must not loop
    act(() => { roCallback?.(); });
    expect(ta.style.height).toBe('80px');
  });
});
