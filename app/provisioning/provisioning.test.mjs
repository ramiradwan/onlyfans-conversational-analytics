import assert from 'node:assert/strict';
import test from 'node:test';

import { createProvisioningController, parseIdentityResponse } from './provisioning.js';

const EXTENSION_ID = 'lfiompogjmmgnbkacdnikbfoihmlloda';
const VALID_PACKAGE = 'cGFzdGVkLXBhY2thZ2U';

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function element() {
  const listeners = new Map();
  const attributes = new Map();
  return {
    textContent: '', value: '', disabled: false, dataset: {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    dispatch(type) { return listeners.get(type)?.({ preventDefault() {} }); },
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
    getAttribute(name) { return attributes.get(name) ?? null; },
  };
}

function harness({ extensionId = EXTENSION_ID, extensionResponse, fetch = async () => response(200, {}) } = {}) {
  const main = { dataset: { provisioningCsrf: 'csrf-token', provisioningExtensionId: extensionId } };
  const elements = {
    status: element(), identityStatus: element(), claimForm: element(), claimPackage: element(),
    claimPackageValidation: element(), claimPackageCount: element(), claimSubmit: element(),
    claimActionHelp: element(), detectedIdentity: element(), refreshIdentity: element(),
    confirmIdentity: element(), identityConfirmHelp: element(), acquireAssociation: element(),
    bindingActionHelp: element(), finalizeProvisioning: element(), finalizeActionHelp: element(),
    claimStep: element(), identityStep: element(), bindingStep: element(), finalizeStep: element(),
    claimStepState: element(), identityStepState: element(), bindingStepState: element(),
    finalizeStepState: element(),
  };
  const document = {
    hidden: false,
    querySelector(selector) { return selector === 'main' ? main : null; },
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

function signedInIdentity(accountId = 'creator-42') {
  return {
    type: 'provisioning.identity.result',
    version: 1,
    authenticated_profile: { creator_account_id: accountId },
  };
}

function stepStates(elements) {
  return [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep]
    .map((step) => step.dataset.state);
}

function currentSteps(elements) {
  return [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep]
    .filter((step) => step.getAttribute('aria-current') === 'step').length;
}

test('identity detection is read-only and confirmation requires registration', async () => {
  const fetchCalls = [];
  const { controller, elements, extensionCalls } = harness({
    extensionResponse: signedInIdentity(),
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, {});
    },
  });

  await controller.refreshIdentity();

  assert.deepEqual(extensionCalls, [[EXTENSION_ID, { type: 'provisioning.identity.query', version: 1 }]]);
  assert.equal(elements.detectedIdentity.textContent, 'creator-42');
  assert.equal(elements.confirmIdentity.disabled, true, 'identity_confirmation_requires_registration');
  assert.match(elements.identityConfirmHelp.textContent, /Register this installation/);
  assert.equal(fetchCalls.length, 0, 'identity_query_never_starts_association');
  assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
});

test('extension missing, malformed, or signed out gives actionable identity guidance', async (context) => {
  await context.test('missing extension answer', async () => {
    const { controller, elements } = harness({ extensionResponse: new Error('no receiver') });
    await controller.refreshIdentity();
    assert.match(elements.identityStatus.textContent, /Install or enable/);
    assert.equal(elements.confirmIdentity.disabled, true);
  });

  await context.test('malformed configured extension ID', async () => {
    for (const extensionId of ['', 'wrong', 'q'.repeat(32)]) {
      const { controller, elements, extensionCalls } = harness({ extensionId });
      await controller.refreshIdentity();
      assert.match(elements.identityStatus.textContent, /Install or enable/);
      assert.equal(elements.confirmIdentity.disabled, true);
      assert.equal(extensionCalls.length, 0, 'invalid_extension_id_never_messages_extension');
    }
  });

  await context.test('signed-out extension profile', async () => {
    const { controller, elements } = harness({
      extensionResponse: {
        type: 'provisioning.identity.result', version: 1, authenticated_profile: null,
      },
    });
    await controller.refreshIdentity();
    assert.match(elements.identityStatus.textContent, /Sign in, in a tab/);
    assert.equal(elements.confirmIdentity.disabled, true);
  });

  await context.test('malformed or extended identity response', async () => {
    const { controller, elements } = harness({
      extensionResponse: {
        type: 'provisioning.identity.result', version: 1,
        authenticated_profile: { creator_account_id: 'creator-42', extra: 'not adopted' },
      },
    });
    await controller.refreshIdentity();
    assert.match(elements.identityStatus.textContent, /unexpected account response/);
    assert.equal(elements.confirmIdentity.disabled, true);
    assert.equal(parseIdentityResponse({
      type: 'provisioning.identity.result', version: 1, authenticated_profile: {},
    }), null);
  });
});

test('invalid package input is rejected immediately and never fetched', async () => {
  const fetchCalls = [];
  const { controller, elements } = harness({
    extensionResponse: signedInIdentity(),
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, { state: 'provisioning_ready' });
    },
  });
  await controller.start();
  assert.equal(fetchCalls.length, 1, 'only_initial_status_was_fetched');

  const cases = [
    ['', /Paste the installation package/],
    ['abcd efgh', /only letters, numbers, hyphens, and underscores/],
    ['a', /appears incomplete/],
    ['a'.repeat(1401), /1,400 characters or fewer/],
  ];
  for (const [value, expectedMessage] of cases) {
    elements.claimPackage.value = value;
    elements.claimPackage.dispatch('input');
    assert.equal(elements.claimPackage.getAttribute('aria-invalid'), 'true');
    assert.match(elements.claimPackageValidation.textContent, expectedMessage);
    await controller.submitClaim({ preventDefault() {} });
    assert.equal(fetchCalls.length, 1, `invalid package was fetched: ${expectedMessage}`);
  }
  assert.equal(elements.claimPackageCount.textContent, '1,400+ / 1,400 characters');
});

