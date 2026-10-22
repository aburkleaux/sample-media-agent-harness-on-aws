/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

/*
 * The four editors are stubbed rather than rendered: this test is about the FRAME
 * — who owns the title and the way out — and mounting the real editors would pull
 * in lab-api and the whole draft-loading path for no added coverage.
 *
 * Each stub reports the `showHeader` it was handed, which is the contract under
 * test. If a stub renders its own header while the frame also does, that is the
 * bug: an editor used to show a title + ✕ INSIDE a frame that already had a
 * `← Back`, plus a `Cancel` in its own action row — three ways out of one form
 * and two competing titles.
 *
 * Skills are now the deliberate exception in the other direction: `SkillEditor`
 * owns a toolbar carrying Back and Save together, so the frame must supply NOTHING
 * for that category. Both halves are asserted, because the failure modes are
 * opposite — a frame bar for skills is a duplicate back control, and a missing one
 * for any other category leaves no way out at all.
 */
function stub(name: string) {
  return ({ showHeader }: { showHeader?: boolean }) => (
    <div data-testid={`stub-${name}`} data-showheader={String(showHeader)}>
      {showHeader && <span>{name} own header</span>}
    </div>
  );
}

vi.mock('./SkillEditor', () => ({ default: stub('skill') }));
vi.mock('./SpecialtyAgentEditor', () => ({ default: stub('agent') }));
vi.mock('./ToolCollectionEditor', () => ({ default: stub('tools') }));
vi.mock('./WorkflowEditor', () => ({ default: stub('workflow') }));

import CenterEditor from './CenterEditor';

/** Categories whose chrome the frame still owns. */
const FRAMED = [
  { category: 'specialty-agent', stub: 'agent', create: 'New Agent', edit: 'Edit Agent' },
  { category: 'tool-collection', stub: 'tools', create: 'New Tool Collection', edit: 'Edit Tool Collection' },
  { category: 'workflow', stub: 'workflow', create: 'New Workflow', edit: 'Edit Workflow' },
] as const;

describe('CenterEditor', () => {
  it.each(FRAMED)('owns the header for $category, so the editor does not', ({ category, stub: id, edit }) => {
    render(<CenterEditor category={category} assetId="a1" onClose={() => {}} />);

    // The frame supplies the title...
    expect(screen.getByText(edit)).toBeInTheDocument();
    // ...and tells the editor to stand down.
    expect(screen.getByTestId(`stub-${id}`)).toHaveAttribute('data-showheader', 'false');
    expect(screen.queryByText(`${id} own header`)).toBeNull();
  });

  it.each(FRAMED)('titles $category create mode distinctly', ({ category, create }) => {
    render(<CenterEditor category={category} assetId={null} onClose={() => {}} />);
    expect(screen.getByText(create)).toBeInTheDocument();
  });

  it.each(FRAMED)('renders exactly one way out of a $category form', ({ category }) => {
    render(<CenterEditor category={category} assetId="a1" onClose={() => {}} />);
    // Only the frame's Back control. A per-editor ✕ here would make two.
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '← Back to Chat' })).toBeInTheDocument();
  });

  it('names where Back goes, so the label is not always "Chat"', () => {
    render(
      <CenterEditor category="specialty-agent" assetId="a1" onClose={() => {}} backLabel="Back to Agents" />,
    );
    expect(screen.getByRole('button', { name: '← Back to Agents' })).toBeInTheDocument();
  });

  describe('skills, which bring their own chrome', () => {
    it.each([
      { mode: 'edit', assetId: 'a1' },
      { mode: 'create', assetId: null },
    ])('adds no bar of its own in $mode mode', ({ assetId }) => {
      render(<CenterEditor category="skill" assetId={assetId} onClose={() => {}} backLabel="Back to Skills" />);

      // No second back control and no second title. `SkillEditor`'s own toolbar is
      // the only one, and the Skills surface header above already names the surface
      // — this bar said so twice more.
      expect(screen.queryAllByRole('button')).toHaveLength(0);
      expect(screen.queryByText('Edit SKILL.md')).toBeNull();
      expect(screen.queryByText('New Skill')).toBeNull();
      expect(screen.queryByText(/Back to Skills/)).toBeNull();
    });

    it('still tells the editor the host owns the header', () => {
      // `showHeader` governs the OLD title-plus-✕ header, which nothing should
      // render here — the toolbar is not that header, so this stays false.
      render(<CenterEditor category="skill" assetId="a1" onClose={() => {}} />);
      expect(screen.getByTestId('stub-skill')).toHaveAttribute('data-showheader', 'false');
      expect(screen.queryByText('skill own header')).toBeNull();
    });
  });
});
