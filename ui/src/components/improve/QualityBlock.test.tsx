/**
 * @vitest-environment jsdom
 *
 * The scorecard below is the REAL one, read out of the staging table `mediaworks-harness-improve`
 * (`actorId __system__`, `sk SNAPSHOT`, `createdAt 2026-09-03T08:01:59Z`) and transcribed field for
 * field: 25 requests, 20 of them passing, both judged tiers measured over 14 of those 25, three
 * gating checks named in `outcomes` — one of which reports a real `0` — and two workload classes
 * whose rates are 64% and 100% under an aggregate of 80%.
 *
 * The `spend` half is deliberately NOT transcribed. `MetricBlock` renders it and its own tests pin
 * it; a second copy here would be a second denominator for one figure.
 *
 * Nothing is invented. Every state the live item does not contain — an unmeasurable tier mean, an
 * unscorable population, a missing term — is built by changing NAMED fields of this scorecard, and
 * each test says which.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { QualityBlock } from './QualityBlock';
import { ImproveCenter, type ImproveData } from './ImproveCenter';
import fixture from './__fixtures__/improveData.fixture.json';
import type { Scorecard } from '../../improve-api';

vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));

/**
 * The live scorecard's quality half.
 *
 * Cast because the real item omits nothing this block reads and everything it does not: `spend` is
 * declared REQUIRED on `Scorecard` and is left out here, which is the same shape mismatch every other
 * test of this surface works around — the wire and the declaration are not the same thing.
 */
const LIVE: Scorecard = {
  population: { total: 25, pass: 20, fail: 5, unscored: 0 },
  passRate: 0.8,
  tiers: {
    dispatch: { mean: 0.9392857142857143, measuredOf: 14, total: 25 },
    delivery: { mean: 0.7000000000000001, measuredOf: 14, total: 25 },
  },
  outcomes: {
    failedGating: { results_delivered: 5, dispatch_correct: 1, DagCompletionValidator: 0 },
    unmeasured: { dispatch_correct: 0, DagCompletionValidator: 0, results_delivered: 0 },
  },
  byWorkloadClass: {
    conversational: {
      total: 14, pass: 9, fail: 5, unscored: 0, passRate: 0.6428571428571429,
      gating: [['dispatch_correct', 'results_delivered']], gatingIsMixed: false,
      basis: ['inferred_from_spans'],
    },
    plan_node: {
      total: 11, pass: 11, fail: 0, unscored: 0, passRate: 1,
      gating: [['DagCompletionValidator']], gatingIsMixed: false,
      basis: ['inferred_from_spans'],
    },
  },
  window: {
    start: '2026-09-02T20:34:17.235000+00:00',
    end: '2026-09-03T05:51:50.585000+00:00',
    derived: true,
    provenance: 'union_of_member_windows',
  },
  derivedWindows: 25,
  evidenceComplete: { incomplete: 0, measuredOf: 25, total: 25 },
  attribution: { actors: 2, attributed: 14, unattributable: 0, ownable: 14, runs: 25 },
} as unknown as Scorecard;

/** The live scorecard with named fields replaced. Every derived case below says which. */
function changed(over: Partial<Scorecard>): Scorecard {
  return { ...LIVE, ...over };
}

/** The same, with a key REMOVED rather than replaced — the state a bundle written before that key
 *  existed is served back in. `Scorecard` declares them all required, hence the cast. */
function without(key: keyof Scorecard): Scorecard {
  const { [key]: _dropped, ...rest } = LIVE;
  return rest as unknown as Scorecard;
}

function text(testId: string): string {
  return screen.getByTestId(testId).textContent ?? '';
}

