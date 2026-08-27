import {
  AtomicConfigActivator,
  ReadOnlyAgentConfigClient,
} from './read-only-agent-config-client.mjs';
import { ReadOnlyAgentWebSocketClient } from './read-only-agent-websocket.mjs';
import { READ_ONLY_CAPABILITIES } from '../protocol/read-only.mjs';
import { createChromeAdapter } from './read-only-chrome-adapter.mjs';
import { createReadOnlyConfigHttpAdapter } from './read-only-config-http-adapter.mjs';
import { DurableIngestOutbox } from './read-only-durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from './read-only-history-coordinator.mjs';
import {
  assertOnlyFansTabCanRun,
  guardMainWorldDispatch,
} from './read-only-frozen-tab-guard.mjs';
import {
  createReadOnlyIndexedDbIngestionStorage,
} from './read-only-indexeddb-ingestion-storage.mjs';

import {
  AgentRuntime,
  createAccountSigningPersistence,
} from './agent-runtime-core.mjs';



/** Compose the production Agent runtime behind injectable seams for deterministic tests. */
export function createReadOnlyAgentRuntime(options = {}) {
  const chromeAdapter = options.chromeAdapter ?? createChromeAdapter();
  const ingestionStorageFactory = options.ingestionStorageFactory
    ?? ((storageOptions) => createReadOnlyIndexedDbIngestionStorage(undefined, storageOptions));
  const outboxFactory = options.outboxFactory
    ?? ((outboxOptions) => new DurableIngestOutbox(outboxOptions));
  const configActivatorFactory = options.configActivatorFactory
    ?? (() => new AtomicConfigActivator());
  const configHttpFactory = options.configHttpFactory
    ?? (() => createReadOnlyConfigHttpAdapter());
  const configClientFactory = options.configClientFactory
    ?? ((configOptions) => new ReadOnlyAgentConfigClient(configOptions));
  const transportFactory = options.transportFactory
    ?? ((transportOptions) => new ReadOnlyAgentWebSocketClient(transportOptions));
  const historyCoordinatorFactory = options.historyCoordinatorFactory
    ?? ((historyOptions) => new HistoryAcquisitionCoordinator(historyOptions));
  const signerFactory = options.signerFactory ?? null;
  const resolveBinding = () => (
    options.creatorAccountId && options.authTicket
      ? { creatorAccountId: options.creatorAccountId, authTicket: options.authTicket }
      : chromeAdapter.loadBrainBinding()
  );
  const bindingFingerprint = (binding) => binding.creatorAccountId;
  return new AgentRuntime({
    registerWakeListeners: (listener) => chromeAdapter.onWake(listener),
    onStartupError: options.onStartupError,
    resolveBindingFingerprint: async () => {
      const binding = await resolveBinding();
      return {
        fingerprint: bindingFingerprint(binding),
        authTicket: binding.authTicket,
      };
    },
    onBindingMatched: (transport, resolution) => {
      transport.replaceAuthTicket?.(resolution.authTicket);
    },
    initialize: async () => {
      const binding = await resolveBinding();
      const { creatorAccountId, authTicket } = binding;
      const reconnectAuthTicket = typeof chromeAdapter.loadReconnectAuthTicket === 'function'
        ? await chromeAdapter.loadReconnectAuthTicket(creatorAccountId)
        : null;
      const agentInstallationId = typeof chromeAdapter.loadAgentInstallationId === 'function'
        ? await chromeAdapter.loadAgentInstallationId()
        : (await chromeAdapter.loadAgentIdentity()).agentInstallationId;
      const accountStorage = ingestionStorageFactory({ creatorAccountId });
      const durableOutbox = outboxFactory({
        storage: accountStorage,
        creatorAccountId,
      });
      const ingestionState = await durableOutbox.initialize();
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
        identity,
        capabilities: READ_ONLY_CAPABILITIES,
        creatorAccountId,
        http: configHttpFactory(),
        persistence: durableOutbox,
        activator: configActivatorFactory(),
        reportApplied: (report) => {
          const sent = transport?.sendConfigApplied(report) ?? false;
          void history?.wake().catch(() => undefined);
          return sent;
        },
        onUnauthorized: () => transport?.stop(),
      });
      await configuration.initialize();

      if (signerFactory !== null) {
        let signer = null;
        let signerIdentity = null;
        const lazySigner = {
          async read(request) {
            const expectedIdentity = configuration.activeDocument
              ?.history_acquisition
              ?.authorized_platform_creator_id ?? null;
            if (typeof expectedIdentity !== 'string' || expectedIdentity.length === 0) {
              throw new Error('History acquisition has no authorized signer identity');
            }
            if (signer === null || signerIdentity !== expectedIdentity) {
              const chromeApi = options.chromeApi ?? globalThis.chrome;
              signer = await signerFactory({
                creatorAccountId,
                chromeApi: guardMainWorldDispatch(chromeApi),
                persistence: createAccountSigningPersistence(accountStorage, creatorAccountId),
                expectedIdentity,
              });
              signerIdentity = expectedIdentity;
            }
            // This happens before the signer can mint a request. The guarded Chrome
            // API performs the matching check immediately before MAIN-world dispatch.
            await assertOnlyFansTabCanRun(options.chromeApi ?? globalThis.chrome);
            return signer.read(request);
          },
        };
        history = historyCoordinatorFactory({
          outbox: durableOutbox,
          signer: lazySigner,
          configuration: () => configuration.activeDocument,
          session: () => transport?.session === null || transport?.session === undefined
            ? null
            : {
                ...transport.session,
                applied_config_revision: identity.appliedConfigRevision,
              },
        });
      }

      transport = transportFactory({
        identity,
        capabilities: READ_ONLY_CAPABILITIES,
        creatorAccountId,
        authTicket,
        reconnectAuthTicket,
        persistReconnectAuthTicket: typeof chromeAdapter.saveReconnectAuthTicket === 'function'
          ? (ticket) => chromeAdapter.saveReconnectAuthTicket({
              creatorAccountId,
              authTicket: ticket,
            })
          : undefined,
        persistence: durableOutbox,
        outbox: durableOutbox,
        configClient: configuration,
        health: () => configuration.healthSummary(),
        onSession: () => { void history?.wake().catch(() => undefined); },
        onSessionLost: () => { history?.cancelCurrent?.('Agent session ended'); },
      });
      return {
        transport,
        configuration,
        history,
        bindingFingerprint: bindingFingerprint(binding),
      };
    },
  });
}
