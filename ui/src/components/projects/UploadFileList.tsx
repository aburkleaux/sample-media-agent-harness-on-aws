/**
 * The Upload_Client's per-file list, shared by the Projects_Surface and the chat's upload sheet.
 *
 * {@link UploadRow} is presentational: one file's name, size, state, a `role="progressbar"` while it is in
 * progress, and the actions its state allows (Cancel while queued or uploading, Retry for a failed file that was
 * not rejected, Dismiss once finished). {@link UploadFileList} renders a Project's files from the upload queue
 * and announces each finished batch through a polite live region. Past {@link UPLOAD_SUMMARY_THRESHOLD} files (a
 * dropped folder) it shows one summary row instead (`312 of 1,000 uploaded · 2 failed`, Retry failed, Cancel
 * remaining) with the per-file rows behind `Show files`.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import CloseIcon from '../icons/CloseIcon';
import WarningIcon from '../icons/WarningIcon';
import CheckIcon from '../icons/CheckIcon';
import { formatBytes } from '../../projects/upload/kinds';
import {
  batchSummary,
  cancel,
  dismiss,
  isInProgress,
  retry,
  type BatchSummary,
  type UploadEntry,
  type UploadFileStatus,
} from '../../projects/upload/uploadQueue';

export interface UploadRowProps {
  name: string;
  /** The folder the file goes into, shown muted beside the name. */
  folder?: string;
  size: number;
  status: UploadFileStatus;
  pct: number;
  error?: string;
  rejected?: boolean;
  /** Overrides the `registering` label (the chat says "Adding to chat"). */
  registeringLabel?: string;
  /** Overrides the `ready` label. */
  readyLabel?: string;
  onCancel?: () => void;
  onRetry?: () => void;
  onDismiss?: () => void;
}

function statusText(p: UploadRowProps): string {
  switch (p.status) {
    case 'queued':
      return 'Waiting';
    case 'uploading':
      return `Uploading ${p.pct}%`;
    case 'registering':
      return p.registeringLabel ?? 'Adding to project';
    case 'ready':
      return p.readyLabel ?? 'Uploaded';
    case 'canceled':
      return 'Canceled';
    case 'failed':
      return p.error ? `${p.rejected ? 'Not uploaded' : 'Failed'}: ${p.error}` : 'Failed';
  }
}

