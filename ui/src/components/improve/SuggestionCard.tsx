/**
 * One suggestion: WHAT HAPPENED and what a reader may do about it, and behind one disclosure
 * everything the engine needs to say — the proposal, the candidate switcher, the diff, the warrant,
 * the evidence quality, the blame, the actions and the provenance of the claim.
 *
 * THE COLLAPSED CARD IS FOR THE READER; the disclosure is for the engine. Four lines of the reader's
 * own words, one of which — the headline — is the only text that distinguishes this card from the next
 * in a list. Everything phrased in the harness's vocabulary is behind the click: the facet a shape
 * rests on, the registry's permission token, the component's id, the judge's config hash. The one
 * thing that may NOT be moved back there is the caveat that the claim is inferred, because it changes
 * how the line above it should be read and a reader who never expands would take an inference for a
 * measurement.
 *
 * Every control's enabled state comes from `suggestion.affordances`, computed server-side.
 * The browser cannot decide whether something is publishable, that depends on the Σ
 * registry's mutability matrix and on Lab version lineage, so it renders the answer and
 * never infers it. A disabled control always carries the reason in its tooltip, because
 * "greyed out with no explanation" is how a permission model becomes folklore.
 *
 * The write actions are inert for now and announce themselves; only reads are wired. The
 * notable handlers to come are that Try-both must `EditAsset`-fork first because publishing
 * consumes the working copy, and that Publish is gated on a trial whenever the blast radius
 * is wider than one instance, since `RollbackAsset` is still unwired and there is no
 * one-click revert.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import type { FailurePattern, PatternEvidence, Suggestion } from '../../improve-api';
import { TwoPaneDiff, UnifiedDiff } from './DiffView';
import {
  ActionButton, DispositionBadge, EvidenceQualityBlock, NotReproducingBadge, SentinelValue,
  cardStyle, formatIsoDay, metaTextStyle, pluralise,
} from './improveChrome';
import { checkLabel, permissionLabel } from './verdictWording';

/**
 * How a figure is set inside flowing prose, as against inside an identifier or a column.
 *
 * Weight, and deliberately not `numericStyle`. That token is mono plus `tabular-nums`: the tabular
 * digits earn their place where figures line up down a column, and the mono face earns its place on an
 * identifier — a pattern id, a component id, a judge's config hash — where the reader is matching
 * characters. The collapsed card's reading is neither, and it is the case that proves it: `Seen in 1
 * run, last 4 Sep` set the `1` and the `4 Sep` in a second typeface mid-sentence, at a visibly
 * different width from the words either side, and read as a rendering fault rather than as emphasis.
 *
 * Semibold rather than nothing at all, so the figure is still the part of the line a reader's eye
 * lands on first. `ImpactSummary` and `ImproveInbox` set a figure in prose the same way, under this
 * same name, for the same reason; the three are short enough to keep locally, and none wants another's
 * tokens.
 *
 * The body family is NAMED rather than left to inherit, so the choice is visible where it is made and
 * a test can read it — an inherited face and a chosen one look identical on screen, and only one of
 * them survives being moved into a context that sets mono.
 *
 * The numeric spine stays where there IS a column or an identifier, all of it behind the disclosure:
 * `patternId`, `targetId`, the permission the registry recorded, the facet and store names, and the
 * counts in the `why` rows and the gate line, which sit under a label as machine values rather than in
 * a sentence. A change that swept those to match this line would take their alignment with it.
 */
const figureStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontFamily,
  fontWeight: theme.typography.weights.semibold,
};

