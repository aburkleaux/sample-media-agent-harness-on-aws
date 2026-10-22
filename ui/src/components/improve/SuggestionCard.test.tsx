/**
 * @vitest-environment jsdom
 *
 * WHAT THE CARD SAYS BEFORE ANYTHING IS CLICKED, and then THE WARRANT AND ITS PROVENANCE: whether a
 * suggestion states the facts needed to check the claim it makes, and where that claim came from.
 *
 * The two subjects meet at one rule. The collapsed card is the reader's, so it carries what happened,
 * how much evidence there is, what may be done about it, and — the one caveat that may not be moved —
 * whether the claim was measured at all. Everything phrased in the harness's own vocabulary is behind
 * the disclosure, and the disclosure has to have content on every card, because a control that opens
 * nothing is worse than no control. The last two describe blocks own that; the ones before them own
 * the warrant.
 *
 * The card has always named a component and a shape. What it withheld was every fact behind them —
 * which registry declaration justified the blame, how many passing runs the shape was tested against,
 * what the shape predicts, and whether some other shape explains those runs just as well. A card that
 * says `pass_hits 0` and nothing else reads identically whether the gate had one control or twelve,
 * which is the difference between nearly free and a claim. Then, separately, WHERE it came from:
 * whether the shape was measured or inferred, which stores were read, which registry revision blamed
 * the component, and when it was judged and first written down.
 *
 * These pin that, and one rule above it: **absent, empty and zero are three different facts.** An
 * absent gate count is a report that did not say; `controlsConsidered: 0` is a gate that had nothing
 * to test against, which is the WORST reading and must never look like one that was passed; and an
 * empty `matchedFailureMode` is a basis that had no declaration to quote, which the card already
 * captions as inferred and must not caption twice.
 *
 * Nothing here re-tests the card's actions or its diff — `ImproveCenter.test.tsx` owns those.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required. `@testing-library/user-event` is not a dependency; use fireEvent.
 */
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SuggestionCard } from './SuggestionCard';
import { checkLabel } from './verdictWording';
import type { FailurePattern, Provenance, Suggestion } from '../../improve-api';

/**
 * A suggestion transcribed from the live staging SNAPSHOT (`mediaworks-harness-improve`,
 * `actorId="__system__"`, read 2026-09-03), field for field.
 *
 * It is the shape the DEPLOYED producer serves, which is why it is the base every case below derives
 * from: each case is this row with ONE field added or changed and nothing else invented. The four
 * `null`s are real — the wire sends DynamoDB NULLs there.
 *
 * It carries none of the SIX warrant fields — `matchedFailureMode` plus the five optional members of
 * `FailurePattern` — so every warrant case below is an addition. It DOES carry all five provenance
 * fields, which is why the provenance cases are changes rather than additions, and why `[]` for
 * `heuristicFacets` is the live reading of "measured, nothing inferred" rather than a shape invented
 * to exercise a branch.
 */
const LIVE: Suggestion = {
  suggestionId: 'ec7e1ed065a6',
  createdAt: '2026-08-29T08:01:24Z',
  status: 'open',
  targetType: 'control',
  targetId: 'validate-retry-loop',
  targetName: 'validate retry loop',
  targetLocation: 'plan_and_execute_tool.py:1046-1086',
  baseVersion: null,
  mutability: 'LOCKED_TODAY',
  operation: 'edit',
  summary: 'validate-retry-loop is implicated, but no change can be proposed '
    + '(permission: LOCKED_TODAY)',
  evidenceSummary: 'from 1 run(s) where a step failed',
  /*
   * The lead line, and on THIS row it does not come from the stored record: the row stopped
   * reproducing on 29 August, so its content is frozen and the builder can never reach it again. The
   * publish funnel derives the headline from the `shapeFacets` the row already carries
   * (`has_failed_node`), by calling the same function the builder calls, so a five-day-old finding is
   * not the one card on the surface with no headline.
   */
  evidenceHeadline: 'A step failed',
  pattern: {
    patternId: 'has_failed_node',
    shape: 'from 1 run(s) where a step failed',
    shapeFacets: ['has_failed_node'],
    runCount: 1,
    passHits: 0,
  },
  candidates: [{
    candidateId: 'b33dac22edc7',
    label: 'validate-retry-loop is implicated, but no change can be proposed '
      + '(permission: LOCKED_TODAY)',
    diffText: '',
    before: '',
    after: '',
    claim: {
      utilityTerm: 'results_delivered',
      direction: 'increase',
      magnitude: null,
      onPattern: 'has_failed_node',
    },
    risk: null,
    disposition: 'advisory',
  }],
  singleCandidateForced: 'no applyable remedy exists for validate-retry-loop: permission is '
    + 'LOCKED_TODAY and it has no version lineage',
  blastRadius: { class: 'single', instanceCount: 1 },
  firstSeenAt: '2026-08-29T08:01:24Z',
  lastSeenAt: '2026-08-29T08:01:24Z',
  reproducing: false,
  affordances: {
    canTryBoth: false,
    tryBothBlockedReason: 'flat_category',
    canPublish: false,
    requiresTrialBeforePublish: false,
    canRollback: false,
    rollbackToVersion: null,
    canSendToTeam: true,
    hostAgentId: null,
    forkNeeded: false,
  },
  provenance: {
    evaluatedAt: '2026-08-29T08:01:24Z',
    judgeConfigHash: 'us.anthropic.claude-sonnet-5:full_conversation:0.7:runs1',
    judgeRuns: 1,
    evidenceSources: ['aws/spans', 'ddb:plan-state', 'harvest:live'],
    attributionBasis: 'registry_declared',
    attributionAlternatives: ['slack-events-endpoint', 'lab-workflow-executor'],
    registryHeadSha: '5c3f40f',
    heuristicFacets: [],
  },
};

/**
 * The registry's OWN declaration for this component, transcribed from
 * `agent/src/improve/data/sigma-registry.json` — `validate-retry-loop.known_failure_modes[0]`.
 *
 * The real sentence rather than a plausible one, because what is being pinned is that engine-written
 * prose about a harness component reaches the screen as written: it carries an identifier
 * (`design_agent`), a defect reference (`D-02`) and an em dash, and a card that reformatted any of
 * those would no longer be quoting the registry.
 */
