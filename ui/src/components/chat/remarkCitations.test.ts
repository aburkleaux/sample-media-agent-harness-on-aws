// remarkCitations plugin unit tests (Component 5, media-harness-search-qa-rendering).
//
// The plugin is a pure mdast transformer: it splits `text` nodes that are NOT inside code,
// inline code, or a link into alternating plain-`text` and `citation` nodes, where each
// `citation` projects to a <span data-mh-citation="<key>"> and keeps the matched text as its
// child. These tests drive the transformer over hand-built trees — no React, no DOM.
//
// Covers Requirements 2.7, 2.10.
import { describe, it, expect } from 'vitest';
import { remarkCitations } from './remarkCitations';
import { citationKey } from './citations';

/** Minimal mdast node for the tests (the plugin's own node type is not exported). */
interface N {
  type: string;
  value?: string;
  url?: string;
  children?: N[];
  data?: { hName?: string; hProperties?: Record<string, string> };
}

function run(tree: N): N {
  remarkCitations()(tree as never);
  return tree;
}

/** A single paragraph wrapping the given children. */
function para(children: N[]): N {
  return { type: 'root', children: [{ type: 'paragraph', children }] };
}

// The en dash (U+2013) the formatter writes between timecodes.
const CITE = `[nba.mp4 @ 00:05\u201300:10]`;
const KEY = citationKey('nba.mp4', 5000, 10000);

describe('remarkCitations', () => {
  it('splits a citation out of a text node into a span-projecting citation node', () => {
    const tree = run(para([{ type: 'text', value: `see ${CITE} now` }]));
    const kids = tree.children![0].children!;
    expect(kids).toHaveLength(3);
    expect(kids[0]).toEqual({ type: 'text', value: 'see ' });
    expect(kids[2]).toEqual({ type: 'text', value: ' now' });

    const cite = kids[1];
    expect(cite.type).toBe('citation');
    expect(cite.data?.hName).toBe('span');
    expect(cite.data?.hProperties?.['data-mh-citation']).toBe(KEY);
    // The matched text is kept verbatim as the span's single child.
    expect(cite.children).toEqual([{ type: 'text', value: CITE }]);
  });

  it('splits multiple citations in one node and preserves the text between them', () => {
    const second = `[clip.mp4 @ 01:00\u201301:02]`;
    const tree = run(para([{ type: 'text', value: `${CITE} and ${second}` }]));
    const kids = tree.children![0].children!;
    // citation, ' and ', citation
    expect(kids.map((k) => k.type)).toEqual(['citation', 'text', 'citation']);
    expect(kids[1]).toEqual({ type: 'text', value: ' and ' });
    expect(kids[0].data?.hProperties?.['data-mh-citation']).toBe(KEY);
    expect(kids[2].data?.hProperties?.['data-mh-citation']).toBe(
      citationKey('clip.mp4', 60000, 62000),
    );
  });

  it('leaves a text node with no citation exactly as it was (same node identity)', () => {
    const text: N = { type: 'text', value: 'nothing to cite here' };
    const tree = run(para([text]));
    const kids = tree.children![0].children!;
    expect(kids).toHaveLength(1);
    expect(kids[0]).toBe(text);
  });

  it('does not descend into a link node, so citation text in a label stays plain', () => {
    const link: N = { type: 'link', url: 'https://x', children: [{ type: 'text', value: CITE }] };
    const tree = run(para([link]));
    const kids = tree.children![0].children!;
    expect(kids).toHaveLength(1);
    expect(kids[0]).toBe(link);
    expect(link.children).toEqual([{ type: 'text', value: CITE }]);
    expect(JSON.stringify(tree)).not.toContain('data-mh-citation');
  });

  it('leaves inline code and fenced code untouched', () => {
    const inline: N = { type: 'inlineCode', value: CITE };
    const code: N = { type: 'code', value: CITE };
    const tree = run(para([inline, code]));
    const kids = tree.children![0].children!;
    expect(kids).toEqual([inline, code]);
    expect(JSON.stringify(tree)).not.toContain('data-mh-citation');
  });

  it('produces the key the span override will look up (and nothing else classifies here)', () => {
    const tree = run(para([{ type: 'text', value: CITE }]));
    const cite = tree.children![0].children![0];
    // The plugin only emits the citationKey; whether it matches a record is CitationRef's job.
    expect(cite.data?.hProperties).toEqual({ 'data-mh-citation': KEY });
  });
});