export function SuggestionCard({
  suggestion, expanded, onToggle, onAction, evidence,
}: {
  suggestion: Suggestion;
  expanded: boolean;
  onToggle: () => void;
  onAction: (action: string, suggestion: Suggestion, candidateId?: string) => void;
  /**
   * This reader's own evidence for THIS suggestion, from `ImproveData.actor.evidence`.
   *
   * Optional and usually absent: the suggestion is a claim about the shared harness, so its
   * evidence may well be another actor's runs, and the producer writes each actor only their own.
   * Absent means the card shows the claim without the session-level detail — never a placeholder.
   */
  evidence?: PatternEvidence;
}) {
  const [candidateId, setCandidateId] = React.useState(
    suggestion.candidates[0]?.candidateId ?? '',
  );
  const [diffMode, setDiffMode] = React.useState<'two-pane' | 'unified'>('two-pane');
  const candidate = suggestion.candidates.find((c) => c.candidateId === candidateId)
    ?? suggestion.candidates[0];
  const { affordances } = suggestion;

  const symptom = facets(suggestion.pattern.symptom);
  const equallyDiscriminative = facets(suggestion.pattern.equallyDiscriminative);
  const withheld = withheldWarrant(suggestion.pattern);
  const lead = leadLine(suggestion);
  const scale = evidenceScale(suggestion);
  /*
   * Decided HERE rather than inside the note, so the separator between the two meta readings can be
   * drawn only when there really are two of them — and so the rule that only the INFERENCE reaches
   * this position lives in one place. The other two states of the field are the provenance block's
   * (`FacetBasisNote`).
   */
  const inferred = listState(suggestion.provenance.heuristicFacets);
  const inferredShown = inferred.stated && inferred.names.length > 0;
  const summaryEchoed = suggestion.candidates.some(
    (option) => option.label.trim() === suggestion.summary.trim(),
  );
  /*
   * Kept as `string | undefined` rather than defaulted, because the three states are three facts and
   * two of them render differently: a sentence to quote, `''` for a basis that had none to quote, and
   * absent for a report that withheld one it claims to have. `?? ''` would fold the last into the
   * second and report a missing warrant as an inferred attribution.
   */
  const declaration: string | undefined = typeof suggestion.provenance.matchedFailureMode === 'string'
    ? suggestion.provenance.matchedFailureMode.trim() : undefined;

  // Publish is blocked until a trial exists when the change reaches more than one
  // instance, there is no revert to fall back on.
  const publishBlockedReason = !affordances.canPublish
    ? 'This target has no publish path, see the badge for why.'
    : affordances.requiresTrialBeforePublish
      ? `This change affects ${suggestion.blastRadius.instanceCount} places. Run "Try both" `
        + 'first, there is no one-click revert yet.'
      : null;

  return (
    <div style={cardStyle} data-testid={`suggestion-${suggestion.suggestionId}`}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span style={{
          fontSize: theme.typography.sizes.badge, color: theme.colors.textSecondary,
          textTransform: 'uppercase', letterSpacing: 0.4,
        }}>
          {suggestion.targetType}
        </span>
        <span style={{
          fontSize: theme.typography.sizes.heading,
          fontWeight: theme.typography.weights.semibold,
        }}>
          {suggestion.targetName}
        </span>
        <DispositionBadge disposition={candidate?.disposition ?? 'advisory'} />
        {suggestion.reproducing === false && (
          <NotReproducingBadge lastSeenAt={suggestion.lastSeenAt} />
        )}
        {/*
          WHAT A READER MAY DO ABOUT IT, in their words — where the thirty-word evidence clause used
          to sit. `verdictWording` phrases it, shared with the Σ map's row for the same component, and
          the registry's own token stays behind the disclosure with the other identifiers.
        */}
        <span style={{ ...metaTextStyle, marginLeft: 'auto', textAlign: 'right' }}>
          <PermissionPhrase mutability={suggestion.mutability} />
        </span>
      </div>

      {/*
        THE LEAD: what happened, and the most prominent text on the card.

        It is what distinguishes one card from the next. `summary` used to hold this position and
        cannot do that job — on every locked target it is one sentence with the component's id
        substituted into it, so two cards read as one — and the clause that WAS distinct ran to thirty
        words in the corner of the header. Both are behind the disclosure now; this is eight words.
      */}
      <div data-testid="evidence-lead" style={lead.short ? headlineStyle : longLeadStyle}>
        {lead.text}
      </div>

      {/*
        The two readings a reader needs to weigh the line above — how much evidence there is, and
        whether it was measured at all — and the control that opens everything else.

        The inference is a caveat ON THE CLAIM, so it is deliberately OUTSIDE the expand: everything
        behind the toggle describes how the verdict was produced, while this changes how the claim
        itself should be read, and a reader who never expands has to see it or they read an inference
        as a measurement. Only the FACT is here; the facet it rests on is named behind the disclosure,
        because a harness identifier in this position is vocabulary a reader cannot use.

        Renders for the INFERENCE alone — a measured "none inferred" is reassurance and would only
        teach a reader to skip this position. It is stated in the provenance block instead.
      */}
      <div style={{
        ...metaTextStyle, display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap',
      }}>
        {scale}
        {scale !== null && inferredShown && <span aria-hidden="true">·</span>}
        {inferredShown && <InferredClaimNote />}
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          data-testid="toggle-detail"
          style={{
            background: 'none', border: 'none', padding: 0, cursor: 'pointer',
            marginLeft: 'auto',
            color: theme.colors.primary, fontSize: theme.typography.sizes.meta,
          }}
        >
          {expanded ? '▾ Details' : '▸ Details'}
        </button>
      </div>

      {/*
        EVERYTHING THE ENGINE NEEDS TO SAY, behind one disclosure — and it opens on every card,
        candidate or none.

        It used to open only `expanded && candidate`, so on a locked target, which is every
        suggestion the deployed producer serves, the control rendered and did nothing at all. The
        DIFF is the only part that needs a candidate; the claim, the warrant, the evidence quality,
        the blame and the provenance are all properties of the suggestion. So each candidate-bearing
        piece guards itself and the disclosure always has content.
      */}
      {expanded && (
        <div style={{ marginTop: 10 }} data-testid="suggestion-detail">
          {/*
            The producer's one-line account of the suggestion, which on a locked target is the reason
            no change is proposed. Here rather than in the lead position: it is the same sentence on
            every locked card with a different component id in it, so as a lead it made every card
            look alike, and it names that id — which belongs with the other identifiers, behind this
            click, rather than in front of a reader who cannot look it up.

            Silent when a candidate below already says it word for word, which is the ordinary case:
            `propose._summary` sets this field to the first candidate's label, so on nearly every
            suggestion the switcher beneath is already showing this exact sentence. EXACT equality
            only, and the same call `_without_duplicated_caveats` makes on the wire — a near-copy is
            two statements and both are kept, since dropping one would silently withhold whichever
            differed.
          */}
          {!summaryEchoed && (
            <div data-testid="suggestion-summary" style={{
              fontSize: theme.typography.sizes.body, marginBottom: 8,
              lineHeight: theme.typography.lineHeight,
            }}>
              {suggestion.summary}
            </div>
          )}

          {/* --- candidate switcher: acceptance is a selection, not a yes/no --- */}
          {candidate && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
              {suggestion.candidates.map((option, index) => {
                const active = option.candidateId === candidate.candidateId;
                return (
                  <button
                    key={option.candidateId}
                    type="button"
                    onClick={() => setCandidateId(option.candidateId)}
                    data-testid={`candidate-${option.candidateId}`}
                    aria-pressed={active}
                    style={{
                      fontSize: theme.typography.sizes.meta,
                      padding: '4px 9px',
                      borderRadius: theme.radius.button,
                      border: `1px solid ${active ? theme.colors.primary : theme.colors.border}`,
                      background: active ? tint(theme.colors.primary, 12) : theme.colors.surface,
                      color: theme.colors.textPrimary,
                      cursor: 'pointer',
                      textAlign: 'left',
                      maxWidth: 320,
                    }}
                  >
                    <span style={{ color: theme.colors.textTertiary }}>
                      {String.fromCharCode(65 + index)}{' '}
                    </span>
                    {option.label}
                  </button>
                );
              })}
            </div>
          )}

          {suggestion.singleCandidateForced && (
            <div
              data-testid="single-candidate-forced"
              style={{
                ...metaTextStyle, marginBottom: 8, padding: '6px 8px',
                border: `1px dashed ${tint(theme.colors.warning, 40)}`,
                borderRadius: theme.radius.button,
              }}
            >
              Only one option here. {suggestion.singleCandidateForced}
            </div>
          )}

          {/* --- the change itself: the ONE part of this disclosure that needs a candidate --- */}
          {candidate && (candidate.before || candidate.after ? (
            <>
              <div style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
                {(['two-pane', 'unified'] as const).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    onClick={() => setDiffMode(mode)}
                    aria-pressed={diffMode === mode}
                    style={{
                      background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                      fontSize: theme.typography.sizes.badge,
                      color: diffMode === mode ? theme.colors.primary : theme.colors.textTertiary,
                    }}
                  >
                    {mode === 'two-pane' ? 'side by side' : 'unified'}
                  </button>
                ))}
              </div>
              {diffMode === 'two-pane' ? (
                <TwoPaneDiff
                  before={candidate.before}
                  after={candidate.after}
                  currentLabel={suggestion.baseVersion !== null
                    ? `v${suggestion.baseVersion} (current)` : 'current'}
                  proposedLabel="proposed"
                />
              ) : (
                <UnifiedDiff diffText={candidate.diffText} />
              )}
            </>
          ) : (
            <UnifiedDiff diffText={candidate.diffText} />
          ))}

          {/* --- claim, risk, evidence --- */}
          <dl style={{ margin: '10px 0 0', display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 10px' }}>
            {/* The claim and its risk are the CANDIDATE's, so both are absent with it — a card with
                no candidate has nothing to expect and nothing to risk, and an "expected" row with
                nothing in it would read as a claim that failed to render. */}
            {candidate && (
              <>
                <Row label="expected">
                  {/* `/g`: without it only the FIRST underscore is replaced, so a three-part term
                      like `time_to_first_token` renders as "time to_first_token". */}
                  {candidate.claim.utilityTerm.replace(/_/g, ' ')}{' '}
                  {candidate.claim.direction === 'increase' ? 'improves' : 'drops'}
                  {' '}
                  {/* An unquantified claim must show the SENTINEL, not fall silent. This branch
                      previously rendered nothing for `magnitude: null`, leaving "results delivered
                      improves", confident prose with no number behind it, which is exactly what
                      `SentinelValue` exists to prevent. Every real proposal today has a null
                      magnitude, so this was the common case, not the corner. */}
                  {candidate.claim.magnitude === null
                    ? <SentinelValue value="UNMEASURED" />
                    : (
                      <span style={numericStyle}>
                        ({candidate.claim.direction === 'increase' ? '+' : '−'}
                        {Math.round(Math.abs(candidate.claim.magnitude) * 100)}%)
                      </span>
                    )}
                </Row>
                {candidate.risk && <Row label="risk">{candidate.risk}</Row>}
              </>
            )}
            <Row label="why">
              {/*
                The shape in plain English, always. `evidenceSummary` is what the shared bundle
                carries and it is the CLAIM: "from 3 run(s) where a step that several later steps
                rely on was recorded finished before the step it depends on".

                Under it, when the reader owns one of the runs, the facet evidence from their own
                session — plan node ids, span indices, timestamps. That detail is on the reader's
                own bundle, so a suggestion resting entirely on somebody else's runs shows the
                claim with no detail beneath it, which is the true state and not a gap. There is
                no invented placeholder: `evidenceQuality` below already says how many runs there
                are and caveats thin evidence.
              */}
              {suggestion.evidenceSummary}
              {evidence?.rootCauseHypo && (
                <div
                  style={{ marginTop: 4, color: theme.colors.textSecondary }}
                  data-testid="root-cause-hypo"
                >
                  {evidence.rootCauseHypo}
                </div>
              )}
              <div style={{ marginTop: 3, color: theme.colors.textTertiary }}>
                pattern <code style={numericStyle}>{suggestion.pattern.patternId}</code>
                {' · '}
                <span style={numericStyle}>{suggestion.pattern.runCount}</span>
                {' '}{pluralise(suggestion.pattern.runCount, 'run')}
                {' · pass_hits '}
                <span style={numericStyle}>{suggestion.pattern.passHits}</span>
                {suggestion.pattern.passHits === 0
                  ? ' (fires on no passing run)'
                  : ', WARNING: this pattern also fires on a passing run'}
              </div>
              {/*
                The CONSEQUENT. `evidenceSummary` above is the antecedent — the shape — so without
                this the card states half a hypothesis, and two patterns differing only in which
                gating check failed read identically. Phrased through `verdictWording`, shared with
                the per-run rows, so one check is not named two ways on one surface.
              */}
              {symptom.length > 0 && (
                <div
                  data-testid="pattern-symptom"
                  style={{ marginTop: 3, color: theme.colors.textSecondary }}
                >
                  {`predicts: ${predicts(symptom)}`}
                </div>
              )}
              {/* How well the shape was tested, which is the whole of what `pass_hits 0` means. */}
              <GateLine pattern={suggestion.pattern} />
              {/*
                A non-empty list is a caveat ON the shape named above: the evidence did not
                distinguish it from these, so it was chosen by the miner's facet precedence rather
                than by the data. Facet names are harness identifiers, so they travel as written.

                A full stop between the names and the caveat, where a dash used to join them: the
                names are joined by ` · ` themselves, and a second punctuation mark inside one line
                left the caveat reading as one more entry in the list.
              */}
              {equallyDiscriminative.length > 0 && (
                <div
                  data-testid="pattern-equally-discriminative"
                  style={{ marginTop: 3, color: theme.colors.warning }}
                >
                  {`these separate the same runs just as well: ${equallyDiscriminative.join(' · ')}`
                    + '. So the shape above was chosen by precedence, not by the evidence.'}
                </div>
              )}
              {withheld.length > 0 && (
                <div
                  data-testid="pattern-warrant-absent"
                  style={{ marginTop: 3, color: theme.colors.textTertiary }}
                >
                  {`Not stated in this report: ${withheld.join(' · ')}.`}
                </div>
              )}
            </Row>
            {suggestion.evidenceQuality && (
              <Row label="evidence">
                <EvidenceQualityBlock quality={suggestion.evidenceQuality} />
              </Row>
            )}
            <Row label="blamed on">
              <code style={numericStyle}>{suggestion.targetId}</code>
              {suggestion.targetLocation && (
                <span style={{ color: theme.colors.textTertiary }}> · {suggestion.targetLocation}</span>
              )}
              {/*
                The registry's OWN token for the permission, set as the identifier it is, inside a
                sentence saying what it is — the treatment this file gives every harness name. The
                reader's phrase for it is on the collapsed card, so this adds the spelling a reader
                can quote in a bug report rather than repeating what they have already read.
              */}
              <div style={{ marginTop: 3, color: theme.colors.textTertiary }}>
                {'permission recorded as '}
                <code style={numericStyle}>{suggestion.mutability || 'nothing'}</code>
                {suggestion.provenance.attributionBasis === 'keyword_inferred' && (
                  <span title={'The registry does not itself declare this failure mode for '
                    + 'this component, the link was inferred from its name and location, '
                    + 'so treat it as a hypothesis.'}>
                    {' · '}inferred attribution, not declared
                  </span>
                )}
              </div>
              {(suggestion.provenance.attributionAlternatives?.length ?? 0) > 0 && (
                <div style={{ marginTop: 3, color: theme.colors.textTertiary }}>
                  also considered: {suggestion.provenance.attributionAlternatives!.join(', ')}
                </div>
              )}
              {/*
                THE WARRANT FOR THE ACCUSATION. `registry_declared` above asserts that the registry
                declares this component fails this way; this is the declaration, so the claim can be
                checked rather than taken. Prose an engineer wrote about a harness component in a repo
                document — not user input, not model prose, and it names no session — so it is shown as
                written.
              */}
              {declaration !== undefined && declaration !== '' && (
                <div
                  data-testid="matched-failure-mode"
                  style={{ marginTop: 3, color: theme.colors.textSecondary }}
                >
                  {`declared in the registry as: “${declaration}”`}
                </div>
              )}
              {/*
                What decides this line is the BASIS, not which flavour of nothing arrived. On
                `keyword_inferred` there was no declaration to quote and the caption above already
                says so, so any absence is silent. On `registry_declared` a missing sentence is a
                report asserting a declaration and withholding it — which is every suggestion on the
                wire today, and the one state a reader cannot otherwise detect. An absent key and an
                empty one both reach that reading here, so no producer bug can slip through as
                silence.
              */}
              {(declaration === undefined || declaration === '')
                && suggestion.provenance.attributionBasis === 'registry_declared' && (
                <div
                  data-testid="matched-failure-mode-absent"
                  style={{ marginTop: 3, color: theme.colors.textTertiary }}
                >
                  This report does not quote the registry declaration behind that, so the attribution
                  cannot be checked here.
                </div>
              )}
            </Row>
          </dl>

          {/* --- actions --- */}
          <div style={{
            display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap', alignItems: 'center',
          }}>
            {/* Both act ON a candidate, so both are absent when there is none: a Try-both with no
                variant to try would be the dead control this disclosure just stopped being. */}
            {candidate && (
              <>
                <ActionButton
                  label="Try both"
                  variant="secondary"
                  testId="action-try-both"
                  disabled={!affordances.canTryBoth}
                  disabledReason={TRY_BOTH_REASON[affordances.tryBothBlockedReason ?? 'none']}
                  onClick={() => onAction('try-both', suggestion, candidate.candidateId)}
                />
                {affordances.canPublish && (
                  <ActionButton
                    label={suggestion.baseVersion !== null
                      ? `Publish v${suggestion.baseVersion + 1}` : 'Publish'}
                    testId="action-publish"
                    disabled={Boolean(publishBlockedReason)}
                    disabledReason={publishBlockedReason}
                    onClick={() => onAction('publish', suggestion, candidate.candidateId)}
                  />
                )}
              </>
            )}
            {affordances.canSendToTeam && (
              <ActionButton
                label="Send to team"
                variant="secondary"
                testId="action-send-to-team"
                onClick={() => onAction('send-to-team', suggestion)}
              />
            )}
            <ActionButton
              label="Dismiss"
              variant="secondary"
              testId="action-dismiss"
              onClick={() => onAction('dismiss', suggestion)}
            />
            {affordances.canRollback && (
              <ActionButton
                label={`Revert to v${affordances.rollbackToVersion}`}
                variant="secondary"
                testId="action-revert"
                onClick={() => onAction('revert', suggestion)}
              />
            )}
          </div>

          <ProvenanceBlock suggestion={suggestion} />
        </div>
      )}
    </div>
  );
}

