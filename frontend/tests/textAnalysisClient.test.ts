import { webcrypto } from 'node:crypto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  textAnalysisDigest, type ClientIntent, type ClientIntentRecord, type EnrollmentCommand,
  type EnrollmentCommandResult, type PreviewConfirmation, type PreviewOutcome, type PreviewPrepareRequest,
} from '../src/analytics/textAnalysisContract';
import { TextAnalysisApiError, type TextAnalysisApi } from '../src/services/textAnalysisApi';
import { createTextAnalysisClient, type TextAnalysisLifetime } from '../src/services/textAnalysisClient';

const KEY = '11111111-1111-4111-8111-111111111111';
const JOB = '22222222-2222-4222-8222-222222222222';
const CONTRIBUTION = '33333333-3333-4333-8333-333333333333';
const DIGEST = 'a'.repeat(64);
let commandMeaning = DIGEST;
let scopeMeaning = DIGEST;
const lifetime: TextAnalysisLifetime = { active: () => true };
const command = (): EnrollmentCommand => ({ schema_version: 'text-analysis-enrollment-command.v1', command_key: KEY,
  expected_enrollment_id: null, expected_mode_version: 0, action: 'enable', expected_profile_sha256: DIGEST });
const prepare = (): PreviewPrepareRequest => ({ schema_version: 'text-analysis-preview-prepare.v1', external_key: KEY, expected_profile_sha256: DIGEST });
const confirmation = (): PreviewConfirmation => ({ schema_version: 'text-analysis-confirmation.v1', external_key: KEY,
  job_id: JOB, contribution_id: CONTRIBUTION, spec_sha256: DIGEST, context_sha256: DIGEST, scope_sha256: scopeMeaning,
  parameter_sha256: DIGEST, input_inventory_sha256: DIGEST, reference_id: 'reference', compact_jws_sha256: DIGEST,
  preview_selection_policy: { policy_id: 'selection', revision: 'v1', sha256: DIGEST } });
const commandResult = (): EnrollmentCommandResult => ({ receipt: { command_key: KEY, meaning_sha256: commandMeaning,
  mode_version: 1, enrollment_id: JOB, state: 'enabled' }, current: { installation_id: 'installation', creator_account_id: 'account',
  principal_id: 'principal', mode_version: 1, enrollment_id: JOB, state: 'enabled', capsule_available: true } });
const previewResult = (): PreviewOutcome => ({ request: prepare(), receipt: { job_id: JOB, contribution_id: CONTRIBUTION,
  state: 'pending', terminal_reason: null }, scope: null, confirmation: null, detailed_scope_available: false, disclosure: null });
const previewScope = (): NonNullable<PreviewOutcome['scope']> => ({ schema_version: 'text-analysis-preview-scope.v1',
  cutoff: '2026-10-26T10:00:00.000Z', lower_exclusive: '2026-07-28T10:00:00.000Z', directions: 'both', message_cap: 32,
  source_count: 1, earliest: '2026-10-26T10:00:00.000Z', latest: '2026-10-26T10:00:00.000Z', inventory_sha256: DIGEST });
const preparedResult = (): PreviewOutcome => {
  const policy = { policy_id: 'selection', revision: 'v1', sha256: DIGEST };
  return { ...previewResult(), receipt: { job_id: JOB, contribution_id: CONTRIBUTION, state: 'prepared', terminal_reason: null },
    confirmation: confirmation(), detailed_scope_available: true,
    scope: previewScope(),
    disclosure: { profile_sha256: DIGEST, languages: ['en'], tasks: [{ task: 'message_topics', task_contract: 'message_topics.v1', taxonomy: policy,
      enabled_ids: ['category'], score_policy: { kind: 'none', semantics_id: 'none', calibration: 'not_calibrated', calibration_sha256: null } }],
    presentation: policy, selection_policy: policy, output_profile: 'urn:bridge-clean:text-analysis-preview-output-policy:v1', output_version: 1 } };
};
const commandIntent = (): ClientIntent => ({ schema_version: 'text-analysis-client-intent.v1', kind: 'enrollment_command', key: KEY, body: command() });
const prepareIntent = (): ClientIntent => ({ schema_version: 'text-analysis-client-intent.v1', kind: 'preview_prepare', key: KEY, body: prepare() });
const confirmIntent = (): ClientIntent => ({ schema_version: 'text-analysis-client-intent.v1', kind: 'preview_confirm', key: KEY,
  body: { external_key: KEY, confirmation: confirmation() } });
