const EXTENSION_ID_PATTERN = /^[a-p]{32}$/;
const CLAIM_PACKAGE_PATTERN = /^[A-Za-z0-9_-]+$/;
const MAX_PACKAGE_CHARACTERS = 1400;
const MAX_ASSOCIATION_REQUEST_ID_CHARACTERS = 200;
const IDENTITY_QUERY = Object.freeze({
  type: 'provisioning.identity.query',
  version: 1,
});

const DECODER_REFUSALS = Object.freeze({
  size: 'This installation package is too large. Return to setup, create a new package, and paste it here.',
  encoding: 'This installation package is incomplete or was changed. Copy it again and paste it without changes.',
  profile: 'This installation package is for a different setup. Return to setup and create a new package.',
  schema: 'This installation package is incomplete or out of date. Return to setup and create a new package.',
  device: 'This installation package cannot be used on this device. Run setup on a supported device or contact your administrator.',
  consumed: 'This installation package has already been used. Return to setup and create a new package.',
});

const GENERIC_REFUSAL = 'Bridge could not accept this request. Check the current step and try again. If it continues, restart provisioning from the launcher.';
const REQUEST_FAILURE = 'Bridge could not complete the request. Check that Bridge is still running, then try again.';
const MUTATION_FAILED = Symbol('mutation failed');