const DECLARATION = 'validation_errors never read by design_agent — retry is byte-identical (D-02)';

/** The live row with a different pattern. One field, so nothing else about the card moves. */
function withPattern(over: Partial<FailurePattern>): Suggestion {
  return { ...LIVE, pattern: { ...LIVE.pattern, ...over } };
}

/** The live row with a different provenance. Same discipline. */
function withProvenance(over: Partial<Provenance>): Suggestion {
  return { ...LIVE, provenance: { ...LIVE.provenance, ...over } };
}

/** Returns the render result, so a test comparing TWO states can unmount between them — a second
 *  `render` into the same container leaves both in the DOM and every `getByTestId` finds two. */
function show(suggestion: Suggestion) {
  return render(<SuggestionCard suggestion={suggestion} expanded onToggle={() => {}}
                                onAction={() => {}} />);
}

/**
 * Which typeface an element ASKED for.
 *
 * Read off the inline `style` and not a computed font, because jsdom resolves no custom properties:
 * `var(--font-body)` and `var(--font-mono)` both compute to the same empty string, so a computed-style
 * assertion would pass whichever face the code chose. The declaration is the choice, so it is what is
 * asserted — which is also why both faces are NAMED at the call sites rather than one being left to
 * inherit.
 */
const familyOf = (element: Element) => (element as HTMLElement).style.fontFamily;

// --- what the deployed producer actually serves -------------------------------
describe('a suggestion carrying no warrant, which is every one on the wire today', () => {
  it('names all three withheld facts in one line rather than three sentences', () => {
    show(LIVE);
    const line = screen.getByTestId('pattern-warrant-absent');
    expect(line).toHaveTextContent('what this shape predicts');
    expect(line).toHaveTextContent('how many passing runs it was tested against');
    expect(line).toHaveTextContent('whether another shape explains those runs just as well');
  });

  it('draws no gate line at all, rather than a row of zeros', () => {
    // Three zeros would read as a shape that survived everything thrown at it. There is nothing on
    // the wire that says how many controls there were, so nothing is claimed.
    show(LIVE);
    expect(screen.queryByTestId('pattern-gate')).toBeNull();
    expect(screen.queryByTestId('pattern-gate-none')).toBeNull();
  });

  it('says the registry declaration is not quoted, since the basis claims there is one', () => {
    show(LIVE);
    expect(screen.getByTestId('matched-failure-mode-absent'))
      .toHaveTextContent('does not quote the registry declaration');
    expect(screen.queryByTestId('matched-failure-mode')).toBeNull();
  });

  it('states neither a symptom nor an equally-good alternative it does not have', () => {
    show(LIVE);
    expect(screen.queryByTestId('pattern-symptom')).toBeNull();
    expect(screen.queryByTestId('pattern-equally-discriminative')).toBeNull();
  });
});

// --- the registry's own sentence ---------------------------------------------
describe('the declaration behind the blame', () => {
  it('quotes the registry sentence as written, identifier and defect id included', () => {
    show(withProvenance({ matchedFailureMode: DECLARATION }));
    expect(screen.getByTestId('matched-failure-mode'))
      .toHaveTextContent(`declared in the registry as: “${DECLARATION}”`);
    // The absence line is the alternative to this one, never a companion to it.
    expect(screen.queryByTestId('matched-failure-mode-absent')).toBeNull();
  });

  it('does not caption an inferred attribution twice', () => {
    /*
     * `''` is what the producer emits EXACTLY when the basis is `keyword_inferred`: there was no
     * declaration to quote. The card already captions that case beside the permission, so an empty
     * sentence must produce neither the quote nor the "did not quote it" line — the second would
     * report the producer as withholding something that never existed.
     */
    show(withProvenance({ attributionBasis: 'keyword_inferred', matchedFailureMode: '' }));
    expect(screen.queryByTestId('matched-failure-mode')).toBeNull();
    expect(screen.queryByTestId('matched-failure-mode-absent')).toBeNull();
    expect(screen.getAllByText(/inferred attribution, not declared/)).toHaveLength(1);
  });

  it('reports a withheld warrant on a declaring basis, whichever nothing arrived', () => {
    /*
     * The BASIS decides, not the flavour of absence. `''` on `registry_declared` is impossible
     * upstream, so if it ever arrives it is a producer bug — and going silent on it would let a report
     * that asserts a declaration and quotes none pass as an inferred attribution.
     */
    for (const nothing of [undefined, '', '   ']) {
      const { unmount } = render(
        <SuggestionCard suggestion={withProvenance({ matchedFailureMode: nothing })} expanded
                        onToggle={() => {}} onAction={() => {}} />);
      expect(screen.getByTestId('matched-failure-mode-absent'), String(nothing))
        .toBeInTheDocument();
      expect(screen.queryByTestId('matched-failure-mode')).toBeNull();
      unmount();
    }
  });
});

