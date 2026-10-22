/**
 * @vitest-environment jsdom
 *
 * The "already-handled notifications keep alerting on every login" fix.
 *
 * Root cause (observed on staging: 20 items, 19 `seen:false`, all `bannered:true`, up to 39h
 * old): `seen` was only ever set by clicking an individual item or "Mark all read". An
 * auto-expired banner set just `bannered`, and opening the bell marked nothing — so the unread
 * badge persisted forever and every fresh login re-presented old work as new alerts. Three
 * guards now hold the line:
 *   1. opening the bell marks the listed items seen (badge clears for good),
 *   2. history older than MAX_ITEM_AGE_MS leaves the bell entirely, and
 *   3. a terminal job older than MAX_NOTIFY_AGE_MS never notifies, on ANY path — the SSE
 *      handler previously had no age check at all.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import '@testing-library/jest-dom';
import { render, screen, fireEvent } from '@testing-library/react';
import { notificationsStore, __resetNotificationsForTests } from '../services/notifications';
import type { Job } from '../types/jobs';
import CompletionNotifications from './CompletionNotifications';

function job(id: string, agoMs: number, extra: Partial<Job> = {}): Job {
  return {
    jobId: id, actorId: 'a', sessionId: 's',
    trioId: 'face-recognition|recognize_faces', gatewayId: '__aws_sfn__',
    status: 'SUCCEEDED',
    // A SUCCEEDED standalone job only notifies when it has renderable artifacts (or is a plan
    // parent) — see pushFromTerminal — so the fixture must set it or every push is skipped for
    // a reason unrelated to what these tests pin.
    hasRenderableArtifacts: true, lastError: null, runId: null, nodeId: null, dependsOn: [],
    startAt: new Date(Date.now() - agoMs).toISOString(),
    terminalAt: new Date(Date.now() - agoMs).toISOString(),
    updatedAt: new Date(Date.now() - agoMs).toISOString(),
    ...extra,
  } as Job;
}

beforeEach(() => {
  localStorage.clear();
  __resetNotificationsForTests();
});

describe('notification staleness + read-state', () => {
  it('does NOT notify for a terminal job that finished long ago (covers the SSE path)', () => {
    // pushFromTerminal is the single choke point both the REST refresh and the SSE
    // `job.terminal` handler call; the latter had no recency guard.
    expect(notificationsStore.pushFromTerminal(job('old', 6 * 60 * 60 * 1000))).toBeNull();
    expect(notificationsStore.getSnapshot().items).toHaveLength(0);
  });

  it('still notifies for a job that just finished', () => {
    expect(notificationsStore.pushFromTerminal(job('fresh', 5_000))).not.toBeNull();
    expect(notificationsStore.getSnapshot().unseen).toBe(1);
  });

  it('still notifies when updatedAt is missing/unparseable (never swallow a real completion)', () => {
    expect(notificationsStore.pushFromTerminal(job('nots', 0, { updatedAt: '' }))).not.toBeNull();
  });

  it('drops aged history from the bell on load so old items cannot accumulate', async () => {
    const base = {
      sessionId: 's', kind: 'success', label: 'Old', message: 'Old finished',
      renderable: false, error: null, seen: false, bannered: true,
    };
    const stale = { ...base, jobId: 'stale', ts: Date.now() - 72 * 60 * 60 * 1000 };
    const recent = { ...base, jobId: 'recent', label: 'New', ts: Date.now() - 60_000 };
    localStorage.setItem('lab-notifications', JSON.stringify([stale, recent]));
    // The store reads persisted items at MODULE LOAD, and __resetNotificationsForTests wipes
    // storage rather than re-reading it — so exercise the load path with a fresh registry.
    vi.resetModules();
    const fresh = await import('../services/notifications');
    const ids = fresh.notificationsStore.getSnapshot().items.map((i) => i.jobId);
    expect(ids).toContain('recent');
    expect(ids).not.toContain('stale');
  });

  it('keeps items unread (highlighted) while the panel is OPEN, and clears on close', () => {
    notificationsStore.pushFromTerminal(job('j1', 1_000));
    notificationsStore.pushFromTerminal(job('j2', 2_000));
    expect(notificationsStore.getSnapshot().unseen).toBe(2);

    render(<CompletionNotifications onOpen={vi.fn()} />);
    const bell = screen.getByLabelText(/Notifications \(2 unread\)/);

    // Opening must NOT dim the list: the whole point of the badge is telling the user WHICH
    // completions are new, so the unread state has to survive until they dismiss the panel.
    fireEvent.click(bell);
    expect(notificationsStore.getSnapshot().unseen).toBe(2);
    expect(screen.getAllByRole('menuitem')).toHaveLength(2);

    // Closing settles read state — this is what stops handled items re-alerting on next login.
    fireEvent.click(screen.getByLabelText(/Notifications \(2 unread\)/));
    expect(notificationsStore.getSnapshot().unseen).toBe(0);
    // History is kept; only the unread state changed.
    expect(notificationsStore.getSnapshot().items).toHaveLength(2);
  });

  it('clicking one item marks ONLY that item read (the rest stay highlighted)', () => {
    notificationsStore.pushFromTerminal(job('j1', 1_000));
    notificationsStore.pushFromTerminal(job('j2', 2_000));

    render(<CompletionNotifications onOpen={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/Notifications \(2 unread\)/));
    fireEvent.click(screen.getAllByRole('menuitem')[0]);

    const snap = notificationsStore.getSnapshot();
    expect(snap.unseen).toBe(1);
    expect(snap.items.filter((i) => i.seen)).toHaveLength(1);
  });
});
