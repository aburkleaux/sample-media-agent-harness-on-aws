// @vitest-environment jsdom
//
// The Moments tab's own search (2026-10-11): the box is pre-filled with the turn's query, a search
// replaces the cards in place, back/forward step through the tab's history (entry 0 = the agent's
// results), the history persists in viewState (queries only) and a reopened tab re-runs its query.
// Same mocks as ClipGallery.moments.test.tsx, plus the search API.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import { within, render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import React from 'react';

import ClipGallery from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { MomentItem, MomentsBlock, ClipGalleryData } from '../types';

// --- hoisted mock state the factories below close over ---------------------------------------------
const h = vi.hoisted(() => ({
  state: vi.fn(),
  freshUrlFor: vi.fn(),
  findSimilar: vi.fn(),
  blocks: [] as { items: { id: string }[] }[],
  searchMoments: vi.fn(),
  openAssetAt: vi.fn(),
}));

vi.mock('../../../projects/api', () => ({ searchMoments: h.searchMoments, getAsset: vi.fn(), getAssetUrls: vi.fn() }));
vi.mock('../../../projects/openCitation', () => ({ openAssetAt: h.openAssetAt }));

vi.mock('../../../projects/useMomentUrls', () => ({
  useMomentUrls: (block: { items: { id: string }[] }) => (h.blocks.push(block), {
    state: h.state,
    onLoadStart: vi.fn(),
    onLoadOk: vi.fn(),
    onLoadError: vi.fn(),
    freshUrlFor: h.freshUrlFor,
  }),
  URL_MAX_AGE_MS: 3_000_000,
}));
vi.mock('../../../projects/findSimilar', () => ({ findSimilar: h.findSimilar }));
vi.mock('../../../projects/turnState', () => ({ useSessionTurnInProgress: () => false }));
vi.mock('../../../services/activeSession', () => ({ getActiveSessionId: () => 'sess-1' }));
vi.mock('../../../projects/sessionProjectStore', () => ({ getSessionProject: () => ({ activeProjectId: 'p1' }) }));

// A light MomentCard stub: it registers a fake media element and exposes a button that sets a mark on
// the card, so the gallery's membership counts and dispatched entries can be driven deterministically.
vi.mock('./MomentCard', () => ({
  default: (props: {
    moment: MomentItem;
    onPreview?: (m: MomentItem) => void;
    playing?: boolean;
    onFindSimilar?: (m: MomentItem) => void;
  }) => {
    return (
      <div data-testid={`mc-${props.moment.id}`}>
        <button data-testid={`preview-${props.moment.id}`} data-playing={props.playing ? 'true' : undefined}
          onClick={() => props.onPreview?.(props.moment)}>
          preview
        </button>
        <button data-testid={`fs-${props.moment.id}`} onClick={() => props.onFindSimilar?.(props.moment)}>
          find similar
        </button>
      </div>
    );
  },
}));

function item(id: string, kind: MomentItem['kind'], over: Partial<MomentItem> = {}): MomentItem {
  return {
    id,
    ref: { project_id: 'p1', asset_id: `asset-${id}`, frame_s3_key: null },
    kind,
    display_name: id,
    start_ms: 0,
    end_ms: 4000,
    source_index: null,
    final_score: 0.5,
    text: '',
    text_truncated: false,
    highlight_terms: [],
    citation: null,
    ...over,
  };
}

function momentsData(items: MomentItem[], extra: Record<string, unknown> = {}): ClipGalleryData {
  const galleryMoments = { v: 1, project_id: 'p1', turn_id: 't1', basis: 'searched', truncated: false, items, query: 'night drive' };
  return { title: 'Moments', subtitle: 'Matched by this turn\'s searches', galleryMoments, ...extra } as unknown as ClipGalleryData;
}

const wire = (id: string) => ({ ...item(id, 'video'), id });
const ok = (items: unknown[]) => ({ ok: true, value: { query: 'q', kinds: null, items, total: items.length } });

function renderGallery(data: ClipGalleryData) {
  return render(<ClipGallery data={data} title="Moments" theme={theme as never} />);
}

const cardIds = () => screen.queryAllByTestId(/^mc-/).map((el) => el.getAttribute('data-testid'));

beforeEach(() => {
  h.state.mockReset().mockReturnValue({ phase: 'ready', url: 'https://x', obtainedAt: 0 });
  h.searchMoments.mockReset();
  h.blocks.length = 0;
});