// --- the gate ----------------------------------------------------------------
describe('how well the shape was tested', () => {
  const GATE = { controlsConsidered: 12, controlsExcluded: 9, controlsInconclusive: 3 };

  it('states the pool, what was ruled out and what could not be placed', () => {
    show(withPattern(GATE));
    const line = screen.getByTestId('pattern-gate');
    expect(line).toHaveTextContent('tested against 12 passing runs · 9 ruled out '
      + '· 3 could not be placed');
    expect(screen.queryByTestId('pattern-warrant-absent'))
      .not.toHaveTextContent('how many passing runs');
  });

  it('renders one control differently from twelve, which is the whole point', () => {
    show(withPattern({ controlsConsidered: 1, controlsExcluded: 1, controlsInconclusive: 0 }));
    // Singular, and the count is there to be read: `pass_hits 0` against one run is nearly free.
    expect(screen.getByTestId('pattern-gate'))
      .toHaveTextContent('tested against 1 passing run · 1 ruled out · 0 could not be placed');
  });

  it('says a gate with no controls establishes nothing, not that it was passed', () => {
    // A MEASURED zero, and the worst reading on the card. `0 of 0` rendered as a gate line would
    // read as a shape tested and cleared.
    show(withPattern({ controlsConsidered: 0, controlsExcluded: 0, controlsInconclusive: 0 }));
    expect(screen.getByTestId('pattern-gate-none'))
      .toHaveTextContent('No passing run was available to test this shape against');
    expect(screen.queryByTestId('pattern-gate')).toBeNull();
  });

  it('prints all three when they do not add up, and asserts no identity between them', () => {
    /*
     * The triple closes only within ONE refinement, and a pattern takes it from its least-tested
     * member — so across the runs a pattern rests on the three need not sum, and a card that
     * reported the gap would call a sound pattern broken. The tooltip is where that is said.
     */
    show(withPattern({ controlsConsidered: 12, controlsExcluded: 4, controlsInconclusive: 2 }));
    const line = screen.getByTestId('pattern-gate');
    expect(line).toHaveTextContent('tested against 12 passing runs · 4 ruled out '
      + '· 2 could not be placed');
    expect(line.getAttribute('title')).toContain('need not add up');
    expect(line.getAttribute('title')).toContain('own evidence was missing');
  });

  it('reports an inconclusive control as a count and never as a run id', () => {
    // An inconclusive control is another tenant's run. There is no field for those ids anywhere on
    // the wire, and a card that grew one would be a per-row disclosure on a shared item.
    show(withPattern({ ...GATE, controlsInconclusive: 3 }));
    const line = screen.getByTestId('pattern-gate');
    expect(line.textContent).toMatch(/3 could not be placed$/);
    expect(line.textContent).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}/);
  });

  it('treats a corrupt count as absent rather than printing it', () => {
    for (const broken of [{ controlsExcluded: -1 }, { controlsInconclusive: Number.NaN },
                          { controlsConsidered: '12' as unknown as number }]) {
      const { unmount } = render(
        <SuggestionCard suggestion={withPattern({ ...GATE, ...broken })} expanded
                        onToggle={() => {}} onAction={() => {}} />);
      expect(screen.queryByTestId('pattern-gate'), JSON.stringify(broken)).toBeNull();
      expect(screen.getByTestId('pattern-warrant-absent'))
        .toHaveTextContent('how many passing runs it was tested against');
      unmount();
    }
  });
});

// --- the consequent ----------------------------------------------------------
describe('what the shape predicts', () => {
  it('phrases the symptom through the surface’s shared vocabulary, not as its identifier', () => {
    show(withPattern({ symptom: ['tier_failed:results_delivered'] }));
    const line = screen.getByTestId('pattern-symptom');
    expect(line).toHaveTextContent(`predicts: ${checkLabel('results_delivered', 'failed')}`);
    // The evaluator's own name is what the wire carries and is not what a reader is shown.
    expect(line).not.toHaveTextContent('tier_failed');
  });

  it('names every symptom facet, so two patterns differing only in one read differently', () => {
    show(withPattern({
      symptom: ['tier_failed:dispatch_correct', 'check_failed:DagCompletionValidator'],
    }));
    const line = screen.getByTestId('pattern-symptom');
    expect(line).toHaveTextContent(checkLabel('dispatch_correct', 'failed'));
    expect(line).toHaveTextContent(checkLabel('DagCompletionValidator', 'failed'));
  });

  it('keeps the identifier visible for a check it has no phrase for', () => {
    // The producer can add an evaluator and ship before this surface does, and a blank line would
    // read as a render fault rather than as the unphrased check it is.
    show(withPattern({ symptom: ['check_failed:SomeNewValidator'] }));
    expect(screen.getByTestId('pattern-symptom')).toHaveTextContent('SomeNewValidator');
  });
});

// --- one of several equally good explanations --------------------------------
describe('other shapes that separate the same runs', () => {
  it('says the named shape was chosen by precedence and not by the evidence', () => {
    show(withPattern({ equallyDiscriminative: ['has_retried_tool', 'plan_step_ran_early'] }));
    const line = screen.getByTestId('pattern-equally-discriminative');
    expect(line).toHaveTextContent('these separate the same runs just as well: has_retried_tool '
      + '· plan_step_ran_early');
    expect(line).toHaveTextContent('chosen by precedence, not by the evidence');
    // The names are joined by ` · `, so what follows them is a sentence of its own rather than a
    // dash-joined clause a reader could take for one more name.
    expect(line.textContent).toContain('plan_step_ran_early. So the shape');
  });

  it('is silent on a measured empty list, and stops calling it withheld', () => {
    /*
     * `[]` is the producer's measured "nothing else did as well", which is the ordinary case — a
     * caveat printed under every card teaches a reader to skip the line that matters. So it is
     * silent, and it also drops out of the withheld list, since the wire did answer.
     */
    show(withPattern({ equallyDiscriminative: [] }));
    expect(screen.queryByTestId('pattern-equally-discriminative')).toBeNull();
    expect(screen.getByTestId('pattern-warrant-absent'))
      .not.toHaveTextContent('whether another shape explains');
  });
});

// --- a warrant in full --------------------------------------------------------
describe('a suggestion carrying the whole warrant', () => {
  const full: Suggestion = {
    ...withPattern({
      symptom: ['tier_failed:results_delivered'],
      controlsConsidered: 12,
      controlsExcluded: 9,
      controlsInconclusive: 3,
      equallyDiscriminative: [],
    }),
    provenance: { ...LIVE.provenance, matchedFailureMode: DECLARATION },
  };

  it('states nothing as withheld', () => {
    show(full);
    expect(screen.queryByTestId('pattern-warrant-absent')).toBeNull();
  });

  it('shows the gate, the consequent and the declaration together', () => {
    show(full);
    expect(screen.getByTestId('pattern-gate')).toBeInTheDocument();
    expect(screen.getByTestId('pattern-symptom')).toBeInTheDocument();
    expect(screen.getByTestId('matched-failure-mode')).toBeInTheDocument();
  });
});

// --- whether the shape was measured or inferred --------------------------------
/**
 * The OTHER live suggestion's inferred facet, transcribed from the same staging SNAPSHOT
 * (`8080f09550d0`, target `builtin-tools`), where it is both `provenance.heuristicFacets[0]` and the
 * pattern's only `shapeFacets` entry — so on that row the whole shape rests on the inference.
 *
 * Used to change ONE field of the row above rather than transcribing a second suggestion, because
 * what is under test is the card's reading of the field and the two live rows differ in it: this one
 * names a facet, `LIVE` reports a measured none.
 */
