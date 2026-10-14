import '../shared-setup/shell.css';
import { applyTheme } from '../shared-setup/presentation.js';
import { continueReceivingTransfer, startReceivingCode } from '../../app/provisioning/receiving-client.mjs';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

applyTheme(document);
const status = document.getElementById('receiving-status'), actions = document.getElementById('receiving-actions');
const form = document.getElementById('receiving-form'), code = document.getElementById('receiving-code');
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
let active = true, session, timer, busy = false;
const current = () => active && session && Date.now() < session.expires_at * 1000;
const onStatus = text => { if (active) status.textContent = text; };
const onRecovery = (text, action) => {
  if (!current()) return;
  const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
  button.addEventListener('click', async () => {
    if (!current() || busy) return;
    busy = true; button.disabled = true;
    try { await action(); } finally { busy = false; }
  }); actions.append(button);
};
const expire = () => { active = false; form.hidden = true; code.value = ''; actions.replaceChildren(); status.textContent = 'This receiving session expired. Open a fresh link in the desktop app.'; };
async function json(path, body) {
  const response = await fetch(path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
    signal: AbortSignal.timeout(10000), headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}) });
  if (!response.ok || !active || !/^application\/json(?:;|$)/iu.test(response.headers.get('content-type') ?? '')) throw Error('unavailable');
  return parseOnboardingJson(await response.text());
}
const options = () => ({ fetch: globalThis.fetch.bind(globalThis), document, journeyId: session.journey_id,
  registeredHostedUrl: session.hosted_url, current, onStatus, onRecovery });
async function start(readOnly = false) {
  try {
    const fields = new URLSearchParams(location.hash.slice(1));
    if (location.pathname === '/setup-code/entry') {
      if (fields.size !== 1 || !/^[A-Za-z0-9_-]{43}$/u.test(fields.get('code') || '')) throw Error('entry');
      const entry = fields.get('code'); history.replaceState(null, '', '/setup-code');
      const result = await json('/api/v1/setup-code/consume', { code: entry });
      if (Object.keys(result).join(',') !== 'journey_id' || !uuid.test(result.journey_id)) throw Error('entry');
      history.replaceState(null, '', '/setup-code#journey=' + result.journey_id);
    }
    session = await json('/api/v1/setup-code/session');
    if (Object.keys(session).sort().join(',') !== 'csrf_token,expires_at,hosted_url,journey_id,workspace_journey_id'
      || !uuid.test(session.journey_id) || !uuid.test(session.workspace_journey_id)
      || location.hash !== '#journey=' + session.journey_id || !Number.isFinite(session.expires_at)
      || !/^[A-Za-z0-9_-]{43}$/u.test(session.csrf_token) || session.expires_at > Date.now()/1000+1800) throw Error('session');
    if (!current()) { expire(); return; }
    clearTimeout(timer); timer = setTimeout(expire, session.expires_at*1000-Date.now());
    actions.replaceChildren();
    const result = await continueReceivingTransfer({ ...options(), readOnly });
    if (result === 'none' && current()) { form.hidden = false; onStatus('Enter the code shown on the other device.'); }
  } catch {
    onStatus('Receiving setup is unavailable. Open a fresh link in the desktop app.');
    active = false; clearTimeout(timer); form.hidden = true; code.value = ''; actions.replaceChildren();
  }
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !current()) return;
  const value = code.value.toUpperCase().replace(/[ -]/gu, '');
  if (!/^[0-9A-HJKMNP-TV-Z]{12}$/u.test(value)) { onStatus('Enter the 12-character setup code.'); return; }
  busy = true; document.getElementById('receiving-submit').disabled = true;
  code.value = ''; form.hidden = true;
  try { await startReceivingCode({ ...options(), setupCode: value, csrfToken: session.csrf_token }); }
  catch { onStatus('Receiving could not be confirmed. Open a fresh link in the desktop app to check its status.'); }
  finally { busy = false; }
});
window.addEventListener('pagehide', () => {active = false; clearTimeout(timer); code.value = '';});
window.addEventListener('pageshow', event => {if (event.persisted) {active = true; void start(true);}});
void start();