test('surrounding package whitespace is accepted and the exact trimmed value is submitted', async () => {
  const fetchCalls = [];
  const { controller, elements } = harness({
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, { state: 'installation_registered' });
    },
  });
  elements.claimPackage.value = ` \n\t${VALID_PACKAGE}\u00a0\u2007 `;

  await controller.submitClaim({ preventDefault() {} });

  assert.equal(fetchCalls.length, 1);
  assert.deepEqual(JSON.parse(fetchCalls[0][1].body), { package: VALID_PACKAGE });
  assert.equal(elements.claimPackageCount.textContent, '19 / 1,400 characters');
});

test('all decoder refusal reasons have dedicated actionable public copy', async () => {
  const expectedMessages = {
    size: 'This installation package is too large. Return to setup, create a new package, and paste it here.',
    encoding: 'This installation package is incomplete or was changed. Copy it again and paste it without changes.',
    profile: 'This installation package is for a different setup. Return to setup and create a new package.',
    schema: 'This installation package is incomplete or out of date. Return to setup and create a new package.',
    device: 'This installation package cannot be used on this device. Run setup on a supported device or contact your administrator.',
    consumed: 'This installation package has already been used. Return to setup and create a new package.',
  };

  for (const [reason, expected] of Object.entries(expectedMessages)) {
    const { controller, elements } = harness({
      fetch: async () => response(409, { state: 'provisioning_ready', reason }),
    });
    elements.claimPackage.value = VALID_PACKAGE;
    await controller.submitClaim({ preventDefault() {} });
    assert.equal(
      elements.status.textContent,
      expected,
      reason === 'encoding' ? 'encoding_reason_has_dedicated_public_copy' : `${reason}_reason_has_dedicated_public_copy`,
    );
    assert.notEqual(elements.status.textContent, reason, `${reason}_reason_is_not_echoed_raw`);
  }
});

