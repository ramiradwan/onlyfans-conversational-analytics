import { exact, isPayload } from '../../shared/browser-tasks/wire.mjs';
import { connectExtension } from './extension.js';
import { renderBrowserTask } from './view.js';

export function openAccessDocument(ticket, generation) {
  if (window.top !== window || !generation || !/^[A-Za-z0-9_-]{43}$/u.test(ticket ?? '')) {
    throw Error('entry_unavailable');
  }
  const socket = new WebSocket('ws://bridge.localhost:17871/api/v1/browser-tasks/access-document', 'ofca.browser-access.v1');
  let payload, extension, closing = false, completed = false, reporting = false, missing = false;
  const show = state => renderBrowserTask(document, { state, payload });
  function stop() {
    if (closing) return;
    closing = true;
    extension?.close();
    socket.close();
    if (!completed) show('task_changed');
  }
  function attach() {
    if (closing || reporting || !payload) return;
    extension?.close();
    extension = connectExtension({ payload, receive(value) {
      if (closing || reporting) return;
      missing = ['unavailable', 'connection_lost'].includes(value.state);
      show(value.state);
      if (['ready', 'paused'].includes(value.state)) {
        reporting = true;
        socket.send(JSON.stringify(value.observation));
      }
    } });
  }
  document.getElementById('extension-open').addEventListener('click', () => extension?.open());
  document.getElementById('extension-install').target = '_blank';
  // The installer may select the Store tab. Returning to the original helper
  // retries discovery; navigations and reloads always need a fresh desktop entry.
  window.addEventListener('ofca-access-extension-installed', attach);
  document.addEventListener('visibilitychange', () => {
    if (missing && document.visibilityState === 'visible') attach();
  });
  window.addEventListener('pagehide', stop, { once: true });
  socket.addEventListener('open', () => { socket.send(ticket); ticket = null; });
  socket.addEventListener('close', stop, { once: true });
  socket.addEventListener('error', stop, { once: true });
  socket.addEventListener('message', event => {
    try {
      const value = JSON.parse(event.data);
      if (reporting && exact(value, ['state']) && value.state === 'observed') {
        completed = true;
        show('reported');
        stop();
        return;
      }
      if (payload || !isPayload(value) || value.task.purpose !== 'browser.access'
        || value.task.admission_generation !== generation || value.task.expires_at * 1000 <= Date.now()) throw Error('invalid_task');
      payload = value;
      document.documentElement.dataset.accessDocument = 'true';
      attach();
    } catch { stop(); }
  });
}