describe('QualityBlock, on the live scorecard', () => {
  it('gives the rate the denominator it was actually taken over', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // `passRate` is pass / (pass + fail), so 25 here is the JUDGED count and not the population —
    // the two coincide on this bundle only because nothing was unscored. A bare `80%` fails this.
    expect(text('quality-rate')).toBe('80% of the judged requests worked · 20 of 25 passed');
  });

  it('prints the partition whole, zero included', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // `unscored: 0` is a measurement — every gating check could measure on every run — and dropping
    // it for being zero would make it indistinguishable from the key being absent.
    expect(text('quality-population'))
      .toBe('25 requests: 20 passed · 5 failed · 0 could not be judged');
  });

  it('breaks the aggregate rate into the classes it is a mixture of', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // The number this block exists to qualify: 80% over 25 runs is 64% over the 14 requests people
    // made and 100% over the 11 plan steps inside them. Showing only the aggregate lets the plan
    // steps' perfect record stand in as evidence about the requests.
    expect(text('quality-class-conversational')).toBe('64%requests somebody made9 of 14 passed');
    expect(text('quality-class-plan_node')).toBe('100%steps inside a plan11 of 11 passed');
    expect(text('quality-classes-caveat'))
      .toContain('the single rate above is a mixture of these 2');
  });

  it('carries each tier mean over the runs it was a mean OF', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // 14 of 25: the judged tiers apply to one workload class only, so neither mean is the
    // population's. A bare `0.94` reads as though all 25 were scored on it.
    expect(text('quality-tier-dispatch')).toBe('0.94started the right workmean over 14 of 25');
    expect(text('quality-tier-delivery'))
      .toBe('0.70delivered a usable result in the sessionmean over 14 of 25');
  });

  it('names which check turned a request down, ranked, and never as its identifier', () => {
    render(<QualityBlock scorecard={LIVE} />);

    expect(text('quality-gating-results_delivered'))
      .toBe('5requests — no usable result reached the user');
    // Singular where the count is 1, so the row does not read as a defect on the one-failure check.
    expect(text('quality-gating-dispatch_correct'))
      .toBe('1request — the wrong work was started, or none');
    // Ranked by count: `results_delivered` (5) precedes `dispatch_correct` (1). Producer order puts
    // them the same way round here, so the order is asserted against the DOM, not the input.
    const rows = [...screen.getByTestId('quality-gating').children]
      .map((node) => node.getAttribute('data-testid'));
    expect(rows.indexOf('quality-gating-results_delivered'))
      .toBeLessThan(rows.indexOf('quality-gating-dispatch_correct'));
    const whole = text('quality-block');
    expect(whole).not.toContain('results_delivered');
    expect(whole).not.toContain('DagCompletionValidator');
  });

  it('keeps a gating check that reported zero, apart from the ones that failed', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // `DagCompletionValidator: 0` means that check gated every plan step and never once failed. It
    // is not the same fact as the name being absent from the map, so it is neither filtered out nor
    // ranked among the causes.
    expect(text('quality-gating-clear'))
      .toBe('Gated and never the cause: every planned step finished, and after what it depends on.');
    expect(screen.queryByTestId('quality-gating-DagCompletionValidator')).toBeNull();
  });

  it('says the failure counts do not add up to the failures, because they do not', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // 5 + 1 = 6 over 5 failures: the run that failed both checks is counted under both. Left
    // unstated, the arithmetic reads as a bug in one of the two numbers.
    expect(text('quality-gating-caveat'))
      .toBe('A request that failed two checks is counted under both, so these do not add up to '
        + 'the 5 failures above.');
  });

  it('states that every check had its evidence, rather than saying nothing', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // All three `unmeasured` counts are a measured 0. Silence here would be indistinguishable from a
    // bundle that never recorded the term.
    expect(text('quality-unmeasured-none'))
      .toBe('Every gating check had the evidence it needed, on every request.');
  });

  it('carries the reconstruction caveat on a window nothing stamped', () => {
    render(<QualityBlock scorecard={LIVE} />);

    expect(text('quality-window'))
      .toBe('2 Sep 20:34 → 3 Sep 05:51 (9h 18m, reconstructed)');
    // 25 of 25 derived. The caption above has already said `reconstructed`, so what is said here is
    // the CONSEQUENCE — the same split `EvidenceQualityBlock` makes for the same fact.
    expect(text('quality-derived-windows'))
      .toBe('no collector recorded these timings, so the extent may be short');
  });

  it('reports the evidence check as a check, with both its numbers', () => {
    render(<QualityBlock scorecard={LIVE} />);

    expect(text('quality-evidence-clean'))
      .toBe('25 of 25 requests were checked for missing evidence, and none was short.');
  });

  it('says whose runs these are, so an 80% rate cannot be read as the reader\'s own', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // The heading is the whole defence against the misreading: this scorecard mixes every tenant's
    // runs, so no figure in it is about the person looking at it.
    expect(screen.getByRole('heading')).toHaveTextContent('everyone’s requests');
    expect(screen.getByRole('heading'))
      .toHaveAttribute('title', expect.stringContaining('no figure here is about you alone'));
  });
});

