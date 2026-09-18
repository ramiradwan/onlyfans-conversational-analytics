import assert from 'node:assert/strict';
import test from 'node:test';

import { createProvisioningController } from './provisioning.js';

const ASSOCIATION_ID = 'association-1';
const CREATOR_ID = 'creator-1';

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
  assert.equal(ui.status.textContent, '');
  assert.equal(ui.status.dataset.tone, 'neutral');

  await controller.acquireAssociation();
  assert.equal(ui.bindingStep.dataset.state, 'current');
  assert.equal(ui.finalizeProvisioning.disabled, true);
  assert.match(ui.status.textContent, /connection is not approved yet/i);
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
