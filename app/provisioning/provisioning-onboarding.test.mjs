import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createProvisioningController, parseBrainOnboardingState, parseJourneyHash, submitHostedHandoff } from './provisioning.js';

const journey = '11111111-1111-4111-8111-111111111111';
const hosted = 'https://setup.example.test/public/onboarding/setup';
const payload = { state: 'waiting', journey_id: journey, handoff_reference: 'a'.repeat(43), hosted_start_url: `${hosted}/start` };
const vectors = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)));
const snapshot = structuredClone(vectors.cases.find((value) => value.id === 'brain-snapshot').value);

function element() {
  return { dataset: {}, hidden: false, textContent: '', value: '', children: [], listeners: {}, attributes: {},
    append(child) { this.children.push(child); }, submit() { this.submitted = true; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; } };
}

function document(extensionId = 'a'.repeat(32)) {
  const link = { ...element(), href: hosted };
  const nodes = new Map(['#initial-browser-prerequisite', '#initial-extension-install', '#initial-open-extension',
    '#initial-open-onlyfans', '#claim-heading', '#transfer-recovery-action'].map((name) => [name, element()]));
  const callbacks = new Map();
  const page = { location: { hash: `#journey=${journey}`, replace(value) { this.replaced = value; }, assign(value) { this.assigned = value; } },
    addEventListener(name, listener) { callbacks.set(name, listener); } };
  return { defaultView: page, hidden: false, body: element(), createElement: element, addEventListener() {},
    callbacks, querySelector(selector) { return selector === 'main' ? { dataset: { provisioningCsrf: 'test-csrf', provisioningExtensionId: extensionId } }
      : selector === '#open-secure-setup' ? link : nodes.get(selector) ?? null; } };
}

test('initial admission posts context in the same tab without credentials or arbitrary redirects in its URL', () => {
  const doc = document();
  assert.equal(submitHostedHandoff({ document: doc, payload, journeyId: journey, registeredHostedUrl: hosted, intendedCreatorId: 'creator-1' }), true);
  const form = doc.body.children[0];
  assert.equal(form.target, '_self'); assert.equal(form.method, 'post'); assert.equal(form.action, `${hosted}/start`);
  assert.deepEqual(form.children.map((input) => [input.name, input.value]), [['journey_id', journey],
    ['handoff_reference', 'a'.repeat(43)], ['intended_creator_id', 'creator-1']]);
  assert.equal(form.submitted, true);
});

test('mismatched journey, target, extended responses and authority-bearing references never navigate', () => {
  for (const bad of [{ ...payload, journey_id: snapshot.epoch }, { ...payload, token: 'forbidden' },
    { ...payload, hosted_start_url: 'https://unregistered.example.test/start' },
    { ...payload, hosted_start_url: `${hosted}/start?token=x` }, { ...payload, handoff_reference: 'short' }]) {
    const doc = document();
    assert.equal(submitHostedHandoff({ document: doc, payload: bad, journeyId: journey, registeredHostedUrl: hosted, intendedCreatorId: 'creator-1' }), false);
    assert.equal(doc.body.children.length, 0);
  }
  assert.equal(parseJourneyHash(`#journey=${journey}&token=bad`), null);
});

test('standalone provisioning validates the same closed Brain state contract', () => {
  for (const fixture of vectors.cases) {
    const expected = fixture.valid && fixture.value.profile === 'local-onboarding-state.v1' && fixture.value.source === 'brain';
    assert.equal(parseBrainOnboardingState(fixture.value) !== null, expected, fixture.id);
  }
});

test('fresh entry requires a bounded creator; a receiving continuation never accepts an override', () => {
  for (const creator of [undefined, null, 42, {}, '', 'a'.repeat(129), 'creator/name', '<script>']) {
    const doc = document();
    assert.equal(submitHostedHandoff({ document: doc, payload, journeyId: journey, registeredHostedUrl: hosted, intendedCreatorId: creator }), false);
    assert.equal(doc.body.children.length, 0);
  }
  const receiving = { ...payload, continuation_reference: 'b'.repeat(43) };
  const doc = document();
  assert.equal(submitHostedHandoff({ document: doc, payload: receiving, journeyId: journey, registeredHostedUrl: hosted, intendedCreatorId: 'creator-1' }), false);
  assert.equal(submitHostedHandoff({ document: doc, payload: receiving, journeyId: journey, registeredHostedUrl: hosted }), true);
  assert.deepEqual(doc.body.children[0].children.map((input) => input.name), ['journey_id', 'handoff_reference', 'continuation_reference']);
});

