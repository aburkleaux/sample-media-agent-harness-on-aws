/**
 * @vitest-environment jsdom
 *
 * What the default view is allowed to be: four things in one order, and a ceiling on how much prose
 * a reader has to get through to reach the last of them.
 *
 * **The ceiling is the point of this file.** The surface it guards had grown to roughly eleven facts
 * against fourteen qualifying sentences, in the harness's own vocabulary, with the recommendations
 * fifth and below the fold — and it got there one true sentence at a time. Every one of those
 * sentences was individually accurate, which is what made each of them impossible to refuse; nothing
 * measured the total. "Honest" can be verified per sentence and "simple" cannot, so without a
 * measured ceiling the pressure runs one way for ever.
 *
 * So the budget is an assertion, not a preference. A surface that grows past it has regressed even if
 * every added sentence is true, and the remedy is to move something behind **Advanced** rather than
 * to raise the number here.
 *
 * A SENTENCE is a run of prose ending in `.`, `!` or `?`. Labels, figures, fractions and table cells
 * end in none of those and are not counted — and the two kinds the brief excludes outright, tile
 * labels and table rows, are stripped before counting as well, so the measure does not quietly change
 * meaning the day a label gains a full stop.
 *
 * That definition has one consequence worth naming: the SUGGESTION CARDS, which are the longest
 * reader-facing text on this page, were escaping it. Their strings mostly carry no terminal
 * punctuation, and the payloads here left the one card line that does end in a full stop unrendered.
 * `populated` below closes that, so the ceiling now governs the part of the page most able to blow it.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required. `@testing-library/user-event` is not a dependency, so this uses `fireEvent`.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ImproveCenter, type ImproveData } from './ImproveCenter';
import fixture from './__fixtures__/improveData.fixture.json';
import type { ActorBundle } from '../../improve-api';

vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));

/**
 * The ceiling, in sentences, on the default view's reader-facing prose.
 *
 * Eight, and the number does not move — what moves is the headroom under it. Four of those eight used
 * to go to the spend block: with no personal bundle it collapsed to one cause plus the producer's own
 * four-sentence account of it, in the harness's vocabulary, above the tiles that do have figures. That
 * cause now says itself in ONE phrase and keeps the account on the line as a tooltip, and the
 * one-liner under the outcome tiles has since given up the two clauses that only restated them — so
 * the same payload measures THREE sentences rather than seven.
 *
 * The ceiling stays where it is deliberately. It is a limit on what a reader has to get through, not a
 * quota to refill, and holding it while the surface shrinks is the only way the headroom is worth
 * anything. `SPEND_BUDGET` and `OWNED_BUDGET` below are what pin the two reductions in place; the
 * ceiling on its own would let either of them creep back and still pass.
 */
const SENTENCE_BUDGET = 8;

/**
 * And a tighter one on the two bands this file's subject actually owns.
 *
 * TWO, down from three. Both of the one-liner's figure clauses were readings the tiles above already
 * carry — and carrying them twice is what put two different denominators, `16 of 23` and `4 of 25`, in
 * adjacent clauses and got the report filed as a bug. What is left is one sentence that says what the
 * two tiles do not, and one that answers whether any of it is the reader's to act on. A third means a
 * fact has been added that belongs behind Advanced, or a figure has come back.
 */
const OWNED_BUDGET = 2;

/**
 * What the spend block may spend on a reader with no figures of their own, which is most readers.
 *
 * One. A ceiling of eight cannot see this on its own — four sentences could come back here and the
 * page would still pass — so the piece that was the whole defect gets its own number. It is not a
 * budget for prose about the absence; it is the phrase, and nothing under it.
 */
const SPEND_BUDGET = 1;

const data = fixture as unknown as ImproveData;

/**
 * The live bundle's SHAPE, with no personal bundle — which is the state most readers are in, because
 * the producer writes one only for an actor who had a session in the harvest window.
 *
 * Figures shaped on staging and none of them asserted as a live figure: they moved by more than a
 * factor of two inside 24 hours. `unscored` is non-zero and `history` has two rows, so both of the
 * shapes that decide a branch here are the live ones.
 */
