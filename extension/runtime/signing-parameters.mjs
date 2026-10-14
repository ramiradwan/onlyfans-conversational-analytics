import { createVerifiedSigningParameterSource } from 'local-authenticated-read-connector/browser-signing';
import { signingPublicationTrust } from './signing-publication-trust.mjs';
import { parameterDependency, validateParameterWait } from '../protocol/signing-parameters.mjs';

export const PARAMETER_STATE_KEY = 'signing_publication_authority_v1';
export const PARAMETER_INITIALIZED_KEY = 'signing_publication_initialized_v1';
export const PARAMETER_ALARM = 'ofca-signing-parameter-dependency';
const REFRESH_MS = 6 * 3600_000;

export function createParameterPersistence(storage) {
  let queue = Promise.resolve();
  const write = (state, marker) => {
    const writing = queue.then(() => storage.set({ [PARAMETER_STATE_KEY]: structuredClone(state), [PARAMETER_INITIALIZED_KEY]: marker }));
    queue = writing.catch(() => undefined);
    return writing;
  };
  return Object.freeze({
    async load() {
      // Consent/preview already restrict this same area. This does not change
      // the storage policy of another consumer or touch encrypted credentials.
      await storage.setAccessLevel?.({ accessLevel: 'TRUSTED_CONTEXTS' });
      const saved = await storage.get([PARAMETER_STATE_KEY, PARAMETER_INITIALIZED_KEY]);
      if (!Object.hasOwn(saved, PARAMETER_STATE_KEY)) {
        if (saved[PARAMETER_INITIALIZED_KEY] !== undefined) throw new Error('Publication authority disappeared');
        await write(null, 'first_use');
        return null;
      }
      if (!['first_use', 'checkpointed'].includes(saved[PARAMETER_INITIALIZED_KEY])
        || (saved[PARAMETER_STATE_KEY] === null && saved[PARAMETER_INITIALIZED_KEY] !== 'first_use')) throw new Error('Publication authority marker is corrupt');
      return structuredClone(saved[PARAMETER_STATE_KEY]);
    },
    save(state) {
      return write(state, 'checkpointed');
    },
    drain: () => queue,
  });
}

/** Exactly one verified publication owner per worker, across account owners. */
export function createWorkerParameterOwner({ chromeApi, resolver, now = Date.now, trust = signingPublicationTrust } = {}) {
  const persistence = createParameterPersistence(chromeApi.storage.local);
  let sourcePromise = null;
  let queue = Promise.resolve();
  let activeContext = null;
  let activeOperation = null;
  let closed = false;
  let nextRefresh = 0;
  const waits = new Map();
  const source = () => sourcePromise ??= createVerifiedSigningParameterSource({ trust, persistence, now,
    resolver: async (request, controls) => {
      const operation = activeOperation;
      const context = activeContext;
      if (!operation || !context || closed) throw Object.assign(new Error('Signing dependency unavailable'), { code: 'parameters_unavailable' });
      operation.revision = request.source_revision;
      context.signal?.throwIfAborted();
      context.assertCurrent();
      const result = await resolver(request, { ...controls, context });
      context.signal?.throwIfAborted();
      context.assertCurrent();
      if (activeContext !== context || activeOperation !== operation || closed) throw new Error('Signing parameter owner changed');
      return result;
    },
  });
  const exclusive = (context, work) => {
    const operation = { revision: null };
    const running = queue.then(async () => {
      context.signal?.throwIfAborted(); context.assertCurrent();
      if (closed) throw new Error('Signing publication owner closed');
      activeContext = context; activeOperation = operation;
      try {
        const result = await work(await source(), operation);
        context.signal?.throwIfAborted(); context.assertCurrent();
        return result;
      } catch (error) {
        if (operation.revision && ['parameters_pending', 'parameters_unavailable', 'parameter_publication_rejected', 'parameter_engine_unsupported'].includes(error?.code)) {
          error.neededRevision = operation.revision;
        }
        throw error;
      } finally { activeContext = null; activeOperation = null; }
    });
    queue = running.catch(() => undefined);
    return running;
  };
  return Object.freeze({
    source,
    operation: exclusive,
    async refresh(context, revision, { force = false } = {}) {
      if (!revision) return;
      await exclusive(context, async (current, operation) => {
        // Only admitted work may arm a deadline. Recheck inside the owner queue
        // so ordinary concurrent callers coalesce after a completed refresh.
        if (!force && now() < nextRefresh) return;
        operation.revision = revision;
        try {
          const result = await current.resolve(revision, { signal: context.signal, refresh: true });
          context.signal?.throwIfAborted(); context.assertCurrent();
          nextRefresh = now() + REFRESH_MS;
          return result;
        } catch (error) {
          context.signal?.throwIfAborted(); context.assertCurrent();
          const dependency = parameterDependency({ ...error, neededRevision: revision });
          if (dependency) nextRefresh = now() + dependency.retry_after_ms;
          throw error;
        }
      });
    },
    async prepareWait(context, wait) {
      validateParameterWait(wait);
      if (now() < wait.next_attempt_at) return false;
      await exclusive(context, (current, operation) => {
        operation.revision = wait.needed_revision;
        return current.resolve(wait.needed_revision, { signal: context.signal, refresh: true });
      });
      return true;
    },
    async scheduleWait(wait, jobId = wait.needed_revision) {
      validateParameterWait(wait);
      waits.set(jobId, wait);
      // The caller must have persisted its account-bound job before this call.
      const existing = await chromeApi.alarms?.get?.(PARAMETER_ALARM);
      const deadline = Math.min(now() + 300_000, Math.max(now() + 1_000, wait.next_attempt_at));
      if (!existing || existing.scheduledTime > deadline) await chromeApi.alarms?.create?.(PARAMETER_ALARM, { when: deadline });
    },
    async clearWait(jobId) {
      waits.delete(jobId);
      if (waits.size === 0) await chromeApi.alarms?.clear?.(PARAMETER_ALARM);
    },
    async drain() { await queue; await persistence.drain(); },
    async suspend() { waits.clear(); await chromeApi.alarms?.clear?.(PARAMETER_ALARM); },
    async close() {
      closed = true;
      if (sourcePromise) await (await sourcePromise).close();
      await queue;
      await persistence.drain();
    },
  });
}

export async function persistParameterWait({ error, job, save, owner, clock = Date.now }) {
  const dependency = parameterDependency(error);
  if (!dependency) return false;
  const wait = { reason: dependency.reason, needed_revision: dependency.needed_revision,
    next_attempt_at: clock() + dependency.retry_after_ms };
  await save({ ...job, parameter_wait: wait });
  await owner?.scheduleWait(wait, job.job_id);
  return true;
}

export async function prepareSigningDependency({ signer, job, save, signal, clock = Date.now, reserveProof = null }) {
  if (!signer.prepare) return true;
  try {
    if (!await signer.prepare({ signal, parameterWait: job.parameter_wait ?? null, reserveProof })) {
      if (job.parameter_wait) await signer.parameterOwner?.scheduleWait(job.parameter_wait, job.job_id);
      return false;
    }
    signal?.throwIfAborted();
    if (job.parameter_wait) await save({ ...job, parameter_wait: null });
    await signer.parameterOwner?.clearWait(job.job_id);
    return true;
  } catch (error) {
    signal?.throwIfAborted();
    if (await persistParameterWait({ error, job, save, owner: signer.parameterOwner, clock })) return false;
    throw error;
  }
}
