/**
 * The saved moments (bookmarks) filed in the open folder, shown after its files. A bookmark is a range of a project
 * asset — no bytes, never indexed — so one take can sit in several folders. It is the app's ONE card (MediaCard):
 * Preview (the range, once) · Cut to a new tab · Open in side panel at the moment, and ⋯ Move to folder… / Remove
 * (deletes only the bookmark). A card drags onto a tree folder like a file card (`move_assets` takes bookmark ids).
 */
import React, { useCallback, useEffect, useState } from 'react';
import { listBookmarks, moveAssets, removeBookmarks, type Bookmark } from '../../projects/api';
import { openAssetAt, resolveCitationSource } from '../../projects/openCitation';
import { theme } from '../../styles/tokens';
import { AssetPreviewModal, rangeLabel, type PreviewKind } from '../chat/AssetPreviewModal';
import { MediaCard, formatSpan, type CardAction } from '../cards/MediaCard';
import { FileLocation } from './FileLocation';
import { buildBatchTrimPrompt } from '../renderer/templates/ClipGallery';
import { requestMomentClipAutoOpen } from '../../services/momentClipAutoOpen';
import { FolderPicker } from './FolderPicker';
import { ASSET_DRAG_TYPE } from './assetDrag';

export interface SavedMomentsProps {
  projectId: string;
  folder: string;
  sessionId: string | null;
  /** Bumped by the section after a move or rename, so the list re-reads. */
  refreshKey: number;
  /** How many are shown (the section hides its "folder is empty" state when there are some). */
  onCount?: (n: number) => void;
  /** No Remove or Move (a browse-only grid). */
  readOnly?: boolean;
  /** Every folder path, for Move to folder… (absent → not offered). */
  folders?: string[];
  /** A moment moved out of this folder (the tree's counts change). */
  onMoved?: () => void;
}

const previewKind = (k: string | undefined): PreviewKind =>
  (k === 'image' || k === 'audio' || k === 'document' ? k : 'video');

