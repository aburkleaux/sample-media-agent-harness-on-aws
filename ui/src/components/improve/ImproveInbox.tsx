/**
 * Inbox, the default Improve surface: open suggestions, then what the loop could NOT turn
 * into one.
 *
 * The sections below the list are the honest half. An inbox showing only what the miner
 * managed to explain implies coverage it does not have, so a failure that produced no
 * suggestion is listed, a failure the platform caused is listed separately, and a pattern we CAN
 * see but cannot yet attribute to any component is listed separately again. Those three are not
 * the same claim and must not share a heading.
 *
 * The first two come from the READER'S OWN bundle and the third from the shared one, and that
 * split is the store's, not a choice made here: a diverted row is one tenant's failing run, so the
 * shared bundle carries those rows without ids and only the owner's own copy can be identified to
 * them; an unowned pattern is a coverage claim about the harness and carries no ids at all.
 *
 * Each of them is hidden at zero, which is right for THIS surface and wrong for the admin
 * Findings tab, where the same buckets state absent / none / listed. The Inbox's rule is
 * specific recommendations only, so an empty section here is clutter that never becomes an
 * action; Findings is a coverage audit, where a section that vanishes is a claim about what
 * was checked.
 *
 * All of it arrives as props. It previously imported the two lists straight from `mockData`
 * while `ImproveCenter` loaded the same facts into `PatternsResponse` and handed them only to
 * the admin tab, two data paths for one truth, and the failure mode was silent: once the API
 * landed, this surface would have gone on rendering the mock forever, looking fine.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import type {
  PatternEvidence, ResidueEvidence, Suggestion, UnattributedPattern,
} from '../../improve-api';
import { SuggestionCard } from './SuggestionCard';
import { fractionLabel, metaTextStyle, pluralise } from './improveChrome';

/**
 * Optimistic-undo marker in the `handled` overlay. It must OVERRIDE a server-stored dismissal:
 * deleting the id from the map would let the suggestion's `status === 'dismissed'` keep the row in
 * the handled list, so undo would appear to do nothing. `effectiveHandled` treats this value as
 * "open — ignore the server status" until the next load reconciles it.
 */
const RESTORED = '__restored__';

/**
 * How a figure is set inside flowing prose, as against inside an identifier.
 *
 * Weight, and deliberately not `numericStyle`. That token is mono plus `tabular-nums`: the tabular
 * digits earn their place where figures line up down a column, and the mono face earns its place on an
 * identifier — a run id, a component id — where the reader is matching characters. A sentence is
 * neither. The trim notice below is the case that proves it: the span holds whatever `fractionLabel`
 * returns, which is `12 of 40`, so the word `of` came out monospace in the middle of a sentence and
 * read as a rendering fault.
 *
 * Semibold rather than nothing at all, so the count is still the part of the line a reader's eye lands
 * on first. `ImpactSummary` sets a figure in prose the same way, for the same reason; the two are
 * short enough to keep locally, and neither wants the other's tokens.
 */
const figureStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontFamily,
  fontWeight: theme.typography.weights.semibold,
};

