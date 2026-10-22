/**
 * Whether the work landed, for every request on the harness: the scorecard's quality half.
 *
 * Reads `Scorecard` and nothing else. Every figure here is measured over EVERY tenant's runs, so
 * nothing on it can be captioned as the reader's own — the heading says whose runs these are, and
 * the personal blocks above it are where a reader finds theirs.
 *
 * Four absences reach this block and each says in words which one it is, because collapsing any two
 * would report a fact nobody measured:
 *
 *   - no `scorecard` key: the bundle predates it. Nobody scored anything.
 *   - a scorecard with no `population`: this producer wrote the spend half and not the outcome half.
 *   - `population.total === 0`: the producer ran and had nothing to score. A measurement.
 *   - a term missing inside a populated scorecard: said per term, where it is missing.
 *
 * Three rules it exists to keep, all three of which the wire makes it possible to break:
 *
 * 1. **A rate is not a count, and `unscored` is neither a pass nor a failure.** `passRate` is taken
 *    over `pass + fail` alone, so its denominator is NOT `population.total` whenever anything was
 *    unscorable. Both denominators are on screen, and the partition is printed whole — a `0` for
 *    `unscored` is a measurement and is set as the number it is.
 * 2. **A mean carries the runs it is a mean OF.** `tiers.*` is a `MeasuredMean` over 14 of 25 runs
 *    on the live population, because the judged tiers apply to one workload class only. A bare
 *    `0.94` would read as the population's.
 * 3. **One rate over several workload classes is a mixture, not a measurement.** The live population
 *    is 14 conversational requests at 64% and 11 plan steps at 100%; the aggregate 80% is neither.
 *    Each class is gated on a different objective, so the classes are listed under the aggregate
 *    rather than left implied by it.
 * 4. **A figure counted for a reader who will never see it is still counted here.** `attribution`
 *    says how many tenants "everyone" is and how many of these requests reach no tenant at all. Both
 *    are counts and never lists — an actor id is a Cognito subject. Live: 2 tenants, and 14 of the 25
 *    requests could have an owner, all 14 of which found one.
 *
 * Check and class NAMES are the engine's identifiers (`results_delivered`, `plan_node`,
 * `DagCompletionValidator`) and never reach the screen as themselves. Their reader phrasing lives in
 * `verdictWording`, shared with the per-run block: this block names a check that FAILED and that one
 * names the same check as a requirement a run MET, and two spellings would read as two checks.
 */
import React from 'react';
import { theme, numericStyle } from '../../styles/tokens';
import type {
  Measured, MeasuredMean, PopulationCounts, RunWindow, Scorecard, WorkloadClassScore,
} from '../../improve-api';
import {
  MeasuredValue, formatWindow, fractionLabel, metaTextStyle, pluralise, tileValueStyle,
} from './improveChrome';
import { checkLabel, classLabel, formatRate, formatScore } from './verdictWording';

/** A stated absence, never a hidden section — a region that vanishes reads as a render failure.
 *  Same treatment as the waste band's, so the two read as one register. */
function Absence({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div data-testid={testId} style={{ ...metaTextStyle, color: theme.colors.textSecondary }}>
      {children}
    </div>
  );
}

/**
 * The rate, over the runs it was actually taken over.
 *
 * `passRate` is `pass / (pass + fail)`, so the denominator printed here is that sum and NOT
 * `population.total`: on a population with anything unscored the two differ, and using the total
 * would report a rate nobody computed. `null` is the producer saying nothing was scorable, which is
 * different again from the key being absent.
 */
function RateLine({ population, passRate }: {
  population: PopulationCounts;
  passRate: number | null | undefined;
}) {
  const judged = population.pass + population.fail;
  if (passRate === undefined) {
    return (
      <Absence testId="quality-rate-absent">
        This report states no pass rate, so how much of the work landed is not recorded here.
      </Absence>
    );
  }
  if (passRate === null || !(judged > 0)) {
    return (
      <div data-testid="quality-rate-none" style={{ color: theme.colors.warning }}>
        No request was judged, so there is no rate. Not a pass.
      </div>
    );
  }
  return (
    <div data-testid="quality-rate">
      <span style={{ ...tileValueStyle, fontSize: theme.typography.sizes.body }}>
        {formatRate(passRate)}
      </span>
      {' of the judged requests worked · '}
      <span style={numericStyle}>{fractionLabel(population.pass, judged)}</span>
      {' passed'}
    </div>
  );
}