const INFERRED_FACET = 'session_ended_on_unretrieved_dispatch';

/**
 * SPLIT ACROSS THE TWO POSITIONS, on purpose, and these pin which part goes where.
 *
 * `heuristicFacets` is the one field of the five that is not provenance about the judging: the other
 * four say how the verdict was produced and belong behind the click. What it carries is split in two.
 * The FACT that the claim rests on an inference is a caveat on the claim, so it renders outside the
 * expand, in four words. The facet's NAME is a harness identifier — no use to the reader this surface
 * is written for, real use to somebody re-reading the evidence — so it renders behind the click with
 * the store tokens. Keep the fact, drop the vocabulary.
 *
 * A measured none and a missing key are bookkeeping and are only behind the click, and that half of
 * the split is the whole design: a reassurance ("none inferred") printed under every card in the list
 * is what teaches a reader to skip that position, which is the argument the equally-discriminative
 * line already makes — so making it unmissable would cost the one reading that changes the claim.
 *
 * Its nearest sibling in kind is `equallyDiscriminative`, also a caveat on the shape, but that line
 * lives inside the expanded `<dl>` — so sitting beside it would put the caveat behind the click, which
 * is the one thing the placement exists to prevent.
 */
describe('whether the shape was measured or inferred', () => {
  it('shows the INFERENCE without expanding, so a reader who never opens the card sees it', () => {
    /*
     * THE PLACEMENT TEST, and it is pointed at the caveat specifically rather than at "some facet
     * line exists": what it pins is that the inference is visible without opening the card.
     * `expanded={false}` is the state every card in the list is in, and everything behind the toggle
     * is absent here. Without this, a later change could move the line back inside `{expanded && …}`
     * and every other test in this file would still pass, because they all render expanded.
     */
    render(<SuggestionCard suggestion={withProvenance({ heuristicFacets: [INFERRED_FACET] })}
                           expanded={false} onToggle={() => {}} onAction={() => {}} />);

    expect(screen.queryByTestId('suggestion-detail')).toBeNull();
    expect(screen.queryByTestId('provenance')).toBeNull();
    expect(screen.getByTestId('provenance-inferred-facet'))
      .toHaveTextContent('inferred, not measured');
    // And the OTHER half of the split, which is what keeps this line short enough to be read: the
    // facet's own name is nowhere on the collapsed card.
    expect(document.body.textContent).not.toContain(INFERRED_FACET);
  });

  it('keeps the two quiet readings OUT of that slot, and says them behind the click', () => {
    /*
     * The other half of the split. Unexpanded, a measured none and a missing key say nothing at all —
     * this position is the caveat's alone. Expanded, both are stated, because absent and empty are
     * different facts and silence in both places would make them one.
     */
    for (const facets of [[], undefined]) {
      const collapsed = render(
        <SuggestionCard suggestion={withProvenance({ heuristicFacets: facets })}
                        expanded={false} onToggle={() => {}} onAction={() => {}} />);
      // No facet line of ANY kind in that slot — including the caveat, which a quiet state must not
      // reach. Asserting only the two quiet ids would pass a caveat rendered over an empty list.
      expect(screen.queryByTestId('provenance-inferred-facet'), JSON.stringify(facets)).toBeNull();
      expect(screen.queryByTestId('provenance-measured-facets')).toBeNull();
      expect(screen.queryByTestId('provenance-inference-absent')).toBeNull();
      collapsed.unmount();
    }

    const measured = show(withProvenance({ heuristicFacets: [] }));
    expect(screen.getByTestId('provenance-measured-facets')).toBeInTheDocument();
    measured.unmount();

    show(withProvenance({ heuristicFacets: undefined }));
    expect(screen.getByTestId('provenance-inference-absent')).toBeInTheDocument();
  });

  it('does not caption the inference twice when the caveat is showing', () => {
    // The caveat answers this question already, so the provenance block stays silent on it. Both
    // lines at once would be one doubt captioned twice, which is what makes all such lines skippable.
    show(withProvenance({ heuristicFacets: [INFERRED_FACET] }));

    expect(screen.getByTestId('provenance-inferred-facet')).toBeInTheDocument();
    expect(screen.queryByTestId('provenance-measured-facets')).toBeNull();
    expect(screen.queryByTestId('provenance-inference-absent')).toBeNull();
  });

  it('says the claim rests on an inference in four words, and names the facet behind the click',
     () => {
       show(withProvenance({ heuristicFacets: [INFERRED_FACET] }));

       /*
        * The whole point of the field: a suggestion resting on an inference used to render
        * identically to one resting on a measurement, so the card could not be read correctly. The
        * FACT is all the caveat carries — asserted whole, because a longer line here is how the
        * caveat stopped being read, and the reasoning it dropped is on the line as a title instead.
        */
       expect(screen.getByTestId('provenance-inferred-facet').textContent)
         .toBe('inferred, not measured');
       expect(screen.getByTestId('provenance-inferred-facet').getAttribute('title'))
         .toContain('may not be what happened');

       // And the name, once, with the store tokens it belongs beside.
       expect(screen.getByTestId('provenance-inferred-facet-names').textContent)
         .toBe(`Deduced rather than measured: ${INFERRED_FACET}.`);
       expect(screen.queryByTestId('provenance-measured-facets')).toBeNull();
     });

  it('tells a measured "none inferred" from a bundle written before the field', () => {
    /*
     * The `?? []` catcher. `LIVE` carries `heuristicFacets: []` — the producer looked and every facet
     * was measured directly — and a bundle predating the field carries no key at all. Folding the
     * second into the first would report a confident measurement nobody took.
     */
    const measured = show(LIVE);
    expect(screen.getByTestId('provenance-measured-facets').textContent)
      .toBe('Every facet in the shape above was measured directly, none inferred.');
    expect(screen.queryByTestId('provenance-inference-absent')).toBeNull();
    measured.unmount();

    show(withProvenance({ heuristicFacets: undefined }));
    expect(screen.getByTestId('provenance-inference-absent').textContent)
      .toBe('This report does not say whether the shape above rests on an inferred facet.');
    expect(screen.queryByTestId('provenance-measured-facets')).toBeNull();
  });

  it('keeps the inferred SHAPE apart from the inferred ATTRIBUTION, which is a different doubt',
     () => {
       /*
        * Two independent inferences, and a card can carry either, both or neither: the permission
        * row's caveat says the registry did not declare WHICH COMPONENT fails this way, and this one
        * says WHAT HAPPENED was deduced rather than observed. One sentence covering both would be
        * false in three of those four cases, so neither line may absorb the other — and the
        * attribution caption must still appear exactly once.
        */
       show(withProvenance({
         attributionBasis: 'keyword_inferred',
         matchedFailureMode: '',
         heuristicFacets: [INFERRED_FACET],
       }));

       expect(screen.getAllByText(/inferred attribution, not declared/)).toHaveLength(1);
       expect(screen.getByTestId('provenance-inferred-facet'))
         .toHaveTextContent('inferred, not measured');
       expect(screen.getByTestId('provenance-inferred-facet'))
         .not.toHaveTextContent('attribution');
     });
});

