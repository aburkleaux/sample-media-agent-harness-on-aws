import React, { useState } from 'react';
import type { RendererProps, ThemeTokens } from '../types';
import type { RenderManifest } from '../../nle/manifestParser';
import { parseManifest } from '../../nle/manifestParser';
import MetadataPanel from '../../nle/MetadataPanel';
import { authFetch } from '../../../auth/authFetch';
import { resolveArtifactUrls } from '../../../artifacts/artifactUrls';
import { frameSandboxFor, toProxySrc } from './artifactSrc';
import DocumentIcon from '../../icons/DocumentIcon';

interface DocumentViewerData { manifestUrl?: string; manifest?: RenderManifest }

// Resolution moved to ./artifactSrc so it can FAIL CLOSED and be tested without a DOM. The
// version here ended with `return url`, so an unrecognised reference went straight to
// `<iframe src>` — and a relative or same-origin path resolves against the SPA origin, where
// CloudFront answers with index.html and the harness renders itself inside its own viewer.

/**
 * Shown INSTEAD of an iframe when the manifest offered a preview that `toProxySrc` refused (not a
 * recognised artifact URL, or a malformed one such as two links joined by `\n`). Framing it anyway
 * shows whatever the server answers for a bad key, which for `/api/artifact` is a raw JSON 400 body
 * (session 865950fd). The offending value stays available, muted and clamped, for debugging.
 */
function PreviewUnavailable({ theme: t, previewUrl }: { theme: ThemeTokens; previewUrl?: string }) {
  return (
    <div
      role="status"
      data-testid="document-preview-unavailable"
      style={{
        margin: 12, padding: '28px 20px', textAlign: 'center',
        background: t.colors.surfaceSubtle, border: `1px dashed ${t.colors.borderStrong}`,
        borderRadius: t.radius.panel, color: t.colors.textSecondary,
      }}
    >
      <div style={{ fontSize: t.typography.sizes.heading, fontWeight: t.typography.weights.semibold, color: t.colors.textPrimary }}>
        Preview unavailable
      </div>
      <div style={{ marginTop: 6, fontSize: t.typography.sizes.body }}>
        This document’s preview link isn’t a valid artifact URL, so it can’t be displayed here.
      </div>
      {previewUrl && (
        <div
          title={previewUrl}
          style={{
            marginTop: 10, fontSize: t.typography.sizes.meta, color: t.colors.textTertiary,
            fontFamily: t.typography.fontMono, wordBreak: 'break-all',
            overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
          }}
        >
          {previewUrl}
        </div>
      )}
    </div>
  );
}

function docTitle(d: { title?: string; previewUrl: string }): string {
  if (typeof d.title === 'string' && d.title) return d.title;
  const last = d.previewUrl.split('?')[0].replace(/\/+$/, '').split('/').pop() || 'Document';
  try { return decodeURIComponent(last); } catch { return last; }
}

/** One tab per document of a multi-document manifest: a single row that scrolls, never wraps. */
function DocumentTabs({ theme: t, docs, active, onSelect }: {
  theme: ThemeTokens;
  docs: { title?: string; previewUrl: string }[];
  active: number;
  onSelect: (i: number) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Documents"
      data-testid="document-tabs"
      style={{ display: 'flex', gap: 4, padding: '8px 8px 0', overflowX: 'auto', whiteSpace: 'nowrap', borderBottom: `1px solid ${t.colors.border}` }}
    >
      {docs.map((d, i) => {
        const selected = i === active;
        const name = docTitle(d);
        return (
          <button
            key={`${i}-${d.previewUrl}`}
            type="button"
            role="tab"
            aria-selected={selected}
            data-testid={`document-tab-${i}`}
            title={name}
            onClick={() => onSelect(i)}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: 260, flexShrink: 0,
              padding: '6px 10px', marginBottom: -1, cursor: 'pointer',
              background: selected ? t.colors.surfaceSubtle : 'transparent',
              color: selected ? t.colors.textPrimary : t.colors.textSecondary,
              border: `1px solid ${selected ? t.colors.border : 'transparent'}`,
              borderBottomColor: selected ? t.colors.surfaceSubtle : 'transparent',
              borderRadius: `${t.radius.button}px ${t.radius.button}px 0 0`,
              fontSize: t.typography.sizes.meta, fontWeight: selected ? t.typography.weights.semibold : undefined,
            }}
          >
            <DocumentIcon size={14} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
          </button>
        );
      })}
    </div>
  );
}

/**
 * DocumentViewer — renders a `document-viewer` manifest: an embedded PDF
 * preview plus optional outline and metadata panels (data-driven).
 */
