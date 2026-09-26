import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConversationQuestions } from '../src/components/analytics/ConversationQuestions';
import type { QuestionApi } from '../src/services/questionApi';
import { QuestionApiError } from '../src/services/questionApi';
import { createQuestionStore, type QuestionStore } from '../src/store/questionStore';
import { theme } from '../src/theme';
import { answer, catalog, context, evidence, NOW, thread } from './questionFixture';

let controller: QuestionStore;
beforeEach(() => { vi.stubGlobal('crypto', webcrypto); vi.setSystemTime(NOW); });
afterEach(() => { cleanup(); controller?.actions.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<QuestionApi> = {}) {
  const api: QuestionApi = { catalog: vi.fn(async () => catalog), answer: vi.fn(async (p) => answer(p)),
    evidence: vi.fn(async () => evidence()), clear: vi.fn(async () => undefined), ...overrides };
  controller = createQuestionStore(api, { getPage: vi.fn(async () => thread()) });
  let state = context();
  const listeners = new Set<() => void>();
  const transport = { getState: () => state, subscribe: (listener: () => void) => {
    listeners.add(listener); return () => { listeners.delete(listener); };
  } };
  render(<ThemeProvider theme={theme} defaultMode="light"><ConversationQuestions controller={controller} transport={transport} /></ThemeProvider>);
  return { api, update: (next: typeof state) => { state = next; listeners.forEach((listener) => listener()); } };
}
async function run() {
  await waitFor(() => expect((screen.getByRole('button', { name: 'Run question' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Run question' }));
  await screen.findByText(/Checked /);
}
describe('Conversation questions', () => {
  it('shows presets, dates and disabled pricing without bypassing availability', async () => {
    setup();
    expect(screen.getByLabelText('Start date')).toBeTruthy();
    await screen.findByText('Pricing discussions are not available yet.');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Question' }));
    expect(screen.getByRole('option', { name: /Pricing discussions/ }).getAttribute('aria-disabled')).toBe('true');
  });
  it('shows paginated results and opens source text and the saved conversation', async () => {
    setup(); await run();
    expect(screen.getByRole('table', { name: 'Conversation question results' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /View source 1/ }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(evidence().text);
    expect(dialog.querySelector('img')).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Open conversation' }));
    await within(dialog).findByText('Synthetic saved conversation');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
  it('distinguishes unknown reply status from an empty match list', async () => {
    setup({ answer: async (p) => { const value = answer(p); value.page.rows = []; value.page.undetermined_conversation_count = 1; value.page.total_matching_conversations = 0; return value; } });
    await run();
    expect(screen.getByText(/Reply status could not be determined/)).toBeTruthy();
    expect(screen.queryByText('No matches in the messages checked.')).toBeNull();
  });
  it('shows an honest empty state when no conversations match', async () => {
    setup({ answer: async (p) => { const value = answer(p); value.page.rows = []; value.page.total_matching_conversations = 0; return value; } });
    await run(); expect(screen.getByText('No matches in the messages checked.')).toBeTruthy();
  });
  it.each(['building', 'unavailable', 'error'] as const)('shows the %s state without old results', async (state) => {
    setup({ answer: async () => { throw new QuestionApiError(state, `Synthetic ${state} message`); } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Run question' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Run question' }));
    await screen.findByText(`Synthetic ${state} message`);
    expect(screen.queryByRole('table')).toBeNull();
  });
  it('clears source text and results immediately after the account changes', async () => {
    const { update } = setup(); await run();
    fireEvent.click(screen.getByRole('button', { name: /View source 1/ }));
    await screen.findByText(evidence().text);
    act(() => update({ ...context(), creatorAccountId: 'synthetic-other' }));
    await waitFor(() => expect(screen.queryByText(evidence().text)).toBeNull());
    expect(screen.queryByRole('table')).toBeNull();
  });
  it('clears results when dates change and displays invalid-range guidance', async () => {
    setup(); await run();
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-19' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-09-18' } });
    expect(screen.queryByRole('table')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Run question' }));
    expect(screen.getByText(/Choose an end date/)).toBeTruthy();
  });
});

it('shows unavailable questions without old results', async () => {
  setup({ catalog: async () => { throw new QuestionApiError('unavailable', 'Questions are unavailable.'); } });
  await screen.findByText('Questions are unavailable.');
  expect(screen.queryByRole('table')).toBeNull();
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
});

it('keeps question controls separate from the summary charts', async () => {
  const { AnalyticsPresentation } = await import('../src/components/analytics/AnalyticsPresentation');
  render(<ThemeProvider theme={theme} defaultMode="light"><AnalyticsPresentation
    state={{ status: 'unavailable', data: null, isRefreshing: false, message: 'Not yet available' }}
    dateRange={{ startDate: '', endDate: '' }} onDateRangeChange={() => undefined}
    questions={<div>Isolated question controls</div>}
  /></ThemeProvider>);
  expect(screen.queryByText('Isolated question controls')).toBeNull();
  fireEvent.click(screen.getByRole('tab', { name: 'Conversation questions' }));
  expect(screen.getByText('Isolated question controls')).toBeTruthy();
  fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
  expect(screen.queryByText('Isolated question controls')).toBeNull();
});
