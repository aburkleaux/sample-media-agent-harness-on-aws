/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import InventoryRow from './InventoryRow';

describe('InventoryRow', () => {
  it('renders name, meta, badge and actions', () => {
    render(
      <InventoryRow
        name="my-agent"
        meta="Updated today"
        badge={{ label: 'Draft', variant: 'neutral' }}
        actions={<button>Edit</button>}
      />,
    );
    expect(screen.getByText('my-agent')).toBeInTheDocument();
    expect(screen.getByText('Updated today')).toBeInTheDocument();
    expect(screen.getByText('Draft')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  /**
   * Cards, not hairline rows. The four inventories used two different idioms
   * under headers that had deliberately been matched 1:1 — Agents drew bordered
   * cards, Skills/Tools/Hooks drew `padding: 11px 0` + a bottom border. Asserting
   * the card treatment here is what stops one host quietly reverting.
   */
  it('draws a bordered card rather than a hairline row', () => {
    const { container } = render(<InventoryRow name="x" />);
    const style = container.firstElementChild!.getAttribute('style')!;
    expect(style).toContain('--color-border');
    expect(style).toContain('border-radius: 8px'); // radius.panel
    expect(style).toContain('padding: 12px 16px');
    expect(style).toContain('--color-surface');
  });

  it('omits the meta line entirely when there is none', () => {
    // Absent is not the same as empty: an always-rendered empty div still takes
    // its margin and makes every row taller than it needs to be.
    const { container } = render(<InventoryRow name="x" />);
    expect(container.textContent).toBe('x');
  });

  it('fires onClick when the row is clickable, and shows a pointer', () => {
    const onClick = vi.fn();
    const { container } = render(<InventoryRow name="my-agent" onClick={onClick} />);
    expect(container.firstElementChild!.getAttribute('style')).toContain('cursor: pointer');
    fireEvent.click(screen.getByText('my-agent'));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('is not clickable by default', () => {
    const { container } = render(<InventoryRow name="my-agent" />);
    expect(container.firstElementChild!.getAttribute('style')).not.toContain('cursor: pointer');
  });

  it('announces a row-scoped error and can dismiss it', () => {
    const onDismissError = vi.fn();
    render(<InventoryRow name="my-agent" error="Publish failed" onDismissError={onDismissError} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Publish failed');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss error for my-agent' }));
    expect(onDismissError).toHaveBeenCalledOnce();
  });

  it('offers no dismiss control when the host cannot clear the error', () => {
    render(<InventoryRow name="my-agent" error="Publish failed" />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  // --- The description slot -------------------------------------------------
  //
  // The point of the whole change: a list of names and buttons says what each
  // capability is CALLED, never what it is FOR.

  it('renders the description under the name', () => {
    render(<InventoryRow name="ai-storyboarding" description="Turn a script into a storyboard." />);
    const name = screen.getByText('ai-storyboarding');
    const description = screen.getByText('Turn a script into a storyboard.');
    expect(name.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('puts the actions on the NAME line, not beside the description', () => {
    // This is what gives the description the full row width. With the cluster as a
    // sibling of the whole text block (its previous shape), a five-button row left
    // the description ~40% of the card and every draft wrapped to an extra line.
    render(
      <InventoryRow
        name="ai-storyboarding"
        description="Turn a script into a storyboard."
        actions={<button>Edit</button>}
      />,
    );
    const nameLine = screen.getByText('ai-storyboarding').parentElement!;
    expect(nameLine.contains(screen.getByRole('button', { name: 'Edit' }))).toBe(true);
    expect(nameLine.contains(screen.getByText('Turn a script into a storyboard.'))).toBe(false);
  });

  it('renders NOTHING for a description the source could not supply', () => {
    // Absent ≠ empty. Gateway tools, built-ins and Registry-only rows carry no
    // description; a row that rendered an empty element for them would still take
    // its margin, and `?? ''` upstream would turn "unknown" into "has none".
    const { container } = render(<InventoryRow name="x" />);
    expect(container.textContent).toBe('x');
    const { container: blank } = render(<InventoryRow name="y" description="" />);
    expect(blank.textContent).toBe('y');
  });

  it('clamps a long description and offers to expand it', async () => {
    // jsdom reports 0 for every geometry, so the toggle cannot be asserted here
    // (see ClampedText.test.tsx for the measurement path). What IS assertable is
    // that the clamp is applied at all, and at the requested number of lines.
    const long = 'Use for the managed M2C micro-workflows: ANALYSE or transform media that already exists. '.repeat(6);
    render(<InventoryRow name="video-processing" description={long} descriptionLines={2} />);
    const text = screen.getByText(long.trim());
    expect(text.style.webkitLineClamp).toBe('2');
    expect(text.style.overflow).toBe('hidden');
  });

  it('puts a name-line qualifier between the name and the badge', () => {
    // `v1` used to sit alone on the meta line under a three-line description, reading
    // as a second paragraph and costing every published row a line for one token.
    render(
      <InventoryRow
        name="ai-storyboarding"
        nameSuffix="v1"
        badge={{ label: 'Published', variant: 'success' }}
        description="Turn a story idea into a storyboard."
      />,
    );
    const nameLine = screen.getByText('ai-storyboarding').parentElement!;
    expect(nameLine.textContent).toBe('ai-storyboardingv1Published');
    // Not on the description's line, and not a row of its own.
    expect(nameLine.contains(screen.getByText('Turn a story idea into a storyboard.'))).toBe(false);
  });

  it('costs no line when there is no qualifier and no meta', () => {
    const { container } = render(<InventoryRow name="x" description="Does a thing." />);
    expect(container.textContent).toBe('xDoes a thing.');
  });

  it('fades while an optimistic removal is in flight', () => {
    const { container } = render(<InventoryRow name="x" dimmed />);
    expect(container.firstElementChild!.getAttribute('style')).toContain('opacity: 0.55');
  });
});
