/**
 * The reader's vocabulary, and the one thing it must never do: put the engine's identifiers on
 * screen.
 *
 * Every `reason` string asserted below is real. Five distinct forms appear across the 13 runs served
 * to actor `68e153d0-f0b1-7072-91bc-84425b517126` in staging `mediaworks-harness-improve`
 * (`sk SNAPSHOT`, `createdAt 2026-09-03T08:01:59Z`) and all five are here. The two the live harvest
 * does not contain — the two UNSCORED forms — are not invented either: they are spelled exactly as
 * `gate()` in `agent/src/improve/evaluate.py` builds them, which is the single place any of these
 * strings comes from, and each such test says so.
 *
 * No DOM here, so no jsdom pragma: these are pure functions and the components' own tests cover the
 * rendering.
 */
import { describe, expect, it } from 'vitest';

import { sharePercent } from './improveChrome';
import {
  VERDICT, checkLabel, classLabel, explainVerdict, formatRate, formatScore, outcomeWord,
  permissionLabel, verdictWord,
} from './verdictWording';

/** Every identifier the wire can put in one of these strings today: the two judged tiers, the two
 *  code evaluators in the conversational evaluator set, and the two for the other classes. */
const WIRE_NAMES = ['dispatch_correct', 'results_delivered', 'DagCompletionValidator',
                    'AsyncJobFlowValidator', 'ToolDiscoveryValidator', 'ControlPlaneOpValidator'];

describe('check and class names', () => {
  it('never leaves a known identifier in the phrase it renders', () => {
    for (const name of WIRE_NAMES) {
      expect(checkLabel(name)).not.toContain(name);
      expect(checkLabel(name, 'failed')).not.toContain(name);
    }
    for (const name of ['conversational', 'plan_node', 'tool_discovery', 'control_plane',
                        'unclassified']) {
      expect(classLabel(name)).not.toContain(name);
    }
  });

  it('says the requirement one way and its failure the other, never the same string twice', () => {
    // "5 requests — delivered a usable result" reads as five successes. The two phrasings are what
    // stops the scorecard's failure counts from reading as passes.
    expect(checkLabel('results_delivered')).toBe('delivered a usable result in the session');
    expect(checkLabel('results_delivered', 'failed')).toBe('no usable result reached the user');
    for (const name of WIRE_NAMES) {
      expect(checkLabel(name)).not.toBe(checkLabel(name, 'failed'));
    }
  });

  it('names an unphrased check as unphrased, and keeps the identifier inside that phrase', () => {
    // The producer can add an evaluator and ship before this file does. A blank would read as a
    // render fault, and the bare identifier as a leak — so it is named AS unrecognised.
    expect(checkLabel('SomeNewValidator'))
      .toBe('a check this screen has no phrase for (SomeNewValidator)');
    expect(checkLabel('')).toBe('a check this screen has no phrase for (unnamed)');
    expect(classLabel('brand_new_class'))
      .toBe('a kind of work this screen has no phrase for (brand_new_class)');
  });
});

describe('outcome and verdict words', () => {
  it('keeps no-evidence and does-not-apply apart, because the engine does', () => {
    // `gate()` treats them differently and the difference decides a verdict: NO_DATA blocks a pass,
    // NOT_APPLICABLE cannot block anything. Collapsing them into one word would hide that.
    expect(outcomeWord('NO_DATA')).toBe('no evidence');
    expect(outcomeWord('NOT_APPLICABLE')).toBe('does not apply');
    expect(outcomeWord('PASS')).toBe('met');
    expect(outcomeWord('FAIL')).toBe('not met');
  });

  it('never renders an outcome or verdict enum as itself', () => {
    for (const outcome of ['PASS', 'FAIL', 'NO_DATA', 'NOT_APPLICABLE']) {
      expect(outcomeWord(outcome)).not.toContain(outcome);
    }
    for (const overall of ['PASS', 'FAIL', 'UNSCORED']) {
      expect(verdictWord(overall)).not.toContain(overall);
    }
  });

  it('gives UNSCORED a word of its own, not a shade of pass', () => {
    expect(verdictWord('UNSCORED')).toBe('not judged');
    expect(verdictWord('UNSCORED')).not.toContain('pass');
    // And the caveat says it in words, because the badge alone cannot.
    expect(VERDICT.UNSCORED.title).toContain('not a pass');
  });
});

