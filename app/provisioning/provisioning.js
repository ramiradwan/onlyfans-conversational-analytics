const EXTENSION_ID_PATTERN = /^[a-p]{32}$/;
const CLAIM_PACKAGE_PATTERN = /^[A-Za-z0-9_-]+$/;
const MAX_PACKAGE_CHARACTERS = 1400;
const MAX_ASSOCIATION_REQUEST_ID_CHARACTERS = 200;
const PROGRESS_STAGES = new Set([
  'registration_required',
  'creator_confirmation_required',
  'creator_approval_pending',
  'finalization_ready',
  'recovery_required',
]);
const IDENTITY_QUERY = Object.freeze({ type: 'provisioning.identity.query', version: 1 });

const DECODER_REFUSALS = Object.freeze({
  size: 'This code is too long. Get a new code from the setup tab.',
  encoding: 'This code is incomplete or changed. Copy the whole code again.',
  profile: 'This code is for a different setup. Get a new code from the setup tab.',
  schema: 'This code is incomplete or out of date. Get a new code from the setup tab.',
  device: 'This computer cannot use this code. Continue on a supported computer.',
  consumed: 'This code was already used. Get a new code from the setup tab.',
});

const OPERATION_REFUSALS = Object.freeze({
  binding_acquisition_unavailable: 'The connection is not approved yet. Allow it in the setup tab, then try again.',
  hosted_origin_unavailable: 'This app is missing its setup link. Return to the website where you got your code for help.',
  hosted_unavailable: 'The setup service could not complete this step. Try again shortly. If it keeps happening, return to the setup tab for help.',
  installation_key_unavailable: 'This computer’s secure device protection is unavailable. Restart the desktop app and try again.',
  membership_reference_unavailable: 'Desktop setup is incomplete. Close this page, reopen the desktop app, and continue setup.',
  candidate_resolution_conflict: 'This setup changed while approval was being checked. Close this page, reopen the desktop app, and continue setup.',
  grant_verification_refused: 'The connection could not be verified. Return to the setup tab and try again.',
  claim_already_consumed: 'This code was already used. Get a new code from the setup tab.',
  claim_refused: 'This code is no longer valid. Get a new code from the setup tab.',
  incomplete_grant_set: 'Setup did not finish. Return to the setup tab and try again.',
  membership_refresh_unavailable: 'The final check did not finish. Check your internet connection and try again.',
});

const GENERIC_REFUSAL = 'This step could not be completed. Reopen the desktop app and try again.';
const REQUEST_FAILURE = 'The desktop app could not be reached. Make sure it is running and try again.';
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
  return typeof value === 'string' && value.length > 0 && value.length <= maximum;
}

export function parseStatusResponse(payload) {
  if (!isRecord(payload)) return null;
  if (hasOnlyKeys(payload, ['state']) && payload.state === 'configured_restart') {
    return { state: 'configured_restart' };
  }
  if (hasOnlyKeys(payload, ['state']) && payload.state === 'provisioning_ready') {
    return {
      state: 'provisioning_ready',
      stage: 'registration_required',
      association_request_id: null,
      creator_account_id: null,
    };
  }
  if (!hasOnlyKeys(payload, ['state', 'stage', 'association_request_id', 'creator_account_id'])
    || payload.state !== 'provisioning_ready'
    || !PROGRESS_STAGES.has(payload.stage)) return null;
  const coordinatesRequired = ['creator_approval_pending', 'finalization_ready'].includes(payload.stage);
  if (coordinatesRequired) {
    if (!isNonEmptyBoundedString(payload.association_request_id, MAX_ASSOCIATION_REQUEST_ID_CHARACTERS)
      || !isNonEmptyBoundedString(payload.creator_account_id, 200)) return null;
  } else if (payload.association_request_id !== null || payload.creator_account_id !== null) return null;
  return { ...payload };
}