describe('the provenance of a claim', () => {
  it('renders the whole block and nothing else, on the row the producer really serves', () => {
    /*
     * Asserted WHOLE rather than clause by clause, and that is the tenant-isolation test as well as
     * the copy test: this block reads five fields off a shared item, and an exact match is the only
     * assertion that fails if a session id, a run id or an actor id is ever set beside them.
     *
     * `LIVE` carries `heuristicFacets: []`, so the bookkeeping half of that field is here, under the
     * stores it belongs with. The CAVEAT never is — it renders outside the expand, and the assertion
     * below is what keeps it from drifting back in and captioning one doubt twice.
     */
    show(LIVE);
    expect(screen.getByTestId('provenance').textContent).toBe(
      'Judged by us.anthropic.claude-sonnet-5:full_conversation:0.7:runs1 over 1 run.'
      + ' A trial is a single sample, indicative, not proof.'
      + 'Read from aws/spans · ddb:plan-state · harvest:live.'
      + 'Every facet in the shape above was measured directly, none inferred.'
      + 'Attributed against registry 5c3f40f.'
      + 'Judged 29 Aug · first mined 29 Aug.',
    );
    expect(screen.getByTestId('provenance').textContent).not.toContain('rests on an inferred facet');
  });

  it('names the stores the evidence was read from', () => {
    show(LIVE);
    expect(screen.getByTestId('provenance-sources').textContent)
      .toBe('Read from aws/spans · ddb:plan-state · harvest:live.');
  });

  it('tells a producer that named no store from a bundle that predates the field', () => {
    // ONE field changed each time. `[]` is louder than the absence: the producer looked and named
    // nowhere, so there is no store to go back to and re-read the evidence.
    const none = show(withProvenance({ evidenceSources: [] }));
    expect(screen.getByTestId('provenance-sources-none').textContent)
      .toBe('This report names no store the evidence was read from, so it cannot be re-read.');
    none.unmount();

    show(withProvenance({ evidenceSources: undefined as unknown as string[] }));
    expect(screen.getByTestId('provenance-sources-absent').textContent)
      .toBe('This report does not record which stores the evidence was read from.');
    expect(screen.queryByTestId('provenance-sources-none')).toBeNull();
  });

  it('names the registry revision that made the attribution, or says it is not recorded', () => {
    const stated = show(LIVE);
    expect(screen.getByTestId('provenance-registry').textContent)
      .toBe('Attributed against registry 5c3f40f.');
    stated.unmount();

    // ONE field removed: a later revision can blame a different component for the same shape, so a
    // claim with no revision behind it cannot be re-checked at all.
    show(withProvenance({ registryHeadSha: undefined }));
    expect(screen.getByTestId('provenance-registry').textContent)
      .toBe('Which registry revision blamed that component is not recorded.');
  });

  it('dates the RECORD and says so, rather than dating the behaviour', () => {
    /*
     * `firstSeenAt` is a store timestamp — when the loop first wrote this finding down — and on both
     * live rows it is the same instant as `evaluatedAt` and `createdAt`, i.e. the pipeline run that
     * minted the record. So the card must not caption it "first seen" or "first happened": the loop
     * only sees a run inside a harvest window it has run over, and the behaviour can be far older.
     */
    show(LIVE);
    const dates = screen.getByTestId('provenance-dates');
    expect(dates.textContent).toBe('Judged 29 Aug · first mined 29 Aug.');
    expect(dates.textContent).not.toContain('first seen');
    expect(dates.textContent).not.toContain('first happened');
    expect(screen.getByTitle(/first recorded this finding/).getAttribute('title'))
      .toContain('the behaviour may be older than this');
  });

  it('shows the day and keeps the stamp the wire carried on hover', () => {
    // The day alone on screen, as every other date on this surface. Two suggestions judged an hour
    // apart would read alike, so the instant has to stay reachable rather than be discarded.
    show(LIVE);
    expect(screen.getByTitle(/^2026-08-29T08:01:24Z · When the judge scored/))
      .toHaveTextContent('29 Aug');
  });

  it('sets these two dates the way the collapsed reading sets its own, one rule per stamp', () => {
    /*
     * `Stamp` renders in two positions — here, and in the collapsed reading — and both are a running
     * line of words rather than a column, so neither takes the mono spine and `4 Sep` is not an
     * identifier anybody matches character by character. Pinned in BOTH places rather than only at the
     * reading, because a face that varies by call site is how one date on this card comes to sit in a
     * different typeface from the other.
     *
     * The WHOLE style, weight included, rather than the family alone. A stamp that took the body face
     * here and the weight only on the collapsed card would be a second spelling of "a figure in prose"
     * inside one file, which is the drift `ImpactSummary` names when it keeps one spelling per file. In
     * this block the weight costs little either way: every value beside these two dates — the config
     * hash, the judge's run count, the registry revision — is already set apart from the words by its
     * own face, so a date that is not is the odd one out.
     */
    show(LIVE);

    const stamps = [...screen.getByTestId('provenance-dates').querySelectorAll('span[style]')];
    expect(stamps.map((stamp) => stamp.textContent)).toEqual(['29 Aug', '29 Aug']);
    for (const stamp of stamps) {
      expect(familyOf(stamp)).not.toContain('--font-mono');
      expect(familyOf(stamp)).toContain('--font-body');
      expect((stamp as HTMLElement).style.fontWeight).toBe('600');
    }
  });

  it('keeps the identifiers and the counts around them in the spine, which is the boundary', () => {
    /*
     * The same card sets figures two ways on purpose, and this is the half that must not move. A
     * pattern id, a component id, the permission the registry recorded, a judge's config hash, a
     * registry revision and a store token are all names a reader quotes in a bug report; the gate's
     * three counts sit under a label as machine values, not in a sentence. A change that tidied these
     * to match the reading on the collapsed card would take their alignment and their quotability with
     * it.
     */
    show(withPattern({ controlsConsidered: 12, controlsExcluded: 9, controlsInconclusive: 3 }));

    const named = [...screen.getByTestId('suggestion-detail').querySelectorAll('code')];
    // Named rather than counted, so a `<code>` that stops rendering cannot make this pass by absence.
    expect(named.map((element) => element.textContent)).toEqual([
      'has_failed_node', 'validate-retry-loop', 'LOCKED_TODAY',
      'us.anthropic.claude-sonnet-5:full_conversation:0.7:runs1',
      'aws/spans', 'ddb:plan-state', 'harvest:live', '5c3f40f',
    ]);
    const counts = [...screen.getByTestId('pattern-gate').querySelectorAll('span')];
    expect(counts.map((count) => count.textContent)).toEqual(['12', '9', '3']);
    for (const element of [...named, ...counts]) {
      expect(familyOf(element), element.textContent ?? '').toContain('--font-mono');
    }
  });

  it('echoes a stamp it cannot read rather than dropping or inventing one', () => {
    // ONE field changed: `evaluatedAt` becomes a string no date parser should trust. A stamp a
    // reader can see is wrong is diagnosable; a silent one is not.
    show(withProvenance({ evaluatedAt: '2026-8-29 08:01' }));
    expect(screen.getByTestId('provenance-dates').textContent)
      .toBe('Judged 2026-8-29 08:01 · first mined 29 Aug.');
  });

  it('says which date is missing rather than printing a blank beside a label', () => {
    // ONE field removed each time. `firstSeenAt` is genuinely optional on the wire; `evaluatedAt` is
    // declared required and can still be absent, as every other field on this surface can.
    const noMined = show({ ...LIVE, firstSeenAt: undefined });
    expect(screen.getByTestId('provenance-dates').textContent)
      .toBe('Judged 29 Aug · first mined on a date this report does not record.');
    noMined.unmount();

    show(withProvenance({ evaluatedAt: undefined as unknown as string }));
    expect(screen.getByTestId('provenance-dates').textContent)
      .toBe('Judged on a date this report does not record · first mined 29 Aug.');
  });

  it('reports an unreadable list as not stated, never as a measured none', () => {
    /*
     * A value that is not a list, and a list nothing usable survives, are both "this report does not
     * tell me" — NOT "the producer found none". Claiming a measurement on the strength of `[null, 7]`
     * is the same defect as `?? []`, one step further in.
     */
    for (const broken of [{ heuristicFacets: 'inferred' as unknown as string[] },
                          { heuristicFacets: [null, 7] as unknown as string[] },
                          { heuristicFacets: [''] as unknown as string[] }]) {
      const { unmount } = render(
        <SuggestionCard suggestion={withProvenance(broken)} expanded onToggle={() => {}}
                        onAction={() => {}} />);
      expect(screen.getByTestId('provenance-inference-absent'), JSON.stringify(broken))
        .toBeInTheDocument();
      expect(screen.queryByTestId('provenance-measured-facets')).toBeNull();
      expect(screen.queryByTestId('provenance-inferred-facet')).toBeNull();
      unmount();
    }
  });
});

