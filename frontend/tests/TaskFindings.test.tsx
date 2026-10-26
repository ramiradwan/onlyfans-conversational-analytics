import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useCallback, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FindingsBaseline } from '../src/analytics/analyticsReadModel';
import { FindingsClientError, type FindingsClient } from '../src/analytics/findingsClient';
import { parseFindings, parseGroupedFindings } from '../src/analytics/findingsContract';
import type { LiteralsClient } from '../src/analytics/literalsClient';
import { parseLiterals } from '../src/analytics/literalsContract';
import { AnalyticsPresentation } from '../src/components/analytics/AnalyticsPresentation';
import { TaskFindings } from '../src/components/analytics/TaskFindings';
import { createFindingsStore, type FindingsStore } from '../src/store/findingsStore';
import { createLiteralsStore, type LiteralsStore } from '../src/store/literalsStore';
import { findingsBaselineFixture as baseline, findingsResponse, findingsFilteredResponse, findingsSource, findingsSourceText, findingsSubsetResponse, groupedFindingsResponse } from '../src/story-only/findingsFixtures';
import { storyBaselineState, storyDateRange } from '../src/story-only/analyticsFixtures';
import { literalsResponse } from '../src/story-only/literalsFixtures';
import { theme } from '../src/theme';
import { context } from './questionFixture';