function isInstallationRegisteredResponse(payload) {
  return isRecord(payload) && hasOnlyKeys(payload, ['state'])
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
  return isRecord(payload) && hasOnlyKeys(payload, ['state'])
    && payload.state === 'configured_restart';
}

function setAttribute(element, name, value) { element.setAttribute?.(name, value); }
function removeAttribute(element, name) { element.removeAttribute?.(name); }

export function validateClaimPackageInput(rawValue) {
  const value = rawValue.trim();
  if (value.length === 0) return { valid: false, value, message: 'Paste your setup code.' };
  if (value.length > MAX_PACKAGE_CHARACTERS) return {
    valid: false, value,
    message: 'This code is too long. Copy a new setup code.',
  };
  if (!CLAIM_PACKAGE_PATTERN.test(value)) return {
    valid: false, value,
    message: 'This code contains unexpected characters. Copy the whole setup code again.',
  };
  if (value.length % 4 === 1) return {
    valid: false, value,
    message: 'This code is incomplete. Copy the whole setup code again.',
  };
  return { valid: true, value, message: '' };
}

export function explainProvisioningFailure(response, payload) {
  if ((response.status === 409 || response.status === 503) && isRecord(payload)) {
    const reason = payload.reason;
    if (typeof reason === 'string' && Object.hasOwn(DECODER_REFUSALS, reason)) return DECODER_REFUSALS[reason];
    if (typeof reason === 'string' && Object.hasOwn(OPERATION_REFUSALS, reason)) return OPERATION_REFUSALS[reason];
    return GENERIC_REFUSAL;
  }
  if (response.status === 401 || response.status === 403) return 'This setup page has expired. Close it and reopen the desktop app to continue.';
  if (response.status === 421) return 'Open the desktop app setup page and continue there.';
  return REQUEST_FAILURE;
}

export function parseIdentityResponse(response) {
  if (!isRecord(response)
    || !hasOnlyKeys(response, ['type', 'version', 'authenticated_profile'])
    || response.type !== 'provisioning.identity.result'
    || response.version !== 1) return null;
  if (response.authenticated_profile === null) return { accountId: null };
  const profile = response.authenticated_profile;
  if (!isRecord(profile) || !hasOnlyKeys(profile, ['creator_account_id'])
    || !isNonEmptyBoundedString(profile.creator_account_id, 200)) return null;
  return { accountId: profile.creator_account_id };
}

export function createChromeExtensionMessenger(chromeRuntime = globalThis.chrome?.runtime) {
  return (extensionId, message) => new Promise((resolve, reject) => {
    if (chromeRuntime?.sendMessage === undefined) return reject(new Error('extension messaging is unavailable'));
    chromeRuntime.sendMessage(extensionId, message, (response) => {
      if (chromeRuntime.lastError !== undefined) return reject(new Error(chromeRuntime.lastError.message));
      resolve(response);
    });
  });
}

const DESKTOP_PORT_NAME = 'ofca.desktop';
const EXTENSION_STAGES = new Set([
  'unavailable', 'needs_terms', 'paused', 'needs_full', 'needs_site_access',
  'needs_account', 'ready_to_pair', 'pairing', 'paired',
]);
const EXTENSION_SETUP_STAGES = new Set(['needs_terms', 'paused', 'needs_full', 'needs_site_access']);

export function parseExtensionState(message) {
  if (!isRecord(message) || !hasOnlyKeys(message, ['type', 'version', 'stage', 'attempt'])
    || message.type !== 'state' || message.version !== 1 || !EXTENSION_STAGES.has(message.stage)) return null;
  return message.stage;
}

