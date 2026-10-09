import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createProvisioningController } from './provisioning.js';

const ASSOCIATION_ID = 'association-1';
const CREATOR_ID = 'creator-1';

test('returning to setup acquires matched approval and finishes exactly once', async () => {
  const ui = elements();
  const callbacks = new Map();
  const doc = { ...document(), addEventListener(name, callback) { callbacks.set(name, callback); } };
  const calls = [];
  const controller = createProvisioningController({ document: doc, elements: ui,
    sendExtensionMessage: async () => { throw new Error('unexpected identity query'); },
    fetch: async (path) => {
      calls.push(path);
      if (path.endsWith('/status')) return response(200, { state: 'provisioning_ready', stage: 'creator_approval_pending', association_request_id: ASSOCIATION_ID, creator_account_id: CREATOR_ID });
      if (path.endsWith('/acquire')) return response(200, { association_request_id: ASSOCIATION_ID, status: 'approved' });
      if (path.endsWith('/finalize')) return response(200, { state: 'configured_restart' });
      throw new Error('unexpected request');
    },
  });
  await controller.start();
  assert.equal(calls.filter((path) => path.endsWith('/acquire')).length, 1, 'a restored document advances without a synthetic focus event');
  doc.hidden = false;
  await callbacks.get('visibilitychange')();
  await callbacks.get('visibilitychange')();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.filter((path) => path.endsWith('/acquire')).length, 1);
  assert.equal(calls.filter((path) => path.endsWith('/finalize')).length, 1);
  assert.equal(ui.finalizeStep.dataset.state, 'completed');
});

function element() {
  return {
    dataset: {},
    textContent: '',
    disabled: false,
    value: '',
    listeners: {},
    attributes: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
  };
}

function elements() {
  return {
    status: element(), identityStatus: element(), claimForm: element(), claimPackage: element(),
    claimPackageValidation: element(), claimPackageCount: element(), claimSubmit: element(),
    claimActionHelp: element(), detectedIdentity: element(), refreshIdentity: element(),
    confirmIdentity: element(), identityConfirmHelp: element(), acquireAssociation: element(),
    bindingActionHelp: element(), finalizeProvisioning: element(), finalizeActionHelp: element(),
    claimStep: element(), identityStep: element(), bindingStep: element(), finalizeStep: element(),
    claimStepState: element(), identityStepState: element(), bindingStepState: element(),
    finalizeStepState: element(),
  };
}

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

function document() {
  return {
    hidden: false,
    querySelector(selector) {
      if (selector != 'main') return null;
      return { dataset: { provisioningCsrf: 'csrf', provisioningExtensionId: '' } };
    },
    addEventListener() {},
  };
}

function pendingStatus() {
  return {
    state: 'provisioning_ready',
    stage: 'creator_approval_pending',
    association_request_id: ASSOCIATION_ID,
    creator_account_id: CREATOR_ID,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const journeyId = '11111111-1111-4111-8111-111111111111';
const snapshot = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)))
  .cases.find((entry) => entry.id === 'brain-snapshot').value;

function lifecycleFixture({ stage, mutation, identity = async () => ({ type: 'provisioning.identity.result', version: 1,
  authenticated_profile: { creator_account_id: CREATOR_ID } }), extensionId = 'a'.repeat(32) }) {
  const callbacks = new Map(), connections = [], calls = [], ui = elements(), forms = [], navigations = [];
  const hosted = 'https://setup.example.test/public/onboarding';
  const link = { href: hosted, hidden: false, addEventListener() {} };
  const recovery = element();
  const doc = { ...document(), defaultView: {
    location: { hash: `#journey=${journeyId}`, replace: (url) => navigations.push(url) },
    addEventListener: (name, callback) => callbacks.set(name, callback),
  }, body: { append: (form) => forms.push(form) },
  createElement: () => ({ append() {}, submit() { this.submitted = true; } }),
  querySelector: (selector) => selector === 'main'
    ? { dataset: { provisioningCsrf: 'csrf', provisioningExtensionId: extensionId } }
    : selector === '#open-secure-setup' ? link : selector === '#transfer-recovery-action' ? recovery : null,
  };
  const state = { progress: stage, handoff: { state: 'unknown', journey_id: journeyId } };
  const controller = createProvisioningController({ document: doc, elements: ui, sendExtensionMessage: identity,
    connectOnboarding: async (options) => { connections.push(options); return { close() {} }; },
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (path.endsWith('/status')) return response(200, state.progress);
      if (path.endsWith('/initial-handoff') && options?.method !== 'POST') return response(200, state.handoff);
      if (path.endsWith('/finalize') && state.completeFinalize) return response(200, { state: 'configured_restart' });
      return mutation.promise;
    } });
  return { controller, state, ui, calls, forms, navigations, connections, recovery,
    hide: () => callbacks.get('pagehide')(),
    async restore() { callbacks.get('pageshow')({ persisted: true }); await settle(); connections.at(-1).onState(snapshot); await settle(); },
  };
}

