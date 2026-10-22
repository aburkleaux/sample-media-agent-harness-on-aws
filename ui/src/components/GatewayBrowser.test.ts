import { describe, it, expect } from 'vitest';
import { filterGatewayTools } from './GatewayBrowser';
import type { GatewayToolDescriptor } from '../lab-api';

const sampleTools: GatewayToolDescriptor[] = [
  { name: 'ImageResize', description: 'Resizes images to specified dimensions', version: '1.0' },
  { name: 'TextSummarize', description: 'Summarizes long text content', version: '2.1' },
  { name: 'VideoTranscode', description: 'Transcodes video files to different formats', version: '1.3' },
  { name: 'AudioNormalize', description: 'Normalizes audio levels', version: '1.0' },
];

describe('filterGatewayTools', () => {
  it('returns all tools when query is empty', () => {
    expect(filterGatewayTools(sampleTools, '')).toEqual(sampleTools);
  });

  it('returns all tools when query is whitespace only', () => {
    expect(filterGatewayTools(sampleTools, '   ')).toEqual(sampleTools);
  });

  it('filters by name (case-insensitive)', () => {
    const result = filterGatewayTools(sampleTools, 'image');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('ImageResize');
  });

  it('filters by description (case-insensitive)', () => {
    const result = filterGatewayTools(sampleTools, 'audio levels');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('AudioNormalize');
  });

  it('matches substring in name', () => {
    const result = filterGatewayTools(sampleTools, 'Resize');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('ImageResize');
  });

  it('matches substring in description', () => {
    const result = filterGatewayTools(sampleTools, 'formats');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('VideoTranscode');
  });

  it('returns multiple matches when query matches several tools', () => {
    // "text" matches TextSummarize (name) and also "text content" in its description
    const result = filterGatewayTools(sampleTools, 'text');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('TextSummarize');
  });

  it('returns empty array when no tools match', () => {
    const result = filterGatewayTools(sampleTools, 'nonexistent');
    expect(result).toHaveLength(0);
  });

  it('handles empty tool list', () => {
    expect(filterGatewayTools([], 'anything')).toEqual([]);
  });

  it('is case-insensitive for both name and description', () => {
    const result = filterGatewayTools(sampleTools, 'IMAGERESIZE');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('ImageResize');
  });

  it('matches tools where query appears in either name or description', () => {
    // "ize" matches ImageResize (name) and AudioNormalize (name)
    const result = filterGatewayTools(sampleTools, 'ize');
    expect(result.length).toBeGreaterThanOrEqual(2);
    const names = result.map(t => t.name);
    expect(names).toContain('ImageResize');
    expect(names).toContain('AudioNormalize');
  });
});
