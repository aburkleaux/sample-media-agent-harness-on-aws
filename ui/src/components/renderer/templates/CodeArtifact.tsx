import React from 'react';
import type { RendererProps } from '../types';

interface CodeArtifactData {
  code: string;
  language: string;
  diff?: string;
  highlights?: { line: number; color: string }[];
}

export default function CodeArtifact({ data, theme: t }: RendererProps<CodeArtifactData>) {
  const lines = (data.diff || data.code).split('\n');
  const highlightMap = new Map((data.highlights || []).map(h => [h.line, h.color]));

  return (
    <div style={{ background: '#1e1e2e', borderRadius: t.radius.panel, overflow: 'hidden', border: `1px solid ${t.colors.border}` }}>
      <div style={{ padding: '8px 12px', background: 'rgba(0,0,0,0.3)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: t.typography.sizes.meta, color: '#a0a0b0' }}>{data.language}</span>
        {data.diff && <span style={{ fontSize: t.typography.sizes.meta, padding: '1px 6px', borderRadius: t.radius.badge, background: '#FF990030', color: t.colors.accent }}>diff</span>}
      </div>
      <div style={{ padding: '12px 0', overflowX: 'auto', maxHeight: 300, overflowY: 'auto' }}>
        {lines.map((line, idx) => {
          const lineNum = idx + 1;
          const hl = highlightMap.get(lineNum);
          let lineBg = 'transparent';
          let lineColor = '#e0e0e0';
          if (data.diff) {
            if (line.startsWith('+')) { lineBg = 'rgba(29,129,2,0.15)'; lineColor = '#7ee07e'; }
            else if (line.startsWith('-')) { lineBg = 'rgba(209,50,18,0.15)'; lineColor = '#ff8080'; }
          }
          if (hl) lineBg = `${hl}20`;
          return (
            <div key={idx} style={{ display: 'flex', padding: '0 12px', background: lineBg, lineHeight: '20px' }}>
              <span style={{ width: 32, textAlign: 'right', marginRight: 12, color: '#555', fontSize: t.typography.sizes.code, fontFamily: t.typography.fontMono, userSelect: 'none' }}>{lineNum}</span>
              <pre style={{ margin: 0, fontSize: t.typography.sizes.code, fontFamily: t.typography.fontMono, color: lineColor, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{line}</pre>
            </div>
          );
        })}
      </div>
    </div>
  );
}