for (const operation of ['acquire', 'initial-handoff', 'finalize']) {
  for (const replyAfterRestore of [false, true]) test(`${operation} reply retired by pagehide cannot advance setup, after restore=${replyAfterRestore}`, async () => {
    const mutation = deferred();
    const stage = operation === 'initial-handoff'
      ? { state: 'provisioning_ready', stage: 'registration_required', association_request_id: null, creator_account_id: null }
      : { ...pendingStatus(), stage: operation === 'finalize' ? 'finalization_ready' : 'creator_approval_pending' };
    const f = lifecycleFixture({ stage, mutation });
    await f.controller.start();
    f.connections[0].onState(snapshot);
    await settle();
    assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1);
    f.hide();
    if (replyAfterRestore) await f.restore();
    mutation.resolve(response(200, operation === 'acquire'
      ? { association_request_id: ASSOCIATION_ID, status: 'approved' }
      : operation === 'finalize' ? { state: 'configured_restart' }
        : { state: 'waiting', journey_id: journeyId, handoff_reference: 'a'.repeat(43),
          hosted_start_url: 'https://setup.example.test/public/onboarding/start' }));
    await settle();
    if (!replyAfterRestore) await f.restore();
    await f.controller.acquireAssociation();
    await f.controller.finalizeProvisioning();
    await f.controller.beginHostedSetup();
    assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1, 'an uncertain mutation is not replayed');
    assert.equal(f.forms.length, 0, 'a retired handoff cannot submit a form');
    assert.equal(f.navigations.length, 0);
    assert.ok(f.connections.every((connection) => !connection.runtime), 'a retired finalize reply cannot restart or navigate');
    assert.notEqual(f.ui.finalizeStep.dataset.state, 'completed');
    assert.ok(f.calls.filter((call) => call.path.endsWith('/status')).length >= 2, 'restore reads current owner state');
    assert.equal(f.ui.status.textContent, operation === 'initial-handoff'
      ? 'Setup could not be confirmed. Reopen the desktop app to continue.'
      : 'This step could not be confirmed. Reopen the desktop app to continue.');

    // A separate fresh read can prove completion, regardless of the retired reply.
    f.state.progress = { state: 'configured_restart' };
    await f.controller.checkStatus();
    assert.equal(f.ui.finalizeStep.dataset.state, 'completed');
    assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1);
  });
}

test('identity from a retired page cannot replace a fresh account after browser Back', async () => {
  const oldIdentity = deferred();
  let reads = 0;
  const signedIn = (creator) => ({ type: 'provisioning.identity.result', version: 1,
    authenticated_profile: { creator_account_id: creator } });
  const f = lifecycleFixture({ mutation: deferred(), extensionId: 'a'.repeat(32),
    stage: { state: 'provisioning_ready', stage: 'creator_confirmation_required', association_request_id: null, creator_account_id: null },
    identity: () => ++reads === 1 ? oldIdentity.promise : Promise.resolve(signedIn('current-creator')) });
  const started = f.controller.start();
  await settle();
  f.hide(); await f.restore();
  await f.controller.refreshIdentity();
  assert.equal(f.ui.identityStatus.textContent, 'Signed in now: current-creator');
  oldIdentity.resolve(signedIn('retired-creator'));
  await started;
  assert.equal(f.ui.identityStatus.textContent, 'Signed in now: current-creator');
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 0);
});

