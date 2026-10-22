/**
 * @vitest-environment jsdom
 *
 * CitationRef unit tests (Component 5, media-harness-search-qa-rendering).
 *
 * CitationRef looks up a parsed Citation's key in the CitationBlock carried by CitationContext and
 * renders a verified LINK (Req 2.7) or an unverified MARK (Req 2.10), or — with no provider — the
 * text plain. `openCitation` is mocked so activating a verified link never touches the network, and
 * so these tests stay focused on the markup contract and on the block being left unchanged.
 *
 * Covers Requirements 2.7, 2.10.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { CitationRef, CitationContext } from './CitationRef';
import { citationKey } from './citations';
import type { CitationBlock, CitationRecordWire } from '../../api';

const openCitationMock = vi.fn((_record: unknown, _sessionId: string) =>
  Promise.resolve({ ok: true as const }),
);
const resolveMock = vi.fn((_ref: unknown, _name: string) =>
  Promise.resolve({ ok: true as const, url: 'https://signed/nba.mp4' }) as Promise<{ ok: true; url: string } | { ok: false; message: string }>,
);
vi.mock('../../projects/openCitation', () => ({
  openCitation: (record: unknown, sessionId: string) => openCitationMock(record, sessionId),
  resolveCitationSource: (ref: unknown, name: string) => resolveMock(ref, name),
}));

const KEY = citationKey('nba.mp4', 5000, 10000);
const CITE_TEXT = `[nba.mp4 @ 00:05\u201300:10]`;

function verifiedRecord(): CitationRecordWire {
  return {
    key: KEY,
    text: CITE_TEXT,
    display_name: 'nba.mp4',
    start_ms: 5000,
    end_ms: 10000,
    verified: true,
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null },
    kind: 'video',
    asset_display_name: 'nba.mp4',
  };
}

function blockWith(record: CitationRecordWire): CitationBlock {
  return { v: 1, project_id: 'p1', truncated: false, items: [record] };
}

function renderWithBlock(block: CitationBlock, citationKeyProp = KEY) {
  return render(
    <CitationContext.Provider value={{ block, sessionId: 's1' }}>
      <CitationRef citationKey={citationKeyProp}>{CITE_TEXT}</CitationRef>
    </CitationContext.Provider>,
  );
}

beforeEach(() => {
  openCitationMock.mockClear();
  resolveMock.mockClear();
});

describe('CitationRef', () => {
  it('renders a verified Citation as a link whose accessible name has the name and timecode', () => {
    renderWithBlock(blockWith(verifiedRecord()));
    const link = screen.getByRole('link');
    expect(link).toHaveAccessibleName('Open nba.mp4 at 00:05\u201300:10');
    expect(link).toHaveTextContent(CITE_TEXT);
  });

  it('activating a verified citation opens the PREVIEW at its moment, not the side panel', async () => {
    renderWithBlock(blockWith(verifiedRecord()));
    fireEvent.click(screen.getByRole('link'));
    expect(resolveMock).toHaveBeenCalledWith({ project_id: 'p1', asset_id: 'a1', frame_s3_key: null }, 'nba.mp4');
    expect(await screen.findByRole('dialog', { name: /Preview of nba\.mp4/ })).toBeInTheDocument();
    expect(screen.getByTestId('asset-preview-range')).toHaveTextContent('0:05 – 0:10');
    expect(await screen.findByTestId('asset-preview-video')).toHaveAttribute('src', 'https://signed/nba.mp4');
    expect(openCitationMock).not.toHaveBeenCalled();
    // The side panel is one explicit step away: the former Renderer open.
    fireEvent.click(screen.getByRole('button', { name: /Open in side panel/ }));
    expect(openCitationMock).toHaveBeenCalledTimes(1);
    expect(openCitationMock.mock.calls[0][1]).toBe('s1');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('a citation that cannot be opened says why in the preview', async () => {
    resolveMock.mockResolvedValueOnce({ ok: false, message: 'nba.mp4 was removed from the project' });
    renderWithBlock(blockWith(verifiedRecord()));
    fireEvent.click(screen.getByRole('link'));
    expect(await screen.findByRole('alert')).toHaveTextContent('nba.mp4 was removed from the project');
  });

  it('a document citation opens the preview at its page', async () => {
    const rec = { ...verifiedRecord(), kind: 'document' as const, page: 3, asset_display_name: 'report.pdf' };
    resolveMock.mockResolvedValueOnce({ ok: true, url: 'https://signed/report.pdf' });
    renderWithBlock(blockWith(rec));
    fireEvent.click(screen.getByRole('link'));
    expect(await screen.findByTestId('asset-preview-document')).toHaveAttribute('src', 'https://signed/report.pdf#page=3');
    expect(screen.getByTestId('asset-preview-range')).toHaveTextContent('Page 3');
  });

  it('renders an UNVERIFIED record as a note with no link role and the "could not be matched" label', () => {
    const rec = { ...verifiedRecord(), verified: false, reason: 'no_matching_hit' as const };
    delete (rec as Partial<CitationRecordWire>).ref;
    renderWithBlock(blockWith(rec));

    expect(screen.queryByRole('link')).toBeNull();
    const note = screen.getByRole('note');
    expect(note).toHaveTextContent('could not be matched to a search result');
    expect(note).toHaveAccessibleName(/could not be matched to a search result/);
    expect(openCitationMock).not.toHaveBeenCalled();
  });

  it('renders a note (not a link) when the key matches no record at all', () => {
    // A key beyond the 100-record cap, or a mismatch between the two parsers.
    renderWithBlock(blockWith(verifiedRecord()), 'missing|1|2');
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByRole('note')).toHaveTextContent('could not be matched to a search result');
  });

  it('renders the bracketed text plain when no CitationContext provider wraps it', () => {
    render(<CitationRef citationKey={KEY}>{CITE_TEXT}</CitationRef>);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('note')).toBeNull();
    expect(screen.getByText(CITE_TEXT)).toBeInTheDocument();
  });

  it('leaves the CitationBlock unchanged after an open attempt', async () => {
    const block = blockWith(verifiedRecord());
    const original = structuredClone(block);
    renderWithBlock(block);
    fireEvent.click(screen.getByRole('link'));
    await Promise.resolve();
    expect(block).toEqual(original);
  });
});
