/**
 * @vitest-environment jsdom
 *
 * Personalized empty-state greeting: ChatCanvas greets the signed-in user by
 * first name (from the Cognito display name), NOT the previously-hardcoded
 * "Hi James". Falls back to a neutral "Hi there" when there is no real name.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, act } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  invokeAgentStreaming: vi.fn(),
  selectedOrchestratorModelId: vi.fn(() => 'model-x'),
  createSession: vi.fn(),
  listMemoryEvents: vi.fn(() => Promise.resolve([])),
  memoryEventsToChatMessages: vi.fn(() => []),
  updateSessionActivity: vi.fn(),
  getConfig: vi.fn(() => Promise.resolve({})),
  getSessionTranscript: vi.fn(() => Promise.resolve([] as unknown[])),
  updateSessionTranscript: vi.fn(() => Promise.resolve()),
  logPersistFailure: vi.fn(),
  currentUser: vi.fn(),
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
vi.mock('../services/agent-service', () => ({
  AgentService: { upload: vi.fn() },
  UploadTooLargeError: class extends Error {},
}));
vi.mock('./chat/JobReference', () => ({ default: () => null }));
vi.mock('../auth/useCurrentUser', () => ({ useCurrentUser: mocks.currentUser }));

import ChatCanvas from './ChatCanvas';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
}

function setUser(u: Record<string, unknown>) {
  mocks.currentUser.mockReturnValue({ initials: '?', signOut: vi.fn(), ...u });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.useLabContext.mockReturnValue({ sessionId: 'sess-greet', refreshCategory: vi.fn() });
});

describe('ChatCanvas personalized greeting', () => {
  it('greets by first name from a full display name', async () => {
    setUser({ mode: 'cognito', displayName: 'James Smith' });
    render(<ChatCanvas />); await flush();
    expect(screen.getByText("Hi James, what's on your mind?")).toBeInTheDocument();
  });

  it('derives + capitalizes the name from an email display name', async () => {
    setUser({ mode: 'cognito', displayName: 'jdoe@example.com' });
    render(<ChatCanvas />); await flush();
    expect(screen.getByText("Hi Jdoe, what's on your mind?")).toBeInTheDocument();
  });

  it('capitalizes a lowercase single-token name', async () => {
    setUser({ mode: 'cognito', displayName: 'alice' });
    render(<ChatCanvas />); await flush();
    expect(screen.getByText("Hi Alice, what's on your mind?")).toBeInTheDocument();
  });

  it('falls back to a neutral greeting when anonymous', async () => {
    setUser({ mode: 'anonymous', displayName: 'Not signed in' });
    render(<ChatCanvas />); await flush();
    expect(screen.getByText("Hi there, what's on your mind?")).toBeInTheDocument();
  });

  it('never leaks the old hardcoded "James" for a different user', async () => {
    setUser({ mode: 'cognito', displayName: 'Wei Chen' });
    render(<ChatCanvas />); await flush();
    expect(screen.getByText("Hi Wei, what's on your mind?")).toBeInTheDocument();
    expect(screen.queryByText(/Hi James/)).not.toBeInTheDocument();
  });
});
