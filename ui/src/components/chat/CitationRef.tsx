// CitationRef — renders one parsed Citation as a verified link or an unverified mark
// (Component 5, media-harness-search-qa-rendering, "Where Citations render").
//
// The `span` override in markdownComponents.tsx turns every `data-mh-citation` span the
// remarkCitations plugin produced into a <CitationRef citationKey=…>. This component looks the
// key up in the CitationBlock carried by CitationContext — it does NOT map the Nth Citation in
// the markdown to the Nth record (markdown escaping, code spans and links make ordinal mapping
// unreliable, and a mismatch would attach one Citation's verification to another).
//
//   - A VERIFIED record renders as a pill (Req 2.7): click/Enter opens the chat's PREVIEW of the cited asset at
//     its moment (a video/audio plays from the cited start and pauses at its end; a document opens at its page).
//     The side panel is reached from the preview's "Open in side panel", which is the former openCitation(record,
//     sessionId) Renderer open. aria-busy while the lookup is in flight; further activations ignored until it settles.
//   - An UNVERIFIED record, or a parsed Citation with NO record (beyond the 100 cap, or a key
//     mismatch between the two parsers), renders a role="note" mark with no link and no handlers
//     (Req 2.10), so activating it opens nothing.
//
// Implements Requirements 2.7, 2.10.

import React, { createContext, useCallback, useContext, useState } from 'react';
import type { CitationBlock, CitationRecordWire } from '../../api';
import { formatCitationTimecode } from './citations';
import { openCitation, resolveCitationSource, type VerifiedCitationRecord } from '../../projects/openCitation';
import { AssetPreviewModal, type PreviewKind, type PreviewRange } from './AssetPreviewModal';
import { AssetIcon } from './AssetIcons';
import { downloadFile } from './artifactUrl';

/**
 * The CitationBlock and the active session for the message being rendered. Provided by
 * MessageBubble (ChatCanvas.tsx) only when `message.summary?.citations` is present; `null` when no
 * provider wraps the tree, in which case CitationRef renders its text plain.
 */
export interface CitationContextValue {
  block: CitationBlock;
  sessionId: string;
}

export const CitationContext = createContext<CitationContextValue | null>(null);

const UNVERIFIED_NOTE = 'could not be matched to a search result';

/** Flatten the rendered children to a string for the aria-labels (the citation child is text). */
function childrenToText(children: React.ReactNode): string {
  if (typeof children === 'string') return children;
  if (typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(childrenToText).join('');
  return '';
}

/** The timecode span of the aria-label: floor on the start, ceil on the end, en-dash separator. */
function timecodeRange(record: CitationRecordWire): string {
  const start = formatCitationTimecode(Math.floor(record.start_ms / 1000));
  const end = formatCitationTimecode(Math.ceil(record.end_ms / 1000));
  return `${start}\u2013${end}`;
}

export interface CitationRefProps {
  /** The `citationKey(...)` the remarkCitations plugin wrote into `data-mh-citation`. */
  citationKey: string;
  children?: React.ReactNode;
}

export function CitationRef({ citationKey, children }: CitationRefProps): React.ReactElement {
  const ctx = useContext(CitationContext);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ src: string | null; error: string | null } | null>(null);

  const record = ctx ? ctx.block.items.find((item) => item.key === citationKey) : undefined;
  const verified = record?.verified === true;

  const open = useCallback(() => {
    if (!ctx || !record || record.verified !== true) return;
    if (busy) return; // further activations ignored while an open is in flight
    setBusy(true);
    const r = record as VerifiedCitationRecord;
    setPreview({ src: null, error: null });
    void resolveCitationSource(r.ref, r.asset_display_name ?? record.display_name)
      .then((res) => setPreview(res.ok ? { src: res.url, error: null } : { src: null, error: res.message }))
      .finally(() => setBusy(false));
  }, [ctx, record, busy]);

  const onClick = useCallback(
    (e: React.MouseEvent<HTMLAnchorElement>) => {
      e.preventDefault();
      open();
    },
    [open],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLAnchorElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        open();
      }
    },
    [open],
  );

  // No provider: nothing to classify against — render the text plain (same as a turn with no block).
  if (!ctx) {
    return <>{children}</>;
  }

  if (verified && record) {
    const name = record.asset_display_name ?? record.display_name;
    const kind: PreviewKind = (['image', 'video', 'audio', 'document'] as const).includes(record.kind as PreviewKind)
      ? (record.kind as PreviewKind) : 'video';
    const range: PreviewRange = record.page !== undefined
      ? { page: record.page, pageEnd: record.page_end }
      : { startMs: record.start_ms, endMs: record.end_ms };
    const toPanel = () => { void openCitation(record as VerifiedCitationRecord, ctx.sessionId); };
    const src = preview?.src ?? null;
    return (
      <>
      <a
        href="#"
        className="mh-citation"
        aria-label={`Open ${name} at ${record.page !== undefined
          ? (record.page_end && record.page_end !== record.page ? `pages ${record.page}\u2013${record.page_end}` : `page ${record.page}`)
          : timecodeRange(record)}`}
        aria-busy={busy || undefined}
        onClick={onClick}
        onKeyDown={onKeyDown}
      >
        <AssetIcon kind={record.page !== undefined ? 'page' : 'clock'} size={13} />
        {children}
      </a>
      {preview && (
        <AssetPreviewModal kind={kind} src={src} name={name} range={range} error={preview.error}
          onClose={() => setPreview(null)} onOpenInPanel={toPanel}
          onDownload={src ? () => { void downloadFile(src, name); } : undefined} />
      )}
      </>
    );
  }

  // Unverified record, or a parsed Citation with no matching record (Req 2.10): a note, no link.
  const text = childrenToText(children);
  return (
    <span
      role="note"
      className="mh-citation-unverified"
      aria-label={`${text}: citation ${UNVERIFIED_NOTE}`}
    >
      {children} <span className="mh-citation-badge">{UNVERIFIED_NOTE}</span>
    </span>
  );
}

export default CitationRef;
