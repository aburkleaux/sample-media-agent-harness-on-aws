// @vitest-environment jsdom
//
// The shared button. Exists because 84 ad-hoc `…BtnStyle` constants had drifted
// apart AND lost their meaning — a View action was being drawn with
// `deleteBtnStyle` because that was the neutral outline style that existed.
// These assert the parts that carry meaning, not the exact pixels.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Button from './Button';

afterEach(cleanup);

describe('Button', () => {
  it('defaults to secondary so a bare <Button> never impersonates the primary action', () => {
    render(<Button>View</Button>);
    const btn = screen.getByRole('button', { name: 'View' });
    // Outlined, on the surface — not the filled primary.
    expect(btn.style.background).toBe(theme('--color-surface'));
    expect(btn.style.border).toContain('1px solid');
  });

  it('fills only for primary, and takes its label colour from the fill', () => {
    render(<Button variant="primary">Publish</Button>);
    const btn = screen.getByRole('button', { name: 'Publish' });
    expect(btn.style.background).toBe(theme('--color-primary'));
    // NOT a literal white. dark-kirocrew's primary is a bright mint where white
    // is ~1.5:1, so the label tracks the fill via a token: white on the blue
    // primaries, near-black on the mint.
    expect(btn.style.color).toBe(theme('--color-on-primary'));
  });

  it('derives the danger treatment from the error token, not a raw red', () => {
    render(<Button variant="danger">Delete</Button>);
    const btn = screen.getByRole('button', { name: 'Delete' });
    expect(btn.style.color).toBe(theme('--color-error'));
    // Tinted against the surface so it flips with the theme.
    expect(btn.style.background).toContain('color-mix');
  });

  it('busy disables and swaps the label, so a double submit is not possible', () => {
    const onClick = vi.fn();
    render(<Button busy busyLabel="Publishing…" onClick={onClick}>Publish</Button>);

    const btn = screen.getByRole('button', { name: 'Publishing…' });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('falls back to the idle label when busy has nothing else to say', () => {
    render(<Button busy>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('passes through layout style but is still the one deciding colour', () => {
    render(<Button style={{ marginLeft: 8 }} variant="ghost">Back</Button>);
    const btn = screen.getByRole('button', { name: 'Back' });
    expect(btn.style.marginLeft).toBe('8px');
    expect(btn.style.background).toBe('transparent');
  });
});

/** jsdom keeps `var(--x)` verbatim in inline styles, so compare against that. */
function theme(varName: string): string {
  return `var(${varName})`;
}
