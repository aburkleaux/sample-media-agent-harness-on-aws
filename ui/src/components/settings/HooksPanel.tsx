// HooksPanel.tsx — manage declarative harness hooks (Settings → Capabilities → Hooks).
//
// Self-contained: loads/mutates hooks directly via lab-api (no LabContext
// editing-target coupling). Shows built-in hooks read-only ("clone to
// customize") and the user's own hooks with create/edit/delete/enable. The
// editor validates the condition expression and offers a dry-run against a
// sample event (Task 10).
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { InventorySectionHeader } from './panelChrome';
import { theme } from '../../styles/tokens';
import {
  listHooks, peekHooks, createHook, updateHook, deleteHook, validateHookCondition,
  type HookAsset, type HookUpsert, type HookActionType,
} from '../../lab-api';
import CapabilitySwitch from './CapabilitySwitch';
import Button from '../ui/Button';
import InventoryRow, { InventoryRowList } from '../ui/InventoryRow';

const EVENTS = ['after_tool_result', 'before_tool', 'session_start', 'session_end'];
const ACTIONS: HookActionType[] = ['render', 'notify', 'dispatch'];

const DEFAULT_SAMPLE = JSON.stringify(
  { event: 'after_tool_result', tool: { role: 'results', name: 'x___get_diarization_results' }, result: { urls: ['https://cf/x.wav'] } },
  null, 2,
);

interface EditorState {
  assetId: string | null;
  name: string;
  event: string;
  condition: string;
  actionType: HookActionType;
  actionParamsText: string;
  priority: number;
  enabled: boolean;
}

const BLANK: EditorState = {
  assetId: null, name: '', event: 'after_tool_result', condition: '',
  actionType: 'notify', actionParamsText: '{\n  "message": "Hook fired"\n}',
  priority: 100, enabled: true,
};