async function record(intent: ClientIntent): Promise<ClientIntentRecord> {
  return { ...intent, schema_version: 'text-analysis-client-intent-record.v1',
    body_sha256: await textAnalysisDigest(intent.body), retained_at: '2026-10-26T10:00:00.000Z' };
}

function apiFixture() {
  return {
    current: vi.fn<TextAnalysisApi['current']>(), command: vi.fn<TextAnalysisApi['command']>().mockImplementation(async (body) => {
      const result = commandResult(); result.receipt.meaning_sha256 = await textAnalysisDigest(body); return result;
    }),
    commandReceipt: vi.fn<TextAnalysisApi['commandReceipt']>().mockResolvedValue(null),
    prepare: vi.fn<TextAnalysisApi['prepare']>().mockResolvedValue(previewResult()),
    outcome: vi.fn<TextAnalysisApi['outcome']>().mockResolvedValue(null),
    confirm: vi.fn<TextAnalysisApi['confirm']>().mockResolvedValue(previewResult()),
    savedList: vi.fn<TextAnalysisApi['savedList']>(), saved: vi.fn<TextAnalysisApi['saved']>(),
    retain: vi.fn<TextAnalysisApi['retain']>().mockImplementation((intent) => record(intent)),
    retained: vi.fn<TextAnalysisApi['retained']>().mockResolvedValue(null),
    retainedList: vi.fn<TextAnalysisApi['retainedList']>().mockResolvedValue({ items: [], next_after: null }),
    evidence: vi.fn<TextAnalysisApi['evidence']>(),
  } satisfies TextAnalysisApi;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(async () => {
  vi.stubGlobal('crypto', webcrypto);
  commandMeaning = await textAnalysisDigest(command()); scopeMeaning = await textAnalysisDigest(previewScope());
});
afterEach(() => vi.unstubAllGlobals());

describe('Text analysis durable dispatch', () => {
  it('awaits an exact durable acknowledgement and noncreating domain read before posting', async () => {
    const api = apiFixture(), ack = deferred<ClientIntentRecord>();
    const events: string[] = [];
    api.retained.mockImplementation(async () => { events.push('journal-read'); return null; });
    api.retain.mockImplementation(() => { events.push('retain'); return ack.promise; });
    api.commandReceipt.mockImplementation(async () => { events.push('domain-read'); return null; });
    api.command.mockImplementation(async () => { events.push('post'); return commandResult(); });
    const input = command(), client = createTextAnalysisClient(api);
    const pending = client.command(input, lifetime);
    await vi.waitFor(() => expect(api.retain).toHaveBeenCalledOnce());
    expect(api.command).not.toHaveBeenCalled();
    input.expected_profile_sha256 = 'b'.repeat(64);
    ack.resolve(await record(commandIntent()));
    expect((await pending).state).toBe('resolved');
    expect(events).toEqual(['journal-read', 'retain', 'domain-read', 'post']);
    expect(api.command.mock.calls[0][0]).toEqual(command());
    expect(client.api).toBe(api);
  });

  it.each(['enrollment_command', 'preview_prepare', 'preview_confirm'] as const)('retains and dispatches the exact %s body', async (kind) => {
    const api = apiFixture(), client = createTextAnalysisClient(api);
    if (kind === 'preview_confirm') api.outcome.mockResolvedValue(preparedResult());
    const controller = new AbortController(), scope = { active: () => true, signal: controller.signal };
    const result = kind === 'enrollment_command' ? await client.command(command(), scope)
      : kind === 'preview_prepare' ? await client.prepare(prepare(), scope) : await client.confirm(KEY, confirmation(), scope);
    const intent = kind === 'enrollment_command' ? commandIntent() : kind === 'preview_prepare' ? prepareIntent() : confirmIntent();
    expect(result.state).toBe('resolved');
    expect(api.retain).toHaveBeenCalledWith(intent, controller.signal);
    expect(api.command.mock.calls.length + api.prepare.mock.calls.length + api.confirm.mock.calls.length).toBe(1);
    if (kind === 'preview_confirm') expect(api.confirm).toHaveBeenCalledWith(KEY, confirmation(), controller.signal);
  });

  it('normalizes optional command fields before comparing the acknowledgement', async () => {
    const api = apiFixture(), input = { command_key: KEY, expected_enrollment_id: JOB, expected_mode_version: 1, action: 'pause' };
    const result = await createTextAnalysisClient(api).command(input as EnrollmentCommand, lifetime);
    expect(result.state).toBe('resolved');
    expect(api.retain.mock.calls[0][0].body).toEqual({ ...input, schema_version: 'text-analysis-enrollment-command.v1', expected_profile_sha256: null });
  });

  it.each(['body', 'hash', 'kind', 'key'] as const)('refuses a mismatched acknowledgement %s', async (fault) => {
    const api = apiFixture(), ack = await record(commandIntent());
    if (fault === 'body') {
      ack.body = { ...command(), expected_profile_sha256: 'b'.repeat(64) };
      ack.body_sha256 = await textAnalysisDigest(ack.body);
    }
    if (fault === 'hash') ack.body_sha256 = '0'.repeat(64);
    if (fault === 'kind') Object.assign(ack, await record(prepareIntent()));
    if (fault === 'key') ack.key = JOB;
    api.retain.mockResolvedValue(ack);
    const result = await createTextAnalysisClient(api).command(command(), lifetime);
    expect(result).toMatchObject({ state: 'unknown', stage: 'retention' });
    expect(api.command).not.toHaveBeenCalled();
    expect(api.commandReceipt).not.toHaveBeenCalled();
  });

  it('recovers a lost retain reply without dispatching until an explicit retry', async () => {
    const api = apiFixture(), retained = await record(commandIntent()), client = createTextAnalysisClient(api);
    api.retain.mockRejectedValue(new TextAnalysisApiError('unknown'));
    const result = await client.command(command(), lifetime);
    expect(result).toMatchObject({ state: 'unknown', stage: 'retention', intent: commandIntent(), record: null });
    expect(api.command).not.toHaveBeenCalled();
    api.retained.mockResolvedValue(retained);
    expect((await client.read({ kind: retained.kind, key: KEY }, lifetime)).state).toBe('absent');
    expect(api.command).not.toHaveBeenCalled();
    expect((await client.retry(commandIntent(), lifetime)).state).toBe('resolved');
    expect(api.retain).toHaveBeenCalledOnce();
    expect(api.command).toHaveBeenCalledExactlyOnceWith(command(), undefined);
  });

  it('allows explicit same-key re-retention after a noncreating journal 404', async () => {
    const api = apiFixture(), client = createTextAnalysisClient(api);
    api.retain.mockRejectedValueOnce(new TextAnalysisApiError('unknown'));
    expect((await client.command(command(), lifetime)).state).toBe('unknown');
    expect((await client.retry(commandIntent(), lifetime)).state).toBe('resolved');
    expect(api.retain).toHaveBeenCalledTimes(2);
    expect(api.retain.mock.calls.map(([intent]) => intent)).toEqual([commandIntent(), commandIntent()]);
    expect(api.commandReceipt).toHaveBeenCalledOnce();
    expect(api.command).toHaveBeenCalledOnce();
  });

  it('stops again if explicit re-retention has an unknown outcome', async () => {
    const api = apiFixture(); api.retain.mockRejectedValue(new TextAnalysisApiError('unknown'));
    expect(await createTextAnalysisClient(api).retry(commandIntent(), lifetime)).toMatchObject({ state: 'unknown', stage: 'retention' });
    expect(api.retain).toHaveBeenCalledOnce();
    expect(api.commandReceipt).not.toHaveBeenCalled();
    expect(api.command).not.toHaveBeenCalled();
  });

  it('reads the original domain result after a lost POST reply without reposting', async () => {
    const api = apiFixture(), client = createTextAnalysisClient(api);
    api.command.mockRejectedValue(new TextAnalysisApiError('unknown'));
    expect((await client.command(command(), lifetime)).state).toBe('unknown');
    api.retained.mockResolvedValue(await record(commandIntent()));
    api.commandReceipt.mockResolvedValue(commandResult());
    expect(await client.retry(commandIntent(), lifetime)).toMatchObject({ state: 'resolved', result: commandResult() });
    expect(api.command).toHaveBeenCalledOnce();
  });

  it('does not redispatch an existing journal entry through a new-operation method', async () => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(commandIntent()));
    expect((await createTextAnalysisClient(api).command(command(), lifetime)).state).toBe('absent');
    expect(api.retain).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
  });

  it.each(['unauthorized', 'conflict', 'unavailable', 'unknown'] as const)('does not treat %s journal or domain failures as absence', async (kind) => {
    const api = apiFixture(), client = createTextAnalysisClient(api), error = new TextAnalysisApiError(kind);
    api.retained.mockRejectedValueOnce(error);
    expect((await client.retry(commandIntent(), lifetime)).state).toBe('unknown');
    expect(api.retain).not.toHaveBeenCalled();
    api.retained.mockResolvedValue(await record(commandIntent())); api.commandReceipt.mockRejectedValue(error);
    expect((await client.retry(commandIntent(), lifetime)).state).toBe('unknown');
    expect(api.command).not.toHaveBeenCalled();
  });

  it('refuses retry with a different meaning under a retained key', async () => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(commandIntent()));
    const changed = commandIntent(); if (changed.kind === 'enrollment_command') changed.body.expected_profile_sha256 = 'b'.repeat(64);
    expect(await createTextAnalysisClient(api).retry(changed, lifetime)).toMatchObject({ state: 'unknown', stage: 'lookup' });
    expect(api.commandReceipt).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
  });

  it('refuses a receipt whose key matches but immutable command meaning differs', async () => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(commandIntent()));
    const result = commandResult(); result.receipt.meaning_sha256 = 'b'.repeat(64);
    api.commandReceipt.mockResolvedValue(result);
    expect(await createTextAnalysisClient(api).retry(commandIntent(), lifetime)).toMatchObject({ state: 'unknown', stage: 'lookup' });
    expect(api.command).not.toHaveBeenCalled();
  });
});

