import React, { useState, useEffect, useRef } from 'react';
import { listSkills, peekSkills, type SkillInfo } from '../api';
import { useLabContext } from '../LabContext';
import { BrowserSkeleton, RefreshingNotice } from './BrowserLoadingStates';
import Button from './ui/Button';
import InventoryRow, { InventoryRowList } from './ui/InventoryRow';
import { theme, tint } from '../styles/tokens';

/**
 * Filters a list of SkillInfo by case-insensitive substring match on name
 * or description. Returns all skills if query is empty. Mirrors
 * `GatewayBrowser`'s filter helper for consistency.
 */
export function filterBuiltInSkills(skills: SkillInfo[], query: string): SkillInfo[] {
  if (!query.trim()) return skills;
  const lower = query.toLowerCase();
  return skills.filter(
    s => s.name.toLowerCase().includes(lower) ||
         (s.description ?? '').toLowerCase().includes(lower),
  );
}

/**
 * BuiltInSkillsBrowser — Read-only listing of skills that ship with the
 * runtime (anything `SkillsManager.list_skills()` returns: bundled
 * filesystem skills plus any S3-loaded ones).
 *
 * Follows `GatewayBrowser` where the two "browse what's already provided" panels
 * genuinely behave alike — it hides its own filter when the parent already owns
 * one (`externalSearch`), and shows a loading state on first fetch.
 *
 * Rows are `InventoryRow` — the SAME card the Yours (drafts) and Published sections
 * use, so the three tiers of the Skills surface read as one list rather than a table
 * stacked on top of two card lists.
 *
 * It was a 3-column table before that, and the two reasons for the table are both
 * preserved by the card:
 *
 *   - **No source grouping.** Grouping by `local`/`s3`/`memory` put a collapsible
 *     "Bundled (filesystem)" heading above every row, because in practice every
 *     skill is bundled — a heading made out of a constant, wrapping a control whose
 *     only power was to hide the whole list. A non-bundled skill carries a badge on
 *     its own row instead, so the distinction survives without the ceremony.
 *   - **Descriptions are the substance.** They are 200–700 characters of
 *     model-facing prose whose job is to say WHEN the skill fires, so they must not
 *     be cut to one line. In the card they take the full row width and clamp at 3
 *     lines with a `more` toggle (`ClampedText`), which is strictly more of the
 *     sentence than the table's name+action columns left room for.
 */