export default function HooksPanel() {
  const [initialSnapshot] = useState(() => peekHooks());
  const [userHooks, setUserHooks] = useState<HookAsset[]>(initialSnapshot?.userHooks ?? []);
  const [builtinHooks, setBuiltinHooks] = useState<HookAsset[]>(initialSnapshot?.builtinHooks ?? []);
  const [loaded, setLoaded] = useState(initialSnapshot !== undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [search, setSearch] = useState('');
  const [showBuiltInHooks, setShowBuiltInHooks] = useState(false);
  const touchedHookIds = useRef(new Set<string>());
  const deletedHookIds = useRef(new Set<string>());

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const res = await listHooks(forceRefresh);
      setUserHooks(current => {
        const currentById = new Map(current.map(hook => [hook.assetId, hook]));
        const remote = (res.userHooks || [])
          .filter(hook => !deletedHookIds.current.has(hook.assetId))
          .map(hook => touchedHookIds.current.has(hook.assetId) ? (currentById.get(hook.assetId) ?? hook) : hook);
        const remoteIds = new Set(remote.map(hook => hook.assetId));
        const localOnly = current.filter(hook => touchedHookIds.current.has(hook.assetId) && !remoteIds.has(hook.assetId));
        return [...localOnly, ...remote];
      });
      setBuiltinHooks(res.builtinHooks || []);
      setLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load hooks');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const openCreate = () => setEditor({ ...BLANK });
  const openEdit = (h: HookAsset) => setEditor({
    assetId: h.assetId, name: h.name, event: String(h.event), condition: h.condition,
    actionType: h.action.type, actionParamsText: JSON.stringify(h.action.params ?? {}, null, 2),
    priority: h.priority, enabled: h.enabled,
  });
  const openClone = (h: HookAsset) => setEditor({
    assetId: null, name: `${h.name}-copy`, event: String(h.event), condition: h.condition,
    actionType: h.action.type, actionParamsText: JSON.stringify(h.action.params ?? {}, null, 2),
    priority: h.priority, enabled: h.enabled,
  });

  const setPending = (id: string, pending: boolean) => {
    setPendingIds(previous => {
      const next = new Set(previous);
      if (pending) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const onDelete = async (h: HookAsset) => {
    setMutationError(null);
    setPending(h.assetId, true);
    try {
      await deleteHook(h.assetId);
      deletedHookIds.current.add(h.assetId);
      touchedHookIds.current.delete(h.assetId);
      setUserHooks(previous => previous.filter(item => item.assetId !== h.assetId));
    } catch (err) {
      throw err;
    } finally {
      setPending(h.assetId, false);
    }
  };

  const onToggleEnabled = async (h: HookAsset) => {
    const enabled = !h.enabled;
    setMutationError(null);
    setPending(h.assetId, true);
    touchedHookIds.current.add(h.assetId);
    setUserHooks(previous => previous.map(item => item.assetId === h.assetId ? { ...item, enabled } : item));
    try {
      await updateHook(h.assetId, { enabled });
    } catch (err) {
      setUserHooks(previous => previous.map(item => item.assetId === h.assetId ? { ...item, enabled: h.enabled } : item));
      setMutationError(err instanceof Error ? err.message : 'Failed to update hook');
    } finally {
      setPending(h.assetId, false);
    }
  };

  // Built-in override: a user hook with the SAME name as a built-in replaces
  // it at runtime (see hooks/loader.build_engine). Disabling a built-in creates
  // a same-named user hook with enabled=false; re-enabling deletes that
  // override so the built-in applies again.
  const overrideFor = (h: HookAsset): HookAsset | undefined =>
    userHooks.find(u => u.name === h.name);

  const onDisableBuiltin = async (h: HookAsset) => {
    setMutationError(null);
    setPending(h.assetId, true);
    try {
      const created = await createHook({
        name: h.name, event: String(h.event), condition: h.condition,
        actionType: h.action.type, actionParams: h.action.params ?? {},
        priority: h.priority, enabled: false,
      });
      touchedHookIds.current.add(created.assetId);
      setUserHooks(previous => [{ ...h, assetId: created.assetId, builtin: false, enabled: false }, ...previous]);
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : 'Failed to disable built-in hook');
    } finally {
      setPending(h.assetId, false);
    }
  };

  const onEnableBuiltin = async (h: HookAsset) => {
    const override = overrideFor(h);
    if (!override) return;
    setMutationError(null);
    setPending(h.assetId, true);
    deletedHookIds.current.add(override.assetId);
    touchedHookIds.current.delete(override.assetId);
    setUserHooks(previous => previous.filter(item => item.assetId !== override.assetId));
    try {
      await deleteHook(override.assetId);
    } catch (err) {
      deletedHookIds.current.delete(override.assetId);
      touchedHookIds.current.add(override.assetId);
      setUserHooks(previous => [override, ...previous]);
      setMutationError(err instanceof Error ? err.message : 'Failed to enable built-in hook');
    } finally {
      setPending(h.assetId, false);
    }
  };

  // Hooks that override a built-in (same name) are surfaced via the built-in
  // row's effective state, so hide them from the "Yours" list to avoid a
  // confusing duplicate.
  const builtinNames = new Set(builtinHooks.map(b => b.name));
  const matchesSearch = useCallback((hook: HookAsset) => {
    const query = search.trim().toLowerCase();
    if (!query) return true;
    return [hook.name, String(hook.event), hook.action.type, hook.condition]
      .some(value => value.toLowerCase().includes(query));
  }, [search]);
  const yourHooks = useMemo(
    () => userHooks.filter(hook => !builtinNames.has(hook.name) && matchesSearch(hook)),
    [userHooks, builtinHooks, matchesSearch],
  );
  const visibleBuiltinHooks = useMemo(
    () => builtinHooks.filter(matchesSearch),
    [builtinHooks, matchesSearch],
  );

  const handleSaved = (saved: HookAsset) => {
    touchedHookIds.current.add(saved.assetId);
    deletedHookIds.current.delete(saved.assetId);
    setUserHooks(previous => {
      const exists = previous.some(item => item.assetId === saved.assetId);
      return exists
        ? previous.map(item => item.assetId === saved.assetId ? saved : item)
        : [saved, ...previous];
    });
    setEditor(null);
  };

  if (editor) {
    return (
      <HookEditor
        state={editor}
        onChange={setEditor}
        onCancel={() => setEditor(null)}
        onSaved={handleSaved}
      />
    );
  }

  return (
    <div>
      <div style={controlRowStyle}>
        <input
          value={search}
          onChange={event => setSearch(event.target.value)}
          placeholder="Search hooks..."
          aria-label="Search hooks"
          style={searchInputStyle}
        />
        <div style={controlActionsStyle}>
          <CapabilitySwitch
            checked={showBuiltInHooks}
            onChange={setShowBuiltInHooks}
            label="Built-in hooks"
          />
          <Button variant="primary" size="md" onClick={openCreate} aria-label="Create hook">+ Create</Button>
        </div>
      </div>

      {error && (
        <div style={errorBanner} role="alert">
          <span>{error}</span>
          <Button onClick={() => void load(true)} style={{ marginLeft: 12 }}>Retry</Button>
        </div>
      )}
      {mutationError && (
        <div style={errorBanner} role="alert">
          <span>{mutationError}</span>
          <Button onClick={() => setMutationError(null)} style={{ marginLeft: 12 }}>Dismiss</Button>
        </div>
      )}
      {loaded && loading && <div role="status" style={{ marginBottom: 8, color: theme.colors.textSecondary, fontSize: 'var(--fs-2xs)' }}>Refreshing hooks…</div>}

      {/* Structure mirrors the Tools body (CapabilitiesSettings ToolsTabBody):
          the "Yours (drafts)" section + its empty-state card render regardless
          of load/error state, so a failed fetch shows the error banner above
          AND the same section scaffold Tools shows — not a bare banner. */}
      <InventorySectionHeader label="Yours (drafts)" />
      {!loaded && loading ? (
        <HookInventorySkeleton />
      ) : yourHooks.length > 0 ? (
        <InventoryRowList>
          {yourHooks.map(h => (
            <HookRow key={h.assetId} hook={h} busy={pendingIds.has(h.assetId)}
              onEdit={() => openEdit(h)} onDelete={() => onDelete(h)} onToggle={() => onToggleEnabled(h)} />
          ))}
        </InventoryRowList>
      ) : (
        <Empty>{search.trim() ? 'No custom hooks match your search.' : 'No hooks yet. Create one to react to agent lifecycle events.'}</Empty>
      )}

      {loaded && showBuiltInHooks && (
        <>
          <InventorySectionHeader label="Built-in" />
          {visibleBuiltinHooks.length === 0 && <Empty>No built-in hooks match your search.</Empty>}
          <InventoryRowList>
            {visibleBuiltinHooks.map(h => {
              const ov = overrideFor(h);
              const effectiveEnabled = ov ? ov.enabled : true;
              return (
                <HookRow key={h.assetId} hook={h} builtin builtinEnabled={effectiveEnabled} busy={pendingIds.has(h.assetId)}
                  onClone={() => openClone(h)}
                  onToggleBuiltin={() => (effectiveEnabled ? onDisableBuiltin(h) : onEnableBuiltin(h))} />
              );
            })}
          </InventoryRowList>
        </>
      )}

    </div>
  );
}

/**
 * One hook row, on the same `InventoryRow` card as Skills, Agents and Tools.
 *
 * It was a hand-rolled hairline row (`padding: 11px 0` + a bottom border) with its
 * own badge and its own name/meta typography — the last surviving copy of the idiom
 * `InventoryRow` was written to replace. The rule the hook enforces takes the
 * DESCRIPTION slot rather than the muted meta line, because it is what the row is
 * actually about: a hook has no authored description, so "auto-renders on
 * tool_result when …" IS its description.
 *
 * The enable switch stays in the action cluster, last. It is the only control here
 * that acts without a confirm, so it sits furthest from Delete.
 */
function HookRow({ hook, builtin, builtinEnabled, busy = false, onEdit, onDelete, onToggle, onClone, onToggleBuiltin }: {
  hook: HookAsset; builtin?: boolean; builtinEnabled?: boolean; busy?: boolean;
  onEdit?: () => void; onDelete?: () => Promise<void> | void; onToggle?: () => void; onClone?: () => void; onToggleBuiltin?: () => void;
}) {
  const enabled = builtin ? (builtinEnabled ?? true) : hook.enabled;
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const confirmDeletion = async () => {
    if (!onDelete) return;
    setDeleteError(null);
    try {
      await onDelete();
      setConfirmDelete(false);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Delete failed');
    }
  };

  /* The hook's own rule, in prose. `condition` is a JMESPath-ish expression, shown
     only when there is one — an unconditional hook fires on every event of its type
     and "when true" would be noise. */
  const rule = `auto-${hook.action.type}s on ${hook.event}`;
  const description = hook.condition?.trim()
    ? `${rule} when ${hook.condition.trim()}`
    : rule;

  return (
    <InventoryRow
      name={hook.name}
      description={description}
      /* `built-in` is provenance; enabled/disabled is already spoken by the switch
         in the actions, so it is not repeated as a badge. */
      badge={builtin ? { label: 'built-in', variant: 'info' } : undefined}
      meta={<span className="numeric">priority {hook.priority}</span>}
      error={deleteError}
      onDismissError={deleteError ? () => setDeleteError(null) : undefined}
      actions={
        confirmDelete ? (
          <>
            <Button variant="danger" onClick={confirmDeletion} busy={busy} busyLabel="Deleting…" aria-label={`Confirm delete ${hook.name}`}>
              Confirm delete
            </Button>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={busy}>Cancel</Button>
          </>
        ) : (
          <>
            {!builtin && onEdit && (
              <Button onClick={onEdit} disabled={busy} aria-label={`Edit ${hook.name}`}>Edit</Button>
            )}
            {!builtin && onDelete && (
              <Button variant="danger" onClick={() => setConfirmDelete(true)} disabled={busy} aria-label={`Delete ${hook.name}`}>Delete</Button>
            )}
            {builtin && onClone && (
              <Button onClick={onClone} disabled={busy} aria-label={`Clone ${hook.name}`}>Clone</Button>
            )}
            <CapabilitySwitch
              checked={enabled}
              label={enabled ? 'Enabled' : 'Disabled'}
              ariaLabel={`${enabled ? 'Disable' : 'Enable'} ${hook.name}`}
              busy={busy}
              onChange={() => {
                if (builtin && onToggleBuiltin) onToggleBuiltin();
                else if (onToggle) onToggle();
              }}
            />
          </>
        )
      }
    />
  );
}

function HookEditor({ state, onChange, onCancel, onSaved }: {
  state: EditorState;
  onChange: (s: EditorState) => void;
  onCancel: () => void;
  onSaved: (saved: HookAsset) => void;
}) {
  const [validateMsg, setValidateMsg] = useState<string>('');
  const [sample, setSample] = useState<string>(DEFAULT_SAMPLE);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string>('');

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      onCancel();
    };
    window.addEventListener('keydown', handleEscape, true);
    return () => window.removeEventListener('keydown', handleEscape, true);
  }, [onCancel]);

  const runValidate = async () => {
    let sampleObj: Record<string, unknown> | undefined;
    try { sampleObj = sample.trim() ? JSON.parse(sample) : undefined; }
    catch { setValidateMsg('Sample event is not valid JSON'); return; }
    const res = await validateHookCondition(state.condition, sampleObj);
    if (!res.valid) setValidateMsg(`✗ invalid: ${res.error}`);
    else if (sampleObj) setValidateMsg(res.matched ? '✓ valid — matches sample' : '✓ valid — does NOT match sample');
    else setValidateMsg('✓ valid');
  };

  const onSave = async () => {
    setSaveErr('');
    let params: Record<string, unknown> = {};
    try { params = state.actionParamsText.trim() ? JSON.parse(state.actionParamsText) : {}; }
    catch { setSaveErr('Action params must be valid JSON'); return; }
    const payload: HookUpsert = {
      name: state.name, event: state.event, condition: state.condition,
      actionType: state.actionType, actionParams: params, priority: state.priority, enabled: state.enabled,
    };
    setSaving(true);
    try {
      let assetId = state.assetId;
      if (assetId) await updateHook(assetId, payload);
      else assetId = (await createHook(payload)).assetId;
      onSaved({
        assetId,
        name: state.name,
        event: state.event,
        condition: state.condition,
        action: { type: state.actionType, params },
        priority: state.priority,
        enabled: state.enabled,
        builtin: false,
      });
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const set = (patch: Partial<EditorState>) => onChange({ ...state, ...patch });

  return (
    <div style={editorOverlay} role="region" aria-label="Hook editor">
      <div style={editorCard}>
        <button autoFocus style={backBtn} onClick={onCancel}>← Back to Hooks</button>
        <h3 style={{ margin: '10px 0 12px', fontSize: 'var(--fs-lg)' }}>{state.assetId ? 'Edit hook' : 'Create hook'}</h3>

        <label style={lbl}>Name</label>
        <input style={inp} value={state.name} onChange={e => set({ name: e.target.value })} aria-label="Hook name" />

        <label style={lbl}>Event</label>
        <select style={inp} value={state.event} onChange={e => set({ event: e.target.value })} aria-label="Hook event">
          {EVENTS.map(ev => <option key={ev} value={ev}>{ev}</option>)}
        </select>

        <label style={lbl}>Condition (expression)</label>
        <textarea style={{ ...inp, fontFamily: 'var(--font-mono)', minHeight: 44 }} value={state.condition}
          onChange={e => set({ condition: e.target.value })} aria-label="Hook condition"
          placeholder='tool.role == "results" and result.urls' />

        <label style={lbl}>Action</label>
        <select style={inp} value={state.actionType} onChange={e => set({ actionType: e.target.value as HookActionType })} aria-label="Hook action type">
          {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
        </select>

        <label style={lbl}>Action params (JSON)</label>
        <textarea style={{ ...inp, fontFamily: 'var(--font-mono)', minHeight: 60 }} value={state.actionParamsText}
          onChange={e => set({ actionParamsText: e.target.value })} aria-label="Hook action params" />

        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <div style={{ flex: 1 }}>
            <label style={lbl}>Priority</label>
            <input style={inp} type="number" value={state.priority}
              onChange={e => set({ priority: parseInt(e.target.value || '100', 10) })} aria-label="Hook priority" />
          </div>
          <div style={{ marginTop: 18 }}>
            <CapabilitySwitch
              checked={state.enabled}
              onChange={enabled => set({ enabled })}
              label={state.enabled ? 'Enabled' : 'Disabled'}
              ariaLabel="Hook enabled"
            />
          </div>
        </div>

        <label style={lbl}>Dry-run — sample event (JSON)</label>
        <textarea style={{ ...inp, fontFamily: 'var(--font-mono)', minHeight: 60 }} value={sample}
          onChange={e => setSample(e.target.value)} aria-label="Dry-run sample event" />
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Button onClick={runValidate} aria-label="Validate condition">Validate / Dry-run</Button>
          {validateMsg && <span style={{ fontSize: 'var(--fs-2xs)', color: validateMsg.startsWith('✗') ? theme.colors.error : theme.colors.textSecondary }}>{validateMsg}</span>}
        </div>

        {saveErr && <div style={{ ...errorBanner, marginTop: 10 }}>{saveErr}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <Button variant="ghost" size="md" onClick={onCancel}>Cancel</Button>
          <Button variant="primary" size="md" onClick={onSave} disabled={!state.name} busy={saving} busyLabel="Saving…" aria-label="Save hook">
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}

function HookInventorySkeleton() {
  return (
    <div role="status" aria-label="Loading hooks" style={{ display: 'grid', gap: 8, padding: '12px 0' }}>
      {[0, 1, 2].map(index => (
        <div key={index} style={{ height: 44, borderRadius: theme.radius.button, background: theme.colors.surfaceSubtle, opacity: 1 - index * 0.18 }} />
      ))}
    </div>
  );
}

const Empty = ({ children }: { children: React.ReactNode }) => (
  // Bordered dashed card matching Tools' EmptyState (CapabilitiesSettings) so
  // the Tools and Hooks empty states read identically.
  <div style={{ padding: 24, border: `1px dashed ${theme.colors.border}`, borderRadius: theme.radius.panel, textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' }}>{children}</div>
);

const rowStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, padding: '11px 0', borderBottom: `1px solid ${theme.colors.border}` };
const badge: React.CSSProperties = { fontSize: 'var(--fs-3xs)', textTransform: 'uppercase', background: theme.colors.border, color: theme.colors.textSecondary, padding: '2px 7px', borderRadius: theme.radius.button, marginLeft: 8 };
const errorBanner: React.CSSProperties = { padding: '9px 12px', background: `color-mix(in srgb, ${theme.colors.error} 10%, transparent)`, color: theme.colors.error, borderRadius: theme.radius.button, fontSize: 'var(--fs-xs)', marginBottom: 12 };
const lbl: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary, margin: '10px 0 4px' };
const inp: React.CSSProperties = { width: '100%', boxSizing: 'border-box', padding: '8px 10px', fontSize: 'var(--fs-sm)', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, background: theme.colors.background, color: theme.colors.textPrimary };
const editorOverlay: React.CSSProperties = { width: '100%', minHeight: '100%' };
const editorCard: React.CSSProperties = { width: '100%', maxWidth: 760, boxSizing: 'border-box', paddingBottom: 24 };
const backBtn: React.CSSProperties = { background: 'none', border: 'none', padding: 0, color: theme.colors.primary, fontSize: 'var(--fs-sm)', fontWeight: 500, cursor: 'pointer' };

const controlRowStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 10, justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 };
const controlActionsStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' };
const searchInputStyle: React.CSSProperties = { padding: '8px 12px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, fontSize: 'var(--fs-sm)', width: 220, maxWidth: '100%', flex: '1 1 180px', outline: 'none', background: theme.colors.surface, color: theme.colors.textPrimary };
