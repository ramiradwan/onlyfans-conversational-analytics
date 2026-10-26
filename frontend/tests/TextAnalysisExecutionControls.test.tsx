import { webcrypto } from 'node:crypto';

import { ThemeProvider } from '@mui/material/styles';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import commandExample from '../../shared/nlp/application/v1/fixtures/enrollment-command.json';
import resultExample from '../../shared/nlp/application/v1/fixtures/enrollment-result.json';
import viewExample from '../../shared/nlp/application/v1/fixtures/enrollment-view.json';
import {
  parseEnrollmentCommandResult, parseEnrollmentView, textAnalysisDigest, type ClientIntent, type ClientIntentRecord, type EnrollmentView,
} from '../src/analytics/textAnalysisContract';
import { TextAnalysisExecutionControls } from '../src/components/TextAnalysisExecutionControls';
import type { NlpDependenciesApi, NlpDependencyStatus } from '../src/services/nlpDependenciesApi';
import { TextAnalysisApiError, type TextAnalysisApi } from '../src/services/textAnalysisApi';
import { createTextAnalysisClient } from '../src/services/textAnalysisClient';
import { createBridgeTransportStore } from '../src/store/transportStore';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const ACCOUNT = 'example-account';
const KEY = commandExample.command_key;
const clone = <T,>(value: T): T => structuredClone(value);
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function connect(transport: ReturnType<typeof createBridgeTransportStore>, account = ACCOUNT, suffix = '1') {
  transport.bindAccount(account);
  transport.acceptSession({ connection_id: `10000000-0000-4000-8000-00000000000${suffix}`,
    bridge_session_id: `20000000-0000-4000-8000-00000000000${suffix}`, creator_account_id: account,
    negotiated_protocol_version: '2', server_version: 'example' });
}
async function retained(intent: ClientIntent): Promise<ClientIntentRecord> {
  return { ...intent, schema_version: 'text-analysis-client-intent-record.v1',
    body_sha256: await textAnalysisDigest(intent.body), retained_at: '2026-10-25T00:00:00Z' };
}
function fixture() {
  let current: EnrollmentView = parseEnrollmentView({ current: { ...viewExample.current, enrollment_id: null,
    mode_version: 0, state: 'retired', capsule_available: false }, profile: null }, ACCOUNT);
  const journal = new Map<string, ClientIntentRecord>();
  const api = {
    current: vi.fn<TextAnalysisApi['current']>(async () => clone(current)),
    command: vi.fn<TextAnalysisApi['command']>(async (body) => {
      current = parseEnrollmentView(viewExample, ACCOUNT);
      return parseEnrollmentCommandResult({ ...clone(resultExample), receipt: { ...resultExample.receipt, command_key: body.command_key,
        meaning_sha256: await textAnalysisDigest(body) } }, body.command_key);
    }),
    commandReceipt: vi.fn<TextAnalysisApi['commandReceipt']>().mockResolvedValue(null),
    prepare: vi.fn<TextAnalysisApi['prepare']>(), outcome: vi.fn<TextAnalysisApi['outcome']>().mockResolvedValue(null),
    confirm: vi.fn<TextAnalysisApi['confirm']>(), savedList: vi.fn<TextAnalysisApi['savedList']>().mockResolvedValue({ items: [], next_after: null }),
    saved: vi.fn<TextAnalysisApi['saved']>(), evidence: vi.fn<TextAnalysisApi['evidence']>(),
    retain: vi.fn<TextAnalysisApi['retain']>(async (intent) => {
      const record = await retained(intent); journal.set(`${intent.kind}:${intent.key}`, record); return record;
    }),
    retained: vi.fn<TextAnalysisApi['retained']>(async (kind, key) => journal.get(`${kind}:${key}`) ?? null),
    retainedList: vi.fn<TextAnalysisApi['retainedList']>().mockResolvedValue({ items: [], next_after: null }),
  } satisfies TextAnalysisApi;
  const profile = viewExample.profile;
  const status: NlpDependencyStatus = { configured: true, profiles: [{ profile_id: profile.profile_id, revision: profile.revision,
    profile_sha256: profile.profile_sha256, catalogued: true, installed: false, prepared: false, withdrawn: false,
    storage_state: 'not_installed', languages: ['en'], tasks: ['message_topics', 'concept_mentions'], resources: null }],
  available_profiles: [], failure: null, resume_granularity: 'verified_files_current_file_restarts', acquisitions: [], selected: null };
  const dependencies = { get: vi.fn<NlpDependenciesApi['get']>().mockResolvedValue(status),
    command: vi.fn<NlpDependenciesApi['command']>(), observe: vi.fn<NlpDependenciesApi['observe']>() } satisfies NlpDependenciesApi;
  const transport = createBridgeTransportStore(); connect(transport);
  const client = createTextAnalysisClient(api);
  const view = (account = ACCOUNT) => <ThemeProvider theme={theme}><TextAnalysisExecutionControls creatorAccountId={account}
    transport={transport} client={client} dependenciesApi={dependencies} /></ThemeProvider>;
  return { api, dependencies, transport, view, journal };
}
async function review() {
  await screen.findByText('Processing: not enrolled.');
  fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Approved processing profile' }));
  fireEvent.click(await screen.findByRole('option', { name: 'profile.synthetic · 1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Review enablement' }));
  return within(await screen.findByRole('dialog', { name: 'Review processing change' }));
}
function feedbackRegion() {
  const node = document.querySelector('[data-reserved-region="processing-feedback"]');
  if (!(node instanceof HTMLElement)) throw new Error('Processing feedback is missing');
  return node;
}
async function readFeedback(text: string | RegExp) {
  fireEvent.click(within(feedbackRegion()).getByRole('button', { name: 'Show details' }));
  const dialog = await screen.findByRole('dialog', { name: 'Details' });
  expect(within(dialog).getByText(text)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
}
beforeEach(() => {
  vi.stubGlobal('crypto', { subtle: webcrypto.subtle, randomUUID: () => KEY });
  useUserStore.getState().actions.setUserRole('creator-ceo');
});
afterEach(() => { cleanup(); useUserStore.setState({ role: null, roleResolved: false }); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('Account processing controls', () => {
  it('reviews cold-profile enablement and waits for a durable intent acknowledgement before the command', async () => {
    const x = fixture(), acknowledgement = deferred<ClientIntentRecord>();
    x.api.retain.mockReturnValue(acknowledgement.promise);
    render(x.view());
    await screen.findByText('Processing: not enrolled.');
    expect(x.api.retain).not.toHaveBeenCalled(); expect(x.api.command).not.toHaveBeenCalled();
    expect(x.dependencies.command).not.toHaveBeenCalled();
    const dialog = await review();
    expect(dialog.getByText(/Enabling does not download files or promise offline readiness/)).toBeTruthy();
    expect(document.querySelector('[data-journey-state="textanalysis.processing_review"]')).toBeTruthy();
    expect(x.api.retain).not.toHaveBeenCalled();
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm processing change' }));
    await waitFor(() => expect(x.api.retain).toHaveBeenCalledOnce());
    expect(x.api.command).not.toHaveBeenCalled();
    const intent = x.api.retain.mock.calls[0][0];
    expect(intent).toMatchObject({ kind: 'enrollment_command', key: KEY, body: commandExample });
    await act(async () => acknowledgement.resolve(await retained(intent)));
    await screen.findByText('Processing: enabled.');
    expect(x.api.command).toHaveBeenCalledExactlyOnceWith(commandExample, expect.any(AbortSignal));
    expect(x.api.commandReceipt).toHaveBeenCalledOnce();
    expect(x.dependencies.command).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review processing change' })).toBeNull());
    const copy = screen.getByRole('region', { name: 'Account text processing' }).textContent;
    expect(copy).not.toContain(KEY); expect(copy).not.toContain(viewExample.profile.profile_sha256);
    expect(copy).not.toContain('example-installation'); expect(copy).not.toContain('capsule');
  });

  it('keeps an unknown original command through refresh and retries only its exact retained meaning', async () => {
    const x = fixture(); x.api.retain.mockRejectedValueOnce(new TextAnalysisApiError('unknown'));
    render(x.view()); const dialog = await review();
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm processing change' }));
    await screen.findByRole('button', { name: 'Retry exact command' });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review processing change' })).toBeNull());
    await readFeedback(/The command outcome is unknown/);
    const original = clone(x.api.retain.mock.calls[0][0]);
    expect(x.api.command).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check processing status' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Retry exact command' }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole('button', { name: 'Review enablement' }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.querySelector('[data-journey-state="textanalysis.processing_recovery"]')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact command' }));
    await screen.findByText('Processing: enabled.');
    expect(x.api.retain.mock.calls.map(([intent]) => intent)).toEqual([original, original]);
    expect(x.api.command).toHaveBeenCalledOnce();
  });

  it.each(['account', 'same_account_session', 'role', 'disconnect'] as const)('clears processing review when %s changes', async (change) => {
    const x = fixture(), rendered = render(x.view()); await review();
    act(() => {
      if (change === 'account') connect(x.transport, 'other-account', '2');
      if (change === 'same_account_session') connect(x.transport, ACCOUNT, '2');
      if (change === 'role') useUserStore.getState().actions.setUserRole('operator');
      if (change === 'disconnect') x.transport.markDisconnected();
    });
    if (change === 'account') rendered.rerender(x.view('other-account'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review processing change' })).toBeNull());
    if (change === 'disconnect') {
      await readFeedback(/Connect with an authorized creator session/);
      expect(screen.queryByRole('combobox', { name: 'Approved processing profile' })).toBeNull();
    }
    expect(x.api.retain).not.toHaveBeenCalled(); expect(x.api.command).not.toHaveBeenCalled();
  });

  it.each(['account', 'same_account_session', 'disconnect'] as const)('discards a late acknowledgement after %s changes', async (change) => {
    const x = fixture(), acknowledgement = deferred<ClientIntentRecord>(); x.api.retain.mockReturnValue(acknowledgement.promise);
    const rendered = render(x.view()), dialog = await review();
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm processing change' }));
    await waitFor(() => expect(x.api.retain).toHaveBeenCalledOnce());
    const [intent, signal] = x.api.retain.mock.calls[0];
    act(() => {
      if (change === 'disconnect') x.transport.markDisconnected();
      else connect(x.transport, change === 'account' ? 'other-account' : ACCOUNT, '2');
    });
    if (change === 'account') rendered.rerender(x.view('other-account'));
    await act(async () => acknowledgement.resolve(await retained(intent)));
    expect(signal?.aborted).toBe(true); expect(x.api.command).not.toHaveBeenCalled();
    expect(screen.queryByText('Processing: enabled.')).toBeNull();
  });

  it('retains one feedback region through busy, unavailable and ready responses', async () => {
    const x = fixture(), first = deferred<EnrollmentView>(), next = deferred<EnrollmentView>();
    x.api.current.mockReturnValueOnce(first.promise);
    render(x.view());
    await readFeedback('Checking account processing…');
    const region = feedbackRegion(), classes = region.className;
    await act(async () => first.resolve(parseEnrollmentView(viewExample, ACCOUNT)));
    await screen.findByText('Processing: enabled.');
    expect(feedbackRegion()).toBe(region);
    expect(feedbackRegion().className).toBe(classes);
    x.api.current.mockRejectedValueOnce(new TextAnalysisApiError('unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Check processing status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check processing status' }).hasAttribute('disabled')).toBe(false));
    await readFeedback(/Processing management is unavailable for this session/);
    expect(feedbackRegion()).toBe(region);
    expect(feedbackRegion().className).toBe(classes);
    x.api.current.mockReturnValueOnce(next.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Check processing status' }));
    await readFeedback('Checking account processing…');
    expect(feedbackRegion()).toBe(region);
    await act(async () => next.resolve(parseEnrollmentView(viewExample, ACCOUNT)));
    await screen.findByText('Processing: enabled.');
    expect(feedbackRegion()).toBe(region);
    expect(document.querySelectorAll('[data-reserved-region="processing-feedback"]')).toHaveLength(1);
    expect(x.api.retain).not.toHaveBeenCalled(); expect(x.api.command).not.toHaveBeenCalled();
  });

  it('keeps complete combined feedback in details and clears it on disconnect', async () => {
    const x = fixture(); x.api.retain.mockRejectedValueOnce(new TextAnalysisApiError('unknown'));
    render(x.view()); const dialog = await review();
    fireEvent.click(dialog.getByRole('button', { name: 'Confirm processing change' }));
    await screen.findByRole('button', { name: 'Retry exact command' });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review processing change' })).toBeNull());
    await readFeedback(/The command outcome is unknown/);
    const original = x.api.retain.mock.calls[0][0];
    if (original.kind !== 'enrollment_command') throw new Error('Unexpected retained request');
    const changed = await retained({ ...original, body: { ...original.body, expected_mode_version: 1 } });
    x.journal.set(`${changed.kind}:${changed.key}`, changed);
    x.api.retainedList.mockResolvedValueOnce({ items: [changed], next_after: null });
    x.api.current.mockRejectedValueOnce(new TextAnalysisApiError('unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Check processing status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check processing status' }).hasAttribute('disabled')).toBe(false));
    fireEvent.click(await screen.findByRole('button', { name: 'Show details' }));
    const details = await screen.findByRole('dialog', { name: 'Details' });
    expect(details.textContent).toContain('Processing management is unavailable for this session. Saved results have separate read access.');
    expect(details.textContent).toContain('The original request meaning could not be verified. New processing changes are unavailable.');
    expect(x.api.command).not.toHaveBeenCalled();
    act(() => x.transport.markDisconnected());
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Details' })).toBeNull());
    expect(feedbackRegion().textContent).not.toContain('The original request meaning could not be verified');
    expect(screen.queryByText('Processing: enabled.')).toBeNull();
  });

  it('keeps management unavailable when the original-owner view cannot be read', async () => {
    const x = fixture(); x.api.current.mockRejectedValue(new TextAnalysisApiError('conflict'));
    render(x.view());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check processing status' }).hasAttribute('disabled')).toBe(false));
    await readFeedback(/Processing management is unavailable for this session/);
    expect(screen.queryByText('Processing: not enrolled.')).toBeNull();
    expect(x.api.retain).not.toHaveBeenCalled(); expect(x.api.command).not.toHaveBeenCalled();
  });
});
