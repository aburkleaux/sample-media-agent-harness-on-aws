import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { RendererProps } from '../types';

interface MarkdownRichData { content: string; }

export default function MarkdownRich({ data, theme: t }: RendererProps<MarkdownRichData>) {
  return (
    <div style={{ padding: 4, fontSize: t.typography.sizes.body, lineHeight: t.typography.lineHeight, color: t.colors.textPrimary }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.content}</ReactMarkdown>
    </div>
  );
}
