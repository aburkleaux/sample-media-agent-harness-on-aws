/**
 * The Improve center surface. Its content is what it cost, whether the work landed, and the
 * recommendations — in that order, and short enough to read at once.
 *
 * What is NOT on it is the point. The engine's own reading of the same facts — the reader's runs
 * with the judge's sentence, their repeated work, the population's quality — is behind the
 * **Advanced** link, together with the component registry; the loop's discarded findings are behind
 * an admin one. Each of those blocks is true and each qualification in them is needed, which is
 * exactly why they cannot lead: a reader who has to get through fourteen caveats to reach a
 * recommendation does not reach it. The default view answers in two figures and one sentence and
 * puts the recommendations above the fold.
 *
 * Load-state handling copies `HooksPanel`: `loaded` gates skeleton-vs-content while
 * `loading` gates only the "Refreshing…" indicator, and `error` is kept separate from
 * `mutationError`. The consequence is the point, a background refetch never blanks the
 * pane, and a failed refetch shows a banner OVER stale-but-present data instead of
 * replacing it with an error page.
 *
 * Data comes from `improve-data`, which fetches the payload the loop generates. There is no
 * fabricated fallback: if the payload is absent the error banner says how to generate it, because
 * a screen that silently substitutes invented data hides its own failure.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import { useIsGatewayAdmin } from '../../auth/isGatewayAdmin';
import { RefreshButton } from '../settings/panelChrome';
import type { PatternsResponse, Suggestion } from '../../improve-api';
import {
  dismissSuggestion, loadImproveData, peekImproveData, undoDismiss, type ImproveData,
} from '../../improve-data';
import { ImpactSummary } from './ImpactSummary';
import { ImproveComponents } from './ImproveComponents';
import { ImproveInbox } from './ImproveInbox';
import { MetricBlock } from './MetricBlock';
import { OutcomeBand } from './OutcomeBand';
import { ModelCompare } from './ModelCompare';
import { QualityBlock } from './QualityBlock';
import { RunsBlock } from './RunsBlock';
import { WasteBlock } from './WasteBlock';
import {
  improveScrollStyle, improveSurfaceStyle, metaTextStyle, pluralise,
} from './improveChrome';
export type ImproveView = 'inbox' | 'components' | 'findings';

/** User surfaces that can be viewed alone. `findings` is admin-only and excluded. */
export const VARIANTS = ['inbox', 'components'] as const;
export type Variant = (typeof VARIANTS)[number];

/**
 * Read `?improve=inbox|components`: PINS that surface, with no link off it.
 *
 * A review aid for looking at one surface as it really is, and the pin is the whole of it — a
 * reader who arrives on such a URL cannot wander to the other surface and back. The two query
 * values are in links already in use, so they do not move.
 *
 * Nothing depends on it, so it can be deleted whenever it stops being useful.
 */
export function readSoloVariant(search: string = window.location.search): Variant | null {
  const requested = new URLSearchParams(search).get('improve');
  return (VARIANTS as readonly string[]).includes(requested ?? '')
    ? (requested as Variant)
    : null;
}

export type { ImproveData };

/**
 * The surfaces reachable from the header, in the order they appear there. `inbox` is not among
 * them: it is what the surface shows, so it needs no link to it.
 *
 * Labels only. The keys are `?improve=` values and `data-testid` suffixes, so they do not move —
 * which is why the label for `components` is free to say `Advanced` instead.
 *
 * `components` is the operator's view: the three engine blocks — the reader's own runs and their
 * repeated work, then the population's quality — followed by a table of every registry component
 * with its health. All of it is useful to whoever maintains the harness and to nobody else, so a
 * link keeps it reachable without it standing beside the suggestions as an equal. The key is
 * unchanged because it is a `?improve=` value and a `data-testid` suffix; only the label is free.
 */
const SECONDARY_VIEWS: { key: ImproveView; label: string; adminOnly?: boolean }[] = [
  { key: 'components', label: 'Advanced' },
  { key: 'findings', label: 'Findings', adminOnly: true },
];

/**
 * Module-scope so the default `load` has a STABLE identity.
 *
 * Written inline as a default parameter (`load = () => loadImproveData()`) it was a new
 * function on every render, which flowed into `useCallback(..., [load])` and then
 * `useEffect(..., [refresh])` — so the effect re-fired on every render and `refresh`'s own
 * setState woke it again, ~8 requests/second forever. See
 * `docs/incident-2026-09-01-improve-render-loop.md`; regression test in
 * `ImproveCenter.refetchLoop.test.tsx`, which needs a DELAYED promise to see it.
 */
