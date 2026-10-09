import assert from 'node:assert/strict';
import test from 'node:test';
import { continueActivationReturn, createProvisioningController, parseStatusResponse, submitHostedContinuation } from './provisioning.js';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const journey = '11111111-1111-4111-8111-111111111111';
const hosted = 'https://setup.example/public/onboarding/setup';
const payload = { state: 'waiting', journey_id: journey, continuation_reference: 'a'.repeat(43),
  hosted_start_url: 'https://setup.example/public/onboarding/installation-continue' };
const pending = (state = 'new') => ({ state: 'provisioning_ready', stage: 'creator_approval_pending',
  association_request_id: 'request-1', creator_account_id: 'creator-1', context_kind: 'registered-continuation',
  continuation_state: state });
const settle = () => new Promise((resolve) => setImmediate(resolve));

function element() {
  return { dataset: {}, hidden: false, textContent: '', value: '', children: [], listeners: {}, attributes: {},
    append(child) { this.children.push(child); }, submit() { this.submitted = true; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; } };
}

function fixture({ prepare = async () => payload, state = 'new', storage = new Map(), continuationTimeoutMs = 10_000,
  activation } = {}) {
  const nodes = new Map(['#initial-browser-prerequisite', '#claim-heading', '#claim-step-description', '#finalize-step-description',
    '#transfer-recovery-action', '.progress-rail', '.intro'].map((key) => [key, element()]));
  nodes.set('#open-secure-setup', { ...element(), href: hosted });
  const lifecycle = {};
  const page = { location: { hash: `#journey=${journey}` },
    sessionStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    addEventListener(name, listener) { lifecycle[name] = listener; } };
  const document = { defaultView: page, hidden: false, body: element(), createElement: element, addEventListener() {},
    querySelector(selector) { return selector === 'main' ? { dataset: { provisioningCsrf: 'test-csrf',
      provisioningContextKind: 'registered-continuation', provisioningExtensionId: '' } } : nodes.get(selector) ?? null; } };
  const elements = Object.fromEntries(['status', 'identityStatus', 'claimForm', 'claimPackage', 'claimPackageValidation', 'claimPackageCount',
    'claimSubmit', 'claimActionHelp', 'refreshIdentity', 'confirmIdentity', 'acquireAssociation', 'finalizeProvisioning', 'finalizeActionHelp',
    'claimStep', 'identityStep', 'bindingStep', 'finalizeStep', 'claimStepState', 'identityStepState', 'bindingStepState', 'finalizeStepState']
    .map((key) => [key, element()]));
  elements.claimActionHelp = nodes.get('#claim-step-description');
  elements.finalizeActionHelp = nodes.get('#finalize-step-description');
  const calls = []; let push; let status = pending(state);
  const controller = createProvisioningController({ document, elements,
    continueActivation: activation ? continueActivationReturn : undefined,
    continuationTimeoutMs, loadContinuationParser: async () => ({ parseOnboardingJson }),
    continueTransfer: () => assert.fail('Registered setup cannot enter receiving'),
    sendExtensionMessage: () => assert.fail('Saved account cannot use a detected account hint'),
    connectExtension: () => assert.fail('Extension is optional for saved setup'),
    loadLocalWorkspace: async () => ({ createLocalWorkspaceOwner: () => ({ stop() {} }) }),
    connectOnboarding: async ({ onState }) => { push = onState; return { close() {} }; },
    fetch: async (path, options) => {
      calls.push({ path, options });
      let result;
      if (path.endsWith('/status')) result = status;
      else if (path.endsWith('/activation-return')) result = await activation(options);
      else if (path.endsWith('/installation-continuation')) result = options.method === 'POST' ? await prepare() : payload;
      else if (path.endsWith('/finalize')) result = { state: 'configured_restart' };
      else assert.fail(`Wrong dispatch: ${path}`);
      return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } });
    } });
  return { controller, document, elements, calls, lifecycle, storage, push: () => push({ epoch: journey, revision: calls.length }),
    status(value) { status = value; } };
}

