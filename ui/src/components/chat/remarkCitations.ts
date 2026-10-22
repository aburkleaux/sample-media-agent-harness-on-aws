// remarkCitations — remark (mdast) plugin for the chat surface
// (Component 5, media-harness-search-qa-rendering).
//
// Visits every `text` node that is NOT inside an `inlineCode`, `code`, `link`, or
// `linkReference` node, runs parseCitations (ui/src/components/chat/citations.ts) on the
// node's value, and splits each match out into a `citation` node whose rehype projection is
// a <span data-mh-citation="<key>">…</span> (via data.hName / data.hProperties), keeping the
// matched text verbatim as the span's child text node. The non-citation runs around each
// match stay plain `text` nodes.
//
// The tree walk is hand-written on purpose: no new dependency (unist-util-visit) is added.
// The 8.5 `span` override in markdownComponents.tsx reads `data-mh-citation` and looks the
// key up in the CitationBlock; this plugin only produces the key, it does not classify.
//
// Implements Requirements 2.7, 2.10.

import { parseCitations, keyOfCitation } from './citations';

// Minimal structural mdast node shape. The index signature keeps unrelated node fields
// (position, depth, url, …) intact as the tree is walked in place, and makes the transformer
// assignable to unified's loose Node-based plugin contract without an mdast type dependency.
interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: {
    hName?: string;
    hProperties?: Record<string, string>;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

// Node types whose text content must never become a citation: fenced/indented code blocks,
// inline code, and link / link-reference label text. The walk does not descend into these.
const SKIP_TYPES = new Set(['inlineCode', 'code', 'link', 'linkReference']);

/**
 * Split one `text` node's value into alternating plain-`text` and `citation` nodes. Returns
 * the single original node unchanged when it carries no citation, so untouched text keeps its
 * identity (and any other fields) exactly.
 */
function splitTextNode(node: MdNode): MdNode[] {
  const value = node.value ?? '';
  const citations = parseCitations(value);
  if (citations.length === 0) {
    return [node];
  }
  const out: MdNode[] = [];
  let cursor = 0;
  for (const citation of citations) {
    const [start, end] = citation.span;
    if (start > cursor) {
      out.push({ type: 'text', value: value.slice(cursor, start) });
    }
    const key = keyOfCitation(citation);
    out.push({
      type: 'citation',
      data: {
        hName: 'span',
        hProperties: { 'data-mh-citation': key },
      },
      children: [{ type: 'text', value: citation.text }],
    });
    cursor = end;
  }
  if (cursor < value.length) {
    out.push({ type: 'text', value: value.slice(cursor) });
  }
  return out;
}

/**
 * Rebuild `node.children` in place: split `text` children into citation runs, leave SKIP_TYPES
 * children untouched (and undescended), and recurse into every other container.
 */
function walk(node: MdNode): void {
  const children = node.children;
  if (!children || children.length === 0) {
    return;
  }
  const next: MdNode[] = [];
  for (const child of children) {
    if (child.type === 'text') {
      next.push(...splitTextNode(child));
    } else if (SKIP_TYPES.has(child.type)) {
      next.push(child);
    } else {
      walk(child);
      next.push(child);
    }
  }
  node.children = next;
}

/**
 * Remark plugin. Use after remarkGfm: `remarkPlugins={[remarkGfm, remarkCitations]}`. Mutates
 * the mdast tree in place and returns nothing, matching unified's transformer contract.
 */
export function remarkCitations(): (tree: MdNode) => void {
  return (tree: MdNode): void => {
    walk(tree);
  };
}

export default remarkCitations;
