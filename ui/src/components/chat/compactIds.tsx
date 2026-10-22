import { Fragment, useState } from 'react';

/**
 * compactIds — fold internal identifiers (SFN/ARNs, s3:// URIs, bare job-ids) that leak into the
 * agent's PROSE into compact, copy-able chips, so the end user sees "⧉ job" not
 * `arn:aws:states:us-west-2:100000000002:execution:m2cv6-…:41059e38-…`. The full value stays one
 * click away for admins/IT/testing (copy to clipboard + title tooltip); the account id, region and
 * path are never shown inline. Media URLs are handled by ArtifactLink; this covers the non-URL
 * identifiers that markdown links can't catch.
 *
 * Pure `splitOnIds` is exported for tests; `renderWithIdChips` wraps markdown children.
 */

// ARNs (incl. SFN execution ARNs) and s3:// URIs. Bounded to identifier chars so we don't swallow
// trailing prose/punctuation.
const ID_RE = /(arn:aws:[A-Za-z0-9:_\-/.]+|s3:\/\/[A-Za-z0-9._\-/]+)/g;

export interface IdSegment { kind: 'text' | 'id'; value: string; label: string; }

/** Friendly short label for an identifier — no account id / region / path shown. */
export function labelForId(id: string): string {
  if (id.startsWith('s3://')) return 's3';
  if (id.includes(':execution:')) {
    const tail = id.split(':').pop() || id;
    const short = tail.length > 10 ? `…${tail.slice(-8)}` : tail;
    return `job ${short}`;
  }
  if (id.startsWith('arn:aws:')) {
    const svc = id.split(':')[2] || 'arn'; // arn:aws:<service>:...
    return svc;
  }
  return 'id';
}

/** Split a prose string into text/id segments. Pure — exported for tests. */
export function splitOnIds(text: string): IdSegment[] {
  const out: IdSegment[] = [];
  let last = 0;
  for (const m of text.matchAll(ID_RE)) {
    const start = m.index ?? 0;
    if (start > last) out.push({ kind: 'text', value: text.slice(last, start), label: '' });
    out.push({ kind: 'id', value: m[0], label: labelForId(m[0]) });
    last = start + m[0].length;
  }
  if (last < text.length) out.push({ kind: 'text', value: text.slice(last), label: '' });
  return out;
}

/** A compact, copy-to-clipboard chip for one internal identifier. */
export function IdChip({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    }).catch(() => { /* clipboard blocked — title still shows the value */ });
  };
  return (
    <button type="button" className="id-chip" data-testid="id-chip" title={value} onClick={copy}>
      ⧉ {copied ? 'copied' : label}
    </button>
  );
}

/**
 * Walk react-markdown children and replace bare identifiers inside string nodes with <IdChip>.
 * Non-string children (already-rendered elements, e.g. ArtifactLink) pass through untouched.
 */
export function renderWithIdChips(children: React.ReactNode): React.ReactNode {
  if (typeof children === 'string') {
    const segs = splitOnIds(children);
    if (segs.length === 1 && segs[0].kind === 'text') return children;
    return segs.map((s, i) => (s.kind === 'text' ? s.value : <IdChip key={i} value={s.value} label={s.label} />));
  }
  if (Array.isArray(children)) return children.map((c, i) => <Fragment key={i}>{renderWithIdChips(c)}</Fragment>);
  return children;
}