test('closed continuation status rejects missing kind/state, extra fields and unknown states', () => {
  assert.deepEqual(parseStatusResponse(pending()), pending());
  for (const invalid of [{ ...pending(), context_kind: 'initial-enrollment' }, { ...pending(), continuation_state: 'unknown-state' },
    { ...pending(), token: 'forbidden' }, { ...pending(), context_kind: undefined }]) assert.equal(parseStatusResponse(invalid), null);
});

test('staged activation waits for an owner push and dispatches before automatic restart', async () => {
  const entry = '22222222-2222-4222-8222-222222222222';
  let ready = false;
  const f = fixture({ state: 'waiting', activation: async (options) => ({ journey_id: journey, entry_id: entry,
    state: options.method === 'POST' ? 'checking' : ready ? 'ready' : 'waiting' }) });
  await f.controller.start(); f.push(); await settle(); await settle();
  assert.equal(f.calls.filter((call) => call.options.method === 'POST').length, 0);
  assert.equal(f.document.body.children.length, 0);
  assert.equal(f.storage.size, 0);
  const waitingReads = f.calls.length;
  await settle(); await settle();
  assert.equal(f.calls.length, waitingReads, 'no polling while native approval is pending');
  ready = true;
  f.status({ ...pending('completed'), stage: 'finalization_ready' });
  f.push(); await settle(); await settle(); await settle();
  const posts = f.calls.filter((call) => call.options.method === 'POST');
  assert.deepEqual(posts.map((call) => call.path), ['/api/v1/onboarding/activation-return', '/api/v1/provisioning/finalize']);
  assert.equal(posts[0].options.headers['X-Onboarding-Activation-Entry'], entry);
  assert.equal(f.storage.get(`ofca.activation.return.v1:${entry}`), 'attempted');
});

test('a pushed activation read retired by pagehide cannot dispatch or finalize', async () => {
  let release;
  let reads = 0;
  const entry = '22222222-2222-4222-8222-222222222222';
  const f = fixture({ state: 'waiting', activation: async () => {
    reads += 1;
    if (reads === 1) return { journey_id: journey, entry_id: entry, state: 'waiting' };
    return new Promise((resolve) => { release = () => resolve({ journey_id: journey, entry_id: entry, state: 'ready' }); });
  } });
  await f.controller.start();
  f.status({ ...pending('completed'), stage: 'finalization_ready' });
  f.push(); await settle();
  f.lifecycle.pagehide(); release(); await settle(); await settle();
  assert.equal(f.calls.filter((call) => call.options.method === 'POST').length, 0);
  assert.equal(f.storage.size, 0);
});

test('hosted continuation uses only exact registered URL, journey and nonauthorizing reference', () => {
  const f = fixture();
  assert.equal(submitHostedContinuation({ document: f.document, payload, journeyId: journey, registeredHostedUrl: hosted }), true);
  const form = f.document.body.children[0];
  assert.equal(form.target, '_self');
  assert.deepEqual(form.children.map((input) => [input.name, input.value]), [['journey_id', journey], ['continuation_reference', 'a'.repeat(43)]]);
  for (const bad of [{ ...payload, intended_creator_id: 'other' }, { ...payload, handoff_reference: 'a'.repeat(43) },
    { ...payload, hosted_start_url: `${hosted}/start` }, { ...payload, hosted_start_url: `${payload.hosted_start_url}?token=bad` },
    { ...payload, continuation_reference: 'short' }]) {
    assert.equal(submitHostedContinuation({ document: f.document, payload: bad, journeyId: journey, registeredHostedUrl: hosted }), false);
  }
});

test('unique saved context automatically opens hosted sign-in without extension, identity lookup or extra Continue', async () => {
  const f = fixture(); await f.controller.start(); f.push(); await settle(); await settle();
  const posted = f.calls.filter((call) => call.options?.method === 'POST');
  assert.equal(posted.length, 1); assert.ok(posted[0].path.endsWith('/installation-continuation'));
  assert.equal(posted[0].options.body, '{}'); assert.equal(posted[0].options.headers['X-Onboarding-Journey'], journey);
  assert.equal(f.document.body.children.length, 1);
  assert.equal(f.elements.identityStep.hidden, true); assert.equal(f.elements.bindingStep.hidden, true);
  assert.equal(f.document.querySelector('#claim-step-description').textContent, '');
  assert.equal(f.document.querySelector('.intro').hidden, true);
  assert.equal(f.elements.claimStep.attributes['aria-current'], 'step');
  assert.equal(f.elements.bindingStep.attributes['aria-current'], undefined);
  assert.equal(f.elements.claimStepState.textContent, '');
});