// --- the lead -----------------------------------------------------------------
/**
 * THE LEAD, and the most prominent text on the card: bigger than the component's name, on its own
 * line, because what happened is what a reader is deciding about and the name only says where.
 */
const headlineStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.sectionTitle,
  fontWeight: theme.typography.weights.semibold,
  lineHeight: theme.typography.lineHeight,
  margin: '6px 0 4px',
};

/**
 * The same slot when only the long form arrived, in the treatment the long form has today.
 *
 * NOT the headline style: thirty words set at heading size is a paragraph pretending to be a title,
 * and the point of the size is that eight words earn it. So the position is filled either way and the
 * prominence is earned by the short form alone.
 */
const longLeadStyle: React.CSSProperties = { ...metaTextStyle, margin: '6px 0 4px' };

/**
 * What happened, and whether it arrived short enough to lead with.
 *
 * `evidenceHeadline` when the producer sent one; `evidenceSummary` — the line that used to lead this
 * card — when it did not. The fallback is not decoration: the field is omitted for a leading facet the
 * producer's table has no phrase for, a stored row that stopped reproducing is frozen with whatever it
 * was written with, and a deploy can serve an SPA newer than the producer behind it. All three land
 * here, and all three must degrade to the sentence a reader had before rather than to a blank line.
 *
 * A blank string is treated as absent, never printed: `nonEmpty` is what decides, so a producer that
 * ever emits `""` where it means "none" cannot leave a hole where the lead should be.
 */