test('unknown and non-string 409 reasons use a non-echoing refusal', async () => {
  for (const reason of ['private_backend_value_947', { internal: 'value' }, 17, null]) {
    const { controller, elements } = harness({
      fetch: async () => response(409, { state: 'provisioning_ready', reason }),
    });
    elements.claimPackage.value = VALID_PACKAGE;
    await controller.submitClaim({ preventDefault() {} });
    assert.doesNotMatch(
      elements.status.textContent,
      /private_backend_value_947/,
      'unknown_reason_is_not_echoed',
    );
    assert.match(elements.status.textContent, /could not accept this request/);
  }
});

test('registration and each successful action advance exactly one accessible step', async () => {
  const fetchCalls = [];
  const { controller, elements } = harness({
    extensionResponse: signedInIdentity(),
    fetch: async (path, options) => {
      fetchCalls.push([path, options]);
      if (path === '/api/v1/provisioning/claim') return response(200, { state: 'installation_registered' });
      if (path === '/api/v1/provisioning/creator-association') {
        return response(200, { association_request_id: 'request-1', status: 'pending', updated_at: 'now' });
      }
      if (path === '/api/v1/provisioning/creator-association/acquire') {
        return response(200, { association_request_id: 'request-1', status: 'approved' });
      }
      if (path === '/api/v1/provisioning/finalize') return response(200, { state: 'configured_restart' });
      throw new Error(`unexpected path ${path}`);
    },
  });

  await controller.refreshIdentity();
  assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
  assert.equal(currentSteps(elements), 1);
  assert.equal(elements.confirmIdentity.disabled, true, 'identity_confirmation_requires_registration');

  elements.claimPackage.value = VALID_PACKAGE;
  await controller.submitClaim({ preventDefault() {} });
  assert.deepEqual(stepStates(elements), ['completed', 'current', 'locked', 'locked']);
  assert.equal(currentSteps(elements), 1);
  assert.equal(elements.claimSubmit.disabled, true);
  assert.equal(elements.confirmIdentity.disabled, false);
  assert.equal(elements.acquireAssociation.disabled, true);
  assert.equal(elements.finalizeProvisioning.disabled, true);

  await controller.confirmIdentity();
  assert.deepEqual(stepStates(elements), ['completed', 'completed', 'current', 'locked']);
  assert.equal(currentSteps(elements), 1);
  assert.equal(elements.confirmIdentity.disabled, true);
  assert.equal(elements.acquireAssociation.disabled, false);
  assert.equal(elements.finalizeProvisioning.disabled, true);

  await controller.acquireAssociation();
  assert.deepEqual(stepStates(elements), ['completed', 'completed', 'completed', 'current']);
  assert.equal(currentSteps(elements), 1);
  assert.equal(elements.acquireAssociation.disabled, true);
  assert.equal(elements.finalizeProvisioning.disabled, false);

  await controller.finalizeProvisioning();
  assert.deepEqual(stepStates(elements), ['completed', 'completed', 'completed', 'completed']);
  assert.equal(currentSteps(elements), 0);
  assert.equal(elements.claimSubmit.disabled, true);
  assert.equal(elements.confirmIdentity.disabled, true);
  assert.equal(elements.acquireAssociation.disabled, true);
  assert.equal(elements.finalizeProvisioning.disabled, true);
  assert.match(elements.status.textContent, /Restart Bridge/);

  for (const [, options] of fetchCalls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-Provisioning-CSRF'], 'csrf-token');
  }
  assert.deepEqual(fetchCalls.map(([path]) => path), [
    '/api/v1/provisioning/claim',
    '/api/v1/provisioning/creator-association',
    '/api/v1/provisioning/creator-association/acquire',
    '/api/v1/provisioning/finalize',
  ]);
});

