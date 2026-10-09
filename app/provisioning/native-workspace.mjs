const ORIGIN = 'http://bridge.localhost:17871';
const CHANNEL = 'ofca.local-workspace.v1';
const RETURN_KEY = 'native_workspace_local_return_v1';
const RECEIPT_KEY = 'native_workspace_owner_receipt_v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const fields = ['request_id', 'entry_id', 'previous_journey_id', 'journey_id'];
const same = (a, b) => fields.every((key) => a?.[key] === b?.[key]);
const message = (value, type, extra = []) => exact(value, ['type', ...fields, ...extra]) && value.type === type
  && fields.every((key) => UUID.test(value[key])) && extra.every((key) => UUID.test(value[key]));
const payload = (value) => Object.fromEntries(fields.map((key) => [key, value[key]]));
const target = (journey) => `${ORIGIN}/provisioning#journey=${journey}`;
const factory = (name) => new BroadcastChannel(name);
const parser = () => import('/provisioning/native-json.mjs');
const fail = () => { throw Error('Local return unconfirmed'); };

async function selected(fetch, request, loadParser, timeoutMs) {
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([(async () => {
      const response = await fetch('/api/v1/provisioning/native-entry', { credentials: 'same-origin', cache: 'no-store',
        redirect: 'error', signal: controller.signal, headers: { Accept: 'application/json' } });
      if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') return false;
      const text = await response.text(); if (text.length > 1024) return false;
      const { parseOnboardingJson } = await loadParser(); const value = parseOnboardingJson(text);
      return exact(value, ['state', 'journey_id', 'previous_journey_id']) && value.state === 'selected'
        && value.previous_journey_id === request.previous_journey_id && value.journey_id === request.journey_id;
    })(), new Promise((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(false); }, timeoutMs); })]);
  } catch { return false; }
  finally { clearTimeout(timer); }
}

function stored(storage, key, now) {
  try {
    const value = JSON.parse(storage.getItem(key));
    if (!exact(value, [...fields, 'owner_document_id', 'phase', 'expires_at']) || !fields.every((key) => UUID.test(value[key]))
      || (value.owner_document_id !== null && !UUID.test(value.owner_document_id))
      || !['returning', 'continued', 'navigating', 'committed'].includes(value.phase)
      || !Number.isSafeInteger(value.expires_at) || value.expires_at <= now() || value.expires_at > now() + 300_000) return null;
    return value;
  } catch { return null; }
}

/** Local messages coordinate documents; only the authenticated read selects setup. */
export function createLocalWorkspaceOwner({ fetch, location, storage, current = () => true, retire,
  channelFactory = factory, loadParser = parser, now = Date.now, timeoutMs = 3000,
  navigate = (url) => { history.replaceState(null, '', url); location.reload(); } }) {
  if (location.origin !== ORIGIN || location.pathname !== '/provisioning' || location.search) return { stop() {} };
  const journey = location.hash?.startsWith('#journey=') ? location.hash.slice(9) : '';
  if (!UUID.test(journey) || location.href !== target(journey)) return { stop() {} };
  const documentId = crypto.randomUUID(); const expected = location.href;
  const channel = channelFactory(CHANNEL); const offers = new Map();
  let active = true, navigating = false;
  const live = () => active && current() && location.href === expected;
  const announce = async (request) => {
    const receipt = stored(storage, RECEIPT_KEY, now);
    if (!live() || !receipt || !['navigating', 'committed'].includes(receipt.phase)
      || receipt.journey_id !== journey || (request && !same(receipt, request))
      || !await selected(fetch, receipt, loadParser, timeoutMs) || !live()) return;
    receipt.phase = 'committed'; storage.setItem(RECEIPT_KEY, JSON.stringify(receipt));
    channel.postMessage({ type: 'committed', ...payload(receipt), owner_document_id: receipt.owner_document_id, document_id: documentId });
  };
  channel.onmessage = (event) => { void (async () => {
    const request = event.data;
    if (message(request, 'reconcile', ['owner_document_id'])) {
      const receipt = stored(storage, RECEIPT_KEY, now);
      if (receipt?.owner_document_id === request.owner_document_id) await announce(request);
      return;
    }
    if (!live() || navigating || request?.previous_journey_id !== journey) return;
    if (message(request, 'probe')) {
      if (offers.size >= 16 || !await selected(fetch, request, loadParser, timeoutMs) || !live()) return;
      offers.set(request.request_id, { ...payload(request), expires_at: now() + 5000 });
      channel.postMessage({ type: 'offer', ...payload(request), owner_document_id: documentId });
    } else if (message(request, 'navigate', ['owner_document_id']) && request.owner_document_id === documentId) {
      const offer = offers.get(request.request_id);
      if (!offer || offer.expires_at <= now() || !same(offer, request)) return;
      navigating = true;
      if (!await selected(fetch, request, loadParser, timeoutMs) || !live()) return;
      storage.setItem(RECEIPT_KEY, JSON.stringify({ ...payload(request), owner_document_id: documentId,
        phase: 'navigating', expires_at: now() + 300_000 }));
      retire(); navigate(target(request.journey_id));
    }
  })().catch(() => undefined); };
  void announce().catch(() => undefined);
  return { stop() { active = false; offers.clear(); channel.close(); } };
}

