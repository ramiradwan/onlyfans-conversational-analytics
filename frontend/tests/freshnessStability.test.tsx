import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FreshnessStatus } from '../src/components/ui/FreshnessStatus';
import { LoadingFrame } from '../src/components/ui/ReservedRegion';
import type { CatchupFreshness } from '../src/protocol';
import { theme } from '../src/theme';
import { connectionGrace, createConnectionGrace } from '../src/utils/connectionGrace';
import { formatSlotDateTime, presentFreshness } from '../src/utils/freshnessPresentation';

const now = new Date('2026-10-02T12:00:00Z');
const fresh = (status: CatchupFreshness['status'], reason: CatchupFreshness['reason'] = null): CatchupFreshness => ({
  status, reason, gap_epoch: 1, uncertain_since: '2026-09-28T11:05:00Z', check_id: null,
  last_closed_at: null, observing_since: null, evaluated_at: now.toISOString(),
});
const present = (freshness: CatchupFreshness | null) => presentFreshness({ freshness, bridge: 'connected', snapshotUsable: true, now, timeZone: 'Europe/Helsinki' });
afterEach(() => { cleanup(); connectionGrace.dispose(); vi.useRealTimers(); });

describe('freshness evidence', () => {
  it('keeps the connection note within three detail facts', () => {
    connectionGrace.observe(false, true);
    const value = { ...fresh('behind', 'awaiting_check'), last_closed_at: now.toISOString(), observing_since: now.toISOString() };
    const view = render(<ThemeProvider theme={theme}><FreshnessStatus freshness={value} bridge="connected" snapshotUsable now={now} timeZone="UTC" /></ThemeProvider>);
    fireEvent.click(view.getByRole('button'));
    expect(view.getByRole('dialog').querySelectorAll('dt')).toHaveLength(3);
    expect(view.getByText('Reconnecting')).toBeTruthy();
  });
  it('only keeps the canary label after a recorded closure', () => {
    expect(present(fresh('checking', 'canary')).label).toBe('Checking messages');
    expect(present({ ...fresh('checking', 'canary'), last_closed_at: now.toISOString() }).label).toBe('Up to date');
    expect(present(fresh('checking', 'catch_up')).label).toBe('Checking messages');
    expect(present(fresh('current')).label).toBe('Up to date');
    expect(present(null).label).toBe('Messages not checked');
  });
  it('never invents a boundary or echoes an unknown reason', () => {
    expect(present({ ...fresh('behind', 'awaiting_check'), uncertain_since: null }).label).toBe('Messages not checked');
    expect(present({ ...fresh('paused'), reason: 'x'.repeat(4096) } as CatchupFreshness).label).toBe('Paused · needs attention');
  });
  it('downgrades a current claim when the bridge or snapshot is unusable', () => {
    for (const bridge of ['idle', 'connecting', 'handshaking', 'disconnected', 'reconnecting', 'error'] as const) {
      expect(presentFreshness({ freshness: fresh('current'), bridge, snapshotUsable: true, now, timeZone: 'UTC' }).label).not.toBe('Up to date');
    }
    expect(presentFreshness({ freshness: fresh('current'), bridge: 'connected', snapshotUsable: false, now, timeZone: 'UTC' }).label).not.toBe('Up to date');
  });
  it('formats the viewer zone and the local year without a fixed offset', () => {
    expect(formatSlotDateTime('2026-09-28T11:05:00Z', now, 'Europe/Helsinki')).toBe('28.09 14.05');
    expect(formatSlotDateTime('2026-01-01T00:30:00Z', now, 'America/New_York')).toBe('31.12.2025 19.30');
    expect(formatSlotDateTime('2026-01-01T00:30:00Z', now, 'Europe/Helsinki')).toBe('01.01 02.30');
  });
  it('keeps checking visible for 800 ms but never delays a loss of observation', () => {
    vi.useFakeTimers();
    const component = (value: CatchupFreshness) => <ThemeProvider theme={theme}><FreshnessStatus freshness={value} bridge="connected" snapshotUsable now={now} timeZone="UTC" /></ThemeProvider>;
    const view = render(component(fresh('checking', 'catch_up')));
    view.rerender(component(fresh('current')));
    expect(view.getByRole('button').textContent).toContain('Checking messages');
    act(() => vi.advanceTimersByTime(800));
    expect(view.getByRole('button').textContent).toContain('Up to date');
    view.rerender(component(fresh('checking', 'canary')));
    view.rerender(component(fresh('paused', 'extension_offline')));
    expect(view.getByRole('button').textContent).toContain('Paused · browser offline');
  });
});

describe('loading label timing', () => {
  it('reserves skeletons immediately and shows a label only at 400 ms', () => {
    vi.useFakeTimers();
    const view = render(<ThemeProvider theme={theme}><LoadingFrame label="Loading dashboard…" /></ThemeProvider>);
    expect(view.container.querySelector('.MuiSkeleton-root')).toBeTruthy();
    expect(view.queryByText('Loading dashboard…')).toBeNull();
    act(() => vi.advanceTimersByTime(399));
    expect(view.queryByText('Loading dashboard…')).toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(view.getByText('Loading dashboard…')).toBeTruthy();
  });
});

describe('shared interruption grace', () => {
  it('starts at the first loss, lasts 3 s, and recovers immediately', () => {
    vi.useFakeTimers();
    const grace = createConnectionGrace();
    grace.observe(true, true);
    grace.observe(false, true);
    expect(grace.getSnapshot()).toBe('grace');
    act(() => vi.advanceTimersByTime(2999));
    expect(grace.getSnapshot()).toBe('grace');
    act(() => vi.advanceTimersByTime(1));
    expect(grace.getSnapshot()).toBe('interrupted');
    grace.observe(true, true);
    expect(grace.getSnapshot()).toBe('connected');
    grace.dispose();
  });
  it('shares one deadline across views, focus events and remounts', () => {
    vi.useFakeTimers();
    const grace = createConnectionGrace();
    function View() { return <span>{useSyncExternalStore(grace.subscribe, grace.getSnapshot)}</span>; }
    grace.observe(true, true);
    grace.observe(false, true);
    const first = render(<View />);
    act(() => vi.advanceTimersByTime(2000));
    first.unmount();
    const second = render(<><View /><View /></>);
    act(() => { window.dispatchEvent(new Event('focus')); grace.observe(false, true); vi.advanceTimersByTime(1000); });
    expect(second.getAllByText('interrupted')).toHaveLength(2);
    grace.dispose();
  });
});