test('a restored handoff is recovered from a fresh owner GET, never the retired reply or another POST', async () => {
  const mutation = deferred();
  const f = lifecycleFixture({ mutation, stage: { state: 'provisioning_ready', stage: 'registration_required',
    association_request_id: null, creator_account_id: null } });
  await f.controller.start(); f.connections[0].onState(snapshot); await settle();
  f.hide(); await f.restore();
  f.state.handoff = { state: 'waiting', journey_id: journeyId, handoff_reference: 'c'.repeat(43),
    hosted_start_url: 'https://setup.example.test/public/onboarding/start' };
  mutation.resolve(response(200, { ...f.state.handoff, handoff_reference: 'b'.repeat(43) }));
  await settle();
  assert.equal(f.forms.length, 1);
  assert.equal(f.forms[0].submitted, true);
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1);
  const read = f.calls.findLast((call) => call.path.endsWith('/initial-handoff') && call.options?.method !== 'POST');
  assert.equal(read.options.headers['X-Onboarding-Journey'], journeyId);
  assert.equal(read.options.headers['X-Provisioning-CSRF'], 'csrf');
  assert.equal(read.options.cache, 'no-store');
  f.connections.at(-1).onState({ ...snapshot, revision: snapshot.revision + 1 }); await settle();
  assert.equal(f.forms.length, 1, 'a new snapshot cannot resubmit the recovered form');
});

test('fresh approval advances even if the old acquire reply remains unresolved', async () => {
  const mutation = deferred();
  const f = lifecycleFixture({ mutation, stage: pendingStatus() });
  await f.controller.start(); f.connections[0].onState(snapshot); await settle();
  f.hide();
  f.state.progress = { ...pendingStatus(), stage: 'finalization_ready' };
  f.state.completeFinalize = true;
  await f.restore();
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 2,
    'the fresh committed owner state can finalize without waiting for the retired fetch');
  assert.equal(f.ui.finalizeStep.dataset.state, 'completed');
  mutation.resolve(response(200, { association_request_id: ASSOCIATION_ID, status: 'approved' }));
  await settle();
  assert.equal(f.calls.filter((call) => call.path.endsWith('/acquire')).length, 1);
  assert.equal(f.calls.filter((call) => call.path.endsWith('/finalize')).length, 1);
  assert.equal(f.ui.finalizeStep.dataset.state, 'completed');
});

test('a retired refusal requires a fresh status and exposes an explicit retry without replaying it', async () => {
  const mutation = deferred();
  const f = lifecycleFixture({ mutation, stage: pendingStatus() });
  await f.controller.start(); f.connections[0].onState(snapshot); await settle();
  f.hide(); await f.restore();
  mutation.resolve(response(409, { state: 'provisioning_ready', reason: 'binding_acquisition_unavailable' }));
  await settle();
  assert.equal(f.ui.acquireAssociation.hidden, false);
  assert.equal(f.ui.acquireAssociation.disabled, false);
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1);
  await f.controller.acquireAssociation();
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 2);
});

test('a retired exact preparation refusal needs a fresh owner read and explicit retry', async () => {
  const mutation = deferred();
  const f = lifecycleFixture({ mutation, stage: { state: 'provisioning_ready', stage: 'registration_required',
    association_request_id: null, creator_account_id: null } });
  await f.controller.start(); f.connections[0].onState(snapshot); await settle();
  f.hide(); await f.restore();
  mutation.resolve(response(409, { state: 'unconfirmed', reason: 'handoff_refused' })); await settle();
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 1);
  assert.equal(f.recovery.hidden, false); assert.equal(f.recovery.textContent, 'Try setup again');
  const before = f.calls.filter((call) => call.path.endsWith('/status')).length;
  await f.recovery.onclick(); await settle();
  assert.equal(f.calls.filter((call) => call.path.endsWith('/status')).length, before + 1);
  assert.equal(f.calls.filter((call) => call.options?.method === 'POST').length, 2);
  assert.equal(f.forms.length, 0);
});