describe('Moments tab search', () => {
  it('pre-fills the box with the turn query and shows the agent results first', () => {
    renderGallery(momentsData([item('m1', 'video'), item('m2', 'video')]));
    expect(screen.getByTestId('moments-search-input')).toHaveValue('night drive');
    expect(cardIds()).toEqual(['mc-m1', 'mc-m2']);
    // Nothing to go back to yet: no history arrows, no position, no Search button on screen (Enter searches).
    expect(screen.queryByTestId('moments-search-back')).toBeNull();
    expect(screen.queryByTestId('moments-search-position')).toBeNull();
  });

  it('a search replaces the cards in place; back and forward move through the history', async () => {
    h.searchMoments.mockResolvedValue(ok([wire('s1'), wire('s2'), wire('s3')]));
    const onViewStateChange = vi.fn();
    renderGallery(momentsData([item('m1', 'video')], { onViewStateChange }));
    fireEvent.change(screen.getByTestId('moments-search-input'), { target: { value: 'rain on windshield' } });
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    expect(screen.getByTestId('moments-search-loading')).toBeInTheDocument();
    await waitFor(() => expect(cardIds()).toHaveLength(3));
    expect(h.searchMoments).toHaveBeenCalledWith('p1', 'rain on windshield');
    expect(cardIds().every((id) => /^mc-t\d+-s\d$/.test(id!))).toBe(true); // a per-search id prefix
    expect(screen.getByText('3 moments for your search')).toBeInTheDocument();
    expect(onViewStateChange).toHaveBeenLastCalledWith({ search: { history: [{ q: 'rain on windshield' }], index: 1 } });
    // No "Search 1" label under the field (mock search-and-actions.html §1): the clock's list marks where you are.
    expect(screen.queryByTestId('moments-search-position')).toBeNull();
    expect(screen.queryByText('Search 1')).toBeNull();

    fireEvent.click(screen.getByTestId('moments-search-back'));
    expect(cardIds()).toEqual(['mc-m1']);
    expect(screen.getByTestId('moments-search-input')).toHaveValue('night drive');
    expect(onViewStateChange).toHaveBeenLastCalledWith({ search: { history: [{ q: 'rain on windshield' }], index: 0 } });

    fireEvent.click(screen.getByTestId('moments-search-forward'));
    expect(cardIds()).toHaveLength(3); // cached: no second request
    expect(h.searchMoments).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('moments-search-input')).toHaveValue('rain on windshield');
  });

  // Staging 2026-10-11: clicking a chip changed nothing -- the chips were only sent with the NEXT search, and never
  // applied to the agent's own results. They now filter the cards on screen, instantly and without a re-fetch.
  it('kind chips filter the cards on screen -- the agent results too -- without re-searching', async () => {
    renderGallery(momentsData([item('m1', 'video'), item('m2', 'image'), item('m3', 'audio')]));
    expect(cardIds()).toHaveLength(3);
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    expect(screen.getByTestId('moments-kind-image')).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => expect(cardIds()).toHaveLength(1));
    fireEvent.click(screen.getByTestId('moments-kind-audio'));
    await waitFor(() => expect(cardIds()).toHaveLength(2));
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    fireEvent.click(screen.getByTestId('moments-kind-audio'));
    await waitFor(() => expect(cardIds()).toHaveLength(3));
    expect(h.searchMoments).not.toHaveBeenCalled();
  });

  it('a search fetches every kind; the chips filter its cards', async () => {
    h.searchMoments.mockResolvedValue(ok([{ ...item('s1', 'video'), id: 's1' }, { ...item('s2', 'image'), id: 's2' }]));
    renderGallery(momentsData([item('m1', 'video')]));
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    await waitFor(() => expect(h.searchMoments).toHaveBeenCalledWith('p1', 'night drive'));
    await waitFor(() => expect(cardIds()).toHaveLength(1));
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    await waitFor(() => expect(cardIds()).toHaveLength(2));
    expect(h.searchMoments).toHaveBeenCalledTimes(1);
  });

  it('a reopened tab restores its history and re-runs the search on screen', async () => {
    h.searchMoments.mockResolvedValue(ok([wire('s1'), wire('s2')]));
    const viewState = { search: { history: [{ q: 'kawhi dunk', kinds: ['video'] }], index: 1 } };
    renderGallery(momentsData([item('m1', 'video')], { viewState }));
    expect(screen.getByTestId('moments-search-input')).toHaveValue('kawhi dunk');
    expect(screen.getByTestId('moments-kind-video')).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => expect(cardIds()).toHaveLength(2));
    expect(h.searchMoments).toHaveBeenCalledWith('p1', 'kawhi dunk');
    expect(screen.getByTestId('moments-search-back')).not.toBeDisabled();
  });

  it('shows empty and error states, and retries', async () => {
    h.searchMoments.mockResolvedValueOnce(ok([]));
    renderGallery(momentsData([item('m1', 'video')]));
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    await waitFor(() => expect(screen.getByTestId('clip-gallery-empty')).toHaveTextContent('No moments match'));

    h.searchMoments.mockResolvedValueOnce({ ok: false, kind: 'error', status: 502, code: 'search_failed', message: 'The search did not complete. Try again.' });
    fireEvent.change(screen.getByTestId('moments-search-input'), { target: { value: 'other' } });
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    await waitFor(() => expect(screen.getByTestId('moments-search-error')).toBeInTheDocument());
    h.searchMoments.mockResolvedValueOnce(ok([wire('s1')]));
    await act(async () => { fireEvent.click(screen.getByText('Try again')); });
    await waitFor(() => expect(cardIds()).toHaveLength(1));
  });

  it('a new search from the middle of the history drops what was ahead', async () => {
    h.searchMoments.mockImplementation(async (_p: string, q: string) => ok([wire(q === 'a' ? 'sa' : q === 'b' ? 'sb' : 'sc')]));
    const onViewStateChange = vi.fn();
    renderGallery(momentsData([item('m1', 'video')], { onViewStateChange }));
    const search = async (q: string) => {
      fireEvent.change(screen.getByTestId('moments-search-input'), { target: { value: q } });
      fireEvent.click(screen.getByTestId('moments-search-submit'));
      await waitFor(() => expect(screen.queryByTestId('moments-search-loading')).toBeNull());
    };
    await search('a');
    await search('b');
    fireEvent.click(screen.getByTestId('moments-search-back')); // on 'a'
    await search('c');
    expect(onViewStateChange).toHaveBeenLastCalledWith({ search: { history: [{ q: 'a' }, { q: 'c' }], index: 2 } });
    expect(screen.getByTestId('moments-search-forward')).toBeDisabled();
  });
});

