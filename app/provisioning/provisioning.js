const EXTENSION_ID_PATTERN = /^[a-p]{32}$/;
const CLAIM_PACKAGE_PATTERN = /^[A-Za-z0-9_-]+$/;
const MAX_PACKAGE_CHARACTERS = 1400;
const MAX_ASSOCIATION_REQUEST_ID_CHARACTERS = 200;
const IDENTITY_QUERY = Object.freeze({
  type: 'provisioning.identity.query',
  version: 1,
});

const DECODER_REFUSALS = Object.freeze({
  size: 'This setup code is too large. Return to secure setup, create a new code, and paste it here.',
  encoding: 'This setup code is incomplete or was changed. Copy it again and paste it without changes.',
  profile: 'This setup code is for a different setup. Return to secure setup and create a new code.',
  schema: 'This setup code is incomplete or out of date. Return to secure setup and create a new code.',
  device: 'This setup code cannot be used on this computer. Run setup on a supported computer or contact support.',
  consumed: 'This setup code has already been used. Return to secure setup and create a new code.',
});

const OPERATION_REFUSALS = Object.freeze({
  binding_acquisition_unavailable: 'Creator approval is still pending. Complete approval in secure setup, then choose Check approval again.',
  hosted_origin_unavailable: 'Secure setup is not configured for this desktop app. Contact support before continuing.',
  hosted_unavailable: 'Secure setup could not be reached. Check your internet connection, then try again.',
  installation_key_unavailable: 'This computer’s secure device protection is unavailable. Restart the desktop app and try again.',
  membership_reference_unavailable: 'Desktop setup is incomplete. Close this page, reopen the desktop app, and continue setup.',
  candidate_resolution_conflict: 'This setup changed while approval was being checked. Close this page, reopen the desktop app, and continue setup.',
  grant_verification_refused: 'Secure setup could not be verified. Return to secure setup and try again.',
  claim_already_consumed: 'This setup code has already been used. Return to secure setup and create a new code.',
  claim_refused: 'This setup code is no longer valid. Return to secure setup and create a new code.',
  incomplete_grant_set: 'Secure setup did not return everything this computer needs. Return to secure setup and try again.',
  membership_refresh_unavailable: 'The final approval check could not reach secure setup. Check your internet connection and try again.',
});

const GENERIC_REFUSAL = 'This step could not be completed. Check the current step and try again. If it continues, close this page and reopen the desktop app.';
const REQUEST_FAILURE = 'The desktop app could not complete this step. Make sure it is still running, check your internet connection, and try again.';
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
    return { valid: false, value, message: 'Paste the setup code before continuing.' };
  }
  if (value.length > MAX_PACKAGE_CHARACTERS) {
    return {
      valid: false,
      value,
      message: 'The setup code must be 1,400 characters or fewer. Copy a new code and paste it again.',
    };
  }
  if (!CLAIM_PACKAGE_PATTERN.test(value)) {
    return {
      valid: false,
      value,
      message: 'Paste the setup code exactly as provided, using only letters, numbers, hyphens, and underscores.',
    };
  }
  if (value.length % 4 === 1) {
    return {
      valid: false,
      value,
      message: 'The setup code appears incomplete. Copy the complete code and paste it again.',
    };
  }
  return { valid: true, value, message: 'Setup code is ready.' };
}

