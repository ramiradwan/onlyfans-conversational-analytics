import { assertOnlyFansTabCanRun, guardMainWorldDispatch } from './read-only-frozen-tab-guard.mjs';

const noOp = () => {};
const SIGNER_STATE_KEY = 'signer-state';

export function createAccountSigningPersistence(
  storage,
  creatorAccountId,
  credentialsStore = 'credentials',
) {
  const pendingSaves = new Set();
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
    drain: () => Promise.allSettled([...pendingSaves]),
    async save(state) {
      if (typeof state !== 'object' || state === null || Array.isArray(state)) {
        throw new Error('Signer state must be an object');
      }
      const saved = storage.runTransaction(
        'readwrite',
        [credentialsStore],
        (tx) => tx.put(credentialsStore, {
          key: SIGNER_STATE_KEY,
          creator_account_id: creatorAccountId,
          state: structuredClone(state),
        }),
      );
      pendingSaves.add(saved);
      try { await saved; }
      finally { pendingSaves.delete(saved); }
    },
  });
}


function awaitSignerWork(promise, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason); };
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => {
      signal.removeEventListener('abort', abort);
    });
  });
}

/** One provider owns the account signing document, including any save already entered on abort. */
export function createLazyAccountSigner({
  creatorAccountId, storage, chromeApi, factory, expectedIdentity, signal,
}) {
  const persistence = createAccountSigningPersistence(storage, creatorAccountId);
  let owner = null;
  let construction = Promise.resolve();
  const identity = () => {
    const value = expectedIdentity();
    // Signer 0.2.0's absent-user-id bootstrap requires this exact string domain.
    if (typeof value !== 'string' || !/^[1-9][0-9]{0,255}$/.test(value)) {
      throw Object.assign(new Error('History acquisition requires an authorized platform creator ID'), {
        code: 'identity_required',
      });
    }
    return value;
  };
  const identityChanged = () => Object.assign(new Error('History signer identity changed'), {
    code: 'account_mismatch',
  });
  const resolveOwner = (requestedIdentity, operationSignal) => {
    const next = construction.then(async () => {
      operationSignal.throwIfAborted();
      if (identity() !== requestedIdentity) throw identityChanged();
      if (owner?.identity === requestedIdentity) return owner;
      owner?.controller.abort(identityChanged());
      owner = null;
      // A cancelled read can return before the signer's already-entered save settles.
      // A replacement store must load after that save, never race it with another cache.
      await persistence.drain();
      operationSignal.throwIfAborted();
      if (identity() !== requestedIdentity) throw identityChanged();
      const controller = new AbortController();
      const ownerSignal = AbortSignal.any([signal, controller.signal]);
      try {
        const provider = await factory({
          creatorAccountId,
          chromeApi: guardMainWorldDispatch(chromeApi, { signal: ownerSignal }),
          persistence,
          expectedIdentity: requestedIdentity,
          signal: AbortSignal.any([ownerSignal, operationSignal]),
        });
        operationSignal.throwIfAborted();
        if (identity() !== requestedIdentity) throw identityChanged();
        owner = { identity: requestedIdentity, provider, controller, signal: ownerSignal };
        return owner;
      } catch (error) {
        controller.abort(error);
        throw error;
      }
    });
    construction = next.then(() => undefined, () => undefined);
    return awaitSignerWork(next, operationSignal);
  };
  return Object.freeze({
    async read(request) {
      const operationSignal = request.signal ? AbortSignal.any([signal, request.signal]) : signal;
      operationSignal.throwIfAborted();
      const requestedIdentity = identity();
      const current = await resolveOwner(requestedIdentity, operationSignal);
      const readSignal = AbortSignal.any([operationSignal, current.signal]);
      readSignal.throwIfAborted();
      await assertOnlyFansTabCanRun(chromeApi);
      readSignal.throwIfAborted();
      const result = await current.provider.read({ ...request, signal: readSignal });
      readSignal.throwIfAborted();
      if (identity() !== requestedIdentity) throw identityChanged();
      return result;
    },
  });
}

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
    this.startupAbort = null;
    this.bindingResolution = null;
    this.startupGeneration = 0;
    this.removeWakeListeners = null;
    this.listenersRegistered = false;
    this.bindingFingerprint = null;
    this.drain = null;
    this.wakeListener = () => {
      void this.wake().catch(() => undefined);
    };
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
    this.startupGeneration += 1;
    this.startupAbort?.abort(Object.assign(new Error('runtime_suspended'), {
      code: 'runtime_suspended',
    }));
    this.startupAbort = null;

    const transport = this.transport;
    const history = this.history;
    const drain = this.drain;
    this.drain = null;
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
    await Promise.allSettled([pendingStartup, this.bindingResolution, drain?.()]);
  }

  wake() {
    if (this.bindingResolution !== null) return this.bindingResolution;
    if (this.transport !== null) {
      if (this.resolveBindingFingerprint !== null) {
        const transport = this.transport;
        const generation = this.startupGeneration;
        const signal = this.startupAbort?.signal;
        const attempt = Promise.resolve(this.resolveBindingFingerprint({ signal })).then(async (resolution) => {
          signal?.throwIfAborted();
          if (this.transport !== transport || generation !== this.startupGeneration) {
            throw Object.assign(new Error('stale_runtime'), { code: 'stale_runtime' });
          }
          const fingerprint = typeof resolution === 'object' && resolution !== null
            ? resolution.fingerprint
            : resolution;
          if (fingerprint !== this.bindingFingerprint) {
            const stale = this.transport;
            const staleHistory = this.history;
            const drain = this.drain;
            this.drain = null;
            this.transport = null;
            this.configuration = null;
            this.history = null;
            this.bindingFingerprint = null;
            staleHistory?.stop?.();
            stale.stop?.();
            try {
              await stale.outbox?.invalidateAccountEpoch?.();
            } finally {
              this.startupAbort?.abort(Object.assign(new Error('account_changed'), { code: 'account_changed' }));
              this.startupAbort = null;
              await drain?.();
            }
            if (generation !== this.startupGeneration) throw new Error('stale_runtime');
            this.bindingResolution = null;
            return this.wake();
          }
          await this.onBindingMatched?.(this.transport, resolution);
          signal?.throwIfAborted();
          return this.#reconcileTransport();
        });
        this.bindingResolution = attempt;
        void attempt.finally(() => {
          if (this.bindingResolution === attempt) this.bindingResolution = null;
        }).catch(() => undefined);
        return attempt;
      }
      return Promise.resolve(this.#reconcileTransport());
    }
    if (this.startupPromise !== null) return this.startupPromise;

    const generation = ++this.startupGeneration;
    const controller = new AbortController();
    this.startupAbort = controller;
    const attempt = Promise.resolve().then(() => this.#initialize(generation, controller));
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

  async #initialize(generation, controller) {
    let components = null;
    try {
      controller.signal.throwIfAborted();
      components = await this.initialize({ signal: controller.signal });
      controller.signal.throwIfAborted();
      if (generation !== this.startupGeneration) {
        const error = new Error('stale_startup');
        error.code = 'stale_startup';
        throw error;
      }
      if (typeof components?.transport?.start !== 'function') {
        throw new Error('Agent runtime initializer did not provide a transport');
      }
      this.drain = components.drain ?? null;
      this.configuration = components.configuration ?? null;
      this.history = components.history ?? null;
      this.transport = components.transport;
      this.bindingFingerprint = components.bindingFingerprint ?? null;
      this.transport.start();
      return this.transport;
    } catch (error) {
      components?.history?.stop?.();
      components?.transport?.stop?.();
      if (generation === this.startupGeneration) {
        this.transport = null;
        this.configuration = null;
        this.history = null;
        this.bindingFingerprint = null;
      }
      const cancelled = controller.signal.aborted;
      controller.abort(error);
      await components?.drain?.();
      if (!cancelled) this.onStartupError(error);
      throw error;
    }
  }
}
