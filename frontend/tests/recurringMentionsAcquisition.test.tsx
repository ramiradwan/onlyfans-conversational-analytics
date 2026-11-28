import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RecurringMentionsAcquisition } from '../src/components/analytics/RecurringMentionsAcquisition';
import { createTextAnalysisAcquisitionApi, type AcquisitionFlow, type AcquisitionKind } from '../src/services/textAnalysisAcquisitionApi';
import { textAnalysisScopeKey } from '../src/store/textAnalysisExecutionStore';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const id = '10000000-0000-4000-8000-000000000001', reviewId = '20000000-0000-4000-8000-000000000001';
const expiry = '2027-01-10T12:00:00.000Z', entry = 'b'.repeat(43);
const terms = { payment_mode: 'test' as const, amount_minor: 999 as const, currency: 'EUR' as const, licensed_major_version: 1 as const, update_rights: true as const };
const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
function row(state: AcquisitionFlow['state'], kind: AcquisitionKind = 'paid'): AcquisitionFlow {
  return { flow_id: id, state, expires_at: expiry, navigation_available: state === 'awaiting_browser',
    review: state === 'confirmation_required' ? { review_id: reviewId, expires_at: expiry, terms: kind === 'paid' ? terms : { execution_limit: 1 } } : null };
}
function setup(kind: AcquisitionKind = 'paid', existing: AcquisitionFlow | null = null) {
  const transport = createBridgeTransportStore(); transport.bindAccount('example');
  transport.acceptSession({ connection_id: id, bridge_session_id: reviewId, creator_account_id: 'example', negotiated_protocol_version: '2', server_version: 'sample' });
  const scope = textAnalysisScopeKey(transport.getState(), 'creator-ceo', 'example');
  let current = existing;
  const base = '/api/v1/nlp/text-analysis-acquisition/' + kind;
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    expect(init).toMatchObject({ credentials: 'same-origin', redirect: 'error', cache: 'no-store' });
    if (url === base && init?.method === 'GET') return response({
      current, eligible: current?.state !== 'adopted', reason: current?.state === 'adopted' ? 'already_acquired' : null,
    });
    expect(init?.method).toBe('POST');
    expect(init?.headers).toMatchObject({ 'X-CSRF-Token': 'test-csrf', 'Content-Type': 'application/json' });
    if (url === base + '/prepare') current = row('awaiting_browser', kind);
    else if (url === base + '/' + id + '/browser-entry') return response({ entry_code: entry });
    else if (url === base + '/' + id + '/read') current = row('confirmation_required', kind);
    else if (url === base + '/' + id + '/submit') {
      expect(JSON.parse(String(init?.body))).toEqual({ review_id: reviewId }); current = row('adopted', kind);
    } else throw new Error('Unexpected request');
    return response({ current });
  });
  const openEntry = vi.fn(async () => {}), adopt = vi.fn();
  const api = createTextAnalysisAcquisitionApi({ fetcher, openEntry, csrf: () => 'test-csrf' });
  const view = (acquired = false) => <ThemeProvider theme={theme}><RecurringMentionsAcquisition kind={kind} account="example"
    scope={scope} acquired={acquired} canAcquire={!acquired} onAdopt={adopt} api={api} transport={transport} /></ThemeProvider>;
  const paths = () => fetcher.mock.calls.map(([input]) => String(input).slice(base.length));
  return { api, transport, view, fetcher, openEntry, adopt, paths };
}
beforeEach(() => { useUserStore.getState().actions.setUserRole('creator-ceo'); });
afterEach(() => { cleanup(); useUserStore.getState().actions.setUserRole(null); });

describe('Installed text analysis acquisition', () => {
  it.each(['paid', 'preview'] as const)('opens the fixed hosted %s entry and submits only the independently reviewed operation', async (kind) => {
    const x = setup(kind), paid = kind === 'paid', rendered = render(x.view());
    const start = paid ? 'Start test checkout' : 'Request free preview', check = paid ? 'Check payment' : 'Check preview access';
    const submit = paid ? 'Activate on this computer' : 'Allow one preview';
    fireEvent.click(await screen.findByRole('button', { name: start }));
    await waitFor(() => expect(x.openEntry).toHaveBeenCalledExactlyOnceWith(kind, entry));
    fireEvent.focus(window);
    expect(screen.queryByRole('button', { name: submit })).toBeNull();
    expect(x.paths().some((path) => path.endsWith('/read') || path.endsWith('/submit'))).toBe(false);
    await waitFor(() => expect((screen.getByRole('button', { name: check }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: check }));
    const button = await screen.findByRole('button', { name: submit });
    expect(x.adopt).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(x.adopt).toHaveBeenCalledOnce());
    expect(screen.queryByText('You’ve activated the text analysis module on this computer.')).toBeNull();
    rendered.rerender(x.view(true));
    await screen.findByText(paid ? 'You’ve activated the text analysis module on this computer.' : 'One preview is available on this computer.');
    expect(x.paths().filter((p) => p.endsWith('/prepare'))).toHaveLength(1);
    expect(x.paths().filter((p) => p.endsWith('/submit'))).toHaveLength(1);
  });
  it('resumes the current operation on reopening without starting a new checkout', async () => {
    const x = setup('paid', row('awaiting_browser')); render(x.view());
    fireEvent.click(await screen.findByRole('button', { name: 'Check payment' }));
    await screen.findByRole('button', { name: 'Activate on this computer' });
    expect(x.paths()).toEqual(['', '/' + id + '/read']);
    expect(x.openEntry).not.toHaveBeenCalled(); expect(x.adopt).not.toHaveBeenCalled();
  });
  it('refuses a different flow, changed paid terms, a swapped preview, or navigation to a raw destination', async () => {
    const signal = new AbortController().signal, openEntry = vi.fn();
    const fetcher = vi.fn<typeof fetch>();
    const api = createTextAnalysisAcquisitionApi({ fetcher, openEntry, csrf: () => 'test' });
    const current = row('confirmation_required'), review = current.review!;
    for (const changed of [{ ...current, flow_id: reviewId }, { ...current, review: { ...review, terms: { ...terms, amount_minor: 1000 } } },
      { ...current, review: { ...review, terms: { ...terms, payment_mode: 'live' } } }, row('confirmation_required', 'preview')]) {
      fetcher.mockResolvedValueOnce(response({ current: changed }));
      await expect(api.read('paid', id, signal)).rejects.toThrow();
    }
    fetcher.mockResolvedValueOnce(response({ entry_code: 'https://example.invalid/' }));
    await expect(api.openBrowser('paid', id, signal, () => true)).rejects.toThrow();
    fetcher.mockResolvedValueOnce(response({ entry_code: entry }));
    await expect(api.openBrowser('paid', id, signal, () => false)).rejects.toThrow();
    expect(openEntry).not.toHaveBeenCalled();
  });
  it('refuses a missing CSRF token before changing a saved request', async () => {
    const fetcher = vi.fn<typeof fetch>(), api = createTextAnalysisAcquisitionApi({ fetcher, csrf: () => null });
    await expect(api.submit('paid', id, reviewId, new AbortController().signal)).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
});
