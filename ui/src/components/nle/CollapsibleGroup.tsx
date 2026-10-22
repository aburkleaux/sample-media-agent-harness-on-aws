/**
 * CollapsibleGroup — a labelled, collapsible section for the composite timeline.
 *
 * The composite view stacks many lanes (audio stems, per-speaker, per-person), so
 * a long video gets very tall. Grouping related lanes under a collapsible header
 * lets the user fold away what they're not looking at. Collapse state is per-group
 * and persisted (keyed by an optional `storageKey`) so it survives navigation.
 *
 * Under components/nle/** (lint:colors-exempt). Own useState, so hooks stay valid
 * even though the parent renders it after conditional returns.
 */
import React, { useState } from 'react';
import type { ThemeTokens } from '../renderer/types';
import ChevronDownIcon from '../icons/ChevronDownIcon';
import ChevronRightIcon from '../icons/ChevronRightIcon';

interface CollapsibleGroupProps {
  title: string;
  count?: number;
  theme: ThemeTokens;
  children: React.ReactNode;
  defaultCollapsed?: boolean;
  /** localStorage key suffix to persist collapse per artifact+group. */
  storageKey?: string;
  testId?: string;
}

export default function CollapsibleGroup({ title, count, theme: t, children, defaultCollapsed = false, storageKey, testId }: CollapsibleGroupProps) {
  const lsKey = storageKey ? `nle-group-collapsed-${storageKey}` : '';
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (!lsKey) return defaultCollapsed;
    try { const v = localStorage.getItem(lsKey); return v === null ? defaultCollapsed : v === '1'; } catch { return defaultCollapsed; }
  });
  const toggle = () => {
    setCollapsed(c => {
      const next = !c;
      if (lsKey) { try { localStorage.setItem(lsKey, next ? '1' : '0'); } catch { /* quota */ } }
      return next;
    });
  };
  return (
    <div data-testid={testId} style={{ borderTop: `1px solid ${t.colors.border}` }}>
      <button
        data-testid={testId ? `${testId}-header` : undefined}
        onClick={toggle}
        aria-expanded={!collapsed}
        style={{
          display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
          padding: '5px 12px', background: t.colors.surface, border: 'none', cursor: 'pointer',
          color: t.colors.textTertiary, fontSize: t.typography.sizes.badge, letterSpacing: 1,
          textTransform: 'uppercase', fontFamily: 'inherit',
        }}
      >
        <span data-testid={testId ? `${testId}-chevron` : undefined} style={{ color: t.colors.textTertiary, width: 12, height: 12, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
          {collapsed ? <ChevronRightIcon size={12} /> : <ChevronDownIcon size={12} />}
        </span>
        <span style={{ color: t.colors.textSecondary }}>{title}</span>
        {typeof count === 'number' && <span style={{ color: t.colors.textTertiary }}>· {count}</span>}
      </button>
      {!collapsed && <div data-testid={testId ? `${testId}-body` : undefined}>{children}</div>}
    </div>
  );
}