function hasOnlyKeys(value, expected) {
  return Object.keys(value).length === expected.length
    && expected.every((key) => Object.hasOwn(value, key));
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isNonEmptyBoundedString(value, maximum) {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= maximum;
}

function parseStatusResponse(payload) {
  if (!isRecord(payload) || !hasOnlyKeys(payload, ['state'])) return null;
  return payload.state === 'provisioning_ready' || payload.state === 'configured_restart'
    ? payload.state
    : null;
}

function isInstallationRegisteredResponse(payload) {
  return isRecord(payload)
    && hasOnlyKeys(payload, ['state'])
    && payload.state === 'installation_registered';
}

function parseAssociationCreationResponse(payload) {
  if (!isRecord(payload)
    || !hasOnlyKeys(payload, ['association_request_id', 'status', 'updated_at'])
    || !isNonEmptyBoundedString(payload.association_request_id, MAX_ASSOCIATION_REQUEST_ID_CHARACTERS)
    || payload.status !== 'pending'
    || typeof payload.updated_at !== 'string'
    || payload.updated_at.length === 0) return null;
  return payload.association_request_id;
}

function isApprovedAssociationResponse(payload, expectedAssociationRequestId) {
  return isRecord(payload)
    && hasOnlyKeys(payload, ['association_request_id', 'status'])
    && payload.association_request_id === expectedAssociationRequestId
    && payload.status === 'approved';
}

function isConfiguredRestartResponse(payload) {
  return isRecord(payload)
    && hasOnlyKeys(payload, ['state'])
    && payload.state === 'configured_restart';
}

function setAttribute(element, name, value) {
  element.setAttribute?.(name, value);
}

function removeAttribute(element, name) {
  element.removeAttribute?.(name);
}

/**
 * Validate only the package's bounded base64url envelope. The server remains
 * responsible for decoding and validating the package contents.
 */
export function validateClaimPackageInput(rawValue) {
  const value = rawValue.trim();
  if (value.length === 0) {
    return { valid: false, value, message: 'Paste the installation package before submitting.' };
  }
  if (value.length > MAX_PACKAGE_CHARACTERS) {
    return {
      valid: false,
      value,
      message: 'The installation package must be 1,400 characters or fewer. Copy a new package and paste it again.',
    };
  }
  if (!CLAIM_PACKAGE_PATTERN.test(value)) {
    return {
      valid: false,
      value,
      message: 'Paste the package exactly as provided, using only letters, numbers, hyphens, and underscores.',
    };
  }
  if (value.length % 4 === 1) {
    return {
      valid: false,
      value,
      message: 'The installation package appears incomplete. Copy the complete package and paste it again.',
    };
  }
  return { valid: true, value, message: 'Package format is ready to submit.' };
}

export function explainProvisioningFailure(response, payload) {
  if (response.status === 409 && isRecord(payload)) {
    const reason = payload.reason;
    if (typeof reason === 'string' && Object.hasOwn(DECODER_REFUSALS, reason)) {
      return DECODER_REFUSALS[reason];
    }
    return GENERIC_REFUSAL;
  }
  if (response.status === 401 || response.status === 403) {
    return 'The provisioning session is no longer valid. Restart provisioning from the launcher.';
  }
  if (response.status === 421) {
    return 'Open provisioning at bridge.localhost:17871 and continue there.';
  }
  return REQUEST_FAILURE;
}

/**
 * Validate the complete, closed response contract from the provisioning extension.
 * A profile is a candidate for the operator only; it is never an authorization.
 */
export function parseIdentityResponse(response) {
  if (!isRecord(response)
    || !hasOnlyKeys(response, ['type', 'version', 'authenticated_profile'])
    || response.type !== 'provisioning.identity.result'
    || response.version !== 1) return null;

  if (response.authenticated_profile === null) return { accountId: null };
  const profile = response.authenticated_profile;
  if (!isRecord(profile)
    || !hasOnlyKeys(profile, ['creator_account_id'])
    || typeof profile.creator_account_id !== 'string'
    || profile.creator_account_id.length < 1
    || profile.creator_account_id.length > 200) return null;
  return { accountId: profile.creator_account_id };
}

export function createChromeExtensionMessenger(chromeRuntime = globalThis.chrome?.runtime) {
  return (extensionId, message) => new Promise((resolve, reject) => {
    if (chromeRuntime?.sendMessage === undefined) {
      reject(new Error('extension messaging is unavailable'));
      return;
    }
    chromeRuntime.sendMessage(extensionId, message, (response) => {
      if (chromeRuntime.lastError !== undefined) {
        reject(new Error(chromeRuntime.lastError.message));
        return;
      }
      resolve(response);
    });
  });
}

/**
 * Provisioning-page controller. Its explicit action handlers are the only
 * paths that make mutation requests; identity detection only updates the view.
 */
export function createProvisioningController({ fetch, sendExtensionMessage, document, elements }) {
  const csrf = document.querySelector('main')?.dataset.provisioningCsrf ?? '';
  const extensionId = document.querySelector('main')?.dataset.provisioningExtensionId ?? '';
  let detectedAccountId = null;
  let associatedAccountId = null;
  let associationRequestId = null;
  let installationRegistered = false;
  let approvalAcquired = false;
  let mutationInFlight = false;
  let configurationComplete = false;

  const setStatus = (message, error = false) => {
    elements.status.textContent = message;
    elements.status.dataset.tone = error ? 'error' : 'neutral';
  };
  const setIdentityStatus = (message) => { elements.identityStatus.textContent = message; };

  function setStepState(step, stateOutput, state) {
    step.dataset.state = state;
    stateOutput.textContent = state === 'current' ? 'Current step' : state === 'completed' ? 'Completed' : 'Locked';
    if (state === 'current') setAttribute(step, 'aria-current', 'step');
    else removeAttribute(step, 'aria-current');
  }

  function renderState() {
    const stepStates = configurationComplete
      ? ['completed', 'completed', 'completed', 'completed']
      : [
        installationRegistered ? 'completed' : 'current',
        !installationRegistered ? 'locked' : associationRequestId === null ? 'current' : 'completed',
        associationRequestId === null ? 'locked' : approvalAcquired ? 'completed' : 'current',
        !approvalAcquired ? 'locked' : 'current',
      ];
    const steps = [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep];
    const stateOutputs = [
      elements.claimStepState,
      elements.identityStepState,
      elements.bindingStepState,
      elements.finalizeStepState,
    ];
    steps.forEach((step, index) => setStepState(step, stateOutputs[index], stepStates[index]));

    elements.claimPackage.disabled = mutationInFlight || installationRegistered || configurationComplete;
    elements.claimSubmit.disabled = mutationInFlight || installationRegistered || configurationComplete;
    elements.refreshIdentity.disabled = configurationComplete || associationRequestId !== null;
    elements.confirmIdentity.disabled = mutationInFlight
      || configurationComplete
      || !installationRegistered
      || detectedAccountId === null
      || associationRequestId !== null;
    elements.acquireAssociation.disabled = mutationInFlight
      || configurationComplete
      || associationRequestId === null
      || approvalAcquired;
    elements.finalizeProvisioning.disabled = mutationInFlight
      || configurationComplete
      || associationRequestId === null
      || !approvalAcquired;

    if (mutationInFlight) {
      elements.claimActionHelp.textContent = 'Wait for the current request to finish.';
      elements.identityConfirmHelp.textContent = 'Wait for the current request to finish.';
      elements.bindingActionHelp.textContent = 'Wait for the current request to finish.';
      elements.finalizeActionHelp.textContent = 'Wait for the current request to finish.';
    } else if (configurationComplete) {
      const restartHelp = 'Configuration is complete. Restart Bridge before making changes.';
      elements.claimActionHelp.textContent = restartHelp;
      elements.identityConfirmHelp.textContent = restartHelp;
      elements.bindingActionHelp.textContent = restartHelp;
      elements.finalizeActionHelp.textContent = restartHelp;
    } else {
      elements.claimActionHelp.textContent = installationRegistered
        ? 'Registration is complete. Continue to the account step.'
        : 'Available now.';
      elements.identityConfirmHelp.textContent = associationRequestId !== null
        ? 'The account is confirmed. Continue to approval.'
        : !installationRegistered
          ? 'Register this installation before confirming an account.'
          : detectedAccountId === null
            ? 'Check the extension and sign in before confirming an account.'
            : 'The detected account is ready to confirm.';
      elements.bindingActionHelp.textContent = approvalAcquired
        ? 'Approval is acquired. Continue to final configuration.'
        : associationRequestId === null
          ? 'Confirm the creator account before acquiring approval.'
          : 'The confirmed account is ready for approval.';
      elements.finalizeActionHelp.textContent = approvalAcquired
        ? 'Approval is complete. Configuration can now be finished.'
        : 'Acquire approval before finishing configuration.';
    }
  }

  const setBusy = (busy) => {
    mutationInFlight = busy;
    renderState();
  };

  function updatePackageGuidance(markInvalid = true) {
    const result = validateClaimPackageInput(elements.claimPackage.value);
    const characterCount = result.value.length > MAX_PACKAGE_CHARACTERS
      ? `${MAX_PACKAGE_CHARACTERS.toLocaleString('en-US')}+`
      : result.value.length.toLocaleString('en-US');
    elements.claimPackageCount.textContent = `${characterCount} / 1,400 characters`;
    elements.claimPackageValidation.textContent = result.message;
    elements.claimPackageValidation.dataset.valid = result.valid ? 'true' : 'false';
    setAttribute(elements.claimPackage, 'aria-invalid', markInvalid && !result.valid ? 'true' : 'false');
    return result;
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  async function mutate(path, body) {
    if (mutationInFlight || configurationComplete) return MUTATION_FAILED;
    setBusy(true);
    try {
      const response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-Provisioning-CSRF': csrf,
        },
        body: JSON.stringify(body),
      });
      const payload = await readJson(response);
      if (!response.ok) {
        setStatus(explainProvisioningFailure(response, payload), true);
        return MUTATION_FAILED;
      }
      return payload;
    } catch {
      setStatus(REQUEST_FAILURE, true);
      return MUTATION_FAILED;
    } finally {
      setBusy(false);
    }
  }

  async function refreshIdentity() {
    if (configurationComplete || associationRequestId !== null) return;
    detectedAccountId = null;
    elements.detectedIdentity.textContent = 'None detected';
    renderState();
    if (!EXTENSION_ID_PATTERN.test(extensionId)) {
      setIdentityStatus('Install or enable the provisioning extension, then check again.');
      return;
    }
    try {
      const response = await sendExtensionMessage(extensionId, IDENTITY_QUERY);
      const identity = parseIdentityResponse(response);
      if (identity === null) {
        setIdentityStatus('The extension returned an unexpected account response. Install or enable the extension, then check again.');
        return;
      }
      if (identity.accountId === null) {
        setIdentityStatus('Sign in, in a tab, to the account being onboarded, then check again.');
        return;
      }
      detectedAccountId = identity.accountId;
      elements.detectedIdentity.textContent = detectedAccountId;
      setIdentityStatus(installationRegistered
        ? 'Confirm the exact account shown before it is associated.'
        : 'Account detected. Register this installation before confirming it.');
      renderState();
    } catch {
      setIdentityStatus('Install or enable the provisioning extension, then check again.');
    }
  }

  async function checkStatus() {
    try {
      const response = await fetch('/api/v1/provisioning/status', { credentials: 'same-origin' });
      const payload = await readJson(response);
      const state = response.ok ? parseStatusResponse(payload) : null;
      if (state === 'configured_restart') {
        configurationComplete = true;
        setStatus('Configuration is complete. Restart Bridge to continue.');
        setIdentityStatus('Configuration is complete. Restart Bridge before checking the account again.');
        renderState();
      } else if (!response.ok) {
        setStatus(explainProvisioningFailure(response, payload), true);
      } else if (state !== 'provisioning_ready') {
        setStatus('Bridge returned an unexpected status. Restart provisioning from the launcher.', true);
      }
    } catch {
      setStatus('The provisioning status could not be checked. Confirm Bridge is running, then reload this page.', true);
    }
  }

  async function submitClaim(event) {
    event?.preventDefault();
    if (installationRegistered || configurationComplete) return;
    const packageResult = updatePackageGuidance(true);
    if (!packageResult.valid) {
      setStatus(packageResult.message, true);
      return;
    }
    const payload = await mutate('/api/v1/provisioning/claim', { package: packageResult.value });
    if (isInstallationRegisteredResponse(payload)) {
      installationRegistered = true;
      setStatus('Installation registered. Confirm the detected creator account.');
      if (detectedAccountId !== null) {
        setIdentityStatus('Confirm the exact account shown before it is associated.');
      }
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('Bridge returned an unexpected result. Try registering the installation again.', true);
    }
  }

  async function confirmIdentity() {
    if (!installationRegistered || detectedAccountId === null || associationRequestId !== null) return;
    const confirmedAccountId = detectedAccountId;
    const payload = await mutate('/api/v1/provisioning/creator-association', {
      detected_creator_account_id: confirmedAccountId,
    });
    const createdAssociationRequestId = parseAssociationCreationResponse(payload);
    if (createdAssociationRequestId !== null) {
      associationRequestId = createdAssociationRequestId;
      associatedAccountId = confirmedAccountId;
      setStatus('Creator account confirmed. Acquire approval to continue.');
      setIdentityStatus('Account confirmed. Continue to approval.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('Bridge returned an unexpected result. Check the account and try again.', true);
    }
  }

  async function acquireAssociation() {
    if (associationRequestId === null || approvalAcquired) return;
    const payload = await mutate('/api/v1/provisioning/creator-association/acquire', {});
    if (isApprovedAssociationResponse(payload, associationRequestId)) {
      approvalAcquired = true;
      setStatus('Approval acquired. Finish configuration to complete setup.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('Bridge returned an unexpected result. Try acquiring approval again.', true);
    }
  }

  async function finalizeProvisioning() {
    if (associationRequestId === null || associatedAccountId === null || !approvalAcquired) return;
    const payload = await mutate('/api/v1/provisioning/finalize', {
      association_request_id: associationRequestId,
      detected_creator_account_id: associatedAccountId,
    });
    if (isConfiguredRestartResponse(payload)) {
      configurationComplete = true;
      setStatus('Configuration is complete. Restart Bridge to continue.');
      setIdentityStatus('Configuration is complete. Restart Bridge before checking the account again.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('Bridge returned an unexpected result. Try finishing configuration again.', true);
    }
  }

  async function start() {
    elements.claimForm.addEventListener('submit', submitClaim);
    elements.claimPackage.addEventListener('input', () => { updatePackageGuidance(true); });
    elements.refreshIdentity.addEventListener('click', refreshIdentity);
    elements.confirmIdentity.addEventListener('click', confirmIdentity);
    elements.acquireAssociation.addEventListener('click', acquireAssociation);
    elements.finalizeProvisioning.addEventListener('click', finalizeProvisioning);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) void refreshIdentity();
    });
    globalThis.addEventListener?.('focus', () => { void refreshIdentity(); });
    updatePackageGuidance(false);
    renderState();
    await checkStatus();
    if (!configurationComplete) await refreshIdentity();
  }

  return {
    start,
    refreshIdentity,
    confirmIdentity,
    acquireAssociation,
    finalizeProvisioning,
    submitClaim,
  };
}

