/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ClampedText from './ClampedText';

/**
 * The toggle is driven by a MEASUREMENT, which jsdom does not perform: every
 * element reports `scrollHeight === clientHeight === 0`. So the overflow tests
 * stub those two properties, which is the honest way to exercise the branch —
 * the alternative (a character-count threshold) is the design this component
 * exists to avoid, since the same text is clamped at one pane width and complete
 * at another.
 */

const LONG = 'Use for the managed M2C micro-workflows: ANALYSE or transform media. '.repeat(8).trim();

function stubOverflow({ scrollHeight, clientHeight }: { scrollHeight: number; clientHeight: number }) {
  const scroll = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(scrollHeight);
  const client = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(clientHeight);
  return () => { scroll.mockRestore(); client.mockRestore(); };
}

let restore: (() => void) | null = null;

beforeEach(() => {
  // ResizeObserver is not in jsdom; the component must not require it.
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      disconnect() {}
    };
  }
});

afterEach(() => {
  restore?.();
  restore = null;
  cleanup();
});

describe('ClampedText', () => {
  it('clamps to the requested number of lines', () => {
    render(<ClampedText lines={3}>{LONG}</ClampedText>);
    const text = screen.getByText(LONG);
    expect(text.style.webkitLineClamp).toBe('3');
    expect(text.style.display).toBe('-webkit-box');
    expect(text.style.overflow).toBe('hidden');
  });

  it('offers no toggle when the text already fits', () => {
    // The measurement says it fits, so "more" would reveal nothing — a control
    // that does nothing is worse than no control.
    restore = stubOverflow({ scrollHeight: 40, clientHeight: 40 });
    render(<ClampedText>Short.</ClampedText>);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('offers a toggle when the text is actually cut off, and expands in place', () => {
    restore = stubOverflow({ scrollHeight: 120, clientHeight: 60 });
    render(<ClampedText>{LONG}</ClampedText>);

    const more = screen.getByRole('button', { name: 'more' });
    fireEvent.click(more);

    // Expanded: the clamp is gone and the control now offers to collapse.
    const text = screen.getByText(LONG);
    expect(text.style.webkitLineClamp).toBe('');
    expect(text.style.overflow).toBe('');
    expect(screen.getByRole('button', { name: 'less' })).toBeInTheDocument();
  });

  it('keeps the collapse control available once expanded', () => {
    // Guards the measurement loop: expanded, scrollHeight === clientHeight, so a
    // naive re-measure would decide the text fits and remove the only way back.
    restore = stubOverflow({ scrollHeight: 120, clientHeight: 60 });
    render(<ClampedText>{LONG}</ClampedText>);
    fireEvent.click(screen.getByRole('button', { name: 'more' }));
    restore();
    restore = stubOverflow({ scrollHeight: 200, clientHeight: 200 });
    expect(screen.getByRole('button', { name: 'less' })).toBeInTheDocument();
  });

  it('renders the full text either way — the clamp is visual only', () => {
    // A JS pre-truncation is what the old built-in row did (80 chars, then a CSS
    // clamp on top), which made the text unrecoverable and unsearchable.
    restore = stubOverflow({ scrollHeight: 120, clientHeight: 60 });
    const { container } = render(<ClampedText>{LONG}</ClampedText>);
    expect(container.textContent).toContain(LONG);
  });
});
