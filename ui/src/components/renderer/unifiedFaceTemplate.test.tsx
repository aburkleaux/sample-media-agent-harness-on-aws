/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render, act } from '@testing-library/react';
import { theme } from '../../styles/tokens';
import { resolveTemplate } from './templates/ManifestRouter';
import NleTimeline from './templates/NleTimeline';

/**
 * ONE container for both face skills — and for every other time-based analysis.
 *
 * History matters here. Selection between render-face-people and render-face-recognition is
 * deterministic and scored, so whichever declared more projections won, and only one of them
 * could draw boxes — a live session showed a face tab with no boxes for exactly that reason.
 * An earlier attempt pointed BOTH skills at the dedicated `video-annotations` component and
 * made things worse on real data: the manifest arrived with every list empty because the
 * correlator had already routed the payload into a separate composite-timeline manifest. A
 * guaranteed blank tab. The lesson recorded then still holds: a template swap does not move
 * the data with it.
 *
 * What changed now is the container, not the routing. `video-annotations` is an alias for the
 * timeline container, which draws boxes ON the frame (activeBoxesAt + per-box label + a Faces
 * toggle) AND puts each appearance on the shared time gutter — the two things a face result
 * needs: where in the picture, and when.
 *
 * DROPPED DELIBERATELY with the old component (per product decision): a distinct colour per
 * person, face thumbnails on subject cards, per-person show/hide (one global Faces toggle
 * remains), the subject list shown when the video itself fails to load, and the raw-artifact
 * links behind the render. Recorded here so the loss is a decision on the record rather than
 * a discovery later.
 */
const routerData = (m: Record<string, unknown>) => ({ ...m, manifest: m });

function renderManifest(m: Record<string, unknown>) {
  const Template = resolveTemplate(m.template as string);
  return render(<Template data={routerData(m) as never} title="Faces" theme={theme} />);
}

const VIDEO = 'https://cdn.example/proxy/transcoded.mp4';

// What render-face-recognition produces: boxes carrying entityId. Box starts at t=0 so it is
// active on mount (activeBoxesAt is time-gated — that IS the feature).
const RECOGNITION = {
  template: 'video-annotations',
  src: VIDEO,
  duration: 300.1,
  mimeType: 'video/mp4',
  tracks: [{ id: 'v', type: 'video', src: VIDEO, start: 0, end: 300.1 }],
  clips: [],
  entities: [{ id: 'p0', label: 'Speaker A' }],
  annotations: [
    { entityId: 'p0', start: 0, end: 2.0, box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 } },
  ],
};

// What render-face/people produces: lanes, NO entities and NO boxes.
const PEOPLE = {
  template: 'video-annotations',
  src: VIDEO,
  duration: 300.1,
  mimeType: 'video/mp4',
  tracks: [{ id: 'v', type: 'video', src: VIDEO, start: 0, end: 300.1 }],
  clips: [],
  segments: [
    { id: 'l-0-0', name: 'James', group: 'James', start: 12.5, end: 18.0 },
    { id: 'l-0-1', name: 'James', group: 'James', start: 40.0, end: 44.2 },
    { id: 'l-1-0', name: 'Ada', group: 'Ada', start: 60.0, end: 63.0 },
  ],
};

describe('one container serves both face skills', () => {
  it('routes video-annotations to the timeline container', () => {
    expect(resolveTemplate('video-annotations')).toBe(NleTimeline);
  });

  it('a recognition payload plays the video and draws its box on the frame', async () => {
    const { container } = renderManifest(RECOGNITION);
    expect(container.querySelector('video')).toBeTruthy();
    await act(async () => { await Promise.resolve(); });
    // The box is positioned from the manifest's normalized coords.
    const box = container.querySelector('[data-testid="face-box-p0"]') as HTMLElement | null;
    expect(box, 'no face box drawn for p0').toBeTruthy();
    expect(box!.style.left).toBe('10%');
    expect(box!.style.width).toBe('20%');
    // And the overlay can be toggled.
    expect(container.querySelector('[data-testid="toggle-faces"]')).toBeTruthy();
  });

  it('a people payload renders its subjects as lanes, not a blank panel', () => {
    // No entities and no boxes — the subjects live in grouped segments.
    expect((PEOPLE as { entities?: unknown }).entities).toBeUndefined();
    expect((PEOPLE as { annotations?: unknown }).annotations).toBeUndefined();
    const { container } = renderManifest(PEOPLE);
    expect(container.querySelector('video')).toBeTruthy();
    expect(container.textContent).toContain('James');
    expect(container.textContent).toContain('Ada');
  });

  it('neither payload renders an empty view, and neither is flagged as lane-less', () => {
    for (const m of [RECOGNITION, PEOPLE]) {
      const { container, queryByTestId } = renderManifest(m);
      expect(container.textContent).not.toContain('No media source available');
      // Both DID produce something on the timeline, so the empty-analysis notice must stay away.
      expect(queryByTestId('analysis-no-lanes')).toBeNull();
    }
  });
});