let store: FindingsStore;
let literalStore: LiteralsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { cleanup(); store?.actions.dispose(); literalStore?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<FindingsClient> = {}, view: 'grouped' | null = null, onRefreshBaseline?: () => Promise<void>,
  parentRefresh?: (setBaseline: (value: FindingsBaseline | null) => void) => Promise<void>) {
  const api: FindingsClient = { read: vi.fn(async (request) => parseFindings(request.category_filter ? findingsFilteredResponse(request) : findingsResponse(request), request)),
    evidence: vi.fn(async (reference) => findingsSource(reference)), ...overrides };
  store = createFindingsStore(api, { view }); let state = context(); const listeners = new Set<() => void>();
  const literalApi: LiteralsClient = { read: vi.fn(async (request) => parseLiterals(literalsResponse(request), request)),
    evidence: vi.fn(async (reference) => findingsSource(reference)) };
  literalStore = createLiteralsStore(literalApi);
  const transport = { getState: () => state, subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
  const ownedStore = store;
  const panel = (nextBaseline: FindingsBaseline | null, nextStore = ownedStore, nextTransport = transport, refresh = onRefreshBaseline) =>
    <ThemeProvider theme={theme}><TaskFindings baseline={nextBaseline} controller={nextStore} literalsController={literalStore}
      transport={nextTransport} onRefreshBaseline={refresh} /></ThemeProvider>;
  function Parent() {
    const [accepted, setAccepted] = useState<FindingsBaseline | null>(baseline);
    const refresh = useCallback(async () => { await parentRefresh?.(setAccepted); }, [parentRefresh]);
    return panel(accepted, ownedStore, transport, refresh);
  }
  const rendered = render(parentRefresh ? <Parent /> : panel(baseline));
  return { api, literalApi, rendered, transport,
    rerender: (nextBaseline: FindingsBaseline | null, nextStore = ownedStore, nextTransport = transport) => rendered.rerender(panel(nextBaseline, nextStore, nextTransport)),
    update: (next: typeof state) => { state = next; listeners.forEach((listener) => listener()); } };
}

describe('TaskFindings', () => {
  it('refreshes the accepted baseline before reading a newly published paused generation', async () => {
    const nextBaseline = { ...baseline, projection_generation: baseline.projection_generation + 1,
      projection_digest: `sha256:${'2'.repeat(64)}` };
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const refreshBaseline = vi.fn(async (setBaseline: (value: FindingsBaseline | null) => void) => {
      setBaseline(null);
      await pending;
      setBaseline(nextBaseline);
    });
    const read = vi.fn<FindingsClient['read']>(async (request) => {
      if (request.baseline.projection_generation === baseline.projection_generation) {
        throw new FindingsClientError('building', 'The saved analysis is being updated.');
      }
      return parseFindings(findingsResponse(request), request);
    });
    setup({ read }, null, undefined, refreshBaseline);
    await screen.findByText('The saved analysis is being updated.');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved findings' }));
    expect(refreshBaseline).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => { release(); await pending; });
    await screen.findByText('Processing state when saved: paused; these saved findings remain visible.');
    expect(store.getState().result?.projection_generation).toBe(nextBaseline.projection_generation);
    expect(read.mock.calls.slice(1).every(([request]) => request.baseline.projection_generation === nextBaseline.projection_generation)).toBe(true);
  });
  it('reloads an unchanged baseline after one explicit refresh and blocks repeated clicks', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const refreshBaseline = vi.fn(() => pending);
    const { api } = setup({}, null, refreshBaseline);
    await screen.findByRole('table', { name: 'Topics findings' });
    const button = screen.getByRole('button', { name: 'Refresh saved findings' });
    fireEvent.click(button);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(refreshBaseline).toHaveBeenCalledTimes(1);
    expect(api.read).toHaveBeenCalledTimes(1);
    await act(async () => { release(); await pending; });
    await waitFor(() => expect(api.read).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ baseline }), expect.any(AbortSignal));
  });
  it('does not reload the previous baseline when its refresh rejects', async () => {
    const refreshBaseline = vi.fn(async () => { throw new Error('Unavailable'); });
    const { api } = setup({ read: vi.fn(async () => {
      throw new FindingsClientError('building', 'The saved analysis is being updated.');
    }) }, null, refreshBaseline);
    await screen.findByText('The saved analysis is being updated.');
    const button = screen.getByRole('button', { name: 'Refresh saved findings' });
    fireEvent.click(button);
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(refreshBaseline).toHaveBeenCalledTimes(1);
    expect(api.read).toHaveBeenCalledTimes(1);
    expect(screen.getByText('The saved analysis is being updated.')).toBeTruthy();
  });
  it('releases a failed refresh subscription before accepting another explicit refresh', async () => {
    const refreshBaseline = vi.fn(async () => {});
    const x = setup({}, null, refreshBaseline);
    await screen.findByRole('table', { name: 'Topics findings' });
    const subscribe = vi.spyOn(x.transport, 'subscribe').mockImplementationOnce(() => {
      throw new Error('Unavailable');
    });
    const button = screen.getByRole('button', { name: 'Refresh saved findings' });
    fireEvent.click(button);
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(refreshBaseline).not.toHaveBeenCalled();
    expect(x.api.read).toHaveBeenCalledTimes(1);
    subscribe.mockRestore();
    fireEvent.click(button);
    await waitFor(() => expect(x.api.read).toHaveBeenCalledTimes(2));
    expect(refreshBaseline).toHaveBeenCalledTimes(1);
  });
  it('releases a refresh even when its subscription cleanup throws', async () => {
    const refreshBaseline = vi.fn(async () => {});
    const x = setup({}, null, refreshBaseline);
    await screen.findByRole('table', { name: 'Topics findings' });
    const original = x.transport.subscribe;
    vi.spyOn(x.transport, 'subscribe').mockImplementationOnce(listener => {
      const unsubscribe = original(listener);
      return () => { unsubscribe(); throw new Error('Unavailable'); };
    });
    const button = screen.getByRole('button', { name: 'Refresh saved findings' });
    fireEvent.click(button);
    await waitFor(() => expect(x.api.read).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(x.api.read).toHaveBeenCalledTimes(3));
    expect(refreshBaseline).toHaveBeenCalledTimes(2);
  });
  it('discards a pending baseline-refresh continuation after unmount', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const x = setup({}, null, () => pending);
    await screen.findByRole('table', { name: 'Topics findings' });
    const reload = vi.spyOn(store.actions, 'reload');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved findings' }));
    x.rendered.unmount();
    await act(async () => { release(); await pending; });
    expect(reload).not.toHaveBeenCalled();
    expect(x.api.read).toHaveBeenCalledTimes(1);
  });
  it.each(['account', 'session', 'disconnect', 'direction', 'dates', 'controller', 'transport'] as const)(
    'discards a pending baseline-refresh continuation after the %s scope changes', async (change) => {
      let release!: () => void;
      const pending = new Promise<void>((resolve) => { release = resolve; });
      const x = setup({}, null, () => pending);
      await screen.findByRole('table', { name: 'Topics findings' });
      const reload = vi.spyOn(store.actions, 'reload');
      fireEvent.click(screen.getByRole('button', { name: 'Refresh saved findings' }));
      if (change === 'account') act(() => x.update({ ...context(), creatorAccountId: 'other-synthetic-account' }));
      if (change === 'session') act(() => x.update({ ...context(), session: {
        connection_id: '10000000-0000-4000-8000-000000000001',
        bridge_session_id: '20000000-0000-4000-8000-000000000001', creator_account_id: context().creatorAccountId,
        negotiated_protocol_version: '2', server_version: 'example' } }));
      if (change === 'disconnect') act(() => {
        x.update({ ...context(), connection: 'disconnected' });
        x.update(context());
      });
      if (change === 'direction') {
        fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Message direction' }));
        fireEvent.click(screen.getByRole('option', { name: 'Received messages' }));
      }
      if (change === 'dates') x.rerender({ ...baseline, requested_window: {
        ...baseline.requested_window, start: '2026-09-19T00:00:00.000Z' } });
      if (change === 'controller') {
        const next = createFindingsStore({ read: async request => parseFindings(findingsResponse(request), request),
          evidence: async reference => findingsSource(reference) });
        x.rerender(baseline, next);
      }
      if (change === 'transport') x.rerender(baseline, store, {
        getState: () => context(), subscribe: () => () => {} });
      await waitFor(() => expect(store.getState().status).not.toBe('loading'));
      const reads = vi.mocked(x.api.read).mock.calls.length;
      await act(async () => { release(); await pending; });
      expect(reload).not.toHaveBeenCalled();
      expect(x.api.read).toHaveBeenCalledTimes(reads);
    });
  it('shows declared unions, overlapping groups, ungrouped categories and pinned child actions', async () => {
    const read = vi.fn<FindingsClient['read']>(async (request) => parseGroupedFindings(await groupedFindingsResponse(request), request));
    setup({ read }, 'grouped');
    const subjects = await screen.findByRole('table', { name: 'Topics findings · Subjects' });
    expect(within(subjects).getByText('Example topic')).toBeTruthy();
    expect(within(subjects).getByText('Second category')).toBeTruthy();
    expect(screen.getAllByText(/Union support: 1 messages · 1 conversations · Complete \/ partial: 1 \/ 0/)).toHaveLength(2);
    expect(screen.getByRole('table', { name: 'Topics findings · Formats café 😀' })).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Mentions findings · Ungrouped categories' })).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Previous Topics findings · Subjects' })).toBeTruthy();
    expect(screen.getByText(/Groups and categories can overlap/)).toBeTruthy();
    const first = store.getState().result;
    if (first?.schema_version !== 'analytics-findings.v4') throw new Error('Grouped view required');
    const pin = `sha256:${first.navigation.policy!.sha256}`;
    fireEvent.click(within(subjects).getByRole('button', { name: 'View sources for Topics: Second category' }));
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(expect.objectContaining({
      category_filter: { task: 'message_topics', category_id: 'synthetic-secondary' },
      expected_navigation_sha256: pin, conversation_ref: null }), expect.any(AbortSignal)));
    await screen.findByText('Selected category: Topics · Second category');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Message direction' }));
    fireEvent.click(screen.getByRole('option', { name: 'Received messages' }));
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ direction: 'inbound', expected_navigation_sha256: pin }), expect.any(AbortSignal)));
    await screen.findByRole('table', { name: 'Topics findings · Subjects' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved findings' }));
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ expected_navigation_sha256: null }), expect.any(AbortSignal)));
  });
  it('retains flat category sources when grouping has no declaration', async () => {
    const read = vi.fn<FindingsClient['read']>(async (request) => {
      if (!request.view) return parseFindings(findingsResponse(request), request);
      const value = await groupedFindingsResponse(request);
      value.navigation = { availability: 'not_declared', policy: null, declaration: null, current: [], previous: null };
      return parseGroupedFindings(value, request);
    });
    setup({ read }, 'grouped');
    expect(await screen.findByRole('table', { name: 'Topics findings' })).toBeTruthy();
    expect(screen.getByText(/Declared category groups are unavailable/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View sources for Topics: Example topic' })).toBeTruthy();
    await store.actions.read(1);
    expect(read).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Use flat categories' }));
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(expect.not.objectContaining({ view: 'grouped' }), expect.any(AbortSignal)));
    await screen.findByRole('table', { name: 'Topics findings' });
    await act(async () => { await store.actions.read(1); });
    expect(read).toHaveBeenLastCalledWith(expect.objectContaining({ evidence_offset: 1 }), expect.any(AbortSignal));
    expect(read.mock.calls.at(-1)![0].expected_navigation_sha256).toBeUndefined();
  });
  it('keeps activated literal reads live across direction, conversation and category changes', async () => {
    const { literalApi } = setup();
    expect(literalApi.read).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Show recurring literal values' }));
    fireEvent.click(await screen.findByRole('button', { name: 'View sources for literal item (synthetic-mention)' }));
    await screen.findByRole('button', { name: 'Clear literal selection' });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Message direction' }));
    fireEvent.click(screen.getByRole('option', { name: 'Received messages' }));
    await waitFor(() => expect(literalApi.read).toHaveBeenLastCalledWith(expect.objectContaining({ direction: 'inbound', node_id: null,
      row_offset: 0, evidence_offset: 0, expected_normalization_sha256: null }), expect.any(AbortSignal)));
    expect(await screen.findByRole('table', { name: 'Recurring literal values' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Show recurring literal values' })).toBeNull();
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: 'Conversation' }));
    fireEvent.click(screen.getByRole('option', { name: 'Conversation 1 · 7 eligible messages' }));
    await waitFor(() => expect(literalApi.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: `c1:${'2'.repeat(64)}` }), expect.any(AbortSignal)));
    await screen.findByRole('table', { name: 'Topics findings' });
    fireEvent.click(screen.getByRole('button', { name: 'View sources for Topics: Example topic' }));
    await waitFor(() => expect(literalApi.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: null,
      category_filter: { task: 'message_topics', category_id: 'synthetic-topic' }, row_offset: 0, evidence_offset: 0 }), expect.any(AbortSignal)));
    expect(await screen.findByRole('table', { name: 'Recurring literal values' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear category filter' }));
    await waitFor(() => expect(literalApi.read).toHaveBeenLastCalledWith(expect.objectContaining({ category_filter: null }), expect.any(AbortSignal)));
    expect(screen.queryByRole('button', { name: 'Show recurring literal values' })).toBeNull();
  });
  it('selects and clears category sources accessibly while returning to all conversations', async () => {
    const { api } = setup();
    await screen.findByRole('table', { name: 'Topics findings' });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Conversation' }));
    fireEvent.click(screen.getByRole('option', { name: 'Conversation 1 · 7 eligible messages' }));
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: `c1:${'2'.repeat(64)}` }), expect.any(AbortSignal)));
    fireEvent.click(await screen.findByRole('button', { name: 'Mentions source 1: synthetic-mention (complete)' }));
    await screen.findByRole('dialog', { name: 'Finding source' });
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'View sources for Topics: Example topic' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ category_filter: { task: 'message_topics', category_id: 'synthetic-topic' },
      conversation_ref: null, evidence_offset: 0, conversation_offset: 0 }), expect.any(AbortSignal)));
    expect(await screen.findByText('Selected category: Topics · Example topic')).toBeTruthy();
    expect(screen.getByText(/Choosing or clearing a category returns to all conversations/)).toBeTruthy();
    const topics = await screen.findByRole('table', { name: 'Topics findings' });
    expect(within(topics).getByText(/50\s*%/)).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Mentions findings' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Mentions source 1:/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Clear category filter' }));
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ category_filter: null, conversation_ref: null,
      evidence_offset: 0, conversation_offset: 0 }), expect.any(AbortSignal)));
    expect(screen.queryByText(/Selected category:/)).toBeNull();
    expect(await screen.findByRole('button', { name: 'Mentions source 2: synthetic-mention (complete)' })).toBeTruthy();
  });
  it('shows both tasks with distinct counting units and honest label fallback', async () => {
    const { api } = setup();
    const topics = await screen.findByRole('table', { name: 'Topics findings' });
    const mentions = screen.getByRole('table', { name: 'Mentions findings' });
    expect(screen.getByRole('combobox', { name: 'Conversation' }).textContent).toBe('All conversations');
    expect(within(topics).getByText('Example topic')).toBeTruthy();
    expect(within(mentions).getByText('synthetic-mention')).toBeTruthy();
    expect(within(mentions).getByText('Display name unavailable; category ID shown.')).toBeTruthy();
    expect(within(mentions).getByRole('columnheader', { name: 'Exact occurrences' })).toBeTruthy();
    expect(within(topics).queryByRole('columnheader', { name: 'Exact occurrences' })).toBeNull();
    expect(screen.getAllByText(/Failed: 1 · Unsupported: 1 · Uncertain: 1/)).toHaveLength(2);
    expect(screen.getByText(/Processing state when saved: paused/)).toBeTruthy();
    expect(api.evidence).not.toHaveBeenCalled();
    expect(screen.getByRole('table', { name: 'Previous Topics findings' })).toBeTruthy();
    expect(screen.getAllByText(/Shares use only the 0 completely assessed messages/)).toHaveLength(2);
  });
  it('opens only the selected repeated Unicode mention occurrence', async () => {
    const { api } = setup();
    fireEvent.click(await screen.findByRole('button', { name: 'Mentions source 2: synthetic-mention (complete)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Finding source' });
    await waitFor(() => expect(dialog.querySelector('mark')?.textContent).toBe('item'));
    expect(dialog.querySelector('blockquote')?.textContent).toBe(findingsSourceText);
    expect(dialog.querySelector('mark')?.previousSibling?.textContent).toBe('😀 e\u0301 item and ');
    expect(dialog.querySelector('img')).toBeNull();
    expect(api.evidence).toHaveBeenCalledWith(expect.objectContaining({ span: { start: 14, end: 18 } }), expect.any(AbortSignal));
  });
  it('shows unavailable topics without fabricated coverage or absence counts', async () => {
    const { api } = setup({ read: async (request) => parseFindings(findingsSubsetResponse(request), request) });
    const topics = await screen.findByRole('region', { name: 'Topics' });
    expect(within(topics).getByText('Topics are unavailable for this analysis profile.')).toBeTruthy();
    expect(within(topics).queryByText(/Eligible:|Assessment pending|No supported categories/)).toBeNull();
    expect(within(topics).queryByRole('table')).toBeNull();
    expect(screen.queryByRole('table', { name: 'Topics findings' })).toBeNull();
    expect(screen.queryByRole('table', { name: 'Previous Topics findings' })).toBeNull();
    expect(screen.getByRole('table', { name: 'Mentions findings' })).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Previous Mentions findings' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Mentions source 2: synthetic-mention (complete)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Finding source' });
    await waitFor(() => expect(dialog.querySelector('mark')?.textContent).toBe('item'));
    expect(api.evidence).toHaveBeenCalledWith(expect.objectContaining({ span: { start: 14, end: 18 } }), expect.any(AbortSignal));
  });
  it('clears sources and results on account change', async () => {
    const { update } = setup();
    fireEvent.click(await screen.findByRole('button', { name: /Mentions source 1:/ }));
    await screen.findByText(/This saved source version was verified/);
    act(() => update({ ...context(), creatorAccountId: 'other-synthetic-account' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByRole('table')).toBeNull();
  });
  it('requests direction and conversation filters explicitly', async () => {
    const { api } = setup(); await screen.findByRole('table', { name: 'Topics findings' });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Message direction' }));
    fireEvent.click(screen.getByRole('option', { name: 'Received messages' }));
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ direction: 'inbound' }), expect.any(AbortSignal)));
    await screen.findByRole('combobox', { name: 'Conversation' });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Conversation' }));
    fireEvent.click(screen.getByRole('option', { name: 'Conversation 1 · 7 eligible messages' }));
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ conversation_ref: `c1:${'2'.repeat(64)}` }), expect.any(AbortSignal)));
  });
  it('keeps unavailable findings separate from baseline summary', async () => {
    setup({ read: async () => { throw new FindingsClientError('unavailable', 'Text analysis is unavailable for this saved analysis. Other analytics remain available.'); } });
    expect(await screen.findByText(/Other analytics remain available/)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull(); cleanup();
    render(<ThemeProvider theme={theme}><AnalyticsPresentation state={storyBaselineState} dateRange={storyDateRange} onDateRangeChange={() => undefined}
      findings={<div>Profile findings panel</div>} /></ThemeProvider>);
    expect(screen.getByRole('region', { name: 'Topics' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Text analysis' }));
    expect(screen.getByText('Profile findings panel')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Topics' })).toBeNull();
  });
});
