/**
 * @vitest-environment jsdom
 *
 * What these pin is that the delivered figure cannot be read as something it is not: it carries a
 * word, the word rides the figure rather than a column header, a measured `0 of 1` stays a figure,
 * and a delivery nothing judged shows the hatched sentinel instead of vanishing.
 *
 * The figure is a FRACTION, and these are what hold it to that. `2` of `3` used to reach this cell
 * as `66.66666666666667` and be set verbatim; a count pair cannot round and cannot overstate, and
 * it shows the denominator, which is not the run count beside it.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below
 * are both required. `@testing-library/user-event` is not a dependency; use fireEvent.
 */
import '@testing-library/jest-dom';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ImproveComponents } from './ImproveComponents';
import fixture from './__fixtures__/improveData.fixture.json';
import type { ComponentRow, ComponentsResponse } from '../../improve-api';
import { numericStyle } from '../../styles/tokens';

/** The REAL Σ map the loop emits, committed as a fixture (see `ImproveCenter.test.tsx`). */
const components = fixture.components as unknown as ComponentsResponse;

/**
 * NO row in the committed fixture carries a `health` — all 41 are `null` — so every figure below
 * is supplied.
 *
 * `MEASURED_HEALTH` is transcribed, not invented: an emitted wire artifact outside the repo tree
 * (`tests/evaluation/experiments/improve-wire.json`, untracked, so deliberately not imported)
 * reads `{"runs": 1, "deliveredPct": 0.0}` on this same component — the percentage the wire
 * carried before the counts replaced it, and that pair fixes both counts exactly: a non-null
 * percentage means the one attributing run WAS judged, so the denominator is 1, and `0.0` means it
 * did not deliver. It is the only measured row in a real bundle, and its `0` is what makes the
 * zero-is-not-absent case the realistic one.
 */
const MEASURED_ID = 'chainer-readiness-rule';
const MEASURED_HEALTH: ComponentRow['health'] = { runs: 1, delivered: 0, deliveredOf: 1 };

/**
 * Runs attribute here, but the delivery tier returned PASS or FAIL on none of them. The producer's
 * own documented example of that state (`dto.component_health`: "a row reading `runs: 4,
 * deliveredOf: 0` says four runs attribute here and none of them could be judged on delivery"),
 * which today's harvest happens not to contain.
 */
const UNJUDGED_HEALTH: ComponentRow['health'] = { runs: 4, delivered: 0, deliveredOf: 0 };

/**
 * The share that broke the percentage, kept as a case of its own.
 *
 * `100.0 * 2 / 3` is `66.66666666666667`, and the cell set that verbatim — sixteen significant
 * digits asserted over three runs. Thirds, sixths and sevenths all did it; halves and quarters
 * did not, which is why the defect survived: `1` of `2` read `50%` and looked correct.
 */
const THIRDS_HEALTH: ComponentRow['health'] = { runs: 3, delivered: 2, deliveredOf: 3 };

const allRows = (data: ComponentsResponse): ComponentRow[] => [
  ...data.yours.flatMap((section) => section.components),
  ...data.fixFirst.components, ...data.admin.components, ...data.locked.components,
];

/** The fixture with a health patched onto the named components, wherever they sit. */
const withHealth = (health: Record<string, ComponentRow['health']>): ComponentsResponse => {
  const apply = (c: ComponentRow): ComponentRow => (
    c.componentId in health ? { ...c, health: health[c.componentId] } : c);
  const bucket = (b: ComponentsResponse['fixFirst']) => (
    { ...b, components: b.components.map(apply) });
  return {
    ...components,
    yours: components.yours.map((s) => ({ ...s, components: s.components.map(apply) })),
    fixFirst: bucket(components.fixFirst),
    admin: bucket(components.admin),
    locked: bucket(components.locked),
  };
};

const healthCell = (componentId: string) => screen.getByTestId(`health-${componentId}`);

