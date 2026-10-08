'use strict';

const crypto = require('node:crypto');
const templates = require('./templates.cjs');
const MAX_BYTES = 4 * 1024 * 1024;
const TIMEOUT_MS = 25000;
const MAX_RETRIES = 3;

class SafeError extends Error {
  constructor(code, serverStatus = null) {
    super(code);
    this.code = code;
    this.serverStatus = serverStatus;
  }
}

function attr(attributes, name) {
  return attributes instanceof Map ? attributes.get(name) : attributes && attributes[name];
}

function retryDelay(value) {
  if (typeof value !== 'string') return null;
  const match = /^(?:(\d+)\.)?(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?$/.exec(value);
  if (!match || Number(match[3]) > 59 || Number(match[4]) > 59 || Number(match[2]) > 23) return null;
  const milliseconds = Number(match[1] || 0) * 86400000 + Number(match[2]) * 3600000 +
    Number(match[3]) * 60000 + Number(match[4]) * 1000 + Number('0.' + (match[5] || '0')) * 1000;
  return Number.isSafeInteger(Math.ceil(milliseconds)) ? Math.ceil(milliseconds) : null;
}

function physical(scope, logical) {
  return crypto.createHash('sha256').update([scope.namespace, scope.generation, logical].join('\0')).digest('hex');
}

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

function integer(value, minimum = Number.MIN_SAFE_INTEGER, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new SafeError('invalid_request');
  return value;
}

function string(value, pattern, maximum = 256) {
  if (typeof value !== 'string' || value.length > maximum || !pattern.test(value)) throw new SafeError('invalid_request');
  return value;
}

function validateScope(scope) {
  if (!scope || Object.keys(scope).sort().join(',') !== 'account_ref,generation,namespace') throw new SafeError('invalid_request');
  string(scope.account_ref, /^a1:[a-f0-9]{64}$/);
  string(scope.generation, /^[a-f0-9]{64}$/);
  string(scope.namespace, /^[a-f0-9]{32}$/);
}

function boundedJson(value) {
  if (typeof value !== 'string' || Buffer.byteLength(value) > MAX_BYTES / 2) throw new SafeError('invalid_request');
  try { JSON.parse(value); } catch { throw new SafeError('invalid_request'); }
  return value;
}

function record(request) {
  const logical = string(request.logical_id, /^(header|(?:node|edge|enrichment|metric|conversation|observation):[a-z][1]:[a-f0-9]{64})$/);
  const label = string(request.label, /^[a-z_]{1,64}$/);
  const fields = request.fields;
  if (!fields || Object.keys(fields).some(key => !['record_index', 'sent_us', 'conversation_ref'].includes(key))) throw new SafeError('invalid_request');
  integer(fields.record_index, 0, 100000);
  if ('sent_us' in fields) integer(fields.sent_us);
  if ('conversation_ref' in fields) string(fields.conversation_ref, /^c1:[a-f0-9]{64}$/);
  const value = { logical_id: logical, label, record_json: boundedJson(request.record_json), fields };
  if (request.op === 'put_edge') {
    value.source_id = string(request.source_id, /^node:g1:[a-f0-9]{64}$/);
    value.target_id = string(request.target_id, /^node:g1:[a-f0-9]{64}$/);
    if (!logical.startsWith('edge:')) throw new SafeError('invalid_request');
  } else if (logical.startsWith('edge:')) throw new SafeError('invalid_request');
  return value;
}

function recoveredRecord(row, scope) {
  const value = JSON.parse(boundedJson(attr(row, 'transport')));
  const edge = Object.hasOwn(value, 'source_id');
  const item = record({ ...value, op: edge ? 'put_edge' : 'put_vertex' });
  const checks = { physical: physical(scope, item.logical_id), label: item.label,
    account: scope.account_ref, namespace: scope.namespace, generation: scope.generation,
    logical: item.logical_id, index: item.fields.record_index,
    sent: item.fields.sent_us ?? 0, conversation: item.fields.conversation_ref ?? '',
    source: edge ? physical(scope, item.source_id) : '', target: edge ? physical(scope, item.target_id) : '' };
  if (canonical(value) !== canonical(item) || Object.entries(checks).some(([key, expected]) => attr(row, key) !== expected)) {
    throw new SafeError('identity_conflict');
  }
  return item;
}

class Adapter {
  constructor(createClient, options = {}) {
    this.createClient = createClient;
    this.client = null;
    this.now = options.now || (() => performance.now());
    this.sleep = options.sleep || (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)));
    this.timeout = options.timeout || TIMEOUT_MS;
  }

  async close() {
    const client = this.client;
    this.client = null;
    if (client) await Promise.race([Promise.resolve(client.close()).catch(() => {}), this.sleep(250)]);
  }

  async submit(template, bindings, metrics) {
    const deadline = this.now() + this.timeout;
    for (let attempt = 0; ; attempt++) {
      const remaining = Math.floor(deadline - this.now());
      if (remaining <= 0) throw new SafeError('timeout');
      let timer;
      try {
        if (!this.client) this.client = this.createClient();
        const client = this.client;
        const result = await Promise.race([
          client.open().then(() => client.submit(template, bindings, { evaluationTimeout: remaining })),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new SafeError('timeout')), remaining); }),
        ]);
        const charge = Number(attr(result.attributes, 'x-ms-total-request-charge') ?? attr(result.attributes, 'x-ms-request-charge') ?? 0);
        if (Number.isFinite(charge) && charge >= 0) metrics.request_charge += charge;
        return result.toArray();
      } catch (error) {
        const status = Number(attr(error.statusAttributes, 'x-ms-status-code') ?? error.statusCode);
        const charge = Number(attr(error.statusAttributes, 'x-ms-total-request-charge') ?? attr(error.statusAttributes, 'x-ms-request-charge') ?? 0);
        if (Number.isFinite(charge) && charge >= 0) metrics.request_charge += charge;
        const wait = retryDelay(attr(error.statusAttributes, 'x-ms-retry-after-ms'));
        await this.close();
        if (status === 429 && wait !== null && attempt < MAX_RETRIES && this.now() + wait < deadline) {
          metrics.retries++;
          await this.sleep(wait);
          continue;
        }
        if (error instanceof SafeError) throw error;
        throw new SafeError(status === 429 ? 'throttled' : status === 401 ? 'unauthorized' :
          status === 409 ? 'identity_conflict' : Number.isFinite(status) ? 'server_error' : 'transport',
          Number.isFinite(status) ? status : null);
      } finally {
        clearTimeout(timer);
      }
    }
  }

  async execute(request) {
    const metrics = { request_charge: 0, retries: 0 };
    integer(request.id, 1);
    if (request.op === 'close') {
      await this.close();
      return { id: request.id, status: 'ok', result: null, metrics };
    }
    validateScope(request.scope);
    const scope = request.scope;
    const bindings = { account: scope.account_ref, namespace: scope.namespace, generation: scope.generation };
    let template, expected = null;
    switch (request.op) {
      case 'put_vertex':
      case 'put_edge': {
        const item = record(request);
        expected = canonical(item);
        Object.assign(bindings, { logical: item.logical_id, physical: physical(scope, item.logical_id),
          label: item.label, transport: expected, recordIndex: item.fields.record_index,
          sentUs: item.fields.sent_us ?? 0, conversation: item.fields.conversation_ref ?? '' });
        if (request.op === 'put_edge') Object.assign(bindings, {
          source: physical(scope, item.source_id), target: physical(scope, item.target_id),
        });
        template = templates[request.op];
        break;
      }
      case 'read_records':
      case 'question_facts':
        bindings.after = string(request.after, /^(?:|[a-z_]+:[a-z]1:[a-f0-9]{64}|header)$/);
        bindings.limit = integer(request.limit, 1, 256);
        template = templates.read_records;
        if (request.op === 'question_facts') {
          if (!['no_later_creator_reply.v1', 'pricing_discussions.v1'].includes(request.question)) throw new SafeError('invalid_request');
          Object.assign(bindings, { start: integer(request.start_us), end: integer(request.end_us),
            cutoff: integer(request.cutoff_us), retention: integer(request.retention_us) });
          if (!(bindings.start < bindings.end && bindings.end <= bindings.cutoff)) throw new SafeError('invalid_request');
          const filtered = request.conversation_ref !== null;
          if (filtered) bindings.conversation = string(request.conversation_ref, /^c1:[a-f0-9]{64}$/);
          template = templates[request.question.split('.')[0] + (filtered ? '_filtered' : '')];
        }
        break;
      case 'publish':
        expected = boundedJson(request.manifest_json);
        bindings.manifest = expected;
        bindings.physical = physical(scope, 'manifest');
        template = templates.publish;
        break;
      case 'read_manifest':
        bindings.physical = physical(scope, 'manifest');
        template = templates.read_manifest;
        break;
      case 'cleanup':
        template = templates.cleanup;
        break;
      default:
        throw new SafeError('invalid_request');
    }
    const rows = await this.submit(template, bindings, metrics);
    let result = null;
    if (expected !== null) {
      if (rows.length !== 1 || rows[0] !== expected) throw new SafeError('identity_conflict');
    } else if (request.op === 'read_manifest') {
      if (rows.length > 1) throw new SafeError('identity_conflict');
      result = rows.length ? boundedJson(rows[0]) : null;
    } else if (request.op === 'read_records' || request.op === 'question_facts') {
      if (rows.length > bindings.limit) throw new SafeError('server_error');
      result = rows.map(row => recoveredRecord(row, scope));
    }
    return { id: request.id, status: 'ok', result, metrics };
  }
}

module.exports = { Adapter, SafeError, retryDelay, physical, recoveredRecord, MAX_BYTES };
