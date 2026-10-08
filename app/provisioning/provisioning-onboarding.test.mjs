import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createProvisioningController, parseBrainOnboardingState, parseJourneyHash, submitHostedHandoff } from './provisioning.js';

const journey = '11111111-1111-4111-8111-111111111111';
const hosted = 'https://setup.example.test/public/onboarding';
const payload = { state: 'waiting', journey_id: journey, handoff_reference: 'a'.repeat(43), hosted_start_url: `${hosted}/start` };
const vectors = JSON.parse(readFileSync(new URL('../../shared/onboarding/vectors.json', import.meta.url)));
const snapshot = structuredClone(vectors.cases.find((value) => value.id === 'brain-snapshot').value);

function element() {
  return { dataset: {}, hidden: false, textContent: '', value: '', children: [], listeners: {}, attributes: {},
    append(child) { this.children.push(child); }, submit() { this.submitted = true; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; } };
}

function document() {
  const link = { ...element(), href: hosted };
  const page = { location: { hash: `#journey=${journey}`, replace(value) { this.replaced = value; } }, addEventListener() {} };
  return { defaultView: page, hidden: false, body: element(), createElement: element, addEventListener() {},
    querySelector(selector) { return selector === 'main' ? { dataset: { provisioningCsrf: 'test-csrf', provisioningExtensionId: '' } }
      : selector === '#open-secure-setup' ? link : null; } };
}

test('initial admission posts context in the same tab without credentials or arbitrary redirects in its URL', () => {
  const doc = document();
  assert.equal(submitHostedHandoff({ document: doc, payload, journeyId: journey, registeredHostedUrl: hosted }), true);
  const form = doc.body.children[0];
  assert.equal(form.target, '_self'); assert.equal(form.method, 'post'); assert.equal(form.action, `${hosted}/start`);
  assert.deepEqual(form.children.map((input) => [input.name, input.value]), [['journey_id', journey], ['handoff_reference', 'a'.repeat(43)]]);
  assert.equal(form.submitted, true);
});

test('mismatched journey, target, extended responses and authority-bearing references never navigate', () => {
  for (const bad of [{ ...payload, journey_id: snapshot.epoch }, { ...payload, token: 'forbidden' },
    { ...payload, hosted_start_url: 'https://unregistered.example.test/start' },
    { ...payload, hosted_start_url: `${hosted}/start?token=x` }, { ...payload, handoff_reference: 'short' }]) {
    const doc = document();
    assert.equal(submitHostedHandoff({ document: doc, payload: bad, journeyId: journey, registeredHostedUrl: hosted }), false);
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

test('fresh desktop entry automates preparation and hides the legacy manual claim step', async () => {
  const doc = document();
  const ui = Object.fromEntries(['status', 'identityStatus', 'claimForm', 'claimPackage', 'claimPackageValidation', 'claimPackageCount',
    'claimSubmit', 'claimActionHelp', 'refreshIdentity', 'confirmIdentity', 'acquireAssociation', 'finalizeProvisioning', 'finalizeActionHelp',
    'claimStep', 'identityStep', 'bindingStep', 'finalizeStep', 'claimStepState', 'identityStepState', 'bindingStepState', 'finalizeStepState']
    .map((name) => [name, element()]));
  const calls = []; let push;
  const controller = createProvisioningController({ document: doc, elements: ui, sendExtensionMessage: async () => null,
    connectOnboarding: async ({ onState }) => { push = onState; return { close() {} }; },
    fetch: async (path, options) => {
      calls.push({ path, options });
      return { ok: true, status: 200, json: async () => path.endsWith('/status') ? {
        state: 'provisioning_ready', stage: 'registration_required', association_request_id: null, creator_account_id: null,
      } : payload };
    } });
  await controller.start();
  assert.equal(calls.length, 0, 'waits for authenticated pushed owner state');
  push(snapshot);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls.map((call) => call.path), ['/api/v1/provisioning/status', '/api/v1/provisioning/initial-handoff']);
  assert.equal(ui.claimForm.hidden, true); assert.equal(ui.claimSubmit.hidden, true);
  assert.equal(doc.body.children[0].submitted, true);
  push(snapshot); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 2, 'same committed revision does not repeat preparation');
});
