import { exact, isPayload, UUID } from '../../shared/browser-tasks/wire.mjs';

export const BROWSER_AUTH_KEY = 'browser_auth_attempts_v2';
const ALARM = 'browser-auth-expiry';
const PATH = 'browser-ceremony/v1/complete';
const TOKEN = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/u;
const CEREMONY = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const NOTIFICATIONS = ['changed', 'retired', 'unavailable'];
// Chrome's WINDOW_CLOSED result; other failures do not establish closure.
const WINDOW_CLOSED = 'The user did not approve access.';

/** An exact correlation callback never establishes the domain outcome. */
export function parseBrowserAuthReturn(value, ceremony) {
  for (const notification of NOTIFICATIONS) {
    if (typeof value === 'string' && value === `${ceremony.completion.return_uri}?ceremony_id=${ceremony.ceremony_id}&attempt_id=${ceremony.attempt_id}&notification=${notification}`) {
      return { ceremony_id: ceremony.ceremony_id, attempt_id: ceremony.attempt_id, notification };
    }
  }
  throw Error('authentication_unconfirmed');
}

function validRecord(value) {
  return exact(value, ['task_id', 'ceremony_id', 'attempt_id', 'fingerprint', 'expires_at', 'sequence', 'phase', 'notification',
    ...(value?.phase === 'closed' ? ['closed_generation'] : [])])
    && UUID.test(value.task_id) && CEREMONY.test(value.ceremony_id) && TOKEN.test(value.attempt_id) && /^[a-f0-9]{64}$/u.test(value.fingerprint)
    && Number.isSafeInteger(value.expires_at) && value.expires_at > 0 && UUID.test(value.sequence)
    && ['opening', 'unknown', 'returned', 'closed'].includes(value.phase)
    && (value.phase !== 'closed' || UUID.test(value.closed_generation))
    && (value.phase === 'returned' ? NOTIFICATIONS.includes(value.notification) : value.notification === null);
}

