/**
 * Import from S3: the project's import inbox. Upload is the ingest — files copied to the inbox prefix with any S3
 * tool (`aws s3 sync`, DataSync, Snowball) are registered in place as they land (swept once a minute), filed in the
 * folder their path names, and indexed like uploads. This panel shows where to copy, how far indexing has got, and
 * what needs attention (camera RAW, files over the size limit) with what to do about it. No start button: Scan now
 * only makes the next sweep happen immediately.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { getImportStatus, rescanImport, type ImportStatus } from '../../projects/api';
import { formatBytes } from '../../projects/upload/kinds';
import { theme } from '../../styles/tokens';
import ConfirmDialog from '../ConfirmDialog';

const REASON: Record<string, { label: string; help: string }> = {
  not_supported: { label: 'Not supported', help: "Camera RAW can't be decoded here. Export ProRes or H.264 proxies into the inbox and they'll ingest." },
  too_large: { label: 'Too large', help: 'Over the per-file size limit. Ask an admin to raise it, then Scan now.' },
  empty: { label: 'Empty', help: 'The file has no bytes; copy it again.' },
};

const POLL_MS = 10_000;

export function ImportPanel({ projectId, open, onClose }: { projectId: string; open: boolean; onClose: () => void }) {
  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    const r = await getImportStatus(projectId);
    if (r.ok) { setStatus(r.value); setError(null); }
    else setError(r.kind === 'error' ? r.message : "Couldn't read the import inbox.");
  }, [projectId]);
  useEffect(() => {
    if (!open) return;
    void load();
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [open, load]);

  const command = status ? `aws s3 sync ./MyShoot s3://${status.bucket}/${status.prefix}` : '';
  const sc = status?.statusCounts ?? {};
  const total = Object.entries(sc).filter(([k]) => k !== 'removed').reduce((n, [, v]) => n + (Number(v) || 0), 0);
  const indexed = Number(sc.indexed) || 0;
  const groups = new Map<string, ImportStatus['attention']>();
  for (const a of status?.attention ?? []) groups.set(a.reason, [...(groups.get(a.reason) ?? []), a]);

  const scan = async () => {
    setScanning(true);
    const r = await rescanImport(projectId);
    setScanning(false);
    if (!r.ok) setError(r.kind === 'error' ? r.message : 'Scanning failed; try again.');
    void load();
  };

  return (
    <ConfirmDialog open={open} title="Import from S3" message="" confirmLabel={scanning ? 'Scanning…' : 'Scan now'}
      cancelLabel="Close" onConfirm={() => void scan()} onCancel={onClose} loading={scanning} maxWidth={640}
      confirmVariant="primary"
      error={error ?? undefined}>
      <div style={bodyStyle} data-testid="import-panel">
        <p style={pStyle}>Copy files into this project with any S3 tool. Each file is registered and indexed as it lands,
          and the folders you copy become project folders.</p>
        {status && (
          <>
            <div style={cmdRowStyle}>
              <code style={cmdStyle} data-testid="import-command">{command}</code>
              <button type="button" style={copyBtnStyle} onClick={() => {
                void navigator.clipboard?.writeText(command).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
              }}>{copied ? 'Copied' : 'Copy'}</button>
            </div>
            <p style={hintStyle}>
              <code>import/Day1/Drone/x.mov</code> lands in <b>Day1 / Drone</b>. Re-running the sync adds nothing twice.
              For multi-terabyte shoots use AWS DataSync or Snowball to the same path.
              {!status.indexing && ' Indexing is off for this project, so files are stored but not searchable until an owner turns it on.'}
            </p>
            <div style={cardsStyle}>
              <div style={cardStyle} data-testid="import-indexed"><span style={kStyle}>Indexed</span>
                <span style={vStyle}>{indexed.toLocaleString('en-US')}</span><span style={uStyle}>of {total.toLocaleString('en-US')} files</span></div>
              <div style={cardStyle} data-testid="import-attention"><span style={kStyle}>Needs attention</span>
                <span style={vStyle}>{status.counts.attention.toLocaleString('en-US')}</span><span style={uStyle}>not ingested</span></div>
              <div style={cardStyle}><span style={kStyle}>Ignored</span>
                <span style={vStyle}>{status.counts.ignored.toLocaleString('en-US')}</span><span style={uStyle}>sidecars, system files</span></div>
            </div>
            {groups.size > 0 && (
              <div>
                <div style={headStyle}>Needs attention</div>
                <ul style={listStyle}>
                  {[...groups].map(([reason, items]) => (
                    <li key={reason} style={itemStyle} data-testid="import-attention-group" data-reason={reason}>
                      <b>{(REASON[reason]?.label ?? reason)} · {items.length}</b>
                      <span style={uStyle}>{REASON[reason]?.help ?? ''}</span>
                      <span style={keysStyle}>{items.slice(0, 3).map((i) => `${i.key.slice(status.prefix.length)} (${formatBytes(i.size)})`).join(' · ')}
                        {items.length > 3 ? ` · +${items.length - 3} more` : ''}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </ConfirmDialog>
  );
}

// `minmax(0, 1fr)`: the one-line copy command scrolls inside its box instead of widening the dialog.
const bodyStyle: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 14, textAlign: 'left' };
const pStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary };
const cmdRowStyle: React.CSSProperties = { display: 'flex', gap: 8, alignItems: 'center' };
const cmdStyle: React.CSSProperties = {
  flex: 1, minWidth: 0, overflowX: 'auto', whiteSpace: 'nowrap', padding: '10px 12px', borderRadius: theme.radius.panel,
  background: theme.colors.surfaceSubtle, border: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-xs)',
};
const copyBtnStyle: React.CSSProperties = {
  height: 32, padding: '0 12px', borderRadius: theme.radius.button, border: `1px solid ${theme.colors.borderStrong}`,
  background: theme.colors.surface, color: theme.colors.textPrimary, font: 'inherit', fontSize: 'var(--fs-sm)', cursor: 'pointer',
};
const hintStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const cardsStyle: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 10 };
const cardStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 2, padding: 12, borderRadius: theme.radius.panel, border: `1px solid ${theme.colors.border}`,
};
const kStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', fontWeight: 600, color: theme.colors.textTertiary };
const vStyle: React.CSSProperties = { fontSize: 'var(--fs-xl)', fontWeight: 700, color: theme.colors.textPrimary };
const uStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const headStyle: React.CSSProperties = { fontWeight: 600, fontSize: 'var(--fs-sm)', marginBottom: 6 };
const listStyle: React.CSSProperties = { listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 };
const itemStyle: React.CSSProperties = {
  display: 'grid', gap: 2, padding: '8px 10px', borderRadius: theme.radius.panel, background: theme.colors.surfaceSubtle,
  fontSize: 'var(--fs-sm)',
};
const keysStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, overflow: 'hidden', textOverflow: 'ellipsis' };
