/**
 * The built-in skill catalog the deploy writes to DynamoDB.
 *
 * The Skills tab's built-in panel paints from a DynamoDB row rather than from the agent
 * runtime, and that row is produced at synth time by reading each package's `SKILL.md`
 * under `agent/skills`.
 * So a parse failure here is not a crash — it is a skill listed with a blank
 * description, or a skill missing from the panel entirely, with nothing anywhere saying
 * so. These tests run the real reader against the real packages for that reason.
 *
 * Lives in `ui/` because that is where vitest runs; `infra/` has no test runner, and
 * `capability-api.routing.contract.test.ts` / `outbox-environment.contract.test.ts`
 * already reach across into `infra/lib` from here.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, existsSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  parseSkillDescription,
  readBuiltinSkills,
} from '../../infra/lib/builtin-skills';

const here = dirname(fileURLToPath(import.meta.url));
const SKILLS_DIR = resolve(here, '../../agent/skills');

/** The packages that actually ship, determined the same way the panel's users would. */
function shippedSlugs(): string[] {
  return readdirSync(SKILLS_DIR)
    .filter((slug) => existsSync(join(SKILLS_DIR, slug, 'SKILL.md')))
    .sort();
}

describe('built-in skill catalog', () => {
  it('lists exactly the packages that ship', () => {
    // Asserted against the directory rather than a fixed count, because the failure
    // being guarded is "someone added a skill and it never appeared".
    const names = readBuiltinSkills(SKILLS_DIR).map((s) => s.name);
    expect(names).toEqual(shippedSlugs());
  });

  it('gives every shipped skill a real description', () => {
    // Per-skill, not in aggregate: one blank description renders as an empty line in
    // the panel, which reads as a UI bug rather than a parse failure.
    for (const skill of readBuiltinSkills(SKILLS_DIR)) {
      expect(skill.description, `${skill.name} has no description`).toBeTruthy();
      expect(skill.description.length, `${skill.name}'s description is suspiciously short`)
        .toBeGreaterThan(20);
    }
  });

  it('strips the quotes YAML required, rather than rendering them', () => {
    // `media-references` and `video-processing` quote their description because it
    // contains a colon. Left quoted, the panel shows a leading `"`.
    for (const skill of readBuiltinSkills(SKILLS_DIR)) {
      expect(skill.description.startsWith('"'), `${skill.name} leaks a quote`).toBe(false);
      expect(skill.description.startsWith("'"), `${skill.name} leaks a quote`).toBe(false);
      expect(skill.description.endsWith('"'), `${skill.name} leaks a quote`).toBe(false);
    }
  });

  it('is stably ordered, so the catalog hash does not churn', () => {
    // The stack hashes this list into the custom resource's physical id. An unsorted
    // readdir would reorder across machines and produce a spurious diff — and therefore
    // a rewrite — on every deploy.
    const once = readBuiltinSkills(SKILLS_DIR).map((s) => s.name);
    expect(once).toEqual([...once].sort());
  });

  describe('parseSkillDescription', () => {
    it('takes description, not merely the line after name', () => {
      // `render-media` declares `handles:` too, so position-based parsing picks up the
      // wrong value.
      const md = '---\nname: x\nhandles: video/mp4\ndescription: The real one.\n---\n# body\n';
      expect(parseSkillDescription(md)).toBe('The real one.');
    });

    it('unescapes a double-quoted scalar', () => {
      const md = '---\nname: x\ndescription: "Use for: things, and \\"quoted\\" bits."\n---\n';
      expect(parseSkillDescription(md)).toBe('Use for: things, and "quoted" bits.');
    });

    it('returns empty rather than throwing on absent frontmatter', () => {
      // Empty means "listed with no description"; throwing would fail the whole synth
      // over one malformed package.
      expect(parseSkillDescription('# just a heading\n')).toBe('');
      expect(parseSkillDescription('---\nname: x\n---\n')).toBe('');
    });
  });
});
