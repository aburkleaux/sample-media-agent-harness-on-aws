/**
 * The ONE large preview for anything the chat shows: an image, a video, an audio file or a document, opened
 * from its card or from a citation. Viewing is not editing: the side panel (the Renderer) is the editing
 * surface, reached by the explicit "Open in side panel". A citation opens at its moment — a video or audio
 * plays from the cited start and pauses at its end; a document opens at the cited page.
 *
 * Closes on Esc, the ✕ or a click on the backdrop, and gives focus back to whatever opened it.
 */
import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AssetIcon } from './AssetIcons';
import { useRefreshedHtml } from '../../projects/htmlEmbeds';
import { frameSandboxFor } from '../renderer/templates/artifactSrc';
import { useMoveToFolder, type MoveTarget } from './MoveToFolderButton';
import { FileLocation } from '../projects/FileLocation';
import './ChatAssets.css';

export type PreviewKind = 'image' | 'video' | 'audio' | 'document';

/** What a citation points at: a time range (video/audio) or a page range (document). */
export type PreviewRange = { startMs: number; endMs?: number } | { page: number; pageEnd?: number };

export interface AssetPreviewModalProps {
  kind: PreviewKind;
  /** The resolved, loadable URL; `null` while it is still being resolved. */
  src: string | null;
  name: string;
  /** "Video · 1280×720 · 0:05" — whatever is known. */
  sub?: string;
  range?: PreviewRange;
  /** Shown instead of the media when the asset could not be opened (removed, no access, failed). */
  error?: string | null;
  onClose: () => void;
  onOpenInPanel?: () => void;
  onDownload?: () => void;
  /** The project file this is, when it is one: offers Move to folder. */
  moveTarget?: MoveTarget;
  /** The cited range (or, with none, the media) finished playing: Play all advances to the next moment. */
  onRangeEnd?: () => void;
  /** The project an HTML file belongs to: its embedded image links are refreshed at view time (they expire). */
  projectId?: string | null;
}

