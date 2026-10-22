/**
 * @vitest-environment jsdom
 *
 * Example/unit tests for the Custom Agent surface in the session sidebar.
 * Covers the SessionSidebar-owned pieces of task 6.5:
 *   - Req 1.1  Agents nav entry selects the custom-agent center mode
 *   - Req 1.2  Custom Agent list renders name / lifecycle state / last-modified
 *   - Req 1.3  Empty-state prompt when the owner has no agents
 *   - Req 1.4  Shared "+ New" action morphs to "New agent" and creates a draft
 *   - Req 1.5  Selecting an agent row opens the editor for that agent
 *   - Req 1.6  Creating via the shared "+ New" action opens the editor (new draft)
 *
 * The sidebar depends on a few app-level hooks (current user, gateway-admin
 * flag, jobs store, branding) that are unrelated to the Custom Agent surface;
 * they are mocked so the component can render in isolation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({
    mode: 'dev',
    displayName: 'Local Dev',
    email: undefined,
    initials: 'LD',
    sub: 'local-dev-user',
    signOut: vi.fn(),
  }),
}));

vi.mock('../auth/isGatewayAdmin', () => ({
  useIsGatewayAdmin: () => false,
}));

vi.mock('../hooks/useJobs', () => ({
  useJobs: () => ({ jobs: [] }),
}));

vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));

import SessionSidebar, { type CustomAgentSummary } from './SessionSidebar';

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

const sampleAgents: CustomAgentSummary[] = [
  {
    id: 'agent-1',
    name: 'Research Agent',
    lifecycleState: 'published',
    lastModified: Date.now() - 2 * 24 * 60 * 60 * 1000, // ~2 days ago
  },
];

describe('SessionSidebar — Custom Agent surface', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('wires the Agents nav entry to the custom-agent center mode (Req 1.1)', () => {
    const onOpenCustomAgents = vi.fn();
    render(<SessionSidebar {...makeProps({ centerMode: 'chat', onOpenCustomAgents })} />);

    // With chat active, the only "Agents" button is the nav/category entry.
    const agentsNav = screen.getByRole('button', { name: 'Agents' });
    fireEvent.click(agentsNav);

    expect(onOpenCustomAgents).toHaveBeenCalledTimes(1);
  });

  it('wires the Skills nav entry to the skills center mode', () => {
    const onOpenSkills = vi.fn();
    render(<SessionSidebar {...makeProps({ centerMode: 'chat', onOpenSkills })} />);

    // With chat active, the only "Skills" button is the nav/category entry —
    // the collapsed rail and the expanded header share the same label.
    fireEvent.click(screen.getAllByRole('button', { name: 'Skills' })[0]);
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
  });

  it('lists the Owner\'s skills only while the Skills category is open', () => {
    const skills = [{ assetId: 'skill-1', name: 'audit-skill', updatedAt: Date.now() }];

    const chat = render(<SessionSidebar {...makeProps({ centerMode: 'chat', skills })} />);
    // Closed: the sidebar must not carry every inventory at once.
    expect(screen.queryByText('audit-skill')).toBeNull();
    chat.unmount();

    render(<SessionSidebar {...makeProps({ centerMode: 'skills', skills })} />);
    expect(screen.getByText('audit-skill')).toBeTruthy();
  });

  it('prompts to create when the Owner has no skills', () => {
    render(<SessionSidebar {...makeProps({ centerMode: 'skills', skills: [] })} />);
    expect(screen.getByText(/No skills yet/i)).toBeTruthy();
  });

  it('renders each agent with name, lifecycle state, and last-modified (Req 1.2)', () => {
    render(
      <SessionSidebar
        {...makeProps({ centerMode: 'custom-agent', customAgents: sampleAgents })}
      />,
    );

    // Name
    expect(screen.getByText('Research Agent')).toBeTruthy();
    // Lifecycle state (capitalized by the sidebar formatter)
    expect(screen.getByText(/Published/)).toBeTruthy();
    // Last-modified rendered as a relative time
    expect(screen.getByText(/ago/)).toBeTruthy();
  });

  it('shows an empty-state prompt when the owner has no agents (Req 1.3)', () => {
    render(
      <SessionSidebar {...makeProps({ centerMode: 'custom-agent', customAgents: [] })} />,
    );

    expect(
      screen.getByText('No custom agents yet. Create one to get started.'),
    ).toBeTruthy();
  });

  it('binds the shared "+ New" action to create a draft agent labelled "New agent" (Req 1.4, 1.6)', () => {
    const onNewCustomAgent = vi.fn();
    render(
      <SessionSidebar
        {...makeProps({ centerMode: 'custom-agent', onNewCustomAgent })}
      />,
    );

    // The shared New_Item_Action morphs its label with the active center mode.
    const newBtnLabel = screen.getByText('New agent');
    fireEvent.click(newBtnLabel.closest('button')!);

    expect(onNewCustomAgent).toHaveBeenCalledTimes(1);
  });

  it('opens the editor for an agent when its row is selected (Req 1.5)', () => {
    const onSelectCustomAgent = vi.fn();
    render(
      <SessionSidebar
        {...makeProps({
          centerMode: 'custom-agent',
          customAgents: sampleAgents,
          onSelectCustomAgent,
        })}
      />,
    );

    fireEvent.click(screen.getByText('Research Agent'));

    expect(onSelectCustomAgent).toHaveBeenCalledWith('agent-1');
  });

  it('does not render the custom-agent list while chat is the active mode', () => {
    render(
      <SessionSidebar
        {...makeProps({ centerMode: 'chat', customAgents: sampleAgents })}
      />,
    );

    // The agent list body is gated on centerMode === 'custom-agent'.
    expect(screen.queryByText('Research Agent')).toBeNull();
  });
});

describe('SessionSidebar — New workflow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  /*
   * The button was bound to `onOpenWorkflows`, which only sets centerMode to
   * 'activity'. Pressed from the Workflow tab — where that is already the mode —
   * it set the state it was already in, so the click did nothing at all.
   */
  it('creates a workflow rather than re-navigating to the tab it is already on', () => {
    const onNewWorkflow = vi.fn();
    const onOpenWorkflows = vi.fn();
    render(
      <SessionSidebar
        {...makeProps({ centerMode: 'activity', onNewWorkflow, onOpenWorkflows })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /New workflow/ }));

    expect(onNewWorkflow).toHaveBeenCalledTimes(1);
    expect(onOpenWorkflows).not.toHaveBeenCalled();
  });

  it('still labels the action "New workflow" in the Workflow center', () => {
    render(<SessionSidebar {...makeProps({ centerMode: 'activity', onNewWorkflow: vi.fn() })} />);

    expect(screen.getByRole('button', { name: /New workflow/ })).toBeTruthy();
  });

  it('active row keeps the SAME box geometry as inactive rows — accent is painted, not laid out', () => {
    // Regression: the active row used borderLeft + a paddingLeft:7 compensation.
    // Even compensated, a border changes box geometry; selection must never move a
    // row's content or its siblings. The accent is now an inset box-shadow.
    const sessions = [
      { id: 'a', title: 'Alpha', timestamp: 2 },
      { id: 'b', title: 'Bravo', timestamp: 1 },
    ];
    const { container } = render(
      <SessionSidebar {...makeProps({ centerMode: 'chat', sessions, activeSessionId: 'a' })} />,
    );
    const active = container.querySelector('[data-session-row][data-active="true"]') as HTMLElement;
    const inactive = container.querySelector('[data-session-row][data-active="false"]') as HTMLElement;
    expect(active).toBeTruthy();
    expect(inactive).toBeTruthy();
    // Identical padding — the selected row does not indent its own content...
    expect(active.style.padding).toBe(inactive.style.padding);
    // ...and carries no left border that would widen its box.
    expect(active.style.borderLeftWidth === '' || active.style.borderLeftWidth === '0px').toBe(true);
    // The accent is drawn with an inset box-shadow instead (painted, zero layout cost).
    expect(active.style.boxShadow).toMatch(/inset/);
  });

  it('Skills rows reflect the active selection, matching Chat/Agents (shared left-list idiom)', () => {
    const skills = [
      { assetId: 's1', name: 'audit-skill', updatedAt: Date.now() },
      { assetId: 's2', name: 'other-skill', updatedAt: Date.now() },
    ];
    const { container } = render(
      <SessionSidebar {...makeProps({ centerMode: 'skills', skills, activeSkillId: 's1' })} />,
    );
    const rows = [...container.querySelectorAll('[data-skill-row]')] as HTMLElement[];
    const activeRows = rows.filter(r => r.getAttribute('data-active') === 'true');
    expect(activeRows).toHaveLength(1);
    expect(activeRows[0].textContent).toContain('audit-skill');
  });
});