export function UploadRow(props: UploadRowProps) {
  const { name, folder, size, status, pct, rejected, onCancel, onRetry, onDismiss } = props;
  const inProgress = status === 'queued' || status === 'uploading' || status === 'registering';
  const terminal = !inProgress;
  return (
    <li style={rowStyle} data-testid="upload-row" data-status={status}>
      <div style={rowTopStyle}>
        <span style={rowIconStyle(status)}>
          {status === 'failed' ? <WarningIcon size={14} /> : status === 'ready' ? <CheckIcon size={14} /> : null}
        </span>
        <span style={nameStyle} title={folder ? `${folder}/${name}` : name}>{name}</span>
        {folder && <span style={folderStyle} title={folder} data-testid="upload-row-folder">{folder}</span>}
        <span style={sizeStyle}>{formatBytes(size)}</span>
        <span style={actionsStyle}>
          {(status === 'queued' || status === 'uploading') && onCancel && (
            <button type="button" style={linkBtnStyle} onClick={onCancel} aria-label={`Cancel ${name}`}>
              Cancel
            </button>
          )}
          {status === 'failed' && !rejected && onRetry && (
            <button type="button" style={linkBtnStyle} onClick={onRetry} aria-label={`Retry ${name}`}>
              Retry
            </button>
          )}
          {terminal && onDismiss && (
            <button type="button" style={iconBtnStyle} onClick={onDismiss} aria-label={`Dismiss ${name}`}>
              <CloseIcon size={12} />
            </button>
          )}
        </span>
      </div>
      <div style={statusLineStyle(status)}>{statusText(props)}</div>
      {inProgress && (
        <div
          role="progressbar"
          aria-label={`Uploading ${name}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
          style={trackStyle}
        >
          <div style={{ ...barStyle, width: `${pct}%` }} />
        </div>
      )}
    </li>
  );
}

/** The batch summary sentence the live region announces. */
export function summaryText(s: BatchSummary): string {
  return `Upload finished: ${s.ready} uploaded, ${s.failed} failed, ${s.rejected} rejected, ${s.canceled} canceled.`;
}

/**
 * The last finished batch's summary for a set of files, announced once per batch. Shared by the chat sheet,
 * which passes its own `summarize` (its files are not all in the upload queue).
 */
export function useBatchAnnouncement(
  files: readonly { batchId: string }[],
  summarize: (batchId: string) => BatchSummary = batchSummary,
): string {
  const [text, setText] = useState('');
  const announced = useRef<Set<string>>(new Set());
  useEffect(() => {
    const ids = [...new Set(files.map((f) => f.batchId))];
    for (const id of ids) {
      if (announced.current.has(id)) continue;
      const s = summarize(id);
      if (s.total > 0 && s.done) {
        announced.current.add(id);
        setText(summaryText(s));
      }
    }
  }, [files, summarize]);
  return text;
}

/** Above this many files the list collapses into one summary row (the per-file rows stay behind `Show files`). */
export const UPLOAD_SUMMARY_THRESHOLD = 50;

const count = (n: number) => n.toLocaleString('en-US');

export interface UploadCounts {
  total: number;
  ready: number;
  failed: number;
  rejected: number;
  canceled: number;
  inProgress: number;
}

export function countUploads(entries: readonly UploadEntry[]): UploadCounts {
  const c: UploadCounts = { total: entries.length, ready: 0, failed: 0, rejected: 0, canceled: 0, inProgress: 0 };
  for (const e of entries) {
    if (e.status === 'ready') c.ready++;
    else if (e.status === 'canceled') c.canceled++;
    else if (e.status === 'failed') {
      if (e.rejected) c.rejected++;
      else c.failed++;
    } else c.inProgress++;
  }
  return c;
}

/** `312 of 1,000 uploaded · 2 failed` (plus not-uploaded and canceled counts when there are any). */
export function uploadCountsText(c: UploadCounts): string {
  const parts = [`${count(c.ready)} of ${count(c.total)} uploaded`];
  if (c.failed) parts.push(`${count(c.failed)} failed`);
  if (c.rejected) parts.push(`${count(c.rejected)} not uploaded`);
  if (c.canceled) parts.push(`${count(c.canceled)} canceled`);
  return parts.join(' · ');
}

export interface UploadFileListProps {
  entries: readonly UploadEntry[];
  /** Hide finished rows' Dismiss buttons (the caller offers its own clear). */
  hideDismiss?: boolean;
}

export default function UploadFileList({ entries, hideDismiss }: UploadFileListProps) {
  const announcement = useBatchAnnouncement(entries);
  const [showFiles, setShowFiles] = useState(false);
  const summarize = entries.length > UPLOAD_SUMMARY_THRESHOLD;
  const counts = useMemo(() => (summarize ? countUploads(entries) : null), [summarize, entries]);
  const rows = (
    <ul style={listStyle} aria-label="Uploads">
      {entries.map((e) => (
        <UploadRow
          key={e.id}
          name={e.name}
          folder={e.folder}
          size={e.size}
          status={e.status}
          pct={e.pct}
          error={e.error}
          rejected={e.rejected}
          onCancel={() => cancel(e.id)}
          onRetry={() => retry(e.id)}
          onDismiss={hideDismiss ? undefined : () => dismiss(e.id)}
        />
      ))}
    </ul>
  );
  return (
    <div data-testid="upload-file-list">
      {counts && (
        <div style={summaryRowStyle} data-testid="upload-batch-summary">
          <div style={rowTopStyle}>
            <span style={nameStyle} data-testid="upload-batch-counts">{uploadCountsText(counts)}</span>
            <span style={actionsStyle}>
              {counts.failed > 0 && (
                <button
                  type="button"
                  style={linkBtnStyle}
                  onClick={() => entries.forEach((e) => { if (e.status === 'failed' && !e.rejected) retry(e.id); })}
                >
                  Retry failed
                </button>
              )}
              {counts.inProgress > 0 && (
                <button
                  type="button"
                  style={linkBtnStyle}
                  onClick={() => entries.forEach((e) => { if (isInProgress(e.status)) cancel(e.id); })}
                >
                  Cancel remaining
                </button>
              )}
              <button
                type="button"
                style={linkBtnStyle}
                aria-expanded={showFiles}
                onClick={() => setShowFiles((v) => !v)}
              >
                {showFiles ? 'Hide files' : 'Show files'}
              </button>
            </span>
          </div>
          <div
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={counts.total}
            aria-valuenow={counts.total - counts.inProgress}
            style={{ ...trackStyle, marginLeft: 0 }}
          >
            <div style={{ ...barStyle, width: `${counts.total ? Math.floor(((counts.total - counts.inProgress) / counts.total) * 100) : 0}%` }} />
          </div>
        </div>
      )}
      {entries.length > 0 && (!counts || showFiles) && rows}
      <div aria-live="polite" aria-atomic="true" style={srOnlyStyle} data-testid="upload-summary">
        {announcement}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------ styles

export const srOnlyStyle: React.CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
};
const listStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
};
const rowStyle: React.CSSProperties = {
  padding: '6px 8px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
};
const rowTopStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 };
const rowIconStyle = (status: UploadFileStatus): React.CSSProperties => ({
  display: 'inline-flex',
  width: 14,
  flexShrink: 0,
  color: status === 'failed' ? theme.colors.error : theme.colors.success,
});
const nameStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  fontWeight: theme.typography.weights.medium,
};
const sizeStyle: React.CSSProperties = { color: theme.colors.textTertiary, flexShrink: 0 };
const folderStyle: React.CSSProperties = {
  maxWidth: '40%',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: theme.colors.textTertiary,
};
const summaryRowStyle: React.CSSProperties = {
  padding: '6px 8px',
  marginBottom: 6,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
};
const actionsStyle: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 };
const statusLineStyle = (status: UploadFileStatus): React.CSSProperties => ({
  marginTop: 2,
  paddingLeft: 22,
  color: status === 'failed' ? theme.colors.error : theme.colors.textSecondary,
  overflowWrap: 'anywhere',
});
const trackStyle: React.CSSProperties = {
  marginTop: 4,
  marginLeft: 22,
  height: 4,
  borderRadius: theme.radius.pill,
  background: theme.colors.surfaceSubtle,
  overflow: 'hidden',
};
const barStyle: React.CSSProperties = {
  height: '100%',
  background: theme.colors.primary,
  transition: `width ${theme.transitions.hover}`,
};
const linkBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'inherit',
};
const iconBtnStyle: React.CSSProperties = {
  ...linkBtnStyle,
  display: 'inline-flex',
  color: theme.colors.textTertiary,
};