export function ImproveInbox({
  suggestions,
  suggestionsTotal,
  unexplained = [],
  environment = [],
  unattributed = [],
  evidence,
  targetFilter,
  onClearFilter,
  onAction,
  onDismiss,
  onUndoDismiss,
  onMutationError,
}: {
  suggestions: Suggestion[];
  /**
   * How many publishable suggestions existed BEFORE the producer trimmed the list to fit one
   * DynamoDB item, which is the only trace that trim leaves: `suggestions.length < suggestionsTotal`
   * is a trim, and without the total a trimmed list is indistinguishable from a short one.
   *
   * OPTIONAL, and absent is the live state — the producer stamps it (`improve/dto.py`) but no bundle
   * in staging carries it yet. Absent means "nothing counted", NOT "nothing was trimmed", so it
   * renders nothing at all rather than a reassuring `12 of 12`.
   */
  suggestionsTotal?: number;
  /**
   * The READER'S OWN failures that no pattern could explain, `ActorBundle.patterns.residue`.
   *
   * From their bundle and not from the shared one, because the shared rows carry no session id —
   * a diverted row is one tenant's failing run, so the platform-wide list cannot identify a row to
   * anybody. So this section is absent for a reader with no bundle, which is right for this
   * surface: with nothing of theirs to show, a platform-wide count belongs on the admin Findings
   * tab and not in somebody's inbox.
   */
  unexplained?: ResidueEvidence[];
  /** The reader's own failures under a degraded platform, `ActorBundle.patterns.environment`. */
  environment?: ResidueEvidence[];
  /** Admitted patterns no Σ component owns, `PatternsResponse.unattributed`. Shared, because it is
   *  a coverage claim about the harness and carries no ids. Optional on the wire (a bundle can
   *  predate it), so `undefined` and `[]` both render nothing here — the distinction is drawn on
   *  the admin Findings tab, which is where it is actionable. */
  unattributed?: UnattributedPattern[];
  /** The reader's own evidence per suggestion id, `ActorBundle.evidence`. Passed straight to each
   *  card, which shows the facet detail for a run the reader owns and nothing where it has none. */
  evidence?: Record<string, PatternEvidence>;
  targetFilter?: string | null;
  onClearFilter?: () => void;
  /** Inert actions still under construction: publish, send-to-team, try-both. */
  onAction: (action: string, suggestion: Suggestion, candidateId?: string) => void;
  /** Real dismiss write. When present, Dismiss routes here instead of the inert `onAction`;
   *  the row moves optimistically and reverts if the write rejects. */
  onDismiss?: (suggestion: Suggestion) => Promise<void>;
  /** Real undo write, the DELETE counterpart of `onDismiss`. */
  onUndoDismiss?: (suggestion: Suggestion) => Promise<void>;
  /** Surface a failed dismiss/undo, so a rejected write is visible rather than silent. */
  onMutationError?: (message: string) => void;
}) {
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  // Optimistic overlay ON TOP OF each suggestion's server `status`, keyed by id. A value is the
  // action just taken locally ('dismiss' | 'publish' | 'send-to-team'), or RESTORED for an
  // optimistic undo; absent = follow the server. It is local, so it resets when this component
  // remounts (leaving and returning to the tab) — which is exactly why the lists below fall back
  // to the server status rather than trusting the overlay alone.
  const [handled, setHandled] = React.useState<Record<string, string>>({});

  // The row's handled label, combining the optimistic overlay with the server's stored status;
  // null means open. This is the single source both lists read from. Without the server fallback,
  // a dismissal stored on a prior session (`status === 'dismissed'`) would drop out of `open` yet
  // never enter `handledList` — the row would vanish with no undo path, though the DELETE route
  // that would revive it works.
  const effectiveHandled = React.useCallback(
    (s: Suggestion): string | null => {
      const overlay = handled[s.suggestionId];
      if (overlay === RESTORED) return null;              // optimistic undo beats server truth
      if (overlay) return overlay;                        // optimistic dismiss/publish/send
      return s.status === 'dismissed' ? 'dismiss' : null; // server truth (prior session)
    },
    [handled],
  );

  const visible = targetFilter
    ? suggestions.filter((s) => s.targetId === targetFilter)
    : suggestions;
  // `s.status === 'open'` still gates the open list, so a server terminal state we do not model
  // (accepted / superseded) is not resurrected into it; a RESTORED row rejoins open explicitly.
  const open = visible.filter((s) => !effectiveHandled(s)
    && (s.status === 'open' || handled[s.suggestionId] === RESTORED));
  const handledList = visible.filter((s) => effectiveHandled(s) !== null);

  // A review surface with no state transition has no exit: the count never moves and the
  // user cannot tell they finished. Dismiss is a real write now, so it moves the row
  // optimistically and reverts if the write fails; publish/send-to-team are still inert and
  // only record the intent locally, reconciled by the next load.
  const recordAction = React.useCallback(
    (action: string, suggestion: Suggestion, candidateId?: string) => {
      const id = suggestion.suggestionId;
      if (action === 'dismiss' && onDismiss) {
        setHandled((prev) => ({ ...prev, [id]: action }));
        setExpandedId(null);
        onDismiss(suggestion).catch((err: unknown) => {
          // The write failed, so the suggestion is still open: put the row back and say so,
          // rather than leaving it in "handled" as if the dismissal stuck.
          setHandled(({ [id]: _removed, ...rest }) => rest);
          onMutationError?.(errorMessage(err, `Could not dismiss “${suggestion.targetName}”.`));
        });
        return;
      }
      onAction(action, suggestion, candidateId);
      if (action === 'dismiss' || action === 'publish' || action === 'send-to-team') {
        setHandled((prev) => ({ ...prev, [id]: action }));
        setExpandedId(null);
      }
    },
    [onAction, onDismiss, onMutationError],
  );

  // Undo moves the row back to open optimistically by marking it RESTORED — which overrides a
  // server-stored dismissal too, so a row dismissed on a previous session actually moves. The
  // decision to retract comes from `effectiveHandled`, not the local overlay, or a persisted
  // dismissal (no overlay entry) would never fire its DELETE. A purely local move
  // (publish/send-to-team) has nothing on the server, so undoing it is only the local flip.
  const undoHandled = React.useCallback(
    (suggestion: Suggestion) => {
      const id = suggestion.suggestionId;
      const action = effectiveHandled(suggestion);
      setHandled((prev) => ({ ...prev, [id]: RESTORED }));
      if (action === 'dismiss' && onUndoDismiss) {
        onUndoDismiss(suggestion).catch((err: unknown) => {
          // The undo failed, so the dismissal still stands: move the row back to handled.
          setHandled((prev) => ({ ...prev, [id]: 'dismiss' }));
          onMutationError?.(errorMessage(err, `Could not undo “${suggestion.targetName}”.`));
        });
      }
    },
    [effectiveHandled, onUndoDismiss, onMutationError],
  );

  return (
    <div>
      {targetFilter && (
        <div style={{
          ...metaTextStyle, display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 9px', marginBottom: 10,
          background: tint(theme.colors.primary, 10),
          border: `1px solid ${tint(theme.colors.primary, 26)}`,
          borderRadius: theme.radius.button,
        }}>
          <span>
            Filtered to <code style={numericStyle}>{targetFilter}</code>
          </span>
          <button
            type="button"
            onClick={onClearFilter}
            data-testid="clear-filter"
            style={{
              background: 'none', border: 'none', padding: 0, cursor: 'pointer',
              color: theme.colors.primary, fontSize: theme.typography.sizes.meta,
              marginLeft: 'auto',
            }}
          >
            clear
          </button>
        </div>
      )}

      {/*
        The trim, whenever there was one.

        Only when SHORT: `12 of 12` would restate the length of the list beside it, and a reader
        who sees a fraction on every report stops reading it. Absent prints nothing at all — nothing
        counted the total, which is not the same as nothing having been dropped, and a reassuring
        fraction is the last thing a trimmed report should show.

        Absence is tested as `undefined` and not as "is it a number": whether the wire's value IS a
        count is decided once, where the wire is read (`ImproveCenter.readSuggestionsTotal`), and a
        second opinion here would leave that one untestable.

        Rendered under a target filter as well, unlike the three diverted sections below, and the
        difference is deliberate: those are claims about the whole population and would misattribute
        the platform's failures to the filtered component, while this is a caveat about what is
        MISSING from the report — narrowing the view does not make it less true, and a caveat that
        hides when the reader narrows is a caveat nobody sees.
      */}
      {suggestionsTotal !== undefined && suggestions.length < suggestionsTotal && (
        <div data-testid="suggestions-trimmed" style={{ ...metaTextStyle, marginBottom: 8 }}>
          {'This report carries '}
          <span style={figureStyle}>{fractionLabel(suggestions.length, suggestionsTotal)}</span>
          {` ${pluralise(suggestionsTotal, 'suggestion')}, and the rest were dropped to fit its `
            + 'size limit.'}
        </div>
      )}

      {open.length === 0 ? (
        <div data-testid="inbox-empty" style={{ ...metaTextStyle, padding: '18px 4px' }}>
          {handledList.length > 0
            ? 'Nothing left to review.'
            : 'No open suggestions.'}
        </div>
      ) : (
        open.map((suggestion) => (
          <SuggestionCard
            key={suggestion.suggestionId}
            suggestion={suggestion}
            expanded={expandedId === suggestion.suggestionId}
            onToggle={() => setExpandedId(
              expandedId === suggestion.suggestionId ? null : suggestion.suggestionId,
            )}
            onAction={recordAction}
            evidence={evidence?.[suggestion.suggestionId]}
          />
        ))
      )}

      {handledList.length > 0 && (
        <section data-testid="handled" style={{ marginTop: 16 }}>
          <SectionHeading>
            handled ({handledList.length})
          </SectionHeading>
          {handledList.map((suggestion) => (
            <div
              key={suggestion.suggestionId}
              data-testid={`handled-${suggestion.suggestionId}`}
              style={{
                ...metaTextStyle, display: 'flex', alignItems: 'baseline', gap: 8,
                padding: '6px 10px', borderBottom: `1px solid ${theme.colors.border}`,
              }}
            >
              <span style={{ color: theme.colors.textTertiary }}>
                {effectiveHandled(suggestion)}
              </span>
              <span>{suggestion.targetName}</span>
              <button
                type="button"
                onClick={() => undoHandled(suggestion)}
                data-testid={`undo-${suggestion.suggestionId}`}
                style={{
                  marginLeft: 'auto', background: 'none', border: 'none', padding: 0,
                  cursor: 'pointer', color: theme.colors.primary,
                  fontSize: theme.typography.sizes.meta,
                }}
              >
                undo
              </button>
            </div>
          ))}
        </section>
      )}

      {/* All three sections below are deliberately suppressed under a filter: they are
          statements about the WHOLE population, and showing them beside a one-component view
          would attribute the platform's failures to that component. */}
      {!targetFilter && (
        <>
          <DivertedSection
            testId="unexplained-failures"
            rows={unexplained.map(residueRow)}
            heading={(n) => `${n} ${pluralise(n, 'failure')} produced no suggestion`}
            blurb={'These runs failed, but no pattern separated them from a run that passed. '
              + 'They are listed rather than hidden, an unexplained failure is still a failure.'}
          />
          <DivertedSection
            testId="environment-failures"
            rows={environment.map(residueRow)}
            heading={(n) => `${n} ${pluralise(n, 'failure')}, not the agent's fault `
              + '(platform degraded)'}
            blurb={'These runs failed while the platform was degraded underneath the agent. They '
              + 'are not unexplained and not an observability gap. Attribution stood down, so '
              + 'they route to ops / incident review rather than into a suggestion to change '
              + 'the agent.'}
          />
          {/*
            Patterns the loop can see but cannot yet name an owner for.

            Hidden at zero, like its two siblings — and deliberately UNLIKE the same bucket on
            the admin Findings tab, which states all three of absent / none / listed. Different
            audiences, different right answers: Findings is a coverage audit, where a vanished
            section is a lie about what was checked, while the Inbox is the end-user surface and
            its rule is specific recommendations only, nothing unpolished. A "none" line here
            would be clutter that never becomes an action.

            The lead is a run COUNT, not the facet names: a pattern spans several runs so there
            is no id to show, and the facet names are the engine's vocabulary, which is what
            `plainReason` exists to keep off this surface.
          */}
          <DivertedSection
            testId="unowned-patterns"
            rows={unattributed.map((entry, index) => ({
              id: String(index),
              lead: `${entry.runCount} ${pluralise(entry.runCount, 'run')}`,
              text: entry.plainReason ?? entry.reason,
            }))}
            heading={(n) => `${n} ${pluralise(n, 'pattern')} not yet traced to a component`}
            blurb={'These runs show a pattern we can measure, but nothing in the catalogue of '
              + 'system components yet describes the part of the system it points at, so no '
              + 'recommendation could be made. They are listed rather than hidden: a failure we '
              + 'can see and cannot yet explain is still a failure.'}
          />
        </>
      )}
    </div>
  );
}