function leadLine(suggestion: Suggestion): { text: string; short: boolean } {
  const headline = nonEmpty(suggestion.evidenceHeadline);
  return headline === undefined
    ? { text: suggestion.evidenceSummary, short: false }
    : { text: headline, short: true };
}

const LAST_SEEN_TITLE = 'The most recent run this was found in, taken from the store.';

/**
 * HOW MUCH EVIDENCE, and how recent — the two readings the long clause carried in front of its shape
 * ("from 1 run(s) where …") and that eight words cannot.
 *
 * A reading rather than prose, so it carries no full stop and costs the page's sentence budget
 * nothing. `1` versus `8` is the difference between an anecdote and a rate, which is why it is worth
 * the line at all.
 *
 * `null` when the wire carried neither, and the run count is dropped on its own when it is not a
 * count — a fabricated `0` or a bare noun with no figure would both read as a measurement. That
 * absence is not swallowed: `withheldWarrant` states it with the rest of the withheld warrant, once,
 * behind the disclosure, which is the same call `GateLine` makes for its three.
 */
function evidenceScale(suggestion: Suggestion): React.ReactNode {
  const runs: unknown = suggestion.pattern.runCount;
  const seen = nonEmpty(suggestion.lastSeenAt);
  if (!counted(runs) && seen === undefined) return null;
  return (
    <span data-testid="evidence-scale">
      {counted(runs) && (
        <>
          {'Seen in '}
          <span style={figureStyle}>{runs}</span>
          {` ${pluralise(runs, 'run')}`}
        </>
      )}
      {seen !== undefined && (
        <>
          {counted(runs) ? ', last ' : 'Last seen '}
          <Stamp at={seen} title={LAST_SEEN_TITLE} />
        </>
      )}
    </span>
  );
}

