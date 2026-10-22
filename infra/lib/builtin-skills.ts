/**
 * The built-in skill catalog, read from the shipped packages at synth time.
 *
 * `agent/skills/*` is the source of truth for what ships. The CDK stack reads it here
 * and writes the result to DynamoDB during `cdk deploy`, so the Skills tab's built-in
 * panel can paint from a table instead of waking the agent runtime — and so a
 * `SKILL.md` edit propagates on a deploy alone, with no container image to rebuild.
 *
 * Extracted from the stack so it can be tested against the real packages: the parsing
 * is small but not trivial (two of the seven quote their description because it
 * contains a colon, and one carries an extra frontmatter key), and a silent parse
 * failure would show up as a skill with a blank description rather than as an error.
 */
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, relative, sep } from "node:path";

export interface BuiltinSkill {
  name: string;
  description: string;
  /** The whole SKILL.md. The detail view renders it (Preview parses, Source shows raw). */
  skillMd: string;
  /**
   * Package resources beside SKILL.md — NAMES only.
   *
   * The detail view lists them and counts them (`supportingFiles.length + 1`); it never
   * shows their contents. Carrying the bytes would put ~116 KB of one package into a
   * DynamoDB item to render a filename.
   */
  supportingFiles: string[];
}

/** Every file under `dir` except SKILL.md, as slash-separated relative paths. */
function packageFiles(dir: string): string[] {
  const out: string[] = [];
  const walk = (current: string) => {
    for (const entry of readdirSync(current).sort()) {
      const full = join(current, entry);
      if (statSync(full).isDirectory()) {
        walk(full);
        continue;
      }
      const rel = relative(dir, full).split(sep).join("/");
      if (rel !== "SKILL.md") out.push(rel);
    }
  };
  walk(dir);
  return out.sort();
}

/**
 * Extract a `description:` from a `SKILL.md`'s YAML frontmatter.
 *
 * Deliberately not a YAML parser: the stack has no yaml dependency, and this reads one
 * scalar out of a two-or-three-key block. Returns `""` when there is no frontmatter or
 * no description, which the caller surfaces as a skill with no description rather than
 * dropping the skill.
 */
export function parseSkillDescription(skillMd: string): string {
  const frontmatter = skillMd.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!frontmatter) return "";

  // Keyed off `description:` rather than position: `render-media` also declares
  // `handles:`, so "the line after name" is not the description.
  const line = frontmatter[1]
    .split(/\r?\n/)
    .find((l) => l.startsWith("description:"));
  if (!line) return "";

  let value = line.replace(/^description:\s*/, "").trim();

  // `media-references` and `video-processing` quote the value because it contains a
  // colon, which YAML requires. Left as-is the quotes render in the UI.
  const quote = value[0];
  if ((quote === '"' || quote === "'") && value.endsWith(quote) && value.length > 1) {
    value = value.slice(1, -1);
    if (quote === '"') {
      value = value.replace(/\\"/g, '"').replace(/\\\\/g, "\\");
    }
  }
  return value;
}

/**
 * Every shipped skill package, sorted by slug.
 *
 * Enumerated from disk rather than hand-listed so adding a package cannot silently omit
 * it from the catalog — the failure a fixed list would produce is an absent skill, with
 * nothing anywhere saying so. A directory without a `SKILL.md` is not a skill package
 * and is skipped.
 *
 * Sorted for a stable catalog hash: an unsorted `readdirSync` would reorder across
 * machines and produce a spurious resource diff on every deploy.
 */
export function readBuiltinSkills(skillsDir: string): BuiltinSkill[] {
  if (!existsSync(skillsDir)) return [];
  const out: BuiltinSkill[] = [];
  for (const slug of readdirSync(skillsDir).sort()) {
    const packageDir = join(skillsDir, slug);
    const skillMdPath = join(packageDir, "SKILL.md");
    if (!existsSync(skillMdPath)) continue;
    const skillMd = readFileSync(skillMdPath, "utf-8");
    out.push({
      name: slug,
      description: parseSkillDescription(skillMd),
      skillMd,
      supportingFiles: packageFiles(packageDir),
    });
  }
  return out;
}

/**
 * Largest item we will write, well under DynamoDB's hard 400 KB.
 *
 * The margin is for the attribute-name and JSON overhead the low-level `putItem` shape
 * adds on top of the raw text. Exceeding this fails at synth with a readable message
 * instead of at deploy with `ValidationException: Item size has exceeded`, halfway
 * through writing the catalog.
 */
export const MAX_SKILL_ITEM_BYTES = 300_000;

/**
 * Write the catalog to a file for the deploy to upload as an S3 asset.
 *
 * The bodies must NOT ride in the CloudFormation template. They were inlined as
 * `putItem` properties first, and that failed the deploy outright:
 *
 *   ValidationError: Template may not exceed 1000000 bytes in size.
 *
 * The template was already at ~895 KB before these seven skills — 685 KB of it is one
 * Cognito `ManagedLoginBranding` resource, which inlines a 342 KB base64 login logo TWICE
 * (LIGHT and DARK). 163 KB of skill bodies went over the ceiling. Shipping them as an
 * asset instead means the template carries only a bucket and a key, and the catalog can
 * grow toward DynamoDB's real limits rather than a shared 1 MB budget it does not control.
 *
 * The file's CONTENT HASH becomes the asset's S3 key, which is what makes a `SKILL.md`
 * edit reach the table: the key is a custom-resource property, so a new one is a diff
 * CloudFormation acts on. Stable serialisation therefore matters — `readBuiltinSkills`
 * sorts, and the two-space `JSON.stringify` here is deterministic — or every deploy would
 * rewrite the catalog for no reason.
 *
 * Returns the path to hand to `s3assets.Asset`.
 */
export function stageBuiltinSkillCatalog(
  skills: BuiltinSkill[],
  outDir: string,
): string {
  mkdirSync(outDir, { recursive: true });
  const outPath = join(outDir, "builtin-skill-catalog.json");
  writeFileSync(outPath, JSON.stringify({ skills }, null, 2), "utf-8");
  return outPath;
}

/** Throws if any package would make an item DynamoDB will not accept. */
export function assertSkillItemsFit(skills: BuiltinSkill[]): void {
  for (const skill of skills) {
    const bytes = Buffer.byteLength(JSON.stringify(skill), "utf-8");
    if (bytes > MAX_SKILL_ITEM_BYTES) {
      throw new Error(
        `built-in skill "${skill.name}" would be ${bytes} bytes, over the ` +
          `${MAX_SKILL_ITEM_BYTES} limit for one DynamoDB item. Trim SKILL.md, or move ` +
          `the catalog to S3 with a pointer in the row.`,
      );
    }
  }
}
