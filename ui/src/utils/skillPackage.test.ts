import { describe, expect, it } from 'vitest';
import {
  parseSkillDocument,
  readSkillPackageFolder,
  renameSkillDocument,
  slugifySkillName,
  validateImportCandidate,
} from './skillPackage';

function packageFile(path: string, content: string): File {
  const parts = path.split('/');
  const file = new File([content], parts[parts.length - 1] || path, { type: 'text/plain' });
  Object.defineProperty(file, 'webkitRelativePath', { value: path });
  return file;
}

const SKILL_MD = `---
name: declared-skill
description: Use when a folder Skill is imported.
allowed-tools:
  - read_file
metadata:
  owner: media-team
---

# Imported workflow

Read [the guide](references/guide.md).
`;

describe('Skill package schema', () => {
  it('parses YAML frontmatter without losing lists or metadata', () => {
    const parsed = parseSkillDocument(SKILL_MD);
    expect(parsed.errors).toEqual([]);
    expect(parsed.name).toBe('declared-skill');
    expect(parsed.allowedTools).toEqual(['read_file']);
    expect(parsed.metadata).toEqual({ owner: 'media-team' });
    expect(parsed.body).toContain('# Imported workflow');
  });

  it('renames only the frontmatter name', () => {
    const renamed = renameSkillDocument(SKILL_MD, 'folder-skill');
    expect(parseSkillDocument(renamed).name).toBe('folder-skill');
    expect(renamed).toContain('# Imported workflow');
  });

  it('normalizes folder names into editable Skill names', () => {
    expect(slugifySkillName('My Video Tools')).toBe('my-video-tools');
  });
});

describe('Skill folder preflight', () => {
  it('reads one complete package and rewrites the declared name to the chosen folder name', async () => {
    const candidate = await readSkillPackageFolder([
      packageFile('My Video Tools/SKILL.md', SKILL_MD),
      packageFile('My Video Tools/references/guide.md', '# Guide'),
      packageFile('My Video Tools/assets/schema.json', '{}'),
    ]);

    expect(candidate.folderName).toBe('My Video Tools');
    expect(candidate.suggestedName).toBe('my-video-tools');
    expect(candidate.declaredName).toBe('declared-skill');
    expect(candidate.errors).toEqual([]);
    expect(candidate.filePaths).toEqual([
      'assets/schema.json',
      'references/guide.md',
      'SKILL.md',
    ]);

    const validation = validateImportCandidate(candidate, 'my-video-tools', []);
    expect(validation.errors).toEqual([]);
    expect(parseSkillDocument(validation.skillMd).name).toBe('my-video-tools');
  });

  it('rejects folders without one exact root-level SKILL.md', async () => {
    const candidate = await readSkillPackageFolder([
      packageFile('invalid/skill.md', SKILL_MD),
    ]);
    expect(candidate.errors.join(' ')).toContain('exactly one root-level SKILL.md');
  });

  it('rejects nested references and existing names', async () => {
    const candidate = await readSkillPackageFolder([
      packageFile('folder-skill/SKILL.md', SKILL_MD),
      packageFile('folder-skill/references/nested/guide.md', '# Guide'),
    ]);
    expect(candidate.errors.join(' ')).toContain('directly under references');

    const validation = validateImportCandidate(candidate, 'declared-skill', ['declared-skill']);
    expect(validation.errors.join(' ')).toContain("already exists");
  });
});
