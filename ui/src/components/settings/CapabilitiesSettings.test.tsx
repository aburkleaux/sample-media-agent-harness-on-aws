// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi, beforeAll, afterAll } from 'vitest';
// Type-only, so `vi.mock('../../lab-api')` below does not affect it — but it does
// stop `toolCollections: []` inferring as `never[]`, which made assigning a
// collection in a test a type error while the tests themselves still passed.
import type { ToolCollectionDraft } from '../../lab-api';

const context = vi.hoisted(() => ({
  skills: [{ assetId: 'skill-1', name: 'audit-skill', category: 'skill', status: 'archived', createdAt: 1, updatedAt: 1, s3Prefix: '', skillMd: '', supportingFiles: [] }],
  toolCollections: [] as ToolCollectionDraft[],
  publishedSkills: [],
  loadingCategories: { skill: false, 'specialty-agent': false, 'tool-collection': false, workflow: false, hook: false },
  loadedCategories: { skill: true, 'specialty-agent': true, 'tool-collection': true, workflow: false, hook: false },
  categoryErrors: { skill: null, 'specialty-agent': null, 'tool-collection': null, workflow: null, hook: null },
  ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
  refreshCategory: vi.fn().mockResolvedValue(undefined),
  setEditingTarget: vi.fn(),
  optimisticRemoveSkill: vi.fn(),
  optimisticRemoveSpecialtyAgent: vi.fn(),
  optimisticRemoveToolCollection: vi.fn(),
  setHarnessTarget: vi.fn(),
}));

vi.mock('../../LabContext', () => ({ useLabContext: () => context }));
vi.mock('../../lab-api', () => ({
  deleteSkillDraft: vi.fn(), deleteSpecialtyAgentDraft: vi.fn(), deleteToolCollectionDraft: vi.fn(),
  editAsset: vi.fn(), publishAsset: vi.fn(),
}));
vi.mock('../GatewayBrowser', () => ({ default: () => <div data-testid="gateway-browser">Gateway Browser</div> }));
vi.mock('../BuiltInSkillsBrowser', () => ({ default: () => <div data-testid="built-in-skills">Built-in Skills</div> }));
vi.mock('./HooksPanel', () => ({ default: () => <div>Hooks Panel</div> }));

// The panel gained useIsGatewayAdmin (to gate the "Federate a gateway" item on
// the Create menu). Unmocked it reaches the real auth manager, which does
// `fetch("/config")` — a relative URL with no base in Node, so it rejects
// unhandled. All assertions still pass, but vitest reports the rejection and
// exits non-zero, and whether it lands before teardown depends on how slow the
// run is: green locally at ~16s, 8 errors in CI at ~145s. Same pattern as
// SessionSidebar.test.tsx, which mocks this for the same reason.
vi.mock('../../auth/isGatewayAdmin', () => ({
  useIsGatewayAdmin: () => false,
}));


// Registered-server listing runs on first visit to the Tools tab.
// Controllable so a test can drive the MCP list. A fixed implementation meant
// vi.spyOn could not reach the binding the component captured at import.
// Returns `{ servers, unreadable }` -- listMcpServers resolves McpServerListing.
// A mock of the wrong shape crashes the component on mcpServers.filter and
// surfaces ONLY as an "unhandled error" while the suite still reports every test
// passing, so the shape has to be kept in step deliberately. (It was an array
// until the panel started listing shared targets across every registered gateway
// and needed to report the gateways it could not read.)
const mcpApi = vi.hoisted(() => ({
  impl: (_opts?: any): Promise<any> =>
    Promise.resolve({ servers: [], unreadable: [] }),
}));
vi.mock('../../api', () => ({
  listMcpServers: (opts?: any) => mcpApi.impl(opts),
  mcpServerStatus: () => Promise.resolve('ready'),
  deleteMcpServer: () => Promise.resolve(),
}));
/*
 * Renders a back control and a Run, because the real panel does — it owns both now,
 * on one toolbar. A stub without them would let a host drop its own back control and
 * still pass, leaving the user with no way out of the harness.
 */
vi.mock('../TestHarnessPanel', () => ({
  default: ({ onClose, backLabel = '\u2190 Back' }: { onClose: () => void; backLabel?: string }) => (
    <div data-testid="test-harness">
      Test Harness
      <button onClick={onClose}>{backLabel}</button>
      <button>Run</button>
    </div>
  ),
}));
vi.mock('../SkillDetailsView', () => ({ default: ({ target, onBack }: any) => <div data-testid="skill-detail"><button onClick={onBack}>Back from detail</button>{target.kind}:{target.assetId ?? target.name}</div> }));
vi.mock('../SkillPackageImport', () => ({ default: () => <div data-testid="skill-import">Skill Import</div> }));