export default function BuiltInSkillsBrowser({ externalSearch, onOpenSkill }: { externalSearch?: string; onOpenSkill?: (name: string) => void } = {}) {
  // Two sources, deliberately, because they answer different questions.
  //
  // The CATALOG (name, description) comes from the Capability API page the Skills tab
  // already fetches, so the list paints in ~200ms without waking the agent runtime.
  // LIVE STATE (`activated`, `source`, `dirty`) describes which skills are loaded
  // into the current agent session, which only the runtime knows — so it is fetched
  // separately and merged when it arrives, never blocking the paint.
  //
  // A row with no live state yet renders with NO badge rather than an "inactive" one:
  // "we have not heard back" and "not activated" are different claims, and only one
  // of them is true at that moment.
  const { builtInSkills } = useLabContext();
  const cachedSkills = useRef(peekSkills()).current;
  const [liveSkills, setLiveSkills] = useState<SkillInfo[] | null>(cachedSkills ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const skills: SkillInfo[] = React.useMemo(() => {
    // A UNION, keyed by name — not the catalog alone.
    //
    // Each side knows something the other cannot. The catalog is what SHIPS, so a
    // skill in it must be listed even when the runtime has not reported it (a lagging
    // or never-started runtime must not empty the panel). The runtime knows what has
    // been LOADED, and `list_skills` returns more than the shipped set: a skill pulled
    // from S3 or authored in memory exists only there, and the panel badges it as such
    // (`sourceLabelFor`). Iterating the catalog alone dropped exactly those rows.
    const live = liveSkills ?? [];
    const liveByName = new Map(live.map(s => [s.name, s]));
    const rows: SkillInfo[] = [];
    const seen = new Set<string>();

    // Catalog first, so ordering is deploy-determined and stable across reloads
    // rather than following whatever order the runtime happened to report.
    for (const entry of builtInSkills ?? []) {
      seen.add(entry.name);
      const liveRow = liveByName.get(entry.name);
      rows.push({
        // Live state (activated / source / tools) rides along where we have it...
        ...(liveRow ?? {}),
        name: entry.name,
        // ...but the catalog's description wins: it comes from the shipped SKILL.md,
        // so it cannot be staler than the runtime's copy.
        description: entry.description || liveRow?.description || '',
      });
    }

    // Then anything the runtime knows that the catalog does not — the S3 / in-memory
    // skills. Appended rather than interleaved so the shipped set stays together.
    for (const liveRow of live) {
      if (!seen.has(liveRow.name)) rows.push(liveRow);
    }
    return rows;
  }, [builtInSkills, liveSkills]);

  const loaded = builtInSkills !== null || liveSkills !== null;
  const [filterInput, setFilterInput] = useState('');
  const [debouncedFilter, setDebouncedFilter] = useState('');

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const usingExternalSearch = externalSearch !== undefined;

  useEffect(() => {
    fetchSkills();
  }, []);

  useEffect(() => {
    if (usingExternalSearch) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedFilter(filterInput), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [filterInput, usingExternalSearch]);

  const activeFilter = usingExternalSearch ? (externalSearch ?? '') : debouncedFilter;

  /**
   * Fetch live session state and merge it in. Never gates the list.
   *
   * A failure here is not a failure of the panel: the catalog has already painted,
   * so the badges are simply missing. It is surfaced only when there is nothing else
   * to show, which is the pre-cutover situation (no catalog, runtime unreachable).
   */
  const fetchSkills = async (forceRefresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const result = await listSkills(forceRefresh);
      setLiveSkills(Array.isArray(result) ? result : []);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load built-in skills';
      // Only an error if the catalog did not already answer.
      setError(builtInSkills === null ? message : null);
    } finally {
      setLoading(false);
    }
  };

  const filtered = filterBuiltInSkills(skills, activeFilter);

  if (loaded && !loading && !error && skills.length === 0) {
    return (
      <div style={containerStyle}>
        <div style={headerStyle}>
          <span style={titleStyle}>Built-in Skills</span>
          <Button
            variant="secondary"
            onClick={() => fetchSkills(true)}
            busy={loading}
            busyLabel="⟳ Refreshing…"
            aria-label="Refresh built-in skills"
          >
            ↻ Refresh
          </Button>
        </div>
        <div style={emptyStateStyle}>No built-in skills are available.</div>
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      <div style={headerStyle}>
        <span style={titleStyle}>Built-in Skills</span>
        <Button
          variant="secondary"
          onClick={() => fetchSkills(true)}
          busy={loading}
          busyLabel="⟳ Refreshing…"
          aria-label="Refresh built-in skills"
        >
          ↻ Refresh
        </Button>
      </div>

      {!usingExternalSearch && (
        <input
          type="text"
          placeholder="Filter skills..."
          value={filterInput}
          onChange={e => setFilterInput(e.target.value)}
          style={filterInputStyle}
          aria-label="Filter built-in skills"
        />
      )}

      {/* No `withGroupHeader`: the list is a flat table now, so a placeholder
          heading would stand in for a row that never arrives. See
          BrowserLoadingStates for why both states exist. */}
      {!loaded && loading && (
        <BrowserSkeleton label="Loading built-in skills" />
      )}

      {/* "status", not "refreshing": the ROWS are not being refetched. They came from
          the control-plane catalog and are already final. What is outstanding is the
          per-row status the runtime alone knows — `active`, and the source label for a
          skill that is not bundled. Saying "Refreshing" while dimming a complete list
          made a ~200ms panel feel like a page reload, because that call can take ~9s on
          a cold runtime. */}
      {loaded && loading && <RefreshingNotice label="Getting latest skill status…" />}

      {error && (
        <div style={errorStyle}>
          {error}
          <button onClick={() => setError(null)} style={dismissBtnStyle}>✕</button>
        </div>
      )}

      {loaded && !error && filtered.length === 0 && activeFilter && (
        <div style={noMatchStyle}>No skills match your filter</div>
      )}

      {/* The list below is NOT dimmed while loading. `staleListStyle` (opacity 0.55)
          dates from when the runtime owned this list and a refetch really did replace
          the rows. It no longer does: the rows are catalog-final and only the badges are
          pending, so fading them understates what is on screen — and the status call
          takes ~9s on a cold runtime, which made a ~60ms panel look like a page reload.
          `aria-busy` stays: it tells assistive tech an update is in flight without
          misrepresenting the content. */}
      {loaded && filtered.length > 0 && (
        <div aria-busy={loading || undefined}>
          {/* The same card row the Yours/Published sections use, so all three tiers of
              the Skills surface read as one list of the same kind of thing. It replaced
              a 3-column table: the description is the substance here, and a card gives
              it the full row width instead of whatever a fixed name column and an
              action column leave over. */}
          <InventoryRowList>
            {filtered.map(skill => (
              <InventoryRow
                key={skill.name}
                name={skill.name}
                /* These run 200–700 characters of model-facing prose, so 3 lines with a
                   `more` toggle bounds the row height while keeping the "Use when…"
                   clause reachable. The full text is always in the DOM. */
                description={skill.description}
                /* The source label used to be a group header. Shown only when it is NOT
                   the ordinary bundled case: every skill being "Bundled (filesystem)"
                   made a heading out of a constant, but an S3- or memory-sourced skill
                   genuinely differs and dropping the grouping must not hide that.
                   `activated` takes the badge when both apply — it is the state that
                   changes what the runtime does. */
                badge={
                  skill.activated
                    ? { label: 'active', variant: 'success' as const }
                    : skill.source !== 'local'
                      ? { label: sourceLabelFor(skill.source), variant: 'warning' as const }
                      : undefined
                }
                meta={metaFor(skill)}
                actions={
                  onOpenSkill && (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => onOpenSkill(skill.name)}
                      aria-label={`View ${skill.name}`}
                    >
                      View
                    </Button>
                  )
                }
              />
            ))}
          </InventoryRowList>
        </div>
      )}
    </div>
  );
}

/**
 * Third line of the row: the tools a skill declares, plus its source when that is
 * not the ordinary bundled case AND the badge is already spoken for by `active`.
 *
 * Returns `undefined`, not `''`, when there is nothing to say — `InventoryRow`
 * omits the line entirely for `undefined`, where an empty string would still take
 * its margin on every row that has no tools.
 */
function metaFor(skill: SkillInfo): React.ReactNode | undefined {
  const parts: string[] = [];
  if (skill.activated && skill.source !== 'local') parts.push(sourceLabelFor(skill.source));
  if (skill.tools && skill.tools.length > 0) {
    const shown = skill.tools.slice(0, 4).join(', ');
    parts.push(`Uses: ${shown}${skill.tools.length > 4 ? ` +${skill.tools.length - 4} more` : ''}`);
  }
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

function sourceLabelFor(source: SkillInfo['source']): string {
  switch (source) {
    case 'local': return 'Bundled (filesystem)';
    case 's3': return 'Loaded from S3';
    case 'memory': return 'In-memory (unsaved)';
    default: return 'Other';
  }
}

// --- Styles (kept in sync with GatewayBrowser for visual consistency) ---

/*
 * The panel's left inset lives HERE, on the one box that contains everything, so the
 * "Built-in Skills" title, the rows and the transient loading/error lines all start
 * on the same edge.
 *
 * It used to sit on the row's own name cells instead, which left the title — and
 * every message the panel can show — hard against the bordered edge, an inset away
 * from the content below it.
 */
const PANEL_INSET = 12;

const containerStyle: React.CSSProperties = {
  padding: `8px 0 8px ${PANEL_INSET}px`,
};

/*
 * Title left, Refresh at the panel's right edge.
 *
 * These used to cluster together on the left, on the argument that Refresh acts on the
 * list below it and so reads better beside its title. In practice the button sat
 * against the label and read as part of it. Pushed apart, the title names the panel
 * and the button lands over the row actions beneath it — the same right edge.
 * `BuiltInAgentsBrowser` now uses the same split, so the two panels match here.
 */
const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 10,
  marginBottom: 8,
};

const titleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};


const filterInputStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 8px',
  fontSize: 'var(--fs-xs)',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.borderStrong}`,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  marginBottom: 8,
  boxSizing: 'border-box',
};


const errorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
  background: tint(theme.colors.error),
  padding: '6px 8px',
  borderRadius: theme.radius.button,
  marginBottom: 8,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
};

const dismissBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  color: theme.colors.error,
  padding: '0 4px',
};

const emptyStateStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '8px 0',
};

const noMatchStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '8px 0',
};