const settle = () => new Promise((resolve) => setImmediate(resolve));
const signedIn = (creator = 'creator-1') => ({ type: 'provisioning.identity.result', version: 1,
  authenticated_profile: creator === null ? null : { creator_account_id: creator } });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function freshFixture({ identity = async () => signedIn(), prepare = async () => payload, continueTransfer, loadLocalWorkspace,
  recover = async () => payload, extensionId, prepareStatus = 200 } = {}) {
  const doc = document(extensionId);
  const ui = Object.fromEntries(['status', 'identityStatus', 'claimForm', 'claimPackage', 'claimPackageValidation', 'claimPackageCount',
    'claimSubmit', 'claimActionHelp', 'refreshIdentity', 'confirmIdentity', 'acquireAssociation', 'finalizeProvisioning', 'finalizeActionHelp',
    'claimStep', 'identityStep', 'bindingStep', 'finalizeStep', 'claimStepState', 'identityStepState', 'bindingStepState', 'finalizeStepState']
    .map((name) => [name, element()]));
  const calls = [], ports = []; let push;
  const controller = createProvisioningController({ document: doc, elements: ui, sendExtensionMessage: identity, continueTransfer, loadLocalWorkspace,
    connectExtension: (_id, onStage) => { const port = { onStage, open: (step) => calls.push({ open: step }), close() {} }; ports.push(port); return port; },
    connectOnboarding: async ({ onState }) => { push = onState; return { close() {} }; },
    fetch: async (path, options) => {
      calls.push({ path, options });
      const result = path.endsWith('/status') ? {
        state: 'provisioning_ready', stage: 'registration_required', association_request_id: null, creator_account_id: null,
      } : options?.method === 'POST' ? await prepare() : await recover();
      const status = options?.method === 'POST' ? typeof prepareStatus === 'function' ? prepareStatus() : prepareStatus : 200;
      return { ok: status === 200, status, json: async () => result };
    } });
  return { controller, doc, ui, calls, push: (revision = 1) => push({ ...snapshot, revision }),
    stage: (value) => ports.at(-1).onStage(value), forms: () => doc.body.children,
    posts: () => calls.filter((call) => call.options?.method === 'POST'),
    creator: () => doc.body.children[0]?.children.find((input) => input.name === 'intended_creator_id')?.value };
}

test('fresh desktop waits for an admitted creator instead of navigating from registration_required alone', async () => {
  const f = freshFixture({ identity: async () => signedIn(null) });
  await f.controller.start(); f.push(); await settle();
  assert.equal(f.posts().length, 0);
  assert.equal(f.forms().length, 0);
  assert.equal(f.doc.querySelector('#initial-browser-prerequisite').hidden, false);
  assert.equal(f.doc.querySelector('#open-secure-setup').hidden, true);
  assert.equal(f.doc.querySelector('#initial-extension-install').hidden, false);
});

test('local owner acknowledgement starts only after receiving context establishes ordinary setup', async () => {
  const receiving = deferred(); const eligibility = [];
  const f = freshFixture({ identity: async () => signedIn(null), continueTransfer: () => receiving.promise,
    loadLocalWorkspace: async () => ({ createLocalWorkspaceOwner: ({ current }) => {
      eligibility.push(current()); return { stop() {} };
    } }) });
  const started = f.controller.start(); await settle();
  assert.deepEqual(eligibility, []);
  receiving.resolve('none'); await started;
  assert.deepEqual(eligibility, [true]);
});

for (const outcome of ['unavailable', 'retired']) test(`receiving ${outcome} context cannot mount a local recovery owner`, async () => {
  const receiving = deferred(); let mounted = 0;
  const f = freshFixture({ continueTransfer: () => receiving.promise,
    loadLocalWorkspace: async () => ({ createLocalWorkspaceOwner: () => { mounted++; return { stop() {} }; } }) });
  const started = f.controller.start(); await settle();
  if (outcome === 'retired') f.doc.callbacks.get('pagehide')();
  receiving.resolve(outcome === 'retired' ? 'none' : outcome); await started;
  assert.equal(mounted, 0);
});