describe('QualityBlock, on cases the live item does not contain', () => {
  it('says nothing was scored rather than showing a 0% rate', () => {
    // TWO fields changed together, because they cannot disagree: `passRate` becomes `null` — which is
    // what the producer writes when `pass + fail` is 0 — and the population becomes the unscorable
    // one that produces it. `0%` here would report a total failure over runs nobody judged.
    render(<QualityBlock scorecard={changed({
      passRate: null,
      population: { total: 25, pass: 0, fail: 0, unscored: 25 },
    })} />);

    expect(text('quality-rate-none')).toBe('No request was judged, so there is no rate. Not a pass.');
    expect(screen.queryByTestId('quality-rate')).toBeNull();
    // And the 25 unscorable runs are still counted, as neither passes nor failures.
    expect(text('quality-population'))
      .toBe('25 requests: 0 passed · 0 failed · 25 could not be judged');
  });

  it('takes the rate over the judged runs, not over the population', () => {
    /*
     * TWO fields changed, and they cannot disagree: `population` becomes a partition with two
     * unscorable runs in it, and `passRate` becomes the rate the producer computes from it —
     * 20 / (20 + 3). On the live item `pass + fail` happens to EQUAL `total`, so nothing there can
     * tell the two denominators apart; here they differ by the two runs nothing could judge, and
     * dividing by the total instead would report 80% where the producer computed 87%.
     */
    render(<QualityBlock scorecard={changed({
      population: { total: 25, pass: 20, fail: 3, unscored: 2 },
      passRate: 20 / 23,
    })} />);

    expect(text('quality-rate')).toBe('87% of the judged requests worked · 20 of 23 passed');
    // And the two runs that fell out of that denominator are still counted, as neither.
    expect(text('quality-population'))
      .toBe('25 requests: 20 passed · 3 failed · 2 could not be judged');
  });

  it('hatches a tier mean nothing could measure, and never renders it as 0', () => {
    // ONE field changed: the dispatch tier's `mean` becomes the `UNMEASURED` sentinel over
    // `measuredOf: 0`, which is what the producer writes when no run in the population was scored on
    // that tier. `0.00` would read as "the agent scored zero".
    render(<QualityBlock scorecard={changed({
      tiers: {
        dispatch: { mean: 'UNMEASURED', measuredOf: 0, total: 25 },
        delivery: LIVE.tiers.delivery,
      },
    })} />);

    expect(screen.getByTestId('sentinel')).toHaveTextContent('not measured');
    // The denominator survives the sentinel: `0 of 25` says the tier was attempted over a population
    // of 25 and reached none of them, which is what makes the absence readable.
    expect(text('quality-tier-dispatch')).toBe('not measuredstarted the right workmean over 0 of 25');
    expect(text('quality-tier-dispatch')).not.toContain('0.00');
  });

  it('reports a gating check left without evidence as unscored, not as a failure', () => {
    // ONE field changed: `outcomes.unmeasured.results_delivered` becomes 3. Those three runs are
    // unscored — the distinction the whole partition rests on.
    render(<QualityBlock scorecard={changed({
      outcomes: {
        failedGating: LIVE.outcomes.failedGating,
        unmeasured: { ...LIVE.outcomes.unmeasured, results_delivered: 3 },
      },
    })} />);

    expect(text('quality-unmeasured'))
      .toContain('3 requests carried no evidence for: delivered a usable result in the session');
    expect(text('quality-unmeasured')).toContain('Those requests are unscored, not failures.');
    expect(screen.queryByTestId('quality-unmeasured-none')).toBeNull();
  });

  it('says nobody was checked for missing evidence, rather than reporting a clean bill', () => {
    // ONE field changed: `evidenceComplete.measuredOf` becomes 0, which is what every bundle written
    // before adaptive windowing carries. `incomplete: 0` beside it is NOT an all-clear — nothing
    // looked.
    render(<QualityBlock scorecard={changed({
      evidenceComplete: { incomplete: 0, measuredOf: 0, total: 25 },
    })} />);

    expect(text('quality-evidence-unchecked'))
      .toBe('No request was checked for missing evidence, so nothing here is a clean bill.');
    expect(screen.queryByTestId('quality-evidence-clean')).toBeNull();
  });

  it('names the requests whose evidence is known short', () => {
    // ONE field changed: `evidenceComplete.incomplete` becomes 4 — a harvest window that hit the
    // CloudWatch Logs Insights row cap, so part of those runs' trajectories was never read.
    render(<QualityBlock scorecard={changed({
      evidenceComplete: { incomplete: 4, measuredOf: 25, total: 25 },
    })} />);

    expect(text('quality-evidence-incomplete')).toContain('4 of 25');
    expect(text('quality-evidence-incomplete')).toContain('never read');
  });

  it('counts the reconstructed windows when only some of them were', () => {
    // ONE field changed: `derivedWindows` becomes 9 of the 25. Then the count IS the information —
    // "reconstructed" alone would not say that 16 windows were stamped.
    render(<QualityBlock scorecard={changed({ derivedWindows: 9 })} />);

    expect(text('quality-derived-windows'))
      .toBe('9 of 25 timings reconstructed (no collector recorded them, so the extent may be short)');
  });

  it('says a term is missing rather than reading it as a zero', () => {
    // Each key REMOVED in turn, which is the state a bundle written before it carries. `?? 0` in
    // place of any of these branches is what this test exists to catch.
    for (const [key, testId, phrase] of [
      ['passRate', 'quality-rate-absent', 'states no pass rate'],
      ['tiers', 'quality-tiers-absent', 'no judged scores'],
      ['outcomes', 'quality-gating-absent', 'does not say which check'],
      ['byWorkloadClass', 'quality-classes-absent', 'does not break the rate down'],
      ['evidenceComplete', 'quality-evidence-absent', 'does not say whether any request'],
    ] as const) {
      const view = render(<QualityBlock scorecard={without(key)} />);
      expect(text(testId)).toContain(phrase);
      view.unmount();
    }
  });

  it('tells a report with no scorecard from one that scored nothing', () => {
    // Three different facts, three different sentences. Collapsing any two reports something nobody
    // measured.
    const none = render(<QualityBlock />);
    expect(text('quality-no-scorecard')).toContain('predates the scorecard');
    none.unmount();

    // `population` removed: the producer wrote the spend half and not the outcome half.
    const spendOnly = render(<QualityBlock scorecard={without('population')} />);
    expect(text('quality-no-population')).toContain('carries what the work cost and not whether it '
      + 'worked');
    spendOnly.unmount();

    // A population of 0 IS a measurement: the producer ran and had nothing to score.
    render(<QualityBlock scorecard={changed({
      population: { total: 0, pass: 0, fail: 0, unscored: 0 },
    })} />);
    expect(text('quality-empty')).toContain('found no request to score');
  });

  it('names a partition that does not add up rather than printing four numbers that do not', () => {
    // ONE field changed: `pass` becomes 19 over the same total, so the three no longer partition it.
    // A producer bug, and one this surface can see.
    render(<QualityBlock scorecard={changed({
      population: { total: 25, pass: 19, fail: 5, unscored: 0 },
    })} />);

    expect(text('quality-population-mismatch'))
      .toBe('Those three add to 24, not to the 25 above, so one of the four is wrong.');
  });

  it('names a check and a class it has no phrase for, instead of leaving a blank', () => {
    // TWO fields changed: a name the producer could add tomorrow appears in `failedGating`, and one
    // in `byWorkloadClass`. A blank cell would read as a render fault.
    render(<QualityBlock scorecard={changed({
      outcomes: { failedGating: { PolicyEngineValidator: 2 }, unmeasured: {} },
      byWorkloadClass: {
        batch_render: {
          total: 3, pass: 3, fail: 0, unscored: 0, passRate: 1,
          gating: [['PolicyEngineValidator']], gatingIsMixed: false, basis: ['declared'],
        },
      },
    })} />);

    expect(text('quality-gating-PolicyEngineValidator'))
      .toContain('a check this screen has no phrase for (PolicyEngineValidator)');
    expect(text('quality-class-batch_render'))
      .toContain('a kind of work this screen has no phrase for (batch_render)');
  });

  it('warns when a class was not gated on one set of checks throughout', () => {
    // ONE field changed: `gatingIsMixed` becomes true on the conversational class. Then one rate over
    // that class compares runs judged against different objectives.
    render(<QualityBlock scorecard={changed({
      byWorkloadClass: {
        conversational: { ...LIVE.byWorkloadClass.conversational, gatingIsMixed: true },
      },
    })} />);

    expect(text('quality-classes-mixed')).toContain('not all gated on the same checks');
  });
});

