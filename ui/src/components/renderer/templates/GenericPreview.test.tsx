// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import GenericPreview from './GenericPreview';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';

// Mirrors the manifest the Render Agent builds from a real face-recognition run
// (verified against live facegroup.json / facemetadata.json).
const manifest = {
  id: '550e8400-e29b-41d4-a716-446655440000',
  template: 'video-annotations',
  duration: 718.919,
  tracks: [{ id: 'source-video', type: 'video', src: 'https://cdn.example.com/meridian.mp4', start: 0, end: 718.919 }],
  clips: [],
  captions: [],
  createdAt: '2026-07-30T01:00:00.000Z',
  media: { src: 'https://cdn.example.com/meridian.mp4', duration: 718.919 },
  entities: [
    {
      id: 'entity-0',
      label: 'Kevin Kilner',
      confidence: 99.974,
      screenTimeSec: 8.8,
      thumbnail: 'https://cdn.example.com/person_0000.jpg',
    },
    { id: 'entity-1', label: 'Person_2', screenTimeSec: 3.4 },
  ],
  annotations: [
    { entityId: 'entity-0', start: 30.03, end: 31.031, box: { x: 0.306, y: 0.183, width: 0.136, height: 0.347 }, confidence: 99.94 },
    { entityId: 'entity-1', start: 49.049, end: 50.05, box: { x: 0.6, y: 0.2, width: 0.1, height: 0.3 } },
  ],
  appearances: [
    { entityId: 'entity-0', start: 30.03, end: 30.83 },
    { entityId: 'entity-1', start: 49.049, end: 50.05 },
  ],
  metadata: { num_people: 5, frames_analyzed: 59 },
  artifacts: [
    { url: 'https://cdn.example.com/facemetadata.json', label: 'facemetadata', mime: 'application/json', kind: 'data' },
    { url: 'https://cdn.example.com/person_0000.jpg', label: 'person 0000', mime: 'image/jpeg', kind: 'image' },
  ],
} as unknown as RenderManifest;

/** Drive the video clock the way playback would. */
async function seekTo(seconds: number) {
  const video = document.querySelector('video') as HTMLVideoElement;
  video.currentTime = seconds;
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/**
 * Extracted from VideoAnnotations.test.tsx when the `video-annotations` template was folded
 * into the timeline container and that component was removed. These cases are about
 * GenericPreview and were never about the face view — keeping them intact.
 */
describe('GenericPreview', () => {
  afterEach(() => cleanup());

  it('renders an image gallery and file cards instead of an empty player', () => {
    const generic = {
      id: 'x',
      template: 'generic-preview',
      duration: 0.001,
      tracks: [],
      clips: [],
      createdAt: '2026-07-30T01:00:00.000Z',
      artifacts: [
        { url: 'https://cdn.example.com/person_0000.jpg', label: 'person 0000', mime: 'image/jpeg', kind: 'image' },
        { url: 'https://cdn.example.com/facemetadata.json', label: 'facemetadata', mime: 'application/json', kind: 'data' },
      ],
    } as unknown as RenderManifest;

    render(<GenericPreview data={{ manifest: generic }} title="Artifacts" theme={theme} />);
    const images = document.querySelectorAll('[data-testid="image-gallery"] img');
    expect(images.length).toBe(1);
    expect(images[0].getAttribute('src')).toBe('https://cdn.example.com/person_0000.jpg');
    expect(screen.getByText('facemetadata')).toBeTruthy();
  });

  it('falls back to manifest sources when no artifacts are declared', () => {
    const legacy = {
      id: 'x',
      template: 'generic-preview',
      duration: 0.001,
      tracks: [],
      clips: [],
      createdAt: '2026-07-30T01:00:00.000Z',
      sources: ['https://cdn.example.com/debug/people/person_0001.jpg'],
    } as unknown as RenderManifest;

    render(<GenericPreview data={{ manifest: legacy }} title="Artifacts" theme={theme} />);
    expect(document.querySelectorAll('[data-testid="image-gallery"] img').length).toBe(1);
  });
});