import CapabilitiesSettings, { formatSkillPackageCount, formatToolRefCount } from './CapabilitiesSettings';
import { deleteToolCollectionDraft } from '../../lab-api';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // `impl` is a plain property on a hoisted object, so `clearAllMocks` does not
  // touch it — a test that overrides it would otherwise leak its servers into
  // every test that runs after.
  mcpApi.impl = () => Promise.resolve({ servers: [], unreadable: [] });
  // Same reason, and it matters more here: the tool-collection inventory is
  // count-gated, so a leaked collection would make the "renders nothing when
  // there are none" case pass or fail depending on test order.
  context.toolCollections = [];
});

describe('CapabilitiesSettings', () => {
  it('includes mandatory SKILL.md in package counts', () => {
    expect(formatSkillPackageCount([])).toBe('1 file · SKILL.md');
    expect(formatSkillPackageCount(['references/guide.md'])).toBe('2 files · SKILL.md + 1 resource');
    expect(formatSkillPackageCount(['references/guide.md', 'scripts/run.py'])).toBe('3 files · SKILL.md + 2 resources');
  });

  // Absent ≠ empty. The Capability API's normalized summary carries no
  // per-category file list, so flipping `skill` to `canonical` for the cold-start
  // win makes `supportingFiles` undefined. Rendering that as `[]` asserted
  // "1 file · SKILL.md" for skills that really had resources — a wrong claim,
  // not a missing one, and now on a main-nav surface.
  it('asserts no total when the source does not report the file list', () => {
    expect(formatSkillPackageCount(undefined)).toBe('SKILL.md');
    expect(formatSkillPackageCount()).toBe('SKILL.md');
    // And it must NOT be confused with a skill known to have zero resources.
    expect(formatSkillPackageCount(undefined)).not.toBe(formatSkillPackageCount([]));
  });


  /*
   * Absent ≠ empty, the tool-collection edition. The Capability API's normalized
   * summary carries NO `toolRefs` (verified against sandbox: the field is missing
   * from every row), so `?? 0` reported "0 tools" for a collection holding nine.
   * A locally-created draft does carry it, so the wrong number only appeared after
   * a reload — which is how it survived.
   */
  it('reports no tool count when the source does not carry one', () => {
    expect(formatToolRefCount(undefined)).toBeUndefined();
    expect(formatToolRefCount()).toBeUndefined();
    // And "unknown" must not collapse into the same answer as "genuinely none".
    expect(formatToolRefCount([])).toBe('0 tools');
    expect(formatToolRefCount(undefined)).not.toBe(formatToolRefCount([]));
  });

  it('pluralises the tool count', () => {
    expect(formatToolRefCount([{ name: 'file_read', version: '1' }])).toBe('1 tool');
    expect(formatToolRefCount([
      { name: 'file_read', version: '1' },
      { name: 'http_request', version: '1' },
    ])).toBe('2 tools');
  });

  // Skills moved to the main app (`components/SkillsCenter.tsx`). The four cases
  // that covered the built-in browser, folder import, detail view and nested
  // harness moved with it — see `SkillsCenter.test.tsx`. This tab no longer
  // exists, so asserting on it here would only pin a surface that is gone.
});

describe('MCP servers loading flag', () => {
  // Regression: the panel hung on "Loading MCP servers..." forever. The load
  // effect sets `mcpLoaded`, which is in its OWN dependency array, so the effect
  // re-runs: run 1's cleanup sets `cancelled = true` while run 2 early-returns on
  // the `mcpLoaded` guard without starting a fetch. While the spinner clear was
  // guarded by `!cancelled` it therefore never ran -- the request had already
  // succeeded, yet the panel showed a spinner and a count of 0 indefinitely.
  // A loading flag must settle on EVERY path; `cancelled` gates data writes only.
  it('settles even though the effect re-runs and cancels the first pass', async () => {
    render(<CapabilitiesSettings tab="tools" hideTabBar />);
    await waitFor(() => {
      expect(screen.queryByText(/Loading MCP servers/i)).toBeNull();
    });
  });
});