test('configured restart on arrival completes every step and skips extension detection', async () => {
  const fetchCalls = [];
  const { controller, elements, extensionCalls } = harness({
    extensionResponse: signedInIdentity(),
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, { state: 'configured_restart' });
    },
  });

  await controller.start();

  assert.equal(fetchCalls.length, 1);
  assert.equal(extensionCalls.length, 0, 'configured_restart_does_not_query_or_advance');
  assert.deepEqual(stepStates(elements), ['completed', 'completed', 'completed', 'completed']);
  assert.equal(currentSteps(elements), 0);
  assert.equal(elements.claimSubmit.disabled, true);
  assert.equal(elements.confirmIdentity.disabled, true);
  assert.equal(elements.acquireAssociation.disabled, true);
  assert.equal(elements.finalizeProvisioning.disabled, true);
  assert.match(elements.finalizeActionHelp.textContent, /Restart Bridge/);
});

test('confirm is single-flight and completed actions cannot be repeated', async () => {
  let resolveAssociation;
  const fetchCalls = [];
  const { controller, elements } = harness({
    extensionResponse: signedInIdentity(),
    fetch: (...arguments_) => {
      fetchCalls.push(arguments_);
      if (arguments_[0] === '/api/v1/provisioning/claim') {
        return Promise.resolve(response(200, { state: 'installation_registered' }));
      }
      return new Promise((resolve) => { resolveAssociation = resolve; });
    },
  });
  await controller.refreshIdentity();
  elements.claimPackage.value = VALID_PACKAGE;
  await controller.submitClaim({ preventDefault() {} });

  const first = controller.confirmIdentity();
  const second = controller.confirmIdentity();
  assert.equal(fetchCalls.length, 2, 'confirm_request_is_single_flight');
  assert.deepEqual(JSON.parse(fetchCalls[1][1].body), { detected_creator_account_id: 'creator-42' });
  resolveAssociation(response(200, { association_request_id: 'request-1', status: 'pending', updated_at: 'now' }));
  await Promise.all([first, second]);

  await controller.confirmIdentity();
  await controller.submitClaim({ preventDefault() {} });
  assert.equal(fetchCalls.length, 2, 'completed_actions_are_not_submitted_twice');
  assert.equal(elements.acquireAssociation.disabled, false);
});

test('gated handlers issue no request before their prerequisite succeeds', async () => {
  const fetchCalls = [];
  const { controller } = harness({
    fetch: async (...arguments_) => {
      fetchCalls.push(arguments_);
      return response(200, {});
    },
  });

  await controller.confirmIdentity();
  await controller.acquireAssociation();
  await controller.finalizeProvisioning();

  assert.equal(fetchCalls.length, 0, 'locked_handlers_make_no_requests');
});

test('session, host, and interrupted requests retain actionable guidance', async (context) => {
  for (const status of [401, 403]) {
    await context.test(`${status} restarts provisioning`, async () => {
      const { controller, elements } = harness({ fetch: async () => response(status, {}) });
      elements.claimPackage.value = VALID_PACKAGE;
      await controller.submitClaim({ preventDefault() {} });
      assert.match(elements.status.textContent, /session is no longer valid.*launcher/i);
    });
  }

  await context.test('wrong host names the correct local address', async () => {
    const { controller, elements } = harness({ fetch: async () => response(421, {}) });
    elements.claimPackage.value = VALID_PACKAGE;
    await controller.submitClaim({ preventDefault() {} });
    assert.match(elements.status.textContent, /bridge\.localhost:17871/);
  });

  await context.test('request interruption suggests recovery', async () => {
    const { controller, elements } = harness({ fetch: async () => { throw new Error('offline'); } });
    elements.claimPackage.value = VALID_PACKAGE;
    await controller.submitClaim({ preventDefault() {} });
    assert.match(elements.status.textContent, /Bridge is still running.*try again/i);
  });
});

test('unexpected successful mutation shape is visible and does not advance', async () => {
  for (const body of [{}, null]) {
    const { controller, elements } = harness({ fetch: async () => response(200, body) });
    elements.claimPackage.value = VALID_PACKAGE;

    await controller.submitClaim({ preventDefault() {} });

    assert.match(elements.status.textContent, /unexpected result/);
    assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
    assert.equal(elements.claimSubmit.disabled, false);
  }
});

