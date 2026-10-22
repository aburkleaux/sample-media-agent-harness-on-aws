// CapabilitiesSettings.tsx — Per-type management of the agent's capability
// surface (Skills / Tools / Hooks) plus a read-only view of the built-in
// and gateway-provided equivalents the agent can also reach.
//
// Agents is NOT here. It duplicated the workspace's own Agents view, which now
// owns that surface outright — including the delete action and built-in agents
// browser that used to live only in this file. See `CustomAgentCenterList` in
// views/WorkspaceView.tsx.
//
// Restructure (per the "drop Connected" review):
//
//   - Peer tabs per type. No more "Connected" union-view tab — it duplicated
//     the per-type tabs and confused the "where do I add a thing of type X"
//     path.
//   - Test Harness is no longer a tab. It's a top-right action button on
//     the page header that toggles an inline harness panel; users return
//     by clicking "Done" or the X on the panel.
//   - Each per-type tab body has two segments:
//       * "Yours" — your draftable, version-controlled assets. Editable.
//       * "Built-in & gateway" — runtime-provided capabilities of the same
//         type. Read-only; managed in Settings → Gateways for gateway
//         entries, and built into the runtime for the others.
//     The second segment is only rendered when there's at least one item.
//   - Gateway-managed entries link out to Settings → Gateways with a hint
//     line so users have a clear next step rather than a dead end.

import React, { useState } from 'react';
import AddMcpServerModal from '../AddMcpServerModal';
import { deleteMcpServer, listMcpServers, type McpRegistration } from '../../api';
import { useIsGatewayAdmin } from '../../auth/isGatewayAdmin';
import { panelStaticHeaderStyle, panelCountBadgeStyle, InventorySectionHeader } from './panelChrome';
import { theme } from '../../styles/tokens';
import { useLabContext } from '../../LabContext';
import type { DraftCategory, SkillDraft, ToolCollectionDraft, ToolRef, PublishedAssetSummary } from '../../lab-api';
import { deleteSkillDraft, deleteToolCollectionDraft } from '../../lab-api';
import { collapseToLineages } from '../../utils/publishedLineages';
import GatewayBrowser from '../GatewayBrowser';
import HooksPanel from './HooksPanel';
import TestHarnessPanel from '../TestHarnessPanel';
import { InventoryStateFilter, type InventoryState } from '../InventoryStateFilter';
import Button from '../ui/Button';
import Badge from '../ui/Badge';
import EmptyState from '../ui/EmptyState';
import InventoryRow, { InventoryRowList } from '../ui/InventoryRow';
import CapabilitySwitch from './CapabilitySwitch';

// Agents is deliberately NOT a tab here. It duplicated the workspace's own
// Agents view (main nav), which now owns that surface entirely — including the
// delete action and built-in agents browser that used to live only here. See
// `CustomAgentCenterList` in views/WorkspaceView.tsx.
export type CapTab = 'tools' | 'hooks';

const TABS: { key: CapTab; label: string }[] = [
  { key: 'tools', label: 'MCP Tools' },
  { key: 'hooks', label: 'Hooks' },
];

interface CapabilitiesSettingsProps {
  /** Opens the Add MCP server form. Omitted where the host cannot host it. */
  onAddMcpServer?: () => void;
  /**
   * Optional handler so the host can route the "Manage gateways" hint
   * to its existing Settings → Gateways navigation. When omitted, the
   * hint is still shown but renders as plain text rather than a link.
   */
  onOpenGatewaysTab?: () => void;
  /** Opens Create/Edit within the host; defaults to the workspace editor. */
  onOpenEditor?: (target: { category: DraftCategory; assetId: string | null }) => void;
  /** Controlled active sub-tab (settings modal nav drives it). */
  tab?: CapTab;
  /** Hide the internal Skills/Agents/Tools/Hooks tab bar (nav drives it). */
  hideTabBar?: boolean;
}