// A single "MCP servers" heading read as "every MCP server the harness can
// reach", which invited the question of why registered gateways were not in it.
// They are a different thing — their tools arrive over the gateway's own OAuth
// connection. So the panel names what it actually holds: the platform server that
// ships with the harness, and whatever the caller registered themselves.
describe('MCP servers panel grouping', () => {
  const own = {
    target_name: 'usr-abc',
    target_id: 'U1',
    status: 'ready' as const,
    server_name: 'my-own-server',
    endpoint: 'https://mine.example.com/mcp',
    removable: true,
  };
  const platform = {
    target_name: 'mediaworks-harness-aws-mcp',
    target_id: 'P8',
    status: 'ready' as const,
    server_name: 'mediaworks-harness-aws-mcp',
    endpoint: 'Platform gateway',
    platform: true,
    removable: false,
  };

  it('names the built-in server as the platform one, singular', async () => {
    mcpApi.impl = () => Promise.resolve({ servers: [platform], unreadable: [] });
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => {
      expect(screen.getByText('Platform MCP server')).toBeTruthy();
    });
    // And says what that means, since the ambiguity was about scope.
    expect(screen.getByText(/Built into the harness/i)).toBeTruthy();
  });

  it('separates the caller\'s own servers from the platform one', async () => {
    mcpApi.impl = () => Promise.resolve({ servers: [own, platform], unreadable: [] });
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => {
      expect(screen.getByText('my-own-server')).toBeTruthy();
    });
    expect(screen.getByText('Platform MCP server')).toBeTruthy();
    expect(screen.getByText('Your MCP servers')).toBeTruthy();
  });

  it('offers Remove for the caller\'s own server but not the platform one', async () => {
    // A platform target has no ownership row, so the delete could only ever fail:
    // absent rather than disabled.
    mcpApi.impl = () => Promise.resolve({ servers: [own, platform], unreadable: [] });
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => {
      expect(screen.getByText('my-own-server')).toBeTruthy();
    });
    expect(screen.getByLabelText('Remove my-own-server')).toBeTruthy();
    expect(screen.queryByLabelText('Remove mediaworks-harness-aws-mcp')).toBeNull();
  });

  it('groups a server with no platform flag as the caller\'s own', async () => {
    // An older runtime sends neither flag. Grouping it as "yours" is the
    // recoverable error; the other way would label it built-in AND hide Remove.
    const legacy = { ...own, platform: undefined, removable: undefined };
    mcpApi.impl = () => Promise.resolve({ servers: [legacy], unreadable: [] });
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => {
      expect(screen.getByText('Your MCP servers')).toBeTruthy();
    });
    expect(screen.queryByText('Platform MCP server')).toBeNull();
    expect(screen.getByLabelText('Remove my-own-server')).toBeTruthy();
  });

  it('paints from the fast phase, then replaces it with the live one', async () => {
    /*
     * The two-phase read that took this list off the agent runtime. Phase 1 is one
     * DynamoDB Query (the caller's own servers); phase 2 adds the platform targets
     * and live status from the control plane. Previously ONE `user_mcp_list` invoke
     * cold-started the runtime every time — 10.9s / 11.6s / 12.7s measured on three
     * separate opens, with the section blank throughout.
     *
     * Asserted through the calls rather than timings: `live` must be absent on the
     * first and true on the second, or we have either lost the instant paint or
     * lost the platform rows.
     */
    const calls: Array<any> = [];
    let releaseLive: (v: any) => void = () => {};
    mcpApi.impl = (opts?: any) => {
      calls.push(opts);
      if (opts?.live) return new Promise(resolve => { releaseLive = resolve; });
      return Promise.resolve({
        servers: [{ target_name: 'usr-a', target_id: 't-a', server_name: 'Alice Notes', endpoint: 'https://a/mcp', status: 'ready', platform: false, removable: true }],
        // Empty: `unreadable` renders as a "couldn't read" warning, so it must not
        // carry "not fetched yet". The platform row simply arrives in phase 2.
        unreadable: [],
      });
    };

    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    // Phase 1 on screen while phase 2 is still outstanding.
    expect(await screen.findByText('Alice Notes')).toBeTruthy();
    expect(calls[0]?.live).toBeUndefined();
    await waitFor(() => expect(calls.some(c => c?.live)).toBe(true));

    releaseLive({
      servers: [
        { target_name: 'usr-a', target_id: 't-a', server_name: 'Alice Notes', endpoint: 'https://a/mcp', status: 'ready', platform: false, removable: true },
        { target_name: 'mediaworks-harness-aws-mcp', target_id: 't-aws', server_name: 'mediaworks-harness-aws-mcp', endpoint: 'Platform gateway', status: 'ready', platform: true, removable: false },
      ],
      unreadable: [],
    });

    // The platform row arrives only in phase 2 — that is the whole reason phase 1
    // reports it as pending rather than saying nothing.
    expect(await screen.findByText('mediaworks-harness-aws-mcp')).toBeTruthy();
    expect(screen.getByText('Alice Notes')).toBeTruthy();
  });

  it('does not run the live phase when the actor could not be resolved', async () => {
    // Fail-closed is terminal: the live pass would fail identically and would only
    // overwrite the reason with a second copy of itself.
    const calls: Array<any> = [];
    mcpApi.impl = (opts?: any) => {
      calls.push(opts);
      return Promise.resolve({ servers: [], unreadable: [], error: 'actor_unresolved' });
    };

    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    await new Promise(r => setTimeout(r, 50));
    expect(calls.filter(c => c?.live)).toHaveLength(0);
  });

  it('says the list is incomplete when the read failed', async () => {
    // An empty panel must state a reason rather than read as "the harness has no
    // MCP server" — the same silent-shortfall bug in a different disguise.
    mcpApi.impl = () =>
      Promise.resolve({
        servers: [],
        unreadable: ['Platform gateway (AccessDeniedException)'],
      });
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    await waitFor(() => {
      expect(screen.getByText(/may be incomplete/i)).toBeTruthy();
    });
    // Zero rows plus a warning must not collapse the section — that would hide
    // the only signal that something is wrong.
    expect(screen.getByText(/Platform gateway \(AccessDeniedException\)/)).toBeTruthy();
  });
});

