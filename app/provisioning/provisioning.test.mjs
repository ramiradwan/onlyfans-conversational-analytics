import assert from 'node:assert/strict';
import test from 'node:test';

import { createProvisioningController, parseIdentityResponse } from './provisioning.js';

const EXTENSION_ID = 'lfiompogjmmgnbkacdnikbfoihmlloda';

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function element() {
  return {
    textContent: '', value: '', disabled: false, dataset: {},
    addEventListener() {},
  };
}

function harness({ extensionId = EXTENSION_ID, extensionResponse, fetch = async () => response(200, {}) } = {}) {
  const main = { dataset: { provisioningCsrf: 'csrf-token', provisioningExtensionId: extensionId } };
  const elements = {
    status: element(), identityStatus: element(), claimForm: element(), claimPackage: element(),
    claimSubmit: element(), detectedIdentity: element(), refreshIdentity: element(),
    confirmIdentity: element(), acquireAssociation: element(), finalizeProvisioning: element(),
  };
  const document = {
    hidden: false,
    querySelector(selector) { return selector === 'main' ? main : elements.claimSubmit; },
    addEventListener() {},
  };
  const extensionCalls = [];
  const controller = createProvisioningController({
    fetch,
    sendExtensionMessage: async (...arguments_) => {
      extensionCalls.push(arguments_);
      if (extensionResponse instanceof Error) throw extensionResponse;
      return extensionResponse;
    },
    document,
    elements,
  });
  return { controller, elements, extensionCalls };
}

test('identity query never starts association without the named confirm action', async () => {
  const fetchCalls = [];
  const { controller, elements, extensionCalls } = harness({
    extensionResponse: {
      type: 'provisioning.identity.result', version: 1,
      authenticated_profile: { creator_account_id: 'creator-42' },
    },
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, {});
    },
  });

  await controller.refreshIdentity();

  assert.deepEqual(extensionCalls, [[EXTENSION_ID, { type: 'provisioning.identity.query', version: 1 }]]);
  assert.equal(elements.detectedIdentity.textContent, 'creator-42');
  assert.equal(elements.confirmIdentity.disabled, false);
  assert.equal(fetchCalls.length, 0, 'identity_query_never_starts_association');
});

test('no extension answer instructs installation and keeps confirm unavailable', async () => {
  const { controller, elements } = harness({ extensionResponse: new Error('no receiver') });

  await controller.refreshIdentity();

  assert.match(elements.identityStatus.textContent, /Install or enable/);
  assert.equal(elements.confirmIdentity.disabled, true);
});

test('missing or malformed configured extension ID never sends a message', async () => {
  for (const extensionId of ['', 'wrong', 'q'.repeat(32)]) {
    const { controller, elements, extensionCalls } = harness({ extensionId });
    await controller.refreshIdentity();
    assert.match(elements.identityStatus.textContent, /Install or enable/);
    assert.equal(elements.confirmIdentity.disabled, true);
    assert.equal(extensionCalls.length, 0, 'invalid_extension_id_never_messages_extension');
  }
});

test('unauthenticated extension profile instructs sign-in and keeps confirm unavailable', async () => {
  const { controller, elements } = harness({
    extensionResponse: {
      type: 'provisioning.identity.result', version: 1, authenticated_profile: null,
    },
  });

  await controller.refreshIdentity();

  assert.match(elements.identityStatus.textContent, /Sign in, in a tab/);
  assert.equal(elements.confirmIdentity.disabled, true);
});

test('unexpected extension fields are rejected as a malformed identity response', async () => {
  const { controller, elements } = harness({
    extensionResponse: {
      type: 'provisioning.identity.result', version: 1,
      authenticated_profile: { creator_account_id: 'creator-42', extra: 'not adopted' },
    },
  });

  await controller.refreshIdentity();

  assert.match(elements.identityStatus.textContent, /unexpected identity response/);
  assert.equal(elements.confirmIdentity.disabled, true);
  assert.equal(parseIdentityResponse({ type: 'provisioning.identity.result', version: 1, authenticated_profile: {} }), null);
});

test('configured restart on arrival disables actions without querying the extension', async () => {
  const fetchCalls = [];
  const { controller, elements, extensionCalls } = harness({
    extensionResponse: {
      type: 'provisioning.identity.result', version: 1,
      authenticated_profile: { creator_account_id: 'creator-42' },
    },
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, { state: 'configured_restart' });
    },
  });

  await controller.start();

  assert.equal(fetchCalls.length, 1);
  assert.equal(extensionCalls.length, 0, 'configured_restart_does_not_query_or_advance');
  assert.equal(elements.claimSubmit.disabled, true);
  assert.match(elements.status.textContent, /Configuration is complete/);
});

test('empty pasted package is refused locally before a claim request', async () => {
  const fetchCalls = [];
  const { controller, elements } = harness({
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, {});
    },
  });

  await controller.submitClaim({ preventDefault() {} });

  assert.equal(fetchCalls.length, 0, 'empty_package_never_submits_claim');
  assert.match(elements.status.textContent, /Paste the installation package/);
});

test('401 mutation response tells the operator to restart provisioning', async () => {
  const { controller, elements } = harness({
    fetch: async () => response(401, { detail: 'provisioning session is invalid' }),
  });
  elements.claimPackage.value = 'pasted-package';

  await controller.submitClaim({ preventDefault() {} });

  assert.match(elements.status.textContent, /session is no longer valid/);
});

test('403 mutation response tells the operator to restart provisioning', async () => {
  const { controller, elements } = harness({
    fetch: async () => response(403, { detail: 'provisioning CSRF is invalid' }),
  });
  elements.claimPackage.value = 'pasted-package';

  await controller.submitClaim({ preventDefault() {} });

  assert.match(elements.status.textContent, /session is no longer valid/);
});

test('a route refusal reason is displayed verbatim without local interpretation', async () => {
  const { controller, elements } = harness({
    fetch: async () => response(409, { state: 'provisioning_ready', reason: 'server_reason_exact' }),
  });
  elements.claimPackage.value = 'pasted-package';

  await controller.submitClaim({ preventDefault() {} });

  assert.equal(elements.status.textContent, 'server_reason_exact');
});

test('confirm sends the displayed identity exactly once while the request is in flight', async () => {
  let resolveAssociation;
  const fetchCalls = [];
  const { controller, elements } = harness({
    extensionResponse: {
      type: 'provisioning.identity.result', version: 1,
      authenticated_profile: { creator_account_id: 'creator-42' },
    },
    fetch: (...arguments_) => {
      fetchCalls.push(arguments_);
      return new Promise((resolve) => { resolveAssociation = resolve; });
    },
  });
  await controller.refreshIdentity();

  const first = controller.confirmIdentity();
  const second = controller.confirmIdentity();
  assert.equal(fetchCalls.length, 1, 'confirm_request_is_single_flight');
  assert.equal(fetchCalls[0][0], '/api/v1/provisioning/creator-association');
  assert.deepEqual(JSON.parse(fetchCalls[0][1].body), { detected_creator_account_id: 'creator-42' });
  resolveAssociation(response(200, { association_request_id: 'request-1', status: 'pending', updated_at: 'now' }));
  await Promise.all([first, second]);
  assert.equal(elements.acquireAssociation.disabled, false);

  await controller.confirmIdentity();
  assert.equal(fetchCalls.length, 1, 'confirmed_identity_is_not_submitted_twice');
});
