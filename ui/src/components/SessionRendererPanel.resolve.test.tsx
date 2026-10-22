// @vitest-environment jsdom
/**
 * The second render host (RendererRegistry path): registry templates receive `block.data` with
 * artifact identifiers presigned, a `custom` document block frames a presigned URL instead of the
 * bare `/api/artifact`, media errors are re-signed by the panel's one listener, and interaction
 * payloads (→ copy source URL) are mapped back to stable identifiers.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

const seen = vi.hoisted(() => ({ props: [] as Array<{ data: any; onInteraction?: (e: any) => void }> }));
vi.mock('./renderer/RendererRegistry', () => ({
  lookupRenderer: (template: string) =>
    template === 'image-annotated'
      ? (props: { data: any; onInteraction?: (e: any) => void }) => {
          seen.props.push(props);
          return (
            <div>
              <img data-testid="tpl-img" src={props.data.src} alt="" />
              <button
                type="button"
                onClick={() => props.onInteraction?.({ type: 'select', target: 'g', data: { url: props.data.src }, timestamp: 1 })}
              >
                pick
              </button>
            </div>
          );
        }
      : null,
}));

import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';
import { __resetArtifactUrlsForTests } from '../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../artifacts/testPresigner';

const CDN = 'https://d1abcd.cloudfront.net';
let presigner: FakePresigner;
const writeText = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
  seen.props = [];
  writeText.mockClear();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
});

function entry(block: RenderHistoryEntry['blocks'][number]): RenderHistoryEntry {
  return { turnIndex: 0, timestamp: 1, label: 'Tab', blocks: [block] } as RenderHistoryEntry;
}

function panel(e: RenderHistoryEntry, onInteraction = vi.fn()) {
  return render(
    <SessionRendererPanel history={[e]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} onInteraction={onInteraction} />,
  );
}

describe('SessionRendererPanel — artifact URL resolution', () => {
  it('hands a registry template resolved block.data (identity keys untouched)', async () => {
    panel(entry({
      template: 'image-annotated',
      title: 'Img',
      data: { src: `${CDN}/artifacts/sess-1/frame.png`, sourceKey: 'artifacts/sess-1/frame.png', annotations: [] },
    }));
    const img = await screen.findByTestId('tpl-img');
    expect(img).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/frame.png'));
    for (const p of seen.props) {
      expect(p.data.src).toBe(presigner.urlFor('artifacts/sess-1/frame.png'));
      expect(p.data.sourceKey).toBe('artifacts/sess-1/frame.png');
    }
  });

  it('renders data without identifiers synchronously, with no presign request', () => {
    panel(entry({ template: 'image-annotated', title: 'Img', data: { src: 'https://v3.fal.media/x.png', annotations: [] } }));
    expect(screen.getByTestId('tpl-img')).toHaveAttribute('src', 'https://v3.fal.media/x.png');
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('maps a template\'s presigned URL back to the identifier for onInteraction, and copies a presigned URL', async () => {
    const onInteraction = vi.fn();
    panel(entry({ template: 'image-annotated', title: 'Img', data: { src: `${CDN}/artifacts/sess-1/frame.png`, annotations: [] } }), onInteraction);
    await waitFor(() => expect(screen.getByTestId('tpl-img')).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/frame.png')));
    fireEvent.click(screen.getByRole('button', { name: 'pick' }));
    expect(onInteraction).toHaveBeenCalledWith(expect.objectContaining({ data: { url: `${CDN}/artifacts/sess-1/frame.png` } }));
    // The selection is held as the identifier; the clipboard gets the presigned URL, because the
    // identifier 403s when opened with the artifacts CDN private.
    fireEvent.click(screen.getByTestId('copy-source-url'));
    expect(writeText).toHaveBeenCalledWith(presigner.urlFor('artifacts/sess-1/frame.png'));
  });

  it('frames a custom document block on the presigned URL, not the bare /api/artifact', async () => {
    panel(entry({ template: 'custom', title: 'Doc', data: { src: `${CDN}/artifacts/sess-1/report.pdf` } }));
    const frame = screen.getByTestId('custom-src-frame');
    await waitFor(() => expect(frame).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/report.pdf')));
  });

  it('shows "Preview unavailable" instead of framing a custom document src that holds two links', () => {
    // Session 865950fd: framed, this became /api/artifact?key=<garbage> and showed the proxy's 400 JSON.
    panel(entry({ template: 'custom', title: 'Doc', data: { src: `${CDN}/artifacts/s/a.json\\nhttps://d1abcd.cloudfront.net/artifacts/s/a.pdf` } }));
    expect(screen.getByTestId('custom-src-unavailable')).toHaveTextContent('Preview unavailable');
    expect(screen.queryByTestId('custom-src-frame')).toBeNull();
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('falls back to the /api/artifact frame when resolution fails', async () => {
    presigner.failStatus = 500;
    panel(entry({ template: 'custom', title: 'Doc', data: { src: `${CDN}/artifacts/sess-1/report.pdf` } }));
    const frame = screen.getByTestId('custom-src-frame');
    await waitFor(() => expect(frame).toHaveAttribute('src', '/api/artifact?key=artifacts%2Fsess-1%2Freport.pdf'));
  });

  it('re-signs a failing media element anywhere in the panel content', async () => {
    panel(entry({ template: 'image-annotated', title: 'Img', data: { src: `${CDN}/artifacts/sess-1/frame.png`, annotations: [] } }));
    const img = await screen.findByTestId('tpl-img');
    await waitFor(() => expect(img).toHaveAttribute('src', presigner.urlFor('artifacts/sess-1/frame.png')));
    presigner.generation = 2;
    img.dispatchEvent(new Event('error', { bubbles: false }));
    await waitFor(() => expect((img as HTMLImageElement).src).toBe(presigner.urlFor('artifacts/sess-1/frame.png', false, 2)));
  });
});
