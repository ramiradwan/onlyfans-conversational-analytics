import type { TextAnalysisApi } from './textAnalysisApi';
import {
  clientIntent, parseClientIntentRecord, parseEnrollmentCommandResult, verifyPreviewOutcome,
  textAnalysisCanonicalJson, TextAnalysisContractError,
  type ClientIntent, type ClientIntentCursor, type ClientIntentRecord,
  type EnrollmentCommand, type EnrollmentCommandResult, type PreviewConfirmation,
  type PreviewOutcome, type PreviewPrepareRequest,
} from '../analytics/textAnalysisContract';

export interface TextAnalysisLifetime {
  active: () => boolean;
  signal?: AbortSignal;
}

type RetainedObservation = {
  locator: ClientIntentCursor;
  intent: ClientIntent;
  record: ClientIntentRecord;
};
export type TextAnalysisObservation =
  | (RetainedObservation & { state: 'resolved'; result: EnrollmentCommandResult | PreviewOutcome })
  | (RetainedObservation & { state: 'confirmable'; result: PreviewOutcome })
  | (RetainedObservation & { state: 'absent' })
  | { state: 'unretained'; locator: ClientIntentCursor; intent: ClientIntent | null }
  | { state: 'unknown'; locator: ClientIntentCursor; intent: ClientIntent | null;
      record: ClientIntentRecord | null; stage: 'retention' | 'lookup' | 'dispatch'; error: unknown };

export interface TextAnalysisRestorePage {
  items: TextAnalysisObservation[];
  next_after: ClientIntentCursor | null;
}

