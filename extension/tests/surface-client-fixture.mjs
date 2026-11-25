
import { createSurfaceClient } from '../ui/surface-client.mjs';

function listeners() {
  const registered = new Set();
  return {
    addListener: listener => registered.add(listener),
    removeListener: listener => registered.delete(listener),
    deliver: (...values) => { for (const listener of registered) listener(...values); },
  };
}

const accountEpoch = '11111111-1111-4111-8111-111111111111';
export const fullStatus = {
  consent: { mode: 'full', resume_mode: null, consent_epoch: accountEpoch },
  delivery: { transport_state: 'authenticated' }, phase: 'identity', brain_reachable: true,
};
export const offStatus = {
  ...fullStatus, consent: { ...fullStatus.consent, mode: 'revoked' }, brain_reachable: false,
  delivery: { transport_state: 'disconnected' },
};

export function surfaceHarness(t) {
  let current = fullStatus, count = 0, heldStatus = null;
  const onConnect = listeners(), posts = [];
  let activePort;
  const url = path => 'chrome-extension://' + 'a'.repeat(32) + '/' + path;
  const chromeApi = {
    runtime: {
      id: 'a'.repeat(32), onConnect, getURL: url,
      async sendMessage(message) {
        if (message.type.includes('legal')) return { ok: true, result: { configured: false } };
        count++;
        const read = heldStatus;
        heldStatus = null;
        return { ok: true, result: read ? await read : current };
      },
      connect({ name }) {
        const fromWorker = listeners(), fromSurface = listeners();
        const disconnect = listeners(), workerDisconnect = listeners();
        const closePort = () => { disconnect.deliver(); workerDisconnect.deliver(); };
        activePort = { fromWorker, closePort };
        onConnect.deliver({ name, sender: { id: this.id, url: url('popup.html'), frameId: 0 },
          onMessage: fromSurface, onDisconnect: workerDisconnect,
          postMessage: value => queueMicrotask(() => fromWorker.deliver(value)) });
        return { onMessage: fromWorker, onDisconnect: disconnect, disconnect: closePort,
          postMessage: value => { posts.push(value); queueMicrotask(() => fromSurface.deliver(value)); } };
      },
    },
    storage: {
      session: { async get() { return {}; } },
      onChanged: listeners(),
    },
  };
  const replacements = {
    chrome: chromeApi, fetch: async () => Response.json({}),
    document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
  };
  const originals = new Map(Object.keys(replacements).map(key => [key, globalThis[key]]));
  Object.assign(globalThis, replacements);
  const errors = [];
  const client = createSurfaceClient(() => {}, error => errors.push(error));
  t.after(() => {
    client.stop();
    for (const [key, value] of originals) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  });
  return { client, chromeApi, posts, errors, get fromWorker() { return activePort.fromWorker; },
    closePort: () => activePort.closePort(), setStatus: value => { current = value; },
    holdStatus: promise => { heldStatus = promise; }, readCount: () => count };
}