// --- a wire that is not the shape the type promises ---------------------------
describe('a malformed warrant', () => {
  it('renders without throwing, whatever arrives in those six fields', () => {
    const shapes: Partial<FailurePattern>[] = [
      { symptom: 'delivery failed' as unknown as string[] },
      { symptom: [null, 7, ''] as unknown as string[] },
      { equallyDiscriminative: {} as unknown as string[] },
      { controlsConsidered: Number.POSITIVE_INFINITY, controlsExcluded: 1,
        controlsInconclusive: 0 },
      { controlsConsidered: 3, controlsExcluded: undefined, controlsInconclusive: 1 },
    ];
    for (const pattern of shapes) {
      expect(() => {
        const { unmount } = render(
          <SuggestionCard suggestion={withPattern(pattern)} expanded onToggle={() => {}}
                          onAction={() => {}} />);
        unmount();
      }, JSON.stringify(pattern)).not.toThrow();
    }
  });

  it('drops a non-string facet rather than rendering it as one', () => {
    /*
     * Asserted as the WHOLE clause rather than by absence of each bad value, because `Array.join`
     * renders `null` and `''` as nothing on its own — so a check for the word `null` passes whether or
     * not the filter is there. A number does survive a join, and an empty entry leaves a bare
     * separator, so the exact string is the only assertion that can fail.
     */
    show(withPattern({
      equallyDiscriminative: ['real_facet', null, 7, ''] as unknown as string[],
    }));
    expect(screen.getByTestId('pattern-equally-discriminative').textContent)
      .toBe('these separate the same runs just as well: real_facet. So the shape above was chosen '
        + 'by precedence, not by the evidence.');
  });
});

// --- what a reader sees before clicking anything -------------------------------
/**
 * THE OTHER LIVE SUGGESTION, so the two cards on the surface can be read side by side.
 *
 * Derived from `LIVE` by changing only the fields the two rows actually differ in — the ids, the
 * target and its type, the shape, the two evidence lines, the forced-candidate reason and the inferred
 * facet — for the reason every other case in this file is built that way: what is under test is the
 * card's reading of those fields.
 * Its permission is `LOCKED_TODAY` like `LIVE`'s, which is not a simplification but the whole problem
 * this describe block is about: both live rows are locked, so the producer's own one-line `summary` is
 * one sentence with a different component id in it, twice.
 *
 * The dates are `LIVE`'s. The two real rows were mined five days apart, and no assertion here reads a
 * date, so carrying one instant for both keeps the difference between the cards where the test is
 * looking.
 */
