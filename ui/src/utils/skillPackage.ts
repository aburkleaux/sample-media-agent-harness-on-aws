import { parse } from 'yaml';
import type { SkillPackageFileUpload } from '../lab-api';

export const ALLOWED_FRONTMATTER_KEYS = new Set([
  'name',
  'description',
  'license',
  'allowed-tools',
  'metadata',
]);
export const MAX_SKILL_PACKAGE_FILES = 100;
export const MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024;
export const MAX_SKILL_PACKAGE_BYTES = 5 * 1024 * 1024;

const SKILL_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/;
const PACKAGE_DIRECTORIES = new Set(['references', 'scripts', 'assets']);

export interface ParsedSkillDocument {
  name: string;
  description: string;
  license?: string;
  allowedTools: string[];
  metadata: Record<string, string>;
  body: string;
  frontmatter: Record<string, unknown>;
  errors: string[];
}

export interface SkillPackageCandidate {
  folderName: string;
  suggestedName: string;
  declaredName: string;
  skillMd: string;
  files: SkillPackageFileUpload[];
  filePaths: string[];
  totalBytes: number;
  errors: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

export function validateSkillName(name: string): string | null {
  const value = name.trim();
  if (!value) return 'Skill name is required';
  if (value.length > 64) return 'Skill name must be 64 characters or fewer';
  if (!SKILL_NAME_RE.test(value)) return 'Use lowercase letters, numbers, and single hyphens only';
  return null;
}

export function slugifySkillName(value: string): string {
  return value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-')
    .slice(0, 64)
    .replace(/-+$/g, '');
}

export function parseSkillDocument(content: string, expectedName?: string): ParsedSkillDocument {
  const errors: string[] = [];
  const match = FRONTMATTER_RE.exec(content);
  if (!match) {
    return {
      name: '', description: '', allowedTools: [], metadata: {}, body: content,
      frontmatter: {},
      errors: ['SKILL.md must begin with YAML frontmatter enclosed by --- delimiters'],
    };
  }

  let parsed: unknown;
  try {
    parsed = parse(match[1]);
  } catch (error) {
    return {
      name: '', description: '', allowedTools: [], metadata: {},
      body: content.slice(match[0].length), frontmatter: {},
      errors: [`SKILL.md frontmatter is invalid YAML: ${error instanceof Error ? error.message : 'parse failed'}`],
    };
  }

  if (!isRecord(parsed)) {
    return {
      name: '', description: '', allowedTools: [], metadata: {},
      body: content.slice(match[0].length), frontmatter: {},
      errors: ['SKILL.md frontmatter must be a YAML mapping'],
    };
  }

  const unexpected = Object.keys(parsed).filter(key => !ALLOWED_FRONTMATTER_KEYS.has(key)).sort();
  if (unexpected.length) errors.push(`Unsupported frontmatter field(s): ${unexpected.join(', ')}`);

  const name = typeof parsed.name === 'string' ? parsed.name.trim() : '';
  if (!name) errors.push('SKILL.md frontmatter requires a string name');
  else {
    const nameError = validateSkillName(name);
    if (nameError) errors.push(`SKILL.md name: ${nameError}`);
    if (expectedName && name !== expectedName) errors.push(`SKILL.md name '${name}' must match '${expectedName}'`);
  }

  const description = typeof parsed.description === 'string' ? parsed.description.trim() : '';
  if (!description) errors.push('SKILL.md frontmatter requires a string description');
  else {
    if (description.length > 1024) errors.push('SKILL.md description must be 1024 characters or fewer');
    if (/[<>]/.test(description)) errors.push('SKILL.md description cannot contain angle brackets');
  }

  const license = parsed.license;
  if (license !== undefined && typeof license !== 'string') errors.push('SKILL.md license must be a string');

  let allowedTools: string[] = [];
  const allowed = parsed['allowed-tools'];
  if (typeof allowed === 'string') allowedTools = [...new Set(allowed.split(/\s+/).filter(Boolean))];
  else if (Array.isArray(allowed) && allowed.every(item => typeof item === 'string')) allowedTools = [...new Set(allowed)];
  else if (allowed !== undefined) errors.push('SKILL.md allowed-tools must be a string or list of strings');

  const metadata: Record<string, string> = {};
  if (parsed.metadata !== undefined) {
    if (!isRecord(parsed.metadata)) errors.push('SKILL.md metadata must be a mapping');
    else {
      for (const [key, value] of Object.entries(parsed.metadata)) {
        metadata[key] = typeof value === 'string' ? value : JSON.stringify(value);
      }
    }
  }

  return {
    name,
    description,
    license: typeof license === 'string' ? license : undefined,
    allowedTools,
    metadata,
    body: content.slice(match[0].length).replace(/^\r?\n/, ''),
    frontmatter: parsed,
    errors,
  };
}

export function renameSkillDocument(content: string, name: string): string {
  const match = FRONTMATTER_RE.exec(content);
  if (!match) return content;
  const updatedYaml = match[1].replace(/^name\s*:.*$/m, `name: ${name}`);
  if (updatedYaml === match[1]) return content;
  return `---\n${updatedYaml}\n---\n\n${content.slice(match[0].length).replace(/^\r?\n/, '')}`;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 8192;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

function packagePathError(path: string): string | null {
  if (!path || path.startsWith('/') || path.includes('\\')) return `Unsafe package path: ${path || '(empty)'}`;
  const parts = path.split('/');
  if (parts.some(part => !part || part === '.' || part === '..')) return `Unsafe package path: ${path}`;
  if (parts.some(part => part.startsWith('.'))) return `Hidden package path is not allowed: ${path}`;
  if (!PACKAGE_DIRECTORIES.has(parts[0]) || parts.length < 2) {
    return `Supporting file '${path}' must be under references/, scripts/, or assets/`;
  }
  if (parts[0] === 'references' && parts.length !== 2) {
    return `Reference '${path}' must be directly under references/`;
  }
  return null;
}

export async function readSkillPackageFolder(input: FileList | File[]): Promise<SkillPackageCandidate> {
  const selected = Array.from(input);
  const errors: string[] = [];
  if (!selected.length) {
    return { folderName: '', suggestedName: '', declaredName: '', skillMd: '', files: [], filePaths: [], totalBytes: 0, errors: ['Choose a folder to import'] };
  }

  const rawPaths = selected.map(file => (file.webkitRelativePath || file.name).replace(/\\/g, '/'));
  const roots = new Set(rawPaths.map(path => path.includes('/') ? path.split('/')[0] : ''));
  if (roots.size !== 1 || roots.has('')) errors.push('Select one folder containing a root-level SKILL.md');
  const folderName = roots.size === 1 ? [...roots][0] : '';
  const relativePaths = rawPaths.map(path => folderName && path.startsWith(`${folderName}/`) ? path.slice(folderName.length + 1) : path);

  const skillIndexes = relativePaths
    .map((path, index) => ({ path, index }))
    .filter(item => item.path === 'SKILL.md');
  if (skillIndexes.length !== 1) errors.push('The folder must contain exactly one root-level SKILL.md (case-sensitive)');

  if (selected.length > MAX_SKILL_PACKAGE_FILES) errors.push(`Skill package may contain at most ${MAX_SKILL_PACKAGE_FILES} files`);
  const totalBytes = selected.reduce((total, file) => total + file.size, 0);
  if (totalBytes > MAX_SKILL_PACKAGE_BYTES) errors.push(`Skill package must be ${MAX_SKILL_PACKAGE_BYTES / (1024 * 1024)} MB or smaller`);

  const seen = new Set<string>();
  for (const [index, path] of relativePaths.entries()) {
    const key = path.toLowerCase();
    if (seen.has(key)) errors.push(`Duplicate package path: ${path}`);
    seen.add(key);
    if (selected[index].size > MAX_SKILL_FILE_BYTES) errors.push(`File '${path}' exceeds the ${MAX_SKILL_FILE_BYTES / (1024 * 1024)} MB limit`);
    if (path !== 'SKILL.md') {
      const pathError = packagePathError(path);
      if (pathError) errors.push(pathError);
    }
  }

  let skillMd = '';
  if (skillIndexes.length === 1) {
    try {
      const bytes = new Uint8Array(await selected[skillIndexes[0].index].arrayBuffer());
      skillMd = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch {
      errors.push('SKILL.md must be valid UTF-8 text');
    }
  }

  const parsed = skillMd ? parseSkillDocument(skillMd) : null;
  if (parsed) errors.push(...parsed.errors);

  const files: SkillPackageFileUpload[] = [];
  for (const [index, file] of selected.entries()) {
    const path = relativePaths[index];
    if (path === 'SKILL.md') continue;
    const bytes = new Uint8Array(await file.arrayBuffer());
    files.push({ path, contentBase64: bytesToBase64(bytes) });
  }

  return {
    folderName,
    suggestedName: slugifySkillName(folderName),
    declaredName: parsed?.name ?? '',
    skillMd,
    files,
    filePaths: ['SKILL.md', ...files.map(file => file.path)].sort((a, b) => a.localeCompare(b)),
    totalBytes,
    errors: [...new Set(errors)],
  };
}

export function validateImportCandidate(
  candidate: SkillPackageCandidate,
  finalName: string,
  existingNames: string[],
): { errors: string[]; skillMd: string } {
  const errors = [...candidate.errors];
  const nameError = validateSkillName(finalName);
  if (nameError) errors.push(nameError);
  if (existingNames.some(name => name.toLowerCase() === finalName.trim().toLowerCase())) {
    errors.push(`A Skill named '${finalName.trim()}' already exists`);
  }
  const skillMd = renameSkillDocument(candidate.skillMd, finalName.trim());
  if (!nameError && skillMd) errors.push(...parseSkillDocument(skillMd, finalName.trim()).errors);
  return { errors: [...new Set(errors)], skillMd };
}
