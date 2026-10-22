/**
 * @vitest-environment jsdom
 *
 * ONE container for the time axis.
 *
 * `video-markers` used to be its own view: a <video> plus a clickable LIST of shots — a strict
 * subset of the timeline's data (`src` + `markers`) with no playhead, no zoom and no shared
 * gutter. Worse, the SAME analysis looked different depending on whether a sibling manifest
 * happened to render (alone: the list; composited: a timeline), which is precisely what staging
 * session 9e977f30 reported. The name is now an ALIAS for the timeline container, so stored
 * manifests keep resolving while new renders declare `nle-timeline`.
 *
 * Two properties are pinned here because they are what make the fold safe:
 *   1. a manifest carrying only a top-level `src` (what the old template read) still PLAYS; and
 *   2. an analysis that produced NO lanes says so, instead of silently looking like a plain video.
 */
import { describe, it, expect } from 'vitest';
import '@testing-library/jest-dom';
import { render } from '@testing-library/react';
import { lookupRenderer } from './RendererRegistry';
import NleTimeline from './templates/NleTimeline';
import { theme } from '../../styles/tokens';

function view(manifest: Record<string, unknown>) {
  return render(<NleTimeline data={{ manifest } as never} title="Result" theme={theme} />);
}

describe('template aliasing', () => {
  it('routes video-markers to the timeline container (stored manifests keep working)', () => {
    expect(lookupRenderer('video-markers')).toBe(NleTimeline);
  });

  it('keeps nle-timeline and composite-timeline on the same container', () => {
    expect(lookupRenderer('nle-timeline')).toBe(NleTimeline);
    expect(lookupRenderer('composite-timeline')).toBe(NleTimeline);
  });

  it('does NOT absorb the non-temporal templates — they have no time axis', () => {
    // Session 7cf83ef3: forcing galleries into a timeline produced an empty
    // "No media source available" tab. Different axis, different container.
    for (const t of ['image-annotated', 'data-table', 'document-viewer']) {
      expect(lookupRenderer(t), t).not.toBe(NleTimeline);
    }
  });

  it('folds the box overlay in too — the container draws boxes AND lanes', () => {
    // Corrected: the container already had the frame overlay (activeBoxesAt + per-box label +
    // a Faces toggle) plus appearance lanes on the shared gutter, so face results belong here.
    // The dedicated component's extras (per-person colour, thumbnails, per-person hide) were
    // dropped by decision — see unifiedFaceTemplate.test.tsx.
    expect(lookupRenderer('video-annotations')).toBe(NleTimeline);
  });
});

describe('a manifest with only a top-level src still plays', () => {
  const srcOnly = {
    template: 'video-markers',
    duration: 30,
    src: 'https://cdn.example/clip.mp4',
    mimeType: 'video/mp4',
    tracks: [],
    clips: [],
    markers: [{ id: 'm0', time: 1, label: '0', category: 'black' }],
  };

  it('synthesizes a video track from src (was "No media source available")', () => {
    const { container } = view(srcOnly);
    expect(container.querySelector('video')?.getAttribute('src')).toBe('https://cdn.example/clip.mp4');
    expect(container.textContent).not.toContain('No media source available');
    expect(container.textContent).not.toContain('No media tracks available');
  });

  it('uses an audio element when the mime says audio', () => {
    const { container } = view({ ...srcOnly, src: 'https://cdn.example/a.wav', mimeType: 'audio/wav' });
    expect(container.querySelector('audio')).toBeTruthy();
    expect(container.querySelector('video')).toBeNull();
  });

  it('refuses a non-playable mime — a document must never become a player', () => {
    const { container } = view({ ...srcOnly, src: 'https://cdn.example/report.pdf', mimeType: 'application/pdf' });
    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('audio')).toBeNull();
  });

  it('prefers declared tracks over the loose src when both exist', () => {
    const { container } = view({
      ...srcOnly,
      tracks: [{ id: 't1', type: 'video', src: 'https://cdn.example/declared.mp4', start: 0, end: 30 }],
    });
    expect(container.querySelector('video')?.getAttribute('src')).toContain('declared.mp4');
  });
});

describe('an empty analysis is stated, not hidden', () => {
  const base = { template: 'nle-timeline', duration: 30, tracks: [
    { id: 't1', type: 'video', src: 'https://cdn.example/v.mp4', start: 0, end: 30 },
  ], clips: [] };

  it('flags an analysis render that produced no lanes', () => {
    const { getByTestId } = view({ ...base, renderSkill: 'render-shot-segmentation', markers: [] });
    expect(getByTestId('analysis-no-lanes')).toBeInTheDocument();
  });

  it('stays silent for a plain media render, which legitimately has no lanes', () => {
    const { queryByTestId } = view({ ...base, renderSkill: 'render-video' });
    expect(queryByTestId('analysis-no-lanes')).toBeNull();
  });

  it('stays silent when the analysis DID produce lanes', () => {
    const { queryByTestId } = view({
      ...base, renderSkill: 'render-shot-segmentation', markers: [{ id: 'm', time: 2, label: '0' }],
    });
    expect(queryByTestId('analysis-no-lanes')).toBeNull();
  });

  it('counts grouped segments and face boxes as lanes too', () => {
    const seg = view({ ...base, renderSkill: 'render-speaker-diarization',
                       segments: [{ id: 's', start: 0, end: 5, group: 'SPEAKER_00' }] });
    expect(seg.queryByTestId('analysis-no-lanes')).toBeNull();
    const box = view({ ...base, renderSkill: 'render-face-recognition',
                       annotations: [{ entityId: 'p1', start: 0, end: 2, box: { x: 0.1, y: 0.1, width: 0.2, height: 0.2 } }] });
    expect(box.queryByTestId('analysis-no-lanes')).toBeNull();
  });

  it('the media still plays when the notice shows — graceful, not broken', () => {
    const { container, getByTestId } = view({ ...base, renderSkill: 'render-shot-segmentation', markers: [] });
    expect(getByTestId('analysis-no-lanes')).toBeInTheDocument();
    expect(container.querySelector('video')?.getAttribute('src')).toContain('v.mp4');
  });

  it('says nothing when there is no renderSkill to judge by', () => {
    const { queryByTestId } = view(base);
    expect(queryByTestId('analysis-no-lanes')).toBeNull();
  });
});