test('approved saved setup finishes automatically and only the visible step owns status', async () => {
  const f = fixture({ state: 'completed' });
  f.status({ ...pending('completed'), stage: 'finalization_ready' });
  await f.controller.start(); f.push(); await settle(); await settle();
  assert.equal(f.calls.filter((call) => call.path.endsWith('/installation-continuation')).length, 0);
  assert.equal(f.calls.filter((call) => call.path.endsWith('/finalize')).length, 1);
  assert.equal(f.elements.claimStep.hidden, true);
  assert.equal(f.elements.claimStep.dataset.state, 'completed');
  assert.equal(f.elements.finalizeStep.hidden, false);
  assert.equal(f.elements.finalizeStepState.textContent, '');
  assert.equal(f.elements.finalizeActionHelp.textContent, '');
  assert.equal(f.elements.status.textContent, 'Restarting the desktop app…');
});

test('unknown preparation only reads the same context on subsequent checks', async () => {
  const f = fixture({ prepare: async () => { throw new Error('lost result'); } });
  await f.controller.start(); f.push(); await settle(); await settle();
  assert.equal(f.elements.status.textContent, 'Setup could not be confirmed.');
  f.push(); await settle(); await settle();
  const continuation = f.calls.filter((call) => call.path.endsWith('/installation-continuation'));
  assert.deepEqual(continuation.map((call) => call.options.method), ['POST', 'GET']);
  assert.equal(f.document.body.children.length, 1);
});

test('restored uncertain context never prepares again', async () => {
  const f = fixture({ state: 'prepare-unknown', prepare: () => assert.fail('Preparation replay') });
  await f.controller.start(); f.push(); await settle(); await settle();
  assert.deepEqual(f.calls.filter((call) => call.path.endsWith('/installation-continuation')).map((call) => call.options.method), ['GET']);
});

test('current-session state is admitted without accepting the old forced-authentication vocabulary', () => {
  assert.deepEqual(parseStatusResponse(pending('authentication-required')), pending('authentication-required'));
  assert.equal(parseStatusResponse(pending('reauthentication-required')), null);
});

test('a never-resolving prepare is bounded and a fresh page only reads the original operation', async () => {
  const storage = new Map();
  const f = fixture({ storage, continuationTimeoutMs: 5, prepare: () => new Promise(() => {}) });
  await f.controller.start(); f.push();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(f.elements.status.textContent, 'Setup could not be confirmed.');
  assert.equal(f.document.querySelector('#transfer-recovery-action').hidden, false);
  const restored = fixture({ storage, prepare: () => assert.fail('Unknown preparation must not replay') });
  await restored.controller.start(); restored.push(); await settle(); await settle();
  assert.deepEqual(restored.calls.filter((call) => call.path.endsWith('/installation-continuation')).map((call) => call.options.method), ['GET']);
  assert.equal(restored.document.body.children.length, 1);
});

test('a retired preparation reply cannot navigate or prepare again after restoration', async () => {
  let complete;
  const f = fixture({ prepare: () => new Promise((resolve) => { complete = resolve; }) });
  await f.controller.start(); f.push(); await settle();
  f.lifecycle.pagehide(); complete(payload); await settle(); await settle();
  assert.equal(f.document.body.children.length, 0);
  f.lifecycle.pageshow({ persisted: true }); await settle(); f.push(); await settle(); await settle();
  assert.deepEqual(f.calls.filter((call) => call.path.endsWith('/installation-continuation')).map((call) => call.options.method), ['POST', 'GET']);
  assert.equal(f.document.body.children.length, 1);
});
