// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FileLocation } from './FileLocation';
import { OPEN_PROJECT_EVENT } from '../../projects/openProject';

afterEach(() => vi.restoreAllMocks());

function opened(): Array<{ projectId: string; folder?: string }> {
  const seen: Array<{ projectId: string; folder?: string }> = [];
  window.addEventListener(OPEN_PROJECT_EVENT, (e) => seen.push((e as CustomEvent).detail));
  return seen;
}

describe('FileLocation', () => {
  it('names the path and opens the clicked folder in the project', () => {
    const seen = opened();
    render(<FileLocation projectId="p1" folder="Footage/Trailers/Tidal Line" />);
    const loc = screen.getByTestId('file-location');
    expect(loc).toHaveTextContent('Footage / Trailers / Tidal Line');
    expect(loc).toHaveAttribute('title', 'In Footage / Trailers / Tidal Line — open the folder');
    fireEvent.click(screen.getByRole('button', { name: 'Trailers' }));
    expect(seen[seen.length - 1]).toEqual({ projectId: 'p1', folder: 'Footage/Trailers' });
  });

  it('says Top level for a file in no folder, and opens the top level', () => {
    const seen = opened();
    render(<FileLocation projectId="p1" folder="" />);
    fireEvent.click(screen.getByRole('button', { name: 'Top level' }));
    expect(seen[seen.length - 1]).toEqual({ projectId: 'p1', folder: '' });
  });

  it('hides when the file is in the folder already on screen, and while unknown', () => {
    const { container, rerender } = render(<FileLocation projectId="p1" folder="Boards" hideIn="Boards" />);
    expect(container).toBeEmptyDOMElement();
    rerender(<FileLocation projectId="p1" assetId={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('a crumb leads with the project name and emphasises the folder', () => {
    const seen = opened();
    render(<FileLocation variant="crumb" projectId="p1" projectName="Promo" folder="Boards/Board 07" />);
    expect(screen.getByTestId('file-location')).toHaveTextContent('Promo/Boards/Board 07');
    fireEvent.click(screen.getByRole('button', { name: 'Promo' }));
    expect(seen[seen.length - 1]).toEqual({ projectId: 'p1', folder: '' });
  });

  it('a click does not reach the card underneath (no preview opens)', () => {
    const onCard = vi.fn();
    render(<div onClick={onCard}><FileLocation projectId="p1" folder="A" /></div>);
    fireEvent.click(screen.getByRole('button', { name: 'A' }));
    expect(onCard).not.toHaveBeenCalled();
  });
});