/** Once dispatch is recorded, later calls only reconcile its receipt. */
export async function returnToLocalWorkspace({ fetch, context, location, storage, current = () => true,
  channelFactory = factory, loadParser = parser, now = Date.now, discoveryMs = 300, timeoutMs = 3000 }) {
  if (!exact(context, ['entry_id', 'previous_journey_id', 'journey_id']) || !Object.values(context).every((value) => UUID.test(value))
    || location.origin !== ORIGIN || location.pathname !== '/provisioning/native-return' || location.search || !current()) fail();
  if (!await selected(fetch, context, loadParser, timeoutMs) || !current()) fail();
  let attempt = stored(storage, RETURN_KEY, now);
  if (attempt && (attempt.entry_id !== context.entry_id || attempt.previous_journey_id !== context.previous_journey_id
    || attempt.journey_id !== context.journey_id)) fail();
  if (attempt?.phase === 'continued') return { status: 'continued' };
  if (attempt && (attempt.phase !== 'returning' || attempt.owner_document_id === null)) fail();
  attempt ??= { ...context, request_id: crypto.randomUUID(), owner_document_id: null,
    phase: 'returning', expires_at: now() + 300_000 };
  const channel = channelFactory(CHANNEL); const offers = new Set(); let timer, resolveReceipt;
  const receipt = new Promise((resolve) => { resolveReceipt = resolve; });
  let dispatched = attempt.owner_document_id !== null, collecting = !dispatched;
  channel.onmessage = ({ data }) => {
    if (!current() || !same(data, attempt)) return;
    if (collecting && message(data, 'offer', ['owner_document_id'])) offers.add(data.owner_document_id);
    if (dispatched && message(data, 'committed', ['owner_document_id', 'document_id'])
      && data.owner_document_id === attempt.owner_document_id && data.document_id !== data.owner_document_id) resolveReceipt(true);
  };
  try {
    if (!dispatched) {
      channel.postMessage({ type: 'probe', ...payload(attempt) });
      await new Promise((resolve) => { timer = setTimeout(resolve, discoveryMs); }); collecting = false;
      if (!current() || offers.size > 1) fail();
      if (offers.size === 0) {
        // No response is not proof that every old physical tab is closed.
        attempt.phase = 'continued'; storage.setItem(RETURN_KEY, JSON.stringify(attempt));
        return { status: 'continued' };
      }
      attempt.owner_document_id = [...offers][0];
      storage.setItem(RETURN_KEY, JSON.stringify(attempt)); dispatched = true;
      channel.postMessage({ type: 'navigate', ...payload(attempt), owner_document_id: attempt.owner_document_id });
    } else channel.postMessage({ type: 'reconcile', ...payload(attempt), owner_document_id: attempt.owner_document_id });
    const confirmed = await Promise.race([receipt, new Promise((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); })]);
    if (!confirmed || !current() || !await selected(fetch, attempt, loadParser, timeoutMs) || !current()) fail();
    return { status: 'returned' };
  } finally { clearTimeout(timer); channel.close(); }
}