export default function CapabilitiesSettings({ onOpenGatewaysTab, onOpenEditor, onAddMcpServer, tab, hideTabBar }: CapabilitiesSettingsProps = {}) {
  const {
    skills, toolCollections,
    loadingCategories, loadedCategories, categoryErrors,
    ensureCategoryLoaded, refreshCategory, setEditingTarget,
    optimisticRemoveSkill, optimisticRemoveSpecialtyAgent,
    optimisticRemoveToolCollection,
    setHarnessTarget,
  } = useLabContext();

  const [activeTab, setActiveTab] = useState<CapTab>('tools');
  const [search, setSearch] = useState('');
  const [showGateway, setShowGateway] = useState(false);
  const [harnessOpen, setHarnessOpen] = useState(false);
  const [gatewayToolCount, setGatewayToolCount] = useState(0);
  const [hookCount, setHookCount] = useState(0);

  // When the settings modal nav drives the sub-tab, follow it.
  React.useEffect(() => {
    if (!tab) return;
    setActiveTab(tab);
  }, [tab]);

  // The production settings modal hides this tab bar and renders one active
  // child browser, so fetching four background counts would duplicate its
  // request. The legacy internal-tab mode still gets counts through the same
  // shared caches used by the children.
  React.useEffect(() => {
    if (hideTabBar) return;
    import('../../lab-api').then(({ listGatewayTools, listHooks }) => {
      listGatewayTools().then(tools => setGatewayToolCount(tools.length)).catch(() => {});
      listHooks().then(res => setHookCount((res.builtinHooks?.length ?? 0) + (res.userHooks?.length ?? 0))).catch(() => {});
    });
  }, [hideTabBar]);

  const category = tabToCategory(activeTab);
  const openEditor = onOpenEditor ?? setEditingTarget;
  const initialCategoryLoad = loadingCategories[category] && !loadedCategories[category];
  const refreshingCategory = loadingCategories[category] && !!loadedCategories[category];
  React.useEffect(() => {
    if (category) void ensureCategoryLoaded(category);
  }, [category, ensureCategoryLoaded]);

  // The Tools tab can create three different KINDS of thing, so + Create opens a
  // menu rather than assuming one. Labels name the scope because the difference
  // between "private to me" and "visible to everyone" is the one a user must not
  // misread.
  const [addMcpOpen, setAddMcpOpen] = React.useState(false);
  // Registered servers shown optimistically. The gateway can take minutes to
  // synchronise a target, so a row appears immediately in `synchronizing` and
  // polls rather than the modal blocking or claiming success it cannot see yet.
  const [mcpServers, setMcpServers] = React.useState<McpRegistration[]>([]);

  const [createMenuOpen, setCreateMenuOpen] = React.useState(false);
  const isAdmin = useIsGatewayAdmin();

  // Load registered servers when the Tools tab is first shown. Without this the
  // list is component-local and empties on reload, even though the rows exist.
  const [mcpLoaded, setMcpLoaded] = React.useState(false);
  // Surfaces a failed removal. Needed because remove is now optimistic: without
  // a visible message the row would silently reappear with no explanation.
  const [mcpError, setMcpError] = React.useState<string | null>(null);
  // Surfaces a failed/degraded LISTING, kept separate from the removal error so
  // one doesn't clobber the other. Fail-closed (`actor_unresolved`) and
  // degraded (`gateway_unavailable`) results land here.
  const [mcpListError, setMcpListError] = React.useState<string | null>(null);
  // Gateways the runtime could not enumerate (e.g. cross-account, invisible to
  // its credentials). Shown rather than dropped: the whole failure this panel had
  // was a short list that looked complete, so an incomplete one must say so.
  const [mcpUnreadable, setMcpUnreadable] = React.useState<string[]>([]);
  // A spinner while the first (instant) phase resolves. The list itself is now
  // a proxy control-plane read, not an agent-runtime cold start -- see the two
  // listMcpServers calls below.
  const [mcpLoading, setMcpLoading] = React.useState(false);
  React.useEffect(() => {
    if (activeTab !== 'tools' || mcpLoaded) return;
    setMcpLoaded(true);
    setMcpLoading(true);
    let cancelled = false;
    // TWO PHASES, both control-plane. Neither wakes the agent runtime.
    //
    // Phase 1: the caller's own registered servers, one DynamoDB Query in the
    // proxy. Phase 2 (`live`) merges the platform targets and live status from the
    // control plane. Previously this was a single `user_mcp_list` invoke of the
    // agent runtime, which sends no session id and so cold-started on EVERY call —
    // measured at 10.9s, 11.6s and 12.7s on three separate opens, blank the whole
    // time. It is the same read either way: ownership rows plus ListGatewayTargets.
    //
    // Phase 1's `unreadable` names the platform gateway as PENDING rather than
    // reporting nothing, so the group renders as loading instead of vanishing for
    // the second before phase 2 lands.
    const load = async () => {
      try {
        const fast = await listMcpServers();
        if (!cancelled) {
          setMcpServers(fast.servers);
          setMcpUnreadable(fast.unreadable);
          setMcpListError(fast.error ? mcpListErrorMessage(fast.error) : null);
        }
        // Fail-closed results are terminal — the live pass would fail identically
        // and would only overwrite the reason with a second copy of itself.
        if (fast.error === 'actor_unresolved') return;
        const live = await listMcpServers({ live: true });
        if (!cancelled) {
          setMcpServers(live.servers);
          setMcpUnreadable(live.unreadable);
          setMcpListError(live.error ? mcpListErrorMessage(live.error) : null);
        }
      } catch (err) {
        // Phase 2 failing leaves phase 1's rows on screen: a short list with a
        // reason beats an empty one. Only report when there is nothing to show.
        if (!cancelled) {
          setMcpListError(err instanceof Error ? err.message : 'Could not list MCP servers');
        }
      } finally {
        // Unconditional: `cancelled` gates DATA writes, not the spinner. Guarding
        // this is what previously left the panel loading forever, because setting
        // `mcpLoaded` re-runs the effect and cancels the only pass that fetched.
        setMcpLoading(false);
      }
    };
    void load();
  }, [activeTab, mcpLoaded]);

  // Poll only rows that are still settling, through the CONTROL PLANE.
  //
  // This used to call `mcpServerStatus` (the runtime's `user_mcp_status`) once per
  // pending row every 5s, and since that action sends no session id each tick was
  // its own cold start — the most expensive thing on the tab, for a status pill.
  // The live listing already carries live status for every row, so one call
  // replaces N and costs a DynamoDB Query plus one ListGatewayTargets.
  const pendingIds = mcpServers.filter(m => m.status === 'synchronizing').map(m => m.target_id);
  React.useEffect(() => {
    if (pendingIds.length === 0) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const { servers } = await listMcpServers({ live: true });
        if (cancelled || servers.length === 0) return;
        const statusByTarget = new Map(servers.map(s => [s.target_name, s.status]));
        setMcpServers(list => list.map(m => {
          const next = statusByTarget.get(m.target_name);
          // Only ever move a row OFF synchronizing. A row the live read omits
          // (mid-registration) keeps its current state rather than being stranded.
          return next && next !== 'synchronizing' ? { ...m, status: next } : m;
        }));
      } catch {
        // Leave rows pending: a failed poll is not a failed target, and marking
        // one failed here would strand a server that is actually fine.
      }
    }, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [pendingIds.join(',')]);

  const removeMcpServer = React.useCallback(async (record: McpRegistration) => {
    // Drop the row FIRST. deleteMcpServer round-trips through the agent runtime,
    // which on a cold container is seconds -- and the row used to stay put for
    // all of it, so the click read as dead and invited a second click. Add
    // already works this way (optimistic insert, then poll); remove was the
    // outlier.
    //
    // Restoring on failure keeps the original guarantee: a server that still
    // exists must not vanish from the list. The row comes back in place rather
    // than at the end, because reordering on error is its own small confusion.
    setMcpError(null);
    let restored = false;
    setMcpServers(list => {
      const at = list.findIndex(m => m.target_name === record.target_name);
      if (at < 0) return list;
      restored = true;
      return [...list.slice(0, at), ...list.slice(at + 1)];
    });
    const reinsert = (at: number) => setMcpServers(list =>
      list.some(m => m.target_name === record.target_name)
        ? list
        : [...list.slice(0, at), record, ...list.slice(at)]);
    const index = mcpServers.findIndex(m => m.target_name === record.target_name);
    try {
      await deleteMcpServer(record.target_name, record.target_id);
    } catch (err) {
      if (restored) reinsert(index < 0 ? mcpServers.length : index);
      setMcpError(
        `Couldn't remove ${record.server_name}: ${err instanceof Error ? err.message : 'unknown error'}`
      );
    }
  }, [mcpServers]);

  const tabLabel = TABS.find(t => t.key === activeTab)?.label ?? 'Capabilities';
  const tabDescription: Record<CapTab, string> = {
    tools: 'Tool collections available to the agent.',
    hooks: 'Event-driven automation (data, not code).',
  };

  if (harnessOpen) {
    return (
      <div role="region" aria-label="Test Harness view" style={nestedHarnessStyle}>
        {/* Back comes from the panel now, beside Run. The label still names the tab:
            inside this modal, "← Back" alone would not say where it lands. */}
        <TestHarnessPanel
          onClose={() => setHarnessOpen(false)}
          backLabel={`← Back to ${tabLabel}`}
        />
      </div>
    );
  }

  // Split by origin so each group can be NAMED. The platform server ships with
  // the harness and belongs to nobody; the rest are the caller's own
  // registrations. `platform === true` is set by the runtime, so an older runtime
  // (which sent no flag) lands everything under "Your MCP servers" — a wrong
  // grouping is recoverable, whereas defaulting the other way would label a
  // user's own server as built-in AND hide its Remove button.
  const platformMcpServers = mcpServers.filter(s => s.platform === true);
  const ownMcpServers = mcpServers.filter(s => s.platform !== true);

  /*
   * MCP server rows, on the same `InventoryRow` card as everything else in this modal.
   *
   * They were the last hairline idiom left on the Tools tab, and on an account with no
   * tool collections they ARE the tab — so the one thing a user saw here looked nothing
   * like the rows on Skills, Agents or Hooks.
   *
   * The connection state moves from a hand-rolled pill into the row's badge, which is
   * where every other surface puts state; the endpoint takes the description slot,
   * being what the row is about.
   */
  const renderMcpRows = (servers: McpRegistration[]) => (
    <InventoryRowList>
      {servers.map(server => (
        // Keyed by gateway + target: a target name is unique per gateway, not across
        // them, and duplicate React keys leave stale rows mounted.
        <InventoryRow
          key={`${server.gateway_id ?? 'own'}/${server.target_name}`}
          name={server.server_name}
          description={server.endpoint}
          /* One line, not three: an endpoint is a URL, and wrapping it to a paragraph
             made the row taller than the information warranted. */
          descriptionLines={1}
          badge={
            server.status === 'ready' ? { label: 'Connected', variant: 'success' }
            : server.status === 'failed' ? { label: "Couldn't connect", variant: 'error' }
            : { label: 'Setting up…', variant: 'neutral' }
          }
          actions={
            /* Removal must work in every state, including a half-registered one — the
               ownership row is written before the target exists so a broken
               registration is still visible and removable.
               But ONLY for the caller's own registrations. A platform target belongs to
               the harness, and `deregister_user_mcp_server` re-checks ownership against
               a recorded row it does not have, so the button could only ever fail:
               absent rather than disabled, because there is no state in which this
               caller may press it. `!== false` so a UI newer than the runtime keeps the
               old behaviour instead of hiding every Remove button. */
            server.removable !== false ? (
              <Button
                variant="danger"
                onClick={() => void removeMcpServer(server)}
                aria-label={`Remove ${server.server_name}`}
              >
                Remove
              </Button>
            ) : undefined
          }
        />
      ))}
    </InventoryRowList>
  );

  return (
    <div>
      {/* Header — compact modal-style matching the design */}
      <div style={{ marginBottom: hideTabBar ? 16 : 4 }}>
        <h2 style={{ margin: '0 0 4px', fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary }}>
          {hideTabBar ? tabLabel : 'Capabilities'}
        </h2>
        <p style={{ margin: '0 0 16px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary }}>
          {hideTabBar ? tabDescription[activeTab] : 'Manage your skills, agents, and tools. Built-in and gateway-provided capabilities are listed read-only below each section.'}
        </p>
      </div>

      {/* Tabs */}
      {!hideTabBar && (
      <div style={{ display: 'flex', gap: 0, borderBottom: `1px solid ${theme.colors.border}`, margin: '20px 0' }}>
        {TABS.map(tab => {
          const yoursCount = countYours(tab.key, skills, toolCollections);
          const extraCount = tab.key === 'tools' ? gatewayToolCount : tab.key === 'hooks' ? hookCount : 0;
          return (
            <button
              key={tab.key}
              onClick={() => { setActiveTab(tab.key); setSearch(''); }}
              style={{
                padding: '8px 16px', fontSize: 'var(--fs-xs)', border: 'none', background: 'none', cursor: 'pointer',
                color: activeTab === tab.key ? theme.colors.primary : theme.colors.textSecondary,
                borderBottom: activeTab === tab.key ? `2px solid ${theme.colors.primary}` : '2px solid transparent',
                fontWeight: activeTab === tab.key ? theme.typography.weights.medium : theme.typography.weights.regular,
              }}
            >
              {tab.label}
              <span style={{ marginLeft: 6, fontSize: 'var(--fs-3xs)', color: theme.colors.textSecondary }}>
                {yoursCount + extraCount}
              </span>
            </button>
          );
        })}
      </div>
      )}

      {/* Hooks tab — self-contained management panel */}
      {activeTab === 'hooks' && <HooksPanel />}

      {/* Search + Actions — compact row for non-hooks tabs */}
      {activeTab !== 'hooks' && (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder={`Search ${activeTab}...`}
          style={searchInputStyle}
        />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
          {activeTab === 'tools' && (
            <CapabilitySwitch checked={showGateway} onChange={setShowGateway} label="Built-in tools" />
          )}
          {activeTab === 'tools' ? (
            <div style={{ position: 'relative' }}>
              <button
                onClick={() => setCreateMenuOpen(o => !o)}
                style={primaryBtnStyle}
                aria-haspopup="menu"
                aria-expanded={createMenuOpen}
              >
                + Create
              </button>
              {createMenuOpen && (
                <>
                  {/* Click-away layer: a menu that only closes on re-click traps
                      the user behind it. */}
                  <div
                    onClick={() => setCreateMenuOpen(false)}
                    style={createMenuScrimStyle}
                    aria-hidden="true"
                  />
                  <div role="menu" style={createMenuStyle}>
                    <button
                      role="menuitem"
                      style={createMenuItemStyle}
                      onClick={() => { setCreateMenuOpen(false); (onAddMcpServer ?? (() => setAddMcpOpen(true)))(); }}
                    >
                      <span style={createMenuTitleStyle}>MCP server</span>
                      <span style={createMenuHintStyle}>
                        Connect your own server. Only you can use its tools.
                      </span>
                    </button>
                    <button
                      role="menuitem"
                      style={createMenuItemStyle}
                      onClick={() => {
                        setCreateMenuOpen(false);
                        openEditor({ category, assetId: null });
                      }}
                    >
                      <span style={createMenuTitleStyle}>Tool collection</span>
                      <span style={createMenuHintStyle}>
                        A reusable set of tools you assign to agents and workflows.
                      </span>
                    </button>
                    {isAdmin && (
                      <button
                        role="menuitem"
                        style={createMenuItemStyle}
                        onClick={() => { setCreateMenuOpen(false); onOpenGatewaysTab?.(); }}
                      >
                        <span style={createMenuTitleStyle}>Federate a gateway</span>
                        <span style={createMenuHintStyle}>
                          Adds an external gateway's tools for <strong>everyone</strong>
                          {' '}in this workspace.
                        </span>
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          ) : (
            <button onClick={() => openEditor({ category, assetId: null })} style={primaryBtnStyle}>
              + Create
            </button>
          )}
        </div>
      </div>
      )}

      {/* Error state */}
      {categoryErrors[category] && (
        <div style={errorBannerStyle}>
          {categoryErrors[category]}
          <button onClick={() => refreshCategory(category)} style={{ marginLeft: 8, ...secondaryBtnStyle, padding: '3px 8px' }}>
            Retry
          </button>
        </div>
      )}

      {refreshingCategory && (
        <div role="status" style={{ marginBottom: 10, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' }}>
          Refreshing your {activeTab}…
        </div>
      )}

      {/* Gateway browser (Tools tab only). The browser is the single
          source of truth for built-in + gateway-provided tools — the
          parent search box is piped in so the user gets one search field
          for both their drafts and the read-only catalog. */}
      {activeTab === 'tools' && showGateway && (
        <div style={{ marginBottom: 20, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, overflow: 'hidden' }}>
          <GatewayBrowser externalSearch={search} />
        </div>
      )}

      {/* MCP servers — a SOURCE of tools, so this sits with the tool groups
          above rather than with collections below (a collection is a selection
          over sources, not a source).

          Split into two named groups because a single "MCP servers" heading was
          genuinely ambiguous: it read as "every MCP server the harness can reach",
          which invited the question of why registered gateways were not in it.
          They are a different thing — a gateway's tools reach agents over its own
          OAuth connection, and Settings → Gateways manages those. What this panel
          holds is the platform server that ships with the harness, plus whatever
          the caller registered themselves. */}
      {activeTab === 'tools' && (mcpServers.length > 0 || mcpLoading || mcpListError || mcpUnreadable.length > 0) && (
        <div style={{ marginBottom: 20 }}>
          {mcpLoading && mcpServers.length === 0 && (
            <div
              style={{
                padding: '10px 12px',
                fontSize: 'var(--fs-xs)',
                color: theme.colors.textSecondary,
              }}
            >
              Loading MCP servers…
            </div>
          )}
          {mcpListError && (
            <div role="alert" style={errorBannerStyle}>
              {mcpListError}
              <button
                onClick={() => setMcpListError(null)}
                style={{ marginLeft: 8, ...secondaryBtnStyle, padding: '3px 8px' }}
              >
                Dismiss
              </button>
            </div>
          )}
          {mcpError && (
            <div role="alert" style={errorBannerStyle}>
              {mcpError}
              <button
                onClick={() => setMcpError(null)}
                style={{ marginLeft: 8, ...secondaryBtnStyle, padding: '3px 8px' }}
              >
                Dismiss
              </button>
            </div>
          )}
          {mcpUnreadable.length > 0 && (
            <div role="status" style={mcpUnreadableStyle}>
              ⚠ Couldn't read {mcpUnreadable.length === 1 ? 'one gateway' : `${mcpUnreadable.length} gateways`}, so this list may be incomplete: {mcpUnreadable.join(', ')}
            </div>
          )}
          {platformMcpServers.length > 0 && (
            <div style={panelStaticHeaderStyle}>
              <span>
                {platformMcpServers.length === 1
                  ? 'Platform MCP server'
                  : 'Platform MCP servers'}
              </span>
              <span style={panelCountBadgeStyle}>{platformMcpServers.length}</span>
            </div>
          )}
          {platformMcpServers.length > 0 && (
            <div style={mcpGroupHintStyle}>
              Built into the harness — available to everyone, not registered by you.
            </div>
          )}
          {renderMcpRows(platformMcpServers)}
          {ownMcpServers.length > 0 && (
            <div style={panelStaticHeaderStyle}>
              <span>Your MCP servers</span>
              <span style={panelCountBadgeStyle}>{ownMcpServers.length}</span>
            </div>
          )}
          {renderMcpRows(ownMcpServers)}
        </div>
      )}

      <AddMcpServerModal
        open={addMcpOpen}
        onClose={() => setAddMcpOpen(false)}
        onRegistered={record => setMcpServers(list => [...list, record])}
      />

      {/* The "Yours" tool-collections inventory, shown ONLY when there is at least
          one. It was deleted outright in `174db4a` because on an account with none
          it rendered an empty bordered box next to the tool SOURCES that matter
          here (built-in tools + MCP servers) — but that left the category with no
          home at all: nothing else in the app can create, list, edit or delete a
          collection, so they could only be SELECTED in the agent editor and never
          made. Count-gating keeps both properties: no empty box, and a real home
          the moment one exists. Creation does not depend on this gate — it lives in
          `+ Create` above and is always reachable. */}
      {toolCollections.length > 0 && (
        <ToolsTabBody
          toolCollections={toolCollections}
          search={search}
          loading={initialCategoryLoad}
          onEdit={(id) => openEditor({ category: 'tool-collection', assetId: id })}
          onDelete={async (id) => { await deleteToolCollectionDraft(id); optimisticRemoveToolCollection(id); }}
          onTest={(id) => { setHarnessTarget({ category: 'tool-collection', assetId: id }); setHarnessOpen(true); }}
        />
      )}
    </div>
  );
}

// --- Tab bodies ----------------------------------------------------------
// Each body owns the "Yours" + "Built-in & gateway" split.

/**
 * The skills inventory list.
 *
 * Rendered by `components/SkillsCenter.tsx` (the main-app Skills surface), not
 * here — this module no longer has a skills tab. It stays in this file because
 * it is built from the inventory primitives below (`SectionHeader`,
 * `PublishedList`, `filterByName`…) that the Tools and Hooks bodies also use;
 * moving it would mean moving those, which is a separate refactor.
 */
export function SkillsTabBody({
  skills, publishedSkills, search, loading, onView, onEdit, onDelete, onTest, onPublish, onForkPublished,
  onViewPublished, onPurge, createLabel,
}: {
  /** Open an immutable published version read-only. */
  onViewPublished?: (assetId: string, version: number) => void;
  /**
   * Label of the sidebar create affordance. The empty state NAMES it rather
   * than rendering its own button: the sidebar "New skill" action is the single
   * create path, and a second identical button one glance away read as a
   * duplicate. Keep this passed — without it `EmptyState` falls back to
   * `+ Create`, a button this surface has never had.
   */
  createLabel?: string;
  skills: SkillDraft[];
  publishedSkills: PublishedAssetSummary[];
  search: string;
  loading: boolean;
  onView: (id: string) => void;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
  onTest: (id: string) => void;
  onPublish: (id: string) => Promise<void> | void;
  onForkPublished: (id: string) => void | Promise<void>;
  /**
   * Hard-delete a published lineage. Optional so the Settings host, which has no
   * purge dialog mounted, simply renders no Delete rather than a button that
   * cannot complete.
   */
  onPurge?: (assetId: string, name: string) => void;
}) {
  const yours = filterByName(skills, search);
  /*
   * One row per SKILL, not per version — the same shape as the Agents center.
   * `counts.published` below therefore counts lineages too, so the filter chip
   * keeps agreeing with the rows on screen.
   */
  const published = collapseToLineages(filterPublished(publishedSkills, search))
    .map(({ summary, versionCount }) => ({ ...summary, versionCount }));
  const [stateFilter, setStateFilter] = useState<InventoryState>('all');

  // `all` keeps the long-standing shape: drafts always shown, Published only
  // when populated. An explicit pick is literal — it shows that one section even
  // when empty, otherwise choosing a filter would blank the pane with no reason
  // given.
  const showDrafts = stateFilter !== 'published';
  const showPublished = stateFilter === 'published' || (stateFilter === 'all' && published.length > 0);

  return (
    <>
      <InventoryStateFilter
        value={stateFilter}
        onChange={setStateFilter}
        ariaLabel="Filter skills by state"
        counts={{ all: yours.length + published.length, drafts: yours.length, published: published.length }}
      />

      {showDrafts && (
        <>
          <InventorySectionHeader label="Yours (drafts)" />
          {loading ? (
            <InventorySkeleton label="Loading your skills" />
          ) : yours.length > 0 ? (
            <SkillsList items={yours} onView={onView} onEdit={onEdit} onDelete={onDelete} onTest={onTest} onPublish={onPublish} />
          ) : (
            <EmptyState label="skills" cta={createLabel} />
          )}
        </>
      )}

      {showPublished && (
        <>
          <InventorySectionHeader label="Published" />
          {published.length > 0 ? (
            <PublishedList
              items={published}
              onEdit={onForkPublished}
              onTest={onTest}
              onView={onViewPublished}
              onDelete={onPurge}
            />
          ) : (
            <EmptyState>No published skills yet.</EmptyState>
          )}
        </>
      )}
    </>
  );
}

/**
 * The tool-collection inventory list.
 *
 * Deliberately has no "Published" segment and no Publish action, unlike Skills
 * and Agents: a tool collection is FLAT. It has no version sub-tier and no
 * published form — `lab._canonical_ops._VERSIONED_CATEGORIES` excludes the
 * category and `ToolCollectionCRUD.promote` raises `NotImplementedError` — so
 * the draft row IS the canonical asset. This surface previously rendered both,
 * and the Publish button reached a server call that could only ever throw
 * `ValueError` while the Published section could never populate.
 *
 * A collection reaches agents by being SELECTED in the agent editor, not by
 * being published; the agent's own publish is what freezes it (the published
 * agent snapshot pins the collection's tool refs — see
 * `_canonical_ops._resolve_tool_collection_snapshots`).
 */
function ToolsTabBody({
  toolCollections, search, loading, onEdit, onDelete, onTest,
}: {
  toolCollections: ToolCollectionDraft[];
  search: string;
  loading: boolean;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
  onTest: (id: string) => void;
}) {
  const yours = filterByName(toolCollections, search);

  return (
    <>
      <InventorySectionHeader label="Yours" />
      {loading ? (
        <InventorySkeleton label="Loading your tool collections" />
      ) : yours.length > 0 ? (
        <ToolsList items={yours} onEdit={onEdit} onDelete={onDelete} onTest={onTest} />
      ) : (
        <EmptyState label="tool collections" />
      )}
    </>
  );
}

// --- List components ----------------------------------------------------

function SkillsList({ items, onView, onEdit, onDelete, onTest, onPublish }: { items: SkillDraft[]; onView: (id: string) => void; onEdit: (id: string) => void; onDelete: (id: string) => void; onTest?: (id: string) => void; onPublish?: (id: string) => Promise<void> | void }) {
  if (items.length === 0) return <EmptyState label="skills" />;
  return (
    <InventoryRowList>
      {items.map(item => (
        <ListRow key={item.assetId} name={item.name} description={item.description}
          meta={formatSkillPackageCount(item.supportingFiles)}
          status={item.status} onView={() => onView(item.assetId)} onEdit={() => onEdit(item.assetId)} onDelete={() => onDelete(item.assetId)}
          onTest={onTest ? () => onTest(item.assetId) : undefined}
          onPublish={onPublish ? () => onPublish(item.assetId) : undefined} />
      ))}
    </InventoryRowList>
  );
}

function ToolsList({ items, onEdit, onDelete, onTest }: { items: ToolCollectionDraft[]; onEdit: (id: string) => void; onDelete: (id: string) => void; onTest?: (id: string) => void }) {
  if (items.length === 0) return <EmptyState label="tool collections" />;
  return (
    <InventoryRowList>
      {items.map(item => (
        // No `onPublish`: the category has no published form. See ToolsTabBody.
        <ListRow key={item.assetId} name={item.name} description={item.description}
          meta={formatToolRefCount(item.toolRefs)}
          status={item.status} onEdit={() => onEdit(item.assetId)} onDelete={() => onDelete(item.assetId)}
          onTest={onTest ? () => onTest(item.assetId) : undefined} />
      ))}
    </InventoryRowList>
  );
}

/**
 * Renderer for the "Published" segment — immutable versioned rows from
 * the actor's `USER#<actor>#VERSIONS` partition. The version number is
 * shown as a badge; clicking Edit calls `editAsset` (forks the current
 * version into a fresh `OWNED` working copy) and the host routes the
 * user into the editor for that working copy.
 */
function PublishedList({
  items, onEdit, onTest, onView, onDelete,
}: {
  /**
   * One row each. Skills collapses its versions into one row per lineage and
   * sets `versionCount`; Tools and Workflows still pass one entry per version
   * and leave it unset, so the count is only rendered when it is known and
   * greater than one.
   */
  items: (PublishedAssetSummary & { versionCount?: number })[];
  onEdit: (assetId: string) => void | Promise<void>;
  onTest: (assetId: string) => void;
  /**
   * Open the immutable version read-only. Optional because only Skills has a
   * viewer that can read a published version; Tools has no equivalent, and an
   * always-on button that 404s would be worse than none.
   */
  onView?: (assetId: string, version: number) => void;
  /**
   * Purge the whole lineage — every published version, the working copy if one
   * exists, and their S3 prefixes. Optional for the same reason as `onView`:
   * only `skill` and `specialty-agent` are purgeable server-side, so offering it
   * on a workflow row would render a button that can only ever 400.
   *
   * Irreversible, so the caller is expected to route this through
   * `PurgeAssetDialog` rather than acting on the click directly.
   */
  onDelete?: (assetId: string, name: string) => void;
}) {
  return (
    <div>
      {/* Keyed per VERSION. `assetId` is the version-LINEAGE id — v2 has no
          separate published_id — so every published version of one asset shares
          it, and keying rows by it handed React duplicate keys. Reconciliation is
          undefined in that case: re-rendering with a freshly derived array left
          the old rows mounted, so typing in the search box and clearing it grew
          the Published section a stale row per pass. The filter chip counted the
          array while the DOM held the orphans, which is how the two disagreed. */}
      <InventoryRowList>
        {items.map((item) => (
          <InventoryRow
            key={`${item.assetId}@v${item.version}`}
            name={item.name}
            description={item.description}
            badge={{ label: 'Published', variant: 'success' }}
            /* On the NAME line, not a meta row of its own. `v1` alone under a
               three-line description read as a second paragraph and cost every row a
               line for one token. Numeric spine kept: the version is in tabular mono
               so v1 / v9 / v207 still align down the list. */
            nameSuffix={
              <>
                <span className="numeric">v{item.version}</span>
                {/* Says the row stands for a lineage, so Delete visibly reaches
                    more than the one version named here. */}
                {item.versionCount !== undefined && item.versionCount > 1 && (
                  <> · <span className="numeric">{item.versionCount}</span> versions</>
                )}
                {item.scope !== 'private' && <> · {item.scope}</>}
              </>
            }
            actions={
              <>
                {onView && (
                  <Button onClick={() => onView(item.assetId, item.version)} aria-label={`View ${item.name}`}>View</Button>
                )}
                <Button onClick={() => onTest(item.assetId)} aria-label={`Test ${item.name}`}>Test</Button>
                <Button onClick={() => onEdit(item.assetId)} aria-label={`Edit ${item.name}`}>Edit</Button>
                {onDelete && (
                  <Button onClick={() => onDelete(item.assetId, item.name)} aria-label={`Delete ${item.name}`}>Delete</Button>
                )}
              </>
            }
          />
        ))}
      </InventoryRowList>
    </div>
  );
}
// --- Shared row component -----------------------------------------------
//
// Row actions are the draft lifecycle in miniature:
//   Test    → opens the inline Test Harness bound to this draft, so the user
//             can try it BEFORE publishing (previously `onTest` was threaded
//             all the way down here but no button was ever rendered, leaving
//             the harness unreachable).
//   Publish → promotes the working copy to the next immutable version. Never
//             gated on "did you test it" — publishing is the user's call and a
//             draft may sit unpublished indefinitely. The only thing that
//             stops it is a backend configuration/validation error, which is
//             surfaced inline on the row.
//   Delete  → two-step confirm (setConfirmDelete was previously never called,
//             so delete was unreachable too).

function ListRow({ name, description, meta, status, onView, onEdit, onDelete, onTest, onPublish }: {
  name: string; status: string;
  /**
   * Optional so a caller whose source cannot report the fact can omit the line
   * entirely rather than render a wrong number — `InventoryRow` drops the slot on
   * `undefined`. See `formatToolRefCount`.
   */
  meta?: string;
  /** Absent when the source does not carry one — the row then shows no description. */
  description?: string;
  onView?: () => void;
  onEdit: () => void; onDelete: () => void;
  onTest?: () => void;
  onPublish?: () => Promise<void> | void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);

  const handlePublish = async () => {
    if (!onPublish) return;
    setPublishError(null);
    setPublishing(true);
    try {
      await onPublish();
    } catch (err) {
      // Config/validation errors are the only publish blocker — show why.
      setPublishError(err instanceof Error ? err.message : 'Publish failed');
    } finally {
      setPublishing(false);
    }
  };

  const handleDelete = async () => {
    setDeleteError(null);
    setDeleting(true);
    try {
      await onDelete();
      setConfirmDelete(false);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  const rowError = publishError ?? deleteError;

  return (
    <InventoryRow
      name={name}
      description={description}
      meta={meta}
      badge={{
        label: status === 'active' ? 'Active' : 'Draft',
        // `neutral` for draft, `success` for active — differing by weight rather
        // than hue, which is what makes them distinguishable in dark-kirocrew.
        variant: status === 'active' ? 'success' : 'neutral',
      }}
      error={rowError}
      onDismissError={rowError ? () => { setPublishError(null); setDeleteError(null); } : undefined}
      actions={
        <>
          {confirmDelete ? (
            <>
              <Button
                variant="danger"
                onClick={handleDelete}
                busy={deleting}
                busyLabel="Deleting…"
                aria-label={`Confirm delete ${name}`}
              >
                Confirm delete
              </Button>
              <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleting}>Cancel</Button>
            </>
          ) : (
            <>
              {onView && <Button onClick={onView} aria-label={`View ${name}`}>View</Button>}
              <Button onClick={onEdit} aria-label={`Edit ${name}`}>Edit</Button>
              {onTest && <Button onClick={onTest} aria-label={`Test ${name}`}>Test</Button>}
              {onPublish && (
                <Button
                  variant="primary"
                  onClick={handlePublish}
                  busy={publishing}
                  busyLabel="Publishing…"
                  aria-label={`Publish ${name}`}
                >
                  Publish
                </Button>
              )}
              <Button onClick={() => setConfirmDelete(true)} aria-label={`Delete ${name}`}>Delete</Button>
            </>
          )}
        </>
      }
    />
  );
}

function InventorySkeleton({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} style={{ display: 'grid', gap: 8, padding: '8px 0' }}>
      {[0, 1, 2].map(index => (
        <div key={index} style={{ height: 42, borderRadius: theme.radius.button, background: theme.colors.surfaceSubtle, opacity: 1 - index * 0.18 }} />
      ))}
    </div>
  );
}

/**
 * "Nothing here yet" card that NAMES the create affordance rather than being
 * one. These surfaces have exactly one create control — the sidebar action —
 * and an in-card button duplicated it a glance away, so that variant is gone.
 * `cta` therefore has to be the real label; the `+ Create` default is a button
 * none of these surfaces has ever had.
 */

// --- Helpers ------------------------------------------------------------

function tabToCategory(tab: CapTab): DraftCategory {
  switch (tab) {
    case 'tools': return 'tool-collection';
    case 'hooks': return 'hook';
  }
}

// Maps the proxy's fail-closed / degraded list error codes to a human line.
// `actor_unresolved` is the fail-closed case: we show nothing rather than risk
// listing another actor's servers. `gateway_unavailable` is a soft degrade —
// the caller's own rows still show, just without live platform state.
function mcpListErrorMessage(code: string): string {
  switch (code) {
    case 'actor_unresolved':
      return "Couldn't verify your identity, so MCP servers can't be listed. Try reloading.";
    case 'gateway_unavailable':
      return 'Live server status is temporarily unavailable; showing your registered servers.';
    case 'registry_unavailable':
      return 'The MCP registry is not configured on this environment.';
    default:
      return "Couldn't load MCP servers.";
  }
}

function countYours(
  tab: CapTab,
  skills: SkillDraft[],
  tools: ToolCollectionDraft[],
): number {
  switch (tab) {
    case 'tools': return tools.length;
    case 'hooks': return 0;
  }
}

// No `countPublished` counterpart to `countYours`: neither tab this surface
// still hosts has a published form. Tool collections are flat (no version
// sub-tier) and hooks are not versioned assets at all, so the tab count is just
// "yours" plus the read-only gateway/built-in rows.

function filterByName<T extends { name: string }>(items: T[], search: string): T[] {
  if (!search) return [...items].sort((a: any, b: any) => (b.updatedAt || 0) - (a.updatedAt || 0));
  const lower = search.toLowerCase();
  return items.filter(i => i.name.toLowerCase().includes(lower));
}

function filterPublished(items: PublishedAssetSummary[], search: string): PublishedAssetSummary[] {
  // Same shape as `filterByName` (name+updatedAt sort) but typed for the
  // published-summary record. Kept separate so future per-version
  // sorting (e.g. show v3 before v1 for the same asset) doesn't have to
  // fork the generic.
  if (!search) return [...items].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  const lower = search.toLowerCase();
  return items.filter(i => i.name.toLowerCase().includes(lower));
}

// --- Styles -------------------------------------------------------------
// Aligned with the aurora-refined design mockup: flat row separators,
// generous vertical padding, Inter/system font at 13-14px for content.

const searchInputStyle: React.CSSProperties = {
  padding: '8px 12px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button,
  fontSize: 'var(--fs-sm)', width: 220, maxWidth: '100%', flex: '1 1 180px', outline: 'none', background: theme.colors.surface, color: theme.colors.textPrimary,
};

const primaryBtnStyle: React.CSSProperties = {
  padding: '7px 14px', background: theme.colors.primary, color: theme.colors.onPrimary,
  borderRadius: theme.radius.button, border: 'none', fontSize: 'var(--fs-sm)', cursor: 'pointer',
  fontWeight: 500,
};

const secondaryBtnStyle: React.CSSProperties = {
  padding: '7px 14px', background: 'transparent', color: theme.colors.textPrimary,
  borderRadius: theme.radius.button, border: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-sm)', cursor: 'pointer',
};

const linkBtnStyle: React.CSSProperties = {
  background: 'none', border: 'none', padding: 0, fontSize: 'var(--fs-xs)',
  color: theme.colors.primary, cursor: 'pointer', fontWeight: 500,
};

const errorBannerStyle: React.CSSProperties = {
  padding: '9px 12px', background: `color-mix(in srgb, ${theme.colors.error} 10%, transparent)`, border: `1px solid ${theme.colors.error}`,
  borderRadius: theme.radius.button, fontSize: 'var(--fs-xs)', color: theme.colors.error, marginBottom: 16,
  display: 'flex', alignItems: 'center',
};
// Publish is the affirmative lifecycle action — solid fill so it reads as the
// primary next step once the user is happy with a draft.

/**
 * Row label for a skill's package contents.
 *
 * `undefined` means the source did not report the file list (the Capability API's
 * normalized summary carries no per-category files), and is deliberately NOT
 * treated as an empty list: claiming "1 file · SKILL.md" for a skill that has
 * three resources is a wrong statement, not a missing one. In that case name the
 * one file we know exists and assert no total.
 */
/**
 * A collection's tool count for the inventory row — or NOTHING when the source
 * did not report one.
 *
 * Absent ≠ empty. The Capability API's normalized `tool-collection` summary carries
 * no `toolRefs` (verified in sandbox: the field is missing from every row), so the
 * previous `` `${item.toolRefs?.length || 0} tools` `` rendered "0 tools" for a
 * collection holding nine — a wrong claim stated as fact, where the honest answer is
 * silence. A locally-created draft DOES carry the field, which is why the bug only
 * showed after a reload.
 *
 * Same shape as `formatSkillPackageCount`, which drops its count for the same reason.
 */
export function formatToolRefCount(toolRefs?: ToolRef[]): string | undefined {
  if (!toolRefs) return undefined;
  return `${toolRefs.length} tool${toolRefs.length === 1 ? '' : 's'}`;
}

export function formatSkillPackageCount(supportingFiles?: string[]): string {
  if (!supportingFiles) return 'SKILL.md';
  const resources = supportingFiles.length;
  const total = resources + 1;
  return resources === 0
    ? '1 file · SKILL.md'
    : `${total} files · SKILL.md + ${resources} resource${resources === 1 ? '' : 's'}`;
}
const nestedHarnessStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  minHeight: 0,
};

const backButtonStyle: React.CSSProperties = {
  alignSelf: 'flex-start',
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  fontSize: 'var(--fs-sm)',
  fontWeight: 500,
  cursor: 'pointer',
};

const createMenuScrimStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: theme.zIndex.popover,
};

const createMenuStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 6px)',
  right: 0,
  zIndex: theme.zIndex.popover + 1,
  minWidth: 300,
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 4,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
};

const createMenuItemStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'flex-start',
  gap: 2,
  width: '100%',
  padding: '8px 10px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  cursor: 'pointer',
  textAlign: 'left',
};

const createMenuTitleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const createMenuHintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  lineHeight: 1.35,
};

// Sub-label under a group header, explaining what the group IS. The ambiguity
// this panel had was about scope, not styling, so the answer belongs on screen
// rather than in a comment.
const mcpGroupHintStyle: React.CSSProperties = {
  padding: '0 10px 6px',
  // The 11px "meta/section header" slot uses the shared typography token.
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
};

// Deliberately not `errorBannerStyle`: an unreadable gateway is a caveat on an
// otherwise-good list, not a failure of it. Reads as a note so the rows that DID
// load still look trustworthy.
const mcpUnreadableStyle: React.CSSProperties = {
  padding: '7px 10px',
  fontSize: 'var(--fs-2xs)',
  lineHeight: 1.45,
  color: theme.colors.textSecondary,
  background: 'color-mix(in srgb, var(--color-text-secondary) 7%, transparent)',
  borderRadius: theme.radius.button,
  marginBottom: 6,
};