// The extension pushes its setup stage over a browser port, so this page reacts
// when the extension changes instead of asking the creator to check again.
export function createChromeExtensionPort(chromeRuntime = globalThis.chrome?.runtime) {
  return (extensionId, onStage) => {
    if (typeof chromeRuntime?.connect !== 'function') return null;
    let port = null;
    let closed = false;
    const connect = (retry) => {
      let delivered = false;
      try { port = chromeRuntime.connect(extensionId, { name: DESKTOP_PORT_NAME }); } catch { onStage(null); return; }
      port.onMessage.addListener((message) => {
        const stage = parseExtensionState(message);
        if (stage === null) return;
        delivered = true;
        onStage(stage);
      });
      port.onDisconnect.addListener(() => {
        void chromeRuntime.lastError;
        port = null;
        if (closed) return;
        // A worker that went idle drops the port; reconnecting wakes it.
        if (delivered || retry) connect(false);
        else onStage(null);
      });
    };
    connect(true);
    return {
      open(step) { try { port?.postMessage({ type: 'open', version: 1, step }); } catch {} },
      close() { closed = true; try { port?.disconnect(); } catch {} },
    };
  };
}

export function createProvisioningController({ fetch, sendExtensionMessage, connectExtension, document, elements }) {
  const csrf = document.querySelector('main')?.dataset.provisioningCsrf ?? '';
  const extensionId = document.querySelector('main')?.dataset.provisioningExtensionId ?? '';
  let detectedAccountId = null;
  let associatedAccountId = null;
  let associationRequestId = null;
  let installationRegistered = false;
  let approvalAcquired = false;
  let mutationInFlight = false;
  let configurationComplete = false;
  let recoveryRequired = false;
  let extensionStage = null;
  let extensionPort = null;
  let approvalFailed = false;
  let finalizeFailed = false;
  let finalizeRefused = false;
  let finalizationAttempted = false;
  let finalizationRunning = false;
  let returning = null;

  const setStatus = (message, error = false) => {
    elements.status.textContent = message;
    elements.status.dataset.tone = error ? 'error' : 'neutral';
    placeFeedback();
  };
  const setIdentityStatus = (message) => { elements.identityStatus.textContent = message; };
  const renderExtensionSetup = () => {
    if (elements.openExtensionSetup) {
      elements.openExtensionSetup.hidden = detectedAccountId !== null || !EXTENSION_SETUP_STAGES.has(extensionStage);
      elements.confirmIdentity.hidden = !elements.openExtensionSetup.hidden;
    }
  };
  // With a live extension stage, name the one thing still missing.
  const missingExtensionStep = () => {
    if (EXTENSION_SETUP_STAGES.has(extensionStage)) return 'Finish Full analytics setup in the extension window.';
    if (extensionStage === 'needs_account') return 'Sign in to your creator account on OnlyFans in this browser.';
    return null;
  };

  function setStepState(step, stateOutput, state) {
    step.dataset.state = state;
    const number = [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep].indexOf(step) + 1;
    stateOutput.textContent = `Step ${number} of 4`;
    if (state === 'current') setAttribute(step, 'aria-current', 'step');
    else removeAttribute(step, 'aria-current');
  }

  function renderState() {
    const stepStates = configurationComplete
      ? ['completed', 'completed', 'completed', 'completed']
      : recoveryRequired
        ? ['current', 'locked', 'locked', 'locked']
        : [
          installationRegistered ? 'completed' : 'current',
          !installationRegistered ? 'locked' : associationRequestId === null ? 'current' : 'completed',
          associationRequestId === null ? 'locked' : approvalAcquired ? 'completed' : 'current',
          !approvalAcquired ? 'locked' : 'current',
        ];
    const steps = [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep];
    const outputs = [elements.claimStepState, elements.identityStepState, elements.bindingStepState, elements.finalizeStepState];
    steps.forEach((step, index) => setStepState(step, outputs[index], stepStates[index]));
    document.querySelectorAll?.('[data-rail-step]').forEach((item, index) => { item.dataset.state = recoveryRequired ? 'locked' : stepStates[index]; });
    const stage = document.querySelector('.provisioning-stage');
    if (stage) stage.dataset.complete = String(configurationComplete);
    const heading = document.querySelector('#finalize-heading');
    if (heading) heading.textContent = configurationComplete ? 'Setup finished' : 'Finish desktop setup';
    elements.finalizeActionHelp.textContent = configurationComplete
      ? 'The desktop app is restarting. Continue there when it opens.'
      : finalizeFailed ? finalizeRefused ? 'Setup did not finish. Try again.' : 'Setup completion could not be confirmed. Try again.'
        : approvalAcquired ? 'Finishing setup…' : 'The desktop app restarts when setup is finished.';
    if (configurationComplete) elements.finalizeStep.dataset.state = 'completed';
    elements.acquireAssociation.hidden = !approvalFailed;
    elements.finalizeProvisioning.hidden = !finalizeFailed;
    const valid = validateClaimPackageInput(elements.claimPackage.value).valid;
    elements.claimStep.dataset.codeValid = String(valid);

    elements.claimPackage.disabled = recoveryRequired || mutationInFlight || installationRegistered || configurationComplete;
    elements.claimSubmit.disabled = !valid || recoveryRequired || mutationInFlight || installationRegistered || configurationComplete;
    elements.refreshIdentity.disabled = recoveryRequired || configurationComplete || associationRequestId !== null;
    elements.confirmIdentity.disabled = recoveryRequired || mutationInFlight || configurationComplete
      || !installationRegistered || detectedAccountId === null || associationRequestId !== null;
    elements.acquireAssociation.disabled = recoveryRequired || mutationInFlight || configurationComplete
      || associationRequestId === null || approvalAcquired;
    elements.finalizeProvisioning.disabled = recoveryRequired || mutationInFlight || configurationComplete
      || associationRequestId === null || !approvalAcquired;

    // Each step owns one instruction. Outcomes appear beside its controls.
    elements.claimStep.dataset.recovery = String(recoveryRequired);
    for (const step of steps) setAttribute(step, 'aria-busy', String(mutationInFlight && step.dataset.state === 'current'));
    placeFeedback();
  }

  function placeFeedback() {
    const steps = [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep];
    const active = steps.find((step) => step.dataset.state === 'current') ?? elements.claimStep;
    for (const step of steps) step.dataset.feedback = step === active && elements.status.textContent ? 'true' : 'false';
    const target = configurationComplete ? elements.finalizeStep.querySelector?.('[data-feedback-content]')
      : active.querySelector?.('[data-feedback-content]');
    if (target && elements.status.parentElement !== target) target.append(elements.status);
    const summary = document.querySelector('#feedback-details');
    if (summary && target) {
      const validation = active === elements.claimStep ? elements.claimPackageValidation.textContent : '';
      const message = elements.status.textContent || validation;
      summary.hidden = message.length <= 60;
      elements.status.dataset.long = String(!summary.hidden && Boolean(elements.status.textContent));
      elements.claimPackageValidation.dataset.long = String(!summary.hidden && !elements.status.textContent);
      const copy = document.querySelector('#feedback-copy');
      if (copy) copy.textContent = message;
      if (summary.parentElement !== target) target.append(summary);
    }
    const link = elements.bindingStep.querySelector?.('#continue-creator-approval');
    setAttribute(elements.acquireAssociation, 'aria-describedby', elements.status.textContent
      ? 'provisioning-status' : link && !link.hidden ? 'binding-step-description' : 'creator-approval-unavailable');
  }

  function focusCurrentStep() {
    const heading = document.querySelector('[aria-current="step"] h2');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
  }

  const setBusy = (busy) => { mutationInFlight = busy; renderState(); };

  function applyProgress(progress) {
    recoveryRequired = false;
    if (progress.stage === 'registration_required') {
      installationRegistered = false;
      associationRequestId = null;
      associatedAccountId = null;
      approvalAcquired = false;
      setStatus('');
    } else if (progress.stage === 'creator_confirmation_required') {
      installationRegistered = true;
      associationRequestId = null;
      associatedAccountId = null;
      approvalAcquired = false;
      setStatus('');
    } else if (progress.stage === 'creator_approval_pending') {
      installationRegistered = true;
      associationRequestId = progress.association_request_id;
      associatedAccountId = progress.creator_account_id;
      approvalAcquired = false;
      setStatus('');
      setIdentityStatus('');

    } else if (progress.stage === 'finalization_ready') {
      installationRegistered = true;
      associationRequestId = progress.association_request_id;
      associatedAccountId = progress.creator_account_id;
      approvalAcquired = true;
      setStatus('');
      setIdentityStatus('');

    } else {
      recoveryRequired = true;
      installationRegistered = false;
      associationRequestId = null;
      associatedAccountId = null;
      approvalAcquired = false;
      setStatus('Setup was interrupted. Restart the desktop app. Do not reuse this setup code.', true);
    }
    renderState();
  }

  function updatePackageGuidance(markInvalid = true) {
    const result = validateClaimPackageInput(elements.claimPackage.value);
    elements.claimPackageCount.textContent = '';
    elements.claimStep.dataset.codeValid = String(result.valid);
    elements.claimSubmit.disabled = !result.valid || mutationInFlight || installationRegistered || recoveryRequired || configurationComplete;
    elements.claimPackageValidation.textContent = markInvalid ? result.valid ? 'Setup code pasted.' : result.message : '';
    elements.claimPackageValidation.dataset.valid = result.valid || !markInvalid ? 'true' : 'false';
    setAttribute(elements.claimPackage, 'aria-invalid', markInvalid && !result.valid ? 'true' : 'false');
    placeFeedback();
    return result;
  }

  async function readJson(response) { try { return await response.json(); } catch { return null; } }

  async function mutate(path, body, { neutralReasons = [] } = {}) {
    if (recoveryRequired || mutationInFlight || configurationComplete) return MUTATION_FAILED;
    setStatus(path.endsWith('/acquire') ? 'Checking approval…' : path.endsWith('/finalize') ? 'Finishing setup…' : path.endsWith('/claim') ? 'Checking code…' : '');
    setBusy(true);
    try {
      const response = await fetch(path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Provisioning-CSRF': csrf },
        body: JSON.stringify(body),
      });
      const payload = await readJson(response);
      if (!response.ok) {
        if (path.endsWith('/finalize')) finalizeRefused = response.status >= 400 && response.status < 500;
        if (response.status === 401 || response.status === 403) recoveryRequired = true;
        const reason = isRecord(payload) && typeof payload.reason === 'string' ? payload.reason : null;
        setStatus(explainProvisioningFailure(response, payload), !neutralReasons.includes(reason));
        return MUTATION_FAILED;
      }
      return payload;
    } catch {
      setStatus(REQUEST_FAILURE, true); return MUTATION_FAILED;
    } finally { setBusy(false); }
  }

  async function refreshIdentity() {
    if (recoveryRequired || configurationComplete || associationRequestId !== null) return;
    detectedAccountId = null;

    renderState();
    if (!EXTENSION_ID_PATTERN.test(extensionId)) {
      setIdentityStatus('Enable the Conversation Analytics extension, then check again.'); renderExtensionSetup(); return;
    }
    try {
      const identity = parseIdentityResponse(await sendExtensionMessage(extensionId, IDENTITY_QUERY));
      if (identity === null) {
        setIdentityStatus(missingExtensionStep() ?? 'The extension could not find your account. Sign in on OnlyFans, then check again.');
        renderExtensionSetup(); return;
      }
      if (identity.accountId === null) {
        setIdentityStatus(missingExtensionStep() ?? 'Sign in to your creator account on OnlyFans, then check again.');
        renderExtensionSetup(); return;
      }
      detectedAccountId = identity.accountId;

      setIdentityStatus(installationRegistered
        ? 'Choose the creator account this computer should use.'
        : 'Connect this computer first.');
      renderState(); renderExtensionSetup();
    } catch {
      setIdentityStatus(missingExtensionStep() ?? 'Enable the Conversation Analytics extension, then check again.');
      renderExtensionSetup();
    }
  }

  async function checkStatus() {
    try {
      const response = await fetch('/api/v1/provisioning/status', { credentials: 'same-origin' });
      const payload = await readJson(response);
      const progress = response.ok ? parseStatusResponse(payload) : null;
      if (progress?.state === 'configured_restart') {
        configurationComplete = true;
        setStatus('The desktop app is restarting. Continue there when it opens.');
        setIdentityStatus('');
        renderState();
      } else if (!response.ok) setStatus(explainProvisioningFailure(response, payload), true);
      else if (progress?.state === 'provisioning_ready') applyProgress(progress);
      else setStatus('Setup could not be checked. Reopen the desktop app.', true);
      return progress;
    } catch {
      setStatus('Make sure the desktop app is running, then reload this page.', true);
      return null;
    }
  }

  async function submitClaim(event) {
    event?.preventDefault();
    if (recoveryRequired || installationRegistered || configurationComplete) return;
    const packageResult = updatePackageGuidance(true);
    if (!packageResult.valid) { setStatus(''); elements.claimPackage.focus?.(); return; }
    const payload = await mutate('/api/v1/provisioning/claim', { package: packageResult.value });
    if (isInstallationRegisteredResponse(payload)) {
      installationRegistered = true;
      setStatus('');
      if (detectedAccountId !== null) setIdentityStatus('Choose the creator account this computer should use.');
      renderState();
      focusCurrentStep();
    } else if (payload !== MUTATION_FAILED) setStatus('The code could not be checked. Try again.', true);
  }

  async function confirmIdentity() {
    if (recoveryRequired || !installationRegistered || detectedAccountId === null || associationRequestId !== null) return;
    const confirmedAccountId = detectedAccountId;
    const payload = await mutate('/api/v1/provisioning/creator-association', { detected_creator_account_id: confirmedAccountId });
    const created = parseAssociationCreationResponse(payload);
    if (created !== null) {
      associationRequestId = created; associatedAccountId = confirmedAccountId;
      setStatus('');
      setIdentityStatus(''); renderState(); focusCurrentStep();
    } else if (payload !== MUTATION_FAILED) setStatus('The account could not be confirmed. Check the OnlyFans tab and try again.', true);
  }

  async function acquireAssociation() {
    if (recoveryRequired || mutationInFlight || configurationComplete || associationRequestId === null || approvalAcquired) return;
    approvalFailed = false;
    const payload = await mutate(
      '/api/v1/provisioning/creator-association/acquire',
      {},
      { neutralReasons: ['binding_acquisition_unavailable'] },
    );
    if (isApprovedAssociationResponse(payload, associationRequestId)) {
      approvalAcquired = true; setStatus(''); renderState(); focusCurrentStep();
    } else {
      approvalFailed = true;
      if (payload !== MUTATION_FAILED) setStatus('The connection could not be checked. Try again.', true);
      renderState();
    }
  }

  async function acquireAndFinish() {
    await acquireAssociation();
    if (approvalAcquired) await finalizeProvisioning();
  }

  async function finalizeProvisioning() {
    if (finalizationRunning || recoveryRequired || mutationInFlight || configurationComplete || associationRequestId === null || associatedAccountId === null || !approvalAcquired) return;
    finalizationRunning = true;
    try {
      if (finalizationAttempted) {
        const progress = await checkStatus();
        if (progress?.stage !== 'finalization_ready' || configurationComplete || recoveryRequired) return;
      }
      finalizationAttempted = true;
      finalizeFailed = false;
      finalizeRefused = false;
      const payload = await mutate('/api/v1/provisioning/finalize', {
        association_request_id: associationRequestId, detected_creator_account_id: associatedAccountId,
      });
      if (isConfiguredRestartResponse(payload)) {
        configurationComplete = true;
        setStatus('The desktop app is restarting. Continue there when it opens.');
        setIdentityStatus(''); renderState(); focusCurrentStep();
      } else {
        finalizeFailed = true;
        if (payload !== MUTATION_FAILED) setStatus('Setup did not finish. Try again.', true);
        renderState();
      }
    } finally { finalizationRunning = false; }
  }

  function onReturn() {
    if (document.hidden || recoveryRequired || configurationComplete || mutationInFlight) return returning;
    if (returning) return returning;
    returning = (associationRequestId === null ? refreshIdentity() : approvalAcquired ? Promise.resolve() : acquireAndFinish()).finally(() => { returning = null; });
    return returning;
  }

  async function start() {
    elements.claimForm.addEventListener('submit', submitClaim);
    elements.claimPackage.addEventListener('input', () => { setStatus(''); updatePackageGuidance(true); });
    elements.refreshIdentity.addEventListener('click', refreshIdentity);
    elements.confirmIdentity.addEventListener('click', confirmIdentity);
    elements.acquireAssociation.addEventListener('click', acquireAndFinish);
    elements.finalizeProvisioning.addEventListener('click', finalizeProvisioning);
    document.addEventListener('visibilitychange', onReturn);
    (document.defaultView ?? globalThis).addEventListener?.('focus', onReturn);
    const dialog = document.querySelector('#recovery-dialog');
    document.querySelector('#recovery-open')?.addEventListener('click', () => dialog?.showModal());
    document.querySelector('#recovery-close')?.addEventListener('click', () => dialog?.close());
    document.querySelector('#feedback-details-open')?.addEventListener('click', () => document.querySelector('#feedback-dialog')?.showModal());
    elements.openExtensionSetup?.addEventListener('click', () => extensionPort?.open('setup'));
    if (EXTENSION_ID_PATTERN.test(extensionId) && typeof connectExtension === 'function') {
      extensionPort = connectExtension(extensionId, (stage) => {
        extensionStage = stage;
        // Pushed stages keep this step current, so a manual re-check is only
        // offered when no extension answers in this browser.
        elements.refreshIdentity.hidden = stage !== null;
        if (!configurationComplete && !recoveryRequired && associationRequestId === null) void refreshIdentity();
        else renderExtensionSetup();
      });
    }
    updatePackageGuidance(false); renderState();
    await checkStatus();
    if (approvalAcquired && !configurationComplete && !recoveryRequired) void finalizeProvisioning();
    if (!configurationComplete && !recoveryRequired && associationRequestId === null) await refreshIdentity();
  }

  return { start, refreshIdentity, confirmIdentity, acquireAssociation, finalizeProvisioning, submitClaim, checkStatus };
}

