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
  binding_acquisition_unavailable: 'Approval could not be checked. Try again.',
  hosted_origin_unavailable: 'This app is missing its setup link. Return to the website where you got your code for help.',
  hosted_unavailable: 'Setup is unavailable. Try again shortly.',
  installation_key_unavailable: 'This computer’s secure device protection is unavailable. Restart the desktop app and try again.',
  membership_reference_unavailable: 'Desktop setup is incomplete. Close this page, reopen the desktop app, and continue setup.',
  candidate_resolution_conflict: 'This setup changed while approval was being checked. Close this page, reopen the desktop app, and continue setup.',
  grant_verification_refused: 'The connection could not be verified. Return to the setup tab and try again.',
  claim_already_consumed: 'This code was already used. Get a new code from the setup tab.',
  claim_refused: 'This code is no longer valid. Get a new code from the setup tab.',
  incomplete_grant_set: 'Setup did not finish. Return to the setup tab and try again.',
  membership_refresh_unavailable: 'The final check could not be completed. Try again.',
});

const GENERIC_REFUSAL = 'This step could not be completed.';
const REQUEST_FAILURE = 'The desktop app could not be reached. Make sure it is running and try again.';
const MUTATION_FAILED = Symbol('mutation failed');
const MUTATION_RETIRED = Symbol('mutation retired');
const JOURNEY_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const HOSTED_CREATOR_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;
const CONTINUATION_STATES = new Set(['new', 'prepare-unknown', 'waiting', 'completing', 'unknown',
  'completed', 'expired', 'revoked', 'authentication-required']);

/** Only the registered local workspace identifier; never an authentication value. */
export function parseJourneyHash(hash) {
  const value = typeof hash === 'string' && hash.startsWith('#journey=') ? hash.slice(9) : '';
  return JOURNEY_PATTERN.test(value) ? value : null;
}

export function parseBrainOnboardingState(value) {
  if (!isRecord(value) || !hasOnlyKeys(value, ['profile', 'kind', 'journey_id', 'source', 'epoch', 'revision',
    'account_generation', 'consent_generation', 'facts', 'pending_operation', 'reason'])
    || value.profile !== 'local-onboarding-state.v1' || value.source !== 'brain'
    || !['snapshot', 'event'].includes(value.kind) || !JOURNEY_PATTERN.test(value.journey_id)
    || !JOURNEY_PATTERN.test(value.epoch)
    || ![value.revision, value.account_generation, value.consent_generation].every((n) => Number.isSafeInteger(n) && n >= 0)
    || !isRecord(value.facts) || !hasOnlyKeys(value.facts, ['installation', 'enrollment', 'pairing', 'activation', 'analysis'])
    || !Object.values(value.facts).every((fact) => ['unknown', 'missing', 'verified'].includes(fact))
    || !['none', 'app_unreachable', 'enrollment_required', 'pairing_required', 'activation_required',
      'authorization_expired', 'authorization_revoked', 'operation_unconfirmed'].includes(value.reason)) return null;
  const pending = value.pending_operation;
  if (pending !== null && (!isRecord(pending) || !hasOnlyKeys(pending, ['operation_id', 'status'])
    || !JOURNEY_PATTERN.test(pending.operation_id) || !['pending', 'unknown'].includes(pending.status))) return null;
  return value;
}