test('browser Back restores a closed owner subscription and ignores the previous page generation', async () => {
  const callbacks = new Map();
  const page = { location: { hash: '#journey=11111111-1111-4111-8111-111111111111' },
    addEventListener(name, callback) { callbacks.set(name, callback); } };
  const doc = { ...document(), defaultView: page };
  const connections = [];
  const requests = [];
  const controller = createProvisioningController({ document: doc, elements: elements(),
    sendExtensionMessage: async () => null,
    connectOnboarding: async (options) => {
      const connection = { ...options, closed: false, close() { this.closed = true; } };
      connections.push(connection);
      return connection;
    },
    fetch: async (path, options) => {
      requests.push({ path, options });
      return response(200, { state: 'provisioning_ready', stage: 'creator_confirmation_required',
        association_request_id: null, creator_account_id: null });
    },
  });
  const vectors = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)));
  const snapshot = vectors.cases.find((entry) => entry.id === 'brain-snapshot').value;
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  await controller.start();
  connections[0].onState(snapshot);
  await settle();
  assert.equal(requests.length, 1);

  callbacks.get('pagehide')();
  assert.equal(connections[0].closed, true);
  callbacks.get('pageshow')({ persisted: true });
  await settle();
  assert.equal(connections.length, 2);
  connections[0].onState({ ...snapshot, revision: snapshot.revision + 10 });
  await settle();
  assert.equal(requests.length, 1, 'the old page cannot publish authority after restore');
  connections[1].onState(snapshot);
  await settle();
  assert.equal(requests.length, 2, 'the restored subscription reconciles even the same owner revision');
  assert(requests.every(({ path, options }) => path.endsWith('/status') && !options?.method));
});

test('a fresh owner snapshot after hosted return acquires pending approval once', async () => {
  const callbacks = new Map();
  const doc = { ...document(), defaultView: {
    location: { hash: '#journey=11111111-1111-4111-8111-111111111111' },
    addEventListener(name, callback) { callbacks.set(name, callback); },
  } };
  const connections = [];
  const requests = [];
  const controller = createProvisioningController({ document: doc, elements: elements(),
    sendExtensionMessage: async () => null,
    connectOnboarding: async (options) => { connections.push(options); return { close() {} }; },
    fetch: async (path) => {
      requests.push(path);
      if (path.endsWith('/status')) return response(200, pendingStatus());
      if (path.endsWith('/acquire')) return response(200, { association_request_id: ASSOCIATION_ID, status: 'approved' });
      if (path.endsWith('/finalize')) return response(200, { state: 'configured_restart' });
      throw new Error('Unexpected request');
    },
  });
  const vectors = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)));
  const snapshot = vectors.cases.find((entry) => entry.id === 'brain-snapshot').value;
  await controller.start();
  assert.equal(requests.length, 0);
  connections[0].onState(snapshot);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests.map((path) => path.split('/').at(-1)), ['status', 'acquire', 'finalize']);
  connections[0].onState({ ...snapshot, revision: snapshot.revision + 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 3, 'the retired provisioning connection cannot replay a mutation');
});

for (const unavailable of [false, true]) test(`lost finalization response is reconciled before retry, status unavailable=${unavailable}`, async () => {
  const ui = elements();
  const calls = [];
  let completed = false;
  let statusUnavailable = unavailable;
  const controller = createProvisioningController({ document: document(), elements: ui,
    sendExtensionMessage: async () => null,
    fetch: async (path) => {
      calls.push(path.split('/').at(-1));
      if (path.endsWith('/status')) {
        if (completed && statusUnavailable) throw new Error('Status unavailable');
        return response(200, completed ? { state: 'configured_restart' } : pendingStatus());
      }
      if (path.endsWith('/acquire')) return response(200, { association_request_id: ASSOCIATION_ID, status: 'approved' });
      if (path.endsWith('/finalize')) { completed = true; throw new Error('Response lost'); }
      throw new Error('Unexpected request');
    },
  });
  await controller.start();
  assert.equal(ui.finalizeActionHelp.textContent, 'Setup completion could not be confirmed. Try again.');
  await controller.finalizeProvisioning();
  statusUnavailable = false;
  await controller.finalizeProvisioning();
  assert.equal(calls.filter((call) => call === 'finalize').length, 1);
  assert(calls.slice(calls.indexOf('finalize') + 1).includes('status'));
  assert.equal(ui.finalizeStep.dataset.state, 'completed');
});