if (typeof document !== 'undefined') {
  const main = document.querySelector('main[data-provisioning-csrf]');
  if (main !== null) {
    const byId = (id) => document.getElementById(id);
    const controller = createProvisioningController({
      fetch: globalThis.fetch.bind(globalThis),
      sendExtensionMessage: createChromeExtensionMessenger(),
      connectExtension: createChromeExtensionPort(),
      document,
      elements: {
        status: byId('provisioning-status'), identityStatus: byId('identity-status'), claimForm: byId('claim-form'),
        claimPackage: byId('claim-package'), claimPackageValidation: byId('claim-package-validation'),
        claimPackageCount: byId('claim-package-count'), claimSubmit: byId('claim-submit'),
        claimActionHelp: byId('claim-step-description'),
        refreshIdentity: byId('refresh-identity'), confirmIdentity: byId('confirm-identity'),
        openExtensionSetup: byId('open-extension-setup'),
        identityConfirmHelp: byId('identity-step-description'), acquireAssociation: byId('acquire-association'),
        bindingActionHelp: byId('binding-step-description'), finalizeProvisioning: byId('finalize-provisioning'),
        finalizeActionHelp: byId('finalize-step-description'), claimStep: byId('claim-step'), identityStep: byId('identity-step'),
        bindingStep: byId('binding-step'), finalizeStep: byId('finalize-step'), claimStepState: byId('claim-step-state'),
        identityStepState: byId('identity-step-state'), bindingStepState: byId('binding-step-state'),
        finalizeStepState: byId('finalize-step-state'),
      },
    });
    controller.start();
  }
}
