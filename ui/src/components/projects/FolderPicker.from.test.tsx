// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FolderPicker } from './FolderPicker';

describe('FolderPicker "From"', () => {
  it('says where the item is now, and does not offer that folder', () => {
    render(<FolderPicker folders={['Footage', 'Footage/Trailers', 'Selects']} current="Footage/Trailers" from="Footage/Trailers"
      title="Move a.mp4 to" onPick={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByTestId('folder-picker-from')).toHaveTextContent('From Footage / Trailers');
    expect(screen.queryByRole('option', { name: 'Footage / Trailers' })).toBeNull();
  });

  it('a file in no folder is "From Top level"; no from → no line', () => {
    const { rerender } = render(<FolderPicker folders={['A']} current="" from="" title="Move" onPick={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByTestId('folder-picker-from')).toHaveTextContent(/^From Top level$/);
    rerender(<FolderPicker folders={['A']} title="Save" onPick={vi.fn()} onClose={vi.fn()} />);
    expect(screen.queryByTestId('folder-picker-from')).toBeNull();
  });
});
