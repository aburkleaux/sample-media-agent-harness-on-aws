/**
 * @vitest-environment jsdom
 *
 * #2 fix: "Copy source URL" must copy the IMAGE THE USER IS LOOKING AT in a gallery
 * tab, not artifactUrls[0] (the first). ImageAnnotated emits a 'select' interaction
 * with the visible image's url; the panel captures it and prefers it in copySource,
 * resetting on tab change so a selection never leaks into a different tab's copy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

const writeText = vi.fn().mockResolvedValue(undefined);
beforeEach(() => {
  writeText.mockClear();
  Object.assign(navigator, { clipboard: { writeText } });
});

function galleryEntry(): RenderHistoryEntry {
  return {
    label: 'Frames', timestamp: 1,
    artifactUrls: ['https://cdn/a.png'],
    blocks: [{ template: 'image-annotated', title: 'Frames', data: {
      src: 'https://cdn/a.png',
      images: [{ src: 'https://cdn/a.png', label: 'a' }, { src: 'https://cdn/b.png', label: 'b' }],
    } }],
  } as unknown as RenderHistoryEntry;
}
function soloEntry(): RenderHistoryEntry {
  return {
    label: 'Solo', timestamp: 2,
    artifactUrls: ['https://cdn/solo.png'],
    blocks: [{ template: 'image-annotated', title: 'Solo', data: {
      src: 'https://cdn/solo.png',
      images: [{ src: 'https://cdn/solo.png', label: 'solo' }],
    } }],
  } as unknown as RenderHistoryEntry;
}

describe('SessionRendererPanel — copy source follows gallery selection', () => {
  it('copies the FIRST image before any selection (fallback = tabSourceUrl)', () => {
    render(<SessionRendererPanel history={[galleryEntry()]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith('https://cdn/a.png');
  });

  it('copies the SELECTED image after a thumbnail click', () => {
    render(<SessionRendererPanel history={[galleryEntry()]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /^b$/ }));
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith('https://cdn/b.png');
  });

  it('resets on tab change so a gallery selection does not leak into another tab', () => {
    const history = [galleryEntry(), soloEntry()];
    const { rerender } = render(<SessionRendererPanel history={history} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /^b$/ }));            // select b in tab 0
    rerender(<SessionRendererPanel history={history} currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />); // switch tabs
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith('https://cdn/solo.png');
    expect(writeText).not.toHaveBeenCalledWith('https://cdn/b.png');
  });
});
