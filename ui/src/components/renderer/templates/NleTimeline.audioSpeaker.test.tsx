/**
 * @vitest-environment jsdom
 *
 * A standalone audio asset opened from the Projects grid (AssetPlayer) is ONE audio track whose
 * label is the asset's display name. For a diarized-speaker deliverable that name contains
 * "SPEAKER_01", which used to match the per-speaker-channel filter and strip the only playable
 * track, leaving `primarySrc` undefined → "No media source available" for a perfectly good file.
 * The speaker filter must never remove the last audio track when there is no video.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import NleTimeline from './NleTimeline';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

const speakerWavManifest = {
  id: 'asset-x',
  template: 'nle-timeline',
  title: 'Netflix_Open_Content_Meridian — Diarize Speech (SPEAKER_01)',
  duration: 0,
  durationIsEstimate: true,
  tracks: [
    {
      id: 'src',
      type: 'audio',
      src: 'https://cdn.example/speaker01.wav',
      label: 'Netflix_Open_Content_Meridian — Diarize Speech (SPEAKER_01)',
      start: 0,
      end: 0,
    },
  ],
  clips: [],
} as unknown as RenderManifest;

describe('NleTimeline single speaker-named audio asset', () => {
  it('plays the audio instead of showing "No media source available"', () => {
    const { container } = render(
      <NleTimeline data={{ manifestUrl: '', manifest: speakerWavManifest }} title="Speaker" theme={theme} />,
    );
    expect(screen.queryByText('No media source available')).not.toBeInTheDocument();
    const audio = container.querySelector('audio');
    expect(audio).not.toBeNull();
    expect(audio?.getAttribute('src')).toBe('https://cdn.example/speaker01.wav');
  });

  it('sets crossOrigin="anonymous" by default (render manifests whose media is CORS-enabled)', () => {
    const { container } = render(
      <NleTimeline data={{ manifestUrl: '', manifest: speakerWavManifest }} title="Speaker" theme={theme} />,
    );
    expect(container.querySelector('audio')?.getAttribute('crossorigin')).toBe('anonymous');
  });

  it('omits crossOrigin when mediaCrossOrigin=false — a presigned m2c URL has no CORS (AssetPlayer)', () => {
    // With crossOrigin="anonymous" a browser refuses a no-CORS presigned S3 GET: the <audio> never
    // loads (0:00/0:00) or errors ("could not be loaded"). AssetPlayer passes false.
    const { container } = render(
      <NleTimeline
        data={{ manifestUrl: '', manifest: speakerWavManifest, mediaCrossOrigin: false }}
        title="Speaker"
        theme={theme}
      />,
    );
    const audio = container.querySelector('audio');
    expect(audio).not.toBeNull();
    expect(audio?.hasAttribute('crossorigin')).toBe(false);
    // The src still reaches the element — only the CORS gate is dropped.
    expect(audio?.getAttribute('src')).toBe('https://cdn.example/speaker01.wav');
  });
});