export function explainProvisioningFailure(response, payload) {
  if ((response.status === 409 || response.status === 503) && isRecord(payload)) {
    const reason = payload.reason;
    if (typeof reason === 'string' && Object.hasOwn(DECODER_REFUSALS, reason)) {
      return DECODER_REFUSALS[reason];
    }
    if (typeof reason === 'string' && Object.hasOwn(OPERATION_REFUSALS, reason)) {
      return OPERATION_REFUSALS[reason];
    }
    return GENERIC_REFUSAL;
  }
  if (response.status === 401 || response.status === 403) {
    return 'This setup page has expired. Close it and reopen the desktop app to continue.';
  }
  if (response.status === 421) {
    return 'Open the desktop app setup page and continue there.';
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
    stateOutput.textContent = state === 'current' ? 'Current step' : state === 'completed' ? 'Done' : 'Complete the step above first';
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
      elements.claimActionHelp.textContent = 'Finishing the current step…';
      elements.identityConfirmHelp.textContent = 'Finishing the current step…';
      elements.bindingActionHelp.textContent = 'Finishing the current step…';
      elements.finalizeActionHelp.textContent = 'Finishing the current step…';
    } else if (configurationComplete) {
      const restartHelp = 'Setup is complete. The desktop app will restart before more changes can be made.';
      elements.claimActionHelp.textContent = restartHelp;
      elements.identityConfirmHelp.textContent = restartHelp;
      elements.bindingActionHelp.textContent = restartHelp;
      elements.finalizeActionHelp.textContent = restartHelp;
    } else {
      elements.claimActionHelp.textContent = installationRegistered
        ? 'This computer is connected. Continue to your creator account.'
        : 'Paste the setup code from secure setup.';
      elements.identityConfirmHelp.textContent = associationRequestId !== null
        ? 'Your creator account is confirmed. Continue to approval.'
        : !installationRegistered
          ? 'Connect this computer before confirming your creator account.'
          : detectedAccountId === null
            ? 'Open OnlyFans, sign in to your creator account, then check again.'
            : 'The signed-in creator account is ready to confirm.';
      elements.bindingActionHelp.textContent = approvalAcquired
        ? 'Creator approval is complete. Continue to finish setup.'
        : associationRequestId === null
          ? 'Confirm your creator account before checking approval.'
          : 'Complete creator approval in secure setup, then check again here.';
      elements.finalizeActionHelp.textContent = approvalAcquired
        ? 'Everything required on this page is complete.'
        : 'Creator approval must complete before desktop setup can finish.';
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
    elements.detectedIdentity.textContent = 'Not detected yet';
    renderState();
    if (!EXTENSION_ID_PATTERN.test(extensionId)) {
      setIdentityStatus('Make sure the Conversation Analytics extension is installed and enabled, then check again.');
      return;
    }
    try {
      const response = await sendExtensionMessage(extensionId, IDENTITY_QUERY);
      const identity = parseIdentityResponse(response);
      if (identity === null) {
        setIdentityStatus('The extension could not identify the signed-in creator account. Make sure it is enabled, then check again.');
        return;
      }
      if (identity.accountId === null) {
        setIdentityStatus('Open OnlyFans in another tab and sign in to the creator account you want to analyze, then check again.');
        return;
      }
      detectedAccountId = identity.accountId;
      elements.detectedIdentity.textContent = 'Signed-in creator account detected';
      setIdentityStatus(installationRegistered
        ? 'Check the OnlyFans tab, then confirm that this is the creator account you want to analyze.'
        : 'Creator account detected. Connect this computer before confirming it.');
      renderState();
    } catch {
      setIdentityStatus('Make sure the Conversation Analytics extension is installed and enabled, then check again.');
    }
  }

  async function checkStatus() {
    try {
      const response = await fetch('/api/v1/provisioning/status', { credentials: 'same-origin' });
      const payload = await readJson(response);
      const state = response.ok ? parseStatusResponse(payload) : null;
      if (state === 'configured_restart') {
        configurationComplete = true;
        setStatus('Desktop setup is complete. The desktop app will restart; then return to the extension.');
        setIdentityStatus('Setup is complete. Return to the extension after the desktop app restarts.');
        renderState();
      } else if (!response.ok) {
        setStatus(explainProvisioningFailure(response, payload), true);
      } else if (state !== 'provisioning_ready') {
        setStatus('Desktop setup returned an unexpected state. Close this page and reopen the desktop app.', true);
      }
    } catch {
      setStatus('Desktop setup could not be checked. Make sure the desktop app is still running, then reload this page.', true);
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
      setStatus('This computer is connected to secure setup. Confirm your signed-in creator account.');
      if (detectedAccountId !== null) {
        setIdentityStatus('Check the OnlyFans tab, then confirm that this is the creator account you want to analyze.');
      }
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('The desktop app returned an unexpected result. Try the setup code again.', true);
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
      setStatus('Creator account confirmed. Complete creator approval in secure setup, then come back and check approval.');
      setIdentityStatus('Creator account confirmed. Continue to approval.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('The desktop app returned an unexpected result. Check the signed-in account and try again.', true);
    }
  }

  async function acquireAssociation() {
    if (associationRequestId === null || approvalAcquired) return;
    const payload = await mutate('/api/v1/provisioning/creator-association/acquire', {});
    if (isApprovedAssociationResponse(payload, associationRequestId)) {
      approvalAcquired = true;
      setStatus('Creator account approved. Finish desktop setup.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('The desktop app returned an unexpected approval result. Check approval again.', true);
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
      setStatus('Desktop setup is complete. The desktop app will restart; then return to the extension.');
      setIdentityStatus('Setup is complete. Return to the extension after the desktop app restarts.');
      renderState();
    } else if (payload !== MUTATION_FAILED) {
      setStatus('The desktop app returned an unexpected result. Try finishing setup again.', true);
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