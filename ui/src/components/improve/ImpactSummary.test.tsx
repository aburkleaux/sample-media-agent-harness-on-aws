/**
 * @vitest-environment jsdom
 *
 * The one sentence under the two figures, and the table behind `Details`.
 *
 * As in `OutcomeBand.test.tsx`, every fixture is SHAPED on the live staging bundle and no figure is
 * asserted as a live figure — those numbers moved by more than a factor of two inside 24 hours. What
 * is asserted is the wording each shape selects and the formatting of a given input.
 *
 * The `plainSummary` strings below are the producer's own, verbatim from `dto._WASTE_KIND_PROSE`.
 * They are longer than they look in a mock — sixty characters for one of them — which is why the row
 * label wraps rather than clipping.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required. `@testing-library/user-event` is not a dependency, so this uses `fireEvent`.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ImpactSummary } from './ImpactSummary';
import type { Scorecard, WasteTotals } from '../../improve-api';

/** The producer's sentences for the three kinds, as they arrive on the wire. */
const REPEATED = 'The same series of steps ran more than once';
const ABANDONED = "A job's result was never collected before the request ended";
const RETRIED = 'A step failed and ran again';

/**
 * The live `byKind` block, shaped on staging.
 *
 * The point of these three: `error_then_retry` has the MOST findings and by two orders of magnitude
 * the LEAST cost — five findings at about two milliseconds each. Ordered by count it would lead the
 * table with the noise; ordered by time lost it sits last, which is what the reader needs.
 */
const BY_KIND = {
  repeated_sequence: {
    findings: 3, runs: 3, calls: 12,
    wallMs: { sum: 140_994, measuredOf: 3, total: 3 },
    plainSummary: REPEATED,
  },
  abandoned_dispatch: {
    findings: 1, runs: 1, calls: 1,
    wallMs: { sum: 68_286, measuredOf: 1, total: 1 },
    plainSummary: ABANDONED,
  },
  error_then_retry: {
    findings: 5, runs: 1, calls: 5,
    wallMs: { sum: 10, measuredOf: 1, total: 1 },
    plainSummary: RETRIED,
  },
};

function scorecard(over: Record<string, unknown> = {}): Scorecard {
  return {
    population: { total: 25, pass: 16, fail: 7, unscored: 2 },
    passRate: 16 / 23,
    ...over,
  } as unknown as Scorecard;
}

function waste(over: Record<string, unknown> = {}): WasteTotals {
  return {
    runsWithWaste: 4,
    runs: 25,
    runsNotScanned: 0,
    totalCalls: 18,
    totalWallMs: { sum: 209_290, measuredOf: 9, total: 9 },
    byKind: BY_KIND,
    ...over,
  } as unknown as WasteTotals;
}

const text = (testId: string): string => screen.getByTestId(testId).textContent ?? '';

/** The table's body rows, as `[label, time, calls]`. */
function rows(): string[][] {
  return [...screen.getByTestId('impact-table').querySelectorAll('tbody tr')]
    .map((row) => [...row.children].map((cell) => cell.textContent ?? ''));
}

function open(): void {
  fireEvent.click(screen.getByTestId('impact-details-toggle'));
}

// --- the sentence -----------------------------------------------------------
/**
 * The sentence's whole job is what the two tiles cannot say, so what is asserted here is mostly what
 * it does NOT contain. Every figure it used to carry — `16 of 23`, `4 of 25`, `3m 29s` — is a reading
 * on a tile one line higher, and the two fractions have different denominators for a correct reason
 * (the rate is over the judged requests, the scan over the whole population) that read as a bug when
 * the two sat in adjacent clauses.
 */