test('initial status accepts only its exact closed success shape', async (context) => {
  const cases = [
    ['missing', {}],
    ['null', null],
    ['extra', { state: 'provisioning_ready', extra: true }],
    ['wrong-status', { state: 'installation_registered' }],
    ['wrong-type', { state: 17 }],
  ];

  for (const [name, body] of cases) {
    await context.test(name, async () => {
      const { controller, elements } = harness({
        fetch: async () => response(200, body),
      });

      await controller.start();

      assert.equal(
        elements.status.textContent,
        'Bridge returned an unexpected status. Restart provisioning from the launcher.',
        'unexpected_status_guidance_is_visible',
      );
      assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
      assert.equal(elements.confirmIdentity.disabled, true, 'identity_confirmation_remains_locked');
      assert.equal(elements.acquireAssociation.disabled, true, 'approval_remains_locked');
      assert.equal(elements.finalizeProvisioning.disabled, true, 'finalization_remains_locked');
    });
  }
});

test('claim rejects every malformed successful body without advancing', async (context) => {
  const cases = [
    ['missing', {}],
    ['null', null],
    ['extra', { state: 'installation_registered', extra: true }],
    ['wrong-status', { state: 'configured_restart' }],
    ['wrong-type', { state: 17 }],
  ];

  for (const [name, body] of cases) {
    await context.test(name, async () => {
      const { controller, elements } = harness({
        extensionResponse: signedInIdentity(),
        fetch: async (path) => path === '/api/v1/provisioning/status'
          ? response(200, { state: 'provisioning_ready' })
          : response(200, body),
      });

      await controller.start();
      elements.claimPackage.value = VALID_PACKAGE;
      await controller.submitClaim({ preventDefault() {} });

      assert.match(elements.status.textContent, /unexpected result\. Try registering the installation again/);
      assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
      assert.equal(elements.claimSubmit.disabled, false, 'claim_remains_available');
      assert.equal(elements.confirmIdentity.disabled, true, 'identity_confirmation_remains_locked');
      assert.equal(elements.acquireAssociation.disabled, true, 'approval_remains_locked');
      assert.equal(elements.finalizeProvisioning.disabled, true, 'finalization_remains_locked');
    });
  }
});

test('association creation rejects every malformed successful body without advancing', async (context) => {
  const cases = [
    ['missing', { association_request_id: 'request-1', status: 'pending' }],
    ['null', null],
    ['extra', {
      association_request_id: 'request-1', status: 'pending', updated_at: 'now', extra: true,
    }],
    ['wrong-status', {
      association_request_id: 'request-1', status: 'approved', updated_at: 'now',
    }],
    ['wrong-ID', {
      association_request_id: 'x'.repeat(201), status: 'pending', updated_at: 'now',
    }],
    ['wrong-type', {
      association_request_id: 'request-1', status: 'pending', updated_at: 17,
    }],
  ];

  for (const [name, body] of cases) {
    await context.test(name, async () => {
      const { controller, elements } = harness({
        extensionResponse: signedInIdentity(),
        fetch: async (path) => {
          if (path === '/api/v1/provisioning/status') return response(200, { state: 'provisioning_ready' });
          if (path === '/api/v1/provisioning/claim') return response(200, { state: 'installation_registered' });
          return response(200, body);
        },
      });

      await controller.start();
      elements.claimPackage.value = VALID_PACKAGE;
      await controller.submitClaim({ preventDefault() {} });
      await controller.confirmIdentity();

      assert.match(
        elements.status.textContent,
        /unexpected result\. Check the account and try again/,
        'malformed_association_does_not_advance',
      );
      assert.deepEqual(stepStates(elements), ['completed', 'current', 'locked', 'locked']);
      assert.equal(elements.acquireAssociation.disabled, true, 'approval_remains_locked');
      assert.equal(elements.finalizeProvisioning.disabled, true, 'finalization_remains_locked');
    });
  }
});