if (typeof document !== 'undefined') {
  const main = document.querySelector('main[data-provisioning-csrf]');
  if (main !== null) {
    const byId = (id) => document.getElementById(id);
    const controller = createProvisioningController({
      fetch: globalThis.fetch.bind(globalThis),
      sendExtensionMessage: createChromeExtensionMessenger(),
      document,
      elements: {
        status: byId('provisioning-status'),
        identityStatus: byId('identity-status'),
        claimForm: byId('claim-form'),
        claimPackage: byId('claim-package'),
        claimPackageValidation: byId('claim-package-validation'),
        claimPackageCount: byId('claim-package-count'),
        claimSubmit: byId('claim-submit'),
        claimActionHelp: byId('claim-action-help'),
        detectedIdentity: byId('detected-identity'),
        refreshIdentity: byId('refresh-identity'),
        confirmIdentity: byId('confirm-identity'),
        identityConfirmHelp: byId('identity-confirm-help'),
        acquireAssociation: byId('acquire-association'),
        bindingActionHelp: byId('binding-action-help'),
        finalizeProvisioning: byId('finalize-provisioning'),
        finalizeActionHelp: byId('finalize-action-help'),
        claimStep: byId('claim-step'),
        identityStep: byId('identity-step'),
        bindingStep: byId('binding-step'),
        finalizeStep: byId('finalize-step'),
        claimStepState: byId('claim-step-state'),
        identityStepState: byId('identity-step-state'),
        bindingStepState: byId('binding-step-state'),
        finalizeStepState: byId('finalize-step-state'),
      },
    });
    controller.start();
  }
}