/**
 * The partition, printed whole.
 *
 * All three counts and the total, every time. `unscored: 0` is a measurement — a gating check that
 * could measure on every run — and the surface's rule is that it must not be indistinguishable from
 * the key being missing, so it is set as the number it is rather than dropped for being zero.
 *
 * The three are documented to ADD to the total. When they do not, that is a producer bug and it is
 * named: four numbers that quietly fail to reconcile leave a reader doubting their own arithmetic.
 */
function PopulationLine({ population }: { population: PopulationCounts }) {
  const { total, pass, fail, unscored } = population;
  const partition = pass + fail + unscored;
  return (
    <>
      <div data-testid="quality-population">
        <span style={numericStyle}>{total}</span>
        {` ${pluralise(total, 'request')}: `}
        <span style={numericStyle}>{pass}</span>
        {' passed · '}
        <span style={numericStyle}>{fail}</span>
        {' failed · '}
        <span
          title={'A gating check had no evidence, so the run is neither a pass nor a failure. That '
            + 'says something about the evidence, not about the agent.'}
          style={{ cursor: 'help' }}
        >
          <span style={numericStyle}>{unscored}</span>
          {' could not be judged'}
        </span>
      </div>
      {partition !== total && (
        <div data-testid="quality-population-mismatch" style={{ color: theme.colors.warning }}>
          {'Those three add to '}
          <span style={numericStyle}>{partition}</span>
          {', not to the '}
          <span style={numericStyle}>{total}</span>
          {' above, so one of the four is wrong.'}
        </div>
      )}
    </>
  );
}

/**
 * One judged tier: what it asserts, its mean, and the runs that mean is over.
 *
 * The fraction is printed ALWAYS, unlike the waste band's shortfall clause, which prints only when
 * it is short. A sum over 28 of 28 findings IS the total; a MEAN over 25 of 25 runs is still a
 * statement about which runs were measurable, and the live population means both tiers over 14 of 25
 * — the judged tiers apply to one workload class only. A reader who sees the fraction on one bundle
 * and not on another cannot tell a complete mean from a formatter hiding the complete case.
 */
function TierRow({ name, tier, testId }: {
  name: string;
  tier: MeasuredMean | undefined;
  testId: string;
}) {
  if (tier === undefined) {
    return (
      <Absence testId={`${testId}-absent`}>
        {`${checkLabel(name, 'label')} — not scored in this report.`}
      </Absence>
    );
  }
  return (
    <div data-testid={testId} style={rowStyle}>
      <span style={{ ...numericStyle, textAlign: 'right' }}>
        <MeasuredValue value={tier.mean} format={formatScore} />
      </span>
      <span>{checkLabel(name, 'label')}</span>
      <span style={{ ...numericStyle, color: theme.colors.textTertiary }}>
        {`mean over ${fractionLabel(tier.measuredOf, tier.total)}`}
      </span>
    </div>
  );
}

/**
 * Which check turned a request down, and which checks ran without ever being the cause.
 *
 * The zeros are kept, in a group of their own. `DagCompletionValidator: 0` on the live population
 * means that check gated every plan step and never once failed — a measurement, and not the same
 * fact as a name being absent from the map, which would mean it never gated at all. Filtering zeros
 * out erases that difference; ranking them among the failures files "never the cause" under causes.
 * So they are stated below, as what they are.
 *
 * The counts do NOT sum to `population.fail`, because a run failing two checks is counted under
 * both. Said in words rather than left for a reader to trip over.
 */
