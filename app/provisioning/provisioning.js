const EXTENSION_ID_PATTERN = /^[a-p]{32}$/;
const IDENTITY_QUERY = Object.freeze({
  type: 'provisioning.identity.query',
  version: 1,
});

function hasOnlyKeys(value, expected) {
  return Object.keys(value).length === expected.length
    && expected.every((key) => Object.hasOwn(value, key));
}

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
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
  let mutationInFlight = false;
  let configurationComplete = false;

  const setStatus = (message) => { elements.status.textContent = message; };
  const setIdentityStatus = (message) => { elements.identityStatus.textContent = message; };
  const setBusy = (busy) => {
    mutationInFlight = busy;
    elements.claimSubmit.disabled = busy || configurationComplete;
    elements.confirmIdentity.disabled = busy || configurationComplete || detectedAccountId === null || associationRequestId !== null;
    elements.acquireAssociation.disabled = busy || configurationComplete || associationRequestId === null;
    elements.finalizeProvisioning.disabled = busy || configurationComplete || associationRequestId === null || elements.acquireAssociation.dataset.acquired !== 'true';
  };

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  function explainFailure(response, payload) {
    if (response.status === 409 && isRecord(payload) && typeof payload.reason === 'string') {
      return payload.reason;
    }
    if (response.status === 401 || response.status === 403) {
      return 'The provisioning session is no longer valid. Restart provisioning from the launcher.';
    }
    if (response.status === 421) {
      return 'Open provisioning at bridge.localhost:17871.';
    }
    return 'The provisioning request could not be completed.';
  }

  async function mutate(path, body) {
    if (mutationInFlight || configurationComplete) return null;
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
        setStatus(explainFailure(response, payload));
        return null;
      }
      return payload;
    } catch {
      setStatus('The provisioning request could not be completed.');
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function refreshIdentity() {
    if (configurationComplete) return;
    if (associationRequestId === null) {
      detectedAccountId = null;
      elements.detectedIdentity.textContent = 'None detected';
    }
    setBusy(mutationInFlight);
    if (!EXTENSION_ID_PATTERN.test(extensionId)) {
      setIdentityStatus('Install or enable the provisioning extension, then check again.');
      return;
    }
    try {
      const response = await sendExtensionMessage(extensionId, IDENTITY_QUERY);
      const identity = parseIdentityResponse(response);
      if (associationRequestId !== null) {
        setIdentityStatus('The confirmed account is already being associated.');
        return;
      }
      if (identity === null) {
        setIdentityStatus('The extension returned an unexpected identity response. Install or enable the extension, then check again.');
        return;
      }
      if (identity.accountId === null) {
        setIdentityStatus('Sign in, in a tab, to the account being onboarded, then check again.');
        return;
      }
      detectedAccountId = identity.accountId;
      elements.detectedIdentity.textContent = detectedAccountId;
      setIdentityStatus('Confirm the exact account shown before it is associated.');
      setBusy(false);
    } catch {
      setIdentityStatus('Install or enable the provisioning extension, then check again.');
    }
  }

  async function checkStatus() {
    try {
      const response = await fetch('/api/v1/provisioning/status', { credentials: 'same-origin' });
      const payload = await readJson(response);
      if (response.ok && isRecord(payload) && payload.state === 'configured_restart') {
        configurationComplete = true;
        setBusy(false);
        setStatus('Configuration is complete. Restart Bridge to continue.');
      } else if (!response.ok) {
        setStatus(explainFailure(response, payload));
      }
    } catch {
      setStatus('The provisioning status could not be checked.');
    }
  }

  async function submitClaim(event) {
    event?.preventDefault();
    const packageValue = elements.claimPackage.value.trim();
    if (packageValue.length === 0) {
      setStatus('Paste the installation package before submitting.');
      return;
    }
    const payload = await mutate('/api/v1/provisioning/claim', { package: packageValue });
    if (payload?.state === 'installation_registered') setStatus('Installation registered.');
  }

  async function confirmIdentity() {
    if (detectedAccountId === null || associationRequestId !== null) return;
    const confirmedAccountId = detectedAccountId;
    const payload = await mutate('/api/v1/provisioning/creator-association', {
      detected_creator_account_id: confirmedAccountId,
    });
    if (isRecord(payload) && typeof payload.association_request_id === 'string') {
      associationRequestId = payload.association_request_id;
      associatedAccountId = confirmedAccountId;
      setStatus(`Association request ${associationRequestId} created.`);
      setBusy(false);
    }
  }

  async function acquireAssociation() {
    if (associationRequestId === null) return;
    const payload = await mutate('/api/v1/provisioning/creator-association/acquire', {});
    if (isRecord(payload) && typeof payload.association_request_id === 'string') {
      associationRequestId = payload.association_request_id;
      elements.acquireAssociation.dataset.acquired = 'true';
      setStatus('Association approval acquired.');
      setBusy(false);
    }
  }

  async function finalizeProvisioning() {
    if (associationRequestId === null || associatedAccountId === null || elements.acquireAssociation.dataset.acquired !== 'true') return;
    const payload = await mutate('/api/v1/provisioning/finalize', {
      association_request_id: associationRequestId,
      detected_creator_account_id: associatedAccountId,
    });
    if (payload?.state === 'configured_restart') {
      configurationComplete = true;
      setBusy(false);
      setStatus('Configuration is complete. Restart Bridge to continue.');
    }
  }

  async function start() {
    elements.claimForm.addEventListener('submit', submitClaim);
    elements.refreshIdentity.addEventListener('click', refreshIdentity);
    elements.confirmIdentity.addEventListener('click', confirmIdentity);
    elements.acquireAssociation.addEventListener('click', acquireAssociation);
    elements.finalizeProvisioning.addEventListener('click', finalizeProvisioning);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) void refreshIdentity();
    });
    globalThis.addEventListener?.('focus', () => { void refreshIdentity(); });
    await checkStatus();
    if (!configurationComplete) await refreshIdentity();
  }

  return { start, refreshIdentity, confirmIdentity, acquireAssociation, finalizeProvisioning, submitClaim };
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
        claimSubmit: document.querySelector('#claim-form button[type="submit"]'),
        detectedIdentity: byId('detected-identity'),
        refreshIdentity: byId('refresh-identity'),
        confirmIdentity: byId('confirm-identity'),
        acquireAssociation: byId('acquire-association'),
        finalizeProvisioning: byId('finalize-provisioning'),
      },
    });
    controller.start();
  }
}
