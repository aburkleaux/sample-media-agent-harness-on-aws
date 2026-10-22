/**
 * @vitest-environment jsdom
 *
 * Folder input: the dropped-entry walker (nested folders, `readEntries` batching, hidden and system files skipped),
 * the `webkitdirectory` picker filter, `folderOf`, and the client copy of the server's folder rules.
 */
import { describe, expect, it } from 'vitest';
import {
  filesFromDirectoryInput,
  folderOf,
  isHiddenOrSystem,
  readDroppedFiles,
  validateFolder,
  walkEntries,
  type FsEntry,
} from './folders';

function fileEntry(name: string): FsEntry & { file(cb: (f: File) => void): void } {
  return { isFile: true, isDirectory: false, name, file: (cb) => cb(new File(['x'], name)) };
}

/** A directory whose reader answers `batchSize` entries per `readEntries` call, then an empty batch. */
function dirEntry(name: string, children: FsEntry[], batchSize = 2) {
  const calls = { readEntries: 0 };
  const entry = {
    isFile: false,
    isDirectory: true,
    name,
    calls,
    createReader() {
      let at = 0;
      return {
        readEntries(cb: (batch: FsEntry[]) => void) {
          calls.readEntries++;
          const batch = children.slice(at, at + batchSize);
          at += batch.length;
          setTimeout(() => cb(batch), 0); // async, like the browser
        },
      };
    },
  };
  return entry;
}

const paths = (files: File[]) => files.map((f) => [folderOf(f) ?? '', f.name]);

describe('walkEntries', () => {
  it('walks nested folders, keeping each file’s relative directory; a loose file has none', async () => {
    const day1 = dirEntry('Day 1', [fileEntry('a.mp4'), fileEntry('b.mp4')]);
    const footage = dirEntry('Footage', [fileEntry('root.mp4'), day1, dirEntry('Day 2', [dirEntry('Cam A', [fileEntry('c.mp4')])])]);
    const files = await walkEntries([footage, fileEntry('loose.png')]);
    expect(paths(files)).toEqual([
      ['Footage', 'root.mp4'],
      ['Footage/Day 1', 'a.mp4'],
      ['Footage/Day 1', 'b.mp4'],
      ['Footage/Day 2/Cam A', 'c.mp4'],
      ['', 'loose.png'],
    ]);
  });

  it('calls readEntries until it returns an empty batch (more entries than one batch)', async () => {
    const children = Array.from({ length: 7 }, (_, i) => fileEntry(`f${i}.jpg`));
    const dir = dirEntry('Big', children, 3);
    const files = await walkEntries([dir]);
    expect(files).toHaveLength(7);
    expect(dir.calls.readEntries).toBe(4); // 3 + 3 + 1 + the empty batch
  });

  it('skips hidden and system files, and hidden folders', async () => {
    const dir = dirEntry('Shoot', [
      fileEntry('.DS_Store'),
      fileEntry('Thumbs.db'),
      fileEntry('desktop.ini'),
      fileEntry('keep.mov'),
      dirEntry('.git', [fileEntry('HEAD')]),
    ]);
    const files = await walkEntries([dir, fileEntry('.hidden')]);
    expect(paths(files)).toEqual([['Shoot', 'keep.mov']]);
    expect(isHiddenOrSystem('THUMBS.DB')).toBe(true);
    expect(isHiddenOrSystem('notes.txt')).toBe(false);
  });

  it('skips an entry that cannot be read instead of failing the drop', async () => {
    const bad = { isFile: true, isDirectory: false, name: 'gone.mp4', file: (_ok: unknown, err: (e: unknown) => void) => err(new Error('x')) };
    const files = await walkEntries([bad as FsEntry, fileEntry('ok.mp4')]);
    expect(files.map((f) => f.name)).toEqual(['ok.mp4']);
  });
});

describe('readDroppedFiles', () => {
  it('reads the items’ entries (folders walked) and falls back to getAsFile without an entry', async () => {
    const folder = dirEntry('Footage', [fileEntry('a.mp4')]);
    const plain = new File(['y'], 'plain.png');
    const dt = {
      items: [
        { kind: 'file', webkitGetAsEntry: () => folder, getAsFile: () => null },
        { kind: 'string', webkitGetAsEntry: () => null, getAsFile: () => null },
        { kind: 'file', webkitGetAsEntry: () => null, getAsFile: () => plain },
      ],
      files: [],
      types: ['Files'],
    } as unknown as DataTransfer;
    const files = await readDroppedFiles(dt);
    expect(paths(files)).toEqual([['Footage', 'a.mp4'], ['', 'plain.png']]);
  });

  it('uses dataTransfer.files when there are no items', async () => {
    const f = new File(['z'], 'z.png');
    expect(await readDroppedFiles({ files: [f] } as unknown as DataTransfer)).toEqual([f]);
  });
});

describe('the directory picker', () => {
  it('reads the folder from webkitRelativePath and leaves hidden files out', () => {
    const mk = (rel: string) => {
      const f = new File(['x'], rel.split('/').pop() as string);
      Object.defineProperty(f, 'webkitRelativePath', { value: rel });
      return f;
    };
    const files = filesFromDirectoryInput([
      mk('Footage/Day 1/a.mp4'), mk('Footage/.DS_Store'), mk('Footage/.cache/x.bin'), mk('Footage/b.mp4'),
    ]);
    expect(paths(files)).toEqual([['Footage/Day 1', 'a.mp4'], ['Footage', 'b.mp4']]);
  });
});

describe('validateFolder (the server’s rules)', () => {
  it('accepts and normalizes a good path', () => {
    expect(validateFolder(' Footage / Day 1 ')).toEqual({ ok: true, folder: 'Footage/Day 1' });
    expect(validateFolder('Generated/Trailer · 2026-10-07')).toEqual({ ok: true, folder: 'Generated/Trailer · 2026-10-07' });
  });

  it('rejects .., ., empty segments, forbidden and control characters', () => {
    expect(validateFolder('../x')).toMatchObject({ ok: false, reason: expect.stringContaining('".."') });
    expect(validateFolder('a/./b').ok).toBe(false);
    expect(validateFolder('a//b').ok).toBe(false);
    expect(validateFolder('a/[draft]').ok).toBe(false);
    expect(validateFolder('a/b@c').ok).toBe(false);
    expect(validateFolder('a/b#1').ok).toBe(false);
    expect(validateFolder('a/b\u0007').ok).toBe(false);
  });

  it('rejects more than 8 levels, a segment over 64 characters, and a path over 255', () => {
    expect(validateFolder('1/2/3/4/5/6/7/8').ok).toBe(true);
    expect(validateFolder('1/2/3/4/5/6/7/8/9')).toMatchObject({ ok: false, reason: expect.stringContaining('8 levels') });
    expect(validateFolder('x'.repeat(64)).ok).toBe(true);
    expect(validateFolder('x'.repeat(65)).ok).toBe(false);
    const long = Array.from({ length: 5 }, () => 'y'.repeat(60)).join('/'); // 304 characters
    expect(validateFolder(long)).toMatchObject({ ok: false, reason: expect.stringContaining('255') });
  });
});