test('pending approval is a neutral durable state and remains pending after an authoritative recheck', async () => {
  const ui = elements();
  const calls = [];
  const controller = createProvisioningController({
    document: document(),
    elements: ui,
    sendExtensionMessage: async () => { throw new Error('identity should not be queried'); },
    fetch: async (path, options = {}) => {
      calls.push([path, options]);
      if (path === '/api/v1/provisioning/status') return response(200, pendingStatus());
      if (path === '/api/v1/provisioning/creator-association/acquire') {
        return response(409, { state: 'provisioning_ready', reason: 'binding_acquisition_unavailable' });
      }
      throw new Error(`unexpected request ${path}`);
    },
  });

  await controller.start();
  assert.equal(ui.bindingStep.dataset.state, 'current');
  assert.equal(ui.finalizeStep.dataset.state, 'locked');
  assert.equal(ui.status.textContent, 'Approval could not be checked. Try again.');
  assert.equal(ui.status.dataset.tone, 'neutral');

  await controller.acquireAssociation();
  assert.equal(ui.bindingStep.dataset.state, 'current');
  assert.equal(ui.finalizeProvisioning.disabled, true);
  assert.match(ui.status.textContent, /Approval could not be checked/);
  assert.equal(ui.status.dataset.tone, 'neutral');
  assert.deepEqual(JSON.parse(calls.at(-1)[1].body), {});
});

test('only matching authoritative approval unlocks finalization', async () => {
  const ui = elements();
  let approval = 'mismatch';
  const controller = createProvisioningController({
    document: document(),
    elements: ui,
    sendExtensionMessage: async () => { throw new Error('identity should not be queried'); },
    fetch: async (path) => {
      if (path === '/api/v1/provisioning/status') return response(200, pendingStatus());
      if (path === '/api/v1/provisioning/creator-association/acquire') {
        return response(200, {
          association_request_id: approval === 'match' ? ASSOCIATION_ID : 'different-association',
          status: 'approved',
        });
      }
      throw new Error(`unexpected request ${path}`);
    },
  });

  await controller.start();
  await controller.acquireAssociation();
  assert.equal(ui.bindingStep.dataset.state, 'current');
  assert.equal(ui.finalizeProvisioning.disabled, true);
  assert.equal(ui.status.dataset.tone, 'error');

  approval = 'match';
  await controller.acquireAssociation();
  assert.equal(ui.bindingStep.dataset.state, 'completed');
  assert.equal(ui.finalizeStep.dataset.state, 'current');
  assert.equal(ui.finalizeProvisioning.disabled, false);
  assert.equal(ui.status.textContent, '');
});

test('recovery state never exposes approval or finalization actions', async () => {
  const ui = elements();
  const controller = createProvisioningController({
    document: document(),
    elements: ui,
    sendExtensionMessage: async () => { throw new Error('identity should not be queried'); },
    fetch: async (path) => {
      if (path !== '/api/v1/provisioning/status') throw new Error(`unexpected request ${path}`);
      return response(200, {
        state: 'provisioning_ready',
        stage: 'recovery_required',
        association_request_id: null,
        creator_account_id: null,
      });
    },
  });

  await controller.start();
  assert.equal(ui.bindingStep.dataset.state, 'locked');
  assert.equal(ui.acquireAssociation.disabled, true);
  assert.equal(ui.finalizeProvisioning.disabled, true);
  assert.match(ui.status.textContent, /Do not reuse this setup code/);
});
