/**
 * @vitest-environment jsdom
 *
 * A multi-audio timeline (independent clips: TTS chunks, stems) must render the
 * EDITABLE clip gallery — each clip with its own mark-in/out ruler + a batch
 * "Trim all marked" — not a stack of un-editable players. Session 3dedb10d: a
 * 9-chunk TTS review came back as a plain 9-track timeline with no trim.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import NleTimeline from './NleTimeline';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

const multiAudioManifest = {
  id: 'm-tts',
  template: 'nle-timeline',
  renderSkill: 'render-audio',
  duration: 30,
  tracks: [
    { id: 'c1', type: 'audio', src: 'https://cdn.example/seg1.wav', label: 'Chunk 1', start: 0, end: 16.9 },
    { id: 'c2', type: 'audio', src: 'https://cdn.example/seg2.wav', label: 'Chunk 2', start: 0, end: 7.9 },
    { id: 'c3', type: 'audio', src: 'https://cdn.example/seg3.wav', label: 'Chunk 3', start: 0, end: 5.2 },
  ],
  clips: [],
  captions: [],
} as unknown as RenderManifest;

describe('NleTimeline multi-audio → editable clip gallery', () => {
  it('renders the clip gallery with a per-clip editor and a batch trim button', () => {
    render(<NleTimeline data={{ manifestUrl: '', manifest: multiAudioManifest }} title="TTS" theme={theme} />);
    expect(screen.getByTestId('clip-gallery')).toBeInTheDocument();
    // one editable card per audio track (with its own mark-in/out ruler)
    expect(screen.getByTestId('clip-card-c1')).toBeInTheDocument();
    expect(screen.getByTestId('clip-card-c2')).toBeInTheDocument();
    expect(screen.getByTestId('clip-card-c3')).toBeInTheDocument();
    expect(screen.getByTestId('clip-ruler-c1')).toBeInTheDocument();
    // the batch trim action exists (audio → "Trim")
    expect(screen.getByTestId('trim-all-marked')).toHaveTextContent('Trim all marked (0)');
  });
});
