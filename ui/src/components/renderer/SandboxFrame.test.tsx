// @vitest-environment jsdom
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../../auth/authFetch', () => ({ authFetch }));
vi.mock('../../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

import SandboxFrame from './SandboxFrame';
import { __resetArtifactUrlsForTests } from '../../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../../artifacts/testPresigner';

const CDN = 'https://d1abcd.cloudfront.net';
let presigner: FakePresigner;

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
});

describe('SandboxFrame — artifact URLs in agent HTML', () => {
  it('builds srcDoc with presigned URLs (never the unresolved document first)', async () => {
    const html = `<img src="${CDN}/artifacts/s/a.png"><video src="/api/artifact?key=artifacts%2Fs%2Fv.mp4"></video>`;
    const { container } = render(<SandboxFrame html={html} title="t" />);
    const frame = container.querySelector('iframe')!;
    expect(frame.getAttribute('srcdoc') ?? '').not.toContain(CDN);
    await waitFor(() => expect(frame.getAttribute('srcdoc')).toContain(presigner.urlFor('artifacts/s/a.png').replace(/&/g, '&amp;')));
    expect(frame.getAttribute('srcdoc')).toContain(presigner.urlFor('artifacts/s/v.mp4').replace(/&/g, '&amp;'));
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
  });

  it('renders HTML without artifact URLs immediately, with no request', () => {
    const { container } = render(<SandboxFrame html="<p>hello</p>" title="t" />);
    expect(container.querySelector('iframe')!.getAttribute('srcdoc')).toContain('<p>hello</p>');
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('keeps the original URLs when resolution fails', async () => {
    presigner.failStatus = 500;
    const html = `<img src="${CDN}/artifacts/s/a.png">`;
    const { container } = render(<SandboxFrame html={html} title="t" />);
    await waitFor(() => expect(container.querySelector('iframe')!.getAttribute('srcdoc')).toContain(html));
  });
});
