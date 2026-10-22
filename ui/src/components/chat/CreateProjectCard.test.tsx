/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';

const m = vi.hoisted(() => ({ createProject: vi.fn(), requestNewChatInProject: vi.fn(), refresh: vi.fn() }));
vi.mock('../../projects/api', async (o) => ({ ...(await o<typeof import('../../projects/api')>()), createProject: m.createProject }));
vi.mock('../../projects/newChatInProject', () => ({ requestNewChatInProject: m.requestNewChatInProject }));
vi.mock('../../projects/useProjectsList', () => ({ refreshProjectsList: m.refresh }));

import { markdownComponents } from './markdownComponents';
import { keepAssetUrls } from './InlineAssetImage';
import { parseProjectLink } from './CreateProjectCard';

const md = 'Here you go:\n\n[Create project “project-index-test-v7”](project:new?name=project-index-test-v7&description=Meridian%20tests)';
const renderMd = (text: string) => render(
  <ReactMarkdown components={markdownComponents} urlTransform={(u) => keepAssetUrls(u, defaultUrlTransform)}>{text}</ReactMarkdown>);
const flush = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('a project the agent proposed', () => {
  it('reads the link', () => {
    expect(parseProjectLink('project:new?name=A%20B&indexing=0')).toEqual({ name: 'A B', indexing: false });
    expect(parseProjectLink('project:new?name=')).toBeNull();
  });

  it('is a card the user clicks: nothing is created until then, then it offers a chat in the new project', async () => {
    m.createProject.mockResolvedValue({ ok: true, value: { projectId: 'p7', name: 'project-index-test-v7', personal: false } });
    renderMd(md);
    expect(screen.getByTestId('create-project-card')).toHaveTextContent('project-index-test-v7');
    expect(screen.getByTestId('create-project-card')).toHaveTextContent('Meridian tests');
    expect(m.createProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('create-project-button'));
    await flush();
    expect(m.createProject).toHaveBeenCalledWith('project-index-test-v7', { description: 'Meridian tests', indexing: true });
    expect(m.refresh).toHaveBeenCalled();
    expect(screen.getByTestId('create-project-card')).toHaveTextContent('Project created');
    fireEvent.click(screen.getByTestId('create-project-chat'));
    expect(m.requestNewChatInProject).toHaveBeenCalledWith('p7', 'project-index-test-v7');
  });

  it('shows the server refusal (e.g. the name is taken) and can be tried again', async () => {
    m.createProject.mockResolvedValue({ ok: false, kind: 'error', status: 409, code: 'name_taken', message: 'A project with that name already exists' });
    renderMd(md);
    fireEvent.click(screen.getByTestId('create-project-button'));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('already exists');
    expect(screen.getByTestId('create-project-button')).not.toBeDisabled();
  });
});
