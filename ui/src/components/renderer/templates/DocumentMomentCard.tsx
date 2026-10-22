/**
 * DocumentMomentCard — one DOCUMENT (PDF page) Moment in a Moments_Gallery, on the app's ONE card
 * ({@link MediaCard}).
 *
 * A document hit is a page of a PDF, not a span of time: its `start_ms` encodes the 1-based page
 * (`shared/pageLocator.ts`, the SPA half of `ingestion/documents.py`'s stride). So the card shows WHERE the match
 * is — the page on the picture, the matched text as its one line (`<mark className="mh-moment-hit">` runs, React
 * text only) — and its icons are Preview (the PDF at the page) · Download · Open in side panel, with the score as
 * the ⋯ note. No Cut, no Find similar.
 *
 * The side panel goes through {@link openAssetAt} (kind `document`): the same status + URL refusals as every other
 * open, then an in-harness tab showing the PDF at `#page=N`. `no_access` hides the text and score.
 */
import React, { useCallback, useState } from 'react';

import { MediaCard } from '../../cards/MediaCard';
import { FileLocation } from '../../projects/FileLocation';
import { downloadFile } from '../../chat/artifactUrl';
import { theme } from '../../../styles/tokens';
import { formatRankScore, segmentHighlights } from './momentText';
import { openAssetAt, resolveCitationSource } from '../../../projects/openCitation';
import { pageOfSpan } from '../../../shared/pageLocator';
import type { CardUrl } from '../../../projects/useMomentUrls';
import type { MomentItem, ThemeTokens } from '../types';

export interface DocumentMomentCardProps {
  moment: MomentItem;
  theme: ThemeTokens;
  sessionId: string;
  /** This card's URL phase, from the gallery's URL hook; only `no_access` changes what is shown. */
  cardUrl: CardUrl;
  /** Preview: the gallery's modal, at this page. */
  onPreview: (moment: MomentItem) => void;
}

export default function DocumentMomentCard({ moment, sessionId, cardUrl, onPreview }: DocumentMomentCardProps) {
  const [refusal, setRefusal] = useState<string | null>(null);
  const name = moment.display_name;
  const page = pageOfSpan(moment.start_ms);
  const hideContent = cardUrl.phase === 'no_access';
  const score = formatRankScore(moment.final_score);
  const segments = !hideContent && moment.text.length > 0 ? segmentHighlights(moment.text, moment.highlight_terms) : null;

  const openInPanel = useCallback(async () => {
    setRefusal(null);
    const r = await openAssetAt({ ref: moment.ref, kind: 'document', displayName: name, startMs: moment.start_ms }, sessionId);
    if (!r.ok) setRefusal(r.message);
  }, [moment.ref, moment.start_ms, name, sessionId]);

  const download = useCallback(async () => {
    setRefusal(null);
    const url = cardUrl.phase === 'ready' ? cardUrl.url : null;
    if (url) { await downloadFile(url, name); return; }
    const r = await resolveCitationSource(moment.ref, name);
    if (r.ok) await downloadFile(r.url, name);
    else setRefusal(r.message);
  }, [cardUrl, moment.ref, name]);

  const description = refusal
    ? <span role="alert" data-testid={`moment-refusal-${moment.id}`} title={refusal} style={{ color: theme.colors.error }}>{refusal}</span>
    : segments
      ? (
        <span data-testid={`moment-text-${moment.id}`} title={moment.text}>
          {segments.map((seg, i) => (seg.highlighted
            ? <mark key={i} className="mh-moment-hit">{seg.text}</mark>
            : <React.Fragment key={i}>{seg.text}</React.Fragment>))}
          {moment.text_truncated ? '…' : null}
        </span>
      )
      : hideContent ? undefined : <span>Matched on page {page}</span>;

  return (
    <MediaCard
      as="li"
      kind="document"
      title={name}
      titleTip={[name, `page ${page}`, hideContent ? null : `rank score ${score}`].filter(Boolean).join('\n')}
      description={description}
      location={hideContent ? undefined : <FileLocation projectId={moment.ref.project_id} assetId={moment.ref.asset_id} />}
      badgeLeft={`p. ${page}`}
      overlay={hideContent
        ? <span role="status" data-testid={`moment-no-access-${moment.id}`}>You no longer have access to this project</span>
        : undefined}
      onPreview={() => { if (!hideContent) onPreview(moment); }}
      primary={hideContent ? undefined : { label: `Download ${name}`, icon: 'download', onClick: () => void download() }}
      onOpenInPanel={hideContent ? undefined : () => void openInPanel()}
      menuNote={hideContent ? undefined : `PDF · page ${page} · match ${score}`}
      testId={`moment-card-${moment.id}`}
      dataAttrs={{ 'data-moment-card': '', 'data-moment-kind': 'document' }}
    />
  );
}
