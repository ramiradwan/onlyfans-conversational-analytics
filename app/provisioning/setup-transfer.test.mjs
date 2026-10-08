import assert from 'node:assert/strict';
import test from 'node:test';
import { continueReceivingTransfer } from './provisioning.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const journeyId = '11111111-1111-4111-8111-111111111111';
const hosted = 'https://setup.example.test/public/onboarding';
const request = {
  profile: 'urn:bridge-clean:onboarding-transfer:v1', purpose: 'resume-onboarding',
  operation_id: '019a1fa1-0000-7000-8000-000000000001', setup_code: '0123456789AB',
  destination: { kind: 'desktop', destination_id: 'desktop-1',
    public_key: { kty: 'EC', crv: 'P-256', x: 'a'.repeat(43), y: 'b'.repeat(43) } },
};
const challenge = { profile: 'urn:bridge-clean:onboarding-proof:v1', challenge: 'c'.repeat(43),
  request_digest: 'd'.repeat(64), expires_at: '2026-10-08T12:01:00Z' };
const codeContext = { state: 'code_entry', journey_id: journeyId, setup_code: request.setup_code, csrf_token: 'local-only' };
const proofContext = { state: 'proof', journey_id: journeyId, request, challenge, csrf_token: 'local-only', hosted_return_url: hosted };
const prepared = { journey_id: journeyId, request, hosted_start_url: `${hosted}/receive` };
const signed = { challenge: challenge.challenge, signature: 's'.repeat(86) };

function element() { return { children: [], append(value) { this.children.push(value); }, submit() { this.submitted = true; } }; }
function setup(values) {
  const calls = []; const statuses = []; const document = { body: element(), createElement: element };
  return { calls, statuses, document, options: { document, journeyId, registeredHostedUrl: hosted,
    loadParser: async () => ({ parseOnboardingJson }), onStatus: (value) => statuses.push(value),
    fetch: async (path, options) => {
      calls.push({ path, options });
      return new Response(JSON.stringify(values.shift()), { headers: { 'content-type': 'application/json' } });
    } } };
}

test('code relay prepares once with local authority and posts only public workflow data in the same tab', async () => {
  const run = setup([codeContext, prepared]);
  assert.equal(await continueReceivingTransfer(run.options), 'submitted');
  assert.deepEqual(run.calls.map((v) => [v.path, v.options.method]), [
    ['/api/v1/provisioning/setup-transfer/context', 'GET'], ['/api/v1/provisioning/setup-transfer/prepare', 'POST'],
  ]);
  for (const { options } of run.calls) {
    assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
    assert.equal(options.cache, 'no-store'); assert.equal(options.headers['X-Onboarding-Journey'], journeyId);
  }
  assert.equal(run.calls[1].options.headers['X-CSRF-Token'], 'local-only');
  assert.deepEqual(JSON.parse(run.calls[1].options.body), { setup_code: request.setup_code });
  const form = run.document.body.children[0];
  assert.equal(form.method, 'post'); assert.equal(form.target, '_self'); assert.equal(form.submitted, true);
  assert.equal(form.action, `${hosted}/receive#journey=${journeyId}`);
  assert.deepEqual(form.children.map((v) => [v.name, v.value]), [['journey_id', journeyId], ['request', JSON.stringify(request)]]);
  assert.ok(!JSON.stringify(form).includes('local-only'));
});

test('proof relay signs the exact local request once and carries no local session or CSRF to hosted', async () => {
  const run = setup([proofContext, signed]);
  assert.equal(await continueReceivingTransfer(run.options), 'submitted');
  assert.equal(run.calls[1].path, '/api/v1/provisioning/setup-transfer/sign');
  assert.deepEqual(JSON.parse(run.calls[1].options.body), { request, challenge });
  const form = run.document.body.children[0];
  assert.equal(form.action, `${hosted}#journey=${journeyId}`);
  assert.deepEqual(form.children.map((v) => [v.name, v.value]), [
    ['journey_id', journeyId], ['request', JSON.stringify(request)], ['proof', JSON.stringify(signed)],
  ]);
  assert.ok(!JSON.stringify(form).includes('local-only'));
});

test('no transfer context leaves normal setup available without a navigation', async () => {
  const run = setup([{ state: 'none' }]);
  assert.equal(await continueReceivingTransfer(run.options), 'none');
  assert.equal(run.calls.length, 1); assert.equal(run.document.body.children.length, 0);
});