export default function DocumentViewer({ data, theme: t }: RendererProps<DocumentViewerData>) {
  const [manifest, setManifest] = useState<RenderManifest | null>(data.manifest ?? null);
  const [error, setError] = useState<string | null>(null);

  React.useEffect(() => {
    if (manifest || !data.manifestUrl) return;
    let cancelled = false;
    // authFetch: the manifest is served by the auth-protected /api/artifact
    // proxy, so it needs the bearer token (plain fetch 401'd in the hosted
    // deployment; local dev masked it via DEV_BYPASS_AUTH).
    authFetch(data.manifestUrl)
      .then((r) => r.text())
      .then((txt) => {
        const res = parseManifest(txt);
        if (cancelled) return;
        if (res.ok) setManifest(res.manifest);
        else setError(res.errors.map((e) => e.message).join('; '));
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => { cancelled = true; };
  }, [data.manifestUrl, manifest]);

  // The preview is embedded via <iframe src> (and offered as an "Open" link), neither of which can
  // carry an Authorization header. A document routed through the /api/artifact proxy therefore loads
  // with a VIEW TICKET (ui/server/artifacts/viewTicket.ts): valid for that one document, read-only,
  // one hour. NEVER the user's access token — the document is agent-authored, can read its own URL,
  // and a URL also lands in history, logs and Referer headers. Resolved async (a POST mints it).
  const [tokenizedSrc, setTokenizedSrc] = useState<string | undefined>(undefined);
  // A multi-document render (one PDF per video, session d6a560a8) lists every document; `document`
  // stays the first for older manifests. The selector picks which one is framed.
  const docs = (manifest?.documents ?? []).filter((d) => d && typeof d.previewUrl === 'string' && d.previewUrl);
  const [docIndex, setDocIndex] = useState(0);
  const doc = docs.length > 1 ? docs[Math.min(docIndex, docs.length - 1)] : manifest?.document;
  // null (not the original url) when the reference can't be vouched for — see artifactSrc.ts.
  const rawSrc = doc?.previewUrl
    ? toProxySrc(doc.previewUrl, window.location.origin)
    : undefined;
  // Distinguish "no preview offered" from "a preview was offered and we refused it": the
  // second is a data problem worth surfacing, not an empty state.
  const refusedSrc = Boolean(doc?.previewUrl) && rawSrc === null;
  React.useEffect(() => {
    let cancelled = false;
    if (!rawSrc) { setTokenizedSrc(undefined); return; }
    if (!rawSrc.startsWith('/api/artifact')) { setTokenizedSrc(rawSrc); return; }
    // Every type, not only HTML: the ticketed URL is what the proxy serves with the isolation headers
    // and a policed content type. A failed mint keeps the bare proxy path — it carries no credential
    // (and still loads where artifacts are not gated); it never falls back to a token.
    const key = new URLSearchParams(rawSrc.split('?')[1] || '').get('key') || '';
    const bare = key ? `/api/artifact?key=${encodeURIComponent(key)}` : rawSrc;
    if (!key) { setTokenizedSrc(bare); return; }
    void resolveArtifactUrls([key], { frame: true }).then((m) => {
      if (!cancelled) setTokenizedSrc(m.get(key)?.url ?? bare);
    }).catch(() => !cancelled && setTokenizedSrc(bare));
    return () => { cancelled = true; };
  }, [rawSrc]);

  if (error) {
    return <div style={{ padding: 16, color: t.colors.textSecondary }}>Failed to load document: {error}</div>;
  }
  if (!manifest) {
    return <div style={{ padding: 16, color: t.colors.textSecondary }}>Loading document…</div>;
  }

  // `doc` and the tokenized preview `src` are computed above (hooks must run
  // unconditionally, before the early returns).
  const src = tokenizedSrc;
  // Sandboxed unless it is a PDF (Chromium will not render a PDF in a sandboxed frame; see artifactSrc).
  const sandbox = frameSandboxFor(src);
  const outline = doc?.outline || [];

  return (
    // Root sizes to its content (no flex:1 / overflow:hidden): the iframe has a fixed
    // height and the outline/metadata/link panels stack below it, so the parent
    // `renderer-content` scroller can reach the bottom. A flex:1 + overflow:hidden root
    // clipped everything past a viewport-tall iframe (the "can't scroll to bottom" bug).
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, overflow: 'visible' }}>
      {docs.length > 1 && (
        <DocumentTabs theme={t} docs={docs} active={Math.min(docIndex, docs.length - 1)} onSelect={setDocIndex} />
      )}
      {src ? (
        <iframe
          // A new element per sandbox mode: the flags apply only when a frame is created.
          key={sandbox ? 'sandboxed' : 'pdf'}
          title="Document preview"
          src={src}
          sandbox={sandbox}
          data-testid="document-preview-frame"
          // Interactive HTML (e.g. a game/slideshow) receives keyboard events once the
          // iframe is focused. Focus the ELEMENT with preventScroll (not contentWindow,
          // which scrolls the iframe into view and snapped the render pane back to the
          // top on every poll-driven reload). User can still click in to focus content.
          onLoad={(e) => { try { (e.currentTarget as HTMLIFrameElement).focus({ preventScroll: true }); } catch { /* focus unsupported: user clicks to focus */ } }}
          style={{ width: '100%', height: '78vh', minHeight: 520, border: 'none', background: '#333' }}
        />
      ) : refusedSrc ? (
        <PreviewUnavailable theme={t} previewUrl={doc?.previewUrl} />
      ) : (
        <div style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary }}>No document preview available</div>
      )}
      {typeof doc?.pageCount === 'number' && (
        <div style={{ padding: '0 12px', fontSize: 12, color: t.colors.textSecondary }}>{doc.pageCount} pages</div>
      )}
      {outline.length > 0 && (
        <div style={{ padding: '4px 12px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: t.colors.textSecondary, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4 }}>Outline</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: t.colors.textPrimary }}>
            {outline.map((o, i) => (
              <li key={i}>{o.title}{typeof o.page === 'number' ? ` — p.${o.page}` : ''}</li>
            ))}
          </ul>
        </div>
      )}
      {manifest.metadata && Object.keys(manifest.metadata).length > 0 && (
        <MetadataPanel metadata={manifest.metadata} />
      )}
      {src && (
        <div style={{ padding: '6px 12px' }}>
          <a href={src} target="_blank" rel="noopener noreferrer" data-testid="document-open-link" style={{ fontSize: 12, color: t.colors.primary }}>Open document ↗</a>
        </div>
      )}
    </div>
  );
}
