/**
 * How a Project_Asset's kind and Ingestion_Status are drawn, shared by the Projects_Surface's asset cards and
 * the Plus_Menu's `Recent in <project>` rows so both say the same thing about the same asset.
 */
import React from 'react';
import type { BadgeVariant } from '../ui/Badge';
import VideoIcon from '../icons/VideoIcon';
import ImageIcon from '../icons/ImageIcon';
import AudioIcon from '../icons/AudioIcon';
import DocumentIcon from '../icons/DocumentIcon';
import IconBase, { type IconProps } from '../icons/IconBase';
import type { AssetIngestionStatus, AssetKind, AssetRole } from '../../projects/api';

const STATUS_LABEL: Record<AssetIngestionStatus, string> = {
  uploading: 'Uploading',
  queued: 'Queued',
  analyzing: 'Analyzing',
  indexing: 'Indexing',
  indexed: 'Ready',
  ready: 'Ready',
  failed: 'Failed',
  removed: 'Removed',
  // Indexing is off for the Project: the file is kept and findable by name, but not analyzed.
  stored: 'Stored',
};

const STATUS_VARIANT: Record<AssetIngestionStatus, BadgeVariant> = {
  uploading: 'info',
  queued: 'neutral',
  analyzing: 'info',
  indexing: 'info',
  indexed: 'success',
  ready: 'success',
  failed: 'error',
  removed: 'warning',
  stored: 'neutral',
};

export const KIND_LABEL: Record<AssetKind, string> = {
  video: 'Video',
  image: 'Image',
  audio: 'Audio',
  document: 'Document',
  data: 'Data',
};

/** The roles a card names; a `source` or `deliverable` file is just a file. */
export const ROLE_LABEL: Partial<Record<AssetRole, string>> = {
  analysis: 'Analysis',
  intermediate: 'Intermediate',
};

/**
 * The resting states: a file that is simply there (kept by name, or analyzed and searchable). The Assets grid draws
 * no badge for them, so a badge on a card always means something is still happening or went wrong.
 */
export const RESTING_STATUSES: ReadonlySet<AssetIngestionStatus> = new Set(['stored', 'indexed', 'ready']);

/** Whether the Assets grid badges this status: anything not at rest (queued, analyzing, indexing, failed, …). */
export function needsAttention(status: AssetIngestionStatus): boolean {
  return !RESTING_STATUSES.has(status);
}

const SKIPPED_REASON_TEXT: Record<string, string> = {
  no_speech: 'No usable speech to index (for example music or sound effects). Kept in the project, not searchable yet.',
  // A document (PDF) with no extractable text layer (a scan, an encrypted or corrupt file): kept, not searchable.
  no_text_layer: 'No extractable text (for example a scanned PDF). Kept in the project, not searchable.',
  // A document too large for the harness to extract text from. Kept, not searchable.
  document_too_large: 'Too large to extract text from. Kept in the project, not searchable.',
};

/**
 * The hover text for a `stored` asset an ingestion run deliberately left unindexed, or `null` (a file stored only
 * because indexing is off has no reason). Not a failure: the card draws a neutral `Not indexed` badge, never `Failed`.
 */
export function notIndexedReason(asset: { status?: unknown; indexSkippedReason?: unknown }): string | null {
  if (asset.status !== 'stored') return null;
  const r = asset.indexSkippedReason;
  if (typeof r !== 'string' || !r) return null;
  return SKIPPED_REASON_TEXT[r] ?? 'Not indexed.';
}

export function statusLabel(status: AssetIngestionStatus): string {
  return STATUS_LABEL[status] ?? String(status);
}

export function statusVariant(status: AssetIngestionStatus): BadgeVariant {
  return STATUS_VARIANT[status] ?? 'neutral';
}

/** Data asset (JSON/CSV/text analysis output): a table of rows. */
function DataIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M4 10h16M4 15h16M10 10v10" />
    </IconBase>
  );
}

/** Unknown kinds (a newer server) draw as a document rather than failing. */
export function AssetKindIcon({ kind, size }: { kind: AssetKind; size: number }) {
  if (kind === 'video') return <VideoIcon size={size} />;
  if (kind === 'image') return <ImageIcon size={size} />;
  if (kind === 'audio') return <AudioIcon size={size} />;
  if (kind === 'data') return <DataIcon size={size} />;
  return <DocumentIcon size={size} />;
}
