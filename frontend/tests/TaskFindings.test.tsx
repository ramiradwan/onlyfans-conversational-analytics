import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FindingsClientError, type FindingsClient } from '../src/analytics/findingsClient';
import { parseFindings } from '../src/analytics/findingsContract';
import { AnalyticsPresentation } from '../src/components/analytics/AnalyticsPresentation';
import { TaskFindings } from '../src/components/analytics/TaskFindings';
import { createFindingsStore, type FindingsStore } from '../src/store/findingsStore';
import { findingsBaselineFixture as baseline, findingsResponse, findingsFilteredResponse, findingsSource, findingsSourceText, findingsSubsetResponse } from '../src/story-only/findingsFixtures';
import { storyBaselineState, storyDateRange } from '../src/story-only/analyticsFixtures';
import { theme } from '../src/theme';
import { context } from './questionFixture';

let store: FindingsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { cleanup(); store?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<FindingsClient> = {}) {
  const api: FindingsClient = { read: vi.fn(async (request) => parseFindings(request.category_filter ? findingsFilteredResponse(request) : findingsResponse(request), request)),
    evidence: vi.fn(async (reference) => findingsSource(reference)), ...overrides };
  store = createFindingsStore(api); let state = context(); const listeners = new Set<() => void>();
  const transport = { getState: () => state, subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
  const rendered = render(<ThemeProvider theme={theme}><TaskFindings baseline={baseline} controller={store} transport={transport} /></ThemeProvider>);
  return { api, rendered, update: (next: typeof state) => { state = next; listeners.forEach((listener) => listener()); } };
}

describe('TaskFindings', () => {
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
