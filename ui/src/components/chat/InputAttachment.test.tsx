/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../../auth/authFetch', () => ({ authFetch }));
vi.mock('../../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

import { InputAttachment } from './InputAttachment';
import { __resetArtifactUrlsForTests } from '../../artifacts/artifactUrls';
import { makeFakePresigner, type FakePresigner } from '../../artifacts/testPresigner';

const CDN = 'https://dexamplecdn002.cloudfront.net';
let presigner: FakePresigner;

beforeEach(() => {
  __resetArtifactUrlsForTests();
  presigner = makeFakePresigner();
  authFetch.mockReset();
  authFetch.mockImplementation(presigner.handle);
});

describe('InputAttachment', () => {
  it('renders NOTHING for an uploaded video (panel renders the NLE; bubble already names the file)', () => {
    const { container } = render(<InputAttachment url="https://cdn/v.mp4" name="clip.mp4" />);
    expect(container).toBeEmptyDOMElement();
  });

  it('(fd4ce1bd) renders NOTHING for a project copy named "clip.mp4 (2)" either — no play-video link', () => {
    const { container } = render(<InputAttachment url="https://cdn/artifacts/s/input/3-clip.mp4" name="clip.mp4 (2)" />);
    expect(container).toBeEmptyDOMElement();
  });

  it('still previews a non-artifact image inline, as-is', () => {
    const { container } = render(<InputAttachment url="https://example.com/p.png" name="p.png" />);
    expect(container.querySelector('img')).toHaveAttribute('src', 'https://example.com/p.png');
  });

  it('(b42731df) resolves EVERY uploaded image to a presigned URL — never loads the private CDN URL', async () => {
    const keys = [
      'artifacts/b42731df-a99b-4b3e-ba0f-22c93a84976b/input/1791294174237-sam.webp',
      'artifacts/b42731df-a99b-4b3e-ba0f-22c93a84976b/input/1791294174311-images.webp',
    ];
    const { container } = render(
      <>
        <InputAttachment url={`${CDN}/${keys[0]}`} name="sam.webp" />
        <InputAttachment url={`${CDN}/${keys[1]}`} name="images.webp" />
      </>,
    );
    await waitFor(() => expect(container.querySelectorAll('img')).toHaveLength(2));
    const srcs = [...container.querySelectorAll('img')].map((i) => i.getAttribute('src'));
    expect(srcs).toEqual([presigner.urlFor(keys[0]), presigner.urlFor(keys[1])]);
    expect(srcs.some((s) => s?.startsWith(CDN))).toBe(false);
  });

  it('previews an uploaded audio via a presigned URL', async () => {
    const key = 'artifacts/s1/input/a.wav';
    const { container } = render(<InputAttachment url={`${CDN}/${key}`} name="a.wav" />);
    await waitFor(() => expect(container.querySelector('audio')).toHaveAttribute('src', presigner.urlFor(key)));
  });
});