export function submitHostedHandoff({ document, payload, journeyId, registeredHostedUrl, intendedCreatorId }) {
  const keys = ['state', 'journey_id', 'handoff_reference', 'hosted_start_url'];
  const continuation = payload?.continuation_reference;
  if (continuation !== undefined) keys.push('continuation_reference');
  // A receiving continuation derives its creator from the hosted receiver's
  // authorized context. Fresh entry carries only an observed, nonauthorizing hint.
  if (continuation === undefined ? typeof intendedCreatorId !== 'string' || !HOSTED_CREATOR_PATTERN.test(intendedCreatorId)
    : intendedCreatorId !== undefined) return false;
  if (!isRecord(payload) || !hasOnlyKeys(payload, keys)
    || (continuation !== undefined && !/^[A-Za-z0-9_-]{43}$/u.test(continuation))
    || payload.state !== 'waiting' || payload.journey_id !== journeyId || !JOURNEY_PATTERN.test(journeyId)
    || typeof payload.handoff_reference !== 'string' || !/^[A-Za-z0-9_-]{43}$/u.test(payload.handoff_reference)) return false;
  let registered;
  try { registered = new URL(registeredHostedUrl); } catch { return false; }
  if (registered.protocol !== 'https:' || registered.username || registered.password || registered.search || registered.hash
    || registered.pathname !== '/public/onboarding/setup'
    || payload.hosted_start_url !== `${registered.origin}/public/onboarding/setup/start`) return false;
  const form = document.createElement('form');
  form.method = 'post'; form.action = payload.hosted_start_url; form.target = '_self'; form.hidden = true;
  // Submission carries only bounded nonauthorizing entry context. No proof,
  // claim/bootstrap authority, local session or return URL leaves this page.
  for (const [name, value] of Object.entries({ journey_id: journeyId, handoff_reference: payload.handoff_reference,
    ...(continuation === undefined ? { intended_creator_id: intendedCreatorId } : { continuation_reference: continuation }) })) {
    const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
  return true;
}

export function submitHostedContinuation({ document, payload, journeyId, registeredHostedUrl }) {
  if (!isRecord(payload) || !hasOnlyKeys(payload, ['state', 'journey_id', 'continuation_reference', 'hosted_start_url'])
    || payload.state !== 'waiting' || payload.journey_id !== journeyId || !JOURNEY_PATTERN.test(journeyId)
    || typeof payload.continuation_reference !== 'string' || !/^[A-Za-z0-9_-]{43}$/u.test(payload.continuation_reference)) return false;
  let registered;
  try { registered = new URL(registeredHostedUrl); } catch { return false; }
  if (registered.protocol !== 'https:' || registered.username || registered.password || registered.search || registered.hash
    || registered.pathname !== '/public/onboarding/setup'
    || payload.hosted_start_url !== `${registered.origin}/public/onboarding/installation-continue`) return false;
  const form = document.createElement('form');
  form.method = 'post'; form.action = payload.hosted_start_url; form.target = '_self'; form.hidden = true;
  for (const [name, value] of Object.entries({ journey_id: journeyId, continuation_reference: payload.continuation_reference })) {
    const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
  }
  document.body.append(form); form.submit(); return true;
}

/** Automatic same-workspace relay; all authority stays in the local cookie/CSRF. */
export async function continueReceivingTransfer({ fetch, document, journeyId, registeredHostedUrl, onStatus, onRecovery = () => {},
  loadParser = () => import('/provisioning/onboarding/json.mjs'), timeoutMs = 10_000,
  current = () => true, readOnly = false }) {
  const prefix = '/api/v1/provisioning/setup-transfer';
  let submitted = false;
  const fail = () => { if (!current()) return 'retired'; onStatus('Setup could not be continued.'); return 'unavailable'; };
  try {
    const { parseOnboardingJson } = await loadParser();
    const read = async (path, body, csrf) => {
      if (!current()) throw new Error('Page retired');
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), timeoutMs);
      try {
        const response = await fetch(path, { method: body ? 'POST' : 'GET', credentials: 'same-origin',
          cache: 'no-store', redirect: 'error', signal: abort.signal,
          headers: { Accept: 'application/json', 'X-Onboarding-Journey': journeyId,
            ...(body ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf } : {}) },
          ...(body ? { body: JSON.stringify(body) } : {}) });
        if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') {
          throw new Error('Transfer unavailable');
        }
        const text = await response.text();
        if (!current()) throw new Error('Page retired');
        return parseOnboardingJson(text);
      } finally { clearTimeout(timer); }
    };
    const context = await read(`${prefix}/context`);
    if (!current()) return 'retired';
    if (isRecord(context) && hasOnlyKeys(context, ['state']) && context.state === 'none') return 'none';
    if (!isRecord(context) || context.journey_id !== journeyId || !JOURNEY_PATTERN.test(journeyId)) return fail();
    const registered = new URL(registeredHostedUrl);
    if (registered.protocol !== 'https:' || registered.username || registered.password || registered.search || registered.hash
      || registered.pathname !== '/public/onboarding/setup') return fail();
    if (context.state === 'unconfirmed' && hasOnlyKeys(context, ['state', 'journey_id', 'hosted_return_url'])
      && context.hosted_return_url === registered.href) {
      onStatus('Setup could not be confirmed.');
      onRecovery('Return to setup', () => { if (current()) document.defaultView.location.assign(`${registered.href}#journey=${journeyId}`); });
      return 'recovery';
    }
    if (context.state === 'continue_ready' && hasOnlyKeys(context, ['state', 'journey_id', 'result'])
      && isRecord(context.result) && hasOnlyKeys(context.result,
        ['state', 'journey_id', 'handoff_reference', 'hosted_start_url', 'continuation_reference'])
      && context.result.state === 'waiting' && context.result.journey_id === journeyId
      && context.result.hosted_start_url === `${registered.origin}/public/onboarding/setup/start`
      && [context.result.handoff_reference, context.result.continuation_reference].every((part) => /^[A-Za-z0-9_-]{43}$/u.test(part))) {
      onStatus('Continue setup.');
      onRecovery('Continue setup', () => current() && submitHostedHandoff({ document, payload: context.result, journeyId, registeredHostedUrl }));
      return 'recovery';
    }
    // Returning to this document only reconciles. An interrupted mutation may
    // still be committing; its old context cannot authorize a second request.
    if (readOnly) {
      onStatus('Setup could not be confirmed.');
      onRecovery('Return to setup', () => { if (current()) document.defaultView.location.assign(`${registered.href}#journey=${journeyId}`); });
      return 'recovery';
    }
    if (typeof context.csrf_token !== 'string' || !context.csrf_token || context.csrf_token.length > 1024) return fail();
    let target, fields;
    onStatus('Continuing setup…');
    if (context.state === 'continue' && hasOnlyKeys(context, ['state', 'journey_id', 'continuation', 'csrf_token'])
      && isRecord(context.continuation)
      && hasOnlyKeys(context.continuation, ['profile', 'reference', 'return_target', 'expires_at'])
      && context.continuation.profile === 'urn:bridge-clean:onboarding-continuation:v1'
      && context.continuation.return_target === 'desktop-setup'
      && /^[A-Za-z0-9_-]{43}$/u.test(context.continuation.reference)
      && Number.isFinite(Date.parse(context.continuation.expires_at))) {
      const result = await read(`${prefix}/continue`, { continuation: context.continuation }, context.csrf_token);
      if (!current()) return 'retired';
      if (result?.continuation_reference !== context.continuation.reference || !submitHostedHandoff({
        document, payload: result, journeyId, registeredHostedUrl,
      })) return fail();
      submitted = true; return 'submitted';
    } else if (context.state === 'code_entry' && hasOnlyKeys(context, ['state', 'journey_id', 'setup_code', 'csrf_token'])
      && /^[0-9A-HJKMNP-TV-Z]{12}$/u.test(context.setup_code)) {
      const prepared = await read(`${prefix}/prepare`, { setup_code: context.setup_code }, context.csrf_token);
      if (!current()) return 'retired';
      if (!isRecord(prepared) || !hasOnlyKeys(prepared, ['journey_id', 'request', 'hosted_start_url'])
        || prepared.journey_id !== journeyId || prepared.hosted_start_url !== `${registered.href}/receive`
        || !isTransferRequest(prepared.request) || prepared.request.destination.kind !== 'desktop') return fail();
      target = prepared.hosted_start_url; fields = { journey_id: journeyId, request: JSON.stringify(prepared.request) };
    } else if (context.state === 'proof' && hasOnlyKeys(context,
      ['state', 'journey_id', 'request', 'challenge', 'csrf_token', 'hosted_return_url'])
      && context.hosted_return_url === registered.href && isTransferRequest(context.request)
      && context.request.destination.kind === 'desktop' && isRecord(context.challenge)
      && hasOnlyKeys(context.challenge, ['profile', 'challenge', 'expires_at', 'request_digest'])
      && context.challenge.profile === 'urn:bridge-clean:onboarding-proof:v1'
      && /^[A-Za-z0-9_-]{43}$/u.test(context.challenge.challenge)
      && /^[a-f0-9]{64}$/u.test(context.challenge.request_digest)
      && Number.isFinite(Date.parse(context.challenge.expires_at))) {
      const proof = await read(`${prefix}/sign`, { request: context.request, challenge: context.challenge }, context.csrf_token);
      if (!current()) return 'retired';
      if (!isRecord(proof) || !hasOnlyKeys(proof, ['challenge', 'signature'])
        || proof.challenge !== context.challenge.challenge || !/^[A-Za-z0-9_-]{86}$/u.test(proof.signature)) return fail();
      target = context.hosted_return_url;
      fields = { journey_id: journeyId, request: JSON.stringify(context.request), proof: JSON.stringify(proof) };
    } else return fail();
    const form = document.createElement('form'); form.method = 'post'; form.target = '_self';
    form.action = `${target}#journey=${journeyId}`; form.hidden = true;
    for (const [name, value] of Object.entries(fields)) {
      const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
    }
    document.body.append(form); form.submit(); submitted = true;
    return 'submitted';
  } catch { return fail(); }
  finally { if (submitted && current()) onStatus('Continuing setup…'); }
}

function isTransferRequest(value) {
  return isRecord(value) && hasOnlyKeys(value, ['profile', 'operation_id', 'purpose', 'setup_code', 'destination'])
    && value.profile === 'urn:bridge-clean:onboarding-transfer:v1' && value.purpose === 'resume-onboarding'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(value.operation_id)
    && /^[0-9A-HJKMNP-TV-Z]{12}$/u.test(value.setup_code) && isRecord(value.destination)
    && hasOnlyKeys(value.destination, ['kind', 'destination_id', 'public_key'])
    && ['desktop', 'browser-extension'].includes(value.destination.kind)
    && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(value.destination.destination_id)
    && isRecord(value.destination.public_key) && hasOnlyKeys(value.destination.public_key, ['crv', 'kty', 'x', 'y'])
    && value.destination.public_key.crv === 'P-256' && value.destination.public_key.kty === 'EC'
    && [value.destination.public_key.x, value.destination.public_key.y].every((part) => /^[A-Za-z0-9_-]{43}$/u.test(part));
}

export async function connectProvisioningOnboarding({ fetch, journeyId, onState, onUnavailable, onFocus, modules, runtime = false }) {
  const [{ createOnboardingClient }, { readOnboardingEvents }, { parseOnboardingJson }] = modules ?? await Promise.all([
    import('/provisioning/onboarding/client.mjs'), import('/provisioning/onboarding/sse.mjs'), import('/provisioning/onboarding/json.mjs'),
  ]);
  const client = createOnboardingClient({ journeyId, validate: (value) => parseBrainOnboardingState(value) !== null });
  let stop = false;
  let retries = 0;
  let timer;
  const delays = runtime ? [200, 500, 1000, 2000, 4000, 8000, 16000] : [200, 500, 1000];
  const prefix = runtime ? '/api/v1/onboarding' : '/api/v1/provisioning';
  const headers = runtime ? { 'X-Onboarding-Journey': journeyId } : {};
  const changed = client.subscribe(() => {
    const owner = client.getState().sources.brain;
    if (owner?.certain && owner.snapshot) onState(owner.snapshot);
    else onUnavailable({ retrying: !stop && retries < delays.length });
  });
  function connect() {
    if (stop) return;
    const abort = new AbortController();
    let ready, refuse, dropped, closed = false;
    const subscribed = new Promise((resolve, reject) => { ready = resolve; refuse = reject; });
    void subscribed.catch(() => undefined);
    let deadline = setTimeout(close, 10_000);
    function close() {
      if (closed) return;
      closed = true; clearTimeout(deadline); abort.abort(); refuse(new Error('Subscription unavailable')); dropped?.();
      if (!stop && retries < delays.length) timer = setTimeout(connect, delays[retries++]);
      else if (!stop) onUnavailable({ retrying: false });
    }
    client.attach('brain', {
      subscribe(receive, disconnect) {
        dropped = disconnect;
        void readOnboardingEvents({ fetch, path: `${prefix}/events`, headers, signal: abort.signal,
          ready() { clearTimeout(deadline); ready(); }, receive,
          onFocus: (id) => { if (id === journeyId) onFocus?.(); } }).catch(close);
        return close;
      },
      async readSnapshot() {
        await subscribed;
        deadline = setTimeout(close, 10_000);
        try {
          const response = await fetch(`${prefix}/state`, {
            credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal,
            headers,
          });
          if (!response.ok) throw new Error('Snapshot unavailable');
          return parseOnboardingJson(await response.text());
        } finally { clearTimeout(deadline); }
      },
      sendCommand() { throw new Error('Read-only subscription'); },
      invalidate: close,
    });
  }
  connect();
  return { close() { stop = true; clearTimeout(timer); changed(); client.disconnect('brain'); } };
}

