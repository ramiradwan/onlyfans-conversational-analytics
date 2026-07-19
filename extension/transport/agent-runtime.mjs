import {
  AgentConfigClient,
  AtomicConfigActivator,
} from './agent-config-client.mjs';
import { AgentWebSocketClient } from './agent-websocket.mjs';
import { createChromeAdapter } from './chrome-adapter.mjs';
import { createConfigHttpAdapter } from './config-http-adapter.mjs';
import {
  DurableIngestOutbox,
  INGESTION_STORES,
} from './durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from './history-coordinator.mjs';
import {
  createIndexedDbIngestionStorage,
} from './indexeddb-ingestion-storage.mjs';

const noOp = () => {};
const SIGNER_STATE_KEY = 'signer-state';

/** Keep private signing generations inside the same account-hashed IndexedDB partition. */
export function createAccountSigningPersistence(storage, creatorAccountId) {
  const assertAccount = (record) => {
    if (
      typeof record !== 'object'
      || record === null
      || Array.isArray(record)
      || Object.keys(record).length !== 3
      || record.key !== SIGNER_STATE_KEY
      || record.creator_account_id !== creatorAccountId
      || typeof record.state !== 'object'
      || record.state === null
      || Array.isArray(record.state)
    ) {
      throw new Error('Stored signer state does not match its account partition');
    }
    return structuredClone(record.state);
  };
  return Object.freeze({
    async load() {
      const record = await storage.runTransaction(
        'readonly',
        [INGESTION_STORES.credentials],
        (tx) => tx.get(INGESTION_STORES.credentials, SIGNER_STATE_KEY),
      );
      return record === undefined ? null : assertAccount(record);
    },
    async save(state) {
      if (typeof state !== 'object' || state === null || Array.isArray(state)) {
        throw new Error('Signer state must be an object');
      }
      await storage.runTransaction(
        'readwrite',
        [INGESTION_STORES.credentials],
        (tx) => tx.put(INGESTION_STORES.credentials, {
          key: SIGNER_STATE_KEY,
          creator_account_id: creatorAccountId,
          state: structuredClone(state),
        }),
      );
    },
  });
}

/**
 * Owns the disposable in-memory Agent runtime for one MV3 service-worker lifetime.
 * Wake listeners are registered synchronously; durable state is loaded lazily and
 * initialization failures are retryable on the next wake event.
 */
export class AgentRuntime {
  constructor({
    initialize,
    registerWakeListeners,
    onStartupError = noOp,
    resolveBindingFingerprint = null,
    onBindingMatched = null,
  }) {
    if (typeof initialize !== 'function') throw new Error('Agent runtime initializer is required');
    if (typeof registerWakeListeners !== 'function') {
      throw new Error('Agent runtime wake-listener registrar is required');
    }
    this.initialize = initialize;
    this.registerWakeListeners = registerWakeListeners;
    this.onStartupError = onStartupError;
    this.resolveBindingFingerprint = resolveBindingFingerprint;
    this.onBindingMatched = onBindingMatched;
    this.transport = null;
    this.configuration = null;
    this.history = null;
    this.startupPromise = null;
    this.removeWakeListeners = null;
    this.listenersRegistered = false;
    this.bindingFingerprint = null;
    this.wakeListener = () => this.wake().catch(() => undefined);
  }

  registerListeners() {
    if (this.listenersRegistered) return;
    this.removeWakeListeners = this.registerWakeListeners(this.wakeListener) ?? null;
    this.listenersRegistered = true;
  }

  start() {
    this.registerListeners();
    return this.wake();
  }

  wake() {
    if (this.transport !== null) {
      if (this.resolveBindingFingerprint !== null) {
        return Promise.resolve(this.resolveBindingFingerprint()).then(async (resolution) => {
          const fingerprint = typeof resolution === 'object' && resolution !== null
            ? resolution.fingerprint
            : resolution;
          if (fingerprint !== this.bindingFingerprint) {
            const stale = this.transport;
            const staleHistory = this.history;
            this.transport = null;
            this.configuration = null;
            this.history = null;
            this.bindingFingerprint = null;
            staleHistory?.stop?.();
            stale.stop?.();
            await stale.outbox?.invalidateAccountEpoch?.();
            return this.wake();
          }
          await this.onBindingMatched?.(this.transport, resolution);
          return this.#reconcileTransport();
        });
      }
      return Promise.resolve(this.#reconcileTransport());
    }
    if (this.startupPromise !== null) return this.startupPromise;

    const attempt = Promise.resolve().then(() => this.#initialize());
    this.startupPromise = attempt;
    void attempt.then(
      () => {
        if (this.startupPromise === attempt) this.startupPromise = null;
      },
      () => {
        if (this.startupPromise === attempt) this.startupPromise = null;
      },
    );
    return attempt;
  }

  #reconcileTransport() {
    if (this.transport !== null) {
      try {
        if (typeof this.transport.reconcileConnection === 'function') {
          this.transport.reconcileConnection();
        } else {
          this.transport.ensureConnected();
        }
        void this.history?.wake().catch((error) => this.onStartupError(error));
      } catch (error) {
        return Promise.reject(error);
      }
      return this.transport;
    }
    throw new Error('Agent transport is unavailable');
  }

  async #initialize() {
    try {
      const components = await this.initialize();
      if (typeof components?.transport?.start !== 'function') {
        throw new Error('Agent runtime initializer did not provide a transport');
      }
      this.configuration = components.configuration ?? null;
      this.history = components.history ?? null;
      this.transport = components.transport;
      this.bindingFingerprint = components.bindingFingerprint ?? null;
      this.transport.start();
      return this.transport;
    } catch (error) {
      this.transport?.stop?.();
      this.history?.stop?.();
      this.transport = null;
      this.configuration = null;
      this.history = null;
      this.bindingFingerprint = null;
      this.onStartupError(error);
      throw error;
    }
  }
}

/** Compose the production Agent runtime behind injectable seams for deterministic tests. */
export function createAgentRuntime(options = {}) {
  const chromeAdapter = options.chromeAdapter ?? createChromeAdapter();
  const ingestionStorageFactory = options.ingestionStorageFactory
    ?? ((storageOptions) => createIndexedDbIngestionStorage(undefined, storageOptions));
  const outboxFactory = options.outboxFactory
    ?? ((outboxOptions) => new DurableIngestOutbox(outboxOptions));
  const configActivatorFactory = options.configActivatorFactory
    ?? (() => new AtomicConfigActivator());
  const configHttpFactory = options.configHttpFactory
    ?? (() => createConfigHttpAdapter());
  const configClientFactory = options.configClientFactory
    ?? ((configOptions) => new AgentConfigClient(configOptions));
  const transportFactory = options.transportFactory
    ?? ((transportOptions) => new AgentWebSocketClient(transportOptions));
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
              signer = await signerFactory({
                creatorAccountId,
                chromeApi: options.chromeApi ?? globalThis.chrome,
                persistence: createAccountSigningPersistence(accountStorage, creatorAccountId),
                expectedIdentity,
              });
              signerIdentity = expectedIdentity;
            }
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