// Staging 2026-10-11: PDFs were indexed, but a document hit never reached a card -- the gallery's kind check
// dropped it, there was no Document chip, and no card could show a page.
describe('document (PDF page) moments', () => {
  const doc = (id: string, page: number, over: Partial<MomentItem> = {}) =>
    item(id, 'document', { start_ms: (page - 1) * 10_000 + 2, end_ms: (page - 1) * 10_000 + 3, display_name: 'report.pdf',
      text: 'Rated PG-13 for violence', highlight_terms: ['violence'], source_index: 'scene', ...over });

  it('render as a document card at their page, next to media cards', () => {
    renderGallery(momentsData([item('m1', 'video'), doc('d1', 3)]));
    expect(cardIds()).toEqual(['mc-m1']); // the media card; the document is not a MomentCard
    const card = screen.getByTestId('moment-card-d1');
    expect(card).toHaveAttribute('data-moment-kind', 'document');
    expect(within(card).getByTestId('card-badge-left')).toHaveTextContent('p. 3');
    expect(screen.getByTestId('moment-text-d1').querySelector('mark')).toHaveTextContent('violence');
    // The one card's icons: Preview · Download · Open in side panel.
    expect(within(card).getByRole('button', { name: 'Preview report.pdf' })).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: 'Download report.pdf' })).toBeInTheDocument();
  });

  it('open the PDF at the page through the shared open path', () => {
    h.openAssetAt.mockReset().mockResolvedValue({ ok: true });
    renderGallery(momentsData([doc('d1', 7)]));
    fireEvent.click(screen.getByRole('button', { name: 'Open report.pdf in the side panel' }));
    expect(h.openAssetAt).toHaveBeenCalledWith(
      { ref: { project_id: 'p1', asset_id: 'asset-d1', frame_s3_key: null }, kind: 'document', displayName: 'report.pdf', startMs: 60_002 },
      expect.any(String),
    );
  });

  it('show an open refusal in the card', async () => {
    h.openAssetAt.mockReset().mockResolvedValue({ ok: false, message: "Can't open report.pdf: it was removed from the project." });
    renderGallery(momentsData([doc('d1', 1)]));
    fireEvent.click(screen.getByRole('button', { name: 'Open report.pdf in the side panel' }));
    expect(await screen.findByTestId('moment-refusal-d1')).toHaveTextContent('removed from the project');
  });

  it('Preview opens the PDF at the page in the chat preview', () => {
    renderGallery(momentsData([doc('d1', 4)]));
    fireEvent.click(screen.getByRole('button', { name: 'Preview report.pdf' }));
    expect(screen.getByTestId('asset-preview-document').getAttribute('src')).toBe('https://x#page=4');
    expect(screen.getByTestId('asset-preview-range')).toHaveTextContent('Page 4');
  });

  it('have a Document chip that filters to them', async () => {
    renderGallery(momentsData([item('m1', 'video'), doc('d1', 2)]));
    fireEvent.click(screen.getByTestId('moments-kind-document'));
    expect(screen.getByTestId('moments-kind-document')).toHaveTextContent('Document');
    await waitFor(() => expect(cardIds()).toEqual([]));
    expect(screen.getByTestId('moment-card-d1')).toBeInTheDocument();
  });

  it('hide their text when access is lost', () => {
    h.state.mockReturnValue({ phase: 'no_access' });
    renderGallery(momentsData([doc('d1', 2)]));
    expect(screen.getByTestId('moment-no-access-d1')).toBeInTheDocument();
    expect(screen.queryByTestId('moment-text-d1')).toBeNull();
  });
});