const BUILTIN: Suggestion = {
  ...LIVE,
  suggestionId: '8080f09550d0',
  targetType: 'tool',
  targetId: 'builtin-tools',
  targetName: 'builtin tools',
  targetLocation: null,
  summary: 'builtin-tools is implicated, but no change can be proposed (permission: LOCKED_TODAY)',
  evidenceSummary: 'from 1 run where the session finished without fetching the result of the last '
    + 'job it started, although it had fetched results for earlier jobs in the same session',
  evidenceHeadline: "The last job's result was never fetched",
  pattern: {
    patternId: INFERRED_FACET,
    shape: 'from 1 run where the session finished without fetching the result of the last job it '
      + 'started, although it had fetched results for earlier jobs in the same session',
    shapeFacets: [INFERRED_FACET],
    runCount: 1,
    passHits: 0,
  },
  singleCandidateForced: 'no applyable remedy exists for builtin-tools: permission is LOCKED_TODAY '
    + 'and it has no version lineage',
  candidates: [{
    candidateId: 'e167bb0b4490',
    label: 'builtin-tools is implicated, but no change can be proposed (permission: LOCKED_TODAY)',
    diffText: '',
    before: '',
    after: '',
    claim: {
      utilityTerm: 'results_delivered',
      direction: 'increase',
      magnitude: null,
      onPattern: INFERRED_FACET,
    },
    risk: null,
    disposition: 'advisory',
  }],
  provenance: { ...LIVE.provenance, heuristicFacets: [INFERRED_FACET] },
};

/** The collapsed card, which is the state every card in a list is in. */
function showCollapsed(suggestion: Suggestion) {
  return render(<SuggestionCard suggestion={suggestion} expanded={false} onToggle={() => {}}
                                onAction={() => {}} />);
}

function cardText(suggestion: Suggestion): string {
  return screen.getByTestId(`suggestion-${suggestion.suggestionId}`).textContent ?? '';
}

describe('the collapsed card', () => {
  it('leads with what happened, so the two live rows do not read alike', () => {
    /*
     * The defect this whole shape exists to fix. Both live suggestions are locked, so the producer's
     * `summary` is the same sentence twice with a different id substituted, and it used to be the
     * card's most prominent line — two cards that read as one. What distinguishes them is what
     * happened, and that is now the lead.
     */
    const first = showCollapsed(BUILTIN);
    expect(screen.getByTestId('evidence-lead').textContent)
      .toBe("The last job's result was never fetched");
    first.unmount();

    showCollapsed(LIVE);
    expect(screen.getByTestId('evidence-lead').textContent).toBe('A step failed');
  });

  it('keeps the producer’s one-liner and the registry’s own token off it', () => {
    // The two halves of the old second line: a sentence that is identical on every locked card, and
    // the registry constant inside it. Both are behind the disclosure now.
    showCollapsed(BUILTIN);
    expect(cardText(BUILTIN)).not.toContain('is implicated, but no change can be proposed');
    expect(cardText(BUILTIN)).not.toContain('LOCKED_TODAY');
    expect(screen.queryByTestId('suggestion-summary')).toBeNull();
  });

  it('says what a reader may do about it in the reader’s words', () => {
    showCollapsed(BUILTIN);
    expect(screen.getByTestId('permission').textContent).toBe('locked for now');
  });

  it('shows the registry’s own value for a permission this screen has no phrase for', () => {
    /*
     * The registry carries values the `Mutability` union does not name and the producer passes them
     * through verbatim — `BUG` and `INERT` are each live on one component. An invented phrase would
     * report a guess as a decision, so the value is shown as the registry spells it.
     */
    showCollapsed({ ...BUILTIN, mutability: 'BUG' });
    expect(screen.getByTestId('permission').textContent).toBe('BUG');
    expect(screen.getByTestId('permission').getAttribute('title'))
      .toContain('no phrase for the value “BUG”');
  });

  it('says how much evidence there is and when it was last found, as a reading', () => {
    // What the thirty-word clause carried in front of its shape ("from 1 run(s) where …"). No full
    // stop, because it is a reading and not a sentence a reader has to get through.
    showCollapsed(BUILTIN);
    expect(screen.getByTestId('evidence-scale').textContent).toBe('Seen in 1 run, last 29 Aug');
    expect(screen.getByTestId('evidence-scale').textContent).not.toMatch(/[.!?]$/);
  });

  it('sets both of the reading’s figures in the body face, not the numeric spine', () => {
    /*
     * `numericStyle` is mono plus `tabular-nums`, and this line is the case that proves neither belongs
     * here: the `1` and the `29 Aug` sat in a second typeface, at a visibly different width and
     * letter-spacing from the words either side of them, and read as a rendering fault rather than as
     * emphasis. Tabular digits earn a column and mono earns an identifier a reader matches character by
     * character; a run of words is neither. Weight carries what the face was doing, so the figure is
     * still the part of the line the eye lands on first.
     *
     * BOTH figures, because they were set the same way and one of them comes from `Stamp`, which is
     * shared with the dates behind the disclosure — so a fix applied to only the count would leave the
     * two halves of one reading in two typefaces.
     */
    showCollapsed(BUILTIN);

    const figures = [...screen.getByTestId('evidence-scale').querySelectorAll('span[style]')];
    expect(figures.map((figure) => figure.textContent)).toEqual(['1', '29 Aug']);
    for (const figure of figures) {
      expect(familyOf(figure), figure.textContent ?? '').not.toContain('--font-mono');
      expect(familyOf(figure), figure.textContent ?? '').toContain('--font-body');
      expect((figure as HTMLElement).style.fontWeight, figure.textContent ?? '').toBe('600');
    }
  });

  it('reports an unstated run count as unstated, rather than as a zero or a bare noun', () => {
    /*
     * `runCount` is declared required and is absent from rows written before the producer derived it,
     * so the card leads with a figure the wire can fail to carry. A `0` would be a measurement nobody
     * took and a bare "runs" would read as a render fault, so the count drops out of the reading and
     * joins the withheld warrant behind the click — one line, with the other facts the report withheld.
     */
    const collapsed = showCollapsed(
      { ...BUILTIN, pattern: { ...BUILTIN.pattern, runCount: undefined as unknown as number } });
    expect(screen.getByTestId('evidence-scale').textContent).toBe('Last seen 29 Aug');
    expect(cardText(BUILTIN)).not.toContain('0 run');
    collapsed.unmount();

    show({ ...BUILTIN, pattern: { ...BUILTIN.pattern, runCount: undefined as unknown as number } });
    expect(screen.getByTestId('pattern-warrant-absent'))
      .toHaveTextContent('how many runs it rests on');
  });

  it('draws no reading at all when the wire carried neither figure', () => {
    // Not an empty line and not a dangling separator: there is nothing to report, so nothing is.
    showCollapsed({
      ...BUILTIN,
      lastSeenAt: undefined,
      pattern: { ...BUILTIN.pattern, runCount: undefined as unknown as number },
    });
    expect(screen.queryByTestId('evidence-scale')).toBeNull();
    expect(screen.getByTestId('provenance-inferred-facet')).toBeInTheDocument();
  });
});