describe('formatScore', () => {
  it('sets a measured zero as a score and not as an absence', () => {
    // The live bundle's failing runs score delivery at exactly 0. `0` unformatted reads as
    // "nothing"; `0.00` reads as the measurement it is.
    expect(formatScore(0)).toBe('0.00');
  });

  it('keeps two places at the precision the judge emits', () => {
    expect(formatScore(0.9392857142857143)).toBe('0.94');
    expect(formatScore(0.7000000000000001)).toBe('0.70');
    expect(formatScore(1)).toBe('1.00');
  });

  it('refuses a score that is not a number rather than printing NaN', () => {
    expect(formatScore(Number.NaN)).toBe('not a score');
  });
});

describe('formatRate', () => {
  it('reads the live population rate as whole percents', () => {
    expect(formatRate(0.8)).toBe('80%');
    expect(formatRate(0.6428571428571429)).toBe('64%');
    expect(formatRate(1)).toBe('100%');
    expect(formatRate(0)).toBe('0%');
  });

  it('refuses to round a rate to 100% while something failed', () => {
    // `100%` beside a listed failure is a contradiction a reader resolves by distrusting both.
    expect(formatRate(0.998)).toBe('>99%');
    expect(formatRate(0.002)).toBe('<1%');
  });

  it('refuses a rate that is not a number, where the shared share rule would print one', () => {
    // The refusal is this call site's own and is the one thing delegating the arithmetic could
    // silently lose: `sharePercent` does not validate, so an unguarded non-finite figure arrives in
    // the label looking like a reading.
    expect(formatRate(Number.NaN)).toBe('not a rate');
    expect(formatRate(Number.POSITIVE_INFINITY)).toBe('not a rate');
    expect(formatRate(Number.NaN)).not.toBe('NaN%');
    expect(formatRate(Number.POSITIVE_INFINITY)).not.toBe('NaN%');
    // Not inherited from the shared rule, which hands both of these back as figures.
    expect(sharePercent(Number.NaN)).not.toBe('not a rate');
    expect(sharePercent(Number.POSITIVE_INFINITY)).not.toBe('not a rate');
  });

  it('takes its percents from the surface-wide share rule, not arithmetic of its own', () => {
    /*
     * The LINKAGE rather than the wording, which the cases above already pin. Every expectation is
     * `sharePercent`'s live answer and never a literal, so a copy of the rule reintroduced here fails
     * the moment it disagrees with the one the tiles and bars beside this block use — and that
     * disagreement is the defect worth guarding: the rule was written out three times, and three
     * spellings of "a share that exists must not print 0%" are three chances to lose one.
     *
     * Swept across every input the rule turns on — none, a share that rounds to nothing, the rounding
     * boundary, a real rate, a hair short of all, all — so a private copy would have to reproduce the
     * whole rule to survive rather than the one case a single assertion happened to probe. `0.005` is
     * in the sweep although no wording case above pins it: it is where a copy that agrees on every
     * printed figure can still disagree.
     */
    for (const rate of [0, 0.002, 0.005, 0.6428571428571429, 0.998, 1]) {
      expect(formatRate(rate), `rate ${rate}`).toBe(sharePercent(rate));
    }
  });
});

describe('explainVerdict, on the five reason forms the live bundle carries', () => {
  it('translates a single gating failure into what did not happen', () => {
    const explained = explainVerdict('gating: results_delivered:FAIL');
    expect(explained.recognised).toBe(true);
    expect(explained.sentence).toBe('no usable result reached the user');
    expect(explained.sentence).not.toContain('results_delivered');
    expect(explained.sentence).not.toContain('FAIL');
  });

  it('names both checks when two of them failed, in the producer\'s order', () => {
    const explained = explainVerdict('gating: dispatch_correct:FAIL; results_delivered:FAIL');
    expect(explained.sentence)
      .toBe('the wrong work was started, or none; no usable result reached the user');
  });

  it('adds nothing to a plain pass, because the verdict word already says it', () => {
    const explained = explainVerdict('gating passed');
    // Recognised with no sentence — the two together are what let the row stay silent instead of
    // printing "every gating check passed" beside a badge reading "passed".
    expect(explained.recognised).toBe(true);
    expect(explained.sentence).toBeNull();
    expect(explained.informational).toEqual([]);
  });

  it('carries an informational check off a passing run without letting it read as the verdict',
     () => {
       const explained = explainVerdict(
         'gating passed (informational: AsyncJobFlowValidator:FAIL)');
       expect(explained.recognised).toBe(true);
       expect(explained.sentence).toBeNull();
       expect(explained.informational)
         .toEqual([{ name: 'AsyncJobFlowValidator', outcome: 'FAIL' }]);
     });

  it('carries an informational check whose evidence was missing, apart from one that failed', () => {
    const explained = explainVerdict(
      'gating passed (informational: DagCompletionValidator:NO_DATA)');
    expect(explained.informational)
      .toEqual([{ name: 'DagCompletionValidator', outcome: 'NO_DATA' }]);
  });
});

