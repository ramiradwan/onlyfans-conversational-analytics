import { parseAgentConfigGetRequest } from '../protocol/index.mjs';

const defaultScheduler = {
  setTimeout: (handler, delay) => setTimeout(handler, delay),
  clearTimeout: (handle) => clearTimeout(handle),
};

export class ConfigFetchError extends Error {
  constructor(code, detail, retryable = false, status = null) {
    super(detail);
    this.name = 'ConfigFetchError';
    this.code = code;
    this.retryable = retryable;
    this.status = status;
  }
}

export function createConfigHttpAdapter(options = {}) {
  const endpoint = options.endpoint ?? 'http://localhost:8000/api/v1/agent/config';
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const scheduler = options.scheduler ?? defaultScheduler;
  const timeoutMs = options.timeoutMs ?? 5_000;
  const abortControllerFactory =
    options.abortControllerFactory ?? (() => new AbortController());

  if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');

  return {
    async fetchConfig(context) {
      const request = parseAgentConfigGetRequest({
        operation: 'agent.config.get',
        protocol_version: '1',
        auth_ticket: context.authTicket,
        agent_installation_id: context.agentInstallationId,
        creator_account_id: context.creatorAccountId,
        current_etag: context.currentEtag,
        current_config_revision: context.currentConfigRevision,
        supported_config_schema_versions: context.supportedSchemaVersions,
      });
      const url = new URL(endpoint);
      for (const [key, value] of Object.entries(request)) {
        if (value === null) continue;
        if (Array.isArray(value)) {
          for (const item of value) url.searchParams.append(key, item);
        } else {
          url.searchParams.set(key, String(value));
        }
      }

      const controller = abortControllerFactory();
      const timeout = scheduler.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetchImpl(url.toString(), {
          method: 'GET',
          headers: request.current_etag
            ? { 'If-None-Match': request.current_etag }
            : {},
          signal: controller.signal,
          credentials: 'omit',
        });
        const etag = response.headers?.get?.('etag') ?? null;
        if (response.status === 304) return { status: 304, etag, document: null };
        let document = null;
        if (response.status === 200) {
          try {
            document = await response.json();
          } catch (error) {
            throw new ConfigFetchError(
              'invalid_response',
              'Brain returned a non-JSON configuration document',
              true,
            );
          }
        }
        return { status: response.status, etag, document };
      } catch (error) {
        if (error instanceof ConfigFetchError) throw error;
        if (error?.name === 'AbortError') {
          throw new ConfigFetchError(
            'timeout',
            'Agent configuration fetch timed out',
            true,
          );
        }
        throw new ConfigFetchError(
          'network_error',
          error?.message ?? 'Agent configuration fetch failed',
          true,
        );
      } finally {
        scheduler.clearTimeout(timeout);
      }
    },
  };
}