/**
 * Permission in the reader's words, or the registry's own token when this screen has no phrase for it.
 *
 * `verdictWording` holds the phrasing, shared with the Σ map's row for the same component, so one
 * registry value cannot be two permissions on one surface. What was here instead was the raw
 * `LOCKED_TODAY`, inside the sentence that made every locked card read like every other.
 *
 * An unphrased value is set as the identifier it is rather than translated, for the reason the Σ map
 * gives: the registry carries values the type does not name (`BUG`, `INERT`, `n/a`) and inventing a
 * phrase for one would report a guess as a decision.
 */
function PermissionPhrase({ mutability }: { mutability: string }) {
  const label = permissionLabel(mutability);
  if (label === undefined) {
    return (
      <code
        data-testid="permission"
        style={{ ...numericStyle, cursor: 'help' }}
        title={'What the registry recorded about changing this component. This screen has no phrase '
          + `for the value “${mutability}”, so it is shown as the registry spells it.`}
      >
        {mutability || 'no permission recorded'}
      </code>
    );
  }
  return (
    <span
      data-testid="permission"
      style={{ cursor: 'help' }}
      title={'What the registry recorded about changing this component. Details names the value it '
        + 'recorded.'}
    >
      {label}
    </span>
  );
}

/**
 * THE CLAIM RESTS ON AN INFERENCE — the fact, and only the fact.
 *
 * Four words on the collapsed card where a thirty-word sentence naming a facet used to be. The FACT
 * is what changes how the line above should be read, so it cannot go behind the disclosure; the facet
 * NAME is vocabulary a reader of this surface cannot use, so it does — `FacetBasisNote` names it under
 * the actions, where the other identifiers are. Keep the fact, drop the vocabulary.
 *
 * The reasoning stays reachable on the line itself rather than on the page, which is the same trade
 * the spend block makes for its collapsed cause: a reader who wants the argument can have it, and a
 * reader who wants the report is not made to walk past it.
 */
const INFERRED_TITLE = 'What happened here was deduced from what the trace implies, not measured '
  + 'directly, so it may not be what happened. Details names the part that was deduced.';

function InferredClaimNote() {
  return (
    <span
      data-testid="provenance-inferred-facet"
      title={INFERRED_TITLE}
      style={{ color: theme.colors.warning, cursor: 'help' }}
    >
      inferred, not measured
    </span>
  );
}

// --- the warrant for the claim ----------------------------------------------
/**
 * A list of facet names off the wire, or an empty one.
 *
 * `[]` for a key the bundle never carried is deliberate here and is NOT the `?? []` this surface
 * refuses everywhere else: the ABSENCE is reported separately, by `withheldWarrant`, so nothing
 * downstream has to tell an empty list from a missing key. Collapsing them at the render site alone
 * would be the defect; collapsing them after the absence has been counted is what keeps each branch
 * to one job.
 */
function facets(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((name): name is string => typeof name === 'string'
    && name !== '') : [];
}

