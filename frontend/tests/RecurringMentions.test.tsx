import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { normalization, phraseId, recurringSavedFixture, reportFixture, reportRequest } from './recurringMentionsFixture';
import { createRecurringMentionsClient, exportRecurringReport, parseRecurringReport, type RecurringMentionsClient, type RecurringReportRequest } from '../src/analytics/recurringMentions';
import { parseSavedPreview } from '../src/analytics/textAnalysisContract';
import { RecurringMentionsReport } from '../src/components/analytics/RecurringMentionsReport';
import { findingsAccount, findingsBaselineFixture, findingsSource } from '../src/story-only/findingsFixtures';
import { theme } from '../src/theme';

const signal = () => new AbortController().signal;
function fixture() {
  return {
    capability: vi.fn<RecurringMentionsClient['capability']>(),
    report: vi.fn<RecurringMentionsClient['report']>(async (request) => parseRecurringReport(reportFixture(request), request)),
    evidence: vi.fn<RecurringMentionsClient['evidence']>(async (ref) => findingsSource(ref)),
    context: vi.fn<RecurringMentionsClient['context']>(),
  } satisfies RecurringMentionsClient;
}
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Recurring mentions', () => {
  it('verifies the two distinct senders in the actual saved preview shape and keeps generic previews separate', async () => {
    const value = recurringSavedFixture(), id = value.output.contribution_id;
    await expect(parseSavedPreview(value, id)).resolves.toMatchObject({ schema_version: 'text-analysis-preview-saved.v2' });
    const duplicate = structuredClone(value);
    for (const entry of duplicate.output.summaries[0].evidence) entry.source.speaker_ref = duplicate.output.summaries[0].evidence[0].source.speaker_ref;
    await expect(parseSavedPreview(duplicate, id)).rejects.toThrow();
    await expect(parseSavedPreview({ ...value, scope: { ...value.scope, message_cap: 32 } }, id)).rejects.toThrow();
    await expect(parseSavedPreview({ ...value, disclosure: { ...value.disclosure, selection_policy: { ...value.disclosure.selection_policy, revision: '2' } } }, id)).rejects.toThrow();
  });
  it('checks published totals, tied Unicode spellings, source versions and the exact lower date bound', () => {
    const value = reportFixture();
    expect(parseRecurringReport(value, reportRequest).row_page.total).toBe(1);
    const next = structuredClone(value);
    next.row_page.total = 2;
    next.row_page.items[0].literal_key = '\ue100';
    next.row_page.items.push({ ...next.row_page.items[0], phrase_id: `sha256:${'2'.repeat(64)}`, literal_key: '🐟' });
    expect(parseRecurringReport(next, reportRequest).row_page.total).toBe(2);
    next.row_page.items.reverse();
    expect(() => parseRecurringReport(next, reportRequest)).toThrow();
    const ref = value.evidence_page.items[0].reference;
    ref.sent_at = value.requested_window.start!;
    expect(() => parseRecurringReport(value, reportRequest)).toThrow();
    ref.sent_at = ref.sent_at.replace('00Z', '00.000001Z');
    expect(() => parseRecurringReport(value, reportRequest)).toThrow();
    value.effective_window.start = ref.sent_at;
    expect(parseRecurringReport(value, reportRequest).evidence_page.total).toBe(2);
    ref.source_revision += 1;
    expect(() => parseRecurringReport(value, reportRequest)).toThrow();
  });
  it('exports every page against the same normalization, even with no remaining phrases to page', async () => {
    const client = fixture();
    client.report.mockImplementation(async (request) => {
      const page = reportFixture(request);
      const total = 101, all = Array.from({ length: total }, (_, i) => {
        const reference = { ...reportFixture().evidence_page.items[0].reference, message_ref: `m1:${i.toString(16).padStart(64, '0')}` };
        return { phrase_id: phraseId, assessment: 'complete' as const, reference };
      });
      page.row_page.items = page.row_page.items.map((item) => ({ ...item, supporting_messages: total, occurrences: total }));
      page.evidence_page = { ...page.evidence_page, total, has_more: request.evidenceOffset + request.limit < total,
        items: all.slice(request.evidenceOffset, request.evidenceOffset + request.limit) };
      return page;
    });
    const result = await exportRecurringReport(client, reportRequest.baseline, reportFixture(), signal());
    expect(result.phrases).toHaveLength(1);
    expect(result.sources).toHaveLength(101);
    expect(client.report.mock.calls.map(([r]) => [r.normalization, r.rowOffset, r.evidenceOffset])).toEqual([[normalization, 0, 0], [normalization, 1, 100]]);
  });
  it('keeps the selected phrase after it moves to a later page in a new ranking', async () => {
    const client = fixture(), selection = { current: phraseId }, refresh = vi.fn();
    client.report.mockImplementation(async (request) => {
      const result = reportFixture(request), old = reportFixture().row_page.items[0];
      const rows = Array.from({ length: 20 }, (_, index) => ({ ...old, literal_key: 'a' + String(index).padStart(2, '0'),
        phrase_id: 'sha256:' + index.toString(16).padStart(64, '0') })).concat(old);
      result.row_page = { offset: request.rowOffset, limit: request.limit, total: rows.length,
        has_more: request.rowOffset + request.limit < rows.length, items: rows.slice(request.rowOffset, request.rowOffset + request.limit) };
      return parseRecurringReport(result, request);
    });
    render(<ThemeProvider theme={theme}><RecurringMentionsReport account={findingsAccount} baseline={findingsBaselineFixture}
      selection={selection} client={client} onRefreshBaseline={refresh} /></ThemeProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'Open saved report' }));
    await screen.findByRole('region', { name: 'Quotes for item' });
    expect(client.report.mock.calls.at(-1)?.[0]).toMatchObject({ rowOffset: 20, phraseId, normalization });
  });
  it('closes the optional entry on an absent capability and sends only same-origin passive requests', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 404 }));
    const api = createRecurringMentionsClient(fetcher);
    expect(await api.capability(signal())).toBeNull();
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/api/v1/nlp/paid-insights', expect.objectContaining({
      method: 'GET', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
    }));
  });
  it('opens a saved finding and its verified quote, preserves a selection on refresh, and suppresses a changed source', async () => {
    const client = fixture(), refresh = vi.fn();
    render(<ThemeProvider theme={theme}><RecurringMentionsReport account={findingsAccount} baseline={findingsBaselineFixture}
      client={client} onRefreshBaseline={refresh} /></ThemeProvider>);
    expect(client.report).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Open saved report' }));
    await screen.findByText('2 senders · 2 messages');
    fireEvent.click(screen.getByRole('button', { name: 'Show quotes for item' }));
    fireEvent.click(await screen.findByRole('button', { name: /^Open quote 1/ }));
    expect((await screen.findByRole('blockquote')).querySelector('mark')?.textContent).toBe('item');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check for report updates' }));
    await screen.findByRole('button', { name: /^Open quote 1/ });
    expect(client.report.mock.calls.at(-1)?.[0]).toMatchObject({ phraseId, normalization });
    client.evidence.mockImplementation(async (ref) => ({ ...findingsSource(ref), direction: 'outbound' }));
    fireEvent.click(screen.getByRole('button', { name: /^Open quote 2/ }));
    await screen.findByText(/This saved quote could not be verified/);
    expect(screen.queryByRole('blockquote')).toBeNull();
    expect(refresh).toHaveBeenCalledTimes(2);
  });
  it('exports a free saved report through the desktop save picker with its grouped counts and exact spans', async () => {
    const client = fixture(), bytes: Uint8Array[] = [], close = vi.fn();
    const destination = new WritableStream({ write: (part) => { bytes.push(part); }, close });
    const picker = vi.fn(async () => ({ createWritable: async () => destination }));
    vi.stubGlobal('showSaveFilePicker', picker);
    render(<ThemeProvider theme={theme}><RecurringMentionsReport account={findingsAccount} baseline={findingsBaselineFixture}
      client={client} onRefreshBaseline={() => {}} /></ThemeProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'Open saved report' }));
    const button = await screen.findByRole('button', { name: 'Export saved report' });
    fireEvent.click(button);
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(picker).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ suggestedName: 'recurring-mentions.json' }));
    expect(picker.mock.invocationCallOrder[0]).toBeLessThan(client.report.mock.invocationCallOrder[1]);
    const exported = JSON.parse(bytes.map((part) => new TextDecoder().decode(part)).join(''));
    expect(exported.phrases).toEqual(reportFixture().row_page.items);
    expect(exported.sources).toEqual(reportFixture().evidence_page.items);
  });
  it('drops a late result after an account change', async () => {
    const client = fixture(), refresh = vi.fn();
    let done!: (report: ReturnType<typeof reportFixture>) => void;
    client.report.mockImplementationOnce((_r: RecurringReportRequest) => new Promise((resolve) => { done = resolve; }));
    const view = (account: string) => <ThemeProvider theme={theme}><RecurringMentionsReport key={account} account={account}
      baseline={findingsBaselineFixture} client={client} onRefreshBaseline={refresh} /></ThemeProvider>;
    const rendered = render(view(findingsAccount));
    fireEvent.click(screen.getByRole('button', { name: 'Open saved report' }));
    await waitFor(() => expect(client.report).toHaveBeenCalledOnce());
    rendered.rerender(view('another-test-account'));
    expect(client.report.mock.calls[0][1].aborted).toBe(true);
    done(reportFixture());
    expect(screen.queryByText('2 senders · 2 messages')).toBeNull();
  });
});
