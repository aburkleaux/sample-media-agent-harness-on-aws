// @vitest-environment jsdom
//
// The All / Drafts / Published control shared by the Agents and Skills
// inventories. `aria-pressed` is the contract: it is the only thing that
// distinguishes the active option, for a screen reader and for these tests
// alike, so it is asserted rather than the styling.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { InventoryStateFilter } from './InventoryStateFilter';

afterEach(cleanup);

describe('InventoryStateFilter', () => {
  it('offers exactly All, Drafts and Published, in that order', () => {
    render(<InventoryStateFilter value="all" onChange={vi.fn()} ariaLabel="Filter skills by state" />);

    const group = screen.getByRole('group', { name: 'Filter skills by state' });
    const labels = [...group.querySelectorAll('button')].map(b => b.textContent);
    expect(labels).toEqual(['All', 'Drafts', 'Published']);
  });

  it('marks only the active option as pressed', () => {
    render(<InventoryStateFilter value="drafts" onChange={vi.fn()} ariaLabel="Filter skills by state" />);

    expect(screen.getByRole('button', { name: 'Drafts' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Published' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('takes the active label colour from the fill, not a literal white', () => {
    // Shipped as `color: 'white'`, which read fine on the blue primaries and was
    // low-contrast on dark-kirocrew's mint — visibly unlike the sidebar's New
    // button, which had already moved to the token. A hex-only linter could not
    // see the keyword.
    render(<InventoryStateFilter value="all" onChange={vi.fn()} ariaLabel="Filter skills by state" />);

    const active = screen.getByRole('button', { name: 'All' });
    expect(active.style.background).toBe('var(--color-primary)');
    expect(active.style.color).toBe('var(--color-on-primary)');
  });

  it('reports the picked option', () => {
    const onChange = vi.fn();
    render(<InventoryStateFilter value="all" onChange={onChange} ariaLabel="Filter skills by state" />);

    fireEvent.click(screen.getByRole('button', { name: 'Published' }));
    expect(onChange).toHaveBeenCalledWith('published');
  });

  it('shows counts beside each label when given, and none when not', () => {
    const { unmount } = render(
      <InventoryStateFilter
        value="all"
        onChange={vi.fn()}
        ariaLabel="Filter skills by state"
        counts={{ all: 5, drafts: 2, published: 3 }}
      />,
    );
    // Accessible name folds the count in, so match on the rendered text.
    expect(screen.getByRole('button', { name: 'Drafts 2' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Published 3' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'All 5' })).toBeTruthy();
    unmount();

    render(<InventoryStateFilter value="all" onChange={vi.fn()} ariaLabel="Filter skills by state" />);
    expect(screen.getByRole('button', { name: 'Drafts' })).toBeTruthy();
  });
});
