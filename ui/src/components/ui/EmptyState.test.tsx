/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import EmptyState from './EmptyState';

describe('EmptyState', () => {
  /**
   * The bug this signature is shaped around: the old shared default said
   * `Click "+ Create"` on surfaces whose button actually reads `+ New skill`.
   * Nothing caught it because no test passed a label — only a browser click did.
   * So `cta` has no default; a host that wants the sentence must name its button.
   */
  it('names the host\'s real create affordance', () => {
    render(<EmptyState label="skills" cta="+ New skill" />);
    expect(screen.getByText('No skills yet. Click "+ New skill" to add one.')).toBeInTheDocument();
  });

  it('omits the instruction when the host has no create affordance to name', () => {
    render(<EmptyState label="published skills" />);
    expect(screen.getByText('No published skills yet.')).toBeInTheDocument();
    expect(screen.queryByText(/Click/)).toBeNull();
  });

  it('accepts free-form copy for states with nothing to create toward', () => {
    render(<EmptyState>No draft agents match.</EmptyState>);
    expect(screen.getByText('No draft agents match.')).toBeInTheDocument();
  });

  /**
   * Carries its own text styling rather than wrapping the copy in an inner
   * element — two nested nodes with identical text make `getByText` ambiguous,
   * which is why this is asserted rather than left to chance.
   */
  it('renders the copy in a single node', () => {
    const { container } = render(<EmptyState>Nothing here.</EmptyState>);
    expect(container.querySelectorAll('*')).toHaveLength(1);
  });

  it('is a dashed card', () => {
    const { container } = render(<EmptyState>x</EmptyState>);
    expect(container.firstElementChild!.getAttribute('style')).toContain('dashed');
  });
});
