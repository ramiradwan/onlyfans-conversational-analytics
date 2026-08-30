import {
  LEGAL_ACTIVATION_SCHEMA_VERSION,
  LEGAL_INSTRUMENT_NAMES,
  presentedInstruments,
  validateLegalInstrumentBindings,
} from './legal-instruments.mjs';

export const ACTIVATION_EVIDENCE_DATABASE_NAME = 'ofca_legal_evidence_v1';
export const ACTIVATION_EVIDENCE_DATABASE_VERSION = 1;
export const ACTIVATION_EVIDENCE_STORE = 'records';
export const PRE_MODE_EVIDENCE_SCHEMA = 'ofca-pre-mode-legal-evidence/v1';
export const MODE_EVIDENCE_RECORD_SCHEMA = 'ofca-mode-legal-evidence/v1';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SEMVER = /^[0-9]+\.[0-9]+\.[0-9]+$/;
const SHA256 = /^[a-f0-9]{64}$/;
const PUBLIC_ROUTE = /^\/[a-z0-9_/-]+$/;
const LOCALE = /^[a-z]{2}(?:-[A-Z]{2})?$/;
const EVENT_TYPES = new Set([
  'initial_activation',
  'mode_upgrade',
  'terms_reacceptance',
  'reauthorization',
]);
const MODES = new Set(['preview', 'full']);
const HISTORICAL_EVENT_TYPES = new Set(['mode_upgrade', 'reauthorization']);

const clone = (value) => structuredClone(value);

function exactKeys(value, expected, label) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new TypeError(`${label} contains unexpected or missing fields`);
  }
}

function validTimestamp(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function validateUuid(value, label) {
  if (!UUID_V4.test(value)) throw new TypeError(`${label} must be an RFC 4122 UUIDv4`);
}

function validatePresentedInstrument(value, label) {
  exactKeys(value, ['version', 'rendered_sha256', 'public_url', 'locale'], label);
  if (!SEMVER.test(value.version)) throw new TypeError(`${label}.version is invalid`);
  if (!SHA256.test(value.rendered_sha256)) throw new TypeError(`${label}.rendered_sha256 is invalid`);
  if (!PUBLIC_ROUTE.test(value.public_url)) throw new TypeError(`${label}.public_url is invalid`);
  if (!LOCALE.test(value.locale)) throw new TypeError(`${label}.locale is invalid`);
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
  });
}

function transactionCompletion(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB transaction aborted'));
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB transaction failed'));
  });
}

function openEvidenceDatabase(indexedDb) {
  if (typeof indexedDb?.open !== 'function') throw new Error('Activation evidence requires IndexedDB');
  return new Promise((resolve, reject) => {
    const request = indexedDb.open(
      ACTIVATION_EVIDENCE_DATABASE_NAME,
      ACTIVATION_EVIDENCE_DATABASE_VERSION,
    );
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(ACTIVATION_EVIDENCE_STORE)) {
        const store = database.createObjectStore(ACTIVATION_EVIDENCE_STORE, {
          keyPath: 'record_key',
        });
        store.createIndex('event_id', 'event_id', { unique: true });
        store.createIndex('transaction_id', 'transaction_id');
        store.createIndex('record_type', 'record_type');
        store.createIndex('occurred_at', 'occurred_at');
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      database.onversionchange = () => database.close();
      resolve(database);
    };
    request.onerror = () => reject(
      request.error ?? new Error('Activation evidence database failed to open'),
    );
    request.onblocked = () => reject(new Error('Activation evidence database upgrade was blocked'));
  });
}