function GatingList({ failedGating, fail }: {
  failedGating: Record<string, number> | undefined;
  fail: number;
}) {
  if (failedGating === undefined) {
    return (
      <Absence testId="quality-gating-absent">
        This report does not say which check turned a request down.
      </Absence>
    );
  }
  const entries = Object.entries(failedGating);
  if (entries.length === 0) {
    return (
      <Absence testId="quality-gating-empty">
        No gating check is named here, so which check turned a request down was not recorded. Not
        the same as nothing failing.
      </Absence>
    );
  }
  // Descending, so the check costing the most requests is read first. Ties keep the producer's
  // order, which is the only ordering the wire carries.
  const failures = entries.filter(([, count]) => count > 0).sort((a, b) => b[1] - a[1]);
  const clear = entries.filter(([, count]) => count === 0);
  // Everything that is neither: a negative count and a non-numeric one are producer bugs, and
  // neither a failure nor a clean check. Named rather than sorted into one of the two groups.
  const impossible = entries.filter(([, count]) => !(count > 0) && count !== 0);
  return (
    <div data-testid="quality-gating">
      {failures.map(([name, count]) => (
        <div key={name} data-testid={`quality-gating-${name}`} style={rowStyle}>
          <span style={{ ...numericStyle, textAlign: 'right' }}>{count}</span>
          {/* The noun travels with the clause rather than in the third column, where the tier rows
              keep a denominator: these counts have none — they do not sum to anything on screen —
              and `5 … requests` split across two columns leaves the noun orphaned from its figure. */}
          <span>{`${pluralise(count, 'request')} — ${checkLabel(name, 'failed')}`}</span>
        </div>
      ))}
      {failures.length > 0 && (
        <div data-testid="quality-gating-caveat" style={{ color: theme.colors.textTertiary }}>
          {'A request that failed two checks is counted under both, so these do not add up to the '}
          <span style={numericStyle}>{fail}</span>
          {` ${pluralise(fail, 'failure')} above.`}
        </div>
      )}
      {clear.length > 0 && (
        <div data-testid="quality-gating-clear" style={{ color: theme.colors.textTertiary }}>
          {'Gated and never the cause: '}
          {clear.map(([name]) => checkLabel(name, 'label')).join('; ')}
          {'.'}
        </div>
      )}
      {impossible.length > 0 && (
        <div data-testid="quality-gating-impossible" style={{ color: theme.colors.warning }}>
          {'A check reports a count that cannot exist: '}
          {impossible.map(([name, count]) => `${checkLabel(name, 'label')} (${String(count)})`)
            .join('; ')}
          {'.'}
        </div>
      )}
    </div>
  );
}

/**
 * Gating checks whose evidence was missing, which is what makes a run unscored rather than failed.
 *
 * All zeros is the live reading and it is worth stating: every gating check had what it needed on
 * every run. Stating it IS the point — a silent section here would be indistinguishable from a
 * bundle that never recorded the term.
 */
function UnmeasuredList({ unmeasured }: { unmeasured: Record<string, number> | undefined }) {
  if (unmeasured === undefined) {
    return (
      <Absence testId="quality-unmeasured-absent">
        This report does not say whether any check was left without evidence.
      </Absence>
    );
  }
  const entries = Object.entries(unmeasured);
  if (entries.length === 0) {
    return (
      <Absence testId="quality-unmeasured-empty">
        No check is named, so whether any was left without evidence was not recorded.
      </Absence>
    );
  }
  const missing = entries.filter(([, count]) => count > 0).sort((a, b) => b[1] - a[1]);
  if (missing.length === 0) {
    return (
      <div data-testid="quality-unmeasured-none" style={{ color: theme.colors.textTertiary }}>
        Every gating check had the evidence it needed, on every request.
      </div>
    );
  }
  return (
    <div data-testid="quality-unmeasured" style={{ color: theme.colors.warning }}>
      {missing.map(([name, count]) => (
        <div key={name}>
          <span style={numericStyle}>{count}</span>
          {` ${pluralise(count, 'request')} carried no evidence for: ${checkLabel(name, 'label')}`}
        </div>
      ))}
      <div style={{ color: theme.colors.textTertiary }}>
        Those requests are unscored, not failures.
      </div>
    </div>
  );
}