const mmss = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(sec).padStart(2, '0')}`;
};

export function rangeLabel(range: PreviewRange): string {
  if ('page' in range) return range.pageEnd && range.pageEnd !== range.page ? `Pages ${range.page}–${range.pageEnd}` : `Page ${range.page}`;
  return range.endMs !== undefined ? `${mmss(range.startMs)} – ${mmss(range.endMs)}` : `From ${mmss(range.startMs)}`;
}

/** Plays a cited range: seek to its start once metadata is in, pause once at its end. */
/**
 * Plays a cited range ONCE: seek to its start once metadata is in, pause at its end, then hand control to the
 * user (pressing play again carries on past the end; nothing seeks back). Keyed on the range's numbers and the
 * src, never on the `range` object: callers build it inline, so keying on identity re-ran this on every
 * re-render of the chat and seeked back to the start mid-play, which looked like the clip looping.
 */
function useCitedRange(ref: React.RefObject<HTMLMediaElement | null>, range: PreviewRange | undefined, src: string | null,
  onRangeEnd?: () => void) {
  const endRef = useRef(onRangeEnd);
  endRef.current = onRangeEnd;
  const startMs = range && !('page' in range) ? range.startMs : undefined;
  const endMs = range && !('page' in range) ? range.endMs : undefined;
  useEffect(() => {
    const el = ref.current;
    if (!el || startMs === undefined || !src) return;
    const start = startMs / 1000, end = endMs !== undefined ? endMs / 1000 : undefined;
    let stopped = false;
    const seek = () => { try { el.currentTime = start; } catch { /* not seekable yet */ } };
    const onTime = () => {
      if (!stopped && end !== undefined && el.currentTime >= end) {
        stopped = true; el.pause(); el.removeEventListener('timeupdate', onTime); endRef.current?.();
      }
    };
    const onEnded = () => { if (!stopped) { stopped = true; endRef.current?.(); } };
    if (el.readyState >= 1) seek(); else el.addEventListener('loadedmetadata', seek, { once: true });
    el.addEventListener('timeupdate', onTime);
    el.addEventListener('ended', onEnded);
    return () => {
      el.removeEventListener('loadedmetadata', seek); el.removeEventListener('timeupdate', onTime); el.removeEventListener('ended', onEnded);
    };
  }, [ref, startMs, endMs, src]);
}

export function AssetPreviewModal({ kind, src, name, sub, range, error, onClose, onOpenInPanel, onDownload, moveTarget, onRangeEnd, projectId }: AssetPreviewModalProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } };
    window.addEventListener('keydown', onKey, true);
    return () => { window.removeEventListener('keydown', onKey, true); opener?.focus?.(); };
  }, [onClose]);
  useCitedRange(mediaRef, range, src, onRangeEnd);
  // The file's place in the project: a breadcrumb over the name, with Move beside it (you see where it is first).
  const move = useMoveToFolder(moveTarget, name);
  const htmlDoc = useRefreshedHtml(kind === 'document' ? src : null, name, projectId ?? moveTarget?.projectId);

  const docSrc = src && range && 'page' in range ? `${src.split('#')[0]}#page=${range.page}` : src;
  const stage = error
    ? <p className="cap-preview-error" role="alert">{error}</p>
    : !src
      ? <span className="cap-preview-loading" aria-label={`Loading ${name}`} />
      : kind === 'image'
        ? <img src={src} alt={name} className="cap-preview-media" />
        : kind === 'video'
          // No crossOrigin: plain playback needs no CORS, and it would refuse hosts that send none.
          ? <video ref={(el) => { mediaRef.current = el; }} src={src} controls autoPlay playsInline
              className="cap-preview-media" data-testid="asset-preview-video" />
          : kind === 'audio'
            ? <div className="cap-preview-audio">
                <AssetIcon kind="audio" size={56} />
                <audio ref={(el) => { mediaRef.current = el; }} src={src} controls autoPlay data-testid="asset-preview-audio" />
              </div>
            : htmlDoc !== null
              // Re-linked HTML: sandboxed (scripts, never same-origin), so the page cannot reach this app.
              // Its own `key`: a NEW element, sandboxed from creation. Re-using the plain frame and adding `sandbox` with
              // `srcdoc` navigates it before the flags apply, so the page ran with this app's origin and storage.
              ? <iframe key="sandboxed" srcDoc={htmlDoc} sandbox="allow-scripts allow-popups" title={name} className="cap-preview-doc"
                  data-testid="asset-preview-document" />
              // Sandboxed unless it is a PDF (Chromium will not render a PDF in a sandboxed frame).
              : <iframe key={frameSandboxFor(docSrc) ? 'plain-sandboxed' : 'plain'} src={docSrc ?? undefined}
                  sandbox={frameSandboxFor(docSrc)} title={name} className="cap-preview-doc" data-testid="asset-preview-document" />;

  return createPortal(
    <div className="cap-backdrop" data-testid="asset-preview-backdrop"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-label={`Preview of ${name}`} className="cap-preview" data-testid="asset-preview">
        <div className="cap-preview-bar">
          <div className="cap-meta">
            {move.available && move.folder !== undefined && moveTarget && (
              <FileLocation variant="crumb" dark projectId={moveTarget.projectId} folder={move.folder} />
            )}
            <div className="cap-name cap-name--light" title={name}>{name}</div>
            {sub && <div className="cap-sub cap-sub--light">{sub}</div>}
          </div>
          {range && <span className="cap-pill" data-testid="asset-preview-range">{rangeLabel(range)}</span>}
          {move.available && (
            <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 10 }}>
              <button type="button" className="cap-btn" onClick={() => (move.open ? move.close() : move.openPicker())}
                aria-haspopup="dialog" aria-expanded={move.open} data-testid="move-to-folder" title="Move to another folder">
                <AssetIcon kind="folder" size={15} /> Move
              </button>
              {move.ui}
            </span>
          )}
          {onOpenInPanel && (
            <button type="button" className="cap-btn" onClick={() => { onOpenInPanel(); onClose(); }}>
              <AssetIcon kind="panel" size={15} /> Open in side panel
            </button>
          )}
          {onDownload && (
            <button type="button" className="cap-icon cap-icon--light" onClick={onDownload} aria-label={`Download ${name}`} title="Download">
              <AssetIcon kind="download" size={17} />
            </button>
          )}
          <button ref={closeRef} type="button" className="cap-icon cap-icon--light" onClick={onClose} aria-label="Close preview" title="Close (Esc)">
            <AssetIcon kind="close" size={17} />
          </button>
        </div>
        <div className="cap-preview-stage">{stage}</div>
      </div>
    </div>,
    document.body,
  );
}
