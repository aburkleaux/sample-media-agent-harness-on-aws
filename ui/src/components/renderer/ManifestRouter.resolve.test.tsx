// @vitest-environment jsdom
/**
 * ManifestRouter resolves every artifact identifier in a manifest to a presigned URL ONCE, before
 * any template sees it, and hosts the single media re-sign listener. Interaction payloads are
 * mapped back to identifiers so an expiring URL never leaves the renderer.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { theme } from '../../styles/tokens';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../../auth/authFetch', () => ({ authFetch }));
vi.mock('../../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

const seen = vi.hoisted(() => ({ props: [] as Array<{ data: any; onInteraction?: (e: any) => void }> }));
vi.mock('./templates/NleTimeline', () => ({
  default: (props: { data: any; onInteraction?: (e: any) => void }) => {
    seen.props.push(props);
    return <video data-testid="tpl-video" src={props.data.manifest.tracks[0].src} />;
  },
}));
vi.mock('./templates/DocumentViewer', () => ({
  default: (props: { data: any }) => {
    seen.props.push(props);
    return <div data-testid="tpl-doc">{props.data.manifest.document.previewUrl}</div>;
  },
}));

import ManifestRouter from './templates/ManifestRouter';
import { __resetArtifactUrlsForTests } from '../../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../../artifacts/testPresigner';

const CDN = 'https://d1abcd.cloudfront.net';
const MANIFEST_URL = '/api/artifact?key=artifacts%2Fsess-1%2Fmanifest.json';
let presigner: FakePresigner;

const timeline = {
  id: 'm-1',
  createdAt: '2026-09-20T00:00:00Z',
  template: 'nle-timeline',
  sourceKey: 'artifacts/sess-1/source.mp4',
  duration: 10,
  tracks: [
    { id: 'v', type: 'video', src: `${CDN}/artifacts/sess-1/source.mp4`, start: 0, end: 10 },
    { id: 'p', type: 'video', src: 'https://m2c.s3.amazonaws.com/p.mp4?X-Amz-Signature=keep', start: 0, end: 10 },
  ],
  clips: [],
};

function serve(manifest: unknown) {
  authFetch.mockImplementation(async (input: string, init?: RequestInit) => {
    if (input === MANIFEST_URL) return new Response(JSON.stringify(manifest), { status: 200 });
    return presigner.handle(input, init);
  });
}

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  seen.props = [];
});

describe('ManifestRouter — artifact URL resolution', () => {
  it('passes a fetched manifest to the template with artifact URLs presigned (and nothing else touched)', async () => {
    serve(timeline);
    render(<ManifestRouter data={{ manifestUrl: MANIFEST_URL }} title="R" theme={theme} />);
    const video = await screen.findByTestId('tpl-video');
    expect(video).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/source.mp4'));

    // Every render the template saw had resolved data — it never received the raw CDN URL.
    for (const p of seen.props) {
      expect(p.data.manifest.tracks[0].src).toBe(presigner.urlFor('artifacts/sess-1/source.mp4'));
      expect(p.data.tracks[0].src).toBe(presigner.urlFor('artifacts/sess-1/source.mp4'));
      expect(p.data.manifest.tracks[1].src).toBe(timeline.tracks[1].src);
      expect(p.data.sourceKey).toBe('artifacts/sess-1/source.mp4');
    }
    // The manifest itself was fetched through authFetch (not resolved), plus one presign batch.
    expect(authFetch.mock.calls.filter(([u]) => u === MANIFEST_URL)).toHaveLength(1);
    expect(presigner.calls).toEqual([{ keys: ['artifacts/sess-1/source.mp4'], download: false }]);
    // The re-sign host wraps the template.
    expect(screen.getByTestId('manifest-router-host')).toContainElement(video);
  });

  it('resolves an inline (composite) manifest too', async () => {
    authFetch.mockImplementation(presigner.handle);
    render(<ManifestRouter data={{ manifest: timeline as never }} title="R" theme={theme} />);
    await waitFor(() =>
      expect(screen.getByTestId('tpl-video')).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/source.mp4')));
  });

  it('keeps the template rendering on the original URLs when resolution fails', async () => {
    presigner.failStatus = 500;
    serve(timeline);
    render(<ManifestRouter data={{ manifestUrl: MANIFEST_URL }} title="R" theme={theme} />);
    expect(await screen.findByTestId('tpl-video')).toHaveAttribute('src', `${CDN}/artifacts/sess-1/source.mp4`);
  });

  it('leaves document.previewUrl for DocumentViewer (its /api/artifact + access_token path)', async () => {
    const doc = { id: 'd-1', createdAt: '2026-09-20T00:00:00Z', template: 'document-viewer', document: { previewUrl: `${CDN}/artifacts/sess-1/r.pdf` }, tracks: [] };
    serve(doc);
    render(<ManifestRouter data={{ manifestUrl: MANIFEST_URL }} title="R" theme={theme} />);
    expect(await screen.findByTestId('tpl-doc')).toHaveTextContent(`${CDN}/artifacts/sess-1/r.pdf`);
    expect(presigner.calls).toHaveLength(0);
  });

  it('maps presigned URLs in interaction payloads back to the identifier', async () => {
    serve(timeline);
    const onInteraction = vi.fn();
    render(<ManifestRouter data={{ manifestUrl: MANIFEST_URL }} title="R" theme={theme} onInteraction={onInteraction} />);
    await screen.findByTestId('tpl-video');
    const last = seen.props[seen.props.length - 1];
    const presigned = presigner.urlFor('artifacts/sess-1/source.mp4');
    last.onInteraction!({ type: 'action', target: 'crop', data: { src: presigned, prompt: `crop [source: ${presigned}]` }, timestamp: 1 });
    expect(onInteraction).toHaveBeenCalledWith({
      type: 'action',
      target: 'crop',
      data: {
        src: `${CDN}/artifacts/sess-1/source.mp4`,
        prompt: `crop [source: ${CDN}/artifacts/sess-1/source.mp4]`,
      },
      timestamp: 1,
    });
  });

  it('does not invent an onInteraction for a host that passed none', async () => {
    serve(timeline);
    render(<ManifestRouter data={{ manifestUrl: MANIFEST_URL }} title="R" theme={theme} />);
    await screen.findByTestId('tpl-video');
    expect(seen.props[seen.props.length - 1].onInteraction).toBeUndefined();
  });

  it('resolveArtifacts={false} (login-free deep link) never calls the authenticated presign route', async () => {
    authFetch.mockImplementation(presigner.handle);
    render(<ManifestRouter data={{ manifest: timeline as never }} title="R" theme={theme} resolveArtifacts={false} />);
    const video = screen.getByTestId('tpl-video');
    expect(video).toHaveAttribute('src', `${CDN}/artifacts/sess-1/source.mp4`);
    video.dispatchEvent(new Event('error', { bubbles: false }));
    await new Promise((r) => setTimeout(r, 0));
    expect(authFetch).not.toHaveBeenCalled();
  });
});