export interface TextAnalysisClient {
  readonly api: TextAnalysisApi;
  command(body: EnrollmentCommand, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation>;
  prepare(body: PreviewPrepareRequest, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation>;
  confirm(externalKey: string, confirmation: PreviewConfirmation, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation>;
  restore(cursor: ClientIntentCursor | undefined, lifetime: TextAnalysisLifetime): Promise<TextAnalysisRestorePage>;
  read(locator: ClientIntentCursor, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation>;
  retry(intent: ClientIntent, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation>;
}

function active(lifetime: TextAnalysisLifetime) {
  if (!lifetime.active() || lifetime.signal?.aborted) throw new DOMException('The request is no longer active.', 'AbortError');
}

async function guarded<T>(lifetime: TextAnalysisLifetime, action: () => Promise<T>): Promise<T> {
  active(lifetime);
  try { return await action(); } finally { active(lifetime); }
}

function frozen<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) frozen(child);
    Object.freeze(value);
  }
  return value;
}

function original(value: unknown): ClientIntent {
  return frozen(clientIntent.parse(value));
}

const locatorFor = (value: ClientIntent | ClientIntentCursor): ClientIntentCursor => ({ kind: value.kind, key: value.key });
function intentFor(record: ClientIntentRecord): ClientIntent {
  return original({ schema_version: 'text-analysis-client-intent.v1', kind: record.kind, key: record.key, body: record.body });
}
function sameMeaning(intent: ClientIntent, record: ClientIntentRecord) {
  if (record.kind !== intent.kind || record.key !== intent.key
    || textAnalysisCanonicalJson(record.body) !== textAnalysisCanonicalJson(intent.body)) throw new TextAnalysisContractError();
}

async function resultFor(intent: ClientIntent, record: ClientIntentRecord, result: EnrollmentCommandResult | PreviewOutcome, lifetime: TextAnalysisLifetime) {
  if (intent.kind === 'enrollment_command') {
    const command = parseEnrollmentCommandResult(result, intent.key);
    if (command.receipt.meaning_sha256 !== record.body_sha256) throw new TextAnalysisContractError();
    return command;
  }
  const outcome = await guarded(lifetime, () => verifyPreviewOutcome(result, intent.key, intent.kind === 'preview_prepare' ? intent.body : undefined)); active(lifetime);
  if (intent.kind === 'preview_confirm') {
    const confirmation = intent.body.confirmation;
    if (outcome.receipt.job_id !== confirmation.job_id || outcome.receipt.contribution_id !== confirmation.contribution_id
      || outcome.confirmation !== null && textAnalysisCanonicalJson(outcome.confirmation) !== textAnalysisCanonicalJson(confirmation)) {
      throw new TextAnalysisContractError();
    }
  }
  return outcome;
}

async function observedResult(intent: ClientIntent, record: ClientIntentRecord, value: EnrollmentCommandResult | PreviewOutcome, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation> {
  const result = await guarded(lifetime, () => resultFor(intent, record, value, lifetime)); active(lifetime);
  const retained = { locator: locatorFor(intent), intent, record };
  if (intent.kind === 'preview_confirm' && 'request' in result && result.receipt.state === 'prepared') {
    if (!result.detailed_scope_available || result.confirmation === null) throw new TextAnalysisContractError();
    return { ...retained, state: 'confirmable', result };
  }
  return { ...retained, state: 'resolved', result };
}

export function createTextAnalysisClient(api: TextAnalysisApi): TextAnalysisClient {
  async function verifiedRecord(value: ClientIntentRecord, expected: ClientIntent | ClientIntentCursor, lifetime: TextAnalysisLifetime) {
    const record = await guarded(lifetime, () => parseClientIntentRecord(value, expected)); active(lifetime);
    if ('body' in expected) sameMeaning(expected, record);
    return frozen(record);
  }

  async function domainRead(intent: ClientIntent, record: ClientIntentRecord, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation> {
    const locator = locatorFor(intent);
    try {
      const result = await guarded<EnrollmentCommandResult | PreviewOutcome | null>(lifetime, () => intent.kind === 'enrollment_command'
        ? api.commandReceipt(intent.key, lifetime.signal) : api.outcome(intent.key, lifetime.signal)); active(lifetime);
      if (result === null) {
        if (intent.kind === 'preview_confirm') throw new TextAnalysisContractError();
        return { state: 'absent', locator, intent, record };
      }
      const observation = await guarded(lifetime, () => observedResult(intent, record, result, lifetime)); active(lifetime);
      return observation;
    } catch (error) {
      active(lifetime);
      return { state: 'unknown', locator, intent, record, stage: 'lookup', error };
    }
  }

  async function dispatch(intent: ClientIntent, record: ClientIntentRecord, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation> {
    const locator = locatorFor(intent);
    try {
      const result = await guarded<EnrollmentCommandResult | PreviewOutcome>(lifetime, () => {
        if (intent.kind === 'enrollment_command') return api.command(intent.body, lifetime.signal);
        if (intent.kind === 'preview_prepare') return api.prepare(intent.body, lifetime.signal);
        return api.confirm(intent.key, intent.body.confirmation, lifetime.signal);
      }); active(lifetime);
      const observation = await guarded(lifetime, () => observedResult(intent, record, result, lifetime)); active(lifetime);
      return observation;
    } catch (error) {
      active(lifetime);
      return { state: 'unknown', locator, intent, record, stage: 'dispatch', error };
    }
  }

  async function retainAndRead(intent: ClientIntent, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation> {
    active(lifetime);
    const locator = locatorFor(intent);
    let record: ClientIntentRecord;
    try {
      const retained = await guarded(lifetime, () => api.retain(intent, lifetime.signal)); active(lifetime);
      record = await verifiedRecord(retained, intent, lifetime); active(lifetime);
    } catch (error) {
      active(lifetime);
      return { state: 'unknown', locator, intent, record: null, stage: 'retention', error };
    }
    const result = await domainRead(intent, record, lifetime); active(lifetime);
    return result;
  }

  async function submit(value: ClientIntent, lifetime: TextAnalysisLifetime): Promise<TextAnalysisObservation> {
    active(lifetime);
    const intent = original(value);
    const existing = await read(locatorFor(intent), lifetime, intent); active(lifetime);
    if (existing.state !== 'unretained') return existing;
    const observation = await retainAndRead(intent, lifetime); active(lifetime);
    if (observation.state !== 'absent' && observation.state !== 'confirmable') return observation;
    const result = await dispatch(intent, observation.record, lifetime); active(lifetime);
    return result;
  }

  async function read(value: ClientIntentCursor, lifetime: TextAnalysisLifetime, expected?: ClientIntent): Promise<TextAnalysisObservation> {
    active(lifetime);
    const locator = frozen({ ...value });
    let intent: ClientIntent | null = expected ?? null, record: ClientIntentRecord | null = null;
    try {
      const retained = await guarded(lifetime, () => api.retained(locator.kind, locator.key, lifetime.signal)); active(lifetime);
      if (retained === null) return { state: 'unretained', locator, intent };
      record = await verifiedRecord(retained, expected ?? locator, lifetime); active(lifetime);
      intent = intentFor(record);
      const result = await domainRead(intent, record, lifetime); active(lifetime);
      return result;
    } catch (error) {
      active(lifetime);
      return { state: 'unknown', locator, intent, record, stage: 'lookup', error };
    }
  }

  return {
    api,
    command: (body, lifetime) => submit(original({ schema_version: 'text-analysis-client-intent.v1', kind: 'enrollment_command', key: body.command_key, body }), lifetime),
    prepare: (body, lifetime) => submit(original({ schema_version: 'text-analysis-client-intent.v1', kind: 'preview_prepare', key: body.external_key, body }), lifetime),
    confirm: (externalKey, confirmation, lifetime) => submit(original({ schema_version: 'text-analysis-client-intent.v1', kind: 'preview_confirm', key: externalKey,
      body: { external_key: externalKey, confirmation } }), lifetime),
    read: (locator, lifetime) => read(locator, lifetime),
    async retry(value, lifetime) {
      active(lifetime);
      const intent = original(value);
      let observation = await read(locatorFor(intent), lifetime, intent); active(lifetime);
      if (observation.state === 'unretained') {
        observation = await retainAndRead(intent, lifetime); active(lifetime);
      }
      if (observation.state !== 'absent' && observation.state !== 'confirmable') return observation;
      const result = await dispatch(intent, observation.record, lifetime); active(lifetime);
      return result;
    },
    async restore(cursor, lifetime) {
      const page = await guarded(lifetime, () => api.retainedList(cursor, lifetime.signal)); active(lifetime);
      const items: TextAnalysisObservation[] = [];
      for (const value of page.items) {
        const record = await verifiedRecord(value, value, lifetime); active(lifetime);
        const observation = await domainRead(intentFor(record), record, lifetime); active(lifetime);
        items.push(observation);
      }
      active(lifetime);
      return { items, next_after: page.next_after };
    },
  };
}