/** Only an authenticated local owner event calls this non-navigating focus request. */
export function focusExistingWorkspace(extensionId, runtime = globalThis.chrome?.runtime) {
  if (!EXTENSION_ID_PATTERN.test(extensionId) || typeof runtime?.connect !== 'function') return false;
  let port, timer, closed = false;
  const close = () => { if (closed) return; closed = true; clearTimeout(timer); try { port?.disconnect(); } catch { /* Already closed. */ } };
  try {
    port = runtime.connect(extensionId, { name: 'ofca.onboarding.v1' });
    timer = setTimeout(close, 5000);
    port.onDisconnect.addListener(() => { void runtime.lastError; close(); });
    port.onMessage.addListener((value) => {
      if (value?.type === 'capabilities' && Array.isArray(value.capabilities)
        && value.capabilities.includes('local-onboarding.command-result.v2')
        && value.capabilities.includes('persistent-workspace.v1')) port.postMessage({ type: 'focus' });
      else if (value?.type === 'focus') close();
    });
    return true;
  } catch { close(); return false; }
}

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
  const keys = ['state', 'stage', 'association_request_id', 'creator_account_id'];
  if (Object.hasOwn(payload, 'context_kind')) {
    if (payload.context_kind !== 'registered-continuation' || !CONTINUATION_STATES.has(payload.continuation_state)) return null;
    keys.push('context_kind', 'continuation_state');
  }
  if (!hasOnlyKeys(payload, keys)
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
  if (response.status === 401 || response.status === 403) return 'Setup could not continue. Open the desktop app.';
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

export function createChromeExtensionMessenger(runtime) {
  return (extensionId, message) => new Promise((resolve, reject) => {
    const chromeRuntime = runtime ?? globalThis.chrome?.runtime;
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

export async function continueActivationReturn({ fetch, journeyId, csrf, storage, current = () => true,
  onStatus = () => {}, timeoutMs = 10_000, parse }) {
  if (!journeyId || !current()) return 'none';
  const controller = new AbortController();
  const unavailable = () => { if (current()) onStatus('Activation could not be confirmed.'); return 'unconfirmed'; };
  let timer;
  const timedOut = new Promise((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error('Activation deadline')); }, timeoutMs);
  });
  const read = async (entryId = null) => {
    const mutation = entryId !== null;
    const response = await fetch('/api/v1/onboarding/activation-return', {
      method: mutation ? 'POST' : 'GET', cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
      headers: { Accept: 'application/json', 'X-Onboarding-Journey': journeyId,
        ...(mutation ? { 'Content-Type': 'application/json', 'X-Provisioning-CSRF': csrf,
          'X-Onboarding-Activation-Entry': entryId } : {}) },
      ...(mutation ? { body: '{}' } : {}),
    });
    if (!current() || !response.ok || !response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Activation response');
    const body = await response.text();
    if (!current() || body.length > 1024) throw new Error('Activation response');
    const value = parse(body);
    if (!hasOnlyKeys(value, ['state', 'journey_id', 'entry_id']) || value.journey_id !== journeyId
      || !['none', 'waiting', 'ready', 'checking', 'unconfirmed'].includes(value.state)
      || !(value.entry_id === null || (typeof value.entry_id === 'string' && JOURNEY_PATTERN.test(value.entry_id)))
      || (['waiting', 'ready', 'checking'].includes(value.state) && value.entry_id === null)
      || (value.state === 'none' && value.entry_id !== null)
      || (mutation && value.entry_id !== entryId)) throw new Error('Activation response');
    return value;
  };
  try {
    let value = await Promise.race([read(), timedOut]);
    if (value.state === 'none') return 'none';
    if (value.state === 'waiting') { onStatus('Waiting for setup…'); return 'waiting'; }
    if (value.state === 'ready') {
      const marker = `ofca.activation.return.v1:${value.entry_id}`;
      if (!storage || storage.getItem(marker) === 'attempted' || !current()) return unavailable();
      storage.setItem(marker, 'attempted');
      if (!current()) return 'retired';
      onStatus('Applying activation…');
      try { value = await Promise.race([read(value.entry_id), timedOut]); }
      catch { if (!current()) return 'retired'; value = await Promise.race([read(), timedOut]); }
    }
    if (value.state !== 'checking') return unavailable();
    return 'checking';
  } catch { return current() ? unavailable() : 'retired'; }
  finally { clearTimeout(timer); controller.abort(); }
}

export function createProvisioningController({ fetch, sendExtensionMessage, connectExtension, connectOnboarding, continueTransfer, continueActivation, document, elements,
  loadLocalWorkspace = () => import('./native-workspace.mjs'), continuationTimeoutMs = 10_000,
  loadContinuationParser = () => import('/provisioning/onboarding/json.mjs') }) {
  const csrf = document.querySelector('main')?.dataset.provisioningCsrf ?? '';
  const extensionId = document.querySelector('main')?.dataset.provisioningExtensionId ?? '';
  let detectedAccountId = null;
  let associatedAccountId = null;
  let associationRequestId = null;
  let installationRegistered = false;
  let approvalAcquired = false;
  let mutationInFlight = false;
  let configurationComplete = false;
  let activationState = 'none';
  let activationRead = null;
  let recoveryRequired = false;
  let extensionStage = null;
  let extensionPort = null;
  let approvalFailed = false;
  let finalizeFailed = false;
  let finalizeRefused = false;
  let finalizationAttempted = false;
  let finalizationRunning = false;
  let returning = null;
  let onboarding = null;
  let handoffAttempted = false;
  let handoffRunning = false;
  let handoffAdvanceRequested = false;
  let handoffSubmitted = false;
  let handoffRefusal = null;
  let handoffReadUnavailable = false;
  let handoffRetryRunning = false;
  let localWorkspace = null;
  let registrationKnown = false;
  let pushedRevision = -1;
  let pushedEpoch = null;
  let progressRead = null;
  let readAgain = false;
  let restarting = false;
  let pageActive = true;
  let lifecycleGeneration = 0;
  let identityGeneration = 0;
  let identityChecking = false;
  let activeMutation = null;
  let uncertainMutation = null;
  let resumeNeedsAction = false;
  let handoffRecoveryRead = null;
  let transferActive = false;
  let ownerGeneration = 0;
  let extensionGeneration = 0;
  let initialProgressPending = true;
  let contextKind = document.querySelector('main')?.dataset.provisioningContextKind ?? 'initial-enrollment';
  let continuationState = null;
  let continuationRunning = false;
  let continuationAttempted = false;
  let continuationSubmitted = false;
  let continuationAbort = null;
  const page = document.defaultView ?? globalThis;
  const journeyId = parseJourneyHash(page.location?.hash);
  const hostedLink = document.querySelector('#open-secure-setup');
  const hostedSetupAvailable = Boolean(hostedLink && !hostedLink.hidden);
  const currentPage = () => {
    const generation = lifecycleGeneration;
    return () => pageActive && generation === lifecycleGeneration;
  };

  const setStatus = (message, error = false) => {
    elements.status.textContent = message;
    elements.status.dataset.tone = error ? 'error' : 'neutral';
    placeFeedback();
  };
  const setIdentityStatus = (message) => { elements.identityStatus.textContent = message; };

  async function resumeRuntime() {
    if (!pageActive || !journeyId || restarting || typeof connectOnboarding !== 'function') return;
    restarting = true;
    onboarding?.close();
    setStatus('Restarting the desktop app…');
    await connectOwner(true);
  }

  async function connectOwner(runtime = false) {
    if (!pageActive) return;
    const generation = ++ownerGeneration;
    const current = () => pageActive && generation === ownerGeneration;
    const handle = await connectOnboarding({ fetch, journeyId, runtime,
      onFocus: () => { if (current()) focusExistingWorkspace(extensionId); },
      onState: (state) => {
        if (!current()) return;
        if (!runtime) { onOwnerState(state); return; }
        // A fresh authenticated runtime snapshot, not focus or a timer, proves
        // that the new process is listening. Replace this same workspace tab.
        if (state.facts.installation === 'verified') page.location.replace(`/#journey=${journeyId}`);
      },
      onUnavailable: ({ retrying } = {}) => {
        if (current()) setStatus(retrying ? 'Reconnecting…' : runtime
          ? 'Setup could not reconnect. Open the desktop app to continue.'
          : 'Connection to the desktop app was lost.');
      },
    });
    if (current()) onboarding = handle;
    else handle?.close();
  }

  async function beginHostedSetup(event) {
    event?.preventDefault();
    if (contextKind === 'registered-continuation') return beginRegisteredContinuation();
    const current = currentPage();
    if (!current() || !journeyId || !hostedSetupAvailable || transferActive || !registrationKnown || installationRegistered
      || configurationComplete || recoveryRequired || handoffSubmitted || activationState === 'waiting' || activationRead) return;
    if (handoffRefusal !== null) { renderHandoffRecovery(); return; }
    if (handoffRunning) { handoffAdvanceRequested = true; return; }
    if (detectedAccountId === null || mutationInFlight) { renderFreshPrerequisite(); return; }
    if (uncertainMutation && !uncertainMutation.path.endsWith('/initial-handoff')) return;
    handoffRunning = true;
    try {
      if (await refreshIdentity({ advance: false }) === null || !current()) return;
      if (handoffAttempted || uncertainMutation) { await recoverHandoff(current); return; }
      handoffAttempted = true;
      setStatus('Connecting this computer…');
      const payload = await mutate('/api/v1/provisioning/initial-handoff', {});
      if (!current() || payload === MUTATION_RETIRED || payload === MUTATION_FAILED) return;
      // Preparation only binds the computer. Re-read admitted identity after it
      // settles so an account switch cannot reuse the earlier creator hint.
      if (await submitFreshHandoff(payload, current)) return;
      if (!current() || detectedAccountId === null) return;
      if (isRecord(payload) && hasOnlyKeys(payload, ['state', 'journey_id']) && payload.journey_id === journeyId
        && ['completing', 'completed', 'unknown'].includes(payload.state)) {
        setStatus(payload.state === 'unknown' ? 'Setup could not be confirmed.' : 'Connecting this computer…');
        return;
      }
      setStatus('Setup could not be started.', true);
    } finally {
      if (current()) {
        handoffRunning = false;
        if (handoffAdvanceRequested) { handoffAdvanceRequested = false; void beginHostedSetup(); }
      }
    }
  }

  async function beginRegisteredContinuation() {
    const current = currentPage();
    if (!current() || contextKind !== 'registered-continuation' || !registrationKnown || !journeyId
      || !hostedSetupAvailable || continuationRunning || continuationSubmitted || configurationComplete
      || recoveryRequired || approvalAcquired || continuationState === null || activationState === 'waiting' || activationRead) return;
    continuationRunning = true;
    const abort = new AbortController(); continuationAbort = abort;
    const deadline = setTimeout(() => abort.abort(), continuationTimeoutMs);
    const bounded = (operation) => Promise.race([operation, new Promise((_, reject) => {
      if (abort.signal.aborted) { reject(new Error('Setup unconfirmed')); return; }
      abort.signal.addEventListener('abort', () => reject(new Error('Setup unconfirmed')), { once: true });
    })]);
    const endpoint = '/api/v1/provisioning/installation-continuation';
    const attemptedHere = continuationAttempted;
    continuationAttempted = true;
    try {
      const receiptKey = `ofca.continuation.prepare.v2:${journeyId}`;
      const savedAttempt = page.sessionStorage?.getItem(receiptKey);
      const prepare = continuationState === 'new' && !attemptedHere && !savedAttempt;
      if (prepare) {
        if (!page.sessionStorage) throw Error('Setup receipt unavailable');
        // Nonauthorizing attempted-operation provenance survives a reload. A
        // late/lost response permits only the owner's exact noncreating read.
        page.sessionStorage.setItem(receiptKey, 'attempted');
      }
      setStatus(prepare ? 'Opening setup…' : 'Checking setup…');
      const response = await bounded(fetch(endpoint, { method: prepare ? 'POST' : 'GET', credentials: 'same-origin',
        cache: 'no-store', redirect: 'error',
        signal: abort.signal,
        headers: { Accept: 'application/json', 'X-Provisioning-CSRF': csrf, 'X-Onboarding-Journey': journeyId,
          ...(prepare ? { 'Content-Type': 'application/json' } : {}) }, ...(prepare ? { body: '{}' } : {}) }));
      if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw Error('Invalid setup response');
      const text = await bounded(response.text());
      if (new TextEncoder().encode(text).length > 4096) throw Error('Invalid setup response');
      const { parseOnboardingJson } = await bounded(loadContinuationParser());
      const payload = parseOnboardingJson(text);
      if (!current() || configurationComplete || approvalAcquired) return;
      if (response.ok && submitHostedContinuation({ document, payload, journeyId, registeredHostedUrl: hostedLink?.href })) {
        continuationSubmitted = true; return;
      }
      setStatus('Setup could not be confirmed.');
    } catch { if (current()) setStatus('Setup could not be confirmed.'); }
    finally {
      clearTimeout(deadline);
      if (continuationAbort === abort) continuationAbort = null;
      if (current()) {
        continuationRunning = false;
        const action = document.querySelector('#transfer-recovery-action');
        if (action && !continuationSubmitted) { action.hidden = false; action.textContent = 'Check setup'; action.onclick = onReturn; }
      }
    }
  }

  async function submitFreshHandoff(payload, current) {
    if (!current() || installationRegistered || configurationComplete || recoveryRequired || transferActive
      || handoffSubmitted || payload?.continuation_reference !== undefined) return false;
    const creator = await refreshIdentity({ advance: false });
    if (!current() || creator === null || creator !== detectedAccountId || installationRegistered
      || configurationComplete || recoveryRequired || transferActive || handoffSubmitted) return false;
    const submitted = submitHostedHandoff({ document, payload, journeyId,
      registeredHostedUrl: hostedLink?.href, intendedCreatorId: creator });
    if (submitted) handoffSubmitted = true;
    return submitted;
  }

  function initialRefusal(status, payload) {
    if (status === 409 && isRecord(payload) && hasOnlyKeys(payload, ['state', 'reason'])
      && payload.state === 'unconfirmed' && ['handoff_refused', 'journey_expired'].includes(payload.reason)) return payload.reason;
    return 'unavailable';
  }

  async function retryInitialHandoff() {
    const current = currentPage();
    if (handoffRefusal !== 'handoff_refused' || handoffRetryRunning || mutationInFlight || uncertainMutation) return;
    handoffRetryRunning = true;
    try {
      const progress = await checkStatus(current);
      if (!current() || progress?.stage !== 'registration_required' || recoveryRequired || configurationComplete
        || handoffRefusal !== 'handoff_refused' || uncertainMutation) return;
      if (await refreshIdentity({ advance: false }) === null || !current()) return;
      // Only this explicit action after an exact owner refusal permits another
      // preparation. Unknown outcomes and locator reads never clear this latch.
      handoffRefusal = null; handoffAttempted = false; handoffReadUnavailable = false; resumeNeedsAction = false;
      renderState();
      await beginHostedSetup();
    } finally { if (current()) { handoffRetryRunning = false; renderHandoffRecovery(); } }
  }

  function renderHandoffRecovery() {
    if (!handoffAttempted || transferActive) return;
    const action = document.querySelector('#transfer-recovery-action');
    if (action) action.hidden = true;
    if (installationRegistered || configurationComplete || handoffSubmitted) return;
    if (handoffRefusal !== null) {
      if (hostedLink) hostedLink.hidden = true;
      setStatus(handoffRefusal === 'handoff_refused' ? 'Setup could not be started.'
        : handoffRefusal === 'journey_expired' ? 'Setup has expired.' : 'Setup could not be confirmed.');
      if (action && handoffRefusal === 'handoff_refused' && !recoveryRequired) {
        action.hidden = false; action.textContent = 'Try setup again'; action.onclick = retryInitialHandoff;
        action.disabled = handoffRetryRunning || mutationInFlight;
      } else if (action) {
        action.hidden = false; action.disabled = false;
        if (handoffRefusal === 'journey_expired' || recoveryRequired) {
          action.textContent = 'Open desktop app';
          action.onclick = () => { if (pageActive && journeyId) page.location.assign(`ofca://onboarding?journey=${journeyId}`); };
        } else { action.textContent = 'Check setup'; action.onclick = onReturn; }
      }
    } else if (handoffReadUnavailable) {
      if (hostedLink) hostedLink.hidden = true;
      setStatus('Setup could not be confirmed.');
      if (action) {
        action.hidden = false; action.disabled = false;
        action.textContent = 'Check setup'; action.onclick = onReturn;
      }
    } else if (uncertainMutation?.path.endsWith('/initial-handoff') && action) {
      if (hostedLink) hostedLink.hidden = true;
      action.hidden = false; action.textContent = 'Check setup'; action.onclick = onReturn;
    }
  }

  function onOwnerState(state) {
    if (state.epoch === pushedEpoch && state.revision <= pushedRevision) return;
    pushedEpoch = state.epoch; pushedRevision = state.revision;
    if (progressRead) { readAgain = true; return; }
    const read = (async () => {
      do {
        readAgain = false;
        const generation = ownerGeneration;
        const current = () => pageActive && generation === ownerGeneration;
        const progress = await checkStatus(current);
        if (!current()) return;
        if (!progress) continue;
        if (activationState === 'waiting') await reconcileActivation();
        if (!current()) return;
        if (contextKind === 'registered-continuation') {
          initialProgressPending = false;
          if (approvalAcquired) await finalizeProvisioning();
          else void beginRegisteredContinuation();
          continue;
        }
        if (resumeNeedsAction) { initialProgressPending = false; return; }
        if (initialProgressPending) {
          initialProgressPending = false;
          if (progress.stage === 'creator_approval_pending') await acquireAssociation();
        }
        if (!current()) return;
        if (approvalAcquired && !configurationComplete && !recoveryRequired) await finalizeProvisioning();
        if (!current()) return;
        if (!handoffAttempted && !installationRegistered && !recoveryRequired && hostedLink) {
          await beginHostedSetup();
        }
      } while (readAgain);
    })().finally(() => { if (progressRead === read) progressRead = null; });
    progressRead = read;
  }
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

  function renderFreshPrerequisite() {
    if (contextKind === 'registered-continuation') {
      const prerequisite = document.querySelector('#initial-browser-prerequisite');
      if (prerequisite) prerequisite.hidden = true;
      return;
    }
    const fresh = Boolean(journeyId && !installationRegistered && !configurationComplete && !transferActive && !recoveryRequired);
    const prerequisite = document.querySelector('#initial-browser-prerequisite');
    if (prerequisite) prerequisite.hidden = !fresh || detectedAccountId !== null;
    if (!fresh) return;
    if (hostedLink) hostedLink.hidden = !hostedSetupAvailable || detectedAccountId === null || handoffAttempted || handoffRefusal !== null
      || handoffReadUnavailable || uncertainMutation?.path.endsWith('/initial-handoff') === true;
    const heading = document.querySelector('#claim-heading');
    if (heading) heading.textContent = detectedAccountId !== null ? 'Connect this computer'
      : identityChecking ? 'Checking creator account…'
      : extensionStage === 'needs_account' ? 'Sign in to OnlyFans' : 'Set up the browser extension';
    elements.claimActionHelp.textContent = detectedAccountId !== null ? `Signed in now: ${detectedAccountId}`
      : identityChecking ? ''
      : extensionStage === 'needs_account' ? 'Use the creator account you want to analyze.'
        : EXTENSION_SETUP_STAGES.has(extensionStage) ? 'Finish Full analytics setup in the extension tab.'
          : 'The creator account could not be checked.';
    const install = document.querySelector('#initial-extension-install');
    if (install) install.hidden = identityChecking || extensionStage !== null;
    const open = document.querySelector('#initial-open-extension');
    if (open) open.hidden = identityChecking || extensionStage === null || extensionStage === 'needs_account';
    const account = document.querySelector('#initial-open-onlyfans');
    if (account) account.hidden = identityChecking || extensionStage !== 'needs_account';
  }

  function setStepState(step, stateOutput, state) {
    step.dataset.state = state;
    const number = [elements.claimStep, elements.identityStep, elements.bindingStep, elements.finalizeStep].indexOf(step) + 1;
    stateOutput.textContent = `Step ${number} of 4`;
    if (state === 'current') setAttribute(step, 'aria-current', 'step');
    else removeAttribute(step, 'aria-current');
  }

  function renderState() {
    const mutationBlocked = mutationInFlight || uncertainMutation !== null;
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
      ? 'Restarting the desktop app…'
      : finalizeFailed ? finalizeRefused ? 'Setup did not finish. Try again.' : 'Setup completion could not be confirmed. Try again.'
        : approvalAcquired ? 'Finishing setup…' : 'The desktop app restarts when setup is finished.';
    if (configurationComplete) elements.finalizeStep.dataset.state = 'completed';
    elements.acquireAssociation.hidden = !approvalFailed;
    elements.finalizeProvisioning.hidden = !finalizeFailed;
    const valid = validateClaimPackageInput(elements.claimPackage.value).valid;
    elements.claimStep.dataset.codeValid = String(valid);

    elements.claimPackage.disabled = recoveryRequired || mutationBlocked || installationRegistered || configurationComplete;
    elements.claimSubmit.disabled = !valid || recoveryRequired || mutationBlocked || installationRegistered || configurationComplete;
    elements.refreshIdentity.disabled = recoveryRequired || configurationComplete || associationRequestId !== null;
    elements.refreshIdentity.hidden = extensionStage !== null || detectedAccountId !== null
      || configurationComplete || associationRequestId !== null;
    elements.confirmIdentity.disabled = recoveryRequired || mutationBlocked || configurationComplete
      || !installationRegistered || detectedAccountId === null || associationRequestId !== null;
    elements.acquireAssociation.disabled = recoveryRequired || mutationBlocked || configurationComplete
      || associationRequestId === null || approvalAcquired;
    elements.finalizeProvisioning.disabled = recoveryRequired || mutationBlocked || configurationComplete
      || associationRequestId === null || !approvalAcquired;

    // Each step owns one instruction. Outcomes appear beside its controls.
    elements.claimStep.dataset.recovery = String(recoveryRequired);
    for (const step of steps) setAttribute(step, 'aria-busy', String(mutationInFlight && step.dataset.state === 'current'));
    renderFreshPrerequisite();
    renderHandoffRecovery();
    if (contextKind === 'registered-continuation') {
      const rail = document.querySelector('.progress-rail');
      if (rail) rail.hidden = true;
      for (const name of ['claimForm', 'claimSubmit', 'identityStep', 'bindingStep', 'refreshIdentity', 'confirmIdentity', 'acquireAssociation']) elements[name].hidden = true;
      elements.claimStep.hidden = approvalAcquired || configurationComplete;
      elements.finalizeStep.hidden = !approvalAcquired && !configurationComplete;
      elements.claimStep.dataset.state = approvalAcquired || configurationComplete ? 'completed' : 'current';
      for (const step of steps) removeAttribute(step, 'aria-current');
      if (!configurationComplete) setAttribute(approvalAcquired ? elements.finalizeStep : elements.claimStep, 'aria-current', 'step');
      for (const output of outputs) output.textContent = '';
      elements.claimActionHelp.textContent = '';
      if (elements.status.textContent) elements.finalizeActionHelp.textContent = '';
      const intro = document.querySelector('.intro');
      if (intro) intro.hidden = true;
      const heading = document.querySelector('#claim-heading');
      if (heading) heading.textContent = 'Continue setup';
      if (hostedLink) hostedLink.hidden = true;
    }
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
      summary.hidden = true;
      elements.status.dataset.long = 'false';
      elements.claimPackageValidation.dataset.long = 'false';
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
    if (progress.context_kind === 'registered-continuation') {
      contextKind = progress.context_kind;
      continuationState = progress.continuation_state;
    }
    registrationKnown = true;
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
      setStatus(contextKind === 'registered-continuation' ? 'Setup is unavailable. Open the desktop app.'
        : 'Setup was interrupted. Restart the desktop app. Do not reuse this setup code.', true);
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

  function reconcileMutation(progress) {
    if (!uncertainMutation) return;
    const operation = uncertainMutation;
    const { path, associationId, accountId } = uncertainMutation;
    const registered = ['creator_confirmation_required', 'creator_approval_pending', 'finalization_ready'].includes(progress.stage);
    const associated = ['creator_approval_pending', 'finalization_ready'].includes(progress.stage)
      && progress.creator_account_id === accountId;
    const acquired = progress.stage === 'finalization_ready'
      && progress.association_request_id === associationId && progress.creator_account_id === accountId;
    if (progress.state === 'configured_restart'
      || (['/claim', '/initial-handoff'].some((suffix) => path.endsWith(suffix)) && registered)
      || (path.endsWith('/creator-association') && associated)
      || (path.endsWith('/acquire') && acquired)) uncertainMutation = null;
    else if (uncertainMutation.refused) {
      uncertainMutation = null;
      if (path.endsWith('/initial-handoff')) handoffRefusal = operation.initialRefusal ?? 'unavailable';
      approvalFailed = progress.stage === 'creator_approval_pending';
      finalizeFailed = progress.stage === 'finalization_ready';
      finalizeRefused = true;
      setStatus('This step did not finish. Try again.');
    } else setStatus('This step could not be confirmed.');
    // A committed owner fact supersedes an unresolved transport response. The
    // retired request's finally block must not unlock a later page operation.
    if (uncertainMutation === null && activeMutation === operation) {
      activeMutation = null; mutationInFlight = false;
    }
    const recovery = document.querySelector('#transfer-recovery-action');
    if (recovery) {
      recovery.hidden = uncertainMutation === null;
      recovery.textContent = 'Check setup';
      recovery.onclick = onReturn;
    }
    renderState();
  }

  async function recoverHandoff(current) {
    if (handoffRecoveryRead) return handoffRecoveryRead;
    const read = (async () => {
      try {
        const response = await fetch('/api/v1/provisioning/initial-handoff', {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          headers: { Accept: 'application/json', 'X-Provisioning-CSRF': csrf, 'X-Onboarding-Journey': journeyId },
        });
        const payload = await readJson(response);
        if (!current() || transferActive || handoffSubmitted) return;
        if (!response.ok) {
          if (initialRefusal(response.status, payload) === 'journey_expired') handoffRefusal = 'journey_expired';
          handoffReadUnavailable = true; renderHandoffRecovery(); return;
        }
        if (await submitFreshHandoff(payload, current)) {
          if (activeMutation === uncertainMutation) { activeMutation = null; mutationInFlight = false; }
          uncertainMutation = null;
          const recovery = document.querySelector('#transfer-recovery-action');
          if (recovery) recovery.hidden = true;
        } else if (current() && (payload?.state !== 'waiting' || detectedAccountId !== null)) {
          handoffReadUnavailable = true; renderHandoffRecovery();
        }
      } catch {
        if (current()) { handoffReadUnavailable = true; renderHandoffRecovery(); }
      }
    })().finally(() => { if (handoffRecoveryRead === read) handoffRecoveryRead = null; });
    handoffRecoveryRead = read;
    return read;
  }

  async function mutate(path, body, { neutralReasons = [] } = {}) {
    const current = currentPage();
    if (!current() || uncertainMutation) return MUTATION_RETIRED;
    if (recoveryRequired || mutationInFlight || configurationComplete) return MUTATION_FAILED;
    resumeNeedsAction = false;
    const operation = { path, associationId: associationRequestId,
      accountId: body.detected_creator_account_id ?? associatedAccountId };
    activeMutation = operation;
    setStatus(path.endsWith('/acquire') ? 'Checking approval…' : path.endsWith('/finalize') ? 'Finishing setup…' : path.endsWith('/claim') ? 'Checking code…' : path.endsWith('/initial-handoff') ? 'Connecting this computer…' : '');
    setBusy(true);
    try {
      const response = await fetch(path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Provisioning-CSRF': csrf },
        body: JSON.stringify(body),
      });
      const payload = await readJson(response);
      if (!current()) {
        // A refusal never authorizes progress. After a fresh owner read it can
        // offer an explicit retry, instead of trapping the user as uncertain.
        if (response.status >= 400 && response.status < 500) {
          operation.refused = true; resumeNeedsAction = true;
          if (path.endsWith('/initial-handoff')) operation.initialRefusal = initialRefusal(response.status, payload);
        }
        return MUTATION_RETIRED;
      }
      if (!response.ok) {
        if (path.endsWith('/initial-handoff') && response.status >= 500) {
          uncertainMutation = operation;
          setStatus('Setup could not be confirmed.');
          return MUTATION_FAILED;
        }
        if (path.endsWith('/finalize')) finalizeRefused = response.status >= 400 && response.status < 500;
        if (path.endsWith('/initial-handoff')) handoffRefusal = initialRefusal(response.status, payload);
        if (response.status === 401 || response.status === 403) recoveryRequired = true;
        const reason = isRecord(payload) && typeof payload.reason === 'string' ? payload.reason : null;
        setStatus(explainProvisioningFailure(response, payload), !neutralReasons.includes(reason));
        return MUTATION_FAILED;
      }
      return payload;
    } catch {
      if (!current()) return MUTATION_RETIRED;
      if (path.endsWith('/initial-handoff')) {
        uncertainMutation = operation;
        setStatus('Setup could not be confirmed.');
        return MUTATION_FAILED;
      }
      setStatus(REQUEST_FAILURE, true); return MUTATION_FAILED;
    } finally {
      if (activeMutation === operation) {
        activeMutation = null;
        mutationInFlight = false;
        if (current()) renderState();
        // Ignore the retired reply. Only a fresh read can reconcile its effect.
        else if (pageActive) void onReturn();
      }
    }
  }

  async function refreshIdentity({ advance = true } = {}) {
    if (contextKind === 'registered-continuation') return null;
    const pageCurrent = currentPage();
    const generation = ++identityGeneration;
    const current = () => pageCurrent() && generation === identityGeneration && associationRequestId === null;
    if (!current() || recoveryRequired || configurationComplete) return null;
    detectedAccountId = null;
    identityChecking = true;

    renderState();
    if (!EXTENSION_ID_PATTERN.test(extensionId)) {
      identityChecking = false;
      setIdentityStatus('Enable the Conversation Analytics extension, then try again.'); renderFreshPrerequisite(); renderExtensionSetup(); return null;
    }
    try {
      const identity = parseIdentityResponse(await sendExtensionMessage(extensionId, IDENTITY_QUERY));
      if (!current()) return null;
      if (identity === null) {
        setIdentityStatus(missingExtensionStep() ?? 'Your account could not be checked.');
        renderExtensionSetup(); return null;
      }
      if (identity.accountId === null) {
        setIdentityStatus(missingExtensionStep() ?? 'Sign in to your creator account on OnlyFans, then try again.');
        renderExtensionSetup(); return null;
      }
      if (journeyId && !installationRegistered && !HOSTED_CREATOR_PATTERN.test(identity.accountId)) {
        setIdentityStatus('The creator account could not be checked.'); renderFreshPrerequisite(); return null;
      }
      detectedAccountId = identity.accountId;

      setIdentityStatus(`Signed in now: ${detectedAccountId}`);
      renderState(); renderExtensionSetup();
      if (advance) void beginHostedSetup();
      return detectedAccountId;
    } catch {
      if (!current()) return null;
      setIdentityStatus(missingExtensionStep() ?? 'Enable the Conversation Analytics extension, then try again.');
      renderExtensionSetup();
      return null;
    } finally {
      if (current()) { identityChecking = false; renderFreshPrerequisite(); }
    }
  }

  async function checkStatus(current = currentPage()) {
    if (!current()) return null;
    try {
      const response = await fetch('/api/v1/provisioning/status', { credentials: 'same-origin' });
      const payload = await readJson(response);
      if (!current()) return null;
      const progress = response.ok ? parseStatusResponse(payload) : null;
      if (contextKind === 'registered-continuation' && progress?.state !== 'configured_restart'
        && progress?.context_kind !== 'registered-continuation') { setStatus('Setup could not be checked.'); return null; }
      if (progress?.state === 'configured_restart') {
        configurationComplete = true;
        setStatus('The desktop app is restarting. Continue there when it opens.');
        setIdentityStatus('');
        renderState();
        void resumeRuntime();
      } else if (!response.ok) setStatus(explainProvisioningFailure(response, payload), true);
      else if (progress?.state === 'provisioning_ready') applyProgress(progress);
      else setStatus('Setup could not be checked.', true);
      if (progress) reconcileMutation(progress);
      if (progress && uncertainMutation?.path.endsWith('/initial-handoff')) await recoverHandoff(current);
      if (!current()) return null;
      return progress;
    } catch {
      if (!current()) return null;
      setStatus('Setup could not be checked.', true);
      return null;
    }
  }

  async function submitClaim(event) {
    event?.preventDefault();
    if (contextKind === 'registered-continuation') return;
    const current = currentPage();
    if (!current() || recoveryRequired || installationRegistered || configurationComplete) return;
    const packageResult = updatePackageGuidance(true);
    if (!packageResult.valid) { setStatus(''); elements.claimPackage.focus?.(); return; }
    const payload = await mutate('/api/v1/provisioning/claim', { package: packageResult.value });
    if (!current() || payload === MUTATION_RETIRED) return;
    if (isInstallationRegisteredResponse(payload)) {
      installationRegistered = true;
      setStatus('');
      if (detectedAccountId !== null) setIdentityStatus(`Signed in now: ${detectedAccountId}`);
      renderState();
      focusCurrentStep();
    } else if (payload !== MUTATION_FAILED) setStatus('The code could not be checked. Try again.', true);
  }

  async function confirmIdentity() {
    if (contextKind === 'registered-continuation') return;
    const current = currentPage();
    if (!current() || recoveryRequired || !installationRegistered || detectedAccountId === null || associationRequestId !== null) return;
    const confirmedAccountId = detectedAccountId;
    const payload = await mutate('/api/v1/provisioning/creator-association', { detected_creator_account_id: confirmedAccountId });
    if (!current() || payload === MUTATION_RETIRED) return;
    const created = parseAssociationCreationResponse(payload);
    if (created !== null) {
      associationRequestId = created; associatedAccountId = confirmedAccountId;
      setStatus('');
      setIdentityStatus(''); renderState(); focusCurrentStep();
    } else if (payload !== MUTATION_FAILED) setStatus('The account could not be confirmed. Check the OnlyFans tab and try again.', true);
  }

  async function acquireAssociation() {
    if (contextKind === 'registered-continuation') return;
    const current = currentPage();
    if (!current() || uncertainMutation || recoveryRequired || mutationInFlight || configurationComplete || associationRequestId === null || approvalAcquired) return;
    approvalFailed = false;
    const payload = await mutate(
      '/api/v1/provisioning/creator-association/acquire',
      {},
      { neutralReasons: ['binding_acquisition_unavailable'] },
    );
    if (!current() || payload === MUTATION_RETIRED) return;
    if (isApprovedAssociationResponse(payload, associationRequestId)) {
      approvalAcquired = true; setStatus(''); renderState(); focusCurrentStep();
    } else {
      approvalFailed = true;
      if (payload !== MUTATION_FAILED) setStatus('The connection could not be checked. Try again.', true);
      renderState();
    }
  }

  async function acquireAndFinish() {
    const current = currentPage();
    await acquireAssociation();
    if (current() && approvalAcquired) await finalizeProvisioning();
  }

  async function finalizeProvisioning() {
    const current = currentPage();
    // A staged activation is still process-owned until the native binding
    // permits delivery. Do not restart and erase it while approval is pending.
    if (activationState === 'waiting' || activationRead) return;
    if (!current() || uncertainMutation || finalizationRunning || recoveryRequired || mutationInFlight || configurationComplete || associationRequestId === null || associatedAccountId === null || !approvalAcquired) return;
    finalizationRunning = true;
    try {
      if (finalizationAttempted) {
        const progress = await checkStatus();
        if (!current() || progress?.stage !== 'finalization_ready' || configurationComplete || recoveryRequired) return;
      }
      finalizationAttempted = true;
      finalizeFailed = false;
      finalizeRefused = false;
      const payload = await mutate('/api/v1/provisioning/finalize', {
        association_request_id: associationRequestId, detected_creator_account_id: associatedAccountId,
      });
      if (!current() || payload === MUTATION_RETIRED) return;
      if (isConfiguredRestartResponse(payload)) {
        configurationComplete = true;
        setStatus('The desktop app is restarting. Continue there when it opens.');
        setIdentityStatus(''); renderState(); focusCurrentStep();
        void resumeRuntime();
      } else {
        finalizeFailed = true;
        if (payload !== MUTATION_FAILED) setStatus('Setup did not finish. Try again.', true);
        renderState();
      }
    } finally { if (current()) finalizationRunning = false; }
  }

  function onReturn() {
    if (!pageActive || transferActive || document.hidden || recoveryRequired || configurationComplete || (mutationInFlight && !uncertainMutation)) return returning;
    if (returning) return returning;
    if (journeyId && extensionStage === null) connectExtensionState();
    const read = (async () => {
      const generation = ownerGeneration;
      const current = () => pageActive && generation === ownerGeneration;
      const progress = await checkStatus(current);
      if (!progress || !current() || recoveryRequired || configurationComplete || mutationInFlight || resumeNeedsAction
        || uncertainMutation?.path.endsWith('/initial-handoff')) return;
      if (contextKind === 'registered-continuation') {
        if (approvalAcquired) await finalizeProvisioning();
        else await beginRegisteredContinuation();
      } else if (approvalAcquired) await finalizeProvisioning();
      else if (associationRequestId !== null) await acquireAndFinish();
      else await refreshIdentity();
    })().finally(() => { if (returning === read) returning = null; });
    returning = read;
    return returning;
  }

  function connectExtensionState() {
    if (contextKind === 'registered-continuation') return;
    if (!EXTENSION_ID_PATTERN.test(extensionId) || typeof connectExtension !== 'function') return;
    const generation = ++extensionGeneration;
    extensionPort?.close();
    extensionPort = connectExtension(extensionId, (stage) => {
      if (!pageActive || generation !== extensionGeneration) return;
      extensionStage = stage;
      renderFreshPrerequisite();
      elements.refreshIdentity.hidden = stage !== null;
      if (!configurationComplete && !recoveryRequired && associationRequestId === null) void refreshIdentity();
      else renderExtensionSetup();
    });
  }

  async function readReceivingTransfer(readOnly = false) {
    if (contextKind === 'registered-continuation') return 'none';
    if (!journeyId || typeof continueTransfer !== 'function') return 'none';
    const current = currentPage();
    transferActive = true;
    const recoveryButton = document.querySelector('#transfer-recovery-action');
    if (recoveryButton) { recoveryButton.hidden = true; recoveryButton.onclick = null; }
    const outcome = await continueTransfer({ fetch, document, journeyId, registeredHostedUrl: hostedLink?.href,
      current, readOnly, onStatus: (message) => { if (current()) setStatus(message); },
      onRecovery: (label, action) => {
        if (!current() || !recoveryButton) return;
        recoveryButton.textContent = label; recoveryButton.hidden = false;
        recoveryButton.onclick = () => { if (current()) action(); };
      } });
    if (!current()) return 'retired';
    transferActive = outcome !== 'none';
    if (transferActive) {
      for (const name of ['claimForm', 'claimSubmit', 'identityStep', 'bindingStep', 'finalizeStep']) elements[name].hidden = true;
      elements.claimActionHelp.textContent = 'Continue setup in this tab.';
      if (hostedLink) {
        hostedLink.textContent = 'Return to setup';
        hostedLink.hidden = outcome === 'recovery' || outcome === 'submitted';
      }
    }
    return outcome;
  }

  async function start() {
    const current = currentPage();
    elements.claimForm.addEventListener('submit', submitClaim);
    elements.claimPackage.addEventListener('input', () => { setStatus(''); updatePackageGuidance(true); });
    elements.refreshIdentity.addEventListener('click', refreshIdentity);
    elements.confirmIdentity.addEventListener('click', confirmIdentity);
    elements.acquireAssociation.addEventListener('click', acquireAndFinish);
    elements.finalizeProvisioning.addEventListener('click', finalizeProvisioning);
    document.addEventListener('visibilitychange', onReturn);
    (document.defaultView ?? globalThis).addEventListener?.('focus', onReturn);
    page.addEventListener?.('pagehide', retirePage);
    async function connectLocalWorkspace() {
      if (!journeyId) return;
      const current = currentPage();
      try {
        const { createLocalWorkspaceOwner } = await loadLocalWorkspace();
        if (!current()) return;
        localWorkspace?.stop();
        localWorkspace = createLocalWorkspaceOwner({ fetch, location: page.location, storage: page.sessionStorage,
          current: () => pageActive && !transferActive && !configurationComplete, retire: retirePage });
      } catch { /* Local tab coordination cannot change provisioning authority. */ }
    }
    function retirePage() {
      pageActive = false; lifecycleGeneration += 1; ownerGeneration += 1; extensionGeneration += 1;
      identityGeneration += 1;
      if (activeMutation) uncertainMutation = activeMutation;
      progressRead = null; activationRead = null; returning = null; handoffRecoveryRead = null; readAgain = false; finalizationRunning = false;
      detectedAccountId = null;
      identityChecking = false;
      registrationKnown = false; handoffRunning = false; handoffAdvanceRequested = false; handoffSubmitted = false;
      handoffRetryRunning = false;
      continuationRunning = false; continuationSubmitted = false;
      continuationAbort?.abort(); continuationAbort = null;
      onboarding?.close(); onboarding = null;
      extensionPort?.close(); extensionPort = null;
      localWorkspace?.stop(); localWorkspace = null;
    }
    page.addEventListener?.('pageshow', (event) => {
      if (!event.persisted) return;
      pageActive = true; initialProgressPending = true;
      if (transferActive) {
        const restored = currentPage();
        void readReceivingTransfer(true).then(async (outcome) => {
          if (restored() && outcome === 'none') { await connectLocalWorkspace(); if (restored()) void startNormal(); }
        });
        return;
      }
      void connectLocalWorkspace();
      pushedEpoch = null; pushedRevision = -1;
      connectExtensionState();
      if (journeyId && typeof connectOnboarding === 'function') {
        if (configurationComplete) { restarting = false; void resumeRuntime(); }
        else void connectOwner();
      } else void onReturn();
    });
    await reconcileActivation();
    if (!current()) return;
    if (await readReceivingTransfer() !== 'none' || !current()) return;
    await connectLocalWorkspace();
    if (!current()) return;
    await startNormal();
  }

  function reconcileActivation() {
    if (typeof continueActivation !== 'function' || !journeyId) return Promise.resolve('none');
    if (activationRead) return activationRead;
    const current = currentPage();
    const read = (async () => {
      const parser = await loadContinuationParser();
      if (!current()) return 'retired';
      const state = await continueActivation({ fetch, journeyId, csrf, storage: page.sessionStorage, current,
        parse: parser.parseOnboardingJson, onStatus: (message) => { if (current()) setStatus(message); } });
      if (current()) activationState = state;
      return state;
    })().finally(() => { if (activationRead === read) activationRead = null; });
    activationRead = read;
    return read;
  }

  async function startNormal() {
    const current = currentPage();
    if (!current()) return;
    for (const name of ['identityStep', 'bindingStep', 'finalizeStep']) elements[name].hidden = false;
    const dialog = document.querySelector('#recovery-dialog');
    document.querySelector('#recovery-open')?.addEventListener('click', () => dialog?.showModal());
    document.querySelector('#recovery-close')?.addEventListener('click', () => dialog?.close());
    document.querySelector('#feedback-details-open')?.addEventListener('click', () => document.querySelector('#feedback-dialog')?.showModal());
    elements.openExtensionSetup?.addEventListener('click', () => extensionPort?.open('setup'));
    document.querySelector('#initial-open-extension')?.addEventListener('click', () => extensionPort?.open('setup'));
    connectExtensionState();
    if (journeyId && typeof connectOnboarding === 'function') {
      elements.claimForm.hidden = true;
      elements.claimSubmit.hidden = true;
      elements.claimActionHelp.textContent = 'Connect this computer to your account.';
      hostedLink?.addEventListener('click', beginHostedSetup);
      const approvalLink = document.querySelector('#continue-creator-approval');
      if (approvalLink && hostedLink?.href) approvalLink.href = `${hostedLink.href}#journey=${journeyId}`;
      const help = document.querySelector('#secure-setup-help');
      if (help) help.textContent = 'Setup opens in this tab.';
      await connectOwner();
      if (!current()) return;
    }
    updatePackageGuidance(false); renderState();
    if (!onboarding) { await onReturn(); return; }
    if (approvalAcquired && !configurationComplete && !recoveryRequired) void finalizeProvisioning();
    if (!configurationComplete && !recoveryRequired && associationRequestId === null) await refreshIdentity();
  }

  return { start, refreshIdentity, confirmIdentity, acquireAssociation, finalizeProvisioning, submitClaim, checkStatus, beginHostedSetup };
}

if (typeof document !== 'undefined') {
  const main = document.querySelector('main[data-provisioning-csrf]');
  if (main !== null) {
    const byId = (id) => document.getElementById(id);
    const controller = createProvisioningController({
      fetch: globalThis.fetch.bind(globalThis),
      sendExtensionMessage: createChromeExtensionMessenger(),
      connectExtension: createChromeExtensionPort(),
      connectOnboarding: connectProvisioningOnboarding,
      continueTransfer: continueReceivingTransfer,
      continueActivation: continueActivationReturn,
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