function sameInstrument(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function validatePreModeRecord(record, meaning) {
  exactKeys(
    record,
    [
      'schema',
      'record_key',
      'record_type',
      'event_id',
      'transaction_id',
      'legal_meaning',
      'action',
      'occurred_at',
      'software_version',
      'locale',
      'instrument',
    ],
    `${meaning} evidence record`,
  );
  if (record.schema !== PRE_MODE_EVIDENCE_SCHEMA || record.record_type !== 'pre_mode') {
    throw new TypeError(`${meaning} evidence schema is invalid`);
  }
  validateUuid(record.event_id, `${meaning}.event_id`);
  validateUuid(record.transaction_id, `${meaning}.transaction_id`);
  if (record.legal_meaning !== meaning) throw new TypeError(`${meaning} evidence meaning is invalid`);
  const expectedAction = meaning === 'terms' ? 'accepted' : 'acknowledged';
  if (record.action !== expectedAction) throw new TypeError(`${meaning} evidence action is invalid`);
  if (!validTimestamp(record.occurred_at)) throw new TypeError(`${meaning} evidence timestamp is invalid`);
  if (!SEMVER.test(record.software_version)) throw new TypeError(`${meaning} software version is invalid`);
  if (!LOCALE.test(record.locale)) throw new TypeError(`${meaning} locale is invalid`);
  return record;
}

export function validateActivationEnvelopeV2(envelope) {
  exactKeys(
    envelope,
    [
      'schema_version',
      'event_id',
      'event_type',
      'occurred_at',
      'software_version',
      'selected_mode',
      'locale',
      'actions',
      'presented_instruments',
    ],
    'activation evidence envelope',
  );
  if (envelope.schema_version !== LEGAL_ACTIVATION_SCHEMA_VERSION) {
    throw new TypeError('activation evidence schema_version must be 2.0');
  }
  validateUuid(envelope.event_id, 'activation evidence event_id');
  if (!EVENT_TYPES.has(envelope.event_type)) throw new TypeError('activation evidence event_type is invalid');
  if (!validTimestamp(envelope.occurred_at)) throw new TypeError('activation evidence occurred_at is invalid');
  if (!SEMVER.test(envelope.software_version)) throw new TypeError('activation evidence software_version is invalid');
  if (!MODES.has(envelope.selected_mode)) throw new TypeError('activation evidence selected_mode is invalid');
  if (!LOCALE.test(envelope.locale)) throw new TypeError('activation evidence locale is invalid');

  exactKeys(envelope.actions, ['terms', 'risk_disclosure', 'extension_data_handling'], 'actions');
  exactKeys(envelope.actions.terms, ['action', 'timestamp'], 'actions.terms');
  exactKeys(envelope.actions.risk_disclosure, ['action', 'timestamp'], 'actions.risk_disclosure');
  exactKeys(
    envelope.actions.extension_data_handling,
    ['action', 'timestamp'],
    'actions.extension_data_handling',
  );
  if (!new Set(['accepted', 'previously_accepted']).has(envelope.actions.terms.action)) {
    throw new TypeError('terms action is invalid');
  }
  if (!new Set(['acknowledged', 'previously_acknowledged']).has(envelope.actions.risk_disclosure.action)) {
    throw new TypeError('risk disclosure action is invalid');
  }
  if (!validTimestamp(envelope.actions.terms.timestamp)) throw new TypeError('terms timestamp is invalid');
  if (!validTimestamp(envelope.actions.risk_disclosure.timestamp)) throw new TypeError('risk timestamp is invalid');
  if (!validTimestamp(envelope.actions.extension_data_handling.timestamp)) {
    throw new TypeError('extension data-handling timestamp is invalid');
  }
  const expectedHandling = envelope.selected_mode === 'full'
    ? 'affirmatively_authorized'
    : 'preview_only';
  if (envelope.actions.extension_data_handling.action !== expectedHandling) {
    throw new TypeError(`selected_mode ${envelope.selected_mode} requires ${expectedHandling}`);
  }

  exactKeys(envelope.presented_instruments, LEGAL_INSTRUMENT_NAMES, 'presented_instruments');
  for (const name of LEGAL_INSTRUMENT_NAMES) {
    validatePresentedInstrument(
      envelope.presented_instruments[name],
      `presented_instruments.${name}`,
    );
  }
  return clone(envelope);
}

async function transactionGetByEventId(store, eventId) {
  const keys = await requestResult(store.index('event_id').getAllKeys(eventId));
  if (keys.length === 0) return null;
  if (keys.length !== 1) throw new Error(`Activation evidence event_id ${eventId} is not unique`);
  return requestResult(store.get(keys[0]));
}

export class ActivationEvidenceStore {
  constructor({
    indexedDb = globalThis.indexedDB,
    softwareVersion,
    now = () => new Date(),
    uuid = () => crypto.randomUUID(),
  }) {
    if (!SEMVER.test(softwareVersion)) {
      throw new TypeError('Activation evidence requires a semantic software version');
    }
    if (typeof now !== 'function' || typeof uuid !== 'function') {
      throw new TypeError('Activation evidence requires clock and UUID providers');
    }
    this.indexedDb = indexedDb;
    this.softwareVersion = softwareVersion;
    this.now = now;
    this.uuid = uuid;
    this.databasePromise = null;
  }

  async #database() {
    this.databasePromise ??= openEvidenceDatabase(this.indexedDb);
    return this.databasePromise;
  }

  async #readRecord(recordKey) {
    const database = await this.#database();
    const transaction = database.transaction([ACTIVATION_EVIDENCE_STORE], 'readonly');
    const completion = transactionCompletion(transaction);
    const value = await requestResult(
      transaction.objectStore(ACTIVATION_EVIDENCE_STORE).get(recordKey),
    );
    await completion;
    return value === undefined ? null : clone(value);
  }

  async #appendRecord(record) {
    const existing = await this.#readRecord(record.record_key);
    if (existing !== null) return existing;
    const database = await this.#database();
    const transaction = database.transaction([ACTIVATION_EVIDENCE_STORE], 'readwrite');
    const completion = transactionCompletion(transaction);
    const store = transaction.objectStore(ACTIVATION_EVIDENCE_STORE);
    try {
      if (typeof store.add === 'function') await requestResult(store.add(record));
      else await requestResult(store.put(record));
      await completion;
      return clone(record);
    } catch (error) {
      try {
        await completion;
      } catch (_completionError) {
        // Preserve the original write error.
      }
      const raced = await this.#readRecord(record.record_key);
      if (raced !== null) return raced;
      throw error;
    }
  }

  async #event(eventId) {
    validateUuid(eventId, 'eventId');
    const database = await this.#database();
    const transaction = database.transaction([ACTIVATION_EVIDENCE_STORE], 'readonly');
    const completion = transactionCompletion(transaction);
    const value = await transactionGetByEventId(
      transaction.objectStore(ACTIVATION_EVIDENCE_STORE),
      eventId,
    );
    await completion;
    return value === null ? null : clone(value);
  }

  async recordTermsAcceptance({ transactionId, bindings }) {
    validateUuid(transactionId, 'transactionId');
    const validatedBindings = validateLegalInstrumentBindings(bindings);
    const recordKey = `terms:${transactionId}`;
    const existing = await this.#readRecord(recordKey);
    if (existing !== null) {
      validatePreModeRecord(existing, 'terms');
      if (!sameInstrument(existing.instrument, validatedBindings.instruments.terms_of_service)) {
        throw new Error('Terms acceptance retry attempted to change the bound instrument');
      }
      return existing;
    }
    const eventId = this.uuid();
    validateUuid(eventId, 'terms event_id');
    return this.#appendRecord(Object.freeze({
      schema: PRE_MODE_EVIDENCE_SCHEMA,
      record_key: recordKey,
      record_type: 'pre_mode',
      event_id: eventId,
      transaction_id: transactionId,
      legal_meaning: 'terms',
      action: 'accepted',
      occurred_at: this.now().toISOString(),
      software_version: this.softwareVersion,
      locale: validatedBindings.instruments.terms_of_service.locale,
      instrument: clone(validatedBindings.instruments.terms_of_service),
    }));
  }

  async recordRiskAcknowledgment({ transactionId, bindings }) {
    validateUuid(transactionId, 'transactionId');
    const validatedBindings = validateLegalInstrumentBindings(bindings);
    const recordKey = `risk:${transactionId}`;
    const existing = await this.#readRecord(recordKey);
    if (existing !== null) {
      validatePreModeRecord(existing, 'risk_disclosure');
      if (!sameInstrument(existing.instrument, validatedBindings.instruments.risk_disclosure)) {
        throw new Error('Risk acknowledgment retry attempted to change the bound instrument');
      }
      return existing;
    }
    const eventId = this.uuid();
    validateUuid(eventId, 'risk event_id');
    return this.#appendRecord(Object.freeze({
      schema: PRE_MODE_EVIDENCE_SCHEMA,
      record_key: recordKey,
      record_type: 'pre_mode',
      event_id: eventId,
      transaction_id: transactionId,
      legal_meaning: 'risk_disclosure',
      action: 'acknowledged',
      occurred_at: this.now().toISOString(),
      software_version: this.softwareVersion,
      locale: validatedBindings.instruments.risk_disclosure.locale,
      instrument: clone(validatedBindings.instruments.risk_disclosure),
    }));
  }

  async recordModeChoice({
    transactionId,
    eventType,
    selectedMode,
    termsEventId,
    riskEventId,
    bindings,
  }) {
    validateUuid(transactionId, 'transactionId');
    if (!EVENT_TYPES.has(eventType)) throw new TypeError('eventType is invalid');
    if (!MODES.has(selectedMode)) throw new TypeError('selectedMode is invalid');
    const validatedBindings = validateLegalInstrumentBindings(bindings);
    const [terms, risk] = await Promise.all([
      this.#event(termsEventId),
      this.#event(riskEventId),
    ]);
    if (terms === null || risk === null) {
      throw new Error('Mode evidence requires persisted Terms and risk events');
    }
    validatePreModeRecord(terms, 'terms');
    validatePreModeRecord(risk, 'risk_disclosure');
    if (!sameInstrument(terms.instrument, validatedBindings.instruments.terms_of_service)) {
      throw new Error('Terms instrument changed; a new acceptance is required before mode choice');
    }
    if (!sameInstrument(risk.instrument, validatedBindings.instruments.risk_disclosure)) {
      throw new Error('Risk instrument changed; a new acknowledgment is required before mode choice');
    }

    const recordKey = `mode:${transactionId}:${eventType}:${selectedMode}`;
    const existing = await this.#readRecord(recordKey);
    if (existing !== null) return clone(existing);

    const occurredAt = this.now().toISOString();
    const eventId = this.uuid();
    validateUuid(eventId, 'mode event_id');
    const historical = HISTORICAL_EVENT_TYPES.has(eventType);
    const envelope = validateActivationEnvelopeV2({
      schema_version: LEGAL_ACTIVATION_SCHEMA_VERSION,
      event_id: eventId,
      event_type: eventType,
      occurred_at: occurredAt,
      software_version: this.softwareVersion,
      selected_mode: selectedMode,
      locale: terms.locale,
      actions: {
        terms: {
          action: historical ? 'previously_accepted' : terms.action,
          timestamp: terms.occurred_at,
        },
        risk_disclosure: {
          action: historical ? 'previously_acknowledged' : risk.action,
          timestamp: risk.occurred_at,
        },
        extension_data_handling: {
          action: selectedMode === 'full' ? 'affirmatively_authorized' : 'preview_only',
          timestamp: occurredAt,
        },
      },
      presented_instruments: presentedInstruments(validatedBindings),
    });
    return this.#appendRecord(Object.freeze({
      schema: MODE_EVIDENCE_RECORD_SCHEMA,
      record_key: recordKey,
      record_type: 'mode_envelope',
      event_id: eventId,
      transaction_id: transactionId,
      terms_event_id: terms.event_id,
      risk_event_id: risk.event_id,
      occurred_at: occurredAt,
      envelope,
    }));
  }

  async exportAuditTrail() {
    const database = await this.#database();
    const transaction = database.transaction([ACTIVATION_EVIDENCE_STORE], 'readonly');
    const completion = transactionCompletion(transaction);
    const records = await requestResult(
      transaction.objectStore(ACTIVATION_EVIDENCE_STORE).getAll(),
    );
    await completion;
    return records
      .map(clone)
      .sort((left, right) => (
        String(left.occurred_at).localeCompare(String(right.occurred_at))
        || String(left.event_id).localeCompare(String(right.event_id))
      ));
  }

  async latestModeRecord(selectedMode = null) {
    const records = (await this.exportAuditTrail()).filter((record) => (
      record.record_type === 'mode_envelope'
      && (selectedMode === null || record.envelope?.selected_mode === selectedMode)
    ));
    return records.at(-1) ?? null;
  }

  async modeEvidenceExists(selectedMode) {
    if (!MODES.has(selectedMode)) return false;
    return (await this.latestModeRecord(selectedMode)) !== null;
  }

  async event(eventId) {
    return this.#event(eventId);
  }
}