/**
 * One row of a diverted section: a short identifying lead, then the explanation.
 *
 * A row model rather than a residue entry, because the third caller's records are keyed on a
 * SHAPE over several runs and have no `runId` at all. Mapping each source into this shape keeps
 * one copy of the markup, which is the whole reason `DivertedSection` was extracted.
 */
interface DivertedRow {
  id: string;
  /** Rendered monospace. A run id for a per-run bucket, a run COUNT for a per-pattern one. */
  lead: string;
  /** OPTIONAL: a row whose section blurb already says everything true about it carries none.
   *  An empty string here would render as a stray space. */
  text?: string;
}

/**
 * One of the reader's OWN diverted failures as a row. `plainReason` when the server supplies it;
 * `reason` is the engine's own wording and carries facet names, which this surface is not supposed
 * to show a user.
 *
 * The lead is the reader's own run, which is why these rows come from their bundle rather than the
 * shared one: the shared copies carry no session id, so no row there could be identified to anyone.
 */
function residueRow(entry: ResidueEvidence): DivertedRow {
  return {
    id: entry.runId,
    lead: shortRunId(entry.runId),
    text: entry.plainReason ?? entry.reason,
  };
}

/**
 * One diverted-failures section. Every caller previously carried a byte-identical copy of this
 * markup, which is how the headings would have drifted apart.
 */