const defaultLoad = (): Promise<ImproveData> => loadImproveData();

/**
 * How many suggestions existed before the producer trimmed the list to fit one DynamoDB item.
 *
 * `suggestionsTotal` is on the wire — the producer stamps it beside `suggestions` for exactly this
 * reason, so that `len(suggestions) < suggestionsTotal` is the trace of the trim — but `ImproveData`
 * does not declare it, and that file is not this change's to edit. So the key is read structurally
 * and validated here rather than trusted.
 *
 * The validation is not merely a stand-in for the missing declaration: even declared, the key is
 * absent from every bundle in staging today, so a reader must handle absence anyway. Anything that is
 * not a finite non-negative number returns `undefined`, which the inbox renders as nothing at all —
 * "nothing counted the total" must not become "nothing was dropped".
 */
function readSuggestionsTotal(data: ImproveData): number | undefined {
  // Checked at runtime although the type declares a number: this arrives as fetched JSON that
  // nothing validates on the way in, and a count that is not a count must read as absent rather
  // than reach the trim comparison.
  const total: unknown = data.suggestionsTotal;
  return typeof total === 'number' && Number.isFinite(total) && total >= 0 ? total : undefined;
}

export function ImproveCenter({
  load = defaultLoad,
  dismiss = dismissSuggestion,
  undo = undoDismiss,
  solo,
}: {
  /** Injected in tests so load states can be exercised without timers. */
  load?: () => Promise<ImproveData>;
  /** Dismiss write. Injected in tests so the optimistic move can be exercised without the
   *  network (the default hits the authenticated proxy). */
  dismiss?: (suggestionId: string, reason?: string, note?: string) => Promise<void>;
  /** Undo write, the DELETE counterpart of `dismiss`. */
  undo?: (suggestionId: string) => Promise<void>;
  /** Injected in tests; defaults to the URL. */
  solo?: Variant | null;
}) {
  const isAdmin = useIsGatewayAdmin();
  const soloVariant = solo !== undefined ? solo : readSoloVariant();
  const [view, setView] = React.useState<ImproveView>(soloVariant ?? 'inbox');
  // Pinned wins over the local view, so nothing can navigate out of a soloed variant.
  const active: ImproveView = soloVariant ?? view;
  const initial = peekImproveData();
  const [data, setData] = React.useState<ImproveData | null>(initial ?? null);
  const [loaded, setLoaded] = React.useState(initial !== undefined);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  // Kept separate from `error` (a failed load): a failed write must not blank the pane, so it
  // shows over the content the user is still looking at, the same split as `HooksPanel`.
  const [mutationError, setMutationError] = React.useState<string | null>(null);
  const [targetFilter, setTargetFilter] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await load();
      setData(next);
      setLoaded(true);
    } catch (err: unknown) {
      // The banner shows over whatever is already rendered; `loaded` is untouched, so a
      // failed refresh does not discard data the user is reading.
      setError(err instanceof Error ? err.message : 'Could not load the agent report.');
    } finally {
      setLoading(false);
    }
  }, [load]);

  React.useEffect(() => { void refresh(); }, [refresh]);

  // Dismiss/undo are wired to the store (see the props passed to ImproveInbox). The rest —
  // publish, send-to-team, try-both — still have no write path, so this says so rather than
  // pretending the action took effect.
  const onAction = React.useCallback((action: string, suggestion: Suggestion) => {
    setNotice(`“${action}” is not wired yet: nothing stores a decision. `
      + `Target: ${suggestion.targetId}`);
  }, []);

  // A successful dismiss/undo must outlive the inbox's own optimistic move, which is per-mount:
  // following a link and coming back remounts ImproveInbox and empties its overlay, so the stored
  // decision has to live in `data` — the server truth the overlay sits on top of — or the row
  // springs back to open until a manual Refresh. Patch only on success; a rejected write leaves
  // `data` untouched and the inbox reverts its own optimistic move (see ImproveInbox).
  const setSuggestionStatus = React.useCallback(
    (suggestionId: string, status: Suggestion['status']) => {
      setData((prev) => (prev
        ? {
          ...prev,
          suggestions: prev.suggestions.map((s) => (
            s.suggestionId === suggestionId ? { ...s, status } : s)),
        }
        : prev));
    }, []);

  const handleDismiss = React.useCallback(async (s: Suggestion) => {
    await dismiss(s.suggestionId);
    setSuggestionStatus(s.suggestionId, 'dismissed');
  }, [dismiss, setSuggestionStatus]);

  const handleUndoDismiss = React.useCallback(async (s: Suggestion) => {
    await undo(s.suggestionId);
    setSuggestionStatus(s.suggestionId, 'open');
  }, [undo, setSuggestionStatus]);

  const links = SECONDARY_VIEWS.filter((entry) => !entry.adminOnly || isAdmin);

  return (
    <div style={improveSurfaceStyle} data-testid="improve-center">
      <header style={{
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
        padding: '12px 16px 8px',
      }}>
        <h2 style={{
          margin: 0, fontSize: theme.typography.sizes.pageTitle,
          fontWeight: theme.typography.weights.semibold,
        }}>
          Agent report
        </h2>
        {soloVariant && (
          <span
            data-testid="solo-banner"
            style={{
              ...metaTextStyle,
              color: theme.colors.textSecondary,
              border: `1px dashed ${theme.colors.borderStrong}`,
              borderRadius: theme.radius.button,
              padding: '2px 8px',
            }}
          >
            variant <strong>{soloVariant}</strong> only
          </span>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12 }}>
          {loaded && loading && (
            <span role="status" style={metaTextStyle}>Refreshing…</span>
          )}
          <RefreshButton onClick={() => void refresh()} loading={loading} />
          {/* Absent while a variant is pinned: that mode exists to show one surface with no way
              off it. */}
          {!soloVariant && links.map((entry) => {
            const isCurrent = entry.key === active;
            return (
              <button
                key={entry.key}
                type="button"
                // Toggles, so the link the reader followed is also the way back.
                onClick={() => setView(isCurrent ? 'inbox' : entry.key)}
                aria-pressed={isCurrent}
                data-testid={`view-${entry.key}`}
                style={{
                  fontSize: theme.typography.sizes.meta,
                  padding: 0,
                  border: 'none',
                  background: 'none',
                  color: isCurrent ? theme.colors.textPrimary : theme.colors.textSecondary,
                  fontWeight: isCurrent
                    ? theme.typography.weights.semibold : theme.typography.weights.regular,
                  textDecoration: 'underline',
                  textUnderlineOffset: 3,
                  cursor: 'pointer',
                }}
              >
                {entry.label}
              </button>
            );
          })}
        </div>
      </header>

      <div style={{ padding: '0 16px' }}>
        {error && <Banner kind="error" testId="error-banner" message={error} role="alert" />}
        {mutationError && (
          <Banner kind="error" testId="mutation-error-banner" message={mutationError} role="alert"
            onDismiss={() => setMutationError(null)} />
        )}
        {notice && (
          <Banner kind="info" testId="notice-banner" message={notice} role="status"
            onDismiss={() => setNotice(null)} />
        )}
      </div>

      <div style={improveScrollStyle}>
        {!loaded && loading && (
          <div data-testid="improve-skeleton" style={{ ...metaTextStyle, padding: '18px 4px' }}>
            Loading the agent report…
          </div>
        )}

        {!loaded && !loading && error && (
          <div style={{ ...metaTextStyle, padding: '18px 4px' }}>
            Nothing loaded. Use Refresh to try again.
          </div>
        )}

        {loaded && data && (
          <>
            {active === 'inbox' && (
              <>
                {/*
                  Population figures head the Inbox, and only the Inbox: they are sums over the
                  whole harvest, so above the per-component triage they would read as claims about
                  the components listed there.

                  The rule and the gap are this pane's, because the block carries no framing of its
                  own. Deliberately NOT `cardStyle` — on this surface that IS the suggestion-row
                  treatment, so an identical bordered box directly above the list would read as the
                  first suggestion rather than as a summary of the population behind it.

                  `collapse` for the case where a STRUCTURAL cause takes every tile out at once —
                  the reader has no bundle of their own, or the bundle predates the scorecard. Then
                  the band is one sentence naming that cause, rather than vanishing (`hide`,
                  indistinguishable from a render failure) or repeating the same cause under three
                  empty tiles (`tiles`). The deployed producer DOES emit a scorecard, so this is the
                  absent-actor case far more often than the absent-scorecard one.

                  `data` is passed whole and unmemoised. A successful dismiss replaces it with a new
                  object, so the block is handed a fresh reference — which is a re-render, not a
                  remount, and keeps the selection and the open picker it holds in state. Keying or
                  re-wrapping it per payload would discard both mid-read.
                */}
                <div style={metricBandStyle}>
                  <MetricBlock data={data} structuralFallback="collapse" />
                </div>
                {/*
                  WHETHER the work landed, and how much time went into doing it twice — directly
                  under what it cost, and stated once in a sentence beneath the two figures.

                  Here rather than lower because it is the question the surface's title asks and
                  everything above this line is a spend term: a report that says what an agent cost
                  and never whether it worked is not a report on the agent.

                  Both read the SHARED keys — `scorecard`, `waste`, `history` — and never
                  `data.actor`. A per-actor bundle exists only for someone who had a session in the
                  harvest window, so a band built on one is empty for most readers most days, and an
                  empty band is worse than none. The per-actor reading of the same facts is behind
                  Advanced, where its four-way absence wording belongs.

                  One band for the tiles and the sentence together: the sentence restates the two
                  figures directly above it, so a rule between them would present it as a third
                  claim.
                */}
                <div style={metricBandStyle}>
                  {/* Every metric, per model, side by side (2026-10-17). A snapshot written before the
                      per-model rollup has no `byModelScores`, and gets the old harness-wide band. */}
                  {data.scorecard?.byModelScores?.length ? (
                    <ModelCompare models={data.scorecard.byModelScores} />
                  ) : (
                    <>
                      <OutcomeBand
                        scorecard={data.scorecard}
                        waste={data.waste}
                        history={data.history}
                      />
                      <div style={{ marginTop: 8 }}>
                        <ImpactSummary scorecard={data.scorecard} waste={data.waste} />
                      </div>
                    </>
                  )}
                </div>
                <ImproveInbox
                  suggestions={data.suggestions}
                  suggestionsTotal={readSuggestionsTotal(data)}
                  // The reader's OWN diverted failures, so a row can name the run it was. The
                  // shared bundle's copies carry no session id — see `ImproveInbox`'s props.
                  unexplained={data.actor?.patterns.residue}
                  environment={data.actor?.patterns.environment}
                  unattributed={data.patterns.unattributed}
                  evidence={data.actor?.evidence}
                  targetFilter={targetFilter}
                  onClearFilter={() => setTargetFilter(null)}
                  onAction={onAction}
                  onDismiss={handleDismiss}
                  onUndoDismiss={handleUndoDismiss}
                  onMutationError={setMutationError}
                />
              </>
            )}
            {active === 'components' && (
              <>
                {/*
                  The engine's own reading of the same three questions, for an operator.

                  Unchanged, and not a rewrite: every qualification in these blocks is true and each
                  one is another technical sentence, which is precisely why they are here rather than
                  on the surface a freelance editor opens. They speak the harness's vocabulary —
                  tenants, gating checks, harvest windows, reconstructed timings — and they answer
                  about the reader's OWN runs, which is the reading that has to state four different
                  kinds of absence to be honest.

                  Order preserved from where they used to sit, and it is still meaningful: the two
                  personal bands first, then the population's, so the scope changes exactly once and a
                  rate mixing every tenant's runs is never read as the reader's own.

                  `data.actor` is passed whole and possibly undefined to the first two; `RunsBlock` is
                  the only place a reader can see the judge's sentence about a run of theirs, because
                  that text is on their own bundle and on no shared item.
                */}
                <div style={metricBandStyle}>
                  <RunsBlock actor={data.actor} />
                </div>
                <div style={metricBandStyle}>
                  <WasteBlock actor={data.actor} />
                </div>
                {/*
                  `data.scorecard` and nothing else. It holds no per-run row and no judge text by
                  design — one global DynamoDB item is read by every actor — so there is nothing here
                  that could leak between tenants.
                */}
                <div style={metricBandStyle}>
                  <QualityBlock scorecard={data.scorecard} />
                </div>
                <ImproveComponents
                  data={data.components}
                  suggestions={data.suggestions}
                />
              </>
            )}
            {active === 'findings' && isAdmin && <Findings patterns={data.patterns} />}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Admin view of everything the loop produced but could not turn into a suggestion. This is the
 * surface that keeps it honest: it shows the patterns that were thrown away and names the
 * passing run that killed each one.
 *
 * Not all five buckets are rejections, and conflating them would be the mistake this view
 * exists to prevent. `residue` failed discrimination, `evidenceGaps` were never observable and
 * `environment` was diverted to ops — but `unattributed` patterns PASSED mining and then found
 * no owner in the registry, which is a gap in the Σ map rather than a fact about the runs.
 */
function Findings({ patterns }: { patterns: PatternsResponse }) {
  return (
    <div>
      <h3 style={sectionTitleStyle}>
        Admitted patterns (<span style={numericStyle}>{patterns.admitted.length}</span>)
      </h3>
      {patterns.admitted.map((pattern) => (
        <div key={pattern.patternId} style={{ ...metaTextStyle, marginBottom: 8 }}>
          <code style={numericStyle}>{pattern.patternId}</code>, {pattern.shape}
          {' · pass_hits '}<span style={numericStyle}>{pattern.passHits}</span>
          {' · '}<span style={numericStyle}>{pattern.runCount}</span>
          {' '}{pluralise(pattern.runCount, 'run')}
        </div>
      ))}

      {/*
        Of those admitted patterns, the ones nothing owns — placed here because that is the
        claim it qualifies: "1 admitted" alone implies the loop got somewhere with all of them.
        UNLIKE `evidenceGaps` and `environment` below, this section is NOT conditional on having
        rows, and the difference is deliberate. Those two are buckets a healthy population
        genuinely does not fill, so their absence reads correctly as "nothing was diverted".
        This one is a COVERAGE claim about the section above it. Hidden at zero, "every admitted
        pattern found an owner" would look identical to "this bundle predates the key", and
        identical again to a render failure, which is what a blank region reads as
        (`AGENTS.md`: absent ≠ empty). So all three states say in words which one they are.
      */}
      <h3 style={sectionTitleStyle}>
        Admitted, not yet traced to a component (
        <span style={numericStyle}>{patterns.unattributed?.length ?? 0}</span>)
      </h3>
      <p style={{ ...metaTextStyle, margin: '0 0 8px' }}>
        These patterns DID separate failing runs from passing ones, so they are real. What is
        missing is the trace to a component: no Σ component could be attributed, so no suggestion
        was written. Each one names the facets that are not yet traced, which is what a decision to
        map or refuse them rests on.
      </p>
      {patterns.unattributed === undefined ? (
        <div data-testid="unattributed-absent" style={{ ...metaTextStyle, marginBottom: 8 }}>
          Not in this bundle. The pipeline run that produced it predates this record, so
          whether any pattern went untraced is unknown here — not zero. The next run will say.
        </div>
      ) : patterns.unattributed.length === 0 ? (
        <div data-testid="unattributed-none" style={{ ...metaTextStyle, marginBottom: 8 }}>
          None: every admitted pattern was attributed to a component.
        </div>
      ) : (
        /*
          Keyed by POSITION, unlike every other bucket here, because this record has no identity
          on the wire. Mining groups by (symptom, shape) and only `shape` travels, so two
          entries with the SAME shape and different symptoms are normal — the real harvest emits
          exactly that pair — and keying on shape would collide. Same treatment as the
          `refinementLog` list below, which is also ordered with no id.
        */
        patterns.unattributed.map((entry, index) => (
          <div
            key={index}
            data-testid={`unattributed-${index}`}
            style={{
              ...metaTextStyle, marginBottom: 8, padding: '8px 10px',
              border: `1px dashed ${theme.colors.borderStrong}`,
              borderRadius: theme.radius.button,
            }}
          >
            <div>
              <code style={numericStyle}>{entry.shapeFacets.join(' AND ') || '(none)'}</code>
              {', '}<span style={numericStyle}>{entry.runCount}</span>
              {' '}{pluralise(entry.runCount, 'run')}
            </div>
            {/* The engine's `reason`, jargon and all. There is no plainReason to prefer: a
                rewrite belongs on the server or the two drift about what the failure means. */}
            <div style={{ marginTop: 4, color: theme.colors.textTertiary }}>{entry.reason}</div>
          </div>
        ))
      )}

      {/*
        The three diverted buckets below keep the engine's REASON and lose the run ids. Each row
        used to carry its failing session id, the ids of the passing runs that disqualified its
        shape, and its refinement log, and all three are one tenant's session data on an item every
        actor reads. The reason stayed because it states counts rather than ids — two of `mine`'s
        constructions used to interpolate the pass-hit ids and no longer do, since the same object
        already carried them on `pass_hit_runs`. The refinement log did not stay: it names the
        control set and the tools a gateway reported failing, which can come from a user-registered
        MCP server.

        So a row is its shape, its counts and its reason, keyed by position because it has no
        identity on the wire — the same treatment `unattributed` above already gets, for the same
        reason. The LENGTH of each list is still the count. The reader's own rows, with their run
        ids, are on their own bundle and render in the Inbox.
      */}
      <h3 style={sectionTitleStyle}>
        Rejected as non-discriminative (
        <span style={numericStyle}>{patterns.residue.length}</span>)
      </h3>
      <p style={{ ...metaTextStyle, margin: '0 0 8px' }}>
        A pattern that also fires on a passing run cannot explain a failure, so it is
        discarded. Each entry names the shape that was tried and how many passing runs it also
        matched.
      </p>
      {patterns.residue.map((entry, index) => (
        <div
          key={index}
          data-testid={`residue-${index}`}
          style={{
            ...metaTextStyle, marginBottom: 8, padding: '8px 10px',
            border: `1px dashed ${theme.colors.borderStrong}`,
            borderRadius: theme.radius.button,
          }}
        >
          <div>
            best attempt{' '}
            <code style={numericStyle}>{entry.bestShape.join(' AND ') || '(none)'}</code>
            {', also matched '}
            <span style={numericStyle}>{entry.passHits}</span>
            {' passing '}{pluralise(entry.passHits, 'run')}
          </div>
          <div style={{ marginTop: 4, color: theme.colors.textTertiary }}>{entry.reason}</div>
        </div>
      ))}

      {patterns.environment.length > 0 && (
        <>
          <h3 style={sectionTitleStyle}>
            Platform degraded (
            <span style={numericStyle}>{patterns.environment.length}</span>)
          </h3>
          <p style={{ ...metaTextStyle, margin: '0 0 8px' }}>
            Failures diverted because the platform itself was alarming over the run's window.
            Attribution stood down: alarm state is account-wide, so it can support correlation
            (&ldquo;every failure that afternoon coincided with a degraded platform&rdquo;) and
            never per-run blame. These go to ops, not to the proposer and not to the defect
            register.
          </p>
          {patterns.environment.map((entry, index) => (
            <div
              key={index}
              data-testid={`environment-${index}`}
              style={{
                ...metaTextStyle, marginBottom: 8, padding: '8px 10px',
                border: `1px dashed ${theme.colors.borderStrong}`,
                borderRadius: theme.radius.button,
              }}
            >
              {entry.reason}
            </div>
          ))}
        </>
      )}

      {patterns.evidenceGaps.length > 0 && (
        <>
          <h3 style={sectionTitleStyle}>
            Evidence gaps (<span style={numericStyle}>{patterns.evidenceGaps.length}</span>)
          </h3>
          <p style={{ ...metaTextStyle, margin: '0 0 8px' }}>
            Runs we could not observe well enough to judge. These are observability defects,
            not behaviours to change.
          </p>
          {patterns.evidenceGaps.map((entry, index) => (
            <div key={index} data-testid={`evidence-gap-${index}`}
              style={{ ...metaTextStyle, marginBottom: 5 }}>
              {entry.reason}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/**
 * The framing for each summary block, which the blocks themselves deliberately have none of.
 *
 * A rule and a gap, not a card: the summary is a different register from the list below it, and
 * the only other bordered box on this surface is a suggestion. It also has to hold when a block
 * collapses to a single grey sentence, which without the rule would read as a caption on the first
 * suggestion card rather than as the state of the whole population.
 *
 * Shared by every band on both views — the spend figures and the outcome figures here, the
 * operator's three blocks behind Advanced — so they read as one register rather than as competing
 * treatments of the same kind of claim.
 */
const metricBandStyle: React.CSSProperties = {
  padding: '10px 0',
  marginBottom: 10,
  borderBottom: `1px solid ${theme.colors.border}`,
};

const sectionTitleStyle: React.CSSProperties = {
  margin: '14px 0 6px',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
};

function Banner({
  kind, message, testId, role, onDismiss,
}: {
  kind: 'warning' | 'error' | 'info';
  message: string;
  testId: string;
  role?: 'alert' | 'status';
  onDismiss?: () => void;
}) {
  const colour = kind === 'error' ? theme.colors.error
    : kind === 'warning' ? theme.colors.warning : theme.colors.primary;
  return (
    <div
      role={role}
      data-testid={testId}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 8,
        ...metaTextStyle,
        color: theme.colors.textPrimary,
        background: tint(colour, 12),
        border: `1px solid ${tint(colour, 30)}`,
        borderRadius: theme.radius.button,
        padding: '7px 10px',
        marginBottom: 8,
      }}
    >
      <span>{message}</span>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss message"
          style={{
            marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer',
            color: theme.colors.textSecondary, padding: 0,
          }}
        >
          ✕
        </button>
      )}
    </div>
  );
}

export default ImproveCenter;
