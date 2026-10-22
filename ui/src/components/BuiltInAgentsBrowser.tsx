import React, { useState } from 'react';
import { useLabContext } from '../LabContext';
import type { BuiltInAgentRow as BuiltInAgentInfo } from '../capability-api';
import { BrowserSkeleton, RefreshingNotice, staleListStyle } from './BrowserLoadingStates';
import Button from './ui/Button';
import InventoryRow, { InventoryRowList } from './ui/InventoryRow';
import { theme, tint } from '../styles/tokens';

/**
 * Filters built-in agents by case-insensitive substring match on label, key, or
 * purpose. Mirrors `filterBuiltInSkills` / `filterGatewayTools` for consistency.
 */
export function filterBuiltInAgents(agents: BuiltInAgentInfo[], query: string): BuiltInAgentInfo[] {
  if (!query.trim()) return agents;
  const lower = query.toLowerCase();
  return agents.filter(
    a => a.label.toLowerCase().includes(lower) ||
         a.key.toLowerCase().includes(lower) ||
         (a.purpose ?? '').toLowerCase().includes(lower),
  );
}

/**
 * BuiltInAgentsBrowser — read-only listing of the sub-agent runtimes that ship
 * with the harness (task / render / planning / design).
 *
 * Why this exists: the Agents tab previously listed ONLY user-authored
 * specialty-agent drafts, so the built-in sub-agents were invisible in the UI
 * even though they're deployed and actively used (e.g. the Render Agent runs on
 * every artifact-producing turn via the `render-artifacts` hook).
 *
 * Mirrors `BuiltInSkillsBrowser` / `GatewayBrowser`:
 *   - hides its own filter when the parent owns one (`externalSearch`)
 *   - loading / error / empty states with a refresh control
 *
 * `invokedVia` is shown per row because the harness has two distinct sub-agent
 * invocation paths (`dispatch_tool` vs `plan_and_execute_tool`) — listing them
 * together without that context would imply all four are dispatch targets.
 */
export default function BuiltInAgentsBrowser({ externalSearch }: { externalSearch?: string } = {}) {
  // Read from context, not the network. These four agents arrive on the same
  // Capability API page the Agents tab already fetches, so this panel issues NO
  // request of its own — it used to call the agent runtime's `mcp_list_agents` and
  // pay a cold start to be told what is essentially a constant.
  const { builtInAgents, refreshCategory, categoryErrors, loadingCategories } = useLabContext();
  const [filterInput, setFilterInput] = useState('');

  const usingExternalSearch = externalSearch !== undefined;

  const agents = builtInAgents ?? [];
  // `null` means the category page has not been read yet — distinct from an empty
  // list, so "loading" and "there are none" cannot be confused.
  const loaded = builtInAgents !== null;
  const loading = loadingCategories['specialty-agent'] ?? false;
  const error = categoryErrors['specialty-agent'] ?? null;

  // Refresh re-reads the category, which refills this panel as a side effect of the
  // same request that refreshes the list above it.
  const fetchAgents = () => { void refreshCategory('specialty-agent'); };

  const activeFilter = usingExternalSearch ? (externalSearch ?? '') : filterInput;
  const filtered = filterBuiltInAgents(agents, activeFilter);

  return (
    <div style={containerStyle} data-testid="builtin-agents">
      <div style={headerStyle}>
        <span style={titleStyle}>Built-in Agents</span>
        <Button
          variant="secondary"
          onClick={fetchAgents}
          busy={loading}
          busyLabel="⟳ Refreshing…"
          aria-label="Refresh built-in agents"
        >
          ↻ Refresh
        </Button>
      </div>

      {!usingExternalSearch && (
        <input
          type="text"
          placeholder="Filter agents..."
          value={filterInput}
          onChange={e => setFilterInput(e.target.value)}
          style={filterInputStyle}
          aria-label="Filter built-in agents"
        />
      )}

      {/* Same two loading states as the built-in Skills browser, from the same
          shared pieces. Flat list here, so no group-header bar. */}
      {!loaded && loading && <BrowserSkeleton label="Loading built-in agents" />}

      {loaded && loading && <RefreshingNotice label="Refreshing built-in agents…" />}

      {/* No dismiss control: this is the CATEGORY's error now, shared with the
          inventory above, and it clears when the category next loads cleanly
          (`LabContext` resets it on success). A local dismiss would hide a banner
          that is still true and would reappear on the next read. */}
      {error && <div style={errorStyle}>{error}</div>}

      {loaded && !error && agents.length === 0 && (
        <div style={emptyStateStyle}>No built-in agents are available.</div>
      )}

      {loaded && !error && agents.length > 0 && filtered.length === 0 && activeFilter && (
        <div style={emptyStateStyle}>No agents match your filter</div>
      )}

      {loaded && filtered.length > 0 && (
        <div style={loading ? staleListStyle : undefined} aria-busy={loading || undefined}>
          {/* The same InventoryRow card the Skills / Tools / Hooks inventories use, so
              the capability panels read as one list of the same kind of thing rather
              than this one alone in bordered cards. */}
          <InventoryRowList>
            {filtered.map(agent => (
              <InventoryRow
                key={agent.key}
                name={agent.label}
                badge={agent.configured
                  ? { label: 'configured', variant: 'success' as const }
                  : { label: 'not configured', variant: 'warning' as const }}
                description={agent.purpose}
                meta={`Invoked via: ${agent.invokedVia}${agent.runtimeId ? ` · ${agent.runtimeId}` : ''}`}
              />
            ))}
          </InventoryRowList>
        </div>
      )}
    </div>
  );
}

// --- Styles (kept in sync with BuiltInSkillsBrowser) ---

/*
 * The panel's left inset lives HERE, on the one box that contains everything, so the
 * title, the rows and the transient loading/error lines all start on the same edge —
 * matching BuiltInSkillsBrowser / GatewayBrowser exactly.
 */
const PANEL_INSET = 12;

const containerStyle: React.CSSProperties = {
  padding: `8px 0 8px ${PANEL_INSET}px`,
};

/* Title left, Refresh at the panel's right edge — pushed apart so the title names the
 * panel and the button lands over the row actions beneath it, the same right edge, as
 * in the other capability panels (this used to cluster left and diverge from them). */
const headerStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 8,
};

const titleStyle: React.CSSProperties = { fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary };

const filterInputStyle: React.CSSProperties = {
  width: '100%', padding: '6px 8px', fontSize: 'var(--fs-xs)', borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.borderStrong}`, background: theme.colors.surface,
  color: theme.colors.textPrimary, marginBottom: 8, boxSizing: 'border-box',
};

const errorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', color: theme.colors.error, background: tint(theme.colors.error), padding: '6px 8px',
  borderRadius: theme.radius.button, marginBottom: 8, display: 'flex', alignItems: 'center',
  justifyContent: 'space-between',
};

const emptyStateStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary, fontStyle: 'italic', padding: '8px 0',
};