describe('ImproveComponents delivery figure', () => {
  it('names the figure in the row that carries it, leaving no bare percentage', () => {
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: MEASURED_HEALTH })} />);
    expect(healthCell(MEASURED_ID).textContent).toBe('1 run · delivered 0 of 1');
  });

  it('agrees the run noun with its count, so one run never reads "1 runs"', () => {
    // The real bundle's only measured row has exactly one run, so the singular is the case a
    // reader actually meets, not the corner.
    render(<ImproveComponents data={withHealth({
      [MEASURED_ID]: MEASURED_HEALTH, 'lab-asset-index': UNJUDGED_HEALTH,
    })} />);
    expect(healthCell(MEASURED_ID).textContent).toContain('1 run ');
    expect(healthCell(MEASURED_ID).textContent).not.toContain('1 runs');
    expect(healthCell('lab-asset-index').textContent).toContain('4 runs ');
  });

  it('states the share as a fraction and never as a percentage', () => {
    // `100.0 * 2 / 3` reached this cell as `66.66666666666667` and was set verbatim. A fraction
    // cannot do that, and it names the denominator the percentage hid.
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: THIRDS_HEALTH })} />);
    const cell = healthCell(MEASURED_ID);
    expect(cell.textContent).toBe('3 runs · delivered 2 of 3');
    expect(cell.textContent).not.toContain('%');
    expect(cell.textContent).not.toMatch(/66|\.\d/);
  });

  /**
   * The four shares that show what changed, each with what the percentage rendered beside it.
   *
   * The old figures are not guessed: they are `100.0 * k / n` serialized to JSON by the producer
   * and read back by `String(number)`, which is why `100.0` printed `100` and `2 / 3` printed all
   * sixteen digits. Halves and quarters looked right, which is how this lasted.
   */
  it.each([
    { delivered: 1, deliveredOf: 1, was: '100%', now: '1 run · delivered 1 of 1' },
    { delivered: 2, deliveredOf: 3, was: '66.66666666666667%', now: '3 runs · delivered 2 of 3' },
    { delivered: 5, deliveredOf: 6, was: '83.33333333333333%', now: '6 runs · delivered 5 of 6' },
    { delivered: 2, deliveredOf: 7, was: '28.571428571428573%', now: '7 runs · delivered 2 of 7' },
  ])('renders $delivered of $deliveredOf as a fraction, where it read $was', (
    { delivered, deliveredOf, now },
  ) => {
    // Every attributing run judged here, so `runs` and the denominator agree; the case where
    // they do not is `UNJUDGED_HEALTH` above.
    render(<ImproveComponents data={withHealth({
      [MEASURED_ID]: { runs: deliveredOf, delivered, deliveredOf },
    })} />);
    const cell = healthCell(MEASURED_ID);
    expect(cell.textContent).toBe(now);
    // Neither the old figure nor any part of it survives anywhere in the row.
    expect(cell.textContent).not.toContain('%');
    expect(cell.textContent).not.toContain('.');
  });

  it('takes the denominator from the wire, not from the run count beside it', () => {
    // Four runs attribute, delivery was judged on two of them. The fraction must read `1 of 2`:
    // `1 of 4` would be a claim about runs nothing scored, which is what the percentage's tooltip
    // existed to deny. This is the only row here where the two numbers differ AND the share is
    // measurable, so it is the one that catches a render reaching for `runs`.
    render(<ImproveComponents data={withHealth({
      [MEASURED_ID]: { runs: 4, delivered: 1, deliveredOf: 2 },
    })} />);
    expect(healthCell(MEASURED_ID).textContent).toBe('4 runs · delivered 1 of 2');
  });

  it('keeps a measured 0 a figure, because absence is not zero', () => {
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: MEASURED_HEALTH })} />);
    const cell = healthCell(MEASURED_ID);
    expect(within(cell).getByText('0 of 1')).toBeInTheDocument();
    expect(within(cell).queryByTestId('sentinel')).toBeNull();
  });

  it('shows delivery as not measured when no attributing run was judged on it', () => {
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: UNJUDGED_HEALTH })} />);
    const cell = healthCell(MEASURED_ID);
    // Never silence, and never `0 of 0`: the runs are real, the delivery reading is not. A zero
    // denominator is what says so on the wire, and it must not render as a fraction at all — `0 of
    // 0` invites the reader to compute a rate, and `nothing to measure` would drop the hatching
    // every other absent value on this surface wears.
    expect(cell.textContent).toBe('4 runs · delivered not measured');
    expect(cell.textContent).not.toContain(' of ');
    expect(cell.textContent).not.toContain('nothing to measure');
    expect(within(cell).getByTestId('sentinel')).toBeInTheDocument();
  });

  it('tells an unmeasured delivery apart from a measured zero, and both from no runs at all', () => {
    // Three states, three renderings, and collapsing any two is the failure this row exists to
    // avoid. Asserted together rather than one per test, because what matters is that they DIFFER.
    const patched = { [MEASURED_ID]: MEASURED_HEALTH, 'lab-asset-index': UNJUDGED_HEALTH };
    const noRuns = allRows(components).find((c) => !(c.componentId in patched))!;
    render(<ImproveComponents data={withHealth(patched)} />);
    const rendered = [MEASURED_ID, 'lab-asset-index', noRuns.componentId]
      .map((id) => healthCell(id).textContent);
    expect(rendered).toEqual([
      '1 run · delivered 0 of 1', '4 runs · delivered not measured', 'unavailable',
    ]);
    expect(new Set(rendered).size).toBe(3);
  });

  it('makes no delivery claim at all for a component nothing attributes to', () => {
    const unmeasured = allRows(components).find((c) => c.health === null);
    expect(unmeasured).toBeDefined();
    render(<ImproveComponents data={components} />);
    // The surface's own badge for "there was never a reading to take", not a `-`, which said
    // nothing at all on all 41 rows and left the two absence treatments beside it unused.
    expect(healthCell(unmeasured!.componentId).textContent).toBe('unavailable');
  });

  it('hangs the explanation on the figure alone, and says the run count is not its denominator',
    () => {
      render(<ImproveComponents data={withHealth({ [MEASURED_ID]: MEASURED_HEALTH })} />);
      const labelled = within(healthCell(MEASURED_ID)).getByTitle(/denominator/);
      // The run count is outside it: the tooltip describes the share, not the whole cell.
      expect(labelled.textContent).toBe('delivered 0 of 1');
      expect(labelled.getAttribute('title')).toMatch(/failing runs/);
    });

  it('sets the figure in mono and the word beside it in body text', () => {
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: MEASURED_HEALTH })} />);
    const cell = healthCell(MEASURED_ID);
    expect(within(cell).getByText('0 of 1').style.fontFamily).toBe(numericStyle.fontFamily);
    // The word must not inherit the tabular mono column treatment.
    expect(within(cell).getByTitle(/denominator/).style.fontFamily).toBe('');
  });

  it('labels once per measured row and nowhere else — there is no column header to carry it', () => {
    const measured = { [MEASURED_ID]: MEASURED_HEALTH, 'lab-asset-index': UNJUDGED_HEALTH };
    const data = withHealth(measured);
    render(<ImproveComponents data={data} />);
    const rows = allRows(data);
    expect(rows.filter((c) => c.health !== null)).toHaveLength(2);
    expect(screen.queryAllByText(/delivered/)).toHaveLength(2);
    expect(screen.queryAllByTitle(/denominator/)).toHaveLength(2);
    // The other 39 rows make no delivery claim, and say which absence that is.
    expect(rows).toHaveLength(41);
    expect(screen.getByTestId('health-slack-file-ingest').textContent).toBe('unavailable');
  });
});