describe('the one sentence', () => {
  it('interprets the two tiles rather than restating them, and answers "is this mine?"', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);

    expect(text('impact-sentence')).toBe('A request can redo work and still finish cleanly.');
    expect(text('impact-blame')).toBe('Nothing here for you to fix.');
  });

  it('prints no figure at all, which is what leaves the two denominators nothing to clash on', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);

    const prose = text('impact-sentence') + text('impact-blame');
    expect(prose).not.toMatch(/\d/);
    // Named as well as counted, because these three are the specific readings that were doubled.
    for (const figure of ['16 of 23', '4 of 25', '3m 29s', '70%']) {
      expect(prose, figure).not.toContain(figure);
    }
    // And no styled figure span survives inside it, which is the shape a figure would come back in.
    expect(screen.getByTestId('impact-sentence').querySelectorAll('span')).toHaveLength(0);
  });

  it('reads the same whether or not the two populations agree', () => {
    /*
     * The scorecard's JUDGED set and the scan's population are separate counts off separate folds —
     * 23 against 25 on today's bundle, equal on a bundle with nothing unscored. The sentence used to
     * change shape between those two, which is how it came to hold two denominators at once; now it
     * has no denominator to choose, so the payload cannot move it.
     */
    const { rerender } = render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    const differing = text('impact-sentence');

    rerender(<ImpactSummary
      scorecard={scorecard({ population: { total: 25, pass: 18, fail: 7, unscored: 0 } })}
      waste={waste()}
    />);
    expect(text('impact-sentence')).toBe(differing);
  });

  it('says nothing about a clean finish when nothing in the report was judged', () => {
    // The contrast it draws needs a clean-finish reading to draw it against, and an unjudged
    // population has none — `0 of 0` is not a rate of nothing, it is nothing measured. The tile above
    // says exactly that, so this line stands down and the attribution answer stays.
    render(<ImpactSummary
      scorecard={scorecard({ population: { total: 4, pass: 0, fail: 0, unscored: 4 },
                             passRate: null })}
      waste={waste()}
    />);

    expect(screen.queryByTestId('impact-sentence')).toBeNull();
    expect(text('impact-blame')).toBe('Nothing here for you to fix.');
  });

  it('says nothing about a clean finish with no scorecard at all, and still opens the table', () => {
    // The two keys arrive independently — the producer and this SPA are separate artifacts — so
    // "the scan is here and the scorecard is not" is a real payload, not a hypothetical.
    render(<ImpactSummary waste={waste()} />);

    expect(screen.queryByTestId('impact-sentence')).toBeNull();
    expect(screen.getByTestId('impact-details-toggle')).toBeInTheDocument();
  });

  it('states the finding itself when the report carries no totals behind it', () => {
    /*
     * The one branch where this line is the ONLY place the finding appears: `OutcomeBand` needs
     * `totalWallMs` to render its tile at all, so with the totals block missing the band shows an
     * absence and no count. Saying nothing here would leave a reader with no way to tell a report
     * that found redone work from one that found none.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste({ totalWallMs: undefined })} />);

    expect(text('impact-sentence'))
      .toBe('Work was redone or abandoned, and nothing recorded how long all of it took.');
  });

  it('says the TOTAL is what nothing recorded, because the rows can still be timed', () => {
    // `totalWallMs` and a kind's own `wallMs` are separate terms on the wire, so a block can lose the
    // sum and keep the parts. "Nothing recorded how long that took" would then be contradicted by the
    // table two lines below it.
    render(<ImpactSummary scorecard={scorecard()} waste={waste({ totalWallMs: undefined })} />);
    open();

    expect(text('impact-sentence')).toContain('how long all of it took');
    expect(rows()[0][1]).toBe('2m 21s');
  });

  it('does not dress an unmeasured duration as prose, and needs no sentinel to do it', () => {
    /*
     * `spent not measured redoing work` is not a sentence, and that is the whole reason this used to
     * branch on the sentinel. With no duration in the sentence there is no clause for one to finish:
     * the tile above keeps the hatched badge, which is where an unmeasured value belongs.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 9 },
    })} />);

    expect(text('impact-sentence')).toBe('A request can redo work and still finish cleanly.');
    // The table is closed, so any sentinel on screen would be one this sentence put there.
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('words a clean scan as a finding rather than a reading, and blames nobody', () => {
    // The tile shows `0ms` or the hatched badge, and neither reads as "we looked": one reads as
    // instant and the other as unknown. And with nothing lost there is nothing to attribute, so the
    // second sentence has no subject and does not appear.
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      runsWithWaste: 0,
      totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 },
      byKind: {},
    })} />);

    expect(text('impact-sentence')).toBe('Nothing was redone or abandoned.');
    expect(screen.queryByTestId('impact-blame')).toBeNull();
  });

  it('adds nothing when the scorecard is the only readable half', () => {
    /*
     * The clean-finish reading is the TILE's, whole: its label, its percentage, its fraction and its
     * unjudged count. With no scan there is nothing left for this component to interpret, so it
     * renders nothing rather than saying the tile over again in words.
     */
    const { container } = render(<ImpactSummary scorecard={scorecard()} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing at all when neither key can be read', () => {
    // Rather than an empty line or a lone `Details` control with nothing behind it. The tiles above
    // already say which key is missing and why.
    const { container } = render(<ImpactSummary />);
    expect(container.firstChild).toBeNull();
  });

  it('keeps the table when the scan\'s counts cannot be read but its kinds can', () => {
    /*
     * The counts gate the SENTENCE and not the disclosure. `runsWithWaste` without the population it
     * was counted over is not a count of anything, so no line is drawn from it — but the kinds are a
     * separate reading, and this table is the only place on the surface they appear.
     */
    render(<ImpactSummary
      scorecard={scorecard()}
      waste={waste({ runsWithWaste: undefined, runs: undefined })}
    />);

    expect(screen.queryByTestId('impact-sentence')).toBeNull();
    expect(screen.queryByTestId('impact-blame')).toBeNull();
    open();
    expect(rows().map((row) => row[0])).toEqual([REPEATED, ABANDONED, RETRIED]);
    // No totals row: the block's own totals are the thing that could not be read.
    expect(screen.queryByTestId('impact-total')).toBeNull();
  });

  /**
   * A figure in prose is set in the body face and emphasised by weight; a figure in a column keeps
   * the numeric spine.
   *
   * `numericStyle` is mono plus `tabular-nums`, and it used to wrap whole phrases in the sentence
   * rather than bare figures: `fractionLabel` returns `16 of 23`, so the word `of` came out mono. The
   * sentence carries no figure at all now, and the one figure left in prose here is the count of
   * unnamed kinds under the table — so that is where the rule is pinned.
   *
   * Asserted through the inline `style` rather than a computed font, because jsdom resolves no
   * `var(--font-mono)` and would report both faces identically.
   */
  const familyOf = (el: Element) => (el as HTMLElement).style.fontFamily;

  it('sets the one figure left in prose in the body face, not the numeric spine', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        ...BY_KIND,
        polled_without_backoff: { findings: 2, runs: 2, calls: 9,
                                  wallMs: { sum: 4_000, measuredOf: 2, total: 2 } },
      },
    })} />);
    open();

    const spans = [...screen.getByTestId('impact-unnamed').querySelectorAll('span[style]')];
    expect(spans).toHaveLength(1);
    for (const span of spans) {
      expect(familyOf(span), span.textContent ?? '').not.toContain('--font-mono');
      expect(familyOf(span), span.textContent ?? '').toContain('--font-body');
      // Weight is what carries the emphasis the mono face used to.
      expect((span as HTMLElement).style.fontWeight, span.textContent ?? '').toBe('600');
    }
  });

  it('leaves the sentinel badge in mono, because it is a badge and not a figure', () => {
    /*
     * The one exemption, pinned so the rule above cannot be widened into it by accident. An
     * unmeasured value is not set as a value at all: it gets the hatched badge every unmeasured value
     * on this surface gets, in the mono all of them share. A sentinel dressed in the body face would
     * read as a phrase somebody wrote.
     */
    render(<ImpactSummary
      scorecard={scorecard()}
      waste={waste({ totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 9 } })}
    />);
    open();

    expect(familyOf(screen.getByTestId('impact-total').querySelector('[data-testid="sentinel"]')!))
      .toContain('--font-mono');
  });

  it('keeps the numeric spine in the table, which IS a column', () => {
    // The boundary, in one test: the same file sets figures two ways on purpose, and a change that
    // "tidied" the table's cells to match the sentence would take the alignment with it.
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    const cells = [...screen.getByTestId('impact-table').querySelectorAll('td')];
    expect(cells.length).toBeGreaterThan(0);
    for (const cell of cells) {
      expect((cell as HTMLElement).style.fontFamily).toContain('--font-mono');
      expect((cell as HTMLElement).style.fontVariantNumeric).toBe('tabular-nums');
    }
  });
});

