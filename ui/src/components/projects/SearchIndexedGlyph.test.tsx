// @vitest-environment jsdom
import '@testing-library/jest-dom';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import SearchIndexedGlyph, { NameWithIndexGlyph, SEARCH_INDEXED_LABEL } from './SearchIndexedGlyph';

describe('SearchIndexedGlyph', () => {
  it('shows a labelled magnifier only when indexing is on', () => {
    const { rerender } = render(<SearchIndexedGlyph indexing />);
    expect(screen.getByRole('img', { name: SEARCH_INDEXED_LABEL })).toHaveAttribute('title', SEARCH_INDEXED_LABEL);
    for (const v of [false, undefined]) {
      rerender(<SearchIndexedGlyph indexing={v} />);
      expect(screen.queryByTestId('search-indexed-glyph')).toBeNull(); // off or unknown: nothing, the row stays clean
    }
  });
  it('follows a name that still truncates', () => {
    render(<NameWithIndexGlyph name="project-index-test-v4" indexing />);
    const name = screen.getByText('project-index-test-v4');
    expect(name).toHaveStyle({ textOverflow: 'ellipsis', whiteSpace: 'nowrap' });
    expect(name.nextElementSibling).toHaveAttribute('data-testid', 'search-indexed-glyph');
  });
});