/*
 * Tool collections had no home in the UI at all. `174db4a` deleted BOTH the
 * `+ Create → Tool collection` menu item and this page's inventory, because on an
 * account with none the inventory was an empty bordered box next to the tool
 * SOURCES. But nothing else in the app creates, lists, edits or deletes a
 * collection — the agent editor can only SELECT one — so the category became
 * unreachable: users could pick collections they had no way to make.
 *
 * Restored with the inventory COUNT-GATED, which keeps both properties: no empty
 * box, and a real home the moment one exists. Creation is deliberately outside the
 * gate, in `+ Create`, so it works when the list is empty — that is the case that
 * was broken.
 */
describe('tool collections on the Tools tab', () => {
  const collection = {
    assetId: 'tc-1',
    name: 'editorial-tools',
    category: 'tool-collection' as const,
    status: 'active' as const,
    createdAt: 1,
    updatedAt: 1,
    s3Prefix: '',
    toolRefs: [{ name: 'file_read', version: '1' }, { name: 'http_request', version: '1' }],
  };

  // The case the removal broke: with nothing to list there must still be a way to
  // make the first one, or the category is unreachable from a clean account.
  it('offers Tool collection in the Create menu even when there are none', () => {
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    fireEvent.click(screen.getByRole('button', { name: '+ Create' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Tool collection/ }));

    expect(context.setEditingTarget).toHaveBeenCalledWith({
      category: 'tool-collection',
      assetId: null,
    });
  });

  // Honours the complaint that motivated `174db4a`: no bordered "Yours" box, and
  // no "no tool collections" empty state, on an account with none.
  it('renders no inventory section when there are none', () => {
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    expect(screen.queryByText('Yours')).toBeNull();
  });

  it('lists a collection once one exists and routes Edit to its editor', () => {
    context.toolCollections = [collection];
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    expect(screen.getByText('Yours')).toBeTruthy();
    expect(screen.getByText('editorial-tools')).toBeTruthy();
    // Meta names the tool count, which is the one thing a collection row is for.
    expect(screen.getByText('2 tools')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Edit editorial-tools' }));
    expect(context.setEditingTarget).toHaveBeenCalledWith({
      category: 'tool-collection',
      assetId: 'tc-1',
    });
  });

  it('deletes through the API and drops the row optimistically', async () => {
    context.toolCollections = [collection];
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    fireEvent.click(screen.getByRole('button', { name: 'Delete editorial-tools' }));
    // Two-step: the bare Delete only arms the confirm control.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete editorial-tools' }));

    await waitFor(() => {
      expect(deleteToolCollectionDraft).toHaveBeenCalledWith('tc-1');
    });
    expect(context.optimisticRemoveToolCollection).toHaveBeenCalledWith('tc-1');
  });

  // A collection has no published form (`_VERSIONED_CATEGORIES` excludes it and
  // `promote` raises), so a Publish control here would offer an action the backend
  // refuses.
  it('offers no Publish on a collection row', () => {
    context.toolCollections = [collection];
    render(<CapabilitiesSettings tab="tools" hideTabBar />);

    expect(screen.queryByRole('button', { name: /Publish editorial-tools/ })).toBeNull();
  });
});
