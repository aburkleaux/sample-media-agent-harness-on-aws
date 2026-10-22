// @vitest-environment jsdom
/**
 * Lineage spec R8: ManifestRouter is where a tab becomes a PERSISTED view.
 *
 *  - A tab addressed by a session render-manifest URL (artifacts/<sid>/render/<id>.json) hands its
 *    template the stored `viewState` and a change reporter; changes are PATCHed debounced 1 s.
 *  - A tab with no such URL (an inline composite, a deep link) gets neither.
 *  - A persisted composite tab whose inline manifest was stripped (leanRenderHistory) shows a loading
 *    state until the render-list merge re-supplies it — never "No manifest URL provided".
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { theme } from '../../styles/tokens';

const patchSessionRenderView = vi.hoisted(() => vi.fn());
vi.mock('../../lab-api', () => ({
  getConfig: async () => ({ artifactsDomain: '' }),
  patchSessionRenderView,
}));
vi.mock('../../auth/authFetch', () => ({ authFetch: vi.fn() }));

const seen = vi.hoisted(() => ({ data: [] as any[] }));
const last = () => seen.data[seen.data.length - 1];
vi.mock('./templates/NleTimeline', () => ({
  default: (props: { data: any }) => { seen.data.push(props.data); return <div data-testid="tpl-timeline" />; },
}));

import ManifestRouter, { persistedViewOf } from './templates/ManifestRouter';
import { resetViewStatePersist, VIEW_STATE_DEBOUNCE_MS } from './viewStatePersist';

const KEY = 'artifacts/sess-1/render/view-1.json';
const URL_ = `/api/artifact?key=${encodeURIComponent(KEY)}`;
const manifest = {
  id: 'view-1', createdAt: '2026-10-08T00:00:00Z', template: 'nle-timeline', duration: 10,
  tracks: [{ id: 'v', type: 'video', src: 'https://cdn.example/v.mp4', start: 0, end: 10 }], clips: [],
  viewState: { range: { start: 1, end: 4 } },
};

beforeEach(() => {
  seen.data = [];
  patchSessionRenderView.mockReset();
  patchSessionRenderView.mockResolvedValue(undefined);
  resetViewStatePersist();
});
afterEach(() => { vi.useRealTimers(); });

describe('persistedViewOf', () => {
  it('recognises a session render-manifest URL only', () => {
    expect(persistedViewOf(URL_)).toEqual({ sessionId: 'sess-1', key: KEY });
    expect(persistedViewOf('/api/artifact?key=artifacts%2Fsess-1%2Foutput%2Fx.json')).toBeNull();
    expect(persistedViewOf('/api/artifact?key=artifacts%2F..%2Frender%2Fx.json')).toBeNull();
    expect(persistedViewOf(undefined)).toBeNull();
  });
});

describe('ManifestRouter — persisted view state', () => {
  it('hands a persisted view its stored state and PATCHes changes once, 1 s after the last', async () => {
    vi.useFakeTimers();
    render(<ManifestRouter data={{ manifest: manifest as never, manifestUrl: URL_ }} title="v" theme={theme} />);
    const data = last();
    expect(data.viewState).toEqual({ range: { start: 1, end: 4 } });
    expect(typeof data.onViewStateChange).toBe('function');

    data.onViewStateChange({ range: { start: 2, end: 4 } });
    data.onViewStateChange({ range: { start: 2, end: 5 }, frameMs: 3000 });
    await act(async () => { vi.advanceTimersByTime(VIEW_STATE_DEBOUNCE_MS - 1); });
    expect(patchSessionRenderView).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(patchSessionRenderView).toHaveBeenCalledTimes(1);
    expect(patchSessionRenderView).toHaveBeenCalledWith('sess-1', KEY, {
      range: { start: 2, end: 5 }, frameMs: 3000, crop: null, search: null,
    });
  });

  it('a remount restores the latest local state, not the manifest’s older copy', () => {
    const first = render(<ManifestRouter data={{ manifest: manifest as never, manifestUrl: URL_ }} title="v" theme={theme} />);
    last().onViewStateChange({ frameMs: 7000 });
    first.unmount();
    render(<ManifestRouter data={{ manifest: manifest as never, manifestUrl: URL_ }} title="v" theme={theme} />);
    expect(last().viewState).toEqual({ frameMs: 7000 });
  });

  it('an inline tab with no render-manifest URL is not a persisted view', () => {
    render(<ManifestRouter data={{ manifest: manifest as never }} title="v" theme={theme} />);
    expect(last().onViewStateChange).toBeUndefined();
  });

  it('the login-free deep link never persists', () => {
    render(<ManifestRouter data={{ manifest: manifest as never, manifestUrl: URL_ }} title="v" theme={theme} resolveArtifacts={false} />);
    expect(last().onViewStateChange).toBeUndefined();
  });
});

describe('ManifestRouter — persisted composite without its inline manifest', () => {
  it('shows a loading state for a known composite, not the missing-URL error', () => {
    render(
      <ManifestRouter
        data={{ sourceKey: 's/v.mp4', compositeKey: 'composite:s/v.mp4', memberKeys: ['a', 'b'] } as never}
        title="v"
        theme={theme}
      />,
    );
    expect(screen.getByTestId('manifest-router-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('manifest-router-error')).toBeNull();
  });

  it('still reports the error for a block that names nothing', async () => {
    render(<ManifestRouter data={{}} title="v" theme={theme} />);
    expect(await screen.findByTestId('manifest-router-error')).toHaveTextContent('No manifest URL provided');
  });
});