describe('ImproveComponents absence treatments', () => {
  it('wears the surface\'s own badge where nothing attributes, on every such row', () => {
    // All 41 live rows carry `health: null`, so this is not a corner: it is the whole column. A
    // `-` is neither of the two treatments this surface defines and reads as a blank cell.
    render(<ImproveComponents data={components} />);
    expect(allRows(components).filter((c) => c.health === null)).toHaveLength(41);
    expect(screen.queryAllByTestId('unavailable-badge')).toHaveLength(41);
    // No health cell wears the hatched sentinel: with `health: null` on every row, the
    // `deliveredOf === 0` branch is unreachable from live data and this is what says so. Scoped to
    // the cells, because `VersionBadge` renders its own sentinel elsewhere in the same row.
    for (const row of allRows(components)) {
      expect(within(healthCell(row.componentId)).queryByTestId('sentinel')).toBeNull();
    }
  });

  it('says why the badge is there rather than leaving a reader to guess', () => {
    render(<ImproveComponents data={components} />);
    const cell = healthCell('slack-file-ingest');
    // `UnavailableBadge` carries no tooltip of its own by design, so the caller must hang the
    // reason on what encloses it. Nothing about the badge alone says which absence this is.
    expect(within(cell).getByTitle(/No run in this population attributes/)).toBeInTheDocument();
  });

  it('keeps the two absences apart: never a reading, against a reading that came back empty', () => {
    render(<ImproveComponents data={withHealth({ [MEASURED_ID]: UNJUDGED_HEALTH })} />);
    // Same row shape, different facts: runs attribute here and delivery could not be judged
    // (hatched sentinel), against nothing attributing at all (solid badge). One treatment for
    // both would merge "we looked and could not tell" into "there was nothing to look at".
    expect(within(healthCell(MEASURED_ID)).getByTestId('sentinel')).toBeInTheDocument();
    expect(within(healthCell(MEASURED_ID)).queryByTestId('unavailable-badge')).toBeNull();
    expect(within(healthCell('slack-file-ingest')).getByTestId('unavailable-badge'))
      .toBeInTheDocument();
  });
});