const live: ImproveData = {
  ...data,
  scorecard: {
    population: { total: 25, pass: 16, fail: 7, unscored: 2 },
    passRate: 16 / 23,
  },
  waste: {
    runsWithWaste: 4, runs: 25, runsNotScanned: 0, totalCalls: 18,
    totalWallMs: { sum: 209_290, measuredOf: 9, total: 9 },
    byKind: {
      repeated_sequence: {
        findings: 3, runs: 3, calls: 12,
        wallMs: { sum: 140_994, measuredOf: 3, total: 3 },
        plainSummary: 'The same series of steps ran more than once',
      },
      abandoned_dispatch: {
        findings: 1, runs: 1, calls: 1,
        wallMs: { sum: 68_286, measuredOf: 1, total: 1 },
        plainSummary: "A job's result was never collected before the request ended",
      },
      error_then_retry: {
        findings: 5, runs: 1, calls: 5,
        wallMs: { sum: 10, measuredOf: 1, total: 1 },
        plainSummary: 'A step failed and ran again',
      },
    },
  },
  history: [
    { date: '2026-09-03', passRate: 0.8, runsWithWaste: 9 },
    { date: '2026-09-04', passRate: 16 / 23, runsWithWaste: 4 },
  ],
} as unknown as ImproveData;

/** The same payload plus a bundle of the reader's own, which turns the spend block's collapsed cause
 *  back into three figures. The budget has to hold for a reader the producer HAS seen too. */
const withActor: ImproveData = {
  ...live,
  actor: {
    runs: [],
    runsTotal: 0,
    evidence: {},
    history: [{ date: '2026-09-04', runs: 2, tokens: 6_573_271, llmCalls: 127,
                cost: 'UNMEASURED',
                coverage: { total: 2, tokens: 2, llmCalls: 2, latencyMs: 2, cost: 0,
                            costTokensPriced: 0, costTokensUnpriced: 0 } }],
    waste: { runsWithWaste: 0, runs: 2, runsNotScanned: 0, totalCalls: 0,
             totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 2 },
             byKind: {}, findings: [] },
    patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
  } as unknown as ActorBundle,
};

/**
 * Elements whose text is NOT prose, removed before the count.
 *
 * A tile's label, figure and footnote and a bar's direct label are the reading itself; a table is
 * rows of data. None of them is a sentence a reader has to get through, and the brief excludes the
 * first and the last by name — so they come out here rather than being relied on to carry no full
 * stop.
 */
const NOT_PROSE = ['[data-testid="metric-tile-label"]', '[data-testid="metric-tile-value"]',
  '[data-testid="metric-tile-sub"]', '[data-testid="ratio-bar-label"]', 'table'];

/**
 * Block-level elements, whose boundaries end a line.
 *
 * jsdom implements no layout and no `innerText`, so `textContent` runs two block elements' text
 * together — `…switch meanwhile.Your runs…` — and a naive split on "full stop then whitespace" then
 * reads two sentences as one. Inserting the break the layout would have is what makes the count
 * match what a reader sees.
 */
const BLOCK = new Set(['DIV', 'P', 'SECTION', 'HEADER', 'FOOTER', 'ARTICLE', 'LI', 'UL', 'OL',
  'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
  'BUTTON', 'DETAILS', 'SUMMARY', 'LABEL', 'BR']);

function laidOut(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
  if (node.nodeType !== Node.ELEMENT_NODE) return '';
  const element = node as Element;
  const inner = [...element.childNodes].map(laidOut).join('');
  return BLOCK.has(element.tagName) ? `\n${inner}\n` : inner;
}

/** Every reader-facing sentence under `root`, in order. */
function sentences(root: Element): string[] {
  const copy = root.cloneNode(true) as Element;
  for (const selector of NOT_PROSE) {
    copy.querySelectorAll(selector).forEach((node) => node.remove());
  }
  return laidOut(copy)
    .split('\n')
    // A line can still hold more than one sentence — the spend block's cause is four in one div.
    .flatMap((line) => line.trim().split(/(?<=[.!?])\s+/))
    .map((line) => line.trim())
    .filter((line) => /[.!?]$/.test(line));
}

/** The whole surface's prose, so a failure names the sentences rather than only their number. */
function surfaceProse(): string[] {
  return sentences(screen.getByTestId('improve-center'));
}

