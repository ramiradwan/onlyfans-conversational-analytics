import { ChromeMainWorldSigningHost, createChromeBrowserSigningProvider } from 'local-authenticated-read-connector/browser-signing';

const OBSERVATION_WINDOW_MS = 3_000;

// Keep one provider/store writer while the observer owner serializes helper
// recovery. Each retry opens a new document-bound operation in the connector.
export async function createObserverSigningProvider(options, {
  observer,
  createProvider = createChromeBrowserSigningProvider,
  createHost = (controls) => new ChromeMainWorldSigningHost(controls),
  createHelperHost = (controls) => ChromeMainWorldSigningHost.createForOwnedHelper(controls),
  observationWindowMs = OBSERVATION_WINDOW_MS,
} = {}) {
  const provider = await createProvider({ ...options, captureTimeoutMs: observationWindowMs });
  const existingHost = provider.host;
  let selectedHelper = null;
  let queue = Promise.resolve();
  const select = (host, tabId) => {
    provider.host = host;
    selectedHelper = tabId;
  };
  return Object.freeze({
    read(request) {
      const operation = queue.then(async () => {
        request.signal?.throwIfAborted();
        if (selectedHelper !== null && observer.helper?.tab_id !== selectedHelper) {
          select(existingHost, null);
        }
        try {
          return await provider.read(request);
        } catch (error) {
          request.signal?.throwIfAborted();
          if (error?.code === 'context_lost') select(existingHost, null);
          if (error?.code !== 'browser_unavailable') throw error;
          await options.persistence.drain();
          request.signal?.throwIfAborted();
          return observer.recoverSigning({
            signal: request.signal,
            reserve: (signal) => options.reserveRecovery(signal),
            readExisting: async (tabId, signal) => {
              if (selectedHelper === tabId) throw error;
              select(createHost({ chromeApi: options.chromeApi, tabId,
                captureTimeoutMs: observationWindowMs }), tabId);
              return provider.read({ ...request, signal });
            },
            createHelper: async ({ helperPagePath, onCreated, trackCreation, signal }) => {
              const tabs = new Proxy(options.chromeApi.tabs, {
                get(target, property, receiver) {
                  const value = Reflect.get(target, property, receiver);
                  // Track Chrome's Promise form through the actual tab acknowledgement.
                  if (property === 'create') return (createOptions) => trackCreation(() => value.call(target, createOptions));
                  return typeof value === 'function' ? value.bind(target) : value;
                },
              });
              const chromeApi = new Proxy(options.chromeApi, {
                get(target, property, receiver) {
                  return property === 'tabs' ? tabs : Reflect.get(target, property, receiver);
                },
              });
              return createHelperHost({ chromeApi, helperPagePath, onCreated,
                signal, captureTimeoutMs: 12_000 });
            },
            readHelper: (host, signal) => {
              select(host, host.tabId);
              return provider.read({ ...request, signal });
            },
          });
        }
      });
      queue = operation.catch(() => undefined);
      return operation;
    },
  });
}