// --- the lead when the producer sent no headline --------------------------------
describe('a bundle whose producer predates the headline', () => {
  /** The live row with the field the deployed producer may not have sent. */
  const noHeadline: Suggestion = { ...BUILTIN, evidenceHeadline: undefined };

  it('degrades to the long clause, which is what the reader had before', () => {
    /*
     * NOT a blank line. The field is omitted for a leading facet the producer has no phrase for, a
     * row that stopped reproducing is frozen with whatever it was written with, and a deploy can
     * serve an SPA newer than the producer behind it. All three land here, and the sentence a reader
     * had before this change is still true and still distinguishes one card from the next.
     */
    showCollapsed(noHeadline);
    expect(screen.getByTestId('evidence-lead').textContent).toBe(noHeadline.evidenceSummary);
  });

  it('treats an empty headline as absent, so no producer can leave a hole in the lead', () => {
    for (const nothing of ['', '   ']) {
      const { unmount } = showCollapsed({ ...BUILTIN, evidenceHeadline: nothing });
      expect(screen.getByTestId('evidence-lead').textContent, JSON.stringify(nothing))
        .toBe(BUILTIN.evidenceSummary);
      unmount();
    }
  });

  it('never falls back to the facet name, which is what the field exists to avoid', () => {
    // The producer OMITS the headline rather than emitting `session_ended_on_unretrieved_dispatch`,
    // so a consumer that filled the gap from `shapeFacets` would undo the whole point of the field.
    showCollapsed(noHeadline);
    expect(screen.getByTestId('evidence-lead').textContent).not.toContain(INFERRED_FACET);
    expect(cardText(noHeadline)).not.toContain(INFERRED_FACET);
  });
});

// --- the disclosure --------------------------------------------------------------
describe('the disclosure', () => {
  /** A suggestion the producer could propose no change for at all. */
  const noCandidate: Suggestion = { ...BUILTIN, candidates: [], singleCandidateForced: null };

  it('is labelled for what it opens, not for a change that may not exist', () => {
    // `see change` promised a diff on cards that have none — every locked one — and a control whose
    // label is wrong is how a reader learns to stop trusting the controls.
    const collapsed = showCollapsed(BUILTIN);
    expect(screen.getByTestId('toggle-detail').textContent).toBe('▸ Details');
    collapsed.unmount();

    show(BUILTIN);
    expect(screen.getByTestId('toggle-detail').textContent).toBe('▾ Details');
  });

  it('opens with real content when there is no candidate, rather than opening nothing', () => {
    /*
     * THE DEAD CONTROL. The body used to be `expanded && candidate && …`, so a suggestion with no
     * candidate rendered a button that did nothing at all when clicked. Everything here is a property
     * of the SUGGESTION rather than of a candidate, so there is always something to show.
     */
    show(noCandidate);
    expect(screen.getByTestId('suggestion-detail')).toBeInTheDocument();
    expect(screen.getByTestId('suggestion-summary').textContent).toBe(BUILTIN.summary);
    expect(screen.getByTestId('provenance')).toBeInTheDocument();
    expect(screen.getByTestId('pattern-warrant-absent')).toBeInTheDocument();
    expect(screen.getByTestId('provenance-inferred-facet-names').textContent)
      .toBe(`Deduced rather than measured: ${INFERRED_FACET}.`);
  });

  it('withholds only the candidate’s own parts when there is no candidate', () => {
    // The diff, the claim and the two actions that act ON a variant. A "Try both" with nothing to try
    // would be the dead control this disclosure just stopped being.
    show(noCandidate);
    expect(screen.queryByTestId('diff-empty')).toBeNull();
    expect(screen.queryByTestId('diff-unified')).toBeNull();
    expect(screen.queryByTestId('action-try-both')).toBeNull();
    expect(screen.queryByTestId('action-publish')).toBeNull();
    // Still dismissable and still escalatable: neither needs a variant.
    expect(screen.getByTestId('action-dismiss')).toBeInTheDocument();
    expect(screen.getByTestId('action-send-to-team')).toBeInTheDocument();
  });

  it('says the producer’s one-liner once, not twice', () => {
    /*
     * `propose._summary` sets `summary` to the first candidate's label, so on a card that HAS a
     * candidate the switcher below is already showing this exact sentence — and printing it above as
     * well would caption one statement twice. Exact equality only, so a near-copy keeps both.
     */
    const echoed = show(BUILTIN);
    expect(screen.queryByTestId('suggestion-summary')).toBeNull();
    expect(screen.getByTestId('candidate-e167bb0b4490').textContent).toContain(BUILTIN.summary);
    echoed.unmount();

    show({ ...BUILTIN, summary: 'Stop the session ending on an unfetched job' });
    expect(screen.getByTestId('suggestion-summary').textContent)
      .toBe('Stop the session ending on an unfetched job');
  });

  it('keeps the registry’s permission token here, as the identifier it is', () => {
    // The reader's phrase is on the collapsed card; this is the spelling to quote in a bug report,
    // set in the surface's identifier treatment inside a sentence saying what it is.
    show(BUILTIN);
    expect(screen.getByTestId('suggestion-detail').textContent)
      .toContain('permission recorded as LOCKED_TODAY');
  });
});
