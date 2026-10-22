/**
 * @vitest-environment jsdom
 *
 * A manifest the parser flagged as having no usable timeline (`unrenderable`) must DEGRADE to a calm
 * "No preview available" state rather than throwing the hard "Could not display this render" error
 * (staging session 12f6f088: a conversation-analysis node that produced no timeline emitted a
 * duration-0 manifest with an overlay track carrying no start/end). A valid timeline still renders.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import NleTimeline from './NleTimeline';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

// The exact malformed sg4 shape, as `degradedManifestFromJson` would hand it to the renderer.
const degraded = {
  id: '736211b1-9fbe-4b42-85b4-82fe6536db4e',
  duration: 0,
  tracks: [{ id: 'conversation-analysis-track', type: 'overlay', src: 'https://cdn/conversation_analysis.json', label: 'Conversation Analysis', metadata: {} }],
  clips: [],
  createdAt: '2026-10-09T00:00:00.000Z',
  errors: [{ code: 'SOURCE_VIDEO_UNREACHABLE', message: 'The source video URL returned HTTP 403.' }],
  unrenderable: { reason: 'This render has no usable timeline (its duration is 0).' },
} as unknown as RenderManifest;

const valid = {
  id: '736211b1-sg3',
  template: 'nle-timeline',
  duration: 300.167,
  tracks: [{ id: 'video-nba', type: 'video', src: 'https://cdn/nba.mp4', label: 'nba', start: 0, end: 300.167 }],
  clips: [],
  captions: [],
} as unknown as RenderManifest;

describe('NleTimeline — degenerate manifest degrades gracefully', () => {
  it('shows "No preview available" for an unrenderable manifest, not the hard error', () => {
    render(<NleTimeline data={{ manifestUrl: '', manifest: degraded }} title="Analysis" theme={theme} />);
    expect(screen.getByTestId('timeline-no-preview')).toBeInTheDocument();
    expect(screen.getByText(/no preview available/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not display this render/i)).toBeNull();
  });

  it('still renders a valid timeline (regression guard)', () => {
    render(<NleTimeline data={{ manifestUrl: '', manifest: valid }} title="Video" theme={theme} />);
    expect(screen.queryByTestId('timeline-no-preview')).toBeNull();
  });
});