export function SavedMoments({ projectId, folder, sessionId, refreshKey, onCount, readOnly, folders, onMoved }: SavedMomentsProps) {
  const [items, setItems] = useState<Bookmark[]>([]);
  const [preview, setPreview] = useState<{ b: Bookmark; src: string | null; error: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [moving, setMoving] = useState<string | null>(null);

  const load = useCallback(() => {
    let live = true;
    void listBookmarks(projectId, folder).then((r) => {
      if (!live) return;
      const list = r.ok ? r.value : [];
      setItems(list);
      onCount?.(list.length);
    });
    return () => { live = false; };
  }, [projectId, folder, onCount]);
  useEffect(() => load(), [load, refreshKey]);

  const name = (b: Bookmark) => b.assetDisplayName || b.assetId;
  const ref = (b: Bookmark) => ({ project_id: projectId, asset_id: b.assetId, frame_s3_key: null });
  const open = async (b: Bookmark) => {
    setPreview({ b, src: null, error: null });
    const r = await resolveCitationSource(ref(b), name(b));
    setPreview((cur) => (cur?.b.bookmarkId === b.bookmarkId
      ? { b, src: r.ok ? r.url : null, error: r.ok ? null : r.message } : cur));
  };
  const toPanel = async (b: Bookmark) => {
    if (!sessionId) return;
    const end = b.endMs ?? b.startMs;
    const res = await openAssetAt({ ref: ref(b), kind: previewKind(b.assetKind), displayName: name(b),
      startMs: b.startMs, range: { start: b.startMs / 1000, end: end / 1000 } }, sessionId);
    if (!res.ok) setError(res.message);
  };
  const remove = async (b: Bookmark) => {
    const r = await removeBookmarks(projectId, [b.bookmarkId]);
    if (r.ok) {
      setItems((cur) => {
        const next = cur.filter((x) => x.bookmarkId !== b.bookmarkId);
        onCount?.(next.length);
        return next;
      });
    } else setError(`Couldn't remove the moment of ${name(b)}`);
  };

  // Cut: the same one-clip turn a Moments card sends (the clip opens as a new tab when it is ready). The agent
  // gets the source; the chat bubble shows only the clean request.
  const cut = async (b: Bookmark) => {
    if (!sessionId || b.endMs === undefined || b.endMs <= b.startMs) return;
    const r = await resolveCitationSource(ref(b), name(b));
    if (!r.ok) { setError(r.message); return; }
    const clean = buildBatchTrimPrompt(b.assetKind === 'audio' ? 'trim' : 'clip',
      [{ label: name(b), in: b.startMs / 1000, out: b.endMs / 1000 }]);
    requestMomentClipAutoOpen(sessionId, name(b));
    window.dispatchEvent(new CustomEvent('chat:send', { detail: { prompt: `${clean} [source: ${r.url}]`, displayText: clean, sessionId } }));
  };
  const moveTo = async (b: Bookmark, to: string) => {
    setMoving(null);
    if (to === folder) return;
    const r = await moveAssets(projectId, [b.bookmarkId], to);
    if (r.ok && r.value.moved.includes(b.bookmarkId)) {
      setItems((cur) => {
        const next = cur.filter((x) => x.bookmarkId !== b.bookmarkId);
        onCount?.(next.length);
        return next;
      });
      onMoved?.();
    } else setError(`Couldn't move the moment of ${name(b)}`);
  };

  if (items.length === 0) return null;
  return (
    <section aria-label="Saved moments" style={wrapStyle} data-testid="saved-moments">
      <h3 style={titleStyle}>Saved moments <span style={countStyle}>{items.length}</span></h3>
      {error && <p role="alert" style={errorStyle}>{error}</p>}
      <ul style={gridStyle}>
        {items.map((b) => {
          const range = { startMs: b.startMs, ...(b.endMs !== undefined ? { endMs: b.endMs } : {}) };
          const kind = previewKind(b.assetKind);
          const at = rangeLabel(range);
          const hasSpan = b.endMs !== undefined && b.endMs > b.startMs;
          const menu: CardAction[] = [];
          if (!readOnly && folders) menu.push({ label: 'Move to folder…', icon: 'folder', onClick: () => setMoving(b.bookmarkId) });
          if (!readOnly) menu.push({ label: 'Remove', icon: 'trash', danger: true, onClick: () => void remove(b) });
          return (
            <MediaCard key={b.bookmarkId} kind={kind} title={b.label || name(b)} titleTip={name(b)}
              meta={b.label ? name(b) : undefined}
              location={<FileLocation projectId={projectId} assetId={b.assetId} hideIn={folder} />}
              badgeLeft={kind === 'image' || kind === 'document' ? undefined : at.replace(' – ', '–')}
              badge={hasSpan ? formatSpan(b.endMs! - b.startMs) : undefined}
              onPreview={() => void open(b)}
              previewLabel={`Preview ${name(b)} at ${at}`}
              primary={sessionId && hasSpan && (kind === 'video' || kind === 'audio')
                ? { label: `Cut ${name(b)} at ${at} to a new tab`, icon: 'cut', onClick: () => void cut(b), reason: 'Cut to a new tab' }
                : undefined}
              onOpenInPanel={sessionId ? () => void toPanel(b) : undefined}
              panelLabel={`Open ${name(b)} at ${at} in the side panel`}
              menu={menu}
              testId="saved-moment" dataAttrs={{ 'data-bookmark-id': b.bookmarkId }}
              onDragStart={readOnly ? undefined : (e) => {
                e.dataTransfer.setData(ASSET_DRAG_TYPE, JSON.stringify([b.bookmarkId]));
                e.dataTransfer.setData('text/plain', b.label || name(b));
                e.dataTransfer.effectAllowed = 'move';
              }}>
              {moving === b.bookmarkId && folders && (
                <FolderPicker folders={folders} current={folder} title="Move the moment to" align="right"
                  onPick={(f) => void moveTo(b, f)} onClose={() => setMoving(null)} />
              )}
            </MediaCard>
          );
        })}
      </ul>
      {preview && (
        <AssetPreviewModal kind={previewKind(preview.b.assetKind)} src={preview.src} name={name(preview.b)}
          range={{ startMs: preview.b.startMs, ...(preview.b.endMs !== undefined ? { endMs: preview.b.endMs } : {}) }}
          error={preview.error} onClose={() => setPreview(null)}
          moveTarget={readOnly ? undefined : { projectId, assetId: preview.b.assetId }}
          onOpenInPanel={sessionId ? () => void toPanel(preview.b) : undefined} />
      )}
    </section>
  );
}

const wrapStyle: React.CSSProperties = { marginTop: 18 };
const titleStyle: React.CSSProperties = { margin: '0 0 8px', fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textSecondary };
const countStyle: React.CSSProperties = { fontWeight: 400, color: theme.colors.textTertiary, marginLeft: 4 };
const errorStyle: React.CSSProperties = { color: theme.colors.error, fontSize: 'var(--fs-sm)', margin: '0 0 8px' };
const gridStyle: React.CSSProperties = {
  listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12,
};