describe('explainVerdict, on the forms the live harvest has no example of', () => {
  /*
   * Neither string is invented. Both are spelled exactly as `gate()` builds them
   * (`agent/src/improve/evaluate.py`): the UNSCORED branches fire when no gating evaluator measured
   * a failure and at least one could not measure at all, which no run in this harvest hit.
   */
  it('reports a run nothing could measure as neither a pass nor a failure', () => {
    const explained = explainVerdict(
      'could not measure: results_delivered:NO_DATA. Absent evidence is not a failure; this run '
      + 'is neither a defect nor a negative control');
    expect(explained.recognised).toBe(true);
    expect(explained.sentence)
      .toBe('nothing measured whether delivered a usable result in the session');
    expect(explained.sentence).not.toContain('NO_DATA');
  });

  it('separates "no check was configured" from "no check applied"', () => {
    // Two different facts with the same verdict: one is a gap in the loop's configuration, the
    // other a run the configured checks genuinely do not cover.
    expect(explainVerdict('nothing scorable (no gating evaluator was configured)').sentence)
      .toBe('no check was configured to judge this kind of work, so nothing could pass or fail');
    expect(explainVerdict(
      'nothing scorable (every gating evaluator reported NOT_APPLICABLE: AsyncJobFlowValidator, '
      + 'DagCompletionValidator)').sentence)
      .toBe('none of the checks for this kind of work applied to it, so nothing could pass or fail');
  });

  it('names the gating checks that could not measure while a sibling failed', () => {
    // A failure established on partial evidence is weaker than one established on all of it, and
    // this clause is the only thing on the wire that says which half was missing.
    const explained = explainVerdict(
      'gating: dispatch_correct:FAIL (on partial evidence: results_delivered:NO_DATA)');
    expect(explained.sentence).toBe('the wrong work was started, or none');
    expect(explained.partial).toEqual(['delivered a usable result in the session']);
  });
});

describe('explainVerdict, on a string it does not recognise', () => {
  it('reports that it could not read it rather than falling back to the raw text', () => {
    // The fallback a caller must not have. Returning the string would put
    // `gating: results_delivered:FAIL` on screen, which is the whole reason this function exists.
    for (const reason of ['', 'verdict computed by a future producer', 'GATING: results:FAIL']) {
      const explained = explainVerdict(reason);
      expect(explained.recognised).toBe(false);
      expect(explained.sentence).toBeNull();
    }
  });
});

/**
 * PERMISSION, the one phrase two surfaces both need.
 *
 * The Σ map's row and a suggestion's card each name the permission of the same component from the same
 * registry value, so the phrasing lives here rather than beside either of them: two copies would let
 * one screen say "locked for now" where the other said `LOCKED_TODAY`, about one component.
 *
 * The values below are the registry's own. `LOCKED` and `LOCKED_TODAY` are what the live suggestions
 * carry, and `N/A` is live on `dag-node-prompts` in both spellings the registry uses.
 */
describe('permission', () => {
  it('answers the registry\'s values in the reader\'s words', () => {
    expect(permissionLabel('LOCKED_TODAY')).toBe('locked for now');
    expect(permissionLabel('LOCKED')).toBe('locked');
    expect(permissionLabel('FIX_FIRST')).toBe('needs an engineer');
    expect(permissionLabel('AUTO_CANDIDATE')).toBe('you can edit');
  });

  it('keeps "not assessed" and "not applicable" apart, in both spellings of the second', () => {
    // Opposite claims one word apart: nobody has assessed this component, versus the registry
    // assessed it and recorded that permission does not apply. They may not share a phrase.
    expect(permissionLabel('UNRECORDED')).toBe('not assessed');
    expect(permissionLabel('N/A')).toBe('not applicable');
    expect(permissionLabel('n/a')).toBe('not applicable');
  });

  it('answers nothing at all for a value it has no phrase for', () => {
    /*
     * `undefined` rather than a fallback phrase, unlike every other function in this module. The
     * fallback used to be `UNRECORDED`'s own wording, so every value off the union was reported as the
     * one state default-deny is ABOUT — and the registry really does carry values the union does not
     * name: `BUG` and `INERT` are each live on one component. Each caller shows the value as the
     * registry spells it, which reads as untranslated rather than as a decision.
     */
    for (const unknown of ['BUG', 'INERT', 'LOCKED_TOMORROW', '']) {
      expect(permissionLabel(unknown), unknown).toBeUndefined();
    }
  });
});