// --- whose runs these are, and who will never see them -------------------------
/**
 * The live scorecard's attribution, with named fields replaced. Nothing invented: `attribution` above
 * is transcribed from the same staging item as the rest of it.
 *
 * `attributed + unattributable === ownable` holds by construction upstream — `unattributable` IS that
 * difference — so a case moving one of the three moves the others to match, or it would be testing a
 * shape the producer cannot emit rather than a state a reader can meet.
 */
function attributed(over: Partial<Scorecard['attribution']>): Scorecard {
  return changed({ attribution: { ...LIVE.attribution, ...over } });
}

describe('QualityBlock, on who the runs belong to', () => {
  it('says how many tenants “everyone” is, and never who they are', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // The count is what makes the heading checkable rather than a claim a reader has to take. A
    // COUNT and never a list: actor ids are Cognito subjects, so naming them would tell each tenant
    // who else is on the harness.
    expect(text('quality-attribution-actors'))
      .toBe('Aggregated over 2 tenants, so no figure above is any one tenant’s.');
  });

  it('gives the ownable count its denominator, and takes it from this block', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // 14 of 25, and the 14 is the conversational class the two rows above already named — so the
    // reading is that partition said a second way, not a third denominator out of nowhere.
    expect(text('quality-attribution-ownable'))
      .toBe('14 of 25 requests could have an owner at all · the rest are not requests anybody made');
  });

  it('takes that denominator from `runs` and not from the population', () => {
    /*
     * ONE field changed: `attribution.runs` becomes 24 while `population.total` stays 25. The
     * producer counts them differently — `population.total` is the verdict count, `runs` the run
     * count — so they part company on any unpaired verdict. They coincide at 25 on the live bundle,
     * which is exactly how substituting one for the other survives review; here it cannot.
     */
    render(<QualityBlock scorecard={attributed({ runs: 24 })} />);

    expect(text('quality-attribution-ownable')).toContain('14 of 24');
    expect(text('quality-population')).toContain('25 requests');
  });

  it('states a measured zero as the measurement it is, rather than saying nothing', () => {
    render(<QualityBlock scorecard={LIVE} />);

    // `unattributable: 0` on the live bundle: every request that could have an owner found one.
    // Silence here would be indistinguishable from a bundle that never recorded the term.
    expect(text('quality-attribution-owned'))
      .toBe('All 14 of those reached a tenant, so nothing above is counted for somebody who can '
        + 'never see it.');
    expect(screen.queryByTestId('quality-attribution-orphaned')).toBeNull();
  });

  it('will not hide requests counted here for a reader who can never see them', () => {
    /*
     * TWO fields changed together, because they cannot disagree: 11 of the 14 ownable requests lose
     * their owner, so `attributed` falls to 3. Those 11 are conversational, scored, and counted into
     * the 25-request partition and the 80% rate above — and no actor's surface will ever show them.
     *
     * Both denominators are on screen and neither is the population: 11 is over the 14 that could
     * have an owner, and that 14 is over 25.
     */
    render(<QualityBlock scorecard={attributed({ attributed: 3, unattributable: 11 })} />);

    expect(text('quality-attribution-orphaned'))
      .toBe('11 of 14 of those belong to nobody: scored and counted in every figure above, and on '
        + 'no tenant’s own screen, because nothing named an owner for the session.');
    expect(screen.queryByTestId('quality-attribution-owned')).toBeNull();
    expect(text('quality-attribution-ownable')).toContain('14 of 25');
  });

  it('drops the “the rest” clause when there is no rest', () => {
    // TWO fields changed: every one of the 25 requests becomes ownable, and all 25 find an owner.
    // Naming a remainder here would invent a class of work this bundle does not contain.
    render(<QualityBlock scorecard={attributed({ attributed: 25, ownable: 25 })} />);

    expect(text('quality-attribution-ownable')).toBe('25 of 25 requests could have an owner at all');
  });

  it('reports a trio that does not reconcile INSTEAD of reading it', () => {
    /*
     * ONE field changed: `attributed` becomes 13 while `unattributable` stays 0 over 14 ownable.
     * Upstream that is impossible, so it is a producer bug — and it must REPLACE the reading rather
     * than join it: `unattributable: 0` would otherwise print "all 14 reached a tenant" directly
     * above the numbers saying one did not.
     */
    render(<QualityBlock scorecard={attributed({ attributed: 13 })} />);

    expect(text('quality-attribution-mismatch'))
      .toBe('Owned and unowned add to 13, not to the 14 that could have an owner, so one of those '
        + 'three is wrong and none can be read.');
    expect(screen.queryByTestId('quality-attribution-owned')).toBeNull();
    expect(screen.queryByTestId('quality-attribution-orphaned')).toBeNull();
  });

  it('says the term is missing rather than reading any of it as a zero', () => {
    // The key REMOVED, which is the state every bundle written before it is served back in — and
    // `metricRegistry`'s own note says a real emitted bundle has no `attribution` at all. `?? 0`
    // here would report a population where every request has an owner.
    render(<QualityBlock scorecard={without('attribution')} />);

    expect(text('quality-attribution-actors-absent'))
      .toContain('does not say how many tenants');
    expect(text('quality-attribution-absent'))
      .toContain('whether any were counted here for nobody is not recorded');
    expect(screen.queryByTestId('quality-attribution-owned')).toBeNull();
    expect(screen.queryByTestId('quality-attribution-ownable')).toBeNull();
  });

  it('keeps the two facts independent, so one missing count does not hide the other', () => {
    // ONE field removed at a time. A bundle with no `actors` can still answer the ownership
    // question, and one with no `ownable` can still say how many tenants there are — suppressing
    // both because one is absent would hide a measurement that did arrive.
    const noActors = render(<QualityBlock
      scorecard={attributed({ actors: undefined as unknown as number })} />);
    expect(screen.getByTestId('quality-attribution-actors-absent')).toBeInTheDocument();
    expect(text('quality-attribution-ownable')).toContain('14 of 25');
    noActors.unmount();

    render(<QualityBlock scorecard={attributed({ ownable: undefined as unknown as number })} />);
    expect(screen.getByTestId('quality-attribution-absent')).toBeInTheDocument();
    expect(text('quality-attribution-actors')).toContain('2 tenants');
  });

  it('treats a corrupt count as not stated rather than printing it', () => {
    // A negative count, a `NaN` and a string are producer bugs and are none of them a measurement.
    // `-1 of 14` launders one into a figure a reader will believe.
    for (const broken of [{ unattributable: -1 }, { ownable: Number.NaN },
                          { runs: '25' as unknown as number }]) {
      const view = render(<QualityBlock scorecard={attributed(broken)} />);
      expect(screen.getByTestId('quality-attribution-absent'), JSON.stringify(broken))
        .toBeInTheDocument();
      expect(screen.queryByTestId('quality-attribution-ownable')).toBeNull();
      view.unmount();
    }
  });

  it('says nothing can be placed with a tenant rather than dividing by no requests', () => {
    // Every count zeroed: `fractionLabel(0, 0)` answers with a SENTENCE, and `nothing to measure
    // requests could have an owner at all` is not one.
    render(<QualityBlock scorecard={attributed({
      actors: 0, attributed: 0, unattributable: 0, ownable: 0, runs: 0,
    })} />);

    expect(text('quality-attribution-empty')).toContain('count no request at all');
    expect(screen.queryByTestId('quality-attribution-ownable')).toBeNull();
  });

  it('puts no actor id, session id or run id on a shared surface', () => {
    // These five numbers ride the ONE global SNAPSHOT item that every tenant reads. `actors` is a
    // count precisely so that this holds, and an id reaching the screen here is a cross-tenant
    // disclosure rather than a copy defect.
    render(<QualityBlock scorecard={attributed({ attributed: 3, unattributable: 11 })} />);

    // A UUID, and any opaque hex run long enough to be an id rather than a figure. Asserted on the
    // WHOLE block, so a line added later cannot leak one past a per-line check.
    const whole = text('quality-block');
    expect(whole).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/);
    expect(whole).not.toMatch(/[0-9a-f]{12,}/);

    /*
     * And every numeral these lines DO carry is one of the wire's counts — `actors` 2, then `ownable`
     * 14 over `runs` 25, then `unattributable` 11 over that 14. Asserted exhaustively over the
     * attribution lines rather than over the block, so it fails on an id appearing here and not on
     * unrelated copy elsewhere gaining a figure.
     */
    const lines = ['quality-attribution-actors', 'quality-attribution-ownable',
                   'quality-attribution-orphaned'].map(text).join(' ');
    expect(lines.match(/\d+/g)).toEqual(['2', '14', '25', '11', '14']);
  });
});

