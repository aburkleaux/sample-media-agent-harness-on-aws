import { useContext, useRef, useState } from 'react';
import { classifyUrl, deriveArtifactKey, downloadFile } from './artifactUrl';
import { artifactKeyOf, isArtifactKey, resolveArtifactUrls } from '../../artifacts/artifactUrls';
import { useArtifactMediaResign, useResolvedArtifactUrl } from '../../artifacts/useArtifactMedia';
import { AssetPreviewModal } from './AssetPreviewModal';
import { ChatAssetCard } from './ChatAssetCard';
import { ChatAssetContext } from './InlineAssetImage';
import './ArtifactLink.css';

/** Navigate to a presigned `attachment` URL: the browser downloads it without leaving the page. */
function downloadViaUrl(href: string): void {
  const a = document.createElement('a');
  a.href = href;
  a.rel = 'noopener noreferrer';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

interface ArtifactLinkProps {
  href?: string;
  src?: string;
  alt?: string;
  children?: React.ReactNode;
  /** Optional exact artifacts domain for precise trusted/our-artifact classification. */
  artifactsDomain?: string;
}

/**
 * The single smart renderer for any URL that appears in chat — used by the markdown `a`/`img`
 * overrides and (later) the uploaded-file preview. Classifies by extension and renders inline
 * media as ONE ChatAssetCard (image, video, audio, document: same card, same three actions — Preview,
 * Download, Open in side panel), and anything else as a safe link. Trusted-host images auto-embed;
 * untrusted-host images are click-to-load (no auto-fetch of arbitrary hosts).
 */
export function ArtifactLink({ href, src, alt, children, artifactsDomain }: ArtifactLinkProps) {
  const url = (href ?? src ?? '').trim();
  // The chat's active project: a link to one of its files can be filed in another folder from the preview.
  const chat = useContext(ChatAssetContext);
  // An untrusted-host image is fetched only once the user asks (consent).
  const [expanded, setExpanded] = useState(false);
  // The large preview (a modal over the chat). Viewing is not editing: the side panel is for editing.
  const [preview, setPreview] = useState(false);
  const [docSrc, setDocSrc] = useState<string | null>(null);
  // One re-sign listener for this link's inline media (an expired presigned URL is re-resolved).
  const hostRef = useRef<HTMLSpanElement>(null);
  useArtifactMediaResign(hostRef);

  const info = classifyUrl(url, { artifactsDomain });
  const name = info.filename;
  // Inline media loads a presigned URL resolved from the artifact identifier; it is resolved only
  // when the element is actually shown. Non-artifact URLs (fal, third-party) load directly.
  const showsInline =
    (info.kind === 'image' && (info.trustedHost || expanded)) ||
    info.kind === 'video' ||                     // its first frame is the card's preview
    info.kind === 'audio';                       // it plays in the card
  const inline = useResolvedArtifactUrl(url && showsInline ? url : undefined, artifactsDomain);

  if (!url) return <>{children}</>;

  // Our own artifacts: Open and Download resolve the identifier to a presigned URL AT CLICK TIME
  // (Download with `attachment`), so neither depends on a public CDN or a public /api/artifact.
  // The /api/artifact read-through stays as the link's href (modified clicks) and as the fallback
  // when resolution fails; it serves a guessed content-type, and works as a plain link only while
  // the environment's /api/artifact is public (artifactsCdnPrivate off).
  // External URLs use the direct path.
  const _key = info.ourArtifact ? deriveArtifactKey(url) : null;
  const viewHref = _key ? `/api/artifact?key=${encodeURIComponent(_key)}` : url;
  const dlUrl = _key ? `/api/artifact?key=${encodeURIComponent(_key)}&download=1` : url;
  const resolveKey = artifactKeyOf(url, artifactsDomain) ?? (_key && isArtifactKey(_key) ? _key : null);

  const onOpen = (e: React.MouseEvent) => {
    if (!resolveKey) return; // native navigation to the external URL
    // Modified clicks (new tab/window via the keyboard) keep the plain href.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    // Open the window inside the click gesture (popup blockers), then point it at the URL.
    const win = window.open('', '_blank');
    if (win) { try { win.opener = null; } catch { /* cross-origin guard */ } }
    void resolveArtifactUrls([resolveKey]).then((m) => {
      const target = m.get(resolveKey)?.url ?? viewHref;
      if (win && !win.closed) win.location.href = target;
      else window.open(target, '_blank', 'noopener,noreferrer');
    });
  };

  const onDownload = (e: React.MouseEvent) => {
    e.preventDefault();
    void (async () => {
      if (resolveKey) {
        const r = (await resolveArtifactUrls([resolveKey], { download: true })).get(resolveKey);
        if (r) { downloadViaUrl(r.url); return; }
      }
      await downloadFile(dlUrl, name);
    })();
  };

  const downloadBtn = (
    <button
      type="button"
      className="artifact-btn"
      title={`Download ${name}`}
      onClick={onDownload}
    >
      ⤓ Download
    </button>
  );
  // The side panel (Renderer) is the editing surface: timeline, crop, marks.
  const editInPanel = () => window.dispatchEvent(new CustomEvent('lab:render-urls', {
    detail: { urls: [url], label: (typeof alt === 'string' && alt) || name } }));
  const openPreview = () => setPreview(true);
  const openDocPreview = () => {
    setPreview(true);
    if (!resolveKey) { setDocSrc(url); return; }
    void resolveArtifactUrls([resolveKey]).then((m) => setDocSrc(m.get(resolveKey)?.url ?? viewHref));
  };
  if (info.kind === 'image' || info.kind === 'video' || info.kind === 'audio' || info.kind === 'document') {
    const kind = info.kind;
    const ext = (name.split('.').pop() || '').toUpperCase();
    const ready = kind === 'document' ? docSrc : (inline.key ? (inline.src || null) : url);
    return (
      <span ref={hostRef} className="artifact-card-host">
        <ChatAssetCard
          kind={kind}
          name={name}
          src={kind === 'document' ? null : ready}
          docLabel={ext.length <= 5 ? ext : undefined}
          needsConsent={kind === 'image' && !info.trustedHost && !expanded}
          onConsent={() => setExpanded(true)}
          onPreview={kind === 'audio' ? undefined : kind === 'document' ? openDocPreview : openPreview}
          onDownload={() => onDownload({ preventDefault() {} } as React.MouseEvent)}
          onOpenInPanel={editInPanel}
          moveTarget={chat?.projectId && resolveKey ? { projectId: chat.projectId, location: url } : undefined}
        />
        {preview && (
          <AssetPreviewModal kind={kind} src={ready} name={name}
            moveTarget={chat?.projectId && resolveKey ? { projectId: chat.projectId, location: url } : undefined}
            onClose={() => setPreview(false)} onOpenInPanel={editInPanel}
            onDownload={() => onDownload({ preventDefault() {} } as React.MouseEvent)} />
        )}
      </span>
    );
  }

  // Plain (non-media) link — open safely in a new tab, in the theme accent color. Our own artifact
  // (e.g. a `.json` sidecar) resolves at click time like Open: its raw URL is the private artifacts
  // CDN (403 since Phase 3 (c)).
  if (resolveKey) {
    return <a className="artifact-plain-link" href={viewHref} target="_blank" rel="noopener noreferrer" onClick={onOpen}>{children || name}</a>;
  }
  return <a className="artifact-plain-link" href={url} target="_blank" rel="noopener noreferrer">{children || url}</a>;
}