// --- the table --------------------------------------------------------------
describe('the impact table', () => {
  it('stays behind Details until somebody asks for it', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);

    expect(screen.queryByTestId('impact-table')).toBeNull();
    const toggle = screen.getByTestId('impact-details-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(screen.getByTestId('impact-table')).toBeInTheDocument();
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    // Toggles, so the control that opened it is the way back.
    fireEvent.click(toggle);
    expect(screen.queryByTestId('impact-table')).toBeNull();
  });

  it('offers no Details control when there is no row behind it', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      runsWithWaste: 0, byKind: {},
    })} />);
    expect(screen.queryByTestId('impact-details-toggle')).toBeNull();
  });

  it('orders rows by time lost, so the noisiest kind does not lead', () => {
    /*
     * Ordered by findings this would read retry (5), repeat (3), abandon (1) — leading with ten
     * milliseconds of cost. Ordered by time lost the reader meets the two and a bit minutes first.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    expect(rows()).toEqual([
      [REPEATED, '2m 21s', '12'],
      [ABANDONED, '1m 8s', '1'],
      [RETRIED, '10ms', '5'],
    ]);
  });

  it('takes each row\'s label off the wire and never shows the engine\'s key', () => {
    // The keys are producer identifiers. `repeated_sequence` reaching a freelance editor is the
    // defect the producer's prose table exists to prevent, so nothing here falls back to one.
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    const whole = text('impact-summary');
    for (const kind of Object.keys(BY_KIND)) expect(whole).not.toContain(kind);
    expect(whole).not.toContain('_');
  });

  it('omits a kind with no sentence rather than naming it by its key, and says it did', () => {
    /*
     * `plainSummary` is optional on the wire and this SPA deploys separately from the producer, so a
     * kind the detectors have and the prose table does not is a state that really occurs. A table
     * short of a kind is a table whose rows do not add up to the total, and the reader is owed the
     * reason.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        ...BY_KIND,
        polled_without_backoff: {
          findings: 2, runs: 2, calls: 9,
          wallMs: { sum: 4_000, measuredOf: 2, total: 2 },
        },
      },
    })} />);
    open();

    expect(rows()).toHaveLength(3);
    expect(text('impact-summary')).not.toContain('polled_without_backoff');
    expect(text('impact-unnamed')).toBe(
      '1 more kind of redone work has no plain-English name in this report yet, so it is left out '
      + 'of the table above rather than shown by an internal name.');
  });

  it('pluralises that notice, and says nothing when every kind is named', () => {
    const { rerender } = render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        ...BY_KIND,
        polled_without_backoff: { findings: 2, runs: 2, calls: 9,
                                  wallMs: { sum: 4_000, measuredOf: 2, total: 2 } },
        slept_on_a_lock: { findings: 1, runs: 1, calls: 1,
                           wallMs: { sum: 900, measuredOf: 1, total: 1 } },
      },
    })} />);
    open();
    expect(text('impact-unnamed')).toContain('2 more kinds of redone work have no plain-English');
    expect(text('impact-unnamed')).toContain('they are left out');

    rerender(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    expect(screen.queryByTestId('impact-unnamed')).toBeNull();
  });

  it('leaves out a kind that found nothing, and does not count it as unnamed', () => {
    // The producer emits every kind it HAS, with zeros when it found nothing. Three zero rows under
    // a column headed "What happened" would claim three things happened.
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        ...BY_KIND,
        error_then_retry: { findings: 0, runs: 0, calls: 0,
                            wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 },
                            plainSummary: RETRIED },
      },
    })} />);
    open();

    expect(rows().map((row) => row[0])).toEqual([REPEATED, ABANDONED]);
    expect(screen.queryByTestId('impact-unnamed')).toBeNull();
  });

  it('keeps a kind whose finding count cannot be read, because that is not "none"', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        repeated_sequence: { findings: 'UNMEASURED', runs: 3, calls: 12,
                             wallMs: { sum: 140_994, measuredOf: 3, total: 3 },
                             plainSummary: REPEATED },
      },
    })} />);
    open();

    expect(rows().map((row) => row[0])).toEqual([REPEATED]);
  });

  it('sorts an untimed row last AND marks it, so its place is not a claim about its size', () => {
    // Sorted in as a zero it would assert it was the smallest of these. The hatched sentinel in its
    // own cell is what says the position carries no such claim.
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        ...BY_KIND,
        abandoned_dispatch: { findings: 1, runs: 1, calls: 1,
                              wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 1 },
                              plainSummary: ABANDONED },
      },
    })} />);
    open();

    expect(rows().map((row) => row[0])).toEqual([REPEATED, RETRIED, ABANDONED]);
    expect(rows()[2][1]).toBe('not measured');
  });

  it('reconciles its totals row with the block\'s own totals', () => {
    // The columns need something to be read against, and these are the two figures the producer
    // published for exactly that: `totalWallMs` and `totalCalls`.
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    const total = [...screen.getByTestId('impact-total').children]
      .map((cell) => cell.textContent ?? '');
    expect(total).toEqual(['All of it', '3m 29s', '18']);
  });

  it('formats every duration through the shared formatter, never as a literal', () => {
    /*
     * `formatMs` has twice been fixed for carrying a rounded remainder into the next unit. A
     * hand-written duration reintroduces that bug the day the figure moves — and it moves nightly.
     * The table is where the durations live now that the sentence states none.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 3_599_999, measuredOf: 9, total: 9 },
    })} />);
    open();

    const cell = screen.getByTestId('impact-total').children[1].textContent ?? '';
    expect(cell).toBe('60m 0s');
    expect(cell).not.toBe('59m 60s');
  });

  it('says on the total that a shared call is counted once, so the rows can add up to more', () => {
    // `byKind[*].calls` are exact for their kind and `totalCalls` is the union over runs, so a call
    // an errored attempt and a repeated block both charged appears in two rows and once here.
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    const cells = screen.getByTestId('impact-total').children;
    expect(cells[2].getAttribute('title')).toContain('counted once here');
  });

  it('names the three columns and keeps the figures in a column of their own', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    expect([...screen.getByTestId('impact-table').querySelectorAll('th[scope="col"]')]
      .map((cell) => cell.textContent)).toEqual(['What happened', 'Time lost', 'Calls']);
  });

  it('lets the producer\'s own sentence wrap rather than clipping it', () => {
    // The longest of the three runs to sixty characters, and it is the only text on this surface
    // that says what the row IS — so a clipped label loses the whole row.
    render(<ImpactSummary scorecard={scorecard()} waste={waste()} />);
    open();

    const label = screen.getByTestId('impact-row-abandoned_dispatch')
      .querySelector('th') as HTMLElement;
    expect(label.textContent).toBe(ABANDONED);
    expect(label.style.whiteSpace).not.toBe('nowrap');
  });

  it('shows the sentinel for a call count that is not one', () => {
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      byKind: {
        repeated_sequence: { findings: 3, runs: 3, calls: 'UNMEASURED',
                             wallMs: { sum: 140_994, measuredOf: 3, total: 3 },
                             plainSummary: REPEATED },
      },
    })} />);
    open();

    expect(rows()[0][2]).toBe('unknown');
  });
});

// --- what may not travel ----------------------------------------------------
describe('tenant isolation', () => {
  it('renders no session id and no tool sequence, even when the block carries findings', () => {
    /*
     * A waste finding's `detail` names the tool sequence it observed, and a tool name from a
     * user-registered MCP server is user-derived — which is why it lives on the per-actor bundle and
     * not here. This surface reads `byKind` only, so a `findings` array reaching it must still put
     * nothing on screen.
     */
    render(<ImpactSummary scorecard={scorecard()} waste={waste({
      findings: [{ runId: '68e153d0-f0b1-7072-91bc-84425b517126', kind: 'repeated_sequence',
                   calls: 6, wallMs: 140_994, occurrences: 2,
                   detail: 'run_model x6 -> dispatch_tool', confidence: 'OBSERVED: …' }],
    })} />);
    open();

    const whole = text('impact-summary');
    expect(whole).not.toContain('68e153d0');
    expect(whole).not.toContain('run_model');
    expect(whole).not.toContain('->');
    expect(whole).not.toContain('dispatch_tool');
  });
});