describe('where the population\'s quality is mounted', () => {
  const data = fixture as unknown as ImproveData;

  it('sits behind Advanced, last of the three engine blocks and above the registry', async () => {
    /*
     * Still placed by SCOPE within that view: the two personal blocks first, then this one, so the
     * scope changes exactly once and a rate mixing every tenant's runs is never sitting among the
     * reader's own figures, where it would be read as theirs.
     */
    render(<ImproveCenter load={() => Promise.resolve({ ...data, scorecard: LIVE })} solo={null} />);

    const advanced = await screen.findByTestId('view-components');
    // Not on the default view: the same population's pass rate is one tile there, and this block's
    // eight qualifying sentences are what an operator needs and a reader does not.
    expect(screen.queryByTestId('quality-block')).toBeNull();
    fireEvent.click(advanced);

    expect(screen.getByTestId('quality-block')).toBeInTheDocument();
    // Fed the loaded bundle: no other payload on this surface produces a 14-of-25 denominator.
    expect(text('quality-tier-dispatch')).toContain('14 of 25');

    const order = [...screen.getByTestId('improve-center').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    const mine = order.indexOf('quality-block');
    expect(mine).toBeGreaterThan(order.indexOf('runs-block'));
    expect(mine).toBeGreaterThan(order.indexOf('waste-block'));
    expect(mine).toBeLessThan(order.indexOf('section-fix-first'));
  });

  it('states the absence on the payload the committed fixture carries', async () => {
    expect(data.scorecard).toBeUndefined();
    render(<ImproveCenter load={() => Promise.resolve(data)} solo={null} />);

    fireEvent.click(await screen.findByTestId('view-components'));
    await waitFor(() => expect(screen.getByTestId('quality-no-scorecard')).toBeInTheDocument());
  });
});
