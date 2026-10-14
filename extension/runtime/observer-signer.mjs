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
  const run = (request, work) => {
      const operation = queue.then(async () => {
        request.signal?.throwIfAborted();
        if (selectedHelper !== null && observer.helper?.tab_id !== selectedHelper) {
          select(existingHost, null);
        }
        try {
          return await work(request);
        } catch (error) {
          request.signal?.throwIfAborted();
          if (error?.code === 'context_lost') select(existingHost, null);
          if (request.refreshMode === 'never' || request.allowRecovery === false) throw error;
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
              return work({ ...request, signal });
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
              return work({ ...request, signal });
            },
          });
        }
      });
      queue = operation.catch(() => undefined);
      return operation;
  };
  return Object.freeze({
    read: (request) => run(request, (controls) => provider.read(controls)),
    prepare: (request) => run(request, async ({ signal }) => {
      const active = await provider.store.active();
      signal?.throwIfAborted();
      if (active && active.proof_policy_version === 3) {
        const operation = await provider.host.openOperation({ signal });
        await operation.assertContext({ signal });
        if (operation.documentIdentity && operation.documentIdentity === active.summary.activation_document) {
          try { if (options.parameterSource?.matchesGeneration(active)) return; }
          catch (error) {
            // Changed bytes under the same exact published revision require a
            // fresh proof. Withdrawal/expiry never permit another revision here.
            if (error?.code !== 'parameters_unavailable'
              || options.parameterSource?.inspect(active.config.source_revision) !== 'eligible') throw error;
          }
        }
      }
      await provider.refresh({ signal });
    }),
  });
}