test('unknown proof handling offers a return without signing or replaying the operation', async () => {
  const run = setup([{ state: 'unconfirmed', journey_id: journeyId, hosted_return_url: hosted }]);
  let recovery, destination;
  run.document.defaultView = { location: { assign(value) { destination = value; } } };
  run.options.onRecovery = (label, action) => { recovery = { label, action }; };
  assert.equal(await continueReceivingTransfer(run.options), 'recovery');
  assert.equal(run.calls.length, 1); assert.equal(destination, undefined);
  assert.equal(recovery.label, 'Return to setup');
  recovery.action();
  assert.equal(destination, `${hosted}#journey=${journeyId}`);
  assert.equal(run.document.body.children.length, 0);
});

test('a committed continuation with a lost reply is forwarded only after an explicit action', async () => {
  const result = { state: 'waiting', journey_id: journeyId, handoff_reference: 'h'.repeat(43),
    hosted_start_url: `${hosted}/start`, continuation_reference: 'r'.repeat(43) };
  const run = setup([{ state: 'continue_ready', journey_id: journeyId, result }]);
  let recovery;
  run.options.onRecovery = (label, action) => { recovery = { label, action }; };
  assert.equal(await continueReceivingTransfer(run.options), 'recovery');
  assert.equal(run.calls.length, 1); assert.equal(run.document.body.children.length, 0);
  assert.equal(recovery.label, 'Continue setup');
  recovery.action();
  assert.equal(run.calls.length, 1, 'read-only result recovery never replays /continue');
  assert.equal(run.document.body.children[0].action, `${hosted}/start`);
});

test('a received continuation prepares this computer and carries only its reference to the independent initial setup', async () => {
  const continuation = { profile: 'urn:bridge-clean:onboarding-continuation:v1', reference: 'r'.repeat(43),
    return_target: 'desktop-setup', expires_at: '2026-10-08T12:30:00Z' };
  const result = { state: 'waiting', journey_id: journeyId, handoff_reference: 'h'.repeat(43),
    hosted_start_url: `${hosted}/start`, continuation_reference: continuation.reference };
  const run = setup([{ state: 'continue', journey_id: journeyId, continuation, csrf_token: 'local-only' }, result]);
  assert.equal(await continueReceivingTransfer(run.options), 'submitted');
  assert.equal(run.calls[1].path, '/api/v1/provisioning/setup-transfer/continue');
  assert.equal(run.calls[1].options.headers['X-CSRF-Token'], 'local-only');
  assert.deepEqual(JSON.parse(run.calls[1].options.body), { continuation });
  const form = run.document.body.children[0];
  assert.equal(form.action, `${hosted}/start`);
  assert.deepEqual(form.children.map((v) => [v.name, v.value]), [
    ['journey_id', journeyId], ['handoff_reference', 'h'.repeat(43)], ['continuation_reference', 'r'.repeat(43)],
  ]);
  assert.ok(!JSON.stringify(form).includes('local-only'));
});

test('mismatched journey, extended payloads, changed destination and unknown outcomes never start a new setup', async () => {
  const cases = [
    [{ ...codeContext, journey_id: '22222222-2222-4222-8222-222222222222' }],
    [{ ...codeContext, secret: 'unexpected' }],
    [{ ...proofContext, hosted_return_url: 'https://other.example.test/public/onboarding' }],
    [codeContext, { ...prepared, hosted_start_url: `${hosted}/receive?token=bad` }],
    [codeContext, { ...prepared, request: { ...request, destination: { ...request.destination, kind: 'browser-extension' } } }],
    [proofContext, { ...signed, challenge: 'z'.repeat(43) }],
    [{ state: 'unknown' }],
  ];
  for (const values of cases) {
    const run = setup(values);
    assert.equal(await continueReceivingTransfer(run.options), 'unavailable');
    assert.equal(run.document.body.children.length, 0);
    assert.equal(run.statuses.at(-1), 'Setup could not be continued.');
    assert.ok(!run.calls.some((v) => v.path.includes('initial-handoff')));
  }
});

test('lost signing response is bounded and does not replay the mutation', async () => {
  const run = setup([proofContext]); const original = run.options.fetch; let signs = 0;
  run.options.fetch = (path, options) => path.endsWith('/context') ? original(path, options)
    : new Promise((resolve, reject) => {
      signs++; options.signal.addEventListener('abort', () => reject(new Error('lost response')), { once: true });
    });
  assert.equal(await continueReceivingTransfer({ ...run.options, timeoutMs: 5 }), 'unavailable');
  assert.equal(signs, 1); assert.equal(run.document.body.children.length, 0);
});

test('non-JSON and duplicate-key contexts never become a local signing request', async () => {
  for (const response of [new Response('{}'), new Response('{"state":"none","state":"proof"}', {
    headers: { 'content-type': 'application/json' },
  })]) {
    const run = setup([]); let calls = 0;
    run.options.fetch = async () => { calls++; return response; };
    assert.equal(await continueReceivingTransfer(run.options), 'unavailable');
    assert.equal(calls, 1); assert.equal(run.document.body.children.length, 0);
  }
});