test('missing extension leaves usable ZIP guidance without a hosted or invented install navigation', async () => {
  const f = freshFixture({ extensionId: '', identity: async () => { throw Error('must not query an invalid extension ID'); } });
  await f.controller.start(); f.push(); await settle();
  assert.equal(f.posts().length, 0); assert.equal(f.forms().length, 0);
  assert.equal(f.doc.querySelector('#initial-extension-install').hidden, false);
  assert.equal(f.doc.querySelector('#initial-open-extension').hidden, true);
  const html = readFileSync(new URL('./provisioning.html', import.meta.url), 'utf8');
  const instructions = html.slice(html.indexOf('id="initial-extension-install"'), html.indexOf('id="initial-open-extension"'));
  assert.match(instructions, /Extract the extension ZIP/);
  assert.match(instructions, /Developer mode/);
  assert.match(instructions, /Load unpacked/);
  assert.doesNotMatch(instructions, /href=/);
});

test('an already admitted creator skips completed prerequisites and prepares once', async () => {
  const f = freshFixture();
  await f.controller.start();
  assert.equal(f.calls.length, 0, 'identity alone is not an owner registration fact');
  f.push(); await settle();
  assert.equal(f.posts().length, 1);
  assert.equal(f.creator(), 'creator-1');
  assert.equal(f.doc.querySelector('#initial-browser-prerequisite').hidden, true);
  assert.equal(f.ui.claimForm.hidden, true); assert.equal(f.ui.claimSubmit.hidden, true);
  f.push(2); await settle(); await f.controller.beginHostedSetup();
  assert.equal(f.forms().length, 1); assert.equal(f.posts().length, 1);
});

test('extension events finish prerequisites and continue automatically without another owner event', async () => {
  let identity = signedIn(null);
  const f = freshFixture({ identity: async () => identity });
  await f.controller.start(); f.push(); await settle();
  f.stage('needs_full'); await settle();
  assert.equal(f.doc.querySelector('#initial-open-extension').hidden, false);
  assert.equal(f.doc.querySelector('#initial-extension-install').hidden, true);
  f.doc.querySelector('#initial-open-extension').listeners.click();
  assert.equal(f.calls.at(-1).open, 'setup');
  f.stage('needs_account'); await settle();
  assert.equal(f.doc.querySelector('#initial-open-onlyfans').hidden, false);
  assert.equal(f.posts().length, 0);
  identity = signedIn(); f.stage('ready_to_pair'); await settle();
  assert.equal(f.posts().length, 1); assert.equal(f.creator(), 'creator-1');
});

test('late older identity cannot replace a newer admitted creator before preparation', async () => {
  const older = deferred(); let reads = 0;
  const f = freshFixture({ identity: () => ++reads === 1 ? older.promise : Promise.resolve(signedIn('creator-2')) });
  const started = f.controller.start(); await settle(); f.push(); await settle();
  assert.equal(f.posts().length, 0);
  f.stage('ready_to_pair'); await settle();
  older.resolve(signedIn('creator-1')); await started; await settle();
  assert.equal(f.creator(), 'creator-2'); assert.equal(f.posts().length, 1);
});

test('creator changes while preparation awaits use only the newly admitted identity', async () => {
  const prepared = deferred(); let identity = signedIn('creator-1');
  const f = freshFixture({ identity: async () => identity, prepare: () => prepared.promise });
  await f.controller.start(); f.push(); await settle();
  identity = signedIn('creator-2'); f.stage('ready_to_pair'); await settle();
  assert.equal(f.forms().length, 0);
  prepared.resolve(payload); await settle();
  assert.equal(f.creator(), 'creator-2'); assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 1);
});

for (const stage of ['needs_account', 'needs_terms']) test(`withdrawn identity (${stage}) after preparation waits, then recovers through GET without another POST`, async () => {
  const prepared = deferred(); let identity = signedIn();
  const f = freshFixture({ identity: async () => identity, prepare: () => prepared.promise });
  await f.controller.start(); f.push(); await settle();
  identity = signedIn(null); f.stage(stage); prepared.resolve(payload); await settle();
  assert.equal(f.forms().length, 0); assert.equal(f.posts().length, 1);
  identity = signedIn('creator-2'); f.stage('ready_to_pair'); await settle();
  assert.equal(f.creator(), 'creator-2'); assert.equal(f.posts().length, 1);
  assert.equal(f.calls.filter((call) => call.path?.endsWith('/initial-handoff') && call.options?.method !== 'POST').length, 1);
});

