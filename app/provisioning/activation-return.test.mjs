import assert from 'node:assert/strict';
import test from 'node:test';
import { continueActivationReturn } from './provisioning.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const journeyId = '11111111-1111-4111-8111-111111111111';
const entryId = '22222222-2222-4222-8222-222222222222';
const reply = (state, entry_id = entryId) => new Response(JSON.stringify({ state, journey_id: journeyId, entry_id }),
  { headers: { 'Content-Type': 'application/json' } });
function fixture(responses, extra = {}) {
  const journal = new Map(), requests = [], statuses = [];
  const options = { journeyId, csrf: 'local-csrf', parse: parseOnboardingJson,
    storage: { getItem: (key) => journal.get(key) ?? null, setItem: (key, value) => journal.set(key, value) },
    onStatus: (value) => statuses.push(value), fetch: async (url, init) => {
      requests.push({ url, ...init }); const response = responses.shift();
      if (typeof response === 'function') return response();
      return response;
    }, ...extra };
  return { options, journal, requests, statuses };
}

test('automatic activation sends no code to the browser and binds POST to the observed entry', async () => {
  const value = fixture([reply('ready'), reply('checking')]);
  assert.equal(await continueActivationReturn(value.options), 'checking');
  assert.deepEqual(value.requests.map((request) => request.method), ['GET', 'POST']);
  assert.equal(value.requests[1].headers['X-Onboarding-Activation-Entry'], entryId);
  assert.equal(value.requests[1].headers['X-Provisioning-CSRF'], 'local-csrf');
  assert.equal(value.requests[1].headers['X-Onboarding-Journey'], journeyId);
  assert.equal(value.requests[1].body, '{}');
  assert.deepEqual([...value.journal], [[`ofca.activation.return.v1:${entryId}`, 'attempted']]);
});

test('lost reply reconciles once and reload cannot resubmit the same entry', async () => {
  const responses = [reply('ready'), () => { throw new Error('lost'); }, reply('checking'), reply('ready')];
  const value = fixture(responses);
  assert.equal(await continueActivationReturn(value.options), 'checking');
  assert.equal(await continueActivationReturn(value.options), 'unconfirmed');
  assert.deepEqual(value.requests.map((request) => request.method), ['GET', 'POST', 'GET', 'GET']);
});

test('a never-resolving activation POST is bounded and is not replayed', async () => {
  const value = fixture([reply('ready'), () => new Promise(() => {})], { timeoutMs: 5 });
  assert.equal(await continueActivationReturn(value.options), 'unconfirmed');
  assert.equal(value.requests.filter((request) => request.method === 'POST').length, 1);
});

test('a retired page cannot dispatch after its delayed read', async () => {
  let current = true;
  const value = fixture([() => { current = false; return reply('ready'); }], { current: () => current });
  assert.equal(await continueActivationReturn(value.options), 'retired');
  assert.equal(value.requests.length, 1);
});

for (const mutation of [
  { state: 'waiting', entry_id: null }, { state: 'ready', entry_id: null }, { state: 'checking', entry_id: null }, { state: 'none' },
  { state: 'active' }, { journey_id: entryId }, { extra: true },
]) test(`malformed activation context cannot mutate: ${JSON.stringify(mutation)}`, async () => {
  const value = fixture([new Response(JSON.stringify({ state: 'ready', journey_id: journeyId, entry_id: entryId, ...mutation }),
    { headers: { 'Content-Type': 'application/json' } })]);
  assert.equal(await continueActivationReturn(value.options), 'unconfirmed');
  assert.equal(value.requests.length, 1);
});

test('duplicate fields and unavailable storage both refuse automatic mutation', async () => {
  const duplicate = fixture([new Response(`{"state":"none","state":"ready","journey_id":"${journeyId}","entry_id":"${entryId}"}`,
    { headers: { 'Content-Type': 'application/json' } })]);
  assert.equal(await continueActivationReturn(duplicate.options), 'unconfirmed');
  const absentStorage = fixture([reply('ready')], { storage: null });
  assert.equal(await continueActivationReturn(absentStorage.options), 'unconfirmed');
  assert.equal(duplicate.requests.length, 1); assert.equal(absentStorage.requests.length, 1);
});

test('ordinary setup with no activation entry adds no action or status', async () => {
  const value = fixture([reply('none', null)]);
  assert.equal(await continueActivationReturn(value.options), 'none');
  assert.equal(value.requests.length, 1); assert.deepEqual(value.statuses, []);
});

test('pending native approval is read-only until a pushed update permits one dispatch', async () => {
  const value = fixture([reply('waiting'), reply('ready'), reply('checking'), reply('checking')]);
  assert.equal(await continueActivationReturn(value.options), 'waiting');
  assert.equal(value.requests.length, 1);
  assert.equal(value.journal.size, 0);
  assert.deepEqual(value.statuses, ['Waiting for setup…']);
  assert.equal(await continueActivationReturn(value.options), 'checking');
  assert.equal(await continueActivationReturn(value.options), 'checking');
  assert.deepEqual(value.requests.map((request) => request.method), ['GET', 'GET', 'POST', 'GET']);
  assert.equal(value.journal.size, 1);
});
