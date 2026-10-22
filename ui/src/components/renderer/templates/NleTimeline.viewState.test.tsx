/**
 * @vitest-environment jsdom
 *
 * R8.2: a persisted view restores its timeline state on mount — the selected range, the frame
 * marker and the crop box — and reports every change (ManifestRouter PATCHes it, debounced). The
 * restore itself must never be reported back as a change.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import NleTimeline from './NleTimeline';
import { theme } from '../../../styles/tokens';
import type { RenderManifest } from '../../nle/manifestParser';
import type { RenderViewState } from '../../../shared/renderViewState';

const manifest = {
  id: 'view-1',
  template: 'nle-timeline',
  duration: 60,
  tracks: [{ id: 'v1', type: 'video', src: 'https://cdn.example/x.mp4', start: 0, end: 60 }],
  clips: [],
} as unknown as RenderManifest;

beforeEach(() => { localStorage.clear(); });

function mount(viewState: RenderViewState | undefined, onViewStateChange = vi.fn(), m: RenderManifest = manifest) {
  const utils = render(
    <NleTimeline data={{ manifestUrl: '', manifest: m, viewState, onViewStateChange }} title="v" theme={theme} />,
  );
  return { ...utils, onViewStateChange };
}

describe('NleTimeline — persisted view state', () => {
  it('restores the range and the frame marker, and does not report the restore as a change', () => {
    const { container, getByTestId, onViewStateChange } = mount({ range: { start: 5, end: 12 }, frameMs: 20_000 });
    expect(container.querySelectorAll('[data-testid^="range-band-"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-testid^="point-remove-"]')).toHaveLength(1);
    expect(getByTestId('selection-summary')).toHaveTextContent('1 range · 1 marker');
    expect(onViewStateChange).not.toHaveBeenCalled();
  });

  it('restores the crop box by reopening the crop overlay', () => {
    const { getByTestId } = mount({ crop: { x: 0.1, y: 0.1, w: 0.5, h: 0.5 } });
    expect(getByTestId('crop-overlay')).toBeInTheDocument();
    expect(getByTestId('toggle-crop')).toHaveAttribute('aria-pressed', 'true');
  });

  it('the Crop toggle draws an SVG icon in both states and its label never changes (no ✂▢ / ✕ glyphs)', () => {
    const { getByTestId } = mount(undefined);
    const toggle = getByTestId('toggle-crop');
    expect(toggle.querySelector('svg')).not.toBeNull();
    expect(toggle).toHaveTextContent(/^Crop$/);
    act(() => { fireEvent.click(toggle); });
    expect(getByTestId('toggle-crop').querySelector('svg')).not.toBeNull();
    expect(getByTestId('toggle-crop')).toHaveTextContent(/^Crop$/);
  });

  it('reports a change with the full state, and a cleared selection as an empty state', () => {
    const { getByTestId, onViewStateChange } = mount({ range: { start: 5, end: 12 }, frameMs: 20_000 });
    fireEvent.click(getByTestId('selection-clear'));
    expect(onViewStateChange).toHaveBeenLastCalledWith({});
  });

  it('closing a restored crop clears it from the view state', () => {
    const { getByTestId, onViewStateChange } = mount({ range: { start: 1, end: 2 }, crop: { x: 0.1, y: 0.1, w: 0.5, h: 0.5 } });
    act(() => { fireEvent.click(getByTestId('toggle-crop')); });
    expect(onViewStateChange).toHaveBeenLastCalledWith({ range: { start: 1, end: 2 } });
  });

  it('falls back to the manifest’s own viewState, and restores it instead of the per-browser marks', () => {
    localStorage.setItem('nle-edits-view-1', JSON.stringify({ ranges: [{ in: 30, out: 40 }], points: [] }));
    const m = { ...(manifest as object), viewState: { range: { start: 2, end: 3 } } } as unknown as RenderManifest;
    const { container } = render(<NleTimeline data={{ manifestUrl: '', manifest: m }} title="v" theme={theme} />);
    expect(container.querySelectorAll('[data-testid^="range-band-"]')).toHaveLength(1);
  });

  // AssetPlayer's inline manifest (an Expand / openAssetInPanel view) carries `duration: 0` +
  // `durationIsEstimate`: the timeline learns its length only from the video element. Staging
  // 2026-10-09 (session 9d649e32): after a reload the range was dropped and the marker landed at 0,
  // because the restore ran against a 0-length engine, and that clamped state was PATCHed back.
  it('restores a measured-duration view only once the media length is known (Expand view)', () => {
    localStorage.setItem('nle-edits-asset-a1', JSON.stringify({ ranges: [{ in: 5, out: 12 }], points: [{ t: 20 }] }));
    const estimate = {
      id: 'asset-a1',
      template: 'nle-timeline',
      duration: 0,
      durationIsEstimate: true,
      tracks: [{ id: 'src', type: 'video', src: 'https://cdn.example/x.mp4', start: 0, end: 0 }],
      clips: [],
    } as unknown as RenderManifest;
    const { container, onViewStateChange } = mount({ range: { start: 5, end: 12 }, frameMs: 20_000 }, vi.fn(), estimate);
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(video).not.toBeNull();
    // Nothing restored (or reported) before the length is known.
    expect(container.querySelectorAll('[data-testid^="point-remove-"]')).toHaveLength(0);
    Object.defineProperty(video, 'duration', { configurable: true, value: 28.6 });
    act(() => { fireEvent(video, new Event('loadedmetadata')); fireEvent(video, new Event('durationchange')); });
    expect(container.querySelectorAll('[data-testid^="range-band-"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-testid^="point-remove-"]')).toHaveLength(1);
    expect(onViewStateChange).not.toHaveBeenCalled();
  });

  // The reload case itself: the per-browser render cache hands ManifestRouter the Expand view's
  // INLINE manifest (no viewState), so only the per-browser marks are there to restore. They must
  // wait for the measured length too — restored against a 0-length engine the range was rejected
  // and the marker clamped to 0, and that was PATCHed over the stored view.
  it('restores per-browser marks only once the media length is known, and never reports a clamped state', () => {
    localStorage.setItem('nle-edits-asset-a1', JSON.stringify({ ranges: [{ in: 5, out: 12 }], points: [{ t: 20 }] }));
    const estimate = {
      id: 'asset-a1',
      template: 'nle-timeline',
      duration: 0,
      durationIsEstimate: true,
      tracks: [{ id: 'src', type: 'video', src: 'https://cdn.example/x.mp4', start: 0, end: 0 }],
      clips: [],
    } as unknown as RenderManifest;
    const { container, onViewStateChange } = mount(undefined, vi.fn(), estimate);
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(container.querySelectorAll('[data-testid^="point-remove-"]')).toHaveLength(0);
    Object.defineProperty(video, 'duration', { configurable: true, value: 28.6 });
    act(() => { fireEvent(video, new Event('durationchange')); });
    act(() => { fireEvent(video, new Event('loadedmetadata')); });
    expect(container.querySelectorAll('[data-testid^="range-band-"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-testid^="point-remove-"]')).toHaveLength(1);
    for (const [vs] of onViewStateChange.mock.calls) {
      expect(vs).toEqual({ range: { start: 5, end: 12 }, frameMs: 20_000 });
    }
  });

  it('ignores an invalid persisted state', () => {
    const { container, onViewStateChange } = mount({ range: { start: 9, end: 3 } } as RenderViewState);
    expect(container.querySelector('[data-testid^="range-band-"]')).toBeNull();
    expect(onViewStateChange).not.toHaveBeenCalled();
  });
});