/**
 * One rate per workload class, because the aggregate above spans classes gated on different
 * objectives.
 *
 * Not decoration on the headline rate: on the live population that 80% is 14 conversational
 * requests at 64% and 11 plan steps at 100%, and the aggregate is neither of those numbers. A reader
 * shown only the aggregate takes the plan steps' perfect record as evidence about the requests
 * people actually made.
 *
 * Rendered whenever the key is there, single class included: "one kind, and here it is" costs a line
 * and removes the question of whether the aggregate hides a mixture.
 */
function ByClassList({ byWorkloadClass }: {
  byWorkloadClass: Record<string, WorkloadClassScore> | undefined;
}) {
  if (byWorkloadClass === undefined) {
    return (
      <Absence testId="quality-classes-absent">
        This report does not break the rate down by kind of work, so whether the rate above spans
        several kinds is not recorded.
      </Absence>
    );
  }
  const entries = Object.entries(byWorkloadClass);
  if (entries.length === 0) {
    return (
      <Absence testId="quality-classes-empty">
        No kind of work is named, so the rate above cannot be read per kind.
      </Absence>
    );
  }
  return (
    <div data-testid="quality-classes">
      {entries.map(([name, score]) => (
        <div key={name} data-testid={`quality-class-${name}`} style={rowStyle}>
          <span style={{ ...numericStyle, textAlign: 'right' }}>
            {score.passRate === null ? 'none judged' : formatRate(score.passRate)}
          </span>
          <span>{classLabel(name)}</span>
          <span style={{ ...numericStyle, color: theme.colors.textTertiary }}>
            {`${fractionLabel(score.pass, score.pass + score.fail)} passed`}
            {score.unscored > 0 ? `, ${score.unscored} unjudged` : ''}
          </span>
        </div>
      ))}
      {entries.length > 1 && (
        <div data-testid="quality-classes-caveat" style={{ color: theme.colors.textTertiary }}>
          {'Each kind is gated on a different objective, so the single rate above is a mixture of '
            + `these ${entries.length} and not a measurement of any one of them.`}
        </div>
      )}
      {entries.some(([, score]) => score.gatingIsMixed) && (
        <div data-testid="quality-classes-mixed" style={{ color: theme.colors.warning }}>
          Some of these were not all gated on the same checks, so one rate over them compares
          requests judged against different objectives.
        </div>
      )}
    </div>
  );
}

/**
 * A count the wire really carried. A negative or non-finite one is neither a measurement nor a zero,
 * so it is reported as not stated rather than printed: `-1 of 14` launders a producer bug into a
 * figure a reader will believe.
 *
 * The SECOND copy of this rule on the surface — `SuggestionCard` has a byte-identical `counted`,
 * module-private there. Duplicated knowingly rather than shared, because the shared home for it is
 * `improveChrome` and that module is not this change's to touch; the two must be reconciled there
 * before a third caller appears, or they will drift the way `pluralise`'s three spellings did.
 */
