import {
  AgentConfigClient,
  AtomicConfigActivator,
} from './agent-config-client.mjs';
import {
  AgentWebSocketClient,
  DEV_ACCOUNT_ID,
  DEV_AUTH_TICKET,
} from './agent-websocket.mjs';
import { createChromeAdapter } from './chrome-adapter.mjs';
import { createConfigHttpAdapter } from './config-http-adapter.mjs';
import { DurableIngestOutbox } from './durable-outbox.mjs';
import { createIndexedDbIngestionStorage } from './indexeddb-ingestion-storage.mjs';

const noOp = () => {};

/**
 * Owns the disposable in-memory Agent runtime for one MV3 service-worker lifetime.
 * Wake listeners are registered synchronously; durable state is loaded lazily and
 * initialization failures are retryable on the next wake event.
 */
export class AgentRuntime {
  constructor({ initialize, registerWakeListeners, onStartupError = noOp }) {
    if (typeof initialize !== 'function') throw new Error('Agent runtime initializer is required');
    if (typeof registerWakeListeners !== 'function') {
      throw new Error('Agent runtime wake-listener registrar is required');
    }
    this.initialize = initialize;
    this.registerWakeListeners = registerWakeListeners;
    this.onStartupError = onStartupError;
    this.transport = null;
    this.configuration = null;
    this.startupPromise = null;
    this.removeWakeListeners = null;
    this.listenersRegistered = false;
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
      this.transport.ensureConnected();
      return Promise.resolve(this.transport);
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

  async #initialize() {
    try {
      const components = await this.initialize();
      if (typeof components?.transport?.start !== 'function') {
        throw new Error('Agent runtime initializer did not provide a transport');
      }
      this.configuration = components.configuration ?? null;
      this.transport = components.transport;
      this.transport.start();
      return this.transport;
    } catch (error) {
      this.transport?.stop?.();
      this.transport = null;
      this.configuration = null;
      this.onStartupError(error);
      throw error;
    }
  }
}

/** Compose the production Agent runtime behind injectable seams for deterministic tests. */
export function createAgentRuntime(options = {}) {
  const chromeAdapter = options.chromeAdapter ?? createChromeAdapter();
  const ingestionStorageFactory = options.ingestionStorageFactory
    ?? (() => createIndexedDbIngestionStorage());
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
  const creatorAccountId = options.creatorAccountId ?? DEV_ACCOUNT_ID;
  const authTicket = options.authTicket ?? DEV_AUTH_TICKET;

  return new AgentRuntime({
    registerWakeListeners: (listener) => chromeAdapter.onWake(listener),
    onStartupError: options.onStartupError,
    initialize: async () => {
      const identity = await chromeAdapter.loadAgentIdentity();
      const durableOutbox = outboxFactory({
        storage: ingestionStorageFactory(),
        legacyStorage: chromeAdapter,
      });
      const ingestionState = await durableOutbox.initialize();
      identity.lastAcknowledgedSourceSeq = Math.max(
        identity.lastAcknowledgedSourceSeq,
        ingestionState.acknowledged_source_seq,
      );

      let transport = null;
      const configuration = configClientFactory({
        identity,
        creatorAccountId,
        authTicket,
        http: configHttpFactory(),
        persistence: chromeAdapter,
        activator: configActivatorFactory(),
        reportApplied: (report) => transport?.sendConfigApplied(report) ?? false,
        onUnauthorized: () => transport?.stop(),
      });
      await configuration.initialize();

      transport = transportFactory({
        identity,
        persistence: chromeAdapter,
        outbox: durableOutbox,
        configClient: configuration,
        health: () => configuration.healthSummary(),
      });
      return { transport, configuration };
    },
  });
}
