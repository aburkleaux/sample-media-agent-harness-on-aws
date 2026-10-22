/**
 * "Already in this project" — one prompt per upload batch (Projects tab or chat).
 *
 * Use existing (default, focused): skip uploading the matching files. From chat, the existing assets are
 * attached instead. Keep both: upload them anyway; the registry names each copy `name (2)`. Cancel/Escape:
 * upload nothing from this batch.
 */
import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import type { Duplicate } from '../../projects/upload/duplicates';

export interface DuplicatesDialogProps {
  duplicates: Duplicate[] | null;
  /** Total files in the batch, so the message can say how many are new. */
  batchSize: number;
  onUseExisting(): void;
  onKeepBoth(): void;
  onCancel(): void;
}

export default function DuplicatesDialog({ duplicates, batchSize, onUseExisting, onKeepBoth, onCancel }: DuplicatesDialogProps) {
  const primary = useRef<HTMLButtonElement>(null);
  const open = !!duplicates && duplicates.length > 0;
  useEffect(() => {
    if (!open) return;
    primary.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onCancel]);
  if (!open) return null;
  const n = duplicates!.length;
  const fresh = batchSize - n;
  return createPortal(
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby="duplicates-title" data-testid="duplicates-dialog">
      <div style={dialogStyle}>
        <h3 id="duplicates-title" style={titleStyle}>
          {n === 1 ? 'This file is already in the project' : `${n} files are already in the project`}
        </h3>
        <p style={textStyle}>
          Same name and size as {n === 1 ? 'an existing file' : 'existing files'}
          {fresh > 0 ? `. The other ${fresh} file${fresh === 1 ? '' : 's'} will upload either way.` : '.'}
        </p>
        <ul style={listStyle}>
          {/* A re-dropped folder can match hundreds: list the first ones and count the rest. */}
          {duplicates!.slice(0, LIST_LIMIT).map((d, i) => (
            <li key={`${d.existing.assetId}:${d.file.name}:${i}`} style={itemStyle}>
              {typeof d.existing.folder === 'string' && d.existing.folder ? `${d.existing.folder}/` : ''}
              {d.existing.displayName}
            </li>
          ))}
          {n > LIST_LIMIT && <li style={itemStyle}>{`and ${(n - LIST_LIMIT).toLocaleString('en-US')} more`}</li>}
        </ul>
        <div style={actionsStyle}>
          <Button ref={primary} variant="primary" size="md" onClick={onUseExisting}>Use existing</Button>
          <Button variant="secondary" size="md" onClick={onKeepBoth}>Keep both</Button>
          <Button variant="ghost" size="md" onClick={onCancel}>Cancel</Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

const LIST_LIMIT = 50;

const overlayStyle: React.CSSProperties = {
  position: 'fixed', inset: 0, backgroundColor: theme.colors.overlay, display: 'flex',
  alignItems: 'center', justifyContent: 'center', zIndex: theme.zIndex.modal,
};
const dialogStyle: React.CSSProperties = {
  backgroundColor: theme.colors.surface, border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.panel, padding: 24, width: 'min(480px, calc(100vw - 32px))',
  boxShadow: theme.elevation.elevated,
};
const titleStyle: React.CSSProperties = { margin: '0 0 8px', fontSize: 'var(--fs-md)', color: theme.colors.textPrimary };
const textStyle: React.CSSProperties = { margin: '0 0 8px', fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary };
const listStyle: React.CSSProperties = { margin: '0 0 16px', paddingLeft: 18, maxHeight: 180, overflowY: 'auto' };
const itemStyle: React.CSSProperties = { fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary, wordBreak: 'break-all' };
const actionsStyle: React.CSSProperties = { display: 'flex', gap: 8, justifyContent: 'flex-end' };
