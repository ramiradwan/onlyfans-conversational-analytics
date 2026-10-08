import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { resumeOnboarding } from './resume.js';
import { parseBrainOnboardingState } from './provisioning.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const journey = '11111111-1111-4111-8111-111111111111';
const snapshot = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)))
  .cases.find((item) => item.id === 'brain-snapshot').value;
const loadValidation = async () => [{ parseBrainOnboardingState }, { parseOnboardingJson }];
function response(value = snapshot, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: {
    'content-type': 'application/json', 'X-Onboarding-Capabilities': 'local-onboarding.v1',
  } });
}
function location(hash = `#journey=${journey}`) {
  return { hash, reloads: 0, reload() { this.reloads++; }, replace(value) { this.target = value; } };
}

for (const runtime of [false, true]) test(`returns to the same workspace only after an authenticated cookie read, runtime=${runtime}`, async () => {
  const page = location(); const calls = [];
  assert.equal(await resumeOnboarding({ location: page, status: {}, loadValidation,
    history: { replaceState(_state, _unused, value) { page.target = value; } }, fetch: async (path, options) => {
    calls.push(path); assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
    return runtime && path.includes('provisioning') ? response({}, 404) : response();
  } }), true);
  assert.equal(page.target, `${runtime ? '/' : '/provisioning'}#journey=${journey}`);
  assert.equal(calls.length, runtime ? 2 : 1);
  assert.equal(page.reloads, runtime ? 0 : 1);
});

test('expired, mismatched or unrecognized context never loops or navigates', async () => {
  for (const reply of [() => response({}, 401), () => response({}, 409),
    () => response({ ...snapshot, journey_id: '22222222-2222-4222-8222-222222222222' }),
    () => new Response('<h1>Not found</h1>', { headers: { 'content-type': 'text/html' } })]) {
    const page = location(); const status = {}; let calls = 0;
    assert.equal(await resumeOnboarding({ location: page, status, loadValidation, fetch: async () => { calls++; return reply(); } }), false);
    assert.equal(page.target, undefined); assert.equal(calls, 1);
    assert.equal(status.textContent, 'Open the desktop app to continue setup.');
  }
});

test('unknown reference syntax never triggers an authenticated read', async () => {
  assert.equal(await resumeOnboarding({ location: location('#journey=unknown&token=x'), status: {},
    fetch: () => assert.fail('Unexpected request') }), false);
});
