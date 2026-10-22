import React from 'react';
import type { RendererProps } from '../types';

interface ChartData {
  type: 'bar' | 'line' | 'pie';
  data: { label: string; value: number; color?: string }[];
  axes?: { x?: string; y?: string };
  legend?: boolean;
}

export default function ChartInteractive({ data, theme: t, onInteraction }: RendererProps<ChartData>) {
  const maxValue = Math.max(...data.data.map(d => d.value), 1);
  const colors = [t.colors.primary, t.colors.accent, '#1D8102', '#D13212', '#5b6abf', '#8B5CF6'];

  if (data.type === 'bar') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 8 }}>
        {data.axes?.y && <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textSecondary }}>{data.axes.y}</span>}
        {data.data.map((item, idx) => (
          <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}
            onClick={() => onInteraction?.({ type: 'click', target: `bar-${idx}`, data: item, timestamp: Date.now() })}>
            <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textSecondary, minWidth: 60, textAlign: 'right' }}>{item.label}</span>
            <div style={{ flex: 1, height: 20, background: t.colors.border, borderRadius: 4, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${(item.value / maxValue) * 100}%`, background: item.color || colors[idx % colors.length], borderRadius: 4, transition: 'width 0.3s ease' }} />
            </div>
            <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textPrimary, minWidth: 40 }}>{item.value}</span>
          </div>
        ))}
        {data.axes?.x && <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textSecondary, textAlign: 'center' }}>{data.axes.x}</span>}
      </div>
    );
  }

  if (data.type === 'pie') {
    const total = data.data.reduce((sum, d) => sum + d.value, 0);
    let cumulative = 0;
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: 8 }}>
        <svg viewBox="0 0 100 100" style={{ width: 120, height: 120 }}>
          {data.data.map((item, idx) => {
            const pct = (item.value / total) * 100;
            const startAngle = (cumulative / total) * 360;
            cumulative += item.value;
            const endAngle = (cumulative / total) * 360;
            const startRad = (startAngle - 90) * Math.PI / 180;
            const endRad = (endAngle - 90) * Math.PI / 180;
            const large = pct > 50 ? 1 : 0;
            const x1 = 50 + 40 * Math.cos(startRad), y1 = 50 + 40 * Math.sin(startRad);
            const x2 = 50 + 40 * Math.cos(endRad), y2 = 50 + 40 * Math.sin(endRad);
            return <path key={idx} d={`M50,50 L${x1},${y1} A40,40 0 ${large},1 ${x2},${y2} Z`} fill={item.color || colors[idx % colors.length]} />;
          })}
        </svg>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {data.data.map((item, idx) => (
            <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 8, height: 8, borderRadius: 2, background: item.color || colors[idx % colors.length] }} />
              <span style={{ fontSize: t.typography.sizes.meta, color: t.colors.textPrimary }}>{item.label} ({Math.round((item.value / total) * 100)}%)</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // Line chart — simple SVG polyline
  const points = data.data.map((d, i) => ({ x: (i / Math.max(data.data.length - 1, 1)) * 280 + 10, y: 100 - (d.value / maxValue) * 80 }));
  return (
    <div style={{ padding: 8 }}>
      <svg viewBox="0 0 300 120" style={{ width: '100%', height: 120 }}>
        <polyline points={points.map(p => `${p.x},${p.y}`).join(' ')} fill="none" stroke={t.colors.primary} strokeWidth="2" />
        {points.map((p, idx) => (
          <circle key={idx} cx={p.x} cy={p.y} r="4" fill={t.colors.primary} style={{ cursor: 'pointer' }}
            onClick={() => onInteraction?.({ type: 'click', target: `point-${idx}`, data: data.data[idx], timestamp: Date.now() })} />
        ))}
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0 10px' }}>
        {data.data.map((d, idx) => (
          <span key={idx} style={{ fontSize: 9, color: t.colors.textSecondary }}>{d.label}</span>
        ))}
      </div>
    </div>
  );
}
