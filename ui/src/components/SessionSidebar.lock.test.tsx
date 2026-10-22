/**
 * @vitest-environment jsdom
 *
 * Session-lock UI (full-freeze) in the sidebar: a locked session shows a
 * persistent lock badge and renders Rename/Delete DISABLED (still visible), the
 * lock toggle flips state, and clicking a disabled action is a no-op.
 */
import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({ mode: 'dev', displayName: 'Local Dev', email: undefined, initials: 'LD', sub: 'local-dev-user', signOut: vi.fn() }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../hooks/useJobs', () => ({ useJobs: () => ({ jobs: [] }) }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));

import SessionSidebar from './SessionSidebar';

type SidebarProps = React.ComponentProps<typeof SessionSidebar>;

function makeProps(overrides: Partial<SidebarProps> = {}): SidebarProps {
  return {
    sessions: [],
    activeSessionId: '',
    collapsed: false,
    onToggleCollapse: vi.fn(),
    onSelectSession: vi.fn(),
    onNewSession: vi.fn(),
    onGoChat: vi.fn(),
    onShowLanding: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenWorkflows: vi.fn(),
    onOpenCustomAgents: vi.fn(),
    onSelectCustomAgent: vi.fn(),
    onNewCustomAgent: vi.fn(),
    customAgents: [],
    centerMode: 'chat',
    ...overrides,
  };
}

const btn = (name: RegExp) => screen.getByRole('button', { name }) as HTMLButtonElement;

describe('SessionSidebar — session lock (full freeze)', () => {
  it('disables Rename + Delete and shows a lock badge on a locked session', () => {
    const { container } = render(<SessionSidebar {...makeProps({
      sessions: [{ id: 's1', title: 'Demo', timestamp: 1, locked: true }],
      activeSessionId: 's1',
      onDeleteSession: vi.fn(),
      onRenameSession: vi.fn(),
      onToggleLock: vi.fn(),
    })} />);
    expect(btn(/Rename session Demo/i).disabled).toBe(true);
    expect(btn(/Delete session Demo/i).disabled).toBe(true);
    // Unlock toggle is always available so a frozen session can be reopened.
    expect(btn(/Unlock session Demo/i).disabled).toBe(false);
    // Persistent, subtle lock badge on the title.
    expect(container.querySelector('span[aria-label="Locked"]')).not.toBeNull();
  });

  it('enables Rename + Delete and offers a Lock toggle on an unlocked session', () => {
    const { container } = render(<SessionSidebar {...makeProps({
      sessions: [{ id: 's1', title: 'Demo', timestamp: 1 }],
      activeSessionId: 's1',
      onDeleteSession: vi.fn(),
      onRenameSession: vi.fn(),
      onToggleLock: vi.fn(),
    })} />);
    expect(btn(/Rename session Demo/i).disabled).toBe(false);
    expect(btn(/Delete session Demo/i).disabled).toBe(false);
    expect(btn(/Lock session Demo/i)).toBeTruthy();
    expect(container.querySelector('span[aria-label="Locked"]')).toBeNull();
  });

  it('calls onToggleLock with the desired next state', () => {
    const onToggleLock = vi.fn();
    render(<SessionSidebar {...makeProps({
      sessions: [{ id: 's1', title: 'Demo', timestamp: 1, locked: false }],
      activeSessionId: 's1',
      onDeleteSession: vi.fn(),
      onRenameSession: vi.fn(),
      onToggleLock,
    })} />);
    fireEvent.click(btn(/Lock session Demo/i));
    expect(onToggleLock).toHaveBeenCalledWith('s1', true);
  });

  it('does not fire onDeleteSession when the disabled Delete is clicked on a locked session', () => {
    const onDeleteSession = vi.fn();
    render(<SessionSidebar {...makeProps({
      sessions: [{ id: 's1', title: 'Demo', timestamp: 1, locked: true }],
      activeSessionId: 's1',
      onDeleteSession,
      onRenameSession: vi.fn(),
      onToggleLock: vi.fn(),
    })} />);
    fireEvent.click(btn(/Delete session Demo/i));
    expect(onDeleteSession).not.toHaveBeenCalled();
  });
});