/**
 * THE SAME PAYLOAD WITH THE CARDS FILLED IN, which is the wordiest text on the page and until now
 * escaped the ceiling meant to govern it.
 *
 * Two holes let it escape. The committed fixture leaves `provenance.heuristicFacets` empty, so the
 * caveat that a claim was inferred never rendered under this measure at all; and most card strings end
 * in no full stop (`…(permission: LOCKED_TODAY)`), so the ones that DID render were invisible to a
 * count of sentences. A card is the longest reader-facing text on the default view, so the budget was
 * measuring everything except the thing most likely to blow it.
 *
 * Shaped rather than transcribed, and it says what each shape is FOR: the two suggestions carry the
 * two shapes the live bundle has — one whose claim rests on an inferred facet, one whose facets were
 * all measured — under the one permission both live rows carry, which is what makes the producer's own
 * `summary` the same sentence twice. One keeps the fixture's two candidates, and the other has NONE:
 * that is the case where a card has no change to show, so it is where the disclosure has to find its
 * content elsewhere, and it is the only one of the two that can be measured for a hole.
 *
 * `evidenceHeadline` is set on both, because a headline is what the card leads with now and a payload
 * without one measures the fallback instead of the shape a reader is served.
 */
const populated: ImproveData = {
  ...live,
  suggestions: [
    {
      ...data.suggestions[0],
      suggestionId: '8080f09550d0',
      targetType: 'tool',
      targetId: 'builtin-tools',
      targetName: 'builtin tools',
      targetLocation: null,
      mutability: 'LOCKED_TODAY',
      summary: 'builtin-tools is implicated, but no change can be proposed '
        + '(permission: LOCKED_TODAY)',
      evidenceSummary: 'from 1 run where the session finished without fetching the result of the '
        + 'last job it started, although it had fetched results for earlier jobs in the same session',
      evidenceHeadline: "The last job's result was never fetched",
      pattern: {
        ...data.suggestions[0].pattern,
        patternId: 'session_ended_on_unretrieved_dispatch',
        shapeFacets: ['session_ended_on_unretrieved_dispatch'],
        runCount: 1,
      },
      provenance: {
        ...data.suggestions[0].provenance,
        heuristicFacets: ['session_ended_on_unretrieved_dispatch'],
      },
    },
    {
      ...data.suggestions[0],
      suggestionId: 'ec7e1ed065a6',
      targetType: 'control',
      targetId: 'validate-retry-loop',
      targetName: 'validate retry loop',
      mutability: 'LOCKED_TODAY',
      summary: 'validate-retry-loop is implicated, but no change can be proposed '
        + '(permission: LOCKED_TODAY)',
      evidenceSummary: 'from 1 run(s) where a step failed',
      evidenceHeadline: 'A step failed',
      candidates: [],
      singleCandidateForced: null,
      pattern: {
        ...data.suggestions[0].pattern,
        patternId: 'has_failed_node',
        shapeFacets: ['has_failed_node'],
        runCount: 1,
      },
    },
  ],
};

/**
 * The same payload with the suggestion list TRIMMED, which is the one reader-facing line on this
 * surface that no committed payload renders.
 *
 * The producer stamps `suggestionsTotal` when it cuts the list to fit one DynamoDB item, and no bundle
 * in staging carries the key yet — so the notice that says the list is short is live code reachable
 * only by a payload written for it. Included below rather than assumed unreachable: a branch nothing
 * renders is a branch nothing checks, which is how it kept a dash and a monospace `of` after both had
 * been swept off every line beside it.
 */
const trimmed: ImproveData = { ...live, suggestionsTotal: 40 } as unknown as ImproveData;

/**
 * Attributes whose value a reader reads.
 *
 * A tooltip is prose the moment somebody hovers it, and this surface leans on `title` heavily — the
 * spend block's collapsed cause keeps its whole argument there. So a text-only sweep would report a
 * surface clean while the sentences a reader actually opens were untouched.
 */
const READABLE_ATTRIBUTES = ['title', 'aria-label', 'alt', 'placeholder'];

/**
 * Every place an em dash reaches this reader, named so a failure says where.
 *
 * The dash goes UNUSED on this surface, and the rule is about presentation rather than about
 * punctuation being wrong: it joins two things that wanted to be two sentences, or fronts a clause
 * that wanted cutting, and on a surface whose whole discipline is saying less it was doing neither
 * job well. It was also being used as a bare lead-in marker — `— was 80% on 3 Sep` — where the words
 * can name the relation instead.
 *
 * SOURCE COMMENTS ARE NOT IN SCOPE and this cannot see them, which is the point of walking the
 * rendered tree rather than grepping the files: the argument for a decision is written for whoever
 * changes it next, and rewriting those would be churn no reader benefits from.
 */
