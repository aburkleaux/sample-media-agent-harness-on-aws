/**
 * @vitest-environment jsdom
 *
 * Sidebar nav order (Projects second, directly below Chat, in both the rail and the expanded categories)
 * and the per-category cap: 15 most recent rows, a quiet "Show more" pager that adds 15 at a time, "Show
 * less" once expanded, and the active row kept visible even when it sits beyond the cap.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({ mode: 'dev', displayName: 'Local Dev', initials: 'LD', sub: 'u', signOut: vi.fn() }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../hooks/useJobs', () => ({ useJobs: () => ({ jobs: [] }) }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  listProjects: vi.fn().mockResolvedValue({ ok: true, value: [] }),
  projectsEnabled: vi.fn().mockResolvedValue(true),
}));

import SessionSidebar, { capWithActive, SIDEBAR_LIST_PAGE } from './SessionSidebar';
import { __resetProjectsListForTests } from '../projects/useProjectsList';

type SidebarProps = React.ComponentProps<typeof SessionSidebar>;

const NOW = 1_800_000_000_000;
const sessionsOf = (n: number) => Array.from({ length: n }, (_, i) => ({
  id: `s${i}`, title: `Session ${i}`, timestamp: NOW - i * 1000, messageCount: 1,
})) as unknown as SidebarProps['sessions'];

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
    onOpenSkills: vi.fn(),
    onOpenProjects: vi.fn(),
    onNewProject: vi.fn(),
    onSelectProject: vi.fn(),
    projectsEnabled: true,
    centerMode: 'chat',
    ...overrides,
  };
}

const sessionRows = () => Array.from(document.querySelectorAll<HTMLElement>('[data-session-row]'));

beforeEach(() => { __resetProjectsListForTests(); });
afterEach(cleanup);

describe('nav order', () => {
  it('puts Projects second, below Chat, in the collapsed rail', () => {
    render(<SessionSidebar {...makeProps({ collapsed: true })} />);
    const labels = ['Chat', 'Projects', 'Workflow', 'Agents', 'Skills'];
    const buttons = labels.map(l => screen.getByRole('button', { name: l }));
    for (let i = 1; i < buttons.length; i++) {
      // DOCUMENT_POSITION_FOLLOWING: each entry comes after the previous one.
      expect(buttons[i - 1].compareDocumentPosition(buttons[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it('puts Projects second, below Chat, in the expanded categories', () => {
    render(<SessionSidebar {...makeProps()} />);
    const headers = Array.from(document.querySelectorAll('button'))
      .map(b => b.textContent?.replace(/\d+/g, '').trim())
      .filter(t => ['Chat', 'Projects', 'Workflow', 'Agents', 'Skills'].includes(t ?? ''));
    expect(headers).toEqual(['Chat', 'Projects', 'Workflow', 'Agents', 'Skills']);
  });
});

describe('capped category lists', () => {
  it('shows 15 most recent sessions and a pager with the hidden count', () => {
    render(<SessionSidebar {...makeProps({ sessions: sessionsOf(38) })} />);
    expect(sessionRows()).toHaveLength(SIDEBAR_LIST_PAGE);
    expect(sessionRows()[0]).toHaveTextContent('Session 0');
    const more = screen.getByRole('button', { name: 'Show 15 more' });
    expect(more).toHaveTextContent('Show more');
    expect(more).toHaveTextContent('23');
    expect(screen.queryByRole('button', { name: 'Show less' })).toBeNull();
  });

  it('adds 15 per click, then offers Show less to collapse back', () => {
    render(<SessionSidebar {...makeProps({ sessions: sessionsOf(38) })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show 15 more' }));
    expect(sessionRows()).toHaveLength(30);
    fireEvent.click(screen.getByRole('button', { name: 'Show 8 more' }));
    expect(sessionRows()).toHaveLength(38);
    expect(screen.queryByRole('button', { name: /more$/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show less' }));
    expect(sessionRows()).toHaveLength(15);
  });

  it('renders no pager at or under the cap', () => {
    render(<SessionSidebar {...makeProps({ sessions: sessionsOf(15) })} />);
    expect(sessionRows()).toHaveLength(15);
    expect(document.querySelector('[data-list-pager]')).toBeNull();
  });

  it('keeps the active session visible when it is beyond the cap', () => {
    render(<SessionSidebar {...makeProps({ sessions: sessionsOf(30), activeSessionId: 's27' })} />);
    const r = sessionRows();
    expect(r).toHaveLength(16);
    expect(r[15]).toHaveAttribute('data-active', 'true');
    expect(r[15]).toHaveTextContent('Session 27');
    expect(screen.getByRole('button', { name: 'Show 14 more' })).toHaveTextContent('14');
  });
});

describe('capWithActive', () => {
  it('appends the active item only when it is past the limit', () => {
    const xs = [1, 2, 3, 4, 5];
    expect(capWithActive(xs, 3, x => x === 2)).toEqual([1, 2, 3]);
    expect(capWithActive(xs, 3, x => x === 5)).toEqual([1, 2, 3, 5]);
    expect(capWithActive(xs, 10, () => false)).toEqual(xs);
  });
});
