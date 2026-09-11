import { assertOnlyFansTabCanRun, guardMainWorldDispatch } from './read-only-frozen-tab-guard.mjs';
import {
  AgentConfigClient,
  AtomicConfigActivator,
} from './agent-config-client.mjs';
import { AgentWebSocketClient } from './agent-websocket.mjs';
import { createChromeAdapter } from './chrome-adapter.mjs';
import { createConfigHttpAdapter } from './config-http-adapter.mjs';
import { DurableIngestOutbox } from './durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from './history-coordinator.mjs';
import { createIndexedDbIngestionStorage } from './indexeddb-ingestion-storage.mjs';
import { createLifecycleStorage } from '../runtime/lifecycle-storage.mjs';
import { LOCAL_SERVICE_WS } from './local-service-endpoints.mjs';
import { AgentRuntime, createAccountSigningPersistence } from './agent-runtime-core.mjs';

export { AgentRuntime, createAccountSigningPersistence } from './agent-runtime-core.mjs';

export function createAgentRuntime(options = {}) {
  const chromeApi = options.chromeApi ?? globalThis.chrome;
  const extensionVersion = options.extensionVersion ?? chromeApi.runtime.getManifest().version;
  const chromeAdapter = options.chromeAdapter ?? createChromeAdapter();
  const ingestionStorageFactory = options.ingestionStorageFactory
    ?? ((storageOptions) => createIndexedDbIngestionStorage(undefined, storageOptions));
  const outboxFactory = options.outboxFactory ?? ((outboxOptions) => new DurableIngestOutbox(outboxOptions));
  const configActivatorFactory = options.configActivatorFactory ?? (() => new AtomicConfigActivator());
  const configHttpFactory = options.configHttpFactory ?? (() => createConfigHttpAdapter());
  const configClientFactory = options.configClientFactory ?? ((configOptions) => new AgentConfigClient(configOptions));
  const transportFactory = options.transportFactory ?? ((transportOptions) => new AgentWebSocketClient({
    ...transportOptions, url: LOCAL_SERVICE_WS, scheduler: options.scheduler,
  }));
  const historyCoordinatorFactory = options.historyCoordinatorFactory
    ?? ((historyOptions) => new HistoryAcquisitionCoordinator(historyOptions));
  const signerFactory = options.signerFactory ?? null;
  const resolveBinding = (context = {}) => (
    options.creatorAccountId && options.authTicket
      ? { creatorAccountId: options.creatorAccountId, authTicket: options.authTicket, storageKey: options.storageKey }
      : chromeAdapter.loadBrainBinding(context)
  );
  const bindingFingerprint = (binding) => binding.creatorAccountId;
  return new AgentRuntime({
    registerWakeListeners: (listener) => chromeAdapter.onWake(listener),
    onStartupError: options.onStartupError,
    resolveBindingFingerprint: async (context) => {
      const binding = await resolveBinding(context);
      return { fingerprint: bindingFingerprint(binding), authTicket: binding.authTicket };
    },
    onBindingMatched: (transport, resolution) => transport.replaceAuthTicket?.(resolution.authTicket),
    initialize: async ({ signal }) => {
      signal.throwIfAborted();
      const binding = await resolveBinding({ signal });
      signal.throwIfAborted();
      const { creatorAccountId, authTicket } = binding;
      if (options.ingestionStorageFactory === undefined && !binding.storageKey) {
        throw new Error('Brain-unsealed Full-mode encryption key is required');
      }
      const reconnectAuthTicket = typeof chromeAdapter.loadReconnectAuthTicket === 'function'
        ? await chromeAdapter.loadReconnectAuthTicket(creatorAccountId, { signal }) : null;
      signal.throwIfAborted();
      const agentInstallationId = typeof chromeAdapter.loadAgentInstallationId === 'function'
        ? await chromeAdapter.loadAgentInstallationId()
        : (await chromeAdapter.loadAgentIdentity()).agentInstallationId;
      signal.throwIfAborted();
      const rawStorage = ingestionStorageFactory({ creatorAccountId, encryptionKey: binding.storageKey });
      const accountStorage = createLifecycleStorage(rawStorage, signal);
      const durableOutbox = outboxFactory({ storage: accountStorage, creatorAccountId });
      const ingestionState = await durableOutbox.initialize();
      signal.throwIfAborted();
      const identity = {
        agentInstallationId,
        agentStreamId: ingestionState.agent_stream_id,
        lastAcknowledgedSourceSeq: ingestionState.acknowledged_source_seq,
        appliedConfigRevision: ingestionState.applied_config_revision,
        accountEpoch: ingestionState.account_epoch,
      };
      let transport = null;
      let history = null;
      const configuration = configClientFactory({
        signal,
        identity,
        creatorAccountId,
        http: configHttpFactory(),
        persistence: durableOutbox,
        activator: configActivatorFactory(),
        reportApplied: (report) => {
          if (signal.aborted) return false;
          const sent = transport?.sendConfigApplied(report) ?? false;
          void history?.wake().catch(() => undefined);
          return sent;
        },
        onUnauthorized: () => transport?.stop(),
      });
      await configuration.initialize();
      signal.throwIfAborted();
      if (signerFactory !== null) {
        let signer = null;
        let signerIdentity = null;
        const lazySigner = {
          async read(request) {
            const operationSignal = request.signal ? AbortSignal.any([signal, request.signal]) : signal;
            operationSignal.throwIfAborted();
            const expectedIdentity = configuration.activeDocument?.history_acquisition?.authorized_platform_creator_id ?? null;
            if (typeof expectedIdentity !== 'string' || expectedIdentity.length === 0) {
              throw new Error('History acquisition has no authorized signer identity');
            }
            if (signer === null || signerIdentity !== expectedIdentity) {
              signer = await signerFactory({
                creatorAccountId,
                chromeApi: guardMainWorldDispatch(chromeApi, { signal }),
                persistence: createAccountSigningPersistence(accountStorage, creatorAccountId),
                expectedIdentity,
              });
              signerIdentity = expectedIdentity;
            }
            operationSignal.throwIfAborted();
            await assertOnlyFansTabCanRun(chromeApi);
            operationSignal.throwIfAborted();
            const result = await signer.read({ ...request, signal: operationSignal });
            operationSignal.throwIfAborted();
            return result;
          },
        };
        history = historyCoordinatorFactory({
          outbox: durableOutbox,
          signer: lazySigner,
          configuration: () => configuration.activeDocument,
          session: () => transport?.session == null ? null : { ...transport.session, applied_config_revision: identity.appliedConfigRevision },
        });
      }
      signal.throwIfAborted();
      transport = transportFactory({
        identity,
        creatorAccountId,
        authTicket,
        reconnectAuthTicket,
        extensionVersion,
        signal,
        persistReconnectAuthTicket: typeof chromeAdapter.saveReconnectAuthTicket === 'function'
          ? (ticket, configAuthTicket, controls = {}) => chromeAdapter.saveReconnectAuthTicket({
              creatorAccountId, authTicket: ticket, configAuthTicket, agentInstallationId,
            }, { ...controls, signal: controls.signal ? AbortSignal.any([signal, controls.signal]) : signal }) : undefined,
        persistence: durableOutbox,
        outbox: durableOutbox,
        configClient: configuration,
        health: () => configuration.healthSummary(),
        onSession: () => { if (!signal.aborted) void history?.wake().catch(() => undefined); },
        onSessionLost: () => history?.cancelCurrent?.('Agent session ended'),
      });
      signal.throwIfAborted();
      return { transport, configuration, history, drain: () => accountStorage.drain(), bindingFingerprint: bindingFingerprint(binding) };
    },
  });
}
