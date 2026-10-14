import '../shared-setup/shell.css';
import '../shared-setup/providers.css';
import { applyTheme } from '../shared-setup/presentation.js';
import { DesktopSetupClient } from './client.js';
import { renderSetup } from './view.js';
import { openBrowserTask, openSetupCode } from './browser-task.js';
import { DesktopEnrollment } from './enrollment.js';

applyTheme(document);
const fields = new URLSearchParams(location.hash.slice(1));
let client;
try {
  if (fields.size !== 1 || !fields.has('presentation')) throw new Error('session_unavailable');
  let current = { snapshot: null, pending: null, connected: false, error: null };
  let choosingAccount = false;
  let acting = null;
  let finished = false;
  const render = () => {
    const authentication = client?.session?.stage === 'runtime' ? enrollment.state : null;
    const continuing = !finished && current.connected && !current.error
      && current.snapshot?.next_task === 'complete' && current.snapshot.local_ready === true
      && authentication?.checked === true && authentication.authenticated === true
      && !authentication.busy && !authentication.error;
    renderSetup(document, { ...current, choosingAccount, acting, authentication, continuing });
    if (continuing) {
      finished = true;
      location.replace('/');
    }
  };
  const enrollment = new DesktopEnrollment({ receive: render, refresh: () => client.connect() });
  client = new DesktopSetupClient({ generation: fields.get('presentation'), receive: state => {
    if (current.snapshot?.authentication?.authentication_attempt?.authentication_attempt_id
      !== state.snapshot?.authentication?.authentication_attempt?.authentication_attempt_id) choosingAccount = false;
    current = state; render();
    if (client.session?.stage === 'runtime' && state.connected && !enrollment.state.checked && !enrollment.state.busy) void enrollment.check();
  }, openBrowser: openBrowserTask });
  const act = async (action, options) => {
    if (acting) return;
    acting = action; render();
    try { await client.act(action, options); choosingAccount = false; }
    catch { client.notify('operation_unconfirmed'); }
    finally { acting = null; render(); }
  };
  document.getElementById('task-action').addEventListener('click', event => { void act(event.currentTarget.dataset.action); });
  for (const button of document.querySelectorAll('#sign-in-options [data-provider]')) {
    button.addEventListener('click', event => { void act(event.currentTarget.dataset.action, { provider: event.currentTarget.dataset.provider }); });
  }
  document.getElementById('sign-in-switch').addEventListener('click', () => {
    choosingAccount = true; render();
    document.querySelector('#sign-in-options button:not([hidden])')?.focus();
  });
  document.getElementById('sign-in-back').addEventListener('click', () => {
    choosingAccount = false; render(); document.getElementById('sign-in-switch').focus();
  });
  document.getElementById('task-reconnect').addEventListener('click', () => { void client.connect(); });
  document.getElementById('browser-reopen').addEventListener('click', async () => {
    if (acting) return;
    acting = 'browser.open'; render();
    try { await client.launchBrowser(true, current.snapshot?.authentication?.browser_operation_id || undefined); }
    catch { client.notify('browser_unavailable'); }
    finally { acting = null; render(); }
  });
  document.getElementById('setup-code-open').addEventListener('click', async event => {
    event.currentTarget.disabled = true;
    try { await openSetupCode(client); }
    catch (error) { client.notify(error === 'chrome_unavailable' ? 'chrome_unavailable' : 'browser_unavailable'); }
    finally { render(); }
  });
  document.getElementById('local-authenticate').addEventListener('click', () => { void enrollment.authenticate(); });
  document.getElementById('local-auth-retry').addEventListener('click', () => { void enrollment.check(); });
  window.addEventListener('pagehide', () => { finished = true; client.stop(); enrollment.stop(); });
  void client.connect();
} catch {
  renderSetup(document, { snapshot: null, pending: null, connected: false, error: 'session_unavailable', deliberate: false });
}