/** Journal only bounded correlation. Entry URLs, returned URLs and API errors are never stored. */
export function createBrowserAuth({ chromeApi, hostedOrigin, now = Date.now }) {
  const active = new Map(), listeners = new Set();
  const launching = new Map();
  let queue = Promise.resolve(), disposed = false;
  const serial = work => { const next = queue.then(work); queue = next.catch(() => undefined); return next; };
  const changed = () => { for (const listener of listeners) listener(); };
  async function save(records) {
    await chromeApi.storage.local.set({ [BROWSER_AUTH_KEY]: records });
    if (records.length) await chromeApi.alarms.create(ALARM, { when: Math.min(...records.map(value => value.expires_at)) });
    else await chromeApi.alarms.clear(ALARM);
  }
  async function load() {
    const saved = (await chromeApi.storage.local.get(BROWSER_AUTH_KEY))[BROWSER_AUTH_KEY] ?? [];
    if (!Array.isArray(saved) || saved.length > 8 || JSON.stringify(saved).length > 8192
      || !saved.every(validRecord) || new Set(saved.map(value => value.task_id)).size !== saved.length) throw Error('authentication_unavailable');
    const records = saved.filter(value => value.expires_at > now());
    for (const [key, value] of active) if (value.expires_at <= now()) active.delete(key);
    if (records.length !== saved.length) await save(records);
    return records;
  }
  async function binding(payload) {
    if (disposed || !isPayload(payload) || payload.task.purpose !== 'browser.authentication'
      || payload.extension_id !== chromeApi.runtime.id || typeof hostedOrigin !== 'string'
      || payload.creator_account_id === null) throw Error('authentication_unavailable');
    const ceremony = payload.ceremony, origin = new URL(hostedOrigin);
    if (origin.protocol !== 'https:' || origin.origin !== hostedOrigin || origin.username || origin.password
      || ceremony.completion.mode !== 'extension-auth-flow'
      || ceremony.completion.return_uri !== `https://${chromeApi.runtime.id}.chromiumapp.org/${PATH}`
      || chromeApi.identity?.getRedirectURL(PATH) !== ceremony.completion.return_uri
      || !ceremony.entry_url.startsWith(`${hostedOrigin}/onboarding/browser-ceremonies/`)
      || !TOKEN.test(ceremony.entry_url.slice(`${hostedOrigin}/onboarding/browser-ceremonies/`.length))
      || !TOKEN.test(ceremony.attempt_id)) throw Error('authentication_unavailable');
    const expires_at = Math.floor(Math.min(payload.task.expires_at * 1000, Date.parse(ceremony.expires_at)));
    if (expires_at <= now() || expires_at - now() > 1800000) throw Error('task_expired');
    const { admission_generation: _generation, state: _state, ...task } = payload.task;
    const { preparation_recovery: _recovery, ...immutableCeremony } = ceremony;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({ task,
      extension_id: payload.extension_id, entry_source: payload.entry_source, participant: payload.participant,
      creator_account_id: payload.creator_account_id, ceremony: immutableCeremony })));
    return { task_id: payload.task.task_id, ceremony_id: ceremony.ceremony_id, attempt_id: ceremony.attempt_id, expires_at,
      fingerprint: Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('') };
  }
  function find(records, expected) {
    const record = records.find(value => value.task_id === expected.task_id);
    if (record && (record.fingerprint !== expected.fingerprint || record.ceremony_id !== expected.ceremony_id || record.attempt_id !== expected.attempt_id
      || record.expires_at !== expected.expires_at)) throw Error('task_changed');
    return record;
  }
  function recoverClosed(record, payload) {
    return record?.phase === 'closed' && payload.ceremony.preparation_recovery === true
      && record.closed_generation !== payload.task.admission_generation;
  }
  function view(record, payload) {
    if (!record) return { state: 'idle', notification: null };
    if (record.phase === 'closed') return { state: recoverClosed(record, payload) ? 'idle' : 'closed', notification: null };
    if (record.phase === 'opening' && active.get(record.task_id)?.sequence === record.sequence) return { state: 'opening', notification: null };
    return { state: 'checking', notification: { ceremony_id: record.ceremony_id, attempt_id: record.attempt_id,
      notification: record.phase === 'returned' ? record.notification : 'unavailable' } };
  }
  async function finish(expected, sequence, notification, closed, generation) {
    if (disposed) return;
    const records = await load(), record = find(records, expected);
    if (!record || record.sequence !== sequence || active.get(record.task_id)?.sequence !== sequence) return;
    record.phase = closed ? 'closed' : notification ? 'returned' : 'unknown';
    if (closed) record.closed_generation = generation;
    record.notification = notification?.notification ?? null;
    active.delete(record.task_id);
    await save(records); changed();
  }
  const alarm = value => {
    if (value.name === ALARM) void serial(async () => { await load(); changed(); }).catch(() => undefined);
  };
  chromeApi.alarms.onAlarm.addListener(alarm);
  void serial(async () => {
    const records = await load();
    if (records.length) await chromeApi.alarms.create(ALARM, { when: Math.min(...records.map(value => value.expires_at)) });
  }).catch(() => undefined);
  return Object.freeze({
    hasLiveFlow() { return [...launching.values(), ...[...active.values()].map(value => value.expires_at)]
      .some(deadline => deadline > now()); },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    read(payload) { return serial(async () => view(find(await load(), await binding(payload)), payload)); },
    launch(payload, guard) {
      const reservation = Symbol();
      launching.set(reservation, Math.min(payload?.task?.expires_at * 1000, Date.parse(payload?.ceremony?.expires_at)));
      return serial(async () => {
        const expected = await binding(payload), records = await load();
        let record = find(records, expected);
        await guard();
        if (disposed || expected.expires_at <= now()) throw Error('task_expired');
        // Reuse a live flow or a received callback. A lost worker can only
        // reopen this same ceremony after another explicit customer action.
        if (record && (active.has(record.task_id) || record.phase === 'returned'
          || record.phase === 'closed' && !recoverClosed(record, payload))) return view(record, payload);
        if (!record) {
          if (records.length >= 8) throw Error('authentication_unavailable');
          record = { ...expected }; records.push(record);
        }
        Object.assign(record, { sequence: crypto.randomUUID(), phase: 'opening', notification: null });
        delete record.closed_generation;
        await save(records);
        await guard();
        if (disposed || expected.expires_at <= now()) throw Error('task_expired');
        const sequence = record.sequence;
        active.set(record.task_id, { sequence, expires_at: record.expires_at });
        let flow;
        try { flow = chromeApi.identity.launchWebAuthFlow({ interactive: true, url: payload.ceremony.entry_url }); }
        catch { flow = Promise.reject(Error('authentication_unconfirmed')); }
        void Promise.resolve(flow).then(value => ({ notification: parseBrowserAuthReturn(value, payload.ceremony), closed: false }),
          error => ({ notification: null, closed: error?.message === WINDOW_CLOSED }))
          .catch(() => ({ notification: null, closed: false }))
          .then(result => serial(() => finish(expected, sequence, result.notification, result.closed, payload.task.admission_generation))).catch(() => changed());
        changed(); return view(record, payload);
      }).finally(() => launching.delete(reservation));
    },
    dispose() { disposed = true; active.clear(); launching.clear(); listeners.clear(); chromeApi.alarms.onAlarm.removeListener(alarm); },
  });
}
