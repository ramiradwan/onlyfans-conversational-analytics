import { parseAgentConfigDocumentResponse } from '../protocol/index.mjs';

const defaultScheduler = {
  setTimeout: (handler, delay) => setTimeout(handler, delay),
  clearTimeout: (handle) => clearTimeout(handle),
};

const clone = (value) => JSON.parse(JSON.stringify(value));

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalValue(value[key])]),
    );
  }
  return value;
}

export async function calculateConfigDigest(document, cryptoApi = globalThis.crypto) {
  if (!cryptoApi?.subtle) throw new Error('Web Crypto digest support is unavailable');
  const content = clone(document);
  delete content.digest;
  delete content.etag;
  const bytes = new TextEncoder().encode(JSON.stringify(canonicalValue(content)));
  const hash = await cryptoApi.subtle.digest('SHA-256', bytes);
  const hex = [...new Uint8Array(hash)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
  return `sha256:${hex}`;
}

export class ConfigActivationError extends Error {
  constructor(code, detail, outcome = 'rejected') {
    super(detail);
    this.name = 'ConfigActivationError';
    this.code = code;
    this.outcome = outcome;
  }
}

export class AtomicConfigActivator {
  constructor(initialDocument = null) {
    this.document = initialDocument === null ? null : clone(initialDocument);
  }

  async activate(document) {
    this.document = clone(document);
  }

  current() {
    return this.document === null ? null : clone(this.document);
  }
}

async function validateDocument(document, context) {
  if (document?.config_schema_version !== '1') {
    throw new ConfigActivationError(
      'unsupported_schema',
      `Unsupported configuration schema ${String(document?.config_schema_version)}`,
    );
  }

  let parsed;
  try {
    parsed = parseAgentConfigDocumentResponse(clone(document));
  } catch (error) {
    throw new ConfigActivationError(
      'invalid_document',
      error?.message ?? 'Configuration document failed protocol validation',
    );
  }
  if (parsed.creator_account_id !== context.creatorAccountId) {
    throw new ConfigActivationError(
      'identity_mismatch',
      'Configuration creator account does not match the Agent binding',
    );
  }
  if (
    context.expectedRevision !== null
    && parsed.config_revision !== context.expectedRevision
  ) {
    throw new ConfigActivationError(
      'revision_mismatch',
      'Configuration revision does not match the required revision',
    );
  }
  if (context.expectedDigest !== null && parsed.digest !== context.expectedDigest) {
    throw new ConfigActivationError(
      'signaled_digest_mismatch',
      'Configuration digest does not match config.available',
    );
  }
  if (
    context.responseEtag !== null
    && context.responseEtag.replace(/^W\//, '').replaceAll('"', '') !== parsed.etag
  ) {
    throw new ConfigActivationError(
      'etag_mismatch',
      'Configuration response ETag does not match the document',
    );
  }
  const digest = await calculateConfigDigest(parsed, context.cryptoApi);
  if (digest !== parsed.digest) {
    throw new ConfigActivationError(
      'digest_mismatch',
      'Configuration content digest verification failed',
    );
  }
  for (const rule of parsed.capture_policy.rules) {
    if (
      !rule.url_pattern.startsWith('/')
      || rule.url_pattern.includes('://')
      || /[\u0000-\u001f]/.test(rule.url_pattern)
    ) {
      throw new ConfigActivationError(
        'unsafe_capture_pattern',
        'Configuration contains an unsafe capture URL pattern',
      );
    }
  }
  const resourceCapabilities = {
    chats: 'capture.chats',
    messages: 'capture.messages',
    presence: 'capture.presence',
  };
  for (const rule of parsed.capture_policy.rules) {
    if (
      rule.enabled
      && !context.capabilities.includes(resourceCapabilities[rule.resource])
    ) {
      throw new ConfigActivationError(
        'unsupported_capability',
        `Configuration requires unsupported capability ${resourceCapabilities[rule.resource]}`,
      );
    }
  }
  if (
    parsed.command_policy.allowed_actions.includes('message.send')
    && !context.capabilities.includes('command.message.send')
  ) {
    throw new ConfigActivationError(
      'unsupported_capability',
      'Configuration requires unsupported capability command.message.send',
    );
  }
  return parsed;
}

async function bundledSafeDocument(creatorAccountId, cryptoApi) {
  const document = {
    operation: 'agent.config.document',
    protocol_version: '1',
    creator_account_id: creatorAccountId,
    config_revision: 'bundled-safe-1',
    config_schema_version: '1',
    digest: `sha256:${'0'.repeat(64)}`,
    etag: 'bundled-safe-1',
    issued_at: '2026-07-18T00:00:00Z',
    capture_policy: {
      observation_interval_seconds: 30,
      rules: [
        {
          resource: 'messages',
          url_pattern: '/api2/v2/chats/*/messages',
          enabled: false,
        },
      ],
    },
    command_policy: {
      allowed_actions: [],
      max_text_length: 1,
      require_idempotency: true,
    },
  };
  document.digest = await calculateConfigDigest(document, cryptoApi);
  return document;
}

export class AgentConfigClient {
  constructor(options) {
    this.identity = options.identity;
    this.creatorAccountId = options.creatorAccountId;
    this.authTicket = options.authTicket;
    this.http = options.http;
    this.persistence = options.persistence;
    this.activator = options.activator ?? new AtomicConfigActivator();
    this.reportApplied = options.reportApplied ?? (() => false);
    this.onUnauthorized = options.onUnauthorized ?? (() => {});
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.cryptoApi = options.cryptoApi ?? globalThis.crypto;
    this.supportedSchemaVersions = ['1'];
    this.capabilities = options.capabilities ?? [
      'capture.chats',
      'capture.messages',
      'capture.presence',
      'command.message.send',
    ];
    this.retryBaseMs = options.retryBaseMs ?? 1_000;
    this.retryMaxMs = options.retryMaxMs ?? 30_000;
    this.activeDocument = null;
    this.lastFailure = null;
    this.required = null;
    this.refreshPromise = null;
    this.retryTimer = null;
    this.retryAttempt = 0;
  }

  async initialize() {
    const saved = await this.persistence.loadAppliedConfig?.();
    if (saved !== null && saved !== undefined) {
      try {
        const validated = await validateDocument(saved, {
          creatorAccountId: this.creatorAccountId,
          expectedRevision: null,
          expectedDigest: null,
          responseEtag: null,
          capabilities: this.capabilities,
          cryptoApi: this.cryptoApi,
        });
        await this.activator.activate(validated);
        this.activeDocument = clone(validated);
        this.identity.appliedConfigRevision = validated.config_revision;
        this.lastFailure = null;
        return { source: 'persisted', document: clone(validated) };
      } catch (error) {
        this.lastFailure = error;
        this.identity.appliedConfigRevision = null;
      }
    }
    if (this.identity.appliedConfigRevision !== null) {
      this.lastFailure = new ConfigActivationError(
        'missing_persisted_config',
        'Applied configuration revision has no validated persisted document',
        'degraded',
      );
    }
    this.identity.appliedConfigRevision = null;
    await this.persistence.clearAppliedConfig?.();
    const bundled = await bundledSafeDocument(
      this.creatorAccountId,
      this.cryptoApi,
    );
    await this.activator.activate(bundled);
    this.activeDocument = clone(bundled);
    return { source: 'bundled', document: clone(bundled) };
  }

  healthSummary() {
    return this.lastFailure === null
      ? { status: 'healthy', detail: null }
      : { status: 'degraded', detail: this.lastFailure.message };
  }

  async requireConfig(requirement, options = {}) {
    this.required = {
      revision: requirement.required_config_revision,
      digest: requirement.digest ?? null,
    };
    const force = options.force ?? false;
    if (
      !force
      && this.identity.appliedConfigRevision === this.required.revision
    ) {
      return { status: 'current' };
    }
    if (this.refreshPromise !== null) {
      const pendingResult = await this.refreshPromise;
      if (
        pendingResult.status !== 'unauthorized'
        && this.identity.appliedConfigRevision !== this.required.revision
      ) {
        return this.requireConfig(
          {
            required_config_revision: this.required.revision,
            digest: this.required.digest,
          },
          { force: true },
        );
      }
      return pendingResult;
    }

    const targetRevision = this.required.revision;
    this.refreshPromise = this.#refresh();
    let result;
    try {
      result = await this.refreshPromise;
    } finally {
      this.refreshPromise = null;
    }
    if (
      result.status !== 'unauthorized'
      && this.required.revision !== targetRevision
    ) {
      return this.requireConfig(
        {
          required_config_revision: this.required.revision,
          digest: this.required.digest,
        },
        { force: true },
      );
    }
    return result;
  }

  async #refresh() {
    const required = { ...this.required };
    try {
      const result = await this.http.fetchConfig({
        authTicket: this.authTicket,
        agentInstallationId: this.identity.agentInstallationId,
        creatorAccountId: this.creatorAccountId,
        currentEtag: this.activeDocument?.etag ?? null,
        currentConfigRevision: this.identity.appliedConfigRevision,
        supportedSchemaVersions: this.supportedSchemaVersions,
      });
      if (result.status === 401 || result.status === 403) {
        const error = new ConfigActivationError(
          'unauthorized',
          'Brain rejected Agent configuration authentication',
          'degraded',
        );
        this.lastFailure = error;
        this.onUnauthorized(error);
        await this.#reportFailure(error);
        return { status: 'unauthorized' };
      }
      if (result.status >= 500) {
        throw new ConfigActivationError(
          'server_error',
          `Brain configuration fetch failed with HTTP ${result.status}`,
          'degraded',
        );
      }
      let candidate;
      if (result.status === 304) {
        if (this.activeDocument === null) {
          throw new ConfigActivationError(
            'invalid_304',
            'Brain returned 304 without a validated cached configuration',
          );
        }
        candidate = this.activeDocument;
      } else if (result.status === 200) {
        candidate = result.document;
      } else {
        throw new ConfigActivationError(
          'unexpected_status',
          `Unexpected configuration response status ${result.status}`,
          'degraded',
        );
      }
      const validated = await validateDocument(candidate, {
        creatorAccountId: this.creatorAccountId,
        expectedRevision: required.revision,
        expectedDigest: required.digest,
        responseEtag: result.etag,
        capabilities: this.capabilities,
        cryptoApi: this.cryptoApi,
      });
      await this.#activate(validated);
      return {
        status: result.status === 304 ? 'reused' : 'applied',
        document: clone(validated),
      };
    } catch (error) {
      const failure =
        error instanceof ConfigActivationError
          ? error
          : new ConfigActivationError(
              error?.code ?? 'fetch_failed',
              error?.message ?? 'Configuration fetch failed',
              'degraded',
            );
      this.lastFailure = failure;
      await this.#reportFailure(failure);
      this.#scheduleRetry();
      return { status: 'failed', error: failure };
    }
  }

  async #activate(document) {
    const previous = this.activeDocument === null
      ? null
      : clone(this.activeDocument);
    await this.activator.activate(document);
    try {
      await this.persistence.saveAppliedConfig(document);
    } catch (error) {
      if (previous !== null) await this.activator.activate(previous);
      throw new ConfigActivationError(
        'persistence_failed',
        error?.message ?? 'Could not persist the activated configuration',
        'degraded',
      );
    }
    this.activeDocument = clone(document);
    this.identity.appliedConfigRevision = document.config_revision;
    this.lastFailure = null;
    this.retryAttempt = 0;
    this.#clearRetry();
    this.reportApplied({
      config_revision: document.config_revision,
      digest: document.digest,
      outcome: 'applied',
      capabilities: this.#capabilityStatus('active', null),
    });
  }

  async #reportFailure(error) {
    if (this.activeDocument === null) return;
    this.reportApplied({
      config_revision: this.activeDocument.config_revision,
      digest: this.activeDocument.digest,
      outcome: error.outcome ?? 'degraded',
      capabilities: this.#capabilityStatus('degraded', error.message),
    });
  }

  #capabilityStatus(status, detail) {
    return this.capabilities.map((capability) => ({
      capability,
      status,
      detail,
    }));
  }

  #scheduleRetry() {
    if (this.retryTimer !== null || this.required === null) return;
    const delay = Math.min(
      this.retryMaxMs,
      this.retryBaseMs * 2 ** this.retryAttempt,
    );
    this.retryAttempt += 1;
    this.retryTimer = this.scheduler.setTimeout(() => {
      this.retryTimer = null;
      void this.requireConfig(
        {
          required_config_revision: this.required.revision,
          digest: this.required.digest,
        },
        { force: true },
      );
    }, delay);
  }

  #clearRetry() {
    if (this.retryTimer !== null) this.scheduler.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
}