describe('Text analysis recovery and caller lifetime', () => {
  it('restores only one bounded journal page and performs noncreating reads', async () => {
    const api = apiFixture(), records = await Promise.all([record(commandIntent()), record(confirmIntent()), record(prepareIntent())]);
    const cursor = { kind: 'preview_prepare' as const, key: KEY };
    api.retainedList.mockResolvedValue({ items: records, next_after: cursor });
    api.commandReceipt.mockResolvedValue(commandResult()); api.outcome.mockResolvedValue(previewResult());
    const page = await createTextAnalysisClient(api).restore(undefined, lifetime);
    expect(page.items.map((item) => item.state)).toEqual(['resolved', 'resolved', 'resolved']);
    expect(page.next_after).toEqual(cursor); expect(api.retainedList).toHaveBeenCalledOnce();
    expect(api.retain).not.toHaveBeenCalled(); expect(api.command).not.toHaveBeenCalled();
    expect(api.prepare).not.toHaveBeenCalled(); expect(api.confirm).not.toHaveBeenCalled();
  });

  it('preserves a journal404 as unretained rather than inventing a request body', async () => {
    const api = apiFixture(), locator = { kind: 'preview_confirm' as const, key: KEY };
    expect(await createTextAnalysisClient(api).read(locator, lifetime)).toEqual({ state: 'unretained', locator, intent: null });
    expect(api.outcome).not.toHaveBeenCalled(); expect(api.retain).not.toHaveBeenCalled();
  });

  it('retains other recovery observations when one domain lookup is unavailable', async () => {
    const api = apiFixture(); api.retainedList.mockResolvedValue({ items: [await record(commandIntent()), await record(prepareIntent())], next_after: null });
    api.commandReceipt.mockRejectedValue(new TextAnalysisApiError('unavailable')); api.outcome.mockResolvedValue(previewResult());
    expect((await createTextAnalysisClient(api).restore(undefined, lifetime)).items.map((item) => item.state)).toEqual(['unknown', 'resolved']);
    expect(api.retain).not.toHaveBeenCalled();
  });

  it('rejects a confirmation belonging to another external key before any request', () => {
    const api = apiFixture();
    expect(() => createTextAnalysisClient(api).confirm(JOB, confirmation(), lifetime)).toThrow();
    expect(api.retained).not.toHaveBeenCalled(); expect(api.retain).not.toHaveBeenCalled();
  });

  it('refuses a domain result with substituted confirmation job identities', async () => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(confirmIntent()));
    const substituted = previewResult(); substituted.receipt.job_id = CONTRIBUTION;
    api.outcome.mockResolvedValue(substituted);
    expect(await createTextAnalysisClient(api).retry(confirmIntent(), lifetime)).toMatchObject({ state: 'unknown', stage: 'lookup' });
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it('recovers a prepared confirmation without executing it, then permits explicit exact retry', async () => {
    const api = apiFixture(), retained = await record(confirmIntent());
    api.retained.mockResolvedValue(retained); api.outcome.mockResolvedValue(preparedResult());
    const client = createTextAnalysisClient(api);
    expect(await client.read({ kind: 'preview_confirm', key: KEY }, lifetime)).toMatchObject({ state: 'confirmable', result: preparedResult() });
    expect((await client.confirm(KEY, confirmation(), lifetime)).state).toBe('confirmable');
    expect(api.confirm).not.toHaveBeenCalled();
    expect((await client.retry(confirmIntent(), lifetime)).state).toBe('resolved');
    expect(api.confirm).toHaveBeenCalledExactlyOnceWith(KEY, confirmation(), undefined);
  });

  it('rejects a structurally valid displayed scope whose canonical hash differs from the original confirmation', async () => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(confirmIntent()));
    const outcome = preparedResult(); if (outcome.scope) outcome.scope.source_count = 2;
    api.outcome.mockResolvedValue(outcome);
    expect(await createTextAnalysisClient(api).retry(confirmIntent(), lifetime)).toMatchObject({ state: 'unknown', stage: 'lookup' });
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it.each(['missing', 'no_details', 'no_confirmation', 'changed_confirmation'] as const)('does not confirm a %s original preview', async (fault) => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(confirmIntent()));
    if (fault !== 'missing') {
      const outcome = preparedResult();
      if (fault === 'no_details') { outcome.detailed_scope_available = false; outcome.scope = null; outcome.disclosure = null; outcome.confirmation = null; }
      if (fault === 'no_confirmation') outcome.confirmation = null;
      if (fault === 'changed_confirmation' && outcome.confirmation) outcome.confirmation.context_sha256 = 'b'.repeat(64);
      api.outcome.mockResolvedValue(outcome);
    }
    expect(await createTextAnalysisClient(api).retry(confirmIntent(), lifetime)).toMatchObject({ state: 'unknown', stage: 'lookup' });
    expect(api.confirm).not.toHaveBeenCalled(); expect(api.retain).not.toHaveBeenCalled();
  });

  it.each(['pending', 'running', 'committed', 'failed', 'result_deleted'] as const)('never confirms again when original state is %s', async (state) => {
    const api = apiFixture(); api.retained.mockResolvedValue(await record(confirmIntent()));
    const outcome = previewResult(); outcome.receipt.state = state; api.outcome.mockResolvedValue(outcome);
    expect((await createTextAnalysisClient(api).retry(confirmIntent(), lifetime)).state).toBe('resolved');
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it('does not retain when inactive before the first request', async () => {
    const api = apiFixture();
    await expect(createTextAnalysisClient(api).command(command(), { active: () => false })).rejects.toMatchObject({ name: 'AbortError' });
    expect(api.retained).not.toHaveBeenCalled(); expect(api.retain).not.toHaveBeenCalled();
  });

  it.each(['journal', 'ack', 'domain'] as const)('stops after scope changes while awaiting %s', async (phase) => {
    const api = apiFixture(); let alive = true;
    const scope = { active: () => alive }, retained = await record(commandIntent());
    const journal = deferred<ClientIntentRecord | null>(), ack = deferred<ClientIntentRecord>(), domain = deferred<EnrollmentCommandResult | null>();
    if (phase === 'journal') api.retained.mockReturnValue(journal.promise);
    if (phase === 'ack') api.retain.mockReturnValue(ack.promise);
    if (phase === 'domain') api.commandReceipt.mockReturnValue(domain.promise);
    const pending = createTextAnalysisClient(api).command(command(), scope);
    if (phase === 'ack') await vi.waitFor(() => expect(api.retain).toHaveBeenCalledOnce());
    if (phase === 'domain') await vi.waitFor(() => expect(api.commandReceipt).toHaveBeenCalledOnce());
    alive = false;
    if (phase === 'journal') journal.resolve(null);
    if (phase === 'ack') ack.resolve(retained);
    if (phase === 'domain') domain.resolve(null);
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(api.command).not.toHaveBeenCalled();
    if (phase === 'journal') expect(api.retain).not.toHaveBeenCalled();
  });

  it('does not publish a late POST response after abort', async () => {
    const api = apiFixture(), response = deferred<EnrollmentCommandResult>(), controller = new AbortController();
    api.command.mockReturnValue(response.promise);
    const pending = createTextAnalysisClient(api).command(command(), { active: () => true, signal: controller.signal });
    await vi.waitFor(() => expect(api.command).toHaveBeenCalledOnce());
    controller.abort(); response.resolve(commandResult());
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('stops a restore page before the next lookup after a scope change', async () => {
    const api = apiFixture(), response = deferred<EnrollmentCommandResult | null>(); let alive = true;
    api.retainedList.mockResolvedValue({ items: [await record(commandIntent()), await record(prepareIntent())], next_after: null });
    api.commandReceipt.mockReturnValue(response.promise);
    const pending = createTextAnalysisClient(api).restore(undefined, { active: () => alive });
    await vi.waitFor(() => expect(api.commandReceipt).toHaveBeenCalledOnce());
    alive = false; response.resolve(commandResult());
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' }); expect(api.outcome).not.toHaveBeenCalled();
  });
});
