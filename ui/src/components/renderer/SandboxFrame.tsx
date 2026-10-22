import React, { useRef, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import { htmlHasArtifactUrls, resolveArtifactUrlsInHtml } from '../../artifacts/artifactUrls';

interface SandboxFrameProps {
  html: string;
  title: string;
  onInteraction?: (event: { type: string; target: string; data?: unknown; timestamp: number }) => void;
}

/*
 * Literal font stacks, deliberately NOT `theme.typography.fontFamily`.
 *
 * Everything below is interpolated as text into an `srcDoc` iframe — a separate
 * document. A `var(--font-body)` there would resolve against the IFRAME's
 * `:root`, where the app's custom properties do not exist, so the body would
 * fall back to the browser default serif. These mirror `global.css`, which is
 * the one place the values are authored; keep them in step by hand.
 *
 * NOTE (pre-existing, not addressed here): the `--renderer-color-*` values below
 * have the same problem — `theme.colors.*` are themselves `var(--color-*)`
 * references, so they arrive in the iframe unresolvable. Fixing that needs the
 * computed values read at runtime, which is a behaviour change rather than a
 * styling one.
 */
const IFRAME_FONT_BODY = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
const IFRAME_FONT_MONO = "'IBM Plex Mono', 'SF Mono', 'Fira Code', 'Consolas', monospace";

const cssVarsForIframe = `
:root {
  --renderer-color-primary: ${theme.colors.primary};
  --renderer-color-accent: ${theme.colors.accent};
  --renderer-color-text: ${theme.colors.textPrimary};
  --renderer-color-text-secondary: ${theme.colors.textSecondary};
  --renderer-color-surface: ${theme.colors.surface};
  --renderer-color-background: ${theme.colors.background};
  --renderer-color-border: ${theme.colors.border};
  --renderer-font-body: ${IFRAME_FONT_BODY};
  --renderer-font-mono: ${IFRAME_FONT_MONO};
  --renderer-radius: ${theme.radius.panel}px;
  --renderer-spacing-unit: ${theme.spacing.unit}px;
}
body { font-family: ${IFRAME_FONT_BODY}; color: ${theme.colors.textPrimary}; margin: 0; padding: 12px; }
`;

export default function SandboxFrame({ html, onInteraction }: SandboxFrameProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(200);

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (e.source !== iframeRef.current?.contentWindow) return;
      if (e.data?.type === 'renderer-interaction') {
        onInteraction?.(e.data.payload);
      }
      if (e.data?.type === 'renderer-resize') {
        setHeight(Math.min(e.data.height || 200, 600));
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [onInteraction]);

  // Agent HTML references artifacts by their stable URL; substitute short-lived presigned URLs
  // before building srcDoc, so the frame does not depend on the public CDN. HTML without artifact
  // URLs renders at once; otherwise the frame waits for the (never-rejecting) resolution rather
  // than loading the unresolved document and reloading it.
  const [resolved, setResolved] = useState<{ html: string; out: string } | null>(() =>
    htmlHasArtifactUrls(html) ? null : { html, out: html },
  );
  useEffect(() => {
    if (resolved?.html === html) return;
    if (!htmlHasArtifactUrls(html)) { setResolved({ html, out: html }); return; }
    let alive = true;
    void resolveArtifactUrlsInHtml(html)
      .catch(() => html)
      .then((out) => { if (alive) setResolved({ html, out }); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [html]);
  const bodyHtml = resolved?.html === html ? resolved.out : null;

  const wrappedHtml = bodyHtml === null ? '' : `<!DOCTYPE html><html><head><style>${cssVarsForIframe}</style></head><body>${bodyHtml}
<script>
new ResizeObserver(()=>{
  parent.postMessage({type:'renderer-resize',height:document.body.scrollHeight},'*');
}).observe(document.body);
</script></body></html>`;

  return (
    <iframe
      ref={iframeRef}
      srcDoc={wrappedHtml}
      sandbox="allow-scripts"
      style={{ width: '100%', height, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface }}
    />
  );
}
