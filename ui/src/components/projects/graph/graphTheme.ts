/**
 * Graph colours from the theme. Sigma draws with WebGL and cannot use `var(--x)` strings, so the palette is
 * resolved from the computed `--graph-*` custom properties (defined per theme in `styles/global.css`) and
 * re-resolved whenever `html[data-theme]` changes.
 */
import { useEffect, useState } from 'react';

/** Schema order (agent `project_graph/schema.py::NODE_LABELS`) → `--graph-type-<n>`. */
export const NODE_TYPES = [
  'Project', 'User', 'Tag', 'Asset', 'Scene', 'Shot', 'Person', 'Location', 'Organization', 'Topic',
  // asset-lineage Phase 3: lineage nodes (DERIVED_FROM / PRODUCED_BY). Append only — the colour is
  // `--graph-type-<index+1>`, so reordering would recolour every existing type.
  'Session', 'Job',
] as const;

export interface GraphPalette {
  types: Record<string, string>;
  edge: string;
  edgeActive: string;
  label: string;
  dim: string;
}

const FALLBACK = 'gray';

function readVar(style: CSSStyleDeclaration | null, name: string): string {
  const v = style?.getPropertyValue(name).trim();
  return v || FALLBACK;
}

export function resolveGraphPalette(root: Element | null = typeof document === 'undefined' ? null : document.documentElement): GraphPalette {
  const style = root && typeof getComputedStyle === 'function' ? getComputedStyle(root) : null;
  const types: Record<string, string> = {};
  NODE_TYPES.forEach((t, i) => { types[t] = readVar(style, `--graph-type-${i + 1}`); });
  return {
    types,
    edge: readVar(style, '--graph-edge'),
    edgeActive: readVar(style, '--graph-edge-active'),
    label: readVar(style, '--graph-label'),
    dim: readVar(style, '--graph-dim'),
  };
}

/** The palette, kept current across theme switches (Settings → Appearance flips `data-theme`). */
export function useGraphPalette(): GraphPalette {
  const [palette, setPalette] = useState<GraphPalette>(() => resolveGraphPalette());
  useEffect(() => {
    if (typeof MutationObserver === 'undefined' || typeof document === 'undefined') return undefined;
    const obs = new MutationObserver(() => setPalette(resolveGraphPalette()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
    return () => obs.disconnect();
  }, []);
  return palette;
}
