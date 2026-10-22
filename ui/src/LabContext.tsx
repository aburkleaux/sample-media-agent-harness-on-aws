// LabContext.tsx — Shared state provider for the MediaWorks Lab Frontend.
// Manages inventory data, loading states, session, right panel state, and refresh actions.

import React, { createContext, useContext, useState, useRef, useCallback } from 'react';
import type {
  DraftCategory,
  PublishedAssetSummary,
  SkillDraft,
  SpecialtyAgentDraft,
  ToolCollectionDraft,
  WorkflowDraft,
} from './lab-api';
import {
  listMyPublishedAssets,
  listSkillDrafts,
  listSpecialtyAgentDrafts,
  listToolCollectionDrafts,
  listWorkflowDrafts,
} from './lab-api';
import type {
  BuiltInAgentRow,
  BuiltInSkillRow,
  CapabilityMode,
} from './capability-api';
import {
  bootstrapCapabilities,
  fetchCapabilityConfig,
  toBuiltInAgents,
  toBuiltInSkills,
  toDraftMetadata,
  toPublishedSummaries,
} from './capability-api';

// --- Interfaces ---

export interface LabContextValue {
  // Inventory data — drafts (working copies under USER#<actor>#OWNED)
  skills: SkillDraft[];
  specialtyAgents: SpecialtyAgentDraft[];
  toolCollections: ToolCollectionDraft[];
  workflows: WorkflowDraft[];

  // Inventory data — published versions (immutable rows under USER#<actor>#VERSIONS).
  // Refreshed alongside drafts and surfaced as a separate "PUBLISHED" section
  // in the Capabilities tab. Editing a published asset forks it back into
  // the OWNED partition via `editAsset`.
  // No `publishedToolCollections`: the category is flat — it has no version
  // sub-tier and no published form at all, so VERSIONS is empty for it by
  // construction (see `fetchCategory`, which skips the round trip).
  publishedSkills: PublishedAssetSummary[];
  publishedSpecialtyAgents: PublishedAssetSummary[];
  publishedWorkflows: PublishedAssetSummary[];

  // Built-ins, carried on the same page as the drafts above and rendered in their
  // own tab (R10). `null` = this category has not been loaded from the Capability
  // API yet, which the panels show as "loading" rather than as "none exist";
  // `[]` = loaded and genuinely empty.
  builtInAgents: BuiltInAgentRow[] | null;
  builtInSkills: BuiltInSkillRow[] | null;

  // Loading states per category
  loadingCategories: Record<DraftCategory, boolean>;
  loadedCategories: Record<DraftCategory, boolean>;
  categoryErrors: Record<DraftCategory, string | null>;

  // Load once on first use; explicit refresh always bypasses this guard.
  ensureCategoryLoaded: (category: DraftCategory) => Promise<void>;

  // Session
  sessionId: string;
  resetSession: () => void;
  switchSession: (id: string) => void;

  // Right panel
  rightPanelState: 'collapsed' | 'harness' | 'readiness';
  setRightPanelState: (state: 'collapsed' | 'harness' | 'readiness') => void;
  harnessTarget: { category: DraftCategory; assetId: string } | null;
  setHarnessTarget: (target: { category: DraftCategory; assetId: string } | null) => void;
  readinessTarget: string | null;
  setReadinessTarget: (assetId: string | null) => void;

  // Center panel editing state. assetId === null means "create new" mode.
  editingTarget: { category: DraftCategory; assetId: string | null } | null;
  setEditingTarget: (target: { category: DraftCategory; assetId: string | null } | null) => void;

  // Refresh actions
  refreshCategory: (category: DraftCategory) => Promise<void>;
  refreshAll: () => Promise<void>;

  // Optimistic update helpers
  optimisticAddSkill: (skill: SkillDraft) => void;
  optimisticRemoveSkill: (assetId: string) => void;
  optimisticAddSpecialtyAgent: (agent: SpecialtyAgentDraft) => void;
  optimisticRemoveSpecialtyAgent: (assetId: string) => void;
  optimisticAddToolCollection: (collection: ToolCollectionDraft) => void;
  optimisticRemoveToolCollection: (assetId: string) => void;
  optimisticAddWorkflow: (workflow: WorkflowDraft) => void;
  optimisticRemoveWorkflow: (assetId: string) => void;
}

// --- Context ---

const LabContext = createContext<LabContextValue | null>(null);

// --- Helper: generate session ID ---

