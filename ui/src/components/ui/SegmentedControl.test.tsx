/// <reference types="vitest" />
/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SegmentedControl, { panelDomId, tabDomId } from './SegmentedControl';

type Tab = 'a' | 'b' | 'c';
const OPTIONS = [
  { value: 'a' as Tab, label: 'Alpha' },
  { value: 'b' as Tab, label: 'Beta' },
  { value: 'c' as Tab, label: 'Gamma' },
];

function setup(value: Tab = 'a', variant: 'segmented' | 'underline' = 'segmented') {
  const onChange = vi.fn();
  render(
    <SegmentedControl
      options={OPTIONS}
      value={value}
      onChange={onChange}
      label="Creation mode"
      variant={variant}
      idPrefix="t1"
    />,
  );
  return { onChange };
}

describe('SegmentedControl — a11y wiring', () => {
  it('is a labelled tablist of tabs', () => {
    setup();
    expect(screen.getByRole('tablist', { name: 'Creation mode' })).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
  });

  it('marks exactly one tab selected', () => {
    setup('b');
    expect(screen.getByRole('tab', { name: 'Beta' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('aria-selected', 'false');
  });

  it('points each tab at its panel, and exposes matching id helpers', () => {
    setup();
    // The consumer builds its panel from the same helpers, so a mismatch here is a broken
    // aria-labelledby/aria-controls pair — invisible without an assertion.
    expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('id', tabDomId('t1', 'a'));
    expect(screen.getByRole('tab', { name: 'Alpha' }))
      .toHaveAttribute('aria-controls', panelDomId('t1', 'a'));
  });

  it('keeps ONE stop in the tab order (roving tabIndex)', () => {
    setup('b');
    expect(screen.getByRole('tab', { name: 'Beta' })).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('tab', { name: 'Gamma' })).toHaveAttribute('tabindex', '-1');
  });
});

describe('SegmentedControl — interaction', () => {
  it('reports the clicked value', () => {
    const { onChange } = setup('a');
    fireEvent.click(screen.getByRole('tab', { name: 'Gamma' }));
    expect(onChange).toHaveBeenCalledWith('c');
  });

  // Keyboard navigation is the reason this primitive exists rather than a fifth copy of the
  // hand-rolled pattern — none of the four had it.
  it('moves with arrows and wraps at both ends', () => {
    const { onChange } = setup('a');
    const first = screen.getByRole('tab', { name: 'Alpha' });
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith('b');
    fireEvent.keyDown(first, { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenLastCalledWith('c'); // wraps backwards from the first
  });

  it('supports Home and End', () => {
    const { onChange } = setup('b');
    const current = screen.getByRole('tab', { name: 'Beta' });
    fireEvent.keyDown(current, { key: 'End' });
    expect(onChange).toHaveBeenLastCalledWith('c');
    fireEvent.keyDown(current, { key: 'Home' });
    expect(onChange).toHaveBeenLastCalledWith('a');
  });

  it('ignores keys it does not own', () => {
    const { onChange } = setup('a');
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Alpha' }), { key: 'x' });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('SegmentedControl — variants', () => {
  it('renders both variants as the same accessible structure', () => {
    // The look differs; the semantics must not. A variant that dropped the roles would be a
    // silent a11y regression, since it would still look right.
    const { unmount } = render(
      <SegmentedControl options={OPTIONS} value="a" onChange={() => {}} label="V" idPrefix="p"
        variant="underline" />,
    );
    expect(screen.getByRole('tablist', { name: 'V' })).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
    unmount();
  });

  it('never sets a raw px font size (the style ratchet forbids it)', () => {
    setup();
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab.style.fontSize).toMatch(/^var\(--fs-/);
    }
  });
});