/** A count the wire really carried. A negative or non-finite one is corruption and is neither a
 *  measurement nor a zero, so it is treated as absent rather than printed. */
function counted(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * The prefix a symptom facet carries, naming which kind of evaluator produced it.
 *
 * A symptom facet is `tier_failed:<tier>` or `check_failed:<check>`, and the remainder is the
 * evaluator's own name — which is exactly the key `verdictWording` phrases. Stripped rather than
 * shown, because `checkLabel` already holds every phrase this surface has for an evaluator and keeps
 * the raw identifier inside its own sentence for a name it has no phrase for, so asking it hides
 * nothing and inventing a second vocabulary here would let the two drift.
 */
const SYMPTOM_PREFIX = /^(?:tier|check)_failed:/;

/** What a shape PREDICTS, in the reader's words: the negation of each check the symptom names. */
function predicts(symptom: readonly string[]): string {
  return symptom.map((facet) => checkLabel(facet.replace(SYMPTOM_PREFIX, ''), 'failed'))
    .join(' · ');
}

/**
 * What the gate's three counts are and are not, for a reader who has the numbers in front of them.
 *
 * Both halves are load-bearing. The three close only WITHIN one refinement, and a pattern takes them
 * from its least-tested member run, so a reader who takes `excluded + inconclusive === considered` as
 * an identity will read a sound pattern as a broken one. And "could not be placed" has to say what it
 * means, or it reads as a near-miss where the truth is that those runs' own evidence was missing.
 */
const GATE_TITLE = 'A pattern takes these three from its least-tested member run, so they need not '
  + 'add up across the runs it rests on. Could not be placed means those runs’ own evidence was '
  + 'missing, not that the shape fired on them.';

/**
 * How well the shape was actually tested: the pool of passing runs the gate was offered, and what it
 * did with them.
 *
 * This is what makes `passHits` mean anything. `0` against one control is nearly free and `0` against
 * twelve is a real claim, and the two rendered identically for as long as the count was absent from
 * the card — which is every suggestion on the wire today.
 *
 * `considered: 0` is a MEASURED zero and the worst reading here, not a clean one: the gate had nothing
 * to test against, so `passHits: 0` establishes nothing. Said loudly, because a row of three zeros
 * would otherwise read as a shape that survived everything thrown at it.
 *
 * Nothing at all when any of the three is missing — the absence is one fact about the report and is
 * stated once by `withheldWarrant`, rather than three times here.
 */
function GateLine({ pattern }: { pattern: FailurePattern }) {
  const { controlsConsidered, controlsExcluded, controlsInconclusive } = pattern;
  if (!counted(controlsConsidered) || !counted(controlsExcluded)
      || !counted(controlsInconclusive)) {
    return null;
  }
  if (controlsConsidered === 0) {
    return (
      <div data-testid="pattern-gate-none" style={{ marginTop: 3, color: theme.colors.warning }}>
        No passing run was available to test this shape against, so pass_hits establishes nothing.
      </div>
    );
  }
  return (
    <div
      data-testid="pattern-gate"
      title={GATE_TITLE}
      style={{ marginTop: 3, color: theme.colors.textTertiary, cursor: 'help' }}
    >
      {'tested against '}
      <span style={numericStyle}>{controlsConsidered}</span>
      {` passing ${pluralise(controlsConsidered, 'run')} · `}
      <span style={numericStyle}>{controlsExcluded}</span>
      {' ruled out · '}
      <span style={numericStyle}>{controlsInconclusive}</span>
      {' could not be placed'}
    </div>
  );
}

/**
 * Which parts of the warrant this report does not state, in the reader's words.
 *
 * ONE line rather than a sentence per missing fact, and that is the difference between a card that
 * reports a gap and a card nobody finishes reading: the deployed producer predates all of these, so
 * on every suggestion served today all three are missing at once and three separate sentences would
 * bury the shape they qualify. Same argument `MetricBlock` makes for collapsing one structural cause
 * across its tiles.
 *
 * A missing key and an empty list are the same fact for each of these and are reported together, on
 * purpose. `symptom: []` would be a mined failure that predicts nothing and `equallyDiscriminative:
 * []` is the producer's measured "nothing else did as well" — but the second is the ordinary case and
 * saying so on every card would teach a reader to skip the line, so only the CAVEAT is stated when it
 * is real and only the SILENCE is stated when the wire is silent.
 */
function withheldWarrant(pattern: FailurePattern): string[] {
  const missing: string[] = [];
  if (facets(pattern.symptom).length === 0) missing.push('what this shape predicts');
  // Declared required on the wire type and absent from rows written before the producer derived it,
  // which is what makes it a withheld fact rather than an impossible one. The card leads with how much
  // evidence there is, so an unstated count has to be stated as unstated somewhere.
  if (!counted(pattern.runCount)) missing.push('how many runs it rests on');
  if (!counted(pattern.controlsConsidered) || !counted(pattern.controlsExcluded)
      || !counted(pattern.controlsInconclusive)) {
    missing.push('how many passing runs it was tested against');
  }
  if (pattern.equallyDiscriminative === undefined) {
    missing.push('whether another shape explains those runs just as well');
  }
  return missing;
}

// --- where the claim came from ------------------------------------------------
/**
 * The three states of a list off the wire, kept apart.
 *
 * `facets` above deliberately folds an absent key into `[]`, and can, because `withheldWarrant`
 * counts that absence separately. Nothing counts these absences anywhere else, so folding them here
 * would report a bundle written before a key existed as a producer that measured nothing — the
 * `?? []` this surface refuses.
 *
 * A value that is not a list at all, and a list whose entries are all unusable, are reported as NOT
 * STATED rather than as a measured none: the same call `counted` makes for a corrupt number. Saying
 * "the producer found none" on the strength of `[null, 7]` would be inventing an answer. A
 * genuinely EMPTY list is the producer's own none and stays its own state.
 */
type ListState =
  | { stated: false }
  | { stated: true; names: readonly string[] };

function listState(value: unknown): ListState {
  if (!Array.isArray(value)) return { stated: false };
  if (value.length === 0) return { stated: true, names: [] };
  const names = value.filter((name): name is string => typeof name === 'string'
    && name.trim() !== '');
  return names.length > 0 ? { stated: true, names } : { stated: false };
}

/** A wire string that is really one. `''` carries no identifier and no date, so it is not a value
 *  this can print — and printing it would leave a caption with a hole where a name should be. */
function nonEmpty(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

/**
 * Harness identifiers, set as identifiers.
 *
 * A facet name (`session_ended_on_unretrieved_dispatch`) and a store token (`ddb:plan-state`) are
 * NAMES, not prose, and they travel as written for the reason the equally-discriminative line gives
 * a few lines up: `verdictWording` holds every reader phrase this surface has, it has none for
 * either of these, and minting a second vocabulary here would let the two drift. So each is set in
 * the surface's identifier treatment INSIDE a sentence that says what it is — which is what makes it
 * readable as a name to quote in a bug report rather than as English that has gone wrong.
 *
 * ` · ` between them, the separator the warrant lines already use for a list of facet names, so one
 * list of names does not read as a different kind of thing from another.
 */
function Identifiers({ names }: { names: readonly string[] }) {
  return (
    <>
      {names.map((name, index) => (
        // Keyed on both, because a malformed wire can repeat a name and duplicate keys would drop
        // one of the two from the DOM — hiding the repetition that is itself the producer bug.
        <React.Fragment key={`${index}-${name}`}>
          {index > 0 && ' · '}
          <code style={numericStyle}>{name}</code>
        </React.Fragment>
      ))}
    </>
  );
}

/**
 * WHAT `heuristicFacets` SAYS, in all three of its states — and the half of the inference the
 * collapsed card does not carry.
 *
 * The FACT that the claim rests on an inference is a caveat on the claim, so it renders outside the
 * expand (`InferredClaimNote`) where a reader who never opens the card cannot miss it. The facet's
 * NAME is what renders here, with the store tokens and the registry revision: it is a harness
 * identifier, useful for going and re-reading the evidence or quoting it in a bug report, and useless
 * to the reader the collapsed card is written for. So the two positions carry different halves of one
 * field rather than one doubt captioned twice.
 *
 * The other two states are only here. Neither is a caveat, so neither earns the always-visible slot —
 * a reassurance printed under every card is precisely what teaches a reader to skip the line that
 * matters, which is the argument the equally-discriminative line already makes a few functions up. But
 * both have to be SAID somewhere, because absent and empty are different facts and silence would make
 * them one.
 *
 * The absence is NOT folded into `withheldWarrant`'s collapsed silence. That collapse is right for the
 * warrant facts because the deployed producer predates all of them, so they go missing on every card
 * at once; this field the deployed producer DOES send — both live suggestions carry it — so its
 * absence is the rare case and cannot bury anything by being stated.
 *
 * The inference here is not the one beside the permission, and neither absorbs the other: that caveat
 * says the registry did not declare which COMPONENT fails this way, this says WHAT HAPPENED was
 * deduced rather than observed. A suggestion can carry either, both or neither, so one sentence
 * covering both would be false in three of those four cases.
 */
function FacetBasisNote({ heuristicFacets }: { heuristicFacets: unknown }) {
  const inferred = listState(heuristicFacets);
  if (!inferred.stated) {
    return (
      <div data-testid="provenance-inference-absent">
        This report does not say whether the shape above rests on an inferred facet.
      </div>
    );
  }
  if (inferred.names.length === 0) {
    return (
      <div data-testid="provenance-measured-facets">
        Every facet in the shape above was measured directly, none inferred.
      </div>
    );
  }
  return (
    <div data-testid="provenance-inferred-facet-names" style={{ color: theme.colors.warning }}>
      {'Deduced rather than measured: '}
      <Identifiers names={inferred.names} />
      {'.'}
    </div>
  );
}

const JUDGED_TITLE = 'When the judge scored the runs behind this claim.';

/**
 * What `firstSeenAt` actually means, which is not what its name suggests.
 *
 * It is a STORE timestamp: when the loop first wrote this finding down. Not when the behaviour first
 * happened, and not when it was first observable — the loop only ever sees a run inside a harvest
 * window it has run over, so a defect can be arbitrarily older than this date, and on both live
 * suggestions it is the same instant as `evaluatedAt` and `createdAt`, i.e. the pipeline run that
 * minted the record. Captioned as "first mined" rather than "first seen" for exactly that reason:
 * "first seen" would date the DEFECT from a field that dates the RECORD.
 */
const MINED_TITLE = 'When the loop first recorded this finding, taken from the store. Not when the '
  + 'behaviour first happened: the loop only sees a run inside a harvest window it has run over, so '
  + 'the behaviour may be older than this.';

const REGISTRY_TITLE = 'The Σ registry revision whose component map produced the attribution above. '
  + 'A later revision can blame a different component for the same shape, so a claim is only '
  + 'checkable against the revision that made it.';

/**
 * A wire timestamp as a day, with the stamp itself on hover.
 *
 * `formatIsoDay` parses a bare `YYYY-MM-DD` BY FIELD, and every stamp here is a full ISO instant, so
 * the day is sliced off and handed to it rather than parsed here: that helper is field-parsed
 * precisely so one caption cannot shift by a timezone against another, and a second parse at this
 * call site is how this surface would come to hold two answers for one date.
 *
 * The day alone on screen, no year, matching every other date on this surface — `formatWindow`
 * prints none either. The whole stamp is on the title, so nothing the wire carried is out of reach:
 * two suggestions judged an hour apart show one day and differ on hover.
 *
 * A string `formatIsoDay` refuses is ECHOED rather than dropped, as `formatWindow` echoes a
 * malformed bound. A stamp a reader can see is wrong is diagnosable; a silent one is not.
 *
 * `figureStyle` and not the numeric spine, at every call site rather than at one of them. Both places
 * a stamp appears are a running line of words — the collapsed card's reading, and the two dates under
 * the actions — so there is no column for tabular digits to line up in, and `4 Sep` is not an
 * identifier a reader matches character by character the way a config hash beside it is. Mono in
 * either position is the same rendering fault, so the default that varies by call site here is the
 * one nobody wants.
 */
function Stamp({ at, title }: { at: string; title: string }) {
  const day = formatIsoDay(at.slice(0, 10));
  // ` · ` joins the two, and not a dash: it is this surface's own separator between a figure and what
  // qualifies it (`across 553 runs · 30 of 30 days measured`). A stamp and the sentence explaining it
  // are two facts side by side rather than one interrupting the other.
  return (
    <span style={{ ...figureStyle, cursor: 'help' }} title={`${at} · ${title}`}>
      {day ?? at}
    </span>
  );
}

/**
 * WHERE THE CLAIM CAME FROM: which judge, on what evidence, against which registry, and when.
 *
 * Provenance, not findings, so it sits under the actions where the judge line already sat and
 * nothing here moves the claim or the warrant down the card. The judge line is kept verbatim as the
 * block's first line: it was the only provenance on screen, and the three lines added under it are
 * the rest of the same answer.
 *
 * Every fact here describes HOW THE VERDICT WAS PRODUCED, which is what makes the expand the right
 * home for it. `heuristicFacets` is split across the two positions rather than living wholly in
 * either: the FACT of an inference changes how the claim itself reads and so sits outside the expand
 * beside the claim (`InferredClaimNote`), while the facet's NAME, a measured none and a missing key
 * are all identifiers and bookkeeping, and sit here (`FacetBasisNote`).
 */
function ProvenanceBlock({ suggestion }: { suggestion: Suggestion }) {
  const { provenance } = suggestion;
  const sources = listState(provenance.evidenceSources);
  const registry = nonEmpty(provenance.registryHeadSha);
  const judgedAt = nonEmpty(provenance.evaluatedAt);
  const minedAt = nonEmpty(suggestion.firstSeenAt);

  return (
    <div
      data-testid="provenance"
      style={{ ...metaTextStyle, marginTop: 8, color: theme.colors.textTertiary }}
    >
      <div data-testid="provenance-judge">
        Judged by <code style={numericStyle}>{provenance.judgeConfigHash}</code>
        {' over '}
        <span style={numericStyle}>{provenance.judgeRuns}</span>
        {' '}{pluralise(provenance.judgeRuns, 'run')}.
        {' A trial is a single sample, indicative, not proof.'}
      </div>

      {/*
        WHICH STORES the evidence came from. An empty list is not the same fact as a missing key and
        is the louder of the two: the producer looked and named no store, so there is nowhere to go
        and re-read the evidence. A missing key is a bundle written before the field.
      */}
      {sources.stated && sources.names.length > 0 ? (
        <div data-testid="provenance-sources">
          {'Read from '}
          <Identifiers names={sources.names} />
          {'.'}
        </div>
      ) : sources.stated ? (
        <div data-testid="provenance-sources-none" style={{ color: theme.colors.warning }}>
          This report names no store the evidence was read from, so it cannot be re-read.
        </div>
      ) : (
        <div data-testid="provenance-sources-absent">
          This report does not record which stores the evidence was read from.
        </div>
      )}

      {/* Directly under the stores, because it is the same question one step on: what the evidence
          was read from, and then which part of the shape was inferred from it rather than measured.
          Beside those tokens is where the facet name belongs; the collapsed card carries the fact. */}
      <FacetBasisNote heuristicFacets={provenance.heuristicFacets} />

      <div data-testid="provenance-registry">
        {registry === undefined ? (
          'Which registry revision blamed that component is not recorded.'
        ) : (
          <>
            {'Attributed against registry '}
            <code style={{ ...numericStyle, cursor: 'help' }} title={REGISTRY_TITLE}>
              {registry}
            </code>
            {'.'}
          </>
        )}
      </div>

      {/*
        Two dates, and the second one is easy to misread — hence its title. Each says its own absence
        in words rather than taking `UnavailableBadge`: that badge is a solid slab, and mid-clause it
        would set a boxed label where a running sentence needs a word.
      */}
      <div data-testid="provenance-dates">
        {'Judged '}
        {judgedAt === undefined
          ? <span data-testid="provenance-judged-absent">on a date this report does not record</span>
          : <Stamp at={judgedAt} title={JUDGED_TITLE} />}
        {' · first mined '}
        {minedAt === undefined
          ? <span data-testid="provenance-mined-absent">on a date this report does not record</span>
          : <Stamp at={minedAt} title={MINED_TITLE} />}
        {'.'}
      </div>
    </div>
  );
}

const TRY_BOTH_REASON: Record<string, string> = {
  no_host_agent: 'Try-both runs through an agent you own, and this target is not reachable '
    + 'through one.',
  flat_category: 'This category has no versions, so there is no second variant to compare '
    + 'against.',
  locked_today: 'This lives in platform code, so it cannot be swapped at run time.',
  none: '',
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt style={{
        fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary,
        textTransform: 'uppercase', letterSpacing: 0.4, paddingTop: 2,
      }}>
        {label}
      </dt>
      <dd style={{ margin: 0, ...metaTextStyle, color: theme.colors.textPrimary }}>
        {children}
      </dd>
    </>
  );
}

export default SuggestionCard;