function emDashes(root: Element): string[] {
  const found: string[] = [];
  const where = (el: Element) => el.getAttribute('data-testid') ?? el.tagName.toLowerCase();
  const walk = (node: Node, host: Element) => {
    if (node.nodeType === Node.TEXT_NODE) {
      if ((node.textContent ?? '').includes('—')) {
        found.push(`text in ${where(host)}: ${node.textContent}`);
      }
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const element = node as Element;
    for (const name of READABLE_ATTRIBUTES) {
      const value = element.getAttribute(name);
      if (value !== null && value.includes('—')) {
        found.push(`${name} on ${where(element)}: ${value}`);
      }
    }
    element.childNodes.forEach((child) => walk(child, element));
  };
  walk(root, root);
  return found;
}

beforeEach(() => {
  // The spend block reads its metric selection from storage on mount, and jsdom keeps storage for the
  // whole file — so without this one test would decide what the next one renders.
  localStorage.clear();
});

// --- the order --------------------------------------------------------------
describe('the default view\'s render order', () => {
  it('is cost, then outcome, then the recommendations', async () => {
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const order = [...screen.getByTestId('improve-center').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    const at = (id: string): number => order.indexOf(id);
    const firstSuggestion = order.findIndex((id) => id.startsWith('suggestion-'));

    expect(at('metric-block')).toBeGreaterThan(-1);
    expect(at('metric-block')).toBeLessThan(at('outcome-band'));
    expect(at('outcome-band')).toBeLessThan(at('impact-summary'));
    expect(firstSuggestion).toBeGreaterThan(-1);
    expect(at('impact-summary')).toBeLessThan(firstSuggestion);
  });

  it('keeps the spend block first, so its two switches stay at the top of the page', async () => {
    /*
     * The scope and period switches live on that block's header by a deliberate decision: a figure
     * whose basis is hidden cannot be read, so the basis is stated beside it. Keeping the block first
     * leaves the switches above both bands, governing both, with no edit to that decision — putting
     * the outcome figures above it would have pushed them mid-page.
     */
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const order = [...screen.getByTestId('improve-center').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    expect(order.indexOf('metric-scope')).toBeLessThan(order.indexOf('outcome-band'));
    expect(order.indexOf('metric-period')).toBeLessThan(order.indexOf('outcome-band'));
    expect(screen.getByTestId('metric-scope')).toBeInTheDocument();
    expect(screen.getByTestId('metric-period')).toBeInTheDocument();
  });

  it('puts the recommendations above the three engine blocks, which are behind Advanced', async () => {
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    const advanced = await screen.findByTestId('view-components');

    for (const id of ['runs-block', 'waste-block', 'quality-block']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    fireEvent.click(advanced);
    for (const id of ['runs-block', 'waste-block', 'quality-block']) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
    // And the Advanced view is not the default view wearing another name: the two new bands and the
    // suggestion list are not there.
    expect(screen.queryByTestId('outcome-band')).toBeNull();
    expect(screen.queryByTestId('impact-summary')).toBeNull();
    expect(screen.queryByTestId('metric-block')).toBeNull();
  });

  it('leaves the Advanced link where it was, at the end of the header', async () => {
    // The label is free; the key is a `?improve=` value and a testid suffix, and the position is a
    // reader's muscle memory. Neither moves for this change.
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    const advanced = await screen.findByTestId('view-components');

    expect(advanced).toHaveTextContent('Advanced');
    const header = advanced.closest('header');
    expect(header).not.toBeNull();
    const buttons = [...(header as HTMLElement).querySelectorAll('button')];
    expect(buttons[buttons.length - 1]).toBe(advanced);
  });
});

// --- the ceiling ------------------------------------------------------------
describe('the default view\'s prose budget', () => {
  it(`is at most ${SENTENCE_BUDGET} sentences on the payload most readers are served`, async () => {
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const prose = surfaceProse();
    // The list is in the failure message on purpose: a count alone says a budget was blown and not
    // which sentence to move.
    expect(prose.length, `default view prose:\n${prose.map((s) => `  - ${s}`).join('\n')}`)
      .toBeLessThanOrEqual(SENTENCE_BUDGET);
  });

  it('holds for a reader whose own bundle the producer HAS written', async () => {
    // Then the spend block shows three figures instead of its collapsed cause, so this payload has
    // FEWER sentences rather than more — which is worth pinning, because it is the direction the
    // budget could be blown from without anyone noticing the other case.
    render(<ImproveCenter load={() => Promise.resolve(withActor)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const prose = surfaceProse();
    expect(prose.length, `default view prose:\n${prose.map((s) => `  - ${s}`).join('\n')}`)
      .toBeLessThanOrEqual(SENTENCE_BUDGET);
  });

  it('holds on a bundle that carries neither key, where every band states its absence', async () => {
    // The committed fixture: no scorecard, no scan, no day rows and no personal bundle. Four
    // absences, each of which has to say what it is — this is the shape that most invites prose.
    expect(data.scorecard).toBeUndefined();
    expect(data.waste).toBeUndefined();
    render(<ImproveCenter load={() => Promise.resolve(data)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const prose = surfaceProse();
    expect(prose.length, `default view prose:\n${prose.map((s) => `  - ${s}`).join('\n')}`)
      .toBeLessThanOrEqual(SENTENCE_BUDGET);
  });

  it(`is at most ${SENTENCE_BUDGET} with the cards filled in, which the budget used to miss`,
     async () => {
       /*
        * THE GAP THIS FILE HAD. A suggestion card is the longest reader-facing text on the default
        * view, and neither of the two ways it gets long was reaching this measure: the fixture leaves
        * the inferred-facet caveat unrendered, and card strings carry no terminal punctuation, so a
        * count of sentences saw almost none of it.
        */
       render(<ImproveCenter load={() => Promise.resolve(populated)} solo={null} />);
       await screen.findByTestId('outcome-band');

       /*
        * The payload is really rendering what it claims to, asserted BEFORE the ceiling: a card that
        * failed to render would make the count pass for the worst possible reason, which is the same
        * mistake as reading an absence as a zero. Both cards, both leads, and the caveat that only
        * appears when a claim rests on an inference.
        */
       const cards = screen.getAllByTestId(/^suggestion-[0-9a-f]+$/);
       expect(cards).toHaveLength(2);
       const leads = screen.getAllByTestId('evidence-lead').map((node) => node.textContent);
       expect(leads).toEqual(["The last job's result was never fetched", 'A step failed']);
       expect(screen.getAllByTestId('provenance-inferred-facet')).toHaveLength(1);
       expect(screen.getAllByTestId('permission').map((node) => node.textContent))
         .toEqual(['locked for now', 'locked for now']);

       const prose = surfaceProse();
       expect(prose.length, `default view prose:\n${prose.map((s) => `  - ${s}`).join('\n')}`)
         .toBeLessThanOrEqual(SENTENCE_BUDGET);

       /*
        * And the cards spend NONE of it, which is what the ceiling is worth here. Every line of a
        * collapsed card is a reading — a name, a permission, what happened, how many runs, whether it
        * was measured — and the prose that used to sit among them is behind the disclosure. The remedy
        * if this ever fails is to move the sentence behind that click, not to raise the number above.
        */
       const cardProse = cards.flatMap((card) => sentences(card));
       expect(cardProse, cardProse.join(' | ')).toEqual([]);
     });

  it(`spends ${SPEND_BUDGET} of them on a reader with no figures of their own`, async () => {
    /*
     * The payload most readers are served, and the piece of this page that used to be four of its
     * seven sentences. The cause is real and unchanged — no figure of this reader's own can be read
     * from this payload — and what changed is how much of the producer's argument about it a reader
     * has to walk past to reach the figures below.
     */
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const spend = sentences(screen.getByTestId('metric-block'));
    expect(spend.length, spend.join(' | ')).toBe(SPEND_BUDGET);
    expect(spend[0]).toBe("Your own figures aren't in this report yet, but Everyone's are.");
    // Absence, not a zero: the one thing the four sentences existed to refuse, and the phrase must
    // not have traded it away for brevity. No figure of any kind appears in it.
    expect(spend[0]).not.toMatch(/\d/);
    // And the argument is still reachable, on the line rather than on the page.
    expect(screen.getByTestId('metric-block-collapsed').getAttribute('title'))
      .toMatch(/not even as zero/);
  });

  it('keeps the harness\'s own vocabulary off the whole of that page, not just two bands', async () => {
    /*
     * The wider form of the vocabulary rule below, and it holds on THIS payload rather than on every
     * one: the spend block's other absences are still argued in the producer's words, and they are a
     * different fact reached by a different reader. What a reader with no bundle of their own sees is
     * now free of all of it, top to bottom.
     */
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const page = screen.getByTestId('improve-center').textContent ?? '';
    for (const jargon of ['bundle', 'actor', 'tenant', 'harvest', 'producer', 'scope switch']) {
      expect(page.toLowerCase(), jargon).not.toContain(jargon);
    }
  });

  it(`spends at most ${OWNED_BUDGET} of those on the outcome band and its sentence`, async () => {
    // The tighter ceiling, on the part that is this change's. The two tiles spend NONE: a label, a
    // figure, a fraction and a comparison are readings, not prose.
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    expect(sentences(screen.getByTestId('outcome-band'))).toEqual([]);
    const owned = [...sentences(screen.getByTestId('outcome-band')),
                   ...sentences(screen.getByTestId('impact-summary'))];
    expect(owned.length, owned.join(' | ')).toBeLessThanOrEqual(OWNED_BUDGET);
  });

  it('spends no figure on that sentence, because every figure it had is a reading above it', async () => {
    /*
     * The rule that took the other two sentences out. A figure in the prose was a second printing of
     * a figure on a tile, and two of them printed different denominators — the rate is taken over the
     * judged requests, the scan over the whole population — which is correct arithmetic that read as
     * a mismatch to debug. So the prose states none, and the tiles are asserted to still hold every
     * one of them: this is a figure MOVED off a line, not a figure dropped from the report.
     */
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const ours = sentences(screen.getByTestId('impact-summary')).join(' ');
    expect(ours, ours).not.toMatch(/\d/);

    const band = screen.getByTestId('outcome-band').textContent ?? '';
    for (const reading of ['70%', '16 of 23', '2 could not be judged', '3m 29s', '4 of 25']) {
      expect(band, reading).toContain(reading);
    }
  });

  it('spends nothing on opening the table, because rows are not prose', async () => {
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');
    const closed = surfaceProse().length;

    fireEvent.click(screen.getByTestId('impact-details-toggle'));
    expect(screen.getByTestId('impact-table')).toBeInTheDocument();
    expect(surfaceProse()).toHaveLength(closed);
  });

  it('keeps the harness\'s own vocabulary off it', async () => {
    /*
     * The words that made the previous surface unreadable to the reader it is for: a freelance video
     * editor has no idea what a bundle, an actor, a tenant or a harvest window is, and does not need
     * one to act on any of this. They are all still available, in the blocks behind Advanced, to
     * somebody who does.
     *
     * Scoped to the two bands, and kept scoped now that the whole page passes it on this payload: the
     * spend block's OTHER absences are still argued in the producer's words, so the wider check above
     * can only speak for the payload it names while this one holds whatever the block is saying.
     */
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const ours = (screen.getByTestId('outcome-band').textContent ?? '')
      + (screen.getByTestId('impact-summary').textContent ?? '');
    for (const jargon of ['bundle', 'actor', 'tenant', 'harvest', 'gating', 'gated', 'producer',
                          'reconstructed', 'trajectory', 'span', 'evaluator', 'facet']) {
      expect(ours.toLowerCase()).not.toContain(jargon);
    }
  });

  it('uses no em dash in anything a reader reads, on any of the four payloads', async () => {
    /*
     * All four, because the dashes were spread across the branches rather than sitting in one
     * string: the spend block's collapsed cause, the outcome band's four prior-period captions, the
     * sentences each band falls back to when its own key is missing, and the notice that says the
     * suggestion list was cut short. A payload apiece reaches a different set of them, so one render
     * would leave most of the surface unswept.
     */
    for (const payload of [live, withActor, data, trimmed]) {
      const view = render(<ImproveCenter load={() => Promise.resolve(payload)} solo={null} />);
      await screen.findByTestId('outcome-band');
      // The trim notice is the one line only the fourth payload reaches, so its presence is asserted
      // rather than hoped for: a sweep over a branch that did not render proves nothing about it.
      if (payload === trimmed) expect(screen.getByTestId('suggestions-trimmed')).toBeInTheDocument();
      const found = emDashes(screen.getByTestId('improve-center'));
      expect(found, found.join('\n')).toEqual([]);
      view.unmount();
    }
  });

  it('shows no judge tier and no raw score', async () => {
    // `0.94 started the right work` was on this surface. A two-decimal score off an LLM judge is a
    // number a reader cannot act on and cannot check, and it is behind Advanced now.
    render(<ImproveCenter load={() => Promise.resolve(live)} solo={null} />);
    await screen.findByTestId('outcome-band');

    const ours = (screen.getByTestId('outcome-band').textContent ?? '')
      + (screen.getByTestId('impact-summary').textContent ?? '');
    expect(ours).not.toMatch(/0\.\d\d/);
    expect(ours).not.toContain('started the right work');
  });
});
