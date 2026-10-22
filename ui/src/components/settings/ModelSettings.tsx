import React, { useState, useEffect } from 'react';
import { theme } from '../../styles/tokens';
import { fetchModelCatalog, setAgentModel } from '../../api';
import { safeSetItem } from '../../utils/safeStorage';
import Button from '../ui/Button';
import EmptyState from '../ui/EmptyState';
import InventoryRow, { InventoryRowList } from '../ui/InventoryRow';
import EffortSlider from '../ui/EffortSlider';
import { InventorySectionHeader } from './panelChrome';

interface ModelInfo {
  id: string;
  name: string;
  supportsEffort?: boolean;
}

export default function ModelSettings() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [search, setSearch] = useState('');
  const [selectedModel, setSelectedModel] = useState<string>(() =>
    localStorage.getItem('lab-orchestrator-model-id') || ''
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setCatalogLoaded(false);
    setError(null);
    // Deliberately NOT `async`: nothing in here may be awaited. `loading` is
    // cleared by the `.finally` below, which waits for whatever this callback
    // returns — so a single `await` on a runtime call holds the spinner for the
    // whole round trip. Measured in sandbox: `/models` answered in 92ms while an
    // awaited `setAgentModel` took 9,972ms on a cold session, so the tab looked
    // ~100x slower than the data it was waiting on. Keep runtime writes off this
    // path; dropping `async` makes that structural rather than a convention.
    fetchModelCatalog()
      .then(({ models: result, defaultModelId }) => {
        if (cancelled) return;
        setModels(result);
        setCatalogLoaded(true);
        const savedId = localStorage.getItem('lab-orchestrator-model-id') || '';
        const savedValid = savedId && result.some(m => m.id === savedId);
        if (savedValid) {
          setSelectedModel(savedId);
          // Reconciliation, not part of the read: the runtime holds the selected
          // model in memory and loses it on a container restart. Fire and forget
          // — the user's selection is already authoritative in localStorage and
          // re-applied on every change, so a failure here self-heals.
          void setAgentModel(savedId).catch(() => { /* best effort */ });
        } else if (defaultModelId && result.some(m => m.id === defaultModelId)) {
          // Adopt the PLATFORM default, never list position. `/models` is ordered
          // by `ListInferenceProfiles`, which returns Claude 3 Haiku first — a
          // 4096-output model. Picking `result[0]` meant merely opening this tab
          // on a fresh browser pinned the orchestrator to it, and every chat turn
          // then failed with `ValidationException ... exceeds the model limit of
          // 4096`. Observed live in sandbox.
          setSelectedModel(defaultModelId);
          safeSetItem('lab-orchestrator-model-id', defaultModelId);
          window.dispatchEvent(new Event('model-changed'));
        } else if (result.length > 0) {
          // No default reported (older proxy) — show something rather than an
          // empty control, but do NOT persist or announce it. Writing a guess is
          // what caused the incident above; leaving the runtime on its own
          // default is strictly safer than overriding it with list position.
          setSelectedModel(result[0].id);
        }
      })
      .catch(err => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load models');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

    /** Point the chat header at `modelId` (its own event + storage key). */
  const announceActiveModel = (modelId: string) => {
    safeSetItem('lab-orchestrator-model-active', modelId);
    window.dispatchEvent(new CustomEvent('orchestrator:model', { detail: { modelId } }));
  };

const handleModelChange = async (modelId: string) => {
    const prev = selectedModel;
    setSelectedModel(modelId);
    safeSetItem('lab-orchestrator-model-id', modelId);
    window.dispatchEvent(new Event('model-changed'));
    // The chat header listens for 'orchestrator:model' and reads
    // 'lab-orchestrator-model-active' -- a different event and a different key
    // from the two above, both written only by a COMPLETED turn. So selecting a
    // model left the header showing the previous one until a turn finished, and
    // if that turn failed it never updated at all. Announce the selection here
    // so the header follows the choice; a completed turn still overwrites it
    // with what actually ran, which is the authoritative value.
    announceActiveModel(modelId);
    setSwitching(true);

    try {
      await setAgentModel(modelId);
    } catch (err) {
      setSelectedModel(prev);
      safeSetItem('lab-orchestrator-model-id', prev);
      window.dispatchEvent(new Event('model-changed'));
      announceActiveModel(prev);  // roll the header back with the selection
      setError(`Failed to switch model: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setSwitching(false);
    }
  };

  /*
   * 52 inference profiles are visible in sandbox, so the model in USE is not a row
   * in the list at all: it is the card at the top — the answer to "what is running?",
   * which is why anyone opens this tab. The card is never filtered by the search, so
   * a query cannot hide the current model (and the effort control it owns) the way
   * sorting-it-first-into-the-filtered-list did. The rest keep the catalog's own
   * order (`ListInferenceProfiles`), which groups by provider and family.
   */
  const query = search.trim().toLowerCase();
  const current = models.find(m => m.id === selectedModel);
  const others = models.filter(m => m.id !== selectedModel);
  const visible = others
    .filter(m => !query || m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query));

  return (
    <div>
      <h2 style={{ margin: '0 0 4px', fontSize: 'var(--fs-lg)', fontWeight: 600, color: theme.colors.textPrimary }}>Model</h2>
      <p style={{ margin: '0 0 16px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary }}>
        Orchestrator inference profile.
      </p>

      {/* The current model, and the one setting that belongs to it. */}
      {current && (
        <section style={currentCardStyle} aria-label="Current model" data-testid="current-model">
          <div style={currentHeadStyle}>
            <div style={{ minWidth: 0 }}>
              <div style={currentEyebrowStyle}>
                <span style={liveDotStyle} aria-hidden="true" /> In use
              </div>
              <div style={currentNameStyle}>{current.name}</div>
              <div style={currentMetaStyle}>
                {extractProvider(current.name)} · <code style={modelIdCodeStyle}>{current.id}</code>
              </div>
            </div>
            <span style={currentBadgeStyle}>Current</span>
          </div>
          <EffortSlider supported={current.supportsEffort === true} />
        </section>
      )}

      {error && (
        <div style={errorStyle}>
          {error}
          <button onClick={() => setError(null)} style={dismissBtnStyle}>✕</button>
        </div>
      )}

      {/* ONE list, not a select plus a grid plus a detail card.
        *
        * The tab used to show the same information three times: a `<select>`, a
        * detail card repeating the selection, and a grid of clickable tiles. Now it
        * is the same `InventoryRow` list every other capability surface uses — the
        * current model carries a `Current` badge instead of being restated above,
        * and `Use` is an explicit action rather than a click anywhere on a tile.
        *
        * A tile-click was also the only affordance for a destructive-ish action
        * (switching the orchestrator mid-session), with no visible control to aim
        * at. `Use` names it. */}
      <input
        type="text"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Search models..."
        aria-label="Search models"
        style={searchInputStyle}
      />

      {/* The count follows the FILTER, not the catalog: a header reading 52 above
        * three rows is the kind of disagreement that reads as a broken list. */}
      <InventorySectionHeader
        label={`${current ? 'Other models' : 'Available models'}${models.length ? ` · ${visible.length}` : ''}`}
      />

      {loading ? (
        <div style={{ padding: 20, textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' }}>
          Loading available models...
        </div>
      ) : !catalogLoaded ? null : models.length === 0 ? (
        <EmptyState>No models are available to this environment.</EmptyState>
      ) : visible.length === 0 ? (
        <EmptyState>{query ? 'No models match your search.' : 'No other models are available.'}</EmptyState>
      ) : (
        <div style={modelGridStyle}>
          {visible.map(model => {
            return (
              <InventoryRow
                key={model.id}
                name={model.name}
                /* Provider and the exact inference-profile id. The id is what an
                   operator needs when a turn fails with a model-specific error, so it
                   stays visible rather than living behind a tooltip. */
                meta={
                  <>
                    {extractProvider(model.name)} ·{' '}
                    <code style={modelIdCodeStyle}>{model.id}</code>
                  </>
                }
                actions={
                  (
                    <Button
                      size="sm"
                      onClick={() => void handleModelChange(model.id)}
                      disabled={switching}
                      busy={switching}
                      busyLabel="Switching…"
                      aria-label={`Use ${model.name}`}
                    >
                      Use
                    </Button>
                  )
                }
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

export function extractProvider(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes('claude') || lower.includes('anthropic')) return 'Anthropic';
  if (lower.includes('openai') || lower.includes('gpt')) return 'OpenAI';
  if (lower.includes('nova') || lower.includes('amazon') || lower.includes('titan')) return 'Amazon';
  if (lower.includes('llama') || lower.includes('meta')) return 'Meta';
  if (lower.includes('mistral')) return 'Mistral';
  if (lower.includes('cohere')) return 'Cohere';
  if (lower.includes('ai21') || lower.includes('jamba')) return 'AI21';
  return 'Other';
}

/*
 * Two columns of the same card.
 *
 * The other inventories are one row per line because their rows carry a
 * description that needs the width. A model row carries a name, a provider and an
 * id — so a single column wasted half the pane and turned 52 profiles into a very
 * long scroll, while two columns halve it without changing the row anatomy.
 *
 * `minmax(0, 1fr)`, not `1fr`: a grid item's default `min-width: auto` is its
 * content, so a long inference-profile id would otherwise widen the column and push
 * the second one off the pane instead of wrapping.
 */
const modelGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
  gap: 8,
};

/* Same metrics as the Hooks and Skills search inputs — one search affordance across
   the capability surfaces, not three that look nearly alike. */
const searchInputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '8px 10px',
  marginBottom: 4,
  fontSize: 'var(--fs-xs)',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  outline: 'none',
};

/* The inference-profile id, in mono so a column of them lines up and so it reads as
   an identifier rather than prose. Small: it is reference material, not the label. */
const modelIdCodeStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontMono,
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textSecondary,
  // Breaks anywhere: ids have no spaces, so without this one long id widens its
  // grid column and the second column leaves the pane.
  overflowWrap: 'anywhere',
};

/* The current-model card: the one raised surface on the tab, tinted toward primary
   just enough to read as "selected" without competing with the Use buttons below. */
const currentCardStyle: React.CSSProperties = {
  marginBottom: 20,
  padding: '16px 18px',
  borderRadius: theme.radius.panel,
  border: `1px solid color-mix(in srgb, ${theme.colors.primary} 35%, ${theme.colors.border})`,
  background: `linear-gradient(180deg, color-mix(in srgb, ${theme.colors.primary} 7%, ${theme.colors.surfaceRaised}), ${theme.colors.surfaceRaised})`,
  boxShadow: theme.elevation.subtle,
};

const currentHeadStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12,
};

const currentEyebrowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6,
  fontSize: theme.typography.sizes.meta, fontWeight: theme.typography.weights.semibold,
  letterSpacing: '0.04em', textTransform: 'uppercase', color: theme.colors.textSecondary,
};

const liveDotStyle: React.CSSProperties = {
  width: 6, height: 6, borderRadius: theme.radius.dot, background: theme.colors.success,
  boxShadow: `0 0 0 3px color-mix(in srgb, ${theme.colors.success} 22%, transparent)`,
};

const currentNameStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.sectionTitle, fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary, lineHeight: 1.3,
};

const currentMetaStyle: React.CSSProperties = {
  marginTop: 4, fontSize: theme.typography.sizes.meta, color: theme.colors.textSecondary, overflowWrap: 'anywhere',
};

const currentBadgeStyle: React.CSSProperties = {
  flexShrink: 0, padding: '2px 8px', borderRadius: theme.radius.pill,
  fontSize: theme.typography.sizes.badge, fontWeight: theme.typography.weights.semibold,
  color: theme.colors.success,
  background: `color-mix(in srgb, ${theme.colors.success} 14%, transparent)`,
  border: `1px solid color-mix(in srgb, ${theme.colors.success} 35%, transparent)`,
};

const errorStyle: React.CSSProperties = {
  padding: '10px 14px', marginBottom: 16, borderRadius: theme.radius.button,
  background: `color-mix(in srgb, ${theme.colors.error} 10%, transparent)`, border: `1px solid ${theme.colors.error}`, color: theme.colors.error,
  fontSize: 'var(--fs-xs)', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
};

const dismissBtnStyle: React.CSSProperties = {
  background: 'none', border: 'none', cursor: 'pointer', color: theme.colors.error, fontSize: 'var(--fs-md)',
};
