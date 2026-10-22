/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { theme } from '../../styles/tokens';
import { resolveTemplate } from './templates/ManifestRouter';
import DataTable from './templates/DataTable';
import ImageAnnotated from './templates/ImageAnnotated';

/**
 * Two kinds of template share the registry, and the difference is invisible at the
 * name level:
 *
 *   manifest-shaped  — NleTimeline (every time-based template now routes here): reads
 *                      `data.manifest`
 *   direct-prop      — DataTable, ImageAnnotated: read `data.rows`, `data.src` …
 *
 * ManifestRouter used to pass only `{ manifest }`, so routing a manifest to the
 * second group left every prop undefined: the template resolves, mounts, and draws
 * nothing. No error, no failing test — the exact failure mode that made the render
 * agent look broken. These tests feed each template the shape the router actually
 * supplies and assert something real is rendered.
 */

// Mirrors ManifestRouter: both shapes, so either kind of template is satisfied.
const asRouterData = (manifest: Record<string, unknown>) => ({ ...manifest, manifest });

describe('manifest → template props', () => {
  it('a video-markers manifest routes to the timeline container and renders its shots', () => {
    const manifest = {
      template: 'video-markers',
      src: 'https://x/segment_0000.mp4',
      duration: 30.03,
      markers: [
        { id: 'm-0', time: 0, endTime: 0.968, label: '0', category: 'black' },
        { id: 'm-1', time: 1.001, endTime: 5.172, label: '1', category: 'content' },
      ],
      tracks: [], clips: [],
    };
    // `video-markers` is an alias for the timeline container now, and this manifest is the
    // shape that template used to emit: a top-level `src` with NO tracks. The container
    // synthesizes a track from it, which is what keeps stored manifests playable.
    const Template = resolveTemplate(manifest.template);
    const { container } = render(
      <Template data={asRouterData(manifest) as never} title="Shots" theme={theme} />,
    );
    const video = container.querySelector('video');
    expect(video).toBeTruthy();
    expect(video?.getAttribute('src')).toContain('segment_0000.mp4');
    // Every shot is on the shared gutter, and each carries its type in its tooltip
    // (the old template printed them as text rows).
    expect(container.textContent).toContain('Shots · 2');
    const titles = Array.from(container.querySelectorAll('button[title]'))
      .map((b) => b.getAttribute('title') ?? '');
    expect(titles.some((t) => t.includes('black'))).toBe(true);
    expect(titles.some((t) => t.includes('content'))).toBe(true);
  });

  it('data-table receives columns and rows from the manifest', () => {
    const manifest = {
      template: 'data-table',
      columns: [{ key: 'name', label: 'Name' }, { key: 'age_range', label: 'Age Range' }],
      rows: [{ name: 'Kawhi Leonard', age_range: '28–36' }],
      tracks: [], clips: [],
    };
    const { getByText } = render(
      <DataTable data={asRouterData(manifest) as never} title="People" theme={theme} />,
    );
    expect(getByText('Kawhi Leonard')).toBeTruthy();
  });

  it('image-annotated receives src and annotations from the manifest', () => {
    const manifest = {
      template: 'image-annotated',
      src: 'https://x/frame_0000900.jpg',
      annotations: [{ x: 30.488, y: 17.364, w: 14.083, h: 35.581, label: 'Male' }],
      images: [{ src: 'https://x/frame_0000900.jpg', label: 'frame_0000900.jpg', detections: 1 }],
      tracks: [], clips: [],
    };
    const { container } = render(
      <ImageAnnotated data={asRouterData(manifest) as never} title="Faces" theme={theme} />,
    );
    expect((container.querySelector('img') as HTMLImageElement).src).toContain('frame_0000900.jpg');
    expect(container.querySelectorAll('div[style*="position: absolute"]').length).toBe(1);
  });

  it('a manifest-only shape breaks a direct-prop template — the bug this guards', () => {
    // Demonstrates why the spread is required. Worse than rendering blank: with
    // just `{ manifest }`, DataTable dereferences undefined rows and throws.
    const manifest = {
      template: 'data-table',
      columns: [{ key: 'name', label: 'Name' }],
      rows: [{ name: 'Kawhi Leonard' }],
    };
    expect(() =>
      render(<DataTable data={{ manifest } as never} title="People" theme={theme} />),
    ).toThrow();
  });

  it('the router supplies both shapes', async () => {
    const src = await import('node:fs').then((fs) =>
      fs.readFileSync('src/components/renderer/templates/ManifestRouter.tsx', 'utf8'));
    expect(src).toContain('{ ...state.manifest, manifest: state.manifest }');
  });
});