test('approval rejects every malformed successful body without advancing', async (context) => {
  const cases = [
    ['missing', {}],
    ['null', null],
    ['extra', { association_request_id: 'request-1', status: 'approved', extra: true }],
    ['wrong-status', { association_request_id: 'request-1', status: 'pending' }],
    ['wrong-ID', { association_request_id: 'other-request', status: 'approved' }],
    ['wrong-type', { association_request_id: 17, status: 'approved' }],
  ];

  for (const [name, body] of cases) {
    await context.test(name, async () => {
      const { controller, elements } = harness({
        extensionResponse: signedInIdentity(),
        fetch: async (path) => {
          if (path === '/api/v1/provisioning/status') return response(200, { state: 'provisioning_ready' });
          if (path === '/api/v1/provisioning/claim') return response(200, { state: 'installation_registered' });
          if (path === '/api/v1/provisioning/creator-association') {
            return response(200, { association_request_id: 'request-1', status: 'pending', updated_at: 'now' });
          }
          return response(200, body);
        },
      });

      await controller.start();
      elements.claimPackage.value = VALID_PACKAGE;
      await controller.submitClaim({ preventDefault() {} });
      await controller.confirmIdentity();
      await controller.acquireAssociation();

      assert.match(elements.status.textContent, /unexpected result\. Try acquiring approval again/);
      assert.deepEqual(stepStates(elements), ['completed', 'completed', 'current', 'locked']);
      assert.equal(elements.finalizeProvisioning.disabled, true, 'finalization_remains_locked');
    });
  }
});

test('finalization rejects every malformed successful body without advancing', async (context) => {
  const cases = [
    ['missing', {}],
    ['null', null],
    ['extra', { state: 'configured_restart', extra: true }],
    ['wrong-ID', { state: 'configured_restart', association_request_id: 'other-request' }],
    ['wrong-status', { state: 'installation_registered' }],
    ['wrong-type', { state: 17 }],
  ];

  for (const [name, body] of cases) {
    await context.test(name, async () => {
      const { controller, elements } = harness({
        extensionResponse: signedInIdentity(),
        fetch: async (path) => {
          if (path === '/api/v1/provisioning/status') return response(200, { state: 'provisioning_ready' });
          if (path === '/api/v1/provisioning/claim') return response(200, { state: 'installation_registered' });
          if (path === '/api/v1/provisioning/creator-association') {
            return response(200, { association_request_id: 'request-1', status: 'pending', updated_at: 'now' });
          }
          if (path === '/api/v1/provisioning/creator-association/acquire') {
            return response(200, { association_request_id: 'request-1', status: 'approved' });
          }
          return response(200, body);
        },
      });

      await controller.start();
      elements.claimPackage.value = VALID_PACKAGE;
      await controller.submitClaim({ preventDefault() {} });
      await controller.confirmIdentity();
      await controller.acquireAssociation();
      await controller.finalizeProvisioning();

      assert.match(elements.status.textContent, /unexpected result\. Try finishing configuration again/);
      assert.deepEqual(stepStates(elements), ['completed', 'completed', 'completed', 'current']);
      assert.equal(elements.finalizeProvisioning.disabled, false, 'finalization_remains_available');
    });
  }
});

test('reload after intermediate success returns to the server-reported ready step', async () => {
  const { controller, elements } = harness({
    extensionResponse: signedInIdentity(),
    fetch: async (path) => {
      if (path === '/api/v1/provisioning/status') return response(200, { state: 'provisioning_ready' });
      throw new Error(`unexpected path ${path}`);
    },
  });

  await controller.start();

  assert.deepEqual(stepStates(elements), ['current', 'locked', 'locked', 'locked']);
  assert.equal(elements.confirmIdentity.disabled, true);
  assert.match(elements.identityStatus.textContent, /Register this installation/);
});
