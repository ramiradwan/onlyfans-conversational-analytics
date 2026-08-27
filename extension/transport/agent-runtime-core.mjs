const noOp = () => {};
const SIGNER_STATE_KEY = 'signer-state';

/** Keep private signing generations inside the same account-hashed IndexedDB partition. */
export function createAccountSigningPersistence(
  storage,
  creatorAccountId,
  credentialsStore = 'credentials',
) {
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
        [credentialsStore],
        (tx) => tx.get(credentialsStore, SIGNER_STATE_KEY),
      );
      return record === undefined ? null : assertAccount(record);
    },
    async save(state) {
      if (typeof state !== 'object' || state === null || Array.isArray(state)) {
        throw new Error('Signer state must be an object');
      }
      await storage.runTransaction(
        'readwrite',
        [credentialsStore],
        (tx) => tx.put(credentialsStore, {
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

  async suspend() {
    const pendingStartup = this.startupPromise;
    if (pendingStartup !== null) await pendingStartup.catch(() => undefined);
    const transport = this.transport;
    const history = this.history;
    this.transport = null;
    this.configuration = null;
    this.history = null;
    this.bindingFingerprint = null;
    this.startupPromise = null;
    history?.stop?.();
    transport?.stop?.();
    this.removeWakeListeners?.();
    this.removeWakeListeners = null;
    this.listenersRegistered = false;
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
