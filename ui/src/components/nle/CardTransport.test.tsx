// @vitest-environment jsdom
/**
 * The Moment card's transport (2026-10-11): the FULL-duration track with the moment's range highlighted,
 * play that starts at the range start and stops at its end, replay, scrub anywhere, keyboard, mute.
 * Drives the real ClipEditor `embedded` with a stubbed media element (jsdom implements no playback).
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import ClipEditor from './ClipEditor';
import { shortTime } from './CardTransport';
import { theme } from '../../styles/tokens';

const clip = { id: 'm1', label: 'nba.mp4', kind: 'video' as const, src: 'https://x/v.mp4' };
const WINDOW = { start: 81, end: 86 };

/** Give a media element a scriptable clock: currentTime, duration, paused, play(), pause(), muted. */
function stubMedia(el: HTMLMediaElement, duration = 720) {
  let t = 0; let paused = true; let muted = false;
  Object.defineProperty(el, 'currentTime', { configurable: true, get: () => t,
    set: (v: number) => { t = v; el.dispatchEvent(new Event('seeking')); el.dispatchEvent(new Event('timeupdate')); } });
  Object.defineProperty(el, 'duration', { configurable: true, get: () => duration });
  Object.defineProperty(el, 'paused', { configurable: true, get: () => paused });
  Object.defineProperty(el, 'muted', { configurable: true, get: () => muted,
    set: (v: boolean) => { muted = v; el.dispatchEvent(new Event('volumechange')); } });
  el.play = vi.fn(() => { paused = false; el.dispatchEvent(new Event('play')); return Promise.resolve(); });
  el.pause = vi.fn(() => { if (!paused) { paused = true; el.dispatchEvent(new Event('pause')); } });
  /** Advance playback to `to` (as the browser would), firing timeupdate. */
  const tick = (to: number) => { t = to; el.dispatchEvent(new Event('timeupdate')); };
  return { tick, now: () => t };
}

function mount(over: Partial<React.ComponentProps<typeof ClipEditor>> = {}) {
  const registered: HTMLMediaElement[] = [];
  render(
    <ClipEditor clip={clip} theme={theme} embedded onMarkChange={vi.fn()}
      initialMark={{ in: WINDOW.start, out: WINDOW.end }} seekOnFirstLoad={WINDOW.start} playWindow={WINDOW}
      registerMedia={(_id, el) => { if (el) registered.push(el); }} {...over} />,
  );
  const el = registered[0];
  const clock = stubMedia(el);
  act(() => { el.dispatchEvent(new Event('loadedmetadata')); });
  return { el, ...clock };
}

beforeEach(() => vi.restoreAllMocks());

describe('Moment card transport', () => {
  it('has no native controls; the track spans the full duration with the moment highlighted', () => {
    const { el } = mount();
    expect(el.hasAttribute('controls')).toBe(false);
    const range = screen.getByTestId('card-range-m1');
    // 81 s .. 86 s of 720 s.
    expect(Number(range.dataset.left)).toBeCloseTo((81 / 720) * 100, 2);
    expect(Number(range.dataset.width)).toBeCloseTo((5 / 720) * 100, 2);
    const track = screen.getByTestId('card-track-m1');
    expect(track).toHaveAttribute('role', 'slider');
    expect(track).toHaveAttribute('aria-valuemax', '720');
    expect(track.getAttribute('aria-valuetext')).toContain('moment 1:21–1:26');
  });

  it('play starts at the range start and stops (parked) at the range end; play again replays', () => {
    const { el, tick, now } = mount();
    expect(now()).toBe(81); // first-load seek to the moment
    act(() => { fireEvent.click(screen.getByTestId('card-play-m1')); });
    expect(el.play).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('card-play-m1')).toHaveAccessibleName('Pause nba.mp4');
    act(() => tick(84));
    expect(el.pause).not.toHaveBeenCalled();
    act(() => tick(86.2));
    expect(el.pause).toHaveBeenCalledTimes(1);
    expect(now()).toBe(86); // parked exactly at the end
    expect(screen.getByTestId('card-play-m1')).toHaveAccessibleName('Play nba.mp4 1:21–1:26');
    act(() => { fireEvent.click(screen.getByTestId('card-play-m1')); });
    expect(now()).toBe(81); // replay from the start
  });

  it('scrubbing anywhere on the full track is allowed, and play from outside the moment runs on', () => {
    const { el, tick, now } = mount();
    const track = screen.getByTestId('card-track-m1');
    track.getBoundingClientRect = () => ({ left: 0, width: 720, top: 0, height: 18, right: 720, bottom: 18, x: 0, y: 0, toJSON: () => ({}) });
    act(() => { fireEvent.pointerDown(track, { clientX: 300 }); fireEvent.pointerUp(window); });
    expect(now()).toBe(300); // no snap back into the moment
    act(() => { fireEvent.click(screen.getByTestId('card-play-m1')); });
    act(() => tick(400));
    expect(el.pause).not.toHaveBeenCalled();
  });

  it('a scrub out of the moment while it plays cancels the stop-at-end', () => {
    const { el, tick } = mount();
    act(() => { fireEvent.click(screen.getByTestId('card-play-m1')); });
    act(() => { el.currentTime = 200; });
    act(() => tick(201));
    expect(el.pause).not.toHaveBeenCalled();
  });

  it('keyboard: arrows step, Home/End jump to the moment edges', () => {
    const { now } = mount();
    const track = screen.getByTestId('card-track-m1');
    act(() => { fireEvent.keyDown(track, { key: 'ArrowRight' }); });
    expect(now()).toBe(82);
    act(() => { fireEvent.keyDown(track, { key: 'ArrowLeft', shiftKey: true }); });
    expect(now()).toBe(77);
    act(() => { fireEvent.keyDown(track, { key: 'End' }); });
    expect(now()).toBe(86);
    act(() => { fireEvent.keyDown(track, { key: 'Home' }); });
    expect(now()).toBe(81);
  });

  it('mute toggles the element and the button state', () => {
    const { el } = mount();
    act(() => { fireEvent.click(screen.getByTestId('card-mute-m1')); });
    expect(el.muted).toBe(true);
    expect(screen.getByTestId('card-mute-m1')).toHaveAttribute('aria-pressed', 'true');
  });

  it('a gallery-driven play (Play all: seek to start + play, then its own stop) is not fought', () => {
    const { el, tick, now } = mount();
    const onTime = vi.fn(() => { if (now() >= WINDOW.end) el.pause(); });
    el.addEventListener('timeupdate', onTime);
    act(() => { el.currentTime = WINDOW.start; void el.play(); });
    act(() => tick(86));
    expect(onTime).toHaveBeenCalled();
    expect(el.paused).toBe(true);
    expect(now()).toBe(86); // both stop at the same end; the sequence's timeupdate sees >= end and advances
  });

  it('audio cards get the same transport', () => {
    const registered: HTMLMediaElement[] = [];
    render(<ClipEditor clip={{ ...clip, id: 'a1', kind: 'audio' }} theme={theme} embedded onMarkChange={vi.fn()}
      playWindow={WINDOW} registerMedia={(_i, el) => { if (el) registered.push(el); }} />);
    expect(screen.getByTestId('card-transport-a1')).toBeInTheDocument();
    expect(registered[0].hasAttribute('controls')).toBe(false);
  });

  it('shortTime formats m:ss and h:mm:ss', () => {
    expect(shortTime(81.9)).toBe('1:21');
    expect(shortTime(3725)).toBe('1:02:05');
    expect(shortTime(-3)).toBe('0:00');
  });
});