function generateSessionId(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  // Fallback for environments without crypto.randomUUID
  return `session-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// --- Max retry constant ---

const MAX_RETRIES = 3;

// --- Capability API cutover ---
//
// Each category reads from either the legacy `/lab` Runtime path or the
// always-on Capability API, chosen per category by a server-side flag
// (`CAPABILITY_SOURCE_*` on the Capability API task). `legacy` is the default
// and the fallback for every failure mode — an unreachable control plane, an
// unparseable response, a category the deployed service does not know — so the
// worst case of this switch is the behaviour that existed before it.
//
// Rollback is therefore configuration-only, with no deploy and no data change
// (R14 / correctness Property 6).

/** Flags for the whole session, fetched once. Empty until resolved. */
type CapabilityModes = Partial<Record<DraftCategory, CapabilityMode>>;

/**
 * Whether `category` should read from the Capability API.
 *
 * Two modes qualify, and the difference is server-side only:
 *
 *   - `canonical` — the API serves from canonical state (DynamoDB + built-ins).
 *   - `registry`  — the API additionally merges Agent Registry rows.
 *
 * Both mean "an always-on Fargate task answers this read instead of a
 * cold-starting agent runtime", which is where the latency win is (measured in
 * sandbox: ~7.5s cold via `/lab` vs ~220ms). The browser makes the identical
 * request either way, so this must not distinguish them.
 *
 * `shadow` deliberately returns false: shadow mode serves the legacy result and
 * compares asynchronously on the server, so the browser must keep rendering
 * the legacy source or the comparison would be measuring itself.
 */
function usesCapabilityApi(
  modes: CapabilityModes,
  category: DraftCategory,
): boolean {
  const mode = modes[category];
  return mode === 'canonical' || mode === 'registry';
}

// --- Provider Component ---

export function LabContextProvider({ children }: { children: React.ReactNode }) {
  // Inventory state — drafts
  const [skills, setSkills] = useState<SkillDraft[]>([]);
  const [specialtyAgents, setSpecialtyAgents] = useState<SpecialtyAgentDraft[]>([]);
  const [toolCollections, setToolCollections] = useState<ToolCollectionDraft[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowDraft[]>([]);

  // Inventory state — published. Loaded in the same fetchCategory pass as
  // the drafts list so the UI shows a complete picture of "what I own"
  // (working copies + immutable versions) per category. Failures here
  // are non-fatal: the drafts list is the primary surface; published
  // entries become an empty array and the UI just hides the section.
  // Built-ins arrive on the same Capability API page as the drafts. `null` until
  // that page has been read, so a panel can tell "not loaded yet" from "none".
  const [builtInAgents, setBuiltInAgents] = useState<BuiltInAgentRow[] | null>(null);
  const [builtInSkills, setBuiltInSkills] = useState<BuiltInSkillRow[] | null>(null);

  const [publishedSkills, setPublishedSkills] = useState<PublishedAssetSummary[]>([]);
  const [publishedSpecialtyAgents, setPublishedSpecialtyAgents] = useState<PublishedAssetSummary[]>([]);
  const [publishedWorkflows, setPublishedWorkflows] = useState<PublishedAssetSummary[]>([]);

  // Capability inventory is lazy: the workspace/chat shell should not issue
  // eight draft+published requests before the user opens Settings.
  const [loadingCategories, setLoadingCategories] = useState<Record<DraftCategory, boolean>>({
    'skill': false,
    'specialty-agent': false,
    'tool-collection': false,
    'workflow': false,
    'hook': false,
  });
  const [loadedCategories, setLoadedCategories] = useState<Record<DraftCategory, boolean>>({
    'skill': false,
    'specialty-agent': false,
    'tool-collection': false,
    'workflow': false,
    'hook': false,
  });
  const loadedCategoriesRef = useRef(loadedCategories);
  loadedCategoriesRef.current = loadedCategories;
  const categoryLoadsInFlight = useRef<Partial<Record<DraftCategory, Promise<void>>>>({});

  // Error states
  const [categoryErrors, setCategoryErrors] = useState<Record<DraftCategory, string | null>>({
    'skill': null,
    'specialty-agent': null,
    'tool-collection': null,
    'workflow': null,
    'hook': null,
  });

  // Retry counts (not exposed, internal tracking)
  const retryCounts = useRef<Record<DraftCategory, number>>({
    'skill': 0,
    'specialty-agent': 0,
    'tool-collection': 0,
    'workflow': 0,
    'hook': 0,
  });

  // Session state — persist active session ID across reloads
  const [sessionId, setSessionId] = useState<string>(() => {
    const stored = localStorage.getItem('lab-active-session');
    if (stored) return stored;
    const newId = generateSessionId();
    localStorage.setItem('lab-active-session', newId);
    return newId;
  });

  // Per-category read source, fetched once and resolved to "all legacy" on any
  // failure.
  //
  // Held as a PROMISE, not just a value: the first `ensureCategoryLoaded` fires
  // from a mount effect, which can win the race against this fetch. Reading a
  // not-yet-populated ref there would silently pin the first tab of every page
  // load to the legacy path — safe, but it would make a cutover appear not to
  // take effect until a refresh. Every category load awaits this instead, so
  // there is exactly one config request per session and no ordering hazard.
  const capabilityModesPromise = useRef<Promise<CapabilityModes> | null>(null);

  const loadCapabilityModes = useCallback((): Promise<CapabilityModes> => {
    if (!capabilityModesPromise.current) {
      capabilityModesPromise.current = fetchCapabilityConfig()
        .then((config) => (config.capabilitySources ?? {}) as CapabilityModes)
        // fetchCapabilityConfig already swallows failures into all-legacy; this
        // guards against a rejection from anything added around it later.
        .catch(() => ({}) as CapabilityModes);
    }
    return capabilityModesPromise.current;
  }, []);

  // Right panel state
  const [rightPanelState, setRightPanelState] = useState<'collapsed' | 'harness' | 'readiness'>('collapsed');
  const [harnessTarget, setHarnessTarget] = useState<{ category: DraftCategory; assetId: string } | null>(null);
  const [readinessTarget, setReadinessTarget] = useState<string | null>(null);
  const [editingTarget, setEditingTarget] = useState<{ category: DraftCategory; assetId: string | null } | null>(null);

  // --- Fetch a single category ---

  /**
   * Write normalized Capability API rows into the per-category state slot.
   *
   * Each category's view model carries content fields the *list* endpoint has
   * never populated either — `skillMd` and `persona` are loaded by the editor
   * from a separate read — so filling them with empty values here matches the
   * legacy list's actual payload rather than inventing data.
   *
   * The normalized DTO carries no `supportingFiles`, so that field is left
   * UNDEFINED rather than set to `[]`. This used to be a known parity gap: an
   * empty array is indistinguishable from "genuinely no resources", so every
   * skill served this way rendered "1 file · SKILL.md" even with resources
   * attached — a wrong claim rather than a missing one, and more visible now
   * that Skills is a main-nav surface. `formatSkillPackageCount` renders the
   * undefined case without a total instead. The editor still reads the real
   * list, so nothing downstream loses data.
   */
  const setDraftsFor = useCallback(
    (category: DraftCategory, rows: ReturnType<typeof toDraftMetadata>) => {
      switch (category) {
        case 'skill':
          setSkills(rows.map(row => ({
            ...row,
            category: 'skill' as const,
            skillMd: '',
            // supportingFiles intentionally absent — see the docstring above.
          })));
          break;
        case 'specialty-agent':
          setSpecialtyAgents(rows.map(row => ({
            ...row,
            category: 'specialty-agent' as const,
            persona: '',
            compatibleSkills: [],
          })));
          break;
        case 'tool-collection':
          setToolCollections(rows.map(row => ({
            ...row,
            category: 'tool-collection' as const,
            // toolRefs intentionally absent — same reason as `supportingFiles`
            // above. This used to be `toolRefs: []`, which made every listed
            // collection report zero tools once the category read through the
            // Capability API, whose summary does not carry the field.
          })));
          break;
        case 'workflow':
          setWorkflows(rows.map(row => ({
            ...row,
            category: 'workflow' as const,
            steps: [],
          })));
          break;
      }
    },
    [],
  );

  const fetchCategory = useCallback(async (category: DraftCategory, isRetry = false): Promise<void> => {
    // Check retry limit
    if (isRetry && retryCounts.current[category] >= MAX_RETRIES) {
      setCategoryErrors(prev => ({
        ...prev,
        [category]: `Unable to load ${category} inventory after ${MAX_RETRIES} attempts. The service may be unavailable.`,
      }));
      setLoadingCategories(prev => ({ ...prev, [category]: false }));
      return;
    }

    setLoadingCategories(prev => ({ ...prev, [category]: true }));
    setCategoryErrors(prev => ({ ...prev, [category]: null }));

    // Helper: fetch the published list for this category and update the
    // matching state slot. Errors are swallowed and logged so the
    // primary drafts list keeps loading even if VERSIONS is unreadable.
    const setPublishedFor = (data: PublishedAssetSummary[]) => {
      switch (category) {
        case 'skill': setPublishedSkills(data); break;
        case 'specialty-agent': setPublishedSpecialtyAgents(data); break;
        case 'workflow': setPublishedWorkflows(data); break;
        // 'tool-collection' has no published slot — see `hasPublishedForm`.
      }
    };

    // Capability API path. ONE request covers both working copies and versions
    // — the four-tier visibility union returns them together — where the legacy
    // path needs two round trips through the agent runtime (R13: at most one
    // bootstrap request per selected tab).
    if (usesCapabilityApi(await loadCapabilityModes(), category)) {
      try {
        const pages = await bootstrapCapabilities([category]);
        const page = pages[category];
        if (!page) throw new Error(`capability API returned no ${category} page`);
        setDraftsFor(category, toDraftMetadata(page));
        setPublishedFor(toPublishedSummaries(page));
        // Built-ins ride along in the same page (`read_builtins` runs inside the
        // bootstrap), and both mappers above filter them out. Capturing them here
        // is what lets the built-in panels render without a request of their own —
        // they previously called the agent runtime and paid a cold start for data
        // that had already arrived.
        if (category === 'specialty-agent') {
          setBuiltInAgents(toBuiltInAgents(page));
        } else if (category === 'skill') {
          setBuiltInSkills(toBuiltInSkills(page));
        }
        // A degraded source is surfaced, not hidden: the rows shown are real,
        // but incomplete, and presenting them as a full inventory is how a
        // Registry outage would read as "someone deleted my assets".
        if (page.partialErrors?.length) {
          const sources = page.partialErrors.map(e => `${e.source} (${e.error})`).join(', ');
          setCategoryErrors(prev => ({
            ...prev,
            [category]: `Partial results — unavailable: ${sources}`,
          }));
        } else {
          setCategoryErrors(prev => ({ ...prev, [category]: null }));
        }
        retryCounts.current[category] = 0;
        setLoadedCategories(prev => {
          const next = { ...prev, [category]: true };
          loadedCategoriesRef.current = next;
          return next;
        });
      } catch (err) {
        // No automatic fall-back to the runtime path (R12 / Property 5): that
        // would mask a broken control plane behind the very latency this
        // migration removed. Rollback is an operator flipping the category
        // flag back to `legacy`.
        const message = err instanceof Error ? err.message : 'Unknown error';
        retryCounts.current[category] += 1;
        setCategoryErrors(prev => ({ ...prev, [category]: message }));
      } finally {
        setLoadingCategories(prev => ({ ...prev, [category]: false }));
      }
      return;
    }

    // Tool collections are FLAT: no version sub-tier, no published form. The
    // category is excluded from `_canonical_ops._VERSIONED_CATEGORIES` and
    // `ToolCollectionCRUD.promote` raises, so VERSIONS can never hold a row for
    // one and this request is a guaranteed empty answer. Skipping it removes a
    // whole round trip through the agent runtime from opening the Tools tab.
    const hasPublishedForm = category !== 'tool-collection';
    let publishedSucceeded = !hasPublishedForm;
    const fetchPublished = hasPublishedForm
      ? listMyPublishedAssets(category)
          .then(data => {
            setPublishedFor(data);
            publishedSucceeded = true;
          })
          .catch((err) => {
            console.warn(`[lab] listMyPublishedAssets(${category}) failed:`, err);
            setPublishedFor([]);
          })
      : Promise.resolve();

    let draftSucceeded = false;
    try {
      switch (category) {
        case 'skill': {
          const data = await listSkillDrafts();
          setSkills(Array.isArray(data) ? data : []);
          break;
        }
        case 'specialty-agent': {
          const data = await listSpecialtyAgentDrafts();
          setSpecialtyAgents(Array.isArray(data) ? data : []);
          break;
        }
        case 'tool-collection': {
          const data = await listToolCollectionDrafts();
          setToolCollections(Array.isArray(data) ? data : []);
          break;
        }
        case 'workflow': {
          const data = await listWorkflowDrafts();
          setWorkflows(Array.isArray(data) ? data : []);
          break;
        }
      }
      draftSucceeded = true;
      // Reset retry count on success
      retryCounts.current[category] = 0;
      setCategoryErrors(prev => ({ ...prev, [category]: null }));
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      retryCounts.current[category] += 1;

      if (retryCounts.current[category] >= MAX_RETRIES) {
        setCategoryErrors(prev => ({
          ...prev,
          [category]: `Unable to load ${category} inventory after ${MAX_RETRIES} attempts. The service may be unavailable.`,
        }));
      } else {
        setCategoryErrors(prev => ({ ...prev, [category]: message }));
      }
    } finally {
      // Wait for the published-list fetch to settle so the loading flag
      // doesn't drop while VERSIONS is still mid-flight. Already a
      // resolved promise on the success path.
      await fetchPublished;
      if (draftSucceeded && publishedSucceeded) {
        setLoadedCategories(prev => {
          const next = { ...prev, [category]: true };
          loadedCategoriesRef.current = next;
          return next;
        });
      }
      setLoadingCategories(prev => ({ ...prev, [category]: false }));
    }
  }, [setDraftsFor, loadCapabilityModes]);

  // --- Refresh actions ---

  const ensureCategoryLoaded = useCallback(async (category: DraftCategory): Promise<void> => {
    if (category === 'hook' || loadedCategoriesRef.current[category]) return;
    const activeLoad = categoryLoadsInFlight.current[category];
    if (activeLoad) {
      await activeLoad;
      return;
    }
    const load = fetchCategory(category, false).finally(() => {
      delete categoryLoadsInFlight.current[category];
    });
    categoryLoadsInFlight.current[category] = load;
    await load;
  }, [fetchCategory]);

  const refreshCategory = useCallback(async (category: DraftCategory): Promise<void> => {
    // A manual refresh resets the retry counter
    retryCounts.current[category] = 0;
    await fetchCategory(category, false);
  }, [fetchCategory]);

  const refreshAll = useCallback(async (): Promise<void> => {
    const categories: DraftCategory[] = ['skill', 'specialty-agent', 'tool-collection', 'workflow'];
    await Promise.all(categories.map(cat => refreshCategory(cat)));
  }, [refreshCategory]);

  // --- Session management ---

  const resetSession = useCallback(() => {
    const newId = generateSessionId();
    setSessionId(newId);
    localStorage.setItem('lab-active-session', newId);
  }, []);

  const switchSession = useCallback((id: string) => {
    setSessionId(id);
    localStorage.setItem('lab-active-session', id);
  }, []);

  // --- Optimistic update helpers ---

  const optimisticAddSkill = useCallback((skill: SkillDraft) => {
    setSkills(prev => [skill, ...prev]);
  }, []);

  const optimisticRemoveSkill = useCallback((assetId: string) => {
    setSkills(prev => prev.filter(s => s.assetId !== assetId));
  }, []);

  const optimisticAddSpecialtyAgent = useCallback((agent: SpecialtyAgentDraft) => {
    setSpecialtyAgents(prev => [agent, ...prev]);
  }, []);

  const optimisticRemoveSpecialtyAgent = useCallback((assetId: string) => {
    setSpecialtyAgents(prev => prev.filter(a => a.assetId !== assetId));
  }, []);

  const optimisticAddToolCollection = useCallback((collection: ToolCollectionDraft) => {
    setToolCollections(prev => [collection, ...prev]);
  }, []);

  const optimisticRemoveToolCollection = useCallback((assetId: string) => {
    setToolCollections(prev => prev.filter(c => c.assetId !== assetId));
  }, []);

  const optimisticAddWorkflow = useCallback((workflow: WorkflowDraft) => {
    setWorkflows(prev => [workflow, ...prev]);
  }, []);

  const optimisticRemoveWorkflow = useCallback((assetId: string) => {
    setWorkflows(prev => prev.filter(w => w.assetId !== assetId));
  }, []);

  // --- Context value ---

  const value: LabContextValue = {
    skills,
    specialtyAgents,
    toolCollections,
    workflows,
    publishedSkills,
    publishedSpecialtyAgents,
    publishedWorkflows,
    builtInAgents,
    builtInSkills,
    loadingCategories,
    loadedCategories,
    categoryErrors,
    ensureCategoryLoaded,
    sessionId,
    resetSession,
    switchSession,
    rightPanelState,
    setRightPanelState,
    harnessTarget,
    setHarnessTarget,
    readinessTarget,
    setReadinessTarget,
    editingTarget,
    setEditingTarget,
    refreshCategory,
    refreshAll,
    optimisticAddSkill,
    optimisticRemoveSkill,
    optimisticAddSpecialtyAgent,
    optimisticRemoveSpecialtyAgent,
    optimisticAddToolCollection,
    optimisticRemoveToolCollection,
    optimisticAddWorkflow,
    optimisticRemoveWorkflow,
  };

  return <LabContext.Provider value={value}>{children}</LabContext.Provider>;
}

// --- Hook ---

export function useLabContext(): LabContextValue {
  const ctx = useContext(LabContext);
  if (!ctx) {
    throw new Error('useLabContext must be used within a LabContextProvider');
  }
  return ctx;
}
