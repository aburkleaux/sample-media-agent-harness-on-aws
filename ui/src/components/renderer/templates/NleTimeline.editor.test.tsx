/**
 * @vitest-environment jsdom
 *
 * Integration: a video manifest must render the interactive professional editor
 * (ruler + mark controls + action chips), not a bare <video>. This is the
 * "all video renders in the editor" requirement.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import NleTimeline from './NleTimeline';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

const videoManifest = {
  id: 'm1',
  template: 'nle-timeline',
  duration: 60,
  tracks: [{ id: 'v1', type: 'video', src: 'https://cdn.example/x.mp4', start: 0, end: 60 }],
  clips: [],
  captions: [],
} as unknown as RenderManifest;

describe('NleTimeline video → interactive editor', () => {
  it('renders TimelineEditor (ruler, action bar, chips) for a bare video manifest with no clips', () => {
    render(<NleTimeline data={{ manifestUrl: '', manifest: videoManifest }} title="video" theme={theme} />);
    expect(screen.getByTestId('timeline-editor')).toBeInTheDocument();
    expect(screen.getByTestId('timeline-ruler')).toBeInTheDocument();
    expect(screen.getByTestId('action-clip')).toBeInTheDocument();
    expect(screen.getByTestId('action-frame')).toBeInTheDocument();
    expect(screen.getByTestId('analyze-menu')).toBeInTheDocument();
    // lean bar — the old discrete mark buttons are gone
    expect(screen.queryByTestId('mark-in')).not.toBeInTheDocument();
  });

  it('restores persisted marks for the artifact on mount (keyed by sourceKey)', () => {
    localStorage.setItem('nle-edits-persist-key', JSON.stringify({ ranges: [{ in: 5, out: 12 }], points: [{ t: 20 }] }));
    const m = { ...(videoManifest as object), sourceKey: 'persist-key' } as unknown as RenderManifest;
    const { container } = render(<NleTimeline data={{ manifestUrl: '', manifest: m }} title="video" theme={theme} />);
    // the saved range + point are restored into the engine and rendered
    expect(container.querySelector('[data-testid^="range-band-"]')).toBeTruthy();
    expect(container.querySelector('[data-testid^="point-"]')).toBeTruthy();
    localStorage.removeItem('nle-edits-persist-key');
  });
});