function counted(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * How many people "everyone" is.
 *
 * The count that makes this block's heading checkable. Every figure here mixes tenants, so the 80%
 * above cannot be captioned as the reader's — and `2` is how a reader can tell, rather than having to
 * take the heading's word for it.
 *
 * A COUNT and never a list, and not because a list would be long: actor ids are Cognito subjects, so
 * naming them would tell each tenant who else is on the harness.
 *
 * No possessive on the figure. `1 tenant’ runs` is what `pluralise` plus a bare apostrophe produces
 * at a count of one, and the fix is not a ternary here — an irregular possessive belongs in the
 * shared helper or nowhere, so the sentence is built to need neither.
 */
function TenantCount({ actors }: { actors: unknown }) {
  if (!counted(actors)) {
    return (
      <Absence testId="quality-attribution-actors-absent">
        This report does not say how many tenants these figures are aggregated over, so how much of
        the rate above is any one tenant’s cannot be read.
      </Absence>
    );
  }
  return (
    <div data-testid="quality-attribution-actors">
      {'Aggregated over '}
      <span style={numericStyle}>{actors}</span>
      {` ${pluralise(actors, 'tenant')}, so no figure above is any one tenant’s.`}
    </div>
  );
}

/**
 * How many of these requests reach a reader, and how many are counted for nobody.
 *
 * `unattributable` is the number that must not be hidden: those runs are conversational, scored, and
 * counted into every population figure above, and no actor's surface will ever show them, because
 * AgentCore Memory named no owner for the session. `0` is a MEASUREMENT — every ownable request found
 * its owner — and is stated as one rather than dropped for being zero.
 *
 * TWO denominators, because one is not enough to read either number. `unattributable` is over
 * `ownable`, never over the population; and `ownable` is over `runs`, or `0 of 14` sitting under a
 * `25 requests` partition reads as arithmetic that has gone wrong. It has not: only a request
 * somebody made can have an owner at all, so on the live population 11 of the 25 are outside the
 * question rather than failing it. That clause is printed only when there IS a rest — at
 * `ownable === runs` there is none, and saying so anyway would invent a class of work the bundle does
 * not contain.
 *
 * `runs` is this block's own denominator and deliberately not `population.total`. The producer counts
 * the two differently — `population.total` is `len(verdicts)`, `runs` is `len(runs)` — so they part
 * company on any unpaired verdict, and borrowing the other would print a fraction nobody computed.
 * They happen to agree at 25 on the live bundle, which is exactly how such a substitution survives
 * review.
 *
 * `attributed + unattributable === ownable` holds BY CONSTRUCTION upstream, where `unattributable` IS
 * that difference. So a trio that does not reconcile is a producer bug, and it REPLACES the reading
 * rather than joining it: `unattributable: 0` beside `attributed: 13 of 14` would otherwise print
 * "every one reached a tenant" directly above the numbers that say one did not.
 */
function OwnershipLine({ attribution }: {
  attribution: Scorecard['attribution'] | undefined;
}) {
  const { attributed, unattributable, ownable, runs } = attribution ?? {};
  if (!counted(attributed) || !counted(unattributable) || !counted(ownable) || !counted(runs)) {
    return (
      <Absence testId="quality-attribution-absent">
        This report does not say how many of these requests belong to a tenant who can see them, so
        whether any were counted here for nobody is not recorded.
      </Absence>
    );
  }
  // `fractionLabel` answers with a SENTENCE rather than a fraction when there is no denominator, and
  // `nothing to measure requests could have an owner` is not one. An ownership split over no request
  // at all is its own fact anyway, and the population partition above has already given the count.
  if (runs === 0) {
    return (
      <Absence testId="quality-attribution-empty">
        These ownership figures count no request at all, so none of the work above can be placed with
        a tenant either way.
      </Absence>
    );
  }
  return (
    <>
      <div data-testid="quality-attribution-ownable">
        <span style={numericStyle}>{fractionLabel(ownable, runs)}</span>
        {' requests could have an owner at all'}
        {ownable < runs && (
          <span
            title={'Only a request somebody made has a conversation behind it, so only that kind can '
              + 'be traced to a tenant. Every other kind — a step inside a plan, a tool-discovery '
              + 'handshake — has no owner by construction, not by a lookup that failed.'}
            style={{ cursor: 'help' }}
          >
            {' · the rest are not requests anybody made'}
          </span>
        )}
      </div>
      {attributed + unattributable !== ownable ? (
        <div data-testid="quality-attribution-mismatch" style={{ color: theme.colors.warning }}>
          {'Owned and unowned add to '}
          <span style={numericStyle}>{attributed + unattributable}</span>
          {', not to the '}
          <span style={numericStyle}>{ownable}</span>
          {' that could have an owner, so one of those three is wrong and none can be read.'}
        </div>
      ) : unattributable > 0 ? (
        <div data-testid="quality-attribution-orphaned" style={{ color: theme.colors.warning }}>
          <span style={numericStyle}>{fractionLabel(unattributable, ownable)}</span>
          {' of those belong to nobody: scored and counted in every figure above, and on no tenant’s '
            + 'own screen, because nothing named an owner for the session.'}
        </div>
      ) : (
        <div data-testid="quality-attribution-owned" style={{ color: theme.colors.textTertiary }}>
          {'All '}
          <span style={numericStyle}>{ownable}</span>
          {' of those reached a tenant, so nothing above is counted for somebody who can never see '
            + 'it.'}
        </div>
      )}
    </>
  );
}

/**
 * The population's wall clock, and whether a collector actually recorded it.
 *
 * `derivedWindows` read against the population is a caveat and never a pass: every window measured
 * so far is reconstructed from span bounds, and spans vanish first when the platform breaks, so a
 * derived window narrows during the outage it should detect. Worded exactly as
 * `EvidenceQualityBlock` words the same fact — when every window is derived the caption above has
 * already said `reconstructed` and the CONSEQUENCE is the half it cannot carry, so the count is
 * dropped there and kept wherever it is a fraction.
 */
function WindowLine({ runWindow, derivedWindows, total }: {
  runWindow: Measured<RunWindow> | undefined;
  derivedWindows: number | undefined;
  total: number;
}) {
  return (
    <>
      <div
        data-testid="quality-window"
        style={{ ...numericStyle, color: theme.colors.textSecondary }}
      >
        {runWindow === undefined ? 'no window recorded' : formatWindow(runWindow)}
      </div>
      {typeof derivedWindows === 'number' && derivedWindows > 0 && (
        <div data-testid="quality-derived-windows">
          {derivedWindows >= total ? (
            <span style={{ color: theme.colors.warning }}>
              no collector recorded these timings, so the extent may be short
            </span>
          ) : (
            <>
              <span style={{ color: theme.colors.warning }}>
                {`${derivedWindows} of ${total} timings reconstructed`}
              </span>
              <span style={{ color: theme.colors.textTertiary }}>
                {' '}(no collector recorded them, so the extent may be short)
              </span>
            </>
          )}
        </div>
      )}
    </>
  );
}

/**
 * How much of the population's own evidence is known missing.
 *
 * All three numbers or none, per the wire's own rule: `incomplete: 0` alone is indistinguishable
 * from "no harvester recorded completeness for any run", so `measuredOf: 0` is NOT an all-clear and
 * says so in words.
 */
function EvidenceLine({ evidenceComplete }: {
  evidenceComplete: Scorecard['evidenceComplete'] | undefined;
}) {
  const complete = evidenceComplete;
  if (complete === undefined
      || !Number.isFinite(complete.incomplete)
      || !Number.isFinite(complete.measuredOf)
      || !Number.isFinite(complete.total)) {
    return (
      <Absence testId="quality-evidence-absent">
        This report does not say whether any request was harvested incompletely.
      </Absence>
    );
  }
  if (complete.measuredOf === 0) {
    return (
      <div data-testid="quality-evidence-unchecked" style={{ color: theme.colors.warning }}>
        No request was checked for missing evidence, so nothing here is a clean bill.
      </div>
    );
  }
  if (complete.incomplete === 0) {
    return (
      <div data-testid="quality-evidence-clean" style={{ color: theme.colors.textTertiary }}>
        <span style={numericStyle}>{fractionLabel(complete.measuredOf, complete.total)}</span>
        {' requests were checked for missing evidence, and none was short.'}
      </div>
    );
  }
  return (
    <div data-testid="quality-evidence-incomplete" style={{ color: theme.colors.warning }}>
      <span style={numericStyle}>
        {fractionLabel(complete.incomplete, complete.measuredOf)}
      </span>
      {' checked requests had part of their trajectory never read, so what was observed of them '
        + 'is short.'}
    </div>
  );
}

export function QualityBlock({ scorecard }: {
  /** The SHARED scorecard, absent on a bundle that predates it. Never a per-actor bundle: nothing
   *  here is about one reader, and every caption says so. */
  scorecard?: Scorecard;
}) {
  /*
   * Every term is read as OPTIONAL, and widened here rather than at each use.
   *
   * `Scorecard` declares all of these required, and the live bundle already omits keys it declares
   * — an item stored before a key existed is served back unchanged, and the producer and this SPA
   * deploy independently. A reader that trusted the declaration would throw on a real payload, and
   * `?? 0` / `|| {}` in place of these annotations would turn every one of those absences into a
   * confident zero.
   */
  const population: PopulationCounts | undefined = scorecard?.population;
  const passRate: number | null | undefined = scorecard?.passRate;
  const tiers: Scorecard['tiers'] | undefined = scorecard?.tiers;
  const outcomes: Scorecard['outcomes'] | undefined = scorecard?.outcomes;
  const byWorkloadClass: Scorecard['byWorkloadClass'] | undefined = scorecard?.byWorkloadClass;
  const evidenceComplete: Scorecard['evidenceComplete'] | undefined = scorecard?.evidenceComplete;
  const runWindow: Measured<RunWindow> | undefined = scorecard?.window;
  const derivedWindows: number | undefined = scorecard?.derivedWindows;
  const attribution: Scorecard['attribution'] | undefined = scorecard?.attribution;

  return (
    <section data-testid="quality-block" style={metaTextStyle}>
      <h3
        style={headingStyle}
        title={'These are every tenant’s runs aggregated, so no figure here is about you alone. '
          + 'Your own runs are listed above.'}
      >
        Whether the work landed · everyone’s requests
      </h3>

      {scorecard === undefined ? (
        <Absence testId="quality-no-scorecard">
          This report predates the scorecard, so nothing was scored. Not a clean result.
        </Absence>
      ) : population === undefined ? (
        <Absence testId="quality-no-population">
          This report carries what the work cost and not whether it worked: nothing counted passes
          or failures.
        </Absence>
      ) : population.total === 0 ? (
        <Absence testId="quality-empty">
          The producer ran and found no request to score, so there is nothing to pass or fail here.
        </Absence>
      ) : (
        <>
          <RateLine population={population} passRate={passRate} />
          <PopulationLine population={population} />

          <div style={groupStyle}>
            <ByClassList byWorkloadClass={byWorkloadClass} />
          </div>

          {/*
            After the classes, not before them: `ownable` IS the conversational count, so a reader
            who has just been shown 14 conversational and 11 plan steps reads "14 of 25 could have an
            owner" as the same partition said a second way, rather than as a third denominator
            appearing out of nowhere.
          */}
          <div style={groupStyle}>
            <TenantCount actors={attribution?.actors} />
            <OwnershipLine attribution={attribution} />
          </div>

          <div style={groupStyle}>
            {tiers === undefined ? (
              <Absence testId="quality-tiers-absent">
                This report carries no judged scores, so how well the work was done — as against
                whether it passed — is not recorded.
              </Absence>
            ) : (
              <>
                <TierRow
                  name="dispatch_correct"
                  tier={tiers.dispatch}
                  testId="quality-tier-dispatch"
                />
                <TierRow
                  name="results_delivered"
                  tier={tiers.delivery}
                  testId="quality-tier-delivery"
                />
              </>
            )}
          </div>

          <div style={groupStyle}>
            <GatingList failedGating={outcomes?.failedGating} fail={population.fail} />
            <UnmeasuredList unmeasured={outcomes?.unmeasured} />
          </div>

          <div style={groupStyle}>
            <WindowLine
              runWindow={runWindow}
              derivedWindows={derivedWindows}
              total={population.total}
            />
            <EvidenceLine evidenceComplete={evidenceComplete} />
          </div>
        </>
      )}
    </section>
  );
}

/** Figure, then what it is of, then its denominator. The waste band's row geometry, so a count on
 *  one band reads against a count on the other. */
const rowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(56px, auto) minmax(0, 1fr) minmax(0, auto)',
  gap: 12,
  alignItems: 'baseline',
  padding: '3px 0',
};

/** A group of related lines. Spacing only — a border here would make each group read as a card,
 *  and on this surface a bordered box is a suggestion. */
const groupStyle: React.CSSProperties = { marginTop: 6 };

const headingStyle: React.CSSProperties = {
  margin: '0 0 4px',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  cursor: 'help',
};

export default QualityBlock;
