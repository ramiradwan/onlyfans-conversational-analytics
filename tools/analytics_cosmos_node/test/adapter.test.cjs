'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { Adapter, retryDelay, physical, recoveredRecord } = require('../adapter.cjs');
const templates = require('../templates.cjs');

const scope = { account_ref: 'a1:' + 'a'.repeat(64), namespace: 'b'.repeat(32), generation: 'c'.repeat(64) };
const result = (values, attributes = {}) => ({ attributes, toArray: () => values });
function client(submit) { return { open: async () => {}, close: async () => {}, submit }; }

test('pinned driver submits GraphSON v2 script bindings on Node 22', async () => {
  const gremlin = require('gremlin');
  assert.equal(require('gremlin/package.json').version, '3.4.13');
  const actual = new gremlin.driver.Client('wss://invalid.example/', {
    mimeType: 'application/vnd.gremlin-v2.0+json', connectOnStartup: false, rejectUnauthorized: true,
    authenticator: new gremlin.driver.auth.PlainTextSaslAuthenticator('synthetic', 'synthetic'),
  });
  let captured;
  actual._connection.submit = async (...args) => { captured = args; return result([]); };
  await actual.submit(templates.read_manifest, { account: scope.account_ref }, { evaluationTimeout: 100 });
  assert.equal(captured[1], 'eval');
  assert.equal(captured[2].accept, 'application/vnd.gremlin-v2.0+json');
  assert.deepEqual(captured[2].bindings, { account: scope.account_ref });
  assert.equal(captured[2].evaluationTimeout, 100);
  await actual.close();
});

test('TimeSpan retry delay retains fractional milliseconds', () => {
  assert.equal(retryDelay('00:00:03.9500000'), 3950);
  assert.equal(retryDelay('00:00:00.0000001'), 1);
  assert.equal(retryDelay('1.02:03:04.5'), 93784500);
  for (const invalid of [3950, '3950', '-00:00:01', '00:70:00', 'NaN']) assert.equal(retryDelay(invalid), null);
});

test('queries use fixed templates and separate bindings', async () => {
  const calls = [];
  const adapter = new Adapter(() => client(async (...args) => { calls.push(args); return result([]); }));
  await adapter.execute({ id: 1, op: 'question_facts', scope, after: '', limit: 5,
    question: 'no_later_creator_reply.v1', start_us: 10, end_us: 20, cutoff_us: 30,
    retention_us: 0, conversation_ref: null });
  assert.equal(calls[0][0], templates.no_later_creator_reply);
  assert.equal(calls[0][1].account, scope.account_ref);
  assert.equal(calls[0][1].cutoff, 30);
  assert.ok(!calls[0][0].includes(scope.account_ref));
  await adapter.close();
});

test('429 follows the server delay and exposes only selected metrics', async () => {
  let now = 0, requests = 0;
  const waits = [];
  const adapter = new Adapter(() => client(async () => {
    if (!requests++) throw { statusCode: 429, statusMessage: 'private server text',
      statusAttributes: { 'x-ms-retry-after-ms': '00:00:00.0100000', 'x-ms-request-charge': 1 } };
    return result([], new Map([['x-ms-total-request-charge', 2]]));
  }), { now: () => now, sleep: async ms => { waits.push(ms); now += ms; } });
  const response = await adapter.execute({ id: 1, op: 'read_manifest', scope });
  assert.equal(response.metrics.retries, 1);
  assert.equal(response.metrics.request_charge, 3);
  assert.ok(waits.includes(10));
  assert.ok(!JSON.stringify(response).includes('private'));
  await adapter.close();
});

test('unusable retry hints and authentication failures do not retry', async () => {
  for (const status of [401, 429]) {
    let requests = 0;
    const adapter = new Adapter(() => client(async () => { requests++; throw { statusCode: status }; }));
    await assert.rejects(adapter.execute({ id: 1, op: 'read_manifest', scope }));
    assert.equal(requests, 1);
  }
});

test('missing configuration reports not configured successfully', () => {
  const env = { ...process.env };
  for (const name of Object.keys(env)) if (name.startsWith('ANALYTICS_GREMLIN_')) delete env[name];
  const run = spawnSync(process.execPath, [path.join(__dirname, '..', 'runner.cjs')], { env, encoding: 'utf8' });
  assert.equal(run.status, 0);
  assert.equal(JSON.parse(run.stdout).status, 'not_configured');
  assert.equal(run.stderr, '');
});

test('configured invalid endpoint fails without exposing connection fields', () => {
  const env = { ...process.env, ANALYTICS_GREMLIN_ENDPOINT: 'synthetic-invalid-endpoint',
    ANALYTICS_GREMLIN_KEY: 'synthetic-secret-sentinel', ANALYTICS_GREMLIN_DATABASE: 'synthetic-database',
    ANALYTICS_GREMLIN_GRAPH: 'synthetic-graph' };
  const run = spawnSync(process.execPath, [path.join(__dirname, '..', 'runner.cjs')], { env, encoding: 'utf8' });
  assert.equal(run.status, 1);
  assert.equal(JSON.parse(run.stdout).status, 'error');
  for (const field of ['endpoint', 'secret', 'database', 'graph']) assert.ok(!run.stdout.includes('synthetic-' + field));
  assert.equal(run.stderr, '');
});

test('native edge endpoints and stored scalar indexes bind the opaque payload', () => {
  const edge = { logical_id: 'edge:g1:' + '1'.repeat(64), label: 'sent', fields: { record_index: 0 },
    record_json: '{"wide":{"$analytics_integer":"9223372036854775808"},"float":1.0,"null":null}',
    source_id: 'node:g1:' + '2'.repeat(64), target_id: 'node:g1:' + '3'.repeat(64) };
  const row = { transport: JSON.stringify(edge), physical: physical(scope, edge.logical_id),
    label: edge.label, account: scope.account_ref, namespace: scope.namespace, generation: scope.generation,
    logical: edge.logical_id, index: 0, sent: 0, conversation: '',
    source: physical(scope, edge.source_id), target: physical(scope, edge.target_id) };
  assert.deepEqual(recoveredRecord(row, scope), edge);
  for (const property of ['physical', 'label', 'account', 'namespace', 'generation', 'logical', 'index', 'sent', 'source', 'target']) {
    assert.throws(() => recoveredRecord({ ...row, [property]: 'altered' }, scope), /identity_conflict/);
  }
});
