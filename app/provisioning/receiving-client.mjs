import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

const JOURNEY_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

function workspaceHeaders(journeyId) {
  return JOURNEY_PATTERN.test(journeyId) ? { 'X-Onboarding-Journey': journeyId } : {};
}

/** An explicit code submission only prepares the exact receiving intent. */
export async function startReceivingCode({ fetch, document, journeyId, csrfToken, setupCode,
  registeredHostedUrl, current = () => true, timeoutMs = 10_000 }) {
  if (!current() || !JOURNEY_PATTERN.test(journeyId) || !/^[A-Za-z0-9_-]{43}$/u.test(csrfToken)
    || !/^[0-9A-HJKMNP-TV-Z]{12}$/u.test(setupCode)) throw Error('Invalid receiving request');
  const registered = new URL(registeredHostedUrl);
  if (registered.protocol !== 'https:' || registered.username || registered.password || registered.search || registered.hash
    || registered.pathname !== '/public/onboarding/setup') throw Error('Invalid receiving destination');
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const response = await fetch('/api/v1/provisioning/setup-transfer/prepare', {
      method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal: abort.signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
        ...workspaceHeaders(journeyId) }, body: JSON.stringify({ setup_code: setupCode }),
    });
    if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') throw Error('Receiving unavailable');
    const value = parseOnboardingJson(await response.text());
    if (!current()) return false;
    if (!isRecord(value) || !hasOnlyKeys(value, ['journey_id', 'request', 'hosted_start_url'])
      || value.journey_id !== journeyId || value.hosted_start_url !== `${registered.href}/receive`
      || !isTransferRequest(value.request) || value.request.destination.kind !== 'desktop'
      || value.request.setup_code !== setupCode) throw Error('Receiving unavailable');
    const form = document.createElement('form'); form.method = 'post'; form.target = '_self'; form.hidden = true;
    form.action = `${value.hosted_start_url}#journey=${journeyId}`;
    for (const [name, field] of Object.entries({ journey_id: journeyId, request: JSON.stringify(value.request) })) {
      const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = field; form.append(input);
    }
    document.body.append(form); form.submit(); return true;
  } finally { clearTimeout(timer); }
}


export function submitTransferAttachment({ document, payload, journeyId, registeredHostedUrl }) {
  if (!isRecord(payload) || !hasOnlyKeys(payload,
    ['state', 'journey_id', 'workspace_journey_id', 'handoff_reference', 'hosted_start_url', 'continuation_reference'])
    || payload.state !== 'waiting' || payload.journey_id !== journeyId || !JOURNEY_PATTERN.test(journeyId)
    || !JOURNEY_PATTERN.test(payload.workspace_journey_id)
    || ![payload.handoff_reference, payload.continuation_reference].every(value => /^[A-Za-z0-9_-]{43}$/u.test(value))) return false;
  const registered = new URL(registeredHostedUrl);
  if (registered.protocol !== 'https:' || registered.username || registered.password || registered.search || registered.hash
    || registered.pathname !== '/public/onboarding/setup' || payload.hosted_start_url !== `${registered.href}/attach`) return false;
  const form = document.createElement('form'); form.method = 'post'; form.target = '_self'; form.hidden = true;
  form.action = payload.hosted_start_url;
  for (const name of ['journey_id', 'workspace_journey_id', 'handoff_reference', 'continuation_reference']) {
    const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = payload[name]; form.append(input);
  }
  document.body.append(form); form.submit(); return true;
}

export async function continueReceivingTransfer({ fetch, document, journeyId, registeredHostedUrl, onStatus, onRecovery = () => {},
  loadParser = async () => ({ parseOnboardingJson }), timeoutMs = 10_000,
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
          headers: { Accept: 'application/json', ...workspaceHeaders(journeyId),
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
        ['state', 'journey_id', 'workspace_journey_id', 'handoff_reference', 'hosted_start_url', 'continuation_reference'])
      && context.result.state === 'waiting' && context.result.journey_id === journeyId
      && JOURNEY_PATTERN.test(context.result.workspace_journey_id)
      && context.result.hosted_start_url === `${registered.href}/attach`
      && [context.result.handoff_reference, context.result.continuation_reference].every((part) => /^[A-Za-z0-9_-]{43}$/u.test(part))) {
      onStatus('Continue setup.');
      onRecovery('Continue setup', () => current() && submitTransferAttachment({ document, payload: context.result, journeyId, registeredHostedUrl }));
      return 'recovery';
    }
    if (context.state === 'continue_pending' && hasOnlyKeys(context, ['state', 'journey_id', 'csrf_token'])
      && typeof context.csrf_token === 'string' && context.csrf_token.length <= 1024) {
      onStatus('Setup could not be confirmed.');
      onRecovery('Continue setup', async () => {
        try {
          const result = await read(`${prefix}/resume`, {}, context.csrf_token);
          if (current() && !submitTransferAttachment({ document, payload: result, journeyId, registeredHostedUrl })) fail();
        } catch { fail(); }
      });
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
      if (result?.continuation_reference !== context.continuation.reference || !submitTransferAttachment({
        document, payload: result, journeyId, registeredHostedUrl,
      })) return fail();
      submitted = true; return 'submitted';
    } else if (context.state === 'code_entry' && hasOnlyKeys(context, ['state', 'journey_id', 'setup_code', 'csrf_token'])
      && /^[0-9A-HJKMNP-TV-Z]{12}$/u.test(context.setup_code)) {
      const prepared = await read(`${prefix}/prepare`, { setup_code: context.setup_code }, context.csrf_token);
      if (!current()) return 'retired';
      if (!isRecord(prepared) || !hasOnlyKeys(prepared, ['journey_id', 'request', 'hosted_start_url'])
        || prepared.journey_id !== journeyId || prepared.hosted_start_url !== `${registered.href}/receive`
        || !isTransferRequest(prepared.request) || prepared.request.setup_code !== context.setup_code
        || prepared.request.destination.kind !== 'desktop') return fail();
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
