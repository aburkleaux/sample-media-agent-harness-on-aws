import { describe, expect, it } from 'vitest';
import { buildFolderTree, cleanFolder, flattenFolders, folderName, isWithin, parentFolder } from './folderPaths';

describe('folderPaths', () => {
  it('cleans a typed path the way the server normalizes it (segments trimmed, empty ones dropped)', () => {
    expect(cleanFolder(' Boards // Board 07 / ')).toBe('Boards/Board 07');
    expect(cleanFolder('  /  ')).toBe('');
    expect(folderName('Boards/Board 07')).toBe('Board 07');
    expect(parentFolder('Boards/Board 07')).toBe('Boards');
    expect(parentFolder('Boards')).toBe('');
    expect(isWithin('Footage/Day 1/Drone', 'Footage/Day 1')).toBe(true);
    expect(isWithin('Footage/Day 10', 'Footage/Day 1')).toBe(false); // a name prefix is not the folder
  });

  it('builds a gap-free tree with direct counts and subtree totals, numerically sorted', () => {
    const tree = buildFolderTree(
      [{ folder: 'Boards/Board 10', count: 2 }, { folder: 'Boards/Board 2', count: 1 }, { folder: 'Footage', count: 4 },
        { folder: 'Footage/Day 1/Drone', count: 3 }],
      ['Boards/Board 11', 'Empty'],
    );
    expect(tree.map((n) => n.path)).toEqual(['Boards', 'Empty', 'Footage']);
    const boards = tree[0];
    expect(boards.count).toBe(0);
    expect(boards.total).toBe(3);
    expect(boards.children.map((c) => c.name)).toEqual(['Board 2', 'Board 10', 'Board 11']);
    const footage = tree[2];
    expect(footage.count).toBe(4);
    expect(footage.total).toBe(7);
    expect(footage.children[0].path).toBe('Footage/Day 1'); // an implied ancestor
    expect(flattenFolders(tree)).toEqual(['Boards', 'Boards/Board 2', 'Boards/Board 10', 'Boards/Board 11', 'Empty',
      'Footage', 'Footage/Day 1', 'Footage/Day 1/Drone']);
  });
});
