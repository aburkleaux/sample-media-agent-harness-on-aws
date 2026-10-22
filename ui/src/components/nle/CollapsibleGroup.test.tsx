/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import CollapsibleGroup from './CollapsibleGroup';
import { theme } from '../../styles/tokens';

describe('CollapsibleGroup', () => {
  it('shows children with a titled/counted header, and hides them when collapsed', () => {
    render(<CollapsibleGroup title="Audio" count={3} theme={theme} testId="grp"><div>lane-content</div></CollapsibleGroup>);
    expect(screen.getByTestId('grp-body')).toBeInTheDocument();
    expect(screen.getByText('lane-content')).toBeInTheDocument();
    const header = screen.getByTestId('grp-header');
    expect(header).toHaveTextContent('Audio');
    expect(header).toHaveTextContent('3');
    fireEvent.click(header);
    expect(screen.queryByTestId('grp-body')).not.toBeInTheDocument();
    expect(screen.queryByText('lane-content')).not.toBeInTheDocument();
  });

  it('persists collapse state across remounts via storageKey', () => {
    const { unmount } = render(<CollapsibleGroup title="Lanes" theme={theme} storageKey="k1" testId="g"><div>x</div></CollapsibleGroup>);
    fireEvent.click(screen.getByTestId('g-header')); // collapse → persist '1'
    unmount();
    render(<CollapsibleGroup title="Lanes" theme={theme} storageKey="k1" testId="g2"><div>x</div></CollapsibleGroup>);
    expect(screen.queryByTestId('g2-body')).not.toBeInTheDocument(); // restored collapsed
    localStorage.removeItem('nle-group-collapsed-k1');
  });

  it('draws its disclosure as an SVG chevron, never a ▸/▾ glyph', () => {
    render(<CollapsibleGroup title="Audio" theme={theme} testId="c"><div>x</div></CollapsibleGroup>);
    const header = screen.getByTestId('c-header');
    expect(header.textContent).not.toMatch(/[▸▾]/);
    expect(screen.getByTestId('c-chevron').querySelector('svg')).not.toBeNull();
    fireEvent.click(header);
    expect(header.textContent).not.toMatch(/[▸▾]/);
    expect(screen.getByTestId('c-chevron').querySelector('svg')).not.toBeNull();
  });
});
