// @vitest-environment jsdom
import '@testing-library/jest-dom';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { markdownComponents } from './markdownComponents';

function md(text: string) {
  return render(
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {text}
    </ReactMarkdown>,
  );
}

const REEL = 'https://dexamplecdn002.cloudfront.net/artifacts/s/output/olive_james_reel_crop.mp4';

describe('markdownComponents code-span media routing', () => {
  it('renders a backtick-wrapped video URL as an ArtifactLink player (not <code>)', () => {
    md(`Cropped reel:\n\`${REEL}\`\n`);
    // ArtifactLink renders a video as the chat's asset card, named after the file.
    expect(screen.getByTestId('chat-asset-card')).toHaveAttribute('data-kind', 'video');
    expect(screen.getByText('olive_james_reel_crop.mp4')).toBeInTheDocument();
    // It must NOT have been left as a literal code span showing the raw URL.
    expect(screen.queryByText(REEL)).not.toBeInTheDocument();
  });

  it('renders a backtick-wrapped image URL inline (ArtifactLink), not <code>', () => {
    const img = 'https://dexamplecdn002.cloudfront.net/artifacts/s/output/frame.png';
    const { container } = md(`\`${img}\``);
    // image kind: the chat's asset card; either way, not a bare code URL.
    expect(screen.queryByText(img)).not.toBeInTheDocument();
    expect(container.querySelector('code')).toBeNull();
  });

  it('leaves ordinary inline code untouched', () => {
    const { container } = md('Run `npm run build` first.');
    const code = container.querySelector('code');
    expect(code).not.toBeNull();
    expect(code).toHaveTextContent('npm run build');
  });

  it('leaves a non-media URL in backticks as code (no player)', () => {
    const api = 'https://example.com/api/status';
    const { container } = md(`\`${api}\``);
    expect(container.querySelector('code')).toHaveTextContent(api);
    expect(screen.queryByTestId('chat-asset-card')).not.toBeInTheDocument();
  });

  it('still routes a normal markdown video LINK through ArtifactLink', () => {
    md(`[reel](${REEL})`);
    expect(screen.getByTestId('chat-asset-card')).toHaveAttribute('data-kind', 'video');
  });
});
