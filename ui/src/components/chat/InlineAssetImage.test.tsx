/** @vitest-environment jsdom */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';

const mocks = vi.hoisted(() => ({ request: vi.fn(), openAssetAt: vi.fn() }));
vi.mock('../../projects/assetUrlBatcher', () => ({ request: mocks.request }));
vi.mock('../../projects/openCitation', () => ({ openAssetAt: mocks.openAssetAt }));

import { markdownComponents } from './markdownComponents';
import { ChatAssetContext, keepAssetUrls } from './InlineAssetImage';

const transform = (u: string) => keepAssetUrls(u, defaultUrlTransform);
function answer(md: string, projectId: string | null = 'p1') {
  return render(
    <ChatAssetContext.Provider value={{ sessionId: 's1', projectId }}>
      <ReactMarkdown components={markdownComponents} urlTransform={transform}>{md}</ReactMarkdown>
    </ChatAssetContext.Provider>,
  );
}

describe('inline project images in a chat answer', () => {
  beforeEach(() => {
    mocks.request.mockReset();
    mocks.openAssetAt.mockReset();
    mocks.request.mockResolvedValue(new Map([['image:1', { ok: true, entry: { source: { url: 'https://signed/src' }, rendition: { url: 'https://signed/r' } } }]]));
    mocks.openAssetAt.mockResolvedValue({ ok: true });
  });

  it('`![caption](asset:id)` shows the image inline, from an authorised URL of the active project', async () => {
    answer('Here: ![Frames to shots](asset:image:1)');
    await waitFor(() => expect(screen.getByRole('img', { name: 'Frames to shots' })).toHaveAttribute('src', 'https://signed/r'));
    expect(mocks.request).toHaveBeenCalledWith('p1', ['image:1']);
  });

  it('Preview shows it large; Open in side panel opens it as a Renderer tab', async () => {
    answer('![Frames to shots](asset:image:1)');
    fireEvent.click(await screen.findByRole('button', { name: 'Preview Frames to shots' }));
    expect(screen.getByRole('dialog', { name: /Preview of Frames to shots/ })).toBeInTheDocument();
    expect(mocks.openAssetAt).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Open in side panel/ }));
    await waitFor(() => expect(mocks.openAssetAt).toHaveBeenCalledWith(
      expect.objectContaining({ ref: { project_id: 'p1', asset_id: 'image:1', frame_s3_key: null }, kind: 'image', displayName: 'Frames to shots' }),
      's1',
    ));
  });

  it('says so, without a broken image, when the chat has no project or the id is not visible', async () => {
    answer('![Diagram](asset:image:1)', null);
    expect(await screen.findByTestId('inline-asset-missing')).toHaveTextContent('Diagram — not available');
    mocks.request.mockResolvedValue(new Map([['image:2', { ok: false, kind: 'not_found' }]]));
    answer('![Other](asset:image:2)');
    await waitFor(() => expect(screen.getAllByTestId('inline-asset-missing')).toHaveLength(2));
  });

  it('other URLs are still sanitised by react-markdown', () => {
    answer('[x](javascript:alert(1))');
    expect(screen.getByText('x').closest('a')?.getAttribute('href') ?? '').not.toMatch(/^javascript:/);
  });
});
