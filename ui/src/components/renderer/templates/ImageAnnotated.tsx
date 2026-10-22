import React, { useState } from 'react';
import type { RendererProps } from '../types';
import { useResolvedArtifactData } from '../../../artifacts/useArtifactMedia';

interface Annotation { x: number; y: number; w: number; h: number; label: string; color?: string; }
interface GalleryImage { src: string; label?: string; detections?: number; annotations?: Annotation[] }
interface FindingsTable { columns: { key: string; label: string }[]; rows: Record<string, unknown>[] }
interface ImageAnnotatedData {
  src: string;
  annotations: Annotation[];
  /** Every still that carried detections; lets the viewer step between frames. */
  images?: GalleryImage[];
  /** Findings that are not spatial (e.g. who was identified). */
  tables?: FindingsTable[];
  metadata?: Record<string, unknown>;
}

/**
 * ImageAnnotated — boxes drawn over a still, with the other findings alongside.
 *
 * Analysis tools return more than boxes: face recognition also reports who was
 * identified and how long they were on screen. Rendering only the image throws that
 * away, so a gallery (when several stills carried detections) and any findings
 * tables are shown with it.
 */
export default function ImageAnnotated({ data: rawData, theme: t, onInteraction }: RendererProps<ImageAnnotatedData>) {
  const [hovered, setHovered] = useState<number | null>(null);
  // Presign the still URLs here (ISSUE 2a). On the login-free deep-link path templates mount with
  // ManifestRouter's resolution turned off, so a raw `/api/artifact?key=…` or private-CDN `src`
  // reached the `<img>` token-less and the proxy answered 401 `missing_token`. This hook is a no-op
  // on the resolved path (already-presigned strings are passed through) and fails open to the raw
  // url, so it only ever ADDS a presign on the un-resolved path. `src` and `images[].src` are the
  // plain string fields it walks.
  const data = useResolvedArtifactData(rawData) ?? rawData;
  const gallery = Array.isArray(data.images) ? data.images : [];
  const [selected, setSelected] = useState(0);

  // A gallery entry may carry its own boxes; otherwise show the top-level ones,
  // which belong to the still the agent chose as primary.
  const active = gallery[selected];
  const src = active?.src ?? data.src;
  const annotations = (active?.annotations ?? (selected === 0 ? data.annotations : [])) || [];
  const tables = Array.isArray(data.tables) ? data.tables : [];
  const meta = data.metadata && typeof data.metadata === 'object' ? data.metadata : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* The frame shrink-wraps the image so the %-positioned boxes stay on it, and the image is capped at
          62vh like the other previews: a 9:16 still would otherwise be as tall as the whole canvas. */}
      <div data-testid="image-annotated-frame" style={{ position: 'relative', alignSelf: 'center', maxWidth: '100%', borderRadius: t.radius.panel, overflow: 'hidden', border: `1px solid ${t.colors.border}` }}>
        <img src={src} style={{ display: 'block', maxWidth: '100%', maxHeight: '62vh', width: 'auto', height: 'auto' }} alt="" />
        {annotations.map((ann, idx) => (
          <div key={idx}
            onMouseEnter={() => setHovered(idx)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => onInteraction?.({ type: 'click', target: `annotation-${idx}`, data: ann, timestamp: Date.now() })}
            style={{
              position: 'absolute', left: `${ann.x}%`, top: `${ann.y}%`, width: `${ann.w}%`, height: `${ann.h}%`,
              border: `2px solid ${ann.color || t.colors.accent}`, borderRadius: 4, cursor: 'pointer',
              background: hovered === idx ? 'rgba(255,153,0,0.15)' : 'transparent', transition: t.transitions.hover,
            }}>
            {hovered === idx && (
              <div style={{ position: 'absolute', bottom: '100%', left: 0, background: t.colors.textPrimary, color: 'white', padding: '2px 6px', borderRadius: 3, fontSize: 10, whiteSpace: 'nowrap', marginBottom: 2 }}>
                {ann.label}
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {annotations.map((ann, idx) => (
          <span key={idx} style={{ fontSize: t.typography.sizes.meta, padding: '2px 8px', borderRadius: t.radius.badge, background: t.colors.activeBg, color: t.colors.primary, border: `1px solid ${t.colors.border}` }}>
            {ann.label}
          </span>
        ))}
      </div>

      {gallery.length > 1 && (
        <div role="group" aria-label={`${gallery.length} analysed frames`}
          style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(96px, 1fr))', gap: 8 }}>
          {gallery.map((img, i) => (
            <button
              key={img.src}
              type="button"
              onClick={() => {
                setSelected(i); setHovered(null);
                // Report the now-visible image so the panel's "Copy source URL" copies THIS image,
                // not artifactUrls[0] (the first). The panel captures {url} from 'select' events.
                onInteraction?.({ type: 'select', target: `gallery-image-${i}`, data: { url: img.src, index: i }, timestamp: Date.now() });
              }}
              aria-current={i === selected}
              title={`${img.label ?? `frame ${i + 1}`}${img.detections ? ` · ${img.detections} detections` : ''}`}
              style={{
                padding: 0, cursor: 'pointer', display: 'block', overflow: 'hidden',
                borderRadius: 8,
                border: `2px solid ${i === selected ? t.colors.primary : t.colors.border}`,
                background: t.colors.background,
              }}
            >
              <img
                src={img.src}
                alt={img.label ?? `frame ${i + 1}`}
                loading="lazy"
                style={{
                  display: 'block', width: '100%', aspectRatio: '4 / 3', objectFit: 'cover',
                  opacity: i === selected ? 1 : 0.65, transition: t.transitions.hover,
                }}
              />
            </button>
          ))}
        </div>
      )}

      {meta && Object.keys(meta).length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, fontSize: t.typography.sizes.meta, color: t.colors.textSecondary }}>
          {Object.entries(meta).map(([k, v]) => (
            <span key={k}>{k.replace(/_/g, ' ')}: <strong style={{ color: t.colors.primary }}>{String(v)}</strong></span>
          ))}
        </div>
      )}

      {tables.map((tbl, ti) => (
        <div key={ti} style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr>
                {tbl.columns.map((c) => (
                  <th key={c.key} style={{ textAlign: 'left', padding: '4px 8px', borderBottom: `1px solid ${t.colors.border}`, color: t.colors.textSecondary, fontWeight: 500 }}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tbl.rows.map((r, ri) => (
                <tr key={ri}>
                  {tbl.columns.map((c) => (
                    <td key={c.key} style={{ padding: '4px 8px', borderBottom: `1px solid ${t.colors.border}`, color: t.colors.primary }}>
                      {r[c.key] === null || r[c.key] === undefined ? '' : String(r[c.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