function DivertedSection({
  testId, rows, heading, blurb,
}: {
  testId: string;
  rows: DivertedRow[];
  heading: (count: number) => string;
  blurb: string;
}) {
  if (rows.length === 0) return null;
  return (
    <section
      data-testid={testId}
      style={{
        marginTop: 18, padding: '10px 12px',
        border: `1px dashed ${theme.colors.borderStrong}`,
        borderRadius: theme.radius.panel,
      }}
    >
      <SectionHeading>{heading(rows.length)}</SectionHeading>
      <p style={{ ...metaTextStyle, margin: '0 0 8px' }}>{blurb}</p>
      {rows.map((row) => (
        <div key={row.id} style={{ ...metaTextStyle, marginBottom: 5 }}>
          <code style={numericStyle}>{row.lead}</code>
          {row.text && <>{' '}{row.text}</>}
        </div>
      ))}
    </section>
  );
}

/** A fabricated id is not a UUID, so truncating it to 8 chars would hide what it is. */
function shortRunId(runId: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(runId) ? runId.slice(0, 8) : runId;
}

/** An Error carries the server's own message; anything else gets the caller's fallback. */
function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 style={{
      margin: '0 0 6px', fontSize: theme.typography.sizes.meta,
      fontWeight: theme.typography.weights.semibold,
      color: theme.colors.textSecondary, textTransform: 'uppercase', letterSpacing: 0.4,
    }}>
      {children}
    </h3>
  );
}

export default ImproveInbox;
