import '../shared-setup/shell.css';
import { applyTheme } from '../shared-setup/presentation.js';
import { helperGeneration } from '../../shared/browser-tasks/wire.mjs';
import { BrowserTaskClient } from './client.js';
import { renderBrowserTask } from './view.js';
import { consumeLaunchIntent } from './launch-intent.js';
import { openAccessDocument } from './access-document.js';

applyTheme(document);
try {
  const entry = document.getElementById('access-document-entry');
  if (entry) {
    const ticket = entry.content.textContent;
    entry.remove();
    openAccessDocument(ticket, helperGeneration(location.href));
  } else {
    const generation = helperGeneration(location.href);
    const nativeLaunch = consumeLaunchIntent(generation);
    const client = new BrowserTaskClient({ generation, nativeLaunch, receive: value => {
      renderBrowserTask(document, value);
    } });
    document.getElementById('pairing-replace').addEventListener('click', () => { void client.replacePairing(); });
    document.getElementById('extension-open').addEventListener('click', () => { void client.open(); });
    document.getElementById('authentication-open').addEventListener('click', () => { void client.authenticate(); });
    window.addEventListener('pagehide', () => client.stop());
    void client.start();
  }
} catch { renderBrowserTask(document, { state: 'task_changed' }); }
