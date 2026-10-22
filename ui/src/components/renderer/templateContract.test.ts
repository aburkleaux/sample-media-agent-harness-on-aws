import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Schema-compliance gate between the Render Agent and the renderer.
 *
 * A manifest naming a template the registry does not have renders as nothing —
 * silently, with no error anywhere. That is the failure this file exists to catch,
 * and it is invisible to every other test because the agent and the UI are separate
 * artifacts on separate pipelines: a skill can be authored, deployed and "working"
 * while its view never appears.
 *
 * Both sides are parsed from source, so drift fails here rather than on staging.
 */

const REPO = join(__dirname, '..', '..', '..', '..');
const REGISTRY = join(__dirname, 'RendererRegistry.ts');
// The routing table that actually serves Render Agent manifests. Distinct from
// RendererRegistry, which serves the direct-artifact path.
const ROUTER = join(__dirname, 'templates', 'ManifestRouter.ts'.replace('.ts', '.tsx'));
// The directory the CDK bundler ships (agent/src/skills is NOT deployed).
const SKILLS_DIR = join(REPO, 'agent', 'skills');

function registeredTemplates(): Set<string> {
  const src = readFileSync(REGISTRY, 'utf8');
  const names = new Set<string>();
  // Registration is `registry.set('video-markers', VideoMarkers);`
  for (const m of src.matchAll(/registry\.set\(\s*['"]([a-z0-9-]+)['"]/g)) names.add(m[1]);
  return names;
}

function manifestRoutedTemplates(): Set<string> {
  const src = readFileSync(ROUTER, 'utf8');
  const block = /const TEMPLATES: Record<string, TemplateRenderer> = \{([\s\S]*?)\};/.exec(src);
  if (!block) throw new Error('TEMPLATES map not found in ManifestRouter');
  return new Set(Array.from(block[1].matchAll(/['"]([a-z0-9-]+)['"]\s*:/g), (m) => m[1]));
}

function skillDeclarations(): { name: string; template: string; file: string }[] {
  if (!existsSync(SKILLS_DIR)) return [];
  const out: { name: string; template: string; file: string }[] = [];
  for (const entry of readdirSync(SKILLS_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    // Consolidated bundle: the per-type mappings live in render-*/references/*.md, and the
    // bundle's own SKILL.md is a router with no `template:`. Read the references when present.
    for (const file of mappingFiles(entry.name)) {
      if (!existsSync(file)) continue;
      const md = readFileSync(file, 'utf8');
      if (!md.startsWith('---')) continue;
      const fm = md.split('---')[1] ?? '';
      const name = /^name:\s*(.+)$/m.exec(fm)?.[1]?.trim() ?? entry.name;
      const template = /^template:\s*(.+)$/m.exec(fm)?.[1]?.trim() ?? '';
      if (template) out.push({ name, template, file });
    }
  }
  return out;
}

/** SKILL.md frontmatter files that carry mappings for a skill dir: references/*.md if the dir is
 *  a consolidated bundle, else the dir's own SKILL.md. */
function mappingFiles(dirName: string): string[] {
  const refsDir = join(SKILLS_DIR, dirName, 'references');
  if (existsSync(refsDir)) {
    return readdirSync(refsDir)
      .filter((f) => f.endsWith('.md'))
      .map((f) => join(refsDir, f));
  }
  return [join(SKILLS_DIR, dirName, 'SKILL.md')];
}

describe('renderer template contract', () => {
  it('registers the templates this project relies on', () => {
    const reg = registeredTemplates();
    for (const t of ['nle-timeline', 'composite-timeline', 'video-markers',
                     'video-annotations', 'image-annotated', 'data-table', 'generic-preview']) {
      expect(reg.has(t), `${t} must be registered`).toBe(true);
    }
  });

  it('every manifest-routed template is also in the artifact registry', () => {
    // Two tables must not drift apart again; a name in one and not the other is
    // the failure that produced generic previews for specialised results.
    const router = manifestRoutedTemplates();
    const reg = registeredTemplates();
    for (const t of router) {
      if (t === 'manifest-router') continue;
      expect(reg.has(t), `${t} is manifest-routed but absent from RendererRegistry`).toBe(true);
    }
  });

  it('every render skill names a MANIFEST-ROUTED template', () => {
    // Checking RendererRegistry alone gave false confidence: video-markers,
    // image-annotated and data-table were registered there yet absent from
    // ManifestRouter, so every such manifest silently resolved to GenericPreview.
    const reg = manifestRoutedTemplates();
    const skills = skillDeclarations();
    expect(skills.length, 'render skills should exist — an empty skills dir is why the agent only ever emitted two templates').toBeGreaterThan(0);
    for (const s of skills) {
      expect(reg.has(s.template), `skill ${s.name} declares "${s.template}", which no renderer provides`).toBe(true);
    }
  });

  it('every template the agent selects in code is MANIFEST-ROUTED', () => {
    const reg = manifestRoutedTemplates();
    const agent = readFileSync(join(REPO, 'agent', 'src', 'render_agent.py'), 'utf8');
    const selected = new Set<string>();
    for (const m of agent.matchAll(/manifest\["template"\]\s*=\s*"([a-z0-9-]+)"/g)) selected.add(m[1]);
    expect(selected.size).toBeGreaterThan(0);
    for (const t of selected) {
      expect(reg.has(t), `render_agent selects "${t}", which no renderer provides`).toBe(true);
    }
  });

  it('fails when a skill declares an unknown template', () => {
    // Guards the guard: proves the check above can actually fail.
    const reg = registeredTemplates();
    expect(reg.has('totally-made-up-template')).toBe(false);
  });

  it('skills declaring projections use only recognised declaration keys', () => {
    // An unrecognised key is ignored at parse time, so a typo silently produces an
    // empty view. Keep the authored set aligned with the parser's.
    const parser = readFileSync(join(REPO, 'agent', 'src', 'render', 'skill_catalog.py'), 'utf8');
    const declMatch = /_DECL_KEYS = \{([^}]+)\}/s.exec(parser);
    expect(declMatch, '_DECL_KEYS not found in render_skill_catalog').toBeTruthy();
    const known = new Set(
      Array.from((declMatch![1]).matchAll(/"([a-z]+)"/g), (m) => m[1]),
    );
    const reserved = new Set(['name', 'handles', 'template', 'description', 'version']);
    for (const entry of readdirSync(SKILLS_DIR, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      for (const file of mappingFiles(entry.name)) {
        if (!existsSync(file)) continue;
        const md = readFileSync(file, 'utf8');
        if (!md.startsWith('---')) continue;
        for (const line of (md.split('---')[1] ?? '').trim().split('\n')) {
          const key = line.split(':')[0]?.trim().toLowerCase();
          if (!key || !/^[a-z]+$/.test(key)) continue;
          if (reserved.has(key)) continue;
          expect(known.has(key), `skill ${entry.name} (${file}) declares unknown key "${key}"`).toBe(true);
        }
      }
    }
  });
});
