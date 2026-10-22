/**
 * A project image shown INSIDE a chat answer: the agent writes `![caption](asset:<assetId>)` and the image sits
 * next to the explanation it belongs to (a diagram, a frame). Clicking it, or Expand, shows it large in a
 * preview over the chat; Edit in that preview opens it in the side panel (the Renderer), which is for editing.
 *
 * The id resolves in the chat's ACTIVE project through the Asset_URL_Route (the same authorised, short-lived
 * URLs Moment cards use), so the answer text never carries a URL and an old answer re-mints on view.
 */
import React, { createContext, useContext, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import { request as requestAssetUrls } from '../../projects/assetUrlBatcher';
import { openAssetAt } from '../../projects/openCitation';
import { AssetPreviewModal } from './AssetPreviewModal';
import { ChatAssetCard } from './ChatAssetCard';
import { downloadFile } from './artifactUrl';

/** The scheme an answer uses for a project file: `asset:<assetId>`. */
export const ASSET_SCHEME = 'asset:';

/** The chat's session and its active project — what an `asset:` reference resolves against. */
export interface ChatAssetContextValue {
  sessionId: string;
  projectId: string | null;
}
export const ChatAssetContext = createContext<ChatAssetContextValue | null>(null);

/** react-markdown drops URLs with unknown schemes; keep ours, defer everything else to its default. */
export function keepAssetUrls(url: string, fallback: (u: string) => string): string {
  // `asset:` (an inline project image) and `project:new?…` (a proposed project's card) are ours; the default
  // transform would blank both as unsafe schemes.
  return url.startsWith(ASSET_SCHEME) || url.startsWith('project:new?') ? url : fallback(url);
}

type Phase = { state: 'loading' } | { state: 'ready'; url: string } | { state: 'unavailable' };

export function InlineAssetImage({ src, alt }: { src: string; alt?: string }) {
  const ctx = useContext(ChatAssetContext);
  const assetId = src.slice(ASSET_SCHEME.length).trim();
  const caption = (alt || '').trim();
  const [phase, setPhase] = useState<Phase>({ state: 'loading' });
  const [openError, setOpenError] = useState<string | null>(null);
  const [preview, setPreview] = useState(false);

  useEffect(() => {
    let alive = true;
    if (!ctx?.projectId || !assetId) { setPhase({ state: 'unavailable' }); return; }
    setPhase({ state: 'loading' });
    requestAssetUrls(ctx.projectId, [assetId]).then((map) => {
      if (!alive) return;
      const out = map.get(assetId);
      const url = out && out.ok ? (out.entry.rendition?.url ?? out.entry.source?.url) : undefined;
      setPhase(url ? { state: 'ready', url } : { state: 'unavailable' });
    }).catch(() => { if (alive) setPhase({ state: 'unavailable' }); });
    return () => { alive = false; };
  }, [ctx?.projectId, assetId]);

  // Edit: the side panel, where the asset can be worked on (what Expand used to do).
  const editInPanel = async () => {
    if (!ctx?.projectId) return;
    setOpenError(null);
    const res = await openAssetAt(
      { ref: { project_id: ctx.projectId, asset_id: assetId, frame_s3_key: null }, kind: 'image', displayName: caption || assetId, startMs: 0 },
      ctx.sessionId,
    );
    if (!res.ok) setOpenError(res.message);
  };

  if (phase.state === 'unavailable') {
    return <span style={missingStyle} data-testid="inline-asset-missing">{caption || 'Image'} — not available in this project</span>;
  }
  const name = caption || 'Image';
  const url = phase.state === 'ready' ? phase.url : null;
  const download = url ? () => { void downloadFile(url, name); } : undefined;
  return (
    <span data-testid="inline-asset" data-asset-id={assetId}>
      <ChatAssetCard kind="image" name={name} src={url}
        onPreview={() => setPreview(true)} onDownload={download} onOpenInPanel={() => { void editInPanel(); }}
        moveTarget={ctx?.projectId ? { projectId: ctx.projectId, assetId } : undefined} />
      {openError && <span style={missingStyle}>{openError}</span>}
      {preview && (
        <AssetPreviewModal kind="image" src={url} name={name} onClose={() => setPreview(false)}
          onOpenInPanel={() => { void editInPanel(); }} onDownload={download}
          moveTarget={ctx?.projectId ? { projectId: ctx.projectId, assetId } : undefined} />
      )}
    </span>
  );
}

const missingStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
