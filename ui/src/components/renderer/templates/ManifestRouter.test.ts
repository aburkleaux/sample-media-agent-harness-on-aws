import { describe, expect, it } from 'vitest';
import { rendererForManifest } from './ManifestRouter';
import NleTimeline from './NleTimeline';
import DocumentViewer from './DocumentViewer';
import GenericPreview from './GenericPreview';
import type { RenderManifest } from '../../nle/manifestParser';

function manifest(extra: Record<string, unknown>): RenderManifest {
  return {
    id: '550e8400-e29b-41d4-a716-446655440000',
    duration: 60,
    tracks: [],
    clips: [],
    createdAt: '2026-07-30T00:00:00.000Z',
    ...extra,
  } as RenderManifest;
}

describe('rendererForManifest', () => {
  it('routes timeline manifests to the timeline renderer', () => {
    expect(rendererForManifest(manifest({ template: 'nle-timeline' }))).toBe(NleTimeline);
    expect(rendererForManifest(manifest({ template: 'composite-timeline' }))).toBe(NleTimeline);
  });

  it('routes document manifests to the document viewer', () => {
    expect(rendererForManifest(manifest({ template: 'document-viewer' }))).toBe(DocumentViewer);
  });

  it('routes annotation manifests to the overlay renderer', () => {
    // Folded: `video-annotations` is an alias for the one timeline container, which draws the
    // boxes on the frame AND the appearance lanes on the shared gutter.
    expect(rendererForManifest(manifest({ template: 'video-annotations' }))).toBe(NleTimeline);
  });

  it('routes the generic-preview shape the Render Agent produced for face results', () => {
    // This is the exact defect this router fixes: the manifest below previously
    // went through NleTimeline and rendered "No media source available".
    const generic = manifest({
      template: 'generic-preview',
      duration: 0.001,
      sources: [
        'https://example.com/facemetadata.json',
        'https://example.com/debug/people/person_0000.jpg',
      ],
    });
    expect(rendererForManifest(generic)).toBe(GenericPreview);
  });

  it('falls back to the timeline when an unknown template still has media tracks', () => {
    const withTracks = manifest({
      template: 'future-template',
      tracks: [{ id: 'v', type: 'video', src: 'https://example.com/a.mp4', start: 0, end: 60 }],
    });
    expect(rendererForManifest(withTracks)).toBe(NleTimeline);
  });

  it('falls back to the artifact preview when an unknown template has no tracks', () => {
    expect(rendererForManifest(manifest({ template: 'future-template' }))).toBe(GenericPreview);
    expect(rendererForManifest(manifest({}))).toBe(GenericPreview);
  });
});