describe('ImproveComponents permission wording', () => {
  /**
   * `N/A` is live on this row and on no other served row, and it is the registry's own value: the
   * component is `data_or_code: GENERATED`, produced per run by the design agent, so there is
   * nothing stored to permit. It is off the `Mutability` union the wire declares, which is why the
   * default was reached at all.
   */
  const N_A_ID = 'dag-node-prompts';

  it('reads an inapplicable permission as inapplicable, never as unassessed', () => {
    render(<ImproveComponents data={components} />);
    const row = screen.getByTestId(`component-${N_A_ID}`);
    // `PERMISSION_LABEL[…] ?? 'not assessed'` put default-deny's own wording on a row the registry
    // HAD assessed — under a bucket headed "Managed by admins", which is two contradictory claims
    // about one component.
    expect(row.textContent).toContain('not applicable');
    expect(row.textContent).not.toContain('not assessed');
    expect(row.textContent).not.toContain('N/A');
  });

  it('still reads an unrecorded permission as unassessed, which is what default-deny is about', () => {
    // The wording the row above stole. 13 served rows are genuinely UNRECORDED and must keep it.
    render(<ImproveComponents data={components} />);
    expect(screen.getByTestId('component-slack-session-derivation').textContent)
      .toContain('not assessed');
  });

  it('explains on the row why inapplicable is not unassessed', () => {
    render(<ImproveComponents data={components} />);
    expect(within(screen.getByTestId(`component-${N_A_ID}`))
      .getByTitle(/permission does not apply/).textContent).toBe('not applicable');
  });

  it('shows a value it cannot translate as the registry spells it, not as a decision', () => {
    /*
     * One field changed on one real row: `mutability` becomes `BUG`, which the registry really
     * carries (on `node-model-selector`, a component the row cap keeps off this surface today) and
     * which the `Mutability` union does not name either. Inventing a phrase for it would be
     * guessing; the old default reported it as assessed-and-undecided, which is a claim.
     */
    const patched: ComponentsResponse = {
      ...components,
      locked: {
        ...components.locked,
        components: components.locked.components.map((c, i) => (
          i === 0 ? { ...c, mutability: 'BUG' as ComponentRow['mutability'] } : c)),
      },
    };
    const id = components.locked.components[0].componentId;
    render(<ImproveComponents data={patched} />);
    const row = screen.getByTestId(`component-${id}`);
    expect(row.textContent).toContain('BUG');
    expect(row.textContent).not.toContain('not assessed');
    expect(within(row).getByTitle(/no phrase for the registry value/)).toBeInTheDocument();
  });
});