test('an initial preparation 503 is unconfirmed and recovers through a read instead of replay', async () => {
  const f = freshFixture({ prepareStatus: 503, prepare: async () => ({ reason: 'hosted_unavailable' }) });
  await f.controller.start(); f.push(); await settle();
  assert.equal(f.ui.status.textContent, 'Setup could not be confirmed.');
  assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 0);
  await f.controller.checkStatus(); await settle();
  assert.equal(f.posts().length, 1); assert.equal(f.creator(), 'creator-1');
});

test('only an explicit retry after exact preparation refusal and a fresh owner read prepares again', async () => {
  let refused = true;
  const f = freshFixture({ prepareStatus: () => refused ? 409 : 200,
    prepare: async () => refused ? { state: 'unconfirmed', reason: 'handoff_refused' } : payload });
  await f.controller.start(); f.push(); await settle();
  const action = f.doc.querySelector('#transfer-recovery-action');
  assert.equal(action.hidden, false); assert.equal(action.textContent, 'Try setup again');
  f.stage('ready_to_pair'); f.push(2); await settle(); await f.controller.beginHostedSetup();
  assert.equal(f.posts().length, 1, 'events and ordinary continuation do not repeat a refused preparation');
  const before = f.calls.filter((call) => call.path?.endsWith('/status')).length;
  refused = false; await action.onclick(); await settle();
  assert.equal(f.calls.filter((call) => call.path?.endsWith('/status')).length, before + 1);
  assert.equal(f.posts().length, 2); assert.equal(f.creator(), 'creator-1');
});

for (const reason of ['journey_expired', 'handoff_unconfirmed', 'authorization_revoked']) test(`${reason} refusal never permits preparation retry`, async () => {
  const f = freshFixture({ prepareStatus: 409, prepare: async () => ({ state: 'unconfirmed', reason }) });
  await f.controller.start(); f.push(); await settle();
  const action = f.doc.querySelector('#transfer-recovery-action');
  assert.equal(action.hidden, false);
  assert.equal(action.textContent, reason === 'journey_expired' ? 'Open desktop app' : 'Check setup');
  assert.equal(f.ui.status.textContent, reason === 'journey_expired' ? 'Setup has expired.' : 'Setup could not be confirmed.');
  await action.onclick(); await settle();
  if (reason === 'journey_expired') assert.equal(f.doc.defaultView.location.assigned, `ofca://onboarding?journey=${journey}`);
  f.stage('ready_to_pair'); f.push(2); await settle(); await f.controller.beginHostedSetup();
  assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 0);
});

test('an unavailable recovery locator keeps a read-only check without reopening or replaying preparation', async () => {
  const f = freshFixture({ prepare: async () => { throw Error('lost response'); },
    recover: async () => ({ state: 'unknown', journey_id: journey }) });
  await f.controller.start(); f.push(); await settle();
  const action = f.doc.querySelector('#transfer-recovery-action');
  assert.equal(action.hidden, false); assert.equal(action.textContent, 'Check setup');
  await action.onclick(); await settle();
  assert.equal(f.ui.status.textContent, 'Setup could not be confirmed.');
  assert.equal(action.hidden, false); assert.equal(action.textContent, 'Check setup');
  assert.equal(f.doc.querySelector('#open-secure-setup').hidden, true);
  const reads = f.calls.filter((call) => call.path?.endsWith('/initial-handoff') && call.options?.method !== 'POST').length;
  await action.onclick(); await settle();
  assert.equal(f.calls.filter((call) => call.path?.endsWith('/initial-handoff') && call.options?.method !== 'POST').length, reads + 1);
  await f.controller.beginHostedSetup(); await settle();
  assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 0);
});

test('lost preparation response and an account switch during recovery never repeat preparation or use old identity', async () => {
  const recovered = deferred(); let identity = signedIn();
  const f = freshFixture({ identity: async () => identity, prepare: async () => { throw Error('lost response'); }, recover: () => recovered.promise });
  await f.controller.start(); f.push(); await settle();
  assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 0);
  const check = f.controller.checkStatus(); await settle();
  identity = signedIn('creator-2'); f.stage('ready_to_pair'); await settle();
  recovered.resolve(payload); await check; await settle();
  assert.equal(f.creator(), 'creator-2'); assert.equal(f.posts().length, 1); assert.equal(f.forms().length, 1);
});

test('fresh setup cannot adopt a receiving continuation returned by a normal handoff read', async () => {
  const f = freshFixture({ prepare: async () => ({ ...payload, continuation_reference: 'b'.repeat(43) }) });
  await f.controller.start(); f.push(); await settle();
  assert.equal(f.forms().length, 0);
});
