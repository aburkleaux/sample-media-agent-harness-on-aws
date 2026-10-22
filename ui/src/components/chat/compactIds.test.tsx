/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { splitOnIds, labelForId, renderWithIdChips } from './compactIds';

describe('splitOnIds / labelForId', () => {
  it('extracts an SFN execution ARN and labels it "job <tail>" (no account id / region shown)', () => {
    const arn = 'arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-media-processing:41059e38-51c3-52e9-b9b6-5706ffbf38a5';
    const segs = splitOnIds(`Fetched results for job ${arn} — done.`);
    expect(segs.map(s => s.kind)).toEqual(['text', 'id', 'text']);
    expect(segs[1].value).toBe(arn);
    expect(segs[1].label).toMatch(/^job …[0-9a-f]{8}$/);
    // the label never contains the account id or region
    expect(segs[1].label).not.toContain('100000000002');
    expect(segs[1].label).not.toContain('us-west-2');
  });

  it('labels a generic ARN by service and an s3 uri as "s3"', () => {
    expect(labelForId('arn:aws:s3:::my-bucket/key')).toBe('s3');
    expect(labelForId('s3://bucket/path/to/obj.json')).toBe('s3');
    expect(labelForId('arn:aws:lambda:us-west-2:123456789012:function:foo')).toBe('lambda');
  });

  it('returns the text unchanged when there is no identifier', () => {
    expect(splitOnIds('just a normal sentence')).toEqual([{ kind: 'text', value: 'just a normal sentence', label: '' }]);
    expect(renderWithIdChips('plain text')).toBe('plain text');
  });
});

describe('renderWithIdChips', () => {
  it('renders a copy-chip (not the raw ARN) inside prose', () => {
    const node = renderWithIdChips('see arn:aws:states:us-west-2:100000000002:execution:x:y for details');
    render(<p>{node}</p>);
    const chip = screen.getByTestId('id-chip');
    expect(chip).toHaveAttribute('title', 'arn:aws:states:us-west-2:100000000002:execution:x:y');
    expect(chip.textContent).toContain('job');
    // the raw account id is not shown as visible chip text
    expect(chip.textContent).not.toContain('100000000002');
  });
});