describe('ImproveComponents source location', () => {
  it('names where every component lives, which the fix-first bucket otherwise withholds', () => {
    // 24 components live say the fix belongs to an engineer and the surface named the place for
    // none of them, though the wire carries one on all 41 rows.
    render(<ImproveComponents data={components} />);
    expect(allRows(components).filter((c) => c.sourceLocation)).toHaveLength(41);
    for (const row of allRows(components)) {
      expect(screen.getByTestId(`location-${row.componentId}`).textContent)
        .toBe(row.sourceLocation);
    }
  });

  it('prints the producer\'s string as written, including the long multi-file ones', () => {
    // Not abridged and not reformatted. This is the field a reader opens an editor with, so a cut
    // one reads as corruption — and these are not all short: the longest here is 101 characters
    // naming three files and a role each, and the longest served live is 110.
    render(<ImproveComponents data={components} />);
    expect(screen.getByTestId('location-chainer-readiness-rule').textContent)
      .toBe('infra/lib/lambdas/plan-chainer/handler.py:432-470');
    const longest = 'custom_agent_resolver.py:526,568; storage lab/specialty_crud.py:626,683; '
      + 'read task_agent.py:1913,1946';
    expect(longest).toHaveLength(101);
    expect(screen.getByTestId('location-custom-agent-persona').textContent).toBe(longest);
    for (const row of allRows(components)) {
      expect(screen.getByTestId(`location-${row.componentId}`).textContent).not.toContain('…');
    }
  });

  it('says the registry named no location rather than rendering an empty line', () => {
    // One field changed on one real row: `sourceLocation` becomes `''`. Every served row has one
    // today, so this is the state no screenshot can show — and an empty line reads as a fault.
    const id = components.locked.components[0].componentId;
    const patched: ComponentsResponse = {
      ...components,
      locked: {
        ...components.locked,
        components: components.locked.components.map((c, i) => (
          i === 0 ? { ...c, sourceLocation: '' } : c)),
      },
    };
    render(<ImproveComponents data={patched} />);
    expect(screen.getByTestId(`location-${id}`).textContent)
      .toBe('the registry names no location for this');
  });
});

describe('ImproveComponents bucket bound', () => {
  it('states the cut without promising a load that no route performs', () => {
    /*
     * The line used to end "the rest load with the full registry". There is no second fetch on this
     * surface and no route to write one against: `componentCount` is 80, 41 rows are served, and the
     * other 39 are reachable from nowhere. A promise a surface cannot keep is worse than the bound.
     */
    render(<ImproveComponents data={components} />);
    const capped = screen.getByTestId('section-admin-capped');
    expect(capped.textContent).toBe('showing 12 of 39 — the rest are not in this report. '
      + 'Rows carrying a suggestion come first, so the cut is not arbitrary.');
    expect(capped.textContent).not.toContain('load');
    expect(capped.getAttribute('title')).toContain('fits the size limit of the one item');
  });

  it('says nothing about a cut where there is none', () => {
    // `locked` serves all 8 of its 8, so a line claiming a remainder would invent one.
    render(<ImproveComponents data={components} />);
    expect(components.locked.components).toHaveLength(components.locked.count);
    expect(screen.queryByTestId('section-locked-capped')).toBeNull();
    expect(screen.getByTestId('section-fix-first-capped').textContent)
      .toContain('showing 12 of 24');
  });
});

